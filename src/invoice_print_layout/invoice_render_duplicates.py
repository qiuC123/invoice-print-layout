"""Conservative same-batch invoice render identity using already saved evidence."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Any

from invoice_print_layout.glm_invoice_ai import labelled_invoice_numbers
from invoice_print_layout.invoice_checks import parse_tax_qr


IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}


def _number(entry: dict[str, Any], qr_value: str = '') -> tuple[str, bool]:
    number = str((entry.get('fields') or {}).get('invoice_number') or '').strip()
    numbers = set(labelled_invoice_numbers(str(entry.get('text') or '')))
    invalid = bool(number and not re.fullmatch(r'\d{8,30}', number))
    if number:
        numbers.add(number)
    for value in (entry.get('claim_url'), qr_value):
        qr = parse_tax_qr(value) if isinstance(value, str) else None
        if qr:
            numbers.add(qr['invoice_number'])
    conflict = (invalid or len(numbers) > 1 or
        'invoice_number' in (entry.get('qr_check') or {}).get('mismatches', []) or
        any(value.get('field') == 'invoice_number' for value in entry.get('field_conflicts', [])
            if isinstance(value, dict)) or
        any(('发票号码' in str(issue) and any(word in str(issue) for word in ('冲突', '不同', '不一致'))) or
            ('二维码与正文不一致' in str(issue) and '号码' in str(issue))
            for issue in entry.get('issues', [])))
    return (next(iter(numbers)) if len(numbers) == 1 else '', conflict)


def invoice_render_pairs(entries: list[dict[str, Any]], *,
                         qr_values: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Return unambiguous image/PDF ids; conflicting identities always stay visible."""
    batches: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if entry.get('batch') and not entry.get('duplicate_of'):
            batches[(entry['batch'], entry.get('source', ''), entry.get('project_id', ''))].append(entry)
    pairs = []
    for members in batches.values():
        pdfs = [(entry, _number(entry)) for entry in members
                if Path(entry.get('name', '')).suffix.lower() == '.pdf' and entry.get('role') == 'invoice'
                and entry.get('status') in {'received', 'review', 'linked'}]
        for entry in members:
            name = Path(entry.get('name', '')).name.lower()
            if (Path(name).suffix not in IMAGE_SUFFIXES or entry.get('item_id') or
                    entry.get('status') not in {'received', 'review', 'archived', 'unused', 'deleted'} or
                    entry.get('role') not in {'other', 'invoice'}):
                continue
            # A payment/order/receipt is an independent proof even if its filename matches.
            number, conflict = _number(entry, (qr_values or {}).get(entry['id'], ''))
            if conflict:
                continue
            matches = []
            for pdf, (pdf_number, pdf_conflict) in pdfs:
                if pdf_conflict or (number and pdf_number and number != pdf_number):
                    continue
                pdf_name = Path(pdf['name']).name.lower()
                if (Path(name).stem in {pdf_name, Path(pdf_name).stem} or
                        (number and pdf_number == number)):
                    matches.append(pdf['id'])
            if len(matches) == 1:
                pairs.append((entry['id'], matches[0]))
    return pairs
