"""SF express and SF same-city invoices with PDF waybill evidence."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pymupdf

from invoice_print_layout.documents import (
    _parse_positioned_invoice_amount, compact_text,
    parse_invoice_date, parse_invoice_number, sha256_file,
)
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH, _slot, validate_print_package

KINDS = {"顺丰": ("顺丰", "快递"), "同城": ("顺丰同城", "同城配送")}
_FOOTER = re.compile(r"^(?:页码[:：]?)?\d+[/\-]\d+$")


@dataclass(frozen=True)
class CourierPdf:
    path: Path
    kind: str
    is_invoice: bool
    amount: Decimal
    sha256: str
    invoice_number: str | None = None
    invoice_date: date | None = None


def inspect_courier_pdf(path: Path, kind: str | None = None) -> CourierPdf:
    if kind is not None and kind not in KINDS:
        raise ValueError("不支持的快递类型")
    with pymupdf.open(path) as document:
        if document.needs_pass or not len(document):
            raise ValueError("PDF 已加密或没有页面")
        text = "\n".join(page.get_text() for page in document)
        compact = compact_text(text)
        if not compact:
            raise ValueError("PDF 无可提取文字，请提供原始 PDF")
        express_detail = "顺丰电子发票" in compact and "运单明细" in compact
        city_detail = all(word in compact for word in ("运单起止日期", "订单号", "发件信息", "收件信息"))
        if express_detail or city_detail:
            detected = "顺丰" if express_detail else "同城"
            if kind is not None and detected != kind:
                raise ValueError("运单明细类型与当前批次不符")
            kind = detected
            pattern = (r"发票总金额[（(]元[）)]([0-9]+(?:\.[0-9]{1,2})?)"
                       if express_detail else r"共\d+笔运单[，,]?合计([0-9]+(?:\.[0-9]{1,2})?)元")
            amounts = {Decimal(v).quantize(Decimal("0.01")) for v in re.findall(pattern, compact)}
            if len(amounts) != 1:
                raise ValueError("未找到唯一的运单总金额")
            number = None
            if express_detail:
                numbers = set(re.findall(r"(?<!\d)\d{20}(?!\d)", text))
                if len(numbers) != 1:
                    raise ValueError("顺丰运单明细缺少唯一发票号码")
                number = numbers.pop()
            return CourierPdf(path, kind, False, amounts.pop(), sha256_file(path), number)
        if "电子发票" not in compact or "价税合计" not in compact or len(document) != 1:
            raise ValueError("不支持的顺丰票据，请提供电子发票及配套运单明细 PDF")
        invoice_kind = "同城" if "顺丰同城" in compact else "顺丰" if "顺丰速运" in compact else None
        if invoice_kind is None or (kind is not None and invoice_kind != kind):
            raise ValueError("发票服务商与当前顺丰类型不符")
        # PDF extraction order can put the tax amount last. Never use last-currency fallback.
        amount = _parse_positioned_invoice_amount(document[0])
        return CourierPdf(path, invoice_kind, True, amount, sha256_file(path),
                          parse_invoice_number(text), parse_invoice_date(text))


def _detail_clip(page: pymupdf.Page) -> pymupdf.Rect:
    blocks = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0
              and compact_text(b[4]) and not _FOOTER.fullmatch(compact_text(b[4]))]
    if not blocks:
        raise ValueError("运单页面没有可确认的正文")
    top = max(page.rect.y0, min(b[1] for b in blocks) - 12)
    bottom = max(b[3] for b in blocks)
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if isinstance(rect, pymupdf.Rect) and rect.y1 >= top and rect.y0 <= bottom + 36:
            bottom = max(bottom, min(rect.y1, bottom + 36))
    # Keep any embedded graphic in the evidence rather than clipping unknown content.
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 1:
            top = min(top, block["bbox"][1])
            bottom = max(bottom, block["bbox"][3])
    return pymupdf.Rect(0, top, page.rect.width, min(page.rect.height, bottom + 12))


def compose_courier_package(detail: Path, invoice: Path, output: Path, amount: Decimal) -> None:
    fragments: list[str] = []
    with pymupdf.open(detail) as evidence, pymupdf.open(invoice) as bill, pymupdf.open() as result:
        # show_pdf_page does not copy annotations. Preserve their visible appearance
        # on these in-memory print copies; archived source PDFs remain untouched.
        evidence.bake(annots=True, widgets=False)
        bill.bake(annots=True, widgets=False)
        pages = [(evidence, i, _detail_clip(p)) for i, p in enumerate(evidence)]
        pages.append((bill, 0, bill[0].rect))
        for source, index, _ in pages:
            fragments.extend(compact_text(line) for line in source[index].get_text().splitlines()
                             if len(compact_text(line)) >= 2 and not _FOOTER.fullmatch(compact_text(line)))
        for offset in range(0, len(pages), 2):
            target = result.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            for slot, (source, index, clip) in enumerate(pages[offset:offset + 2]):
                target.show_pdf_page(_slot(slot), source, index, clip=clip, keep_proportion=True)
        result.save(output, garbage=4, deflate=True)
        expected = math.ceil(len(pages) / 2)
    validate_print_package(output, expected, f"{amount:.2f}", fragments)
