"""Complete vector proofs, packed at baseline scale within each expense only."""
from __future__ import annotations

from contextlib import ExitStack
from collections import defaultdict
from pathlib import Path
from typing import Any

import pymupdf

from invoice_print_layout.layout import A4_WIDTH, A4_HEIGHT
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.print_packing import pack_fixed_sizes


def payment_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    from invoice_print_layout.report_snapshot import material_batches, snapshot
    return [group for group in material_batches(snapshot(items))
            if group[0].get('material_basis') == 'payment_only']


def supplementary_attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Select only explicitly grouped supporting originals, never optional records."""
    matching = item.get('material_match') or {}
    if not isinstance(matching, dict):
        raise ValueError('补充材料关联记录无效，请检查')
    ids = matching.get('support_attachment_ids', [])
    if (not isinstance(ids, list) or any(not isinstance(key, str) or not key for key in ids)
            or len(ids) != len(set(ids))):
        raise ValueError('补充材料关联记录无效，请检查')
    if not ids:
        return []
    attachments = {attachment['id']: attachment for attachment in item['attachments']}
    selected = []
    for key in ids:
        attachment = attachments.get(key)
        if attachment is None or not attachment['available']:
            raise ValueError('已关联的补充材料原件缺失，请补齐后打印')
        if attachment['role'] in {'invoice', 'receipt', 'payment', 'package'}:
            raise ValueError('补充材料不能替代发票或付款凭证，请检查用途')
        selected.append(attachment)
    return sorted(selected, key=lambda attachment: (attachment['name'], attachment['id']))


def _append_payments_base(output: pymupdf.Document, store: ExpenseStore, items: list[dict[str, Any]]) -> None:
    slot = 0
    cell_w, cell_h = (A4_WIDTH - 36) / 2, (A4_HEIGHT - 36) / 2
    for item in items:
        page_numbers: set[int] = set()
        attachments = [a for a in item['attachments'] if a['role'] == 'payment' and a['available']]
        if not attachments:
            raise ValueError('支付凭证缺失')
        for attachment in attachments:
            path: Path = store.attachment_path(attachment['id'])[0]
            with pymupdf.open(path) as original:
                if original.needs_pass:
                    raise ValueError('支付凭证已加密')
                if original.is_pdf:
                    original.bake(annots=True, widgets=False)
                    source = original
                else:
                    source = pymupdf.open('pdf', original.convert_to_pdf())
                try:
                    for page in range(len(source)):
                        if slot == 0:
                            output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                        left = 12 + (slot % 2) * (cell_w + 12)
                        top = 12 + (slot // 2) * (cell_h + 12)
                        output[-1].show_pdf_page(pymupdf.Rect(left, top, left + cell_w, top + cell_h), source, page, keep_proportion=True)
                        page_numbers.add(len(output))
                        slot = (slot + 1) % 4
                finally:
                    if source is not original:
                        source.close()
        item['pages'] = ','.join(str(p) for p in sorted(page_numbers))
        item['layout'] = '仅支付凭证：当前费用A4纸2×2，不同费用分别起页，完整缩放不裁切'


def _append_invoice_payments_base(output: pymupdf.Document, store: ExpenseStore, item: dict[str, Any],
                            *, allow_incomplete: bool = False) -> None:
    """Keep each matter together: half-sheet vouchers and quarter-sheet payments."""
    vouchers = sorted((a for a in item['attachments']
                       if a['role'] in {'invoice', 'receipt'} and a['available']), key=lambda a: a['name'])
    payments = sorted((a for a in item['attachments']
                       if a['role'] == 'payment' and a['available']), key=lambda a: a['name'])
    supplements = supplementary_attachments(item)
    if (not vouchers or not payments) and not allow_incomplete:
        raise ValueError('发票／收据或付款截图缺失')
    if not vouchers and not payments:
        raise ValueError('没有可预览的发票、收据或付款截图')
    cell_w, cell_h = (A4_WIDTH - 36) / 2, (A4_HEIGHT - 36) / 2
    half = 0
    payment_slot = 0
    page_numbers: set[int] = set()
    for attachment in vouchers + payments + supplements:
        is_payment = attachment['role'] == 'payment'
        is_supplement = attachment['role'] not in {'invoice', 'receipt', 'payment'}
        path: Path = store.attachment_path(attachment['id'])[0]
        with pymupdf.open(path) as original:
            if original.needs_pass:
                raise ValueError('附件已加密，请提供可读取的原始文件')
            if original.is_pdf:
                original.bake(annots=True, widgets=False)
                source = original
            else:
                source = pymupdf.open('pdf', original.convert_to_pdf())
            try:
                if not len(source):
                    raise ValueError('附件没有可读取页面')
                for page in range(len(source)):
                    if is_supplement:
                        target = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                        target.show_pdf_page(pymupdf.Rect(12, 12, A4_WIDTH - 12, A4_HEIGHT - 12),
                                             source, page, keep_proportion=True)
                        page_numbers.add(len(output))
                        continue
                    if half == 0 and payment_slot == 0:
                        output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                    top = 12 + half * (cell_h + 12)
                    left = 12 + payment_slot * (cell_w + 12) if is_payment else 12
                    width = cell_w if is_payment else A4_WIDTH - 24
                    output[-1].show_pdf_page(pymupdf.Rect(left, top, left + width, top + cell_h),
                                             source, page, keep_proportion=True)
                    page_numbers.add(len(output))
                    if is_payment:
                        payment_slot = (payment_slot + 1) % 2
                        if payment_slot == 0:
                            half = (half + 1) % 2
                    else:
                        half = (half + 1) % 2
            finally:
                if source is not original:
                    source.close()
    item['pages'] = ','.join(str(p) for p in sorted(page_numbers))
    item['layout'] = '发票／收据＋付款截图：票据A4纸1×2，付款截图2×2，同一事项合并打印，完整缩放不裁切'
    if supplements:
        item['layout'] += '；已关联补充材料完整原件逐页A4，不另计费用'


def _content_metrics(document: pymupdf.Document) -> dict[tuple[Any, ...], list[tuple[float, float]]]:
    """Check retained native text sizes, bitmap dimensions and vector paint."""
    metrics: dict[tuple[Any, ...], list[tuple[float, float]]] = defaultdict(list)
    for page in (document[number] for number in range(len(document))):
        for block in page.get_text('rawdict')['blocks']:
            if block['type'] != 0:
                continue
            for line in block['lines']:
                for span in line['spans']:
                    for char in span['chars']:
                        if not char['c'].isspace():
                            metrics[('text', span['font'], char['c'])].append((span['size'], span['size']))
        for image in page.get_image_info(hashes=True):
            rect = pymupdf.Rect(image['bbox'])
            metrics[('image', image['digest'], image['width'], image['height'])].append((rect.width, rect.height))
        for drawing in page.get_drawings():
            rect = drawing['rect']
            key = ('vector', drawing['type'], tuple(command[0] for command in drawing['items']),
                   drawing.get('color'), drawing.get('fill'))
            metrics[key].append((rect.width, rect.height))
    return metrics


def _same_printed_content(baseline: pymupdf.Document, candidate: pymupdf.Document) -> bool:
    before, after = _content_metrics(baseline), _content_metrics(candidate)
    if before.keys() != after.keys():
        return False
    return all(len(before[key]) == len(after[key]) and all(
        abs(w - new_w) < .01 and abs(h - new_h) < .01
        for (w, h), (new_w, new_h) in zip(sorted(before[key]), sorted(after[key]), strict=True))
        for key in before)


def _append_at_baseline_size(output: pymupdf.Document, baseline: pymupdf.Document,
                             store: ExpenseStore, item: dict[str, Any],
                             attachments: list[dict[str, Any]]) -> None:
    """Adopt a new placement only when it saves pages at unchanged scale."""
    chosen = baseline
    with ExitStack() as stack:
        if len(baseline) > 1:
            sources: list[tuple[pymupdf.Document, int]] = []
            sizes = []
            for attachment in attachments:
                path = store.attachment_path(attachment['id'])[0]
                original = stack.enter_context(pymupdf.open(path))
                if original.needs_pass:
                    raise ValueError('附件已加密，请提供可读取的原始文件')
                if original.is_pdf:
                    original.bake(annots=True, widgets=False)
                    source = original
                else:
                    source = stack.enter_context(pymupdf.open('pdf', original.convert_to_pdf()))
                role = attachment['role']
                width = (A4_WIDTH - 36) / 2 if role == 'payment' else A4_WIDTH - 24
                height = (A4_HEIGHT - 36) / 2 if role in {'invoice', 'receipt', 'payment'} else A4_HEIGHT - 24
                for number in range(len(source)):
                    page = source[number]
                    scale = min(width / page.rect.width, height / page.rect.height)
                    sizes.append((page.rect.width * scale, page.rect.height * scale))
                    sources.append((source, number))
            placements = pack_fixed_sizes(sizes, A4_WIDTH - 24, A4_HEIGHT - 24, gap=4.0)
            count = max((placement.page for placement in placements or []), default=-1) + 1
            if placements and 0 < count < len(baseline):
                candidate = stack.enter_context(pymupdf.open())
                for _ in range(count):
                    candidate.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                for placement in placements:
                    source, number = sources[placement.index]
                    left, top = 12 + placement.x, 12 + placement.y
                    candidate[placement.page].show_pdf_page(
                        pymupdf.Rect(left, top, left + placement.width, top + placement.height),
                        source, number, keep_proportion=True)
                # Extraction can split/merge spans after moving a proof. Compare
                # characters and physical sizes, not artificial span boundaries.
                if _same_printed_content(baseline, candidate):
                    chosen = candidate
                    item['layout'] += f'；同笔材料自动重排{len(baseline)}→{count}页，原印刷大小不变、原页完整保留'
        start = len(output) + 1
        output.insert_pdf(chosen)
        item['pages'] = ','.join(str(number) for number in range(start, len(output) + 1))


def append_payments(output: pymupdf.Document, store: ExpenseStore, items: list[dict[str, Any]]) -> None:
    """Each expense starts a fresh page, including when given a batch of fees."""
    for item in items:
        with pymupdf.open() as baseline:
            _append_payments_base(baseline, store, [item])
            attachments = [a for a in item['attachments'] if a['role'] == 'payment' and a['available']]
            _append_at_baseline_size(output, baseline, store, item, attachments)


def append_invoice_payments(output: pymupdf.Document, store: ExpenseStore, item: dict[str, Any],
                            *, allow_incomplete: bool = False) -> None:
    with pymupdf.open() as baseline:
        _append_invoice_payments_base(baseline, store, item, allow_incomplete=allow_incomplete)
        vouchers = sorted((a for a in item['attachments']
                           if a['role'] in {'invoice', 'receipt'} and a['available']), key=lambda a: a['name'])
        payments = sorted((a for a in item['attachments']
                           if a['role'] == 'payment' and a['available']), key=lambda a: a['name'])
        _append_at_baseline_size(output, baseline, store, item, vouchers + payments + supplementary_attachments(item))
