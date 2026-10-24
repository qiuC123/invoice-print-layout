"""Read-only identification of historical trip sheets from their native PDF text."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from functools import lru_cache
from typing import Any

import pymupdf

from invoice_print_layout.documents import classify_text, compact_text, _freight_evidence
from invoice_print_layout.models import DocumentKind
from invoice_print_layout.file_cache import FileSignature, file_signature


def invoice_trip_required(item: dict[str, Any]) -> bool:
    """Use the taxi rule while preserving explicit payment-only/history paths."""
    return (item.get('category') == '打车' and not item.get('legacy') and
            item.get('material_basis', 'standard') != 'payment_only')


def native_trip_pdf(path: Path, expected_digest: str = '') -> bool:
    try:
        signature = file_signature(path)
        return _native_trip_pdf(signature, expected_digest) and file_signature(path) == signature
    except (ValueError, OSError, RuntimeError):
        return False


@lru_cache(maxsize=512)
def _native_trip_pdf(signature: FileSignature, expected_digest: str) -> bool:
    """Recognize a trip proof without filename, amount, OCR or ledger changes.

    A supplied digest protects historical role projection from accepting a
    replaced original. Mixed invoice/trip packages and freight documents are
    left for explicit classification rather than treated as a trip proof.
    """
    path = Path(signature[0])
    try:
        if path.suffix.lower() != '.pdf' or not path.is_file():
            return False
        content = path.read_bytes()
        if expected_digest and hashlib.sha256(content).hexdigest() != expected_digest:
            return False
        with pymupdf.open(stream=content, filetype='pdf') as document:  # type: ignore[no-untyped-call]
            if document.needs_pass or not len(document):
                return False
            text = '\n'.join(page.get_text() for page in document)
        rows = [compact_text(row).upper() for row in text.splitlines() if row.strip()]
        if any(re.fullmatch(r'(?:电子发票(?:[（(].*[）)])?|增值税(?:电子)?(?:普通|专用)发票|'
                            r'ELECTRONICINVOICE|铁路电子客票)', row) for row in rows):
            return False
        return _freight_evidence(text) is None and classify_text(text) is DocumentKind.TRIP
    except (ValueError, OSError, RuntimeError):
        return False
