"""Local-only invoice QR checks; issue date is never an expense date."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any

import pymupdf
from invoice_print_layout.mail_search import qr_value


def parse_tax_qr(value: str) -> dict[str, str] | None:
    parts = [part.strip() for part in value.split(',')]
    if (not 7 <= len(parts) <= 12 or parts[0] != '01' or not re.fullmatch(r'\d{2}', parts[1]) or
            not re.fullmatch(r'(?:\d{10}|\d{12})?', parts[2]) or not re.fullmatch(r'\d{8,20}', parts[3]) or
            not re.fullmatch(r'(?:0|[1-9]\d*)(?:\.\d{1,2})?', parts[4]) or
            not re.fullmatch(r'(?:\d{8}|\d{4}-\d{2}-\d{2})', parts[5]) or
            not re.fullmatch(r'\d{0,20}', parts[6])):
        return None
    try:
        date = datetime.strptime(parts[5], '%Y-%m-%d' if '-' in parts[5] else '%Y%m%d').date().isoformat()
    except ValueError:
        return None
    return {'type': parts[1], 'code': parts[2], 'invoice_number': parts[3], 'amount': parts[4], 'issue_date': date}


def issue_date(page: Any) -> str:
    text = page.get_text()
    for label in page.search_for('开票日期'):
        words = [word for word in page.get_text('words') if word[0] >= label.x1 - 2 and
                 abs((word[1]+word[3]-label.y0-label.y1)/2) < max(label.height, word[3]-word[1])*.65]
        text += '\n开票日期：' + ''.join(str(word[4]) for word in sorted(words, key=lambda w: w[0]))
    values = set()
    for match in re.finditer(r'(?:开票日期|INVOICE\s*DATE)\s*[:：]?\s*(\d{4})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})', text, re.I):
        try:
            year, month, day = (int(value) for value in match.groups())
            values.add(datetime(year, month, day).date().isoformat())
        except ValueError:
            pass
    return next(iter(values)) if len(values) == 1 else ''


def _multiple(payload: bytes) -> bool:
    import cv2
    import numpy as np
    pixels = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if pixels is None:
        return False
    scale = min(1.0, 1600 / max(pixels.shape))
    if scale != 1:
        pixels = cv2.resize(pixels, (round(pixels.shape[1]*scale), round(pixels.shape[0]*scale)))
    _, points = cv2.QRCodeDetector().detectMulti(pixels)
    return points is not None and len(points) > 1


def read_tax_qr(page: Any) -> dict[str, str] | None:
    decoded = []
    for image in page.get_images(full=True)[:24]:
        data = page.parent.extract_image(image[0])
        if data and data['width'] * data['height'] <= 16_000_000:
            value = qr_value(data['image'])
            if value:
                if len(page.get_image_rects(image[0])) > 1:
                    return None
                decoded.append(value)
    if len(decoded) > 1:
        return None
    full = page.get_pixmap(dpi=200, alpha=False).tobytes('png')
    if _multiple(full):
        return None
    if decoded:
        return parse_tax_qr(decoded[0])
    region = pymupdf.Rect(page.rect.x0, page.rect.y0, page.rect.x0 + page.rect.width*.6,
                         page.rect.y0 + page.rect.height*.55)  # type: ignore[no-untyped-call]
    cropped = page.get_pixmap(dpi=300, clip=region, alpha=False).tobytes('png')
    return parse_tax_qr(qr_value(cropped) or qr_value(full))


def compare_qr(qr: dict[str, str] | None, fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {'status': 'unreadable', 'checked': [], 'mismatches': [], 'not_compared': [], 'type': ''}
    if not qr:
        return result
    from invoice_print_layout.workbench import amount_cents
    result['type'] = qr['type']
    for key in ('invoice_number', 'amount', 'issue_date'):
        actual = fields.get(key)
        if not actual:
            result['not_compared'].append(key)
            continue
        same = amount_cents(actual) == amount_cents(qr[key]) if key == 'amount' else str(actual).strip() == qr[key]
        if key == 'amount' and not same and qr['type'] not in {'31', '32'}:
            result['not_compared'].append(key)
        else:
            result['checked' if same else 'mismatches'].append(key)
    result['status'] = 'mismatch' if result['mismatches'] else 'partial' if result['not_compared'] else 'match'
    return result
