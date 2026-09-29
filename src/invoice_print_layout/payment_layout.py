"""Four-up full-content payment proofs, grouped only within the same project."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf

from invoice_print_layout.layout import A4_WIDTH, A4_HEIGHT
from invoice_print_layout.workbench import ExpenseStore


def payment_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if item.get('material_basis') == 'payment_only':
            groups.setdefault(str(item.get('project_id') or item['project']), []).append(item)
    return list(groups.values())


def append_payments(output: pymupdf.Document, store: ExpenseStore, items: list[dict[str, Any]]) -> None:
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
        item['layout'] = '仅支付凭证：A4纸2×2，每页最多4张，同项目共用页面'
