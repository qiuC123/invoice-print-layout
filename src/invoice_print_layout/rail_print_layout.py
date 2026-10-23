"""Two native railway tickets per A4, retaining their existing printed size."""
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
from invoice_print_layout.report_types import validate_report_choice
from invoice_print_layout.workbench import ExpenseStore

MARGIN = 12.0
GAP = 12.0
CELL_HEIGHT = (A4_HEIGHT - 2 * MARGIN - GAP) / 2
PRINT_WIDTH = A4_WIDTH - 2 * MARGIN


@dataclass(frozen=True)
class _Ticket:
    document: pymupdf.Document
    path: Path
    digest: str


def _rail_item(item: dict[str, Any]) -> bool:
    if (item.get('category') != '高铁' or item.get('material_basis', 'standard') != 'standard' or
            not item.get('complete') or item.get('amount_pending') or not item.get('project_id') or
            item.get('legacy') or item.get('deleted') or item.get('merged_into') or
            item.get('returned_to_synthesis') or
            item.get('synthesis_material_state') in {'archived', 'deleted'} or
            item.get('stage') not in {'draft', 'submitted', 'reimbursed'}):
        return False
    if item.get('project_type'):
        choice = validate_report_choice(item['project_type'], item.get('project_name'))
        if choice['report_section'] != 'rail':
            return False
    elif item.get('report_section') not in {None, '', 'rail'}:
        return False
    attachments = item.get('attachments')
    return bool(isinstance(attachments, list) and len(attachments) == 1 and
                attachments[0].get('role') == 'invoice' and attachments[0].get('available'))


def _ticket(stack: ExitStack, store: ExpenseStore, item: dict[str, Any]) -> _Ticket | None:
    if not _rail_item(item):
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
        raise ValueError('高铁原件与保存时不一致，请重新检查材料')
    source = stack.enter_context(pymupdf.open(stream=payload, filetype='pdf'))  # type: ignore[no-untyped-call]
    if source.needs_pass or not source.is_pdf or len(source) != 1:
        return None
    source.bake(annots=True, widgets=False)
    page = source[0]
    if page.rotation or list(page.widgets()):
        return None
    boxes = (page.rect, page.mediabox, page.cropbox)
    if (any(box.is_empty or not all(math.isfinite(value) for value in box) for box in boxes) or
            any(abs(left - right) > .01 for box in boxes[1:] for left, right in zip(boxes[0], box, strict=True))):
        return None
    text = re.sub(r'\s+', '', page.get_text())
    if '铁路电子客票' not in text:
        return None
    width, height = page.rect.width, page.rect.height
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        return None
    baseline_scale = min(PRINT_WIDTH / width, (A4_HEIGHT - 2 * MARGIN) / height)
    half_scale = min(PRINT_WIDTH / width, CELL_HEIGHT / height)
    if abs(half_scale - baseline_scale) > 1e-8 or height * baseline_scale > CELL_HEIGHT + 1e-6:
        return None
    return _Ticket(source, path, digest)


def rail_pairable(store: ExpenseStore, item: dict[str, Any]) -> bool:
    """Read only: unknown, extra, tall or multiple proofs retain their old layout."""
    try:
        with ExitStack() as stack:
            return _ticket(stack, store, item) is not None
    except (KeyError, ValueError, OSError, RuntimeError, TypeError):
        return False


def append_rail_tickets(output: pymupdf.Document, store: ExpenseStore,
                        batch: list[dict[str, Any]]) -> None:
    """Append a validated adjacent rail batch; never reorder, crop or shrink.

    Callers fall back to the existing single-expense composer when
    ``rail_pairable`` is false. A source changing between that decision and
    composition rejects this batch without appending pages or changing items.
    """
    if (not isinstance(batch, list) or not batch or len(batch) > 200 or
            any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']
                for item in batch) or len({item['id'] for item in batch}) != len(batch)):
        raise ValueError('请选择1至200笔不重复的相邻高铁费用')
    project_id = batch[0].get('project_id')
    if not project_id or any(item.get('project_id') != project_id for item in batch):
        raise ValueError('高铁票只能在同一项目和报销子项内上下合排')
    current = {item['id']: item for item in store.get_many([item['id'] for item in batch])}
    if any(item.get('version') != current[item['id']]['version'] or
           current[item['id']].get('project_id') != project_id or not _rail_item(current[item['id']])
           for item in batch):
        raise ValueError('高铁费用或材料已变化，请刷新后重新生成打印页')
    start = len(output)
    with ExitStack() as stack:
        tickets = []
        for item in batch:
            ticket = _ticket(stack, store, item)
            if ticket is None:
                raise ValueError('高铁材料不能保持原字号上下合排，请使用原单笔排版')
            tickets.append(ticket)
        with pymupdf.open() as staged, pymupdf.open() as baseline:  # type: ignore[no-untyped-call]
            for index, ticket in enumerate(tickets):
                original = baseline.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                original.show_pdf_page(pymupdf.Rect(MARGIN, MARGIN, A4_WIDTH - MARGIN, A4_HEIGHT - MARGIN),  # type: ignore[no-untyped-call]
                                       ticket.document, 0, keep_proportion=True)
                if index % 2 == 0:
                    staged.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                top = MARGIN + (index % 2) * (CELL_HEIGHT + GAP)
                staged[-1].show_pdf_page(pymupdf.Rect(MARGIN, top, A4_WIDTH - MARGIN, top + CELL_HEIGHT),  # type: ignore[no-untyped-call]
                                         ticket.document, 0, keep_proportion=True)
            if not _same_printed_content(baseline, staged):
                raise ValueError('高铁合排的内容或打印尺寸未能核对，保留原单笔排版')
            current = {item['id']: item for item in store.get_many([item['id'] for item in batch])}
            if (any(item.get('version') != current[item['id']]['version'] for item in batch) or
                    any(hashlib.sha256(ticket.path.read_bytes()).hexdigest() != ticket.digest for ticket in tickets)):
                raise ValueError('生成期间高铁费用或原件已变化，请刷新后重新生成')
            output.insert_pdf(staged)  # type: ignore[no-untyped-call]
    for index, item in enumerate(batch):
        item['pages'] = str(start + index // 2 + 1)
        position = '上半页' if index % 2 == 0 else '下半页'
        item['layout'] = ('高铁12306发票：同项目同报表子项按原序A4上下两张；'
                          '原页完整保留，打印宽度和字号保持，不裁切不旋转；本笔位于' + position)
        if len(batch) % 2 and index == len(batch) - 1:
            item['layout'] += '，末页单张'
