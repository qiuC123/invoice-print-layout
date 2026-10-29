from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from PIL import Image

from invoice_print_layout.documents import (
    PdfInspectionError,
    inspect_pdf,
    sha256_file,
)


class TakeoutError(ValueError):
    """Raised when a takeout ticket group cannot be determined without guessing."""


@dataclass(frozen=True)
class OcrLine:
    text: str
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class OrderReceipt:
    path: Path
    order_number: str
    merchant: str
    paid_amount: Decimal
    sha256: str
    crop: tuple[int, int, int, int]


@dataclass(frozen=True)
class TakeoutInvoice:
    render_path: Path
    source_path: Path
    amount: Decimal
    invoice_date: date
    invoice_number: str
    sha256: str
    crop: tuple[int, int, int, int] | None


def _decimal(value: str, label: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise TakeoutError(f"{label}格式无效") from exc


def _ocr_lines(path: Path, engine: Any) -> list[OcrLine]:
    try:
        result, _ = engine(str(path))
    except Exception as exc:
        raise TakeoutError(f"图片无法读取或识别：{path.name}") from exc
    lines: list[OcrLine] = []
    for raw in result or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        box, raw_text = raw[0], raw[1]
        if not isinstance(raw_text, str) or not isinstance(box, (list, tuple)):
            continue
        points = [point for point in box if isinstance(point, (list, tuple)) and len(point) >= 2]
        if not points:
            continue
        xs = [int(float(point[0])) for point in points]
        ys = [int(float(point[1])) for point in points]
        lines.append(OcrLine(raw_text.strip(), min(xs), min(ys), max(xs), max(ys)))
    if not lines:
        raise TakeoutError(f"图片未识别出文字：{path.name}")
    return lines


def _compact(lines: list[OcrLine]) -> str:
    return re.sub(r"\s+", "", "\n".join(line.text for line in lines))


def _crop_between(
    path: Path,
    lines: list[OcrLine],
    start_markers: tuple[str, ...],
    end_markers: tuple[str, ...],
    bottom_margin: int = 64,
) -> tuple[int, int, int, int]:
    with Image.open(path) as image:
        width, height = image.size
    starts = [line.top for line in lines if any(marker in line.text for marker in start_markers)]
    ends = [line.bottom for line in lines if any(marker in line.text for marker in end_markers)]
    top = max(0, min(starts) - 32) if starts else 0
    bottom = min(height, max(ends) + bottom_margin) if ends else height
    if bottom - top < height * 0.2:
        return (0, 0, width, height)
    return (0, top, width, bottom)


def _parse_order(path: Path, lines: list[OcrLine]) -> OrderReceipt:
    compact = _compact(lines)
    if "订单号" not in compact or "实付" not in compact:
        raise TakeoutError("不是淘宝外卖订单截图")
    order_matches = re.findall(r"(?<!\d)(\d{16,24})(?!\d)", compact)
    if len(set(order_matches)) != 1:
        raise TakeoutError("订单截图未找到唯一订单号")
    amount_matches = re.findall(r"实付[¥￥]?([0-9]+(?:\.[0-9]{1,2})?)", compact)
    if len(set(amount_matches)) != 1:
        raise TakeoutError("订单截图未找到唯一实付金额")
    merchant = "淘宝闪购"
    for line in lines:
        if "闪购" in line.text and "订单" not in line.text:
            candidate = re.sub(r"^.*?闪购", "", line.text).strip()
            if candidate:
                merchant = candidate.rstrip("）)")
                break
    crop = _crop_between(
        path, lines, ("订单已",), ("订单号", order_matches[0]), bottom_margin=20
    )
    return OrderReceipt(
        path=path,
        order_number=order_matches[0],
        merchant=merchant,
        paid_amount=_decimal(amount_matches[0], "实付金额"),
        sha256=sha256_file(path),
        crop=crop,
    )


def _parse_image_invoice(path: Path, lines: list[OcrLine]) -> TakeoutInvoice:
    compact = _compact(lines)
    if "电子发票" not in compact or "发票号码" not in compact:
        raise TakeoutError("不是电子发票图片")
    number_match = re.search(r"发票号码[:：]?([0-9]{8,24})", compact)
    date_match = re.search(r"开票日期[:：]?(\d{4})年(\d{1,2})月(\d{1,2})日", compact)
    amount_match = re.search(
        r"(?:（小写）|\(小写\))[¥￥]?([0-9]+(?:\.[0-9]{1,2})?)",
        compact,
    )
    if number_match is None:
        raise TakeoutError("发票图片未找到发票号码")
    if date_match is None:
        raise TakeoutError("发票图片未找到开票日期")
    if amount_match is None:
        raise TakeoutError("发票图片未找到价税合计")
    try:
        invoice_date = date(*(int(value) for value in date_match.groups()))
    except ValueError as exc:
        raise TakeoutError("发票图片开票日期无效") from exc
    crop = _crop_between(path, lines, ("电子发票",), ("开票人", "备注"))
    return TakeoutInvoice(
        render_path=path,
        source_path=path,
        amount=_decimal(amount_match.group(1), "发票金额"),
        invoice_date=invoice_date,
        invoice_number=number_match.group(1),
        sha256=sha256_file(path),
        crop=crop,
    )


def _parse_pdf_invoice(render_path: Path, source_path: Path) -> TakeoutInvoice:
    try:
        inspected = inspect_pdf(render_path)
    except PdfInspectionError as exc:
        raise TakeoutError(f"电子发票 PDF 无法识别：{render_path.name}：{exc}") from exc
    if inspected.invoice_date is None or inspected.invoice_number is None:
        raise TakeoutError(f"电子发票 PDF 字段不完整：{render_path.name}")
    return TakeoutInvoice(
        render_path=render_path,
        source_path=source_path,
        amount=inspected.amount,
        invoice_date=inspected.invoice_date,
        invoice_number=inspected.invoice_number,
        sha256=inspected.sha256,
        crop=None,
    )
