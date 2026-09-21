from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.layout import (
    LayoutError,
    compose_print_package,
    required_source_fragments,
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
