from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.documents import (
    PdfInspectionError,
    classify_text,
    compact_text,
    detect_provider,
    inspect_pdf,
    pair_documents,
    parse_amount,
    parse_invoice_date,
    parse_invoice_number,
)
from invoice_print_layout.models import DocumentKind, RideProvider
from tests.helpers import (
    make_caocao_trip_pdf,
    make_generic_trip_pdf,
    make_invoice_pdf,
    make_trip_pdf,
    make_xiangdao_invoice_pdf,
    make_xiangdao_trip_pdf,
)


def test_parse_chinese_trip_and_invoice_fields() -> None:
    trip_text = "滴滴出行-行程单 共2笔行程，合计123.45元"
    invoice_text = "电子发票（小写）¥123.45 开票日期：2026年01月15日 发票号码：12345678901234567890"

    assert classify_text(trip_text) is DocumentKind.TRIP
    assert classify_text(invoice_text) is DocumentKind.INVOICE
    assert parse_amount(trip_text, DocumentKind.TRIP) == Decimal("123.45")
    assert parse_amount(invoice_text, DocumentKind.INVOICE) == Decimal("123.45")
    assert parse_invoice_date(invoice_text).isoformat() == "2026-01-15"
    assert parse_invoice_number(invoice_text) == "12345678901234567890"


def test_unrecognized_text_fails_closed() -> None:
    with pytest.raises(PdfInspectionError, match="无法识别"):
        classify_text("普通文档，没有票据信息")


def test_compatibility_characters_normalize_for_the_same_explicit_invoice_fields() -> None:
    text = ("电⼦发票（普通发票） 价税合计（⼩写）￥１６．７９ "
            "开票⽇期：２０２６年１０⽉０６⽇ 发票号码：１２３４５６７８９０１２３４５６７８９０")
    assert compact_text("电 ⼦ 发票 ￥１６．７９") == "电子发票¥16.79"
    assert classify_text(text) is DocumentKind.INVOICE
    assert parse_amount(text, DocumentKind.INVOICE) == Decimal("16.79")
    assert parse_invoice_date(text).isoformat() == "2026-10-06"
    assert parse_invoice_number(text) == "12345678901234567890"


def test_compatibility_invoice_number_on_a_separate_line_returns_ascii() -> None:
    text = "电⼦发票 发票号码：开票⽇期：\n１２３４５６７８９０１２３４５６７８９０\n２０２６年１０⽉０６⽇"
    assert parse_invoice_number(text) == "12345678901234567890"
    assert parse_invoice_date(text).isoformat() == "2026-10-06"


def test_native_compatibility_pdf_inspection_keeps_original_bytes(tmp_path: Path) -> None:
    path = tmp_path / "synthetic-compatibility-invoice.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=410)
        for y, line in ((45, "电⼦发票（普通发票）"),
                        (90, "发票号码：１２３４５６７８９０１２３４５６７８９０"),
                        (120, "开票⽇期：２０２６年１０⽉０６⽇"),
                        (180, "价税合计（⼩写）￥１６．７９")):
            page.insert_text((35, y), line, fontname="china-s", fontsize=12)
        document.save(path)
    before = path.read_bytes()
    parsed = inspect_pdf(path)
    assert parsed.kind is DocumentKind.INVOICE and parsed.provider is RideProvider.UNKNOWN
    assert parsed.amount == Decimal("16.79") and parsed.invoice_number == "12345678901234567890"
    assert parsed.invoice_date is not None and parsed.invoice_date.isoformat() == "2026-10-06"
    assert parsed.sha256 == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before


def test_compatibility_normalization_does_not_invent_invoice_evidence(tmp_path: Path) -> None:
    with pytest.raises(PdfInspectionError, match="无法识别"):
        classify_text("电⼦资料（⼩写）￥１６．７９ ２０２６年１０⽉０６⽇")
    path = tmp_path / "unknown-scan.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        # An image-only page still has no native invoice fields after NFKC.
        pixel = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2, 2), False)
        pixel.clear_with(255)
        page.insert_image(page.rect, pixmap=pixel)
        document.save(path)
    before = path.read_bytes()
    with pytest.raises(PdfInspectionError, match="无可提取文字"):
        inspect_pdf(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("summary", [
    "共1次行程；合计11.85元", "共计1单行程，合计11.85元",
    "共2个行程，合计11.85元", "共1笔行程，合计11.85元",
])
def test_platform_summary_variants_have_the_same_native_structure(summary: str) -> None:
    text = "腾讯出行服务—打车——行程单 上车时间 起点 终点 金额 " + summary
    assert classify_text(text) is DocumentKind.TRIP
    assert parse_amount(text, DocumentKind.TRIP) == Decimal("11.85")


def test_image_heading_trip_requires_all_native_business_fields() -> None:
    body = ("用车人手机号 行程总计：共1笔行程，总计可开票金额6.30元 "
            "序号 订单类型 上车时间 所在城市 起点/终点 可开票金额")
    assert classify_text(body) is DocumentKind.TRIP
    assert parse_amount(body, DocumentKind.TRIP) == Decimal("6.30")
    with pytest.raises(PdfInspectionError):
        classify_text(body.replace("用车人手机号", ""))


def test_structural_trip_recognition_requires_complete_evidence() -> None:
    unknown_trip = (
        "新平台行程单 共1笔行程，合计44.60元 "
        "序号 上车时间 起点 终点 金额[元]"
    )

    assert classify_text(unknown_trip) is DocumentKind.TRIP
    assert detect_provider(unknown_trip) is RideProvider.UNKNOWN
    with pytest.raises(PdfInspectionError, match="无法识别"):
        classify_text("新平台行程单 共1笔行程，合计44.60元")


def test_parse_caocao_text_and_extraction_order() -> None:
    trip_text = "第三方网约车服务提供方曹操出行—行程单 共1笔行程，合计52.14元"
    invoice_text = (
        "电子发票（普通发票） 价税合计（大写）伍拾贰圆壹角肆分 "
        "（小写）其他字段 ¥50.62 ¥1.52 ¥52.14 吉利优行 "
        "发票号码： 开票日期：\n12345678901234567890\n2026年09月03日"
    )

    assert classify_text(trip_text) is DocumentKind.TRIP
    assert classify_text(invoice_text) is DocumentKind.INVOICE
    assert detect_provider(trip_text) is RideProvider.CAOCAO
    assert detect_provider(invoice_text) is RideProvider.CAOCAO
    assert parse_amount(trip_text, DocumentKind.TRIP) == Decimal("52.14")
    # Currency-last cannot determine which value is the printed small total.
    with pytest.raises(PdfInspectionError, match="未找到发票价税合计"):
        parse_amount(invoice_text, DocumentKind.INVOICE)
    assert parse_invoice_date(invoice_text).isoformat() == "2026-09-03"
    assert parse_invoice_number(invoice_text) == "12345678901234567890"


def test_invoice_text_never_joins_a_bare_currency_to_invoice_number_and_year() -> None:
    text = ("电子发票\n价税合计（大写）\n（小写）\n¥\n"
            "12345678901234567890\n2026年10月06日\n17.72")
    with pytest.raises(PdfInspectionError, match="未找到发票价税合计"):
        parse_amount(text, DocumentKind.INVOICE)
    with pytest.raises(PdfInspectionError, match="存在歧义"):
        parse_amount("电子发票\n（小写）¥17.72\n（小写）¥19.44", DocumentKind.INVOICE)


@pytest.mark.parametrize('amount,total_x', [('17.72', 430), ('61.03', 465), ('9.18', 485)])
def test_labels_first_native_invoice_uses_positioned_small_total(
        tmp_path: Path, amount: str, total_x: int) -> None:
    path = tmp_path / 'synthetic-labels-first-invoice.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=800, height=500)
        labels = [(35, 40, '电子发票'), (400, 80, '发票号码：'), (400, 110, '开票日期：'),
                  (35, 300, '价税合计（大写）'), (360, 300, '（小写）'), (35, 160, '金额'),
                  (650, 180, '税额')]
        for x, y, line in labels:
            page.insert_text((x, y), line, fontname='china-s', fontsize=11)
        # Values follow all labels in the PDF stream, independently of their
        # visual locations. A naked final currency precedes ticket ID/date.
        for x, y, line in [(70, 190, '15.33'), (700, 210, '2.39'),
                           (total_x, 300, amount), (total_x - 15, 300, '¥')]:
            page.insert_text((x, y), line, fontsize=11)
        page.insert_text((520, 80), '12345678901234567890', fontsize=11)
        page.insert_text((520, 110), '2026年10月06日', fontname='china-s', fontsize=11)
        document.save(path)
    before = path.read_bytes()
    with pymupdf.open(path) as document:
        text = document[0].get_text()
    with pytest.raises(PdfInspectionError, match='未找到发票价税合计'):
        parse_amount(text, DocumentKind.INVOICE)
    parsed = inspect_pdf(path)
    assert parsed.amount == Decimal(amount)
    assert parsed.invoice_number == '12345678901234567890'
    assert parsed.invoice_date is not None and parsed.invoice_date.isoformat() == '2026-10-06'
    assert parsed.sha256 == hashlib.sha256(before).hexdigest() and path.read_bytes() == before


def test_inspect_xiangdao_with_positioned_invoice_amount(tmp_path: Path) -> None:
    assert (
        classify_text("第三方网约车服务提供方上汽享道出行—行程单")
        is DocumentKind.TRIP
    )
    assert detect_provider("享道出行（上海）科技股份有限公司") is RideProvider.XIANGDAO
    trip = inspect_pdf(make_xiangdao_trip_pdf(tmp_path / "xiangdao-trip.pdf"))
    invoice = inspect_pdf(make_xiangdao_invoice_pdf(tmp_path / "xiangdao-invoice.pdf"))

    assert trip.kind is DocumentKind.TRIP
    assert invoice.kind is DocumentKind.INVOICE
    assert trip.provider is RideProvider.XIANGDAO
    assert invoice.provider is RideProvider.XIANGDAO
    assert trip.amount == Decimal("17.72")
    assert invoice.amount == Decimal("17.72")
    assert invoice.invoice_date is not None
    assert invoice.invoice_date.isoformat() == "2026-09-04"
    assert invoice.invoice_number == "26000000000000000003"

    groups, issues = pair_documents([trip, invoice])
    assert len(groups) == 1
    assert not issues


def test_same_amount_pairs_by_provider(tmp_path: Path) -> None:
    didi_trip = inspect_pdf(make_trip_pdf(tmp_path / "didi-trip.pdf", amount="52.14"))
    didi_invoice = inspect_pdf(make_invoice_pdf(tmp_path / "didi-invoice.pdf", amount="52.14"))
    caocao_trip = inspect_pdf(make_caocao_trip_pdf(tmp_path / "caocao-trip.pdf"))
    caocao_invoice = inspect_pdf(
        make_invoice_pdf(tmp_path / "caocao-invoice.pdf", amount="52.14", provider="caocao")
    )

    groups, issues = pair_documents(
        [didi_trip, caocao_invoice, caocao_trip, didi_invoice]
    )

    assert not issues
    assert {group.provider for group in groups} == {
        RideProvider.DIDI,
        RideProvider.CAOCAO,
    }


def test_inspect_and_pair_unique_amount(tmp_path: Path) -> None:
    trip = inspect_pdf(make_trip_pdf(tmp_path / "trip.pdf"))
    invoice = inspect_pdf(make_invoice_pdf(tmp_path / "invoice.pdf"))

    groups, issues = pair_documents([trip, invoice])

    assert len(groups) == 1
    assert not issues
    assert groups[0].trip.page_count == 1
    assert groups[0].invoice.invoice_number == "12345678901234567890"


def test_unknown_provider_pairs_when_structure_and_amount_are_unique(
    tmp_path: Path,
) -> None:
    trip = inspect_pdf(make_generic_trip_pdf(tmp_path / "unknown-trip.pdf"))
    invoice = inspect_pdf(
        make_invoice_pdf(
            tmp_path / "unknown-invoice.pdf",
            amount="44.60",
            invoice_number="10000000000000000044",
        )
    )

    groups, issues = pair_documents([trip, invoice])

    assert len(groups) == 1
    assert not issues
    assert groups[0].provider is RideProvider.UNKNOWN


def test_pairing_same_amount_is_ambiguous(tmp_path: Path) -> None:
    first_trip = inspect_pdf(make_trip_pdf(tmp_path / "trip-1.pdf"))
    second_trip = inspect_pdf(make_trip_pdf(tmp_path / "trip-2.pdf"))
    invoice = inspect_pdf(make_invoice_pdf(tmp_path / "invoice.pdf"))

    groups, issues = pair_documents([first_trip, second_trip, invoice])

    assert not groups
    assert len(issues) == 1
    assert "无法唯一配对" in issues[0].reason


def test_amount_mismatch_keeps_trip_and_invoice_in_one_issue(tmp_path: Path) -> None:
    trip = inspect_pdf(make_trip_pdf(tmp_path / "trip.pdf", amount="123.45"))
    invoice = inspect_pdf(make_invoice_pdf(tmp_path / "invoice.pdf", amount="124.00"))

    groups, issues = pair_documents([trip, invoice])

    assert not groups
    assert len(issues) == 1
    assert set(issues[0].paths) == {trip.path, invoice.path}
    assert "金额不一致" in issues[0].reason
