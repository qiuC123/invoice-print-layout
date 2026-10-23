"""Explicit courier material rules and read-only native waybill recognition."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any

import pymupdf


_COURIER_NAMES = frozenset({
    '顺丰', '顺丰快递', '顺丰快递费', '顺丰速运', '顺丰速运费', '顺丰同城', '顺丰同城配送',
    '快递', '快递费', '快递运费', '同城配送', '同城配送费',
})


def invoice_waybill_required(item: dict[str, Any]) -> bool:
    """Use confirmed courier classification, preserving explicit old exceptions.

    A free-form title, filename, amount or merchant never classifies an ordinary
    ``其他`` expense. The optional boolean is a view-derived compatibility field.
    """
    if item.get('legacy') or item.get('material_basis', 'standard') == 'payment_only':
        return False
    if (item.get('courier_waybill_required') is True or item.get('category') == '顺丰' or
            item.get('report_section') == 'sf'):
        return True
    return (item.get('project_type') in {'other', '其他'} and
            re.sub(r'\s+', '', str(item.get('project_name') or '')) in _COURIER_NAMES)


def native_waybill_pdf(path: Path, expected_digest: str = '') -> bool:
    """Recognize verified SF/same-city native detail without changing its role.

    Mixed invoice/detail packages, image-only PDFs and changed source bytes are
    left for explicit review. No filename, OCR, amount-only or cloud guess is
    used as evidence of the document type.
    """
    try:
        if path.suffix.lower() != '.pdf' or not path.is_file():
            return False
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if expected_digest and digest != expected_digest:
            return False
        with pymupdf.open(stream=payload, filetype='pdf') as document:  # type: ignore[no-untyped-call]
            if document.needs_pass or not document.is_pdf or not len(document):
                return False
            rows = [re.sub(r'\s+', '', line) for page in document
                    for line in page.get_text().splitlines() if line.strip()]
        if ('价税合计' in ''.join(rows) or any(re.fullmatch(
                r'(?:电子发票(?:[（(].*[）)])?|增值税(?:电子)?(?:普通|专用)发票|ELECTRONICINVOICE)',
                row, re.I) for row in rows)):
            return False
        from invoice_print_layout.courier import inspect_courier_pdf
        inspected = inspect_courier_pdf(path)
        return (not inspected.is_invoice and inspected.sha256 == digest and
                hashlib.sha256(path.read_bytes()).hexdigest() == digest)
    except (ValueError, OSError, RuntimeError):
        return False
