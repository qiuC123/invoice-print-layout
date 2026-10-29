"""Native text dates shared with the existing rail/trip inspectors; no OCR or writes."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re

import pymupdf

from invoice_print_layout.documents import compact_text, parse_invoice_date, PdfInspectionError


def rail_departure_dates(text: str) -> list[str]:
    return sorted({f'{int(y):04d}-{int(m):02d}-{int(d):02d}' for y, m, d in
                   re.findall(r'(\d{4})年(\d{1,2})月(\d{1,2})日\d{1,2}:\d{2}开', compact_text(text))})


def trip_date_span(text: str) -> tuple[str, str]:
    match = re.search(r'行程起止日期[:：]?(\d{4}-\d{2}-\d{2})至(\d{4}-\d{2}-\d{2})', compact_text(text))
    start, end = match.groups() if match else ('', '')
    if start:
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end:
            raise PdfInspectionError('行程日期范围无效')
    return start, end


def attachment_dates(path: Path) -> list[str]:
    if path.suffix.lower() != '.pdf':
        return []
    try:
        with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
            text = '\n'.join(str(page.get_text()) for page in pdf)
        compact = compact_text(text)
        if '铁路电子客票' in compact:
            return [date.fromisoformat(day).isoformat() for day in rail_departure_dates(text)]
        if '行程单' in compact or 'TRIPTABLE' in compact.upper():
            return sorted({day for day in trip_date_span(text) if day})
        return [parse_invoice_date(text).isoformat()]
    except (OSError, RuntimeError, ValueError):
        return []
