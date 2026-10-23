"""Adjacent native single invoices on A4 at their existing full-sheet scale."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
from typing import Any

import pymupdf

from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.payment_layout import _same_printed_content
from invoice_print_layout.rail_print_layout import append_rail_tickets, rail_pairable
from invoice_print_layout.report_types import infer_report_choice, validate_report_choice
from invoice_print_layout.workbench import CATEGORIES, ExpenseStore

MARGIN = 12.0
GAP = 12.0
PRINT_WIDTH = A4_WIDTH - MARGIN * 2
CELL_HEIGHT = (A4_HEIGHT - MARGIN * 2 - GAP) / 2
_INVOICE_HEADING = re.compile(
    r'电子发票[（(](?:增值税)?(?:普通|专用)发票[）)]|'
    r'(?:增值税)?电子(?:普通|专用)发票|增值税(?:普通|专用)发票')


@dataclass(frozen=True)
class _Invoice:
    document: pymupdf.Document
    path: Path
    digest: str


def _item_key(item: dict[str, Any]) -> tuple[str, ...] | None:
    if (item.get('category') not in CATEGORIES or item.get('material_basis', 'standard') != 'standard' or
            not item.get('complete') or item.get('amount_pending') or item.get('missing_acknowledged') or
            not isinstance(item.get('project_id'), str) or not item['project_id'] or
            not isinstance(item.get('version'), str) or not item['version'] or
            item.get('legacy') or item.get('deleted') or item.get('merged_into') or
            item.get('returned_to_synthesis') or
            item.get('synthesis_material_state') in {'archived', 'deleted'} or
            item.get('stage') not in {'draft', 'submitted', 'reimbursed'}):
        return None
    attachments = item.get('attachments')
    if not (isinstance(attachments, list) and len(attachments) == 1 and
            isinstance(attachments[0], dict) and attachments[0].get('role') == 'invoice' and
            attachments[0].get('available')):
        return None
    choice = (validate_report_choice(item['project_type'], item.get('project_name'))
              if item.get('project_type') else infer_report_choice(item))
    if item.get('project_type') and choice['category'] != item['category']:
        return None
    subitem = choice['project_name']
    if choice['project_type'] in {'materials', 'other'}:
        subitem = str(item.get('report_group') or (
            choice['project_name'] if item.get('project_type') or
            choice['project_type'] == 'materials' and choice['project_name'] == '雨衣采购' else item['id']))
    return (item['project_id'], item['category'], choice['report_section'], choice['project_type'], subitem)


def _invoice(stack: ExitStack, store: ExpenseStore, item: dict[str, Any]) -> _Invoice | None:
    if _item_key(item) is None:
        return None
    attachment = item['attachments'][0]
    with store.connect() as db:
        row = db.execute('SELECT item_id,role,path,digest FROM attachments WHERE id=?',
                         (attachment['id'],)).fetchone()
    path, _ = store.attachment_path(attachment['id'])
    if (row is None or row['item_id'] != item['id'] or row['role'] != 'invoice' or
            Path(row['path']).resolve() != path or path.suffix.lower() != '.pdf'):
        return None
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if not row['digest'] or digest != row['digest']:
        raise ValueError('发票原件与保存时不一致，请重新核对材料')
    source = stack.enter_context(pymupdf.open(stream=payload, filetype='pdf'))  # type: ignore[no-untyped-call]
    if (source.needs_pass or source.is_encrypted or not source.is_pdf or len(source) != 1 or
            (source.metadata or {}).get('encryption')):
        return None
    page = source[0]
    if page.rotation or list(page.widgets()):
        return None
    boxes = (page.rect, page.mediabox, page.cropbox)
    if (any(box.is_empty or not all(math.isfinite(value) for value in box) for box in boxes) or
            any(abs(left - right) > .01 for box in boxes[1:] for left, right in zip(boxes[0], box, strict=True))):
        return None
    width, height = page.rect.width, page.rect.height
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        return None
    header_rect = pymupdf.Rect(0, 0, width, height * .45)  # type: ignore[no-untyped-call]
    heading = re.sub(r'\s+', '', page.get_text(clip=header_rect))
    if not _INVOICE_HEADING.search(heading):
        return None
    full_scale = min(PRINT_WIDTH / width, (A4_HEIGHT - 2 * MARGIN) / height)
    half_scale = min(PRINT_WIDTH / width, CELL_HEIGHT / height)
    if abs(full_scale - half_scale) > 1e-8 or height * full_scale > CELL_HEIGHT + 1e-6:
        return None
    source.bake(annots=True, widgets=False)
    return _Invoice(source, path, digest)


def invoice_pairable(store: ExpenseStore, item: dict[str, Any]) -> bool:
    """Read only; uncertain identity, layout, ownership or extra materials fail closed."""
    try:
        if _item_key(item) is None:
            return False
        if item['category'] == '高铁':
            return rail_pairable(store, item)
        with ExitStack() as stack:
            return _invoice(stack, store, item) is not None
    except (KeyError, ValueError, OSError, RuntimeError, TypeError):
        return False


def _current(store: ExpenseStore, batch: list[dict[str, Any]], key: tuple[str, ...]) -> None:
    current = {item['id']: item for item in store.get_many([item['id'] for item in batch])}
    if any(item.get('version') != current[item['id']]['version'] or
           _item_key(current[item['id']]) != key for item in batch):
        raise ValueError('费用或发票材料已变化，请刷新后重新生成打印页')


def append_invoice_pairs(output: pymupdf.Document, store: ExpenseStore,
                         batch: list[dict[str, Any]]) -> None:
    """Append only a proven same-project/category/subitem batch, in supplied order."""
    if (not isinstance(batch, list) or not batch or len(batch) > 200 or
            any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                for item in batch) or len({item['id'] for item in batch}) != len(batch)):
        raise ValueError('请选择1至200笔不重复的相邻发票费用')
    try:
        key = _item_key(batch[0])
        if key is None or any(_item_key(item) != key for item in batch):
            raise ValueError('只能在同一项目、费用类别和报表子项内上下合排发票')
        _current(store, batch, key)
    except (KeyError, TypeError) as error:
        raise ValueError('费用或发票材料已变化，请刷新后重新生成打印页') from error
    if batch[0]['category'] == '高铁':
        append_rail_tickets(output, store, batch)
        return
    start = len(output)
    with ExitStack() as stack:
        invoices = []
        for item in batch:
            invoice = _invoice(stack, store, item)
            if invoice is None:
                raise ValueError('发票不能在保持完整原页和原字号时上下合排，请保留原单笔排版')
            invoices.append(invoice)
        with pymupdf.open() as staged, pymupdf.open() as baseline:  # type: ignore[no-untyped-call]
            for index, invoice in enumerate(invoices):
                original = baseline.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                original.show_pdf_page(pymupdf.Rect(MARGIN, MARGIN, A4_WIDTH - MARGIN, A4_HEIGHT - MARGIN),  # type: ignore[no-untyped-call]
                                       invoice.document, 0, keep_proportion=True)
                if index % 2 == 0:
                    staged.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                top = MARGIN + index % 2 * (CELL_HEIGHT + GAP)
                staged[-1].show_pdf_page(pymupdf.Rect(MARGIN, top, A4_WIDTH - MARGIN, top + CELL_HEIGHT),  # type: ignore[no-untyped-call]
                                         invoice.document, 0, keep_proportion=True)
            if not _same_printed_content(baseline, staged):
                raise ValueError('发票合排的完整内容或打印尺寸未能核对，保留原单笔排版')
            _current(store, batch, key)
            if any(hashlib.sha256(invoice.path.read_bytes()).hexdigest() != invoice.digest for invoice in invoices):
                raise ValueError('生成期间发票原件已变化，请刷新后重新生成打印页')
            output.insert_pdf(staged)  # type: ignore[no-untyped-call]
    for index, item in enumerate(batch):
        item['pages'] = str(start + index // 2 + 1)
        item['layout'] = ('同项目同类别同报表子项单发票：按原序A4上下两张；原页完整保留，打印宽度和字号保持，'
                          '不裁切不旋转；本笔位于' + ('上半页' if index % 2 == 0 else '下半页'))
        if len(batch) % 2 and index == len(batch) - 1:
            item['layout'] += '，末页单张'
