from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from invoice_print_layout.documents import (
    PdfInspectionError,
    classify_text,
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
    assert parse_amount(invoice_text, DocumentKind.INVOICE) == Decimal("52.14")
    assert parse_invoice_date(invoice_text).isoformat() == "2026-09-03"
    assert parse_invoice_number(invoice_text) == "12345678901234567890"


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
