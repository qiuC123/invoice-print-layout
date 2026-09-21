from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pymupdf

from invoice_print_layout.models import (
    DocumentKind,
    PairingIssue,
    PdfDocument,
    RideProvider,
    TicketGroup,
)


class PdfInspectionError(ValueError):
    """Raised when a PDF cannot be handled without guessing."""


_NUMBER = r"([0-9]+(?:\.[0-9]{1,2})?)"
_TRIP_AMOUNT_PATTERNS = (
    re.compile(rf"共\d+笔行程[，,]?合计[¥￥]?{_NUMBER}元?"),
    re.compile(rf"TRIPTOTAL[:：]?[¥￥]?{_NUMBER}", re.IGNORECASE),
)
_INVOICE_AMOUNT_PATTERNS = (
    re.compile(rf"(?:（小写）|\(小写\))[¥￥]?{_NUMBER}"),
    re.compile(rf"TOTALWITHTAX[:：]?[¥￥]?{_NUMBER}", re.IGNORECASE),
)
_INVOICE_DATE_PATTERNS = (
    re.compile(r"开票日期[:：]?(\d{4})年(\d{1,2})月(\d{1,2})日"),
    re.compile(r"INVOICEDATE[:：]?(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", re.IGNORECASE),
)
_INVOICE_NUMBER_PATTERNS = (
    re.compile(r"发票号码[:：]?(\d{8,24})"),
    re.compile(r"INVOICENUMBER[:：]?(\d{8,24})", re.IGNORECASE),
)

_CHINESE_TRIP_COLUMNS = ("上车时间", "起点", "终点", "金额")
_ENGLISH_TRIP_COLUMNS = ("PICKUP", "DESTINATION", "AMOUNT")


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _has_structural_trip_evidence(compact: str) -> bool:
    upper = compact.upper()
    chinese_title = "行程单" in compact
    chinese_summary = any(pattern.search(compact) for pattern in _TRIP_AMOUNT_PATTERNS[:1])
    chinese_columns = all(marker in compact for marker in _CHINESE_TRIP_COLUMNS)
    english_title = "TRIPTABLE" in upper
    english_summary = "TRIPTOTAL" in upper
    english_columns = all(marker in upper for marker in _ENGLISH_TRIP_COLUMNS)
    return (chinese_title and chinese_summary and chinese_columns) or (
        english_title and english_summary and english_columns
    )


def classify_text(text: str) -> DocumentKind:
    compact = compact_text(text)
    trip_markers = (
        "滴滴出行-行程单",
        "滴滴出行行程单",
        "DIDITRAVEL-TRIPTABLE",
        "CAOCAOTRAVEL-TRIPTABLE",
        "第三方网约车服务提供方曹操出行—行程单",
        "第三方网约车服务提供方曹操出行-行程单",
        "第三方网约车服务提供方上汽享道出行—行程单",
        "第三方网约车服务提供方上汽享道出行-行程单",
        "XIANGDAOTRAVEL-TRIPTABLE",
    )
    if any(marker in compact for marker in trip_markers):
        return DocumentKind.TRIP
    if _has_structural_trip_evidence(compact):
        return DocumentKind.TRIP

    invoice_title = "电子发票" in compact or "ELECTRONICINVOICE" in compact.upper()
    invoice_fields = (
        "价税合计" in compact
        or "发票号码" in compact
        or "INVOICENUMBER" in compact.upper()
        or "TOTALWITHTAX" in compact.upper()
    )
    if invoice_title and invoice_fields:
        return DocumentKind.INVOICE
    raise PdfInspectionError("无法识别为支持的网约车行程单或电子发票")


def detect_provider(text: str) -> RideProvider:
    compact = compact_text(text)
    if (
        "享道出行" in compact
        or "上汽享道" in compact
        or "XIANGDAO" in compact.upper()
    ):
        return RideProvider.XIANGDAO
    if (
        "曹操出行" in compact
        or "吉利优行" in compact
        or "CAOCAOTRAVEL" in compact.upper()
        or "GEELYRIDE" in compact.upper()
    ):
        return RideProvider.CAOCAO
    if "滴滴" in compact or "DIDITRAVEL" in compact.upper():
        return RideProvider.DIDI
    return RideProvider.UNKNOWN


def _parse_positioned_invoice_amount(page: pymupdf.Page) -> Decimal:
    anchors = (
        page.search_for("小写")
        or page.search_for("（小写）")
        or page.search_for("(小写)")
        or page.search_for("(SMALL)")
    )
    if len(anchors) != 1:
        raise PdfInspectionError("未找到唯一的发票小写金额位置")
    anchor = anchors[0]
    candidates: list[tuple[float, Decimal]] = []
    for word in page.get_text("words"):
        if len(word) < 5:
            continue
        x0, y0, x1, y1, raw_text = word[:5]
        text = str(raw_text).strip().replace(",", "").lstrip("¥￥")
        joined = re.fullmatch(r"[（(]?小写[）)]?[¥￥]?([0-9]+(?:\.[0-9]{1,2})?)", text)
        if joined:
            text = joined.group(1)
            x0 = anchor.x1
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
            continue
        same_row = y1 >= anchor.y0 - 4 and y0 <= anchor.y1 + 4
        distance = float(x0) - anchor.x1
        if same_row and -2 <= distance <= 160:
            try:
                candidates.append((abs(distance), Decimal(text).quantize(Decimal("0.01"))))
            except InvalidOperation as exc:
                raise PdfInspectionError("金额格式无效") from exc
    if not candidates:
        raise PdfInspectionError("未找到发票价税合计")
    candidates.sort(key=lambda item: item[0])
    nearest_distance = candidates[0][0]
    nearest = {amount for distance, amount in candidates if distance == nearest_distance}
    if len(nearest) != 1:
        raise PdfInspectionError("发票小写金额位置存在歧义")
    return nearest.pop()


def parse_amount(text: str, kind: DocumentKind) -> Decimal:
    compact = compact_text(text)
    patterns = _TRIP_AMOUNT_PATTERNS if kind is DocumentKind.TRIP else _INVOICE_AMOUNT_PATTERNS
    for pattern in patterns:
        match = pattern.search(compact)
        if match:
            try:
                return Decimal(match.group(1)).quantize(Decimal("0.01"))
            except InvalidOperation as exc:
                raise PdfInspectionError("金额格式无效") from exc
    if kind is DocumentKind.INVOICE and "价税合计" in compact:
        currency_amounts = re.findall(rf"[¥￥]{_NUMBER}", compact)
        if currency_amounts:
            try:
                return Decimal(currency_amounts[-1]).quantize(Decimal("0.01"))
            except InvalidOperation as exc:
                raise PdfInspectionError("金额格式无效") from exc
    raise PdfInspectionError("未找到行程合计" if kind is DocumentKind.TRIP else "未找到发票价税合计")


def parse_invoice_date(text: str) -> date:
    compact = compact_text(text)
    for pattern in _INVOICE_DATE_PATTERNS:
        match = pattern.search(compact)
        if match:
            try:
                return date(*(int(value) for value in match.groups()))
            except ValueError as exc:
                raise PdfInspectionError("开票日期无效") from exc
    candidates = set(re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", compact))
    if not candidates:
        candidates = set(re.findall(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", compact))
    if len(candidates) == 1:
        try:
            return date(*(int(value) for value in candidates.pop()))
        except ValueError as exc:
            raise PdfInspectionError("开票日期无效") from exc
    raise PdfInspectionError("未找到开票日期")


def parse_invoice_number(text: str) -> str:
    compact = compact_text(text)
    for pattern in _INVOICE_NUMBER_PATTERNS:
        match = pattern.search(compact)
        if match:
            return match.group(1)
    if "发票号码" in compact or "INVOICENUMBER" in compact.upper():
        candidates = {
            line.strip()
            for line in text.splitlines()
            if re.fullmatch(r"\d{8,24}", line.strip())
        }
        if len(candidates) == 1:
            return candidates.pop()
    raise PdfInspectionError("未找到发票号码")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_pdf(path: Path) -> PdfDocument:
    try:
        document = pymupdf.open(path)
    except Exception as exc:
        raise PdfInspectionError(f"PDF 无法打开：{exc}") from exc

    try:
        if document.needs_pass:
            raise PdfInspectionError("PDF 已加密")
        if document.page_count < 1:
            raise PdfInspectionError("PDF 没有页面")
        text = "\n".join(
            document.load_page(index).get_text("text")
            for index in range(document.page_count)
        )
        if not compact_text(text):
            raise PdfInspectionError("PDF 无可提取文字，可能是扫描件")
        kind = classify_text(text)
        provider = detect_provider(text)
        try:
            amount = parse_amount(text, kind)
        except PdfInspectionError:
            if kind is not DocumentKind.INVOICE or document.page_count != 1:
                raise
            amount = _parse_positioned_invoice_amount(document.load_page(0))
        invoice_date = parse_invoice_date(text) if kind is DocumentKind.INVOICE else None
        invoice_number = parse_invoice_number(text) if kind is DocumentKind.INVOICE else None
        if kind is DocumentKind.INVOICE and document.page_count != 1:
            raise PdfInspectionError("第一版只支持单页电子发票")
        return PdfDocument(
            path=path,
            kind=kind,
            provider=provider,
            amount=amount,
            sha256=sha256_file(path),
            page_count=document.page_count,
            invoice_date=invoice_date,
            invoice_number=invoice_number,
        )
    except PdfInspectionError:
        raise
    except Exception as exc:
        raise PdfInspectionError(f"PDF 读取失败：{exc}") from exc
    finally:
        document.close()


def pair_documents(
    documents: list[PdfDocument],
) -> tuple[list[TicketGroup], list[PairingIssue]]:
    by_provider_amount: dict[tuple[RideProvider, Decimal], list[PdfDocument]] = defaultdict(list)
    for document in documents:
        if document.provider is not RideProvider.UNKNOWN:
            by_provider_amount[(document.provider, document.amount)].append(document)

    groups: list[TicketGroup] = []
    used_paths: set[Path] = set()
    for provider_amount in sorted(by_provider_amount, key=lambda item: (item[1], item[0].value)):
        candidates = by_provider_amount[provider_amount]
        trips = [item for item in candidates if item.kind is DocumentKind.TRIP]
        invoices = [item for item in candidates if item.kind is DocumentKind.INVOICE]
        if len(trips) == 1 and len(invoices) == 1:
            groups.append(TicketGroup(trip=trips[0], invoice=invoices[0]))
            used_paths.update((trips[0].path, invoices[0].path))

    remaining = [item for item in documents if item.path not in used_paths]
    remaining_by_amount: dict[Decimal, list[PdfDocument]] = defaultdict(list)
    for document in remaining:
        remaining_by_amount[document.amount].append(document)
    for amount in sorted(remaining_by_amount):
        candidates = remaining_by_amount[amount]
        trips = [item for item in candidates if item.kind is DocumentKind.TRIP]
        invoices = [item for item in candidates if item.kind is DocumentKind.INVOICE]
        if len(trips) != 1 or len(invoices) != 1:
            continue
        trip, invoice = trips[0], invoices[0]
        providers_compatible = (
            trip.provider is RideProvider.UNKNOWN
            or invoice.provider is RideProvider.UNKNOWN
            or trip.provider is invoice.provider
        )
        if providers_compatible:
            groups.append(TicketGroup(trip=trip, invoice=invoice))
            used_paths.update((trip.path, invoice.path))

    remaining = [item for item in documents if item.path not in used_paths]
    remaining_trips = [item for item in remaining if item.kind is DocumentKind.TRIP]
    remaining_invoices = [item for item in remaining if item.kind is DocumentKind.INVOICE]
    if len(remaining_trips) == 1 and len(remaining_invoices) == 1:
        trip = remaining_trips[0]
        invoice = remaining_invoices[0]
        if trip.amount != invoice.amount:
            reason = (
                f"金额不一致：行程单 {trip.amount:.2f} 元，"
                f"电子发票 {invoice.amount:.2f} 元"
            )
        else:
            reason = (
                f"平台不一致：行程单为{trip.provider.value}，"
                f"电子发票为{invoice.provider.value}"
            )
        return groups, [PairingIssue((trip.path, invoice.path), reason)]

    issues: list[PairingIssue] = []
    remaining_by_amount = defaultdict(list)
    for document in remaining:
        remaining_by_amount[document.amount].append(document)
    for amount in sorted(remaining_by_amount):
        candidates = remaining_by_amount[amount]
        trips = [item for item in candidates if item.kind is DocumentKind.TRIP]
        invoices = [item for item in candidates if item.kind is DocumentKind.INVOICE]
        reason = (
            f"金额 {amount:.2f} 元无法唯一配对："
            f"行程单 {len(trips)} 份，电子发票 {len(invoices)} 份"
        )
        issues.append(PairingIssue(tuple(item.path for item in candidates), reason))
    return groups, issues
