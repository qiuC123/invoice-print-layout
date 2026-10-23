from __future__ import annotations

from pathlib import Path
import hashlib
import io

import pymupdf
import pytest
from PIL import Image
import invoice_print_layout.layout as layout
from invoice_print_layout.documents import compact_text

from invoice_print_layout.layout import (
    LayoutError,
    compose_print_package,
    required_source_fragments,
    invoice_clip_rect,
    trip_clip_rect,
    validate_print_package,
)
from tests.helpers import (
    make_caocao_trip_pdf,
    make_generic_trip_pdf,
    make_invoice_pdf,
    make_trip_pdf,
    make_xiangdao_trip_pdf,
)


@pytest.mark.parametrize(
    ("trip_pages", "expected_print_pages"),
    [(1, 1), (2, 2), (3, 2)],
)
def test_compose_page_sequence(
    tmp_path: Path,
    trip_pages: int,
    expected_print_pages: int,
) -> None:
    trip_path = make_trip_pdf(tmp_path / "trip.pdf", pages=trip_pages)
    invoice_path = make_invoice_pdf(tmp_path / "invoice.pdf")
    output_path = tmp_path / "output.pdf"

    actual_pages = compose_print_package(trip_path, invoice_path, output_path)
    validate_print_package(
        output_path,
        expected_print_pages,
        "123.45",
        required_source_fragments(trip_path, invoice_path),
    )

    assert actual_pages == expected_print_pages
    with pymupdf.open(output_path) as output:
        if trip_pages == 1:
            page = output[0]
            assert page.search_for("DIDI TRAVEL - TRIP TABLE")[0].y0 < page.rect.height / 2
            assert page.search_for("ELECTRONIC INVOICE")[0].y0 > page.rect.height / 2
        if trip_pages == 2:
            page = output[1]
            second_page = page.get_text("text")
            assert "ELECTRONIC INVOICE" in second_page
            assert "TRIP PAGE" not in second_page
            assert page.search_for("ELECTRONIC INVOICE")[0].y0 < page.rect.height / 2
        if trip_pages == 3:
            page = output[1]
            second_page = page.get_text("text")
            assert "TRIP PAGE 3" in second_page
            assert "ELECTRONIC INVOICE" in second_page
            assert page.search_for("TRIP PAGE 3")[0].y0 < page.rect.height / 2
            assert page.search_for("ELECTRONIC INVOICE")[0].y0 > page.rect.height / 2


def test_trip_clip_removes_advertisement_and_footer(tmp_path: Path) -> None:
    trip_path = make_trip_pdf(tmp_path / "trip.pdf")
    with pymupdf.open(trip_path) as document:
        clip = trip_clip_rect(document[0], 0)

    assert clip.y0 > 100
    assert clip.y1 < 500


def test_caocao_trip_anchor_is_supported(tmp_path: Path) -> None:
    trip_path = make_caocao_trip_pdf(tmp_path / "caocao-trip.pdf")
    with pymupdf.open(trip_path) as document:
        clip = trip_clip_rect(document[0], 0)

    assert clip.y0 < 100
    assert clip.y1 < 300


def test_xiangdao_trip_anchor_is_supported(tmp_path: Path) -> None:
    trip_path = make_xiangdao_trip_pdf(tmp_path / "xiangdao-trip.pdf")
    with pymupdf.open(trip_path) as document:
        clip = trip_clip_rect(document[0], 0)

    assert clip.y0 < 100
    assert clip.y1 < 300


def test_generic_trip_table_anchor_is_supported(tmp_path: Path) -> None:
    trip_path = make_generic_trip_pdf(tmp_path / "generic-trip.pdf")
    with pymupdf.open(trip_path) as document:
        clip = trip_clip_rect(document[0], 0)

    assert clip.y0 < 100
    assert clip.y1 < 300


def test_compose_wraps_missing_source_as_layout_error(tmp_path: Path) -> None:
    with pytest.raises(LayoutError, match="PDF 拼版失败"):
        compose_print_package(
            tmp_path / "missing-trip.pdf",
            tmp_path / "missing-invoice.pdf",
            tmp_path / "output.pdf",
        )


@pytest.mark.parametrize('precompacted_source', [False, True])
def test_package_source_and_output_use_the_same_unicode_comparison(
        tmp_path: Path, precompacted_source: bool) -> None:
    output = tmp_path / 'synthetic-compatibility-output.pdf'
    invoice_number = '12345678901234567890'
    lines = ['电⼦发票', '发票号码：' + invoice_number, '开票⽇期：２０２６年１０⽉０６⽇',
             '价税合计（⼩写）￥４３．００', '保留运单正文：ＳＦ１２３４５６７８９０']
    with pymupdf.open() as document:
        page = document.new_page(width=layout.A4_WIDTH, height=layout.A4_HEIGHT)
        for index, line in enumerate(lines):
            page.insert_text((30, 60 + index * 30), line, fontname='china-s', fontsize=11)
        document.save(output)
    before = output.read_bytes()
    fragments = [compact_text(line) for line in lines] if precompacted_source else lines
    validate_print_package(output, 1, '43.00', fragments)
    assert output.read_bytes() == before
    # Normalization must never accept a changed ticket number or lost body.
    with pytest.raises(LayoutError, match='缺少源票据正文'):
        validate_print_package(output, 1, '43.00', [*fragments, '发票号码：12345678901234567891'])
    with pytest.raises(LayoutError, match='缺少源票据正文'):
        validate_print_package(output, 1, '43.00', [*fragments, '保留运单正文：SF9999999999'])
    with pytest.raises(LayoutError, match='未找到票据金额'):
        validate_print_package(output, 1, '43.01', fragments)
    assert output.read_bytes() == before


def test_required_trip_and_invoice_fragments_share_normalization_contract(tmp_path: Path) -> None:
    trip = make_trip_pdf(tmp_path / 'trip.pdf', amount='43.00')
    invoice = tmp_path / 'compatibility-invoice.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=410)
        for index, line in enumerate(['电⼦发票', '发票号码：12345678901234567890',
                                      '（⼩写）￥４３．００']):
            page.insert_text((35, 60 + index * 60), line, fontname='china-s', fontsize=12)
        document.save(invoice)
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (trip, invoice)}
    fragments = required_source_fragments(trip, invoice)
    assert '发票号码:12345678901234567890' in fragments
    assert '电子发票' in fragments and '(小写)¥43.00' in fragments
    output = tmp_path / 'combined.pdf'
    assert compose_print_package(trip, invoice, output) == 1
    validate_print_package(output, 1, '43.00', fragments)
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), "black").save(stream, format="PNG")
    return stream.getvalue()


def test_invoice_blank_crop_keeps_all_native_marks_and_images(tmp_path: Path) -> None:
    path = tmp_path / "invoice-a4.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=595.28, height=841.89)
        page.draw_rect(page.rect, fill=(1, 1, 1), color=None)
        page.insert_text((70, 60), "ELECTRONIC INVOICE", fontsize=18)
        page.insert_text((70, 130), "TOTAL WITH TAX: 123.45", fontsize=12)
        page.insert_image(pymupdf.Rect(20, 30, 60, 70), stream=_png())
        page.draw_circle((490, 455), 20, color=(1, 0, 0))
        clip = invoice_clip_rect(page)
        assert clip.x0 <= 20 and clip.y0 <= 30
        assert clip.y1 >= 475 and clip.y1 < 510
        document.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    trip = make_trip_pdf(tmp_path / "trip.pdf")
    output = tmp_path / "output.pdf"
    compose_print_package(trip, path, output)
    with pymupdf.open(output) as result:
        assert result[0].get_images()
        assert any(drawing.get("color") == (1.0, 0.0, 0.0)
                   for drawing in result[0].get_drawings())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_tall_invoice_gets_more_space_without_an_extra_page(tmp_path: Path) -> None:
    invoice = tmp_path / "tall-invoice.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=595.28, height=841.89)
        page.insert_text((72, 60), "ELECTRONIC INVOICE", fontsize=18)
        page.insert_text((72, 480), "TOTAL WITH TAX: 123.45", fontsize=13)
        page.draw_rect(pymupdf.Rect(20, 30, 575, 500))
        document.save(invoice)
    trip = make_caocao_trip_pdf(tmp_path / "trip.pdf", amount="123.45")
    output = tmp_path / "output.pdf"
    assert compose_print_package(trip, invoice, output) == 1
    validate_print_package(output, 1, "123.45", required_source_fragments(trip, invoice))
    with pymupdf.open(output) as result:
        spans = [span for block in result[0].get_text("dict")["blocks"]
                 if block["type"] == 0 for line in block["lines"] for span in line["spans"]]
        heading = next(span for span in spans if span["text"] == "ELECTRONIC INVOICE")
        assert heading["size"] > 17


def test_trip_preserves_body_image_and_lower_vector_edge(tmp_path: Path) -> None:
    path = make_trip_pdf(tmp_path / "trip.pdf")
    with pymupdf.open(path) as document:
        page = document[0]
        page.insert_image(pymupdf.Rect(72, 350, 172, 420), stream=_png())
        page.draw_rect(pymupdf.Rect(65, 240, 530, 470))
        clip = trip_clip_rect(page, 0)
        assert clip.y1 >= 482 and clip.y1 < 500
        assert clip.contains(pymupdf.Rect(72, 350, 172, 420))


def test_meaningful_text_above_trip_heading_is_never_treated_as_branding(tmp_path: Path) -> None:
    path = make_trip_pdf(tmp_path / "trip.pdf")
    with pymupdf.open(path) as document:
        page = document[0]
        page.insert_text((72, 130), "REQUIRED CUSTOMER REFERENCE")
        clip = trip_clip_rect(page, 0)
        assert clip.contains(page.search_for("REQUIRED CUSTOMER REFERENCE")[0])


def test_known_platform_keeps_business_image_and_signature_above_title(tmp_path: Path) -> None:
    path = make_trip_pdf(tmp_path / "trip.pdf")
    with pymupdf.open(path) as document:
        page = document[0]
        business_qr = pymupdf.Rect(400, 110, 460, 165)
        page.insert_image(business_qr, stream=_png())
        page.draw_circle((300, 130), 18, color=(1, 0, 0))
        clip = trip_clip_rect(page, 0)
        assert clip.contains(business_qr)
        assert clip.contains(pymupdf.Rect(282, 112, 318, 148))


def test_footer_page_number_does_not_remove_other_same_row_marks(tmp_path: Path) -> None:
    path = make_trip_pdf(tmp_path / "trip.pdf")
    with pymupdf.open(path) as document:
        page = document[0]
        business_mark = pymupdf.Rect(70, 800, 90, 814)
        page.insert_image(business_mark, stream=_png())
        page.draw_rect(pymupdf.Rect(110, 801, 180, 813), color=(1, 0, 0))
        clip = trip_clip_rect(page, 0)
        assert clip.contains(business_mark)
        assert clip.contains(pymupdf.Rect(110, 801, 180, 813))


def _advert_trip_page(document: pymupdf.Document, variant: str = "") -> pymupdf.Page:
    page = document.new_page(width=595.28, height=841.89)
    stream = io.BytesIO()
    Image.new("RGBA" if variant == "mask" else "RGB", (64, 64),
              (0, 0, 0, 128) if variant == "mask" else
              "blue" if variant == "image" else "black").save(stream, format="PNG")
    color = (1, 0, 0) if variant == "vector" else (0, 0, 1)
    if variant == "order":
        page.draw_rect(pymupdf.Rect(30, 150, 550, 155), color=color)
    page.insert_image(pymupdf.Rect(61 if variant == "position" else 60, 35, 500, 145),
                      stream=stream.getvalue())
    if variant != "order":
        page.draw_rect(pymupdf.Rect(30, 150, 550, 155), color=color)
    page.insert_text((72, 180), "DIDI TRAVEL - TRIP TABLE", fontsize=18)
    page.insert_text((72, 215), "TRIP TOTAL: 123.45", fontsize=12)
    page.insert_text((72, 260), "INDEX PICKUP DESTINATION AMOUNT", fontsize=10)
    page.insert_text((72, 300), "1 START END 123.45", fontsize=10)
    page.draw_rect(pymupdf.Rect(65, 240, 530, 325))
    if variant == "business-image":
        page.insert_image(pymupdf.Rect(410, 100, 470, 130), stream=_png())
    if variant == "business-stamp":
        page.draw_circle((300, 115), 18, color=(1, 0, 0))
    if variant == "business-text":
        page.insert_text((72, 115), "REQUIRED CUSTOMER REFERENCE")
    return page


def test_confirmed_complete_advertising_cluster_can_be_cropped(monkeypatch: pytest.MonkeyPatch) -> None:
    with pymupdf.open() as document:
        page = _advert_trip_page(document)
        anchor = layout._find_first_anchor(page)
        assert anchor is not None
        signature = layout._masthead_signature(page, anchor)
        assert signature is not None
        monkeypatch.setattr(layout, "_CONFIRMED_MASTHEAD_FINGERPRINTS", frozenset({signature[0]}))
        assert trip_clip_rect(page, 0).y0 >= 156


@pytest.mark.parametrize("variant", [
    "image", "vector", "mask", "position", "order",
    "business-image", "business-stamp", "business-text",
])
def test_any_header_resource_or_business_mark_change_invalidates_confirmation(
    monkeypatch: pytest.MonkeyPatch, variant: str,
) -> None:
    with pymupdf.open() as document:
        confirmed = _advert_trip_page(document)
        anchor = layout._find_first_anchor(confirmed)
        assert anchor is not None
        signature = layout._masthead_signature(confirmed, anchor)
        assert signature is not None
        monkeypatch.setattr(layout, "_CONFIRMED_MASTHEAD_FINGERPRINTS", frozenset({signature[0]}))
        changed = _advert_trip_page(document, variant)
        clip = trip_clip_rect(changed, 0)
        assert clip.contains(pymupdf.Rect(60, 35, 500, 145))
        if variant == "business-stamp":
            assert clip.contains(pymupdf.Rect(282, 97, 318, 133))


def test_image_heading_trip_preserves_the_entire_header_image(tmp_path: Path) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=595.28, height=841.89)
        image = pymupdf.Rect(86, 37, 509, 216)
        page.insert_image(image, stream=_png())
        for index, text in enumerate((
            "用车人手机号：123", "行程总计：共1笔行程，总计可开票金额6.30元",
            "序号 订单类型 上车时间 所在城市 起点/终点 可开票金额",
            "1 舒享 2026-10-04 15:56 南昌 车站/会场 6.30",
        )):
            page.insert_text((60, 235 + index * 25), text, fontname="china-s", fontsize=10)
        clip = trip_clip_rect(page, 0)
        assert clip.contains(image)
        assert clip.y1 < 370


@pytest.mark.parametrize("kind", ["missing-structure", "rotation", "scanned", "continuation"])
def test_uncertain_trip_boundaries_fail_closed(tmp_path: Path, kind: str) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=595.28, height=841.89)
        if kind == "scanned":
            page.insert_image(page.rect, stream=_png())
        else:
            page.insert_text((72, 100), "MYSTERY TRIP TABLE")
            page.insert_text((72, 200), "PICKUP")
            if kind == "rotation":
                page.set_rotation(90)
        with pytest.raises(LayoutError):
            trip_clip_rect(page, 1 if kind == "continuation" else 0)
