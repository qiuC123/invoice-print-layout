from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pymupdf

from invoice_print_layout.delivery import create_delivery_bundle
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.workflow import process_workspace
from tests.helpers import make_generic_trip_pdf, make_invoice_pdf, make_trip_pdf


def test_delivery_bundle_merges_complete_print_packages_and_writes_markdown(
    tmp_path: Path,
) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    make_trip_pdf(paths.incoming / "trip-1.pdf", amount="10.25")
    make_invoice_pdf(
        paths.incoming / "invoice-1.pdf",
        amount="10.25",
        invoice_number="10000000000000000001",
    )
    process_workspace(paths.root, "Test User")
    make_trip_pdf(paths.incoming / "trip-2.pdf", amount="20.50")
    make_invoice_pdf(
        paths.incoming / "invoice-2.pdf",
        amount="20.50",
        invoice_number="10000000000000000002",
    )
    process_workspace(paths.root, "Test User")
    outputs = sorted(paths.completed.glob("*.pdf"))

    bundle = create_delivery_bundle(
        paths,
        outputs,
        generated_at=datetime(2026, 9, 4, 12, 0, 0),
    )

    assert bundle.item_count == 2
    assert bundle.total_amount == Decimal("30.75")
    assert bundle.pdf_path.name == "2026-09-04_打车汇总_30.75元_Test User.pdf"
    assert bundle.markdown_path.stem == bundle.pdf_path.stem
    with pymupdf.open(bundle.pdf_path) as document:
        assert document.page_count == 2
        text = "".join(page.get_text("text") for page in document)
        assert "10.25" in text
        assert "20.50" in text
    markdown = bundle.markdown_path.read_text(encoding="utf-8")
    assert "- 打车：30.75 元" in markdown
    assert "- 总计：30.75 元" in markdown


def test_delivery_bundle_never_overwrites_existing_pair(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    make_trip_pdf(paths.incoming / "trip.pdf", amount="10.25")
    make_invoice_pdf(
        paths.incoming / "invoice.pdf",
        amount="10.25",
        invoice_number="10000000000000000001",
    )
    process_workspace(paths.root, "Test User")
    outputs = list(paths.completed.glob("*.pdf"))
    generated_at = datetime(2026, 9, 4, 12, 0, 0)

    first = create_delivery_bundle(paths, outputs, generated_at=generated_at)
    second = create_delivery_bundle(paths, outputs, generated_at=generated_at)

    assert first.pdf_path.exists()
    assert first.markdown_path.exists()
    assert second.pdf_path.name.endswith("_2.pdf")
    assert second.markdown_path.name.endswith("_2.md")


def test_delivery_marks_structural_unknown_provider_for_naming(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    make_generic_trip_pdf(paths.incoming / "trip.pdf", amount="44.60")
    make_invoice_pdf(
        paths.incoming / "invoice.pdf",
        amount="44.60",
        invoice_number="10000000000000000044",
    )
    process_workspace(paths.root, "Test User")

    bundle = create_delivery_bundle(paths, list(paths.completed.glob("*.pdf")))

    markdown = bundle.markdown_path.read_text(encoding="utf-8")
    assert "网约车（新平台待命名）" in markdown
    assert "结构、金额和唯一配对校验" in markdown
