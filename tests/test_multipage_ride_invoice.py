from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.documents import (
    PdfInspectionError,
    compact_text,
    inspect_pdf,
    pair_documents,
    parse_amount,
)
from invoice_print_layout.layout import (
    A4_HEIGHT,
    A4_WIDTH,
    SLOT_GAP,
    SLOT_MARGIN,
    LayoutError,
    compose_print_package,
    invoice_clip_rect,
    required_source_fragments,
    trip_clip_rect,
    validate_print_package,
)
from invoice_print_layout.models import DocumentKind
from tests.helpers import make_trip_pdf


_NUMBER = "12345678901234567890"
_DATE = "2026年10月08日"


def _invoice(
    path: Path,
    *,
    page_numbers: tuple[int | None, ...] = (1, 2),
    declared_pages: int = 2,
    invoice_numbers: tuple[str, ...] = (_NUMBER, _NUMBER),
    invoice_dates: tuple[str, ...] = (_DATE, _DATE),
    titles: tuple[bool, ...] = (True, True),
    totals: tuple[tuple[str, ...], ...] = ((), ("151.80",)),
    compatibility_page_markers: bool = False,
    tall: bool = False,
    page_bottoms: tuple[int, ...] | None = None,
) -> Path:
    """A fictitious continued invoice whose small total is detached in text order."""
    with pymupdf.open() as document:
        for index, page_number in enumerate(page_numbers):
            page = document.new_page(width=595.28, height=841.89)
            page.insert_text(
                (35, 45),
                "电子发票（普通发票）" if titles[index] else "SUPPORTING DOCUMENT",
                fontname="china-s",
                fontsize=15,
            )
            page.insert_text(
                (35, 80), "发票号码：" + invoice_numbers[index],
                fontname="china-s", fontsize=10,
            )
            page.insert_text(
                (35, 105), "开票日期：" + invoice_dates[index],
                fontname="china-s", fontsize=10,
            )
            page.insert_text((35, 140), f"INVOICE PAGE {index + 1} BUSINESS CONTENT", fontsize=11)
            for total_index, _ in enumerate(totals[index]):
                page.insert_text(
                    (300, 190 + total_index * 28), "价税合计（小写）",
                    fontname="china-s", fontsize=11,
                )
            bottom = page_bottoms[index] if page_bottoms is not None else 800 if tall else 300
            page.draw_rect(pymupdf.Rect(20, 20, 575, bottom), color=(0.2, 0.2, 0.2))
            if page_number is not None:
                marker = f"共 {declared_pages} 页 第 {page_number} 页"
                if compatibility_page_markers:
                    marker = marker.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
                page.insert_text((35, bottom - 15), marker, fontname="china-s", fontsize=10)
            # PDF content order deliberately separates the explicit amount label,
            # its value, and the currency glyph. Only their printed row links them.
            for total_index, amount in enumerate(totals[index]):
                y = 190 + total_index * 28
                page.insert_text((415, y), amount, fontsize=11)
                page.insert_text((395, y), "¥", fontsize=11)
        document.save(path)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _asymmetric_trip(path: Path) -> Path:
    with pymupdf.open() as document:
        for index, bottom in enumerate((810, 235)):
            page = document.new_page(width=595.28, height=841.89)
            page.insert_text(
                (72, 55), "DIDI TRAVEL - TRIP TABLE" if index == 0 else "TRIP PAGE 2",
                fontsize=15,
            )
            page.insert_text(
                (72, 80), "TRIP TOTAL: 151.80" if index == 0 else "TRIP DETAILS",
                fontsize=11,
            )
            page.insert_text((72, 110), "INDEX PICKUP DESTINATION AMOUNT", fontsize=10)
            page.insert_text((72, 140), f"TRIP PAGE {index + 1} BUSINESS CONTENT", fontsize=11)
            page.draw_rect(pymupdf.Rect(65, 95, 530, bottom), width=0.5)
            page.insert_text((470, 835), f"PAGE: {index + 1}/2", fontsize=9)
        document.save(path)
    return path


@pytest.mark.parametrize("compatibility_page_markers", [False, True])
def test_continued_invoice_reads_detached_last_page_total_and_pairs_once(
    tmp_path: Path, compatibility_page_markers: bool,
) -> None:
    invoice_path = _invoice(
        tmp_path / "continued-invoice.pdf",
        compatibility_page_markers=compatibility_page_markers,
    )
    trip_path = make_trip_pdf(tmp_path / "trip.pdf", amount="151.80")
    before = {path: _digest(path) for path in (invoice_path, trip_path)}
    with pymupdf.open(invoice_path) as document:
        text = "\n".join(page.get_text() for page in document)
    with pytest.raises(PdfInspectionError):
        parse_amount(text, DocumentKind.INVOICE)

    invoice = inspect_pdf(invoice_path)
    trip = inspect_pdf(trip_path)
    assert invoice.kind is DocumentKind.INVOICE
    assert invoice.page_count == 2 and invoice.amount == Decimal("151.80")
    assert invoice.invoice_number == _NUMBER
    assert invoice.invoice_date is not None and invoice.invoice_date.isoformat() == "2026-10-08"
    assert invoice.sha256 == before[invoice_path]
    groups, issues = pair_documents([invoice, trip])
    assert not issues and len(groups) == 1
    assert groups[0].invoice is invoice
    assert {path: _digest(path) for path in before} == before


@pytest.mark.parametrize(
    "changes",
    [
        {"invoice_numbers": (_NUMBER, "12345678901234567891")},
        {"invoice_dates": (_DATE, "2026年10月09日")},
        {"titles": (True, False)},
        {"titles": (False, True)},
        {"page_numbers": (1, 1)},
        {"page_numbers": (2, 1)},
        {"page_numbers": (1, 3)},
        {"page_numbers": (None, 2)},
        {"page_numbers": (1, None)},
        {"declared_pages": 3},
        {"totals": ((), ())},
        {"totals": ((), ("151.80", "152.80"))},
        {"totals": (("150.80",), ("151.80",))},
    ],
    ids=[
        "different-invoice-number", "different-invoice-date",
        "missing-last-title", "missing-first-title", "duplicate-page-number",
        "reversed-pages", "out-of-range-page", "missing-first-page-marker",
        "missing-last-page-marker", "missing-continuation-page", "missing-total",
        "conflicting-totals-on-one-page", "conflicting-totals-across-pages",
    ],
)
def test_continued_invoice_requires_one_complete_unambiguous_invoice(
    tmp_path: Path, changes: dict[str, object],
) -> None:
    path = _invoice(tmp_path / "uncertain-invoice.pdf", **changes)  # type: ignore[arg-type]
    before = _digest(path)
    with pytest.raises(PdfInspectionError):
        inspect_pdf(path)
    assert _digest(path) == before


def test_continued_invoice_does_not_override_explicit_conflicting_totals_with_geometry(
    tmp_path: Path,
) -> None:
    source = _invoice(tmp_path / "continued-invoice.pdf")
    path = tmp_path / "explicitly-conflicting-totals.pdf"
    with pymupdf.open(source) as document:
        page = document[-1]
        page.insert_text((35, 235), "TOTAL WITH TAX: 151.80", fontsize=11)
        page.insert_text((35, 260), "（小写）¥152.80", fontname="china-s", fontsize=11)
        document.save(path)
    before = _digest(path)
    with pymupdf.open(path) as document:
        text = document[-1].get_text()
    with pytest.raises(PdfInspectionError, match="歧义"):
        parse_amount(text, DocumentKind.INVOICE)
    with pytest.raises(PdfInspectionError, match="歧义"):
        inspect_pdf(path)
    assert _digest(path) == before


@pytest.mark.parametrize("page_number", [1, 2])
def test_single_remaining_page_cannot_impersonate_a_complete_continued_invoice(
    tmp_path: Path, page_number: int,
) -> None:
    path = _invoice(
        tmp_path / "incomplete-continued-invoice.pdf",
        page_numbers=(page_number,),
        declared_pages=2,
        totals=(("151.80",),),
    )
    before = _digest(path)
    with pytest.raises(PdfInspectionError):
        inspect_pdf(path)
    assert _digest(path) == before


@pytest.mark.parametrize("tall, expected_pages", [(False, 2), (True, 3)])
def test_continued_invoice_layout_preserves_every_page_and_native_content(
    tmp_path: Path, tall: bool, expected_pages: int,
) -> None:
    trip = make_trip_pdf(tmp_path / "trip.pdf", pages=2 if not tall else 1, amount="151.80")
    invoice = _invoice(tmp_path / "invoice.pdf", tall=tall)
    before = {path: _digest(path) for path in (trip, invoice)}
    output = tmp_path / "print.pdf"
    fragments = required_source_fragments(trip, invoice)
    assert compact_text("INVOICE PAGE 1 BUSINESS CONTENT") in fragments
    assert compact_text("INVOICE PAGE 2 BUSINESS CONTENT") in fragments

    actual_pages = compose_print_package(trip, invoice, output)
    assert actual_pages == expected_pages
    validate_print_package(output, expected_pages, "151.80", fragments)
    with pymupdf.open(output) as document:
        texts = [page.get_text() for page in document]
        combined = "\n".join(texts)
        assert combined.index("DIDI TRAVEL - TRIP TABLE") < combined.index("INVOICE PAGE 1 BUSINESS CONTENT")
        assert combined.index("INVOICE PAGE 1 BUSINESS CONTENT") < combined.index("INVOICE PAGE 2 BUSINESS CONTENT")
        assert all(abs(page.rect.width - A4_WIDTH) < 1 and abs(page.rect.height - A4_HEIGHT) < 1
                   for page in document)
        assert all(not page.get_images(full=True) for page in document)
        assert any(page.get_drawings() for page in document)
        if tall:
            assert "INVOICE PAGE 1 BUSINESS CONTENT" in texts[1]
            assert "INVOICE PAGE 2 BUSINESS CONTENT" in texts[2]
            # Each near-A4 original gets a full-width page, rather than being
            # squeezed into a half-page just to preserve a fixed two-up grid.
            for page in list(document)[1:]:
                assert page.search_for("INVOICE PAGE")[0].y0 < A4_HEIGHT / 4
                drawings = page.get_drawings()
                assert max(drawing["rect"].height for drawing in drawings) > A4_HEIGHT * 0.85
        else:
            assert "TRIP PAGE 2" in texts[0]
            assert "INVOICE PAGE 1 BUSINESS CONTENT" in texts[1]
            assert "INVOICE PAGE 2 BUSINESS CONTENT" in texts[1]
            first, second = (document[1].search_for(f"INVOICE PAGE {number} BUSINESS CONTENT")[0]
                             for number in (1, 2))
            assert first.y0 < second.y0
    assert {path: _digest(path) for path in before} == before


def test_continued_invoice_missing_final_page_fails_source_validation(tmp_path: Path) -> None:
    trip = make_trip_pdf(tmp_path / "trip.pdf", amount="151.80")
    invoice = _invoice(tmp_path / "invoice.pdf")
    fragments = required_source_fragments(trip, invoice)
    output = tmp_path / "missing-final-page.pdf"
    # The amount remains visible on the trip, so amount-only validation cannot
    # hide omission of the invoice continuation and its required business text.
    with pymupdf.open(trip) as trip_pdf, pymupdf.open(invoice) as invoice_pdf, pymupdf.open() as document:
        for source in (trip_pdf, invoice_pdf):
            target = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            target.show_pdf_page(target.rect, source, 0)
        document.save(output)
    with pytest.raises(LayoutError, match="缺少源票据正文"):
        validate_print_package(output, 2, "151.80", fragments)


@pytest.mark.parametrize("swap_reduces_pages", [True, False], ids=["four-to-three-pages", "tie-keeps-original-order"])
def test_two_page_invoice_print_copy_swaps_only_when_it_saves_an_a4_page(
    tmp_path: Path, swap_reduces_pages: bool,
) -> None:
    trip = _asymmetric_trip(tmp_path / "trip-long-then-short.pdf")
    invoice = _invoice(
        tmp_path / "same-complete-invoice.pdf",
        page_bottoms=(800, 300) if swap_reduces_pages else (300, 300),
    )
    before = {path: _digest(path) for path in (trip, invoice)}
    inspected_invoice, inspected_trip = inspect_pdf(invoice), inspect_pdf(trip)
    groups, issues = pair_documents([inspected_invoice, inspected_trip])
    assert not issues and len(groups) == 1

    # Establish the fixture's available page combinations independently of the
    # composition result: original order needs four pages in the asymmetric
    # case, while the two short continuations can share a full-width A4 page.
    with pymupdf.open(trip) as trip_pdf, pymupdf.open(invoice) as invoice_pdf:
        clips = [trip_clip_rect(page, index) for index, page in enumerate(trip_pdf)]
        clips.extend(invoice_clip_rect(page) for page in invoice_pdf)
    heights = [clip.height * (A4_WIDTH - 2 * SLOT_MARGIN) / clip.width for clip in clips]
    available = A4_HEIGHT - 2 * SLOT_MARGIN
    assert heights[0] + heights[1] + SLOT_GAP > available
    assert heights[1] + heights[3] + SLOT_GAP <= available
    if swap_reduces_pages:
        assert heights[1] + heights[2] + SLOT_GAP > available
        assert heights[2] + heights[3] + SLOT_GAP > available
    else:
        assert heights[1] + heights[2] + SLOT_GAP <= available

    fragments = required_source_fragments(trip, invoice)
    output = tmp_path / "three-page-print-copy.pdf"
    assert compose_print_package(trip, invoice, output) == 3
    validate_print_package(output, 3, "151.80", fragments)
    with pymupdf.open(output) as document:
        texts = [page.get_text() for page in document]
        middle_invoice = 2 if swap_reduces_pages else 1
        final_invoice = 3 - middle_invoice
        assert "TRIP PAGE 1 BUSINESS CONTENT" in texts[0]
        assert "TRIP PAGE 2 BUSINESS CONTENT" not in texts[0]
        assert "INVOICE PAGE" not in texts[0]
        assert "TRIP PAGE 2 BUSINESS CONTENT" in texts[1]
        assert f"INVOICE PAGE {middle_invoice} BUSINESS CONTENT" in texts[1]
        assert f"INVOICE PAGE {final_invoice} BUSINESS CONTENT" not in texts[1]
        assert f"INVOICE PAGE {final_invoice} BUSINESS CONTENT" in texts[2]
        assert "TRIP PAGE" not in texts[2]
        trip_continuation = document[1].search_for("TRIP PAGE 2 BUSINESS CONTENT")[0]
        invoice_continuation = document[1].search_for(f"INVOICE PAGE {middle_invoice} BUSINESS CONTENT")[0]
        assert trip_continuation.y0 < invoice_continuation.y0
        combined = "\n".join(texts)
        assert all(combined.count(f"{kind} PAGE {number} BUSINESS CONTENT") == 1
                   for kind in ("TRIP", "INVOICE") for number in (1, 2))
        assert all(not page.get_images(full=True) and page.get_drawings() for page in document)
        assert all(abs(page.rect.width - A4_WIDTH) < 1 and abs(page.rect.height - A4_HEIGHT) < 1
                   for page in document)
    assert {path: _digest(path) for path in before} == before
