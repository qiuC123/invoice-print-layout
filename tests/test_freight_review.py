from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.documents import (
    PdfInspectionError,
    classify_text,
    inspect_pdf,
    pair_documents,
    sha256_file,
)
from invoice_print_layout.models import DocumentKind, RideProvider
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.workflow import process_workspace
from tests.helpers import make_invoice_pdf, make_trip_pdf


def _add_synthetic_service(path: Path, service: str) -> None:
    with pymupdf.open(path) as document:
        document[0].insert_text((72, 250), service, fontname="china-s", fontsize=11)
        document.saveIncr()


def test_freight_invoice_cannot_pair_with_same_amount_ride(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    trip = make_trip_pdf(workspace.incoming / "trip.pdf")
    invoice = make_invoice_pdf(workspace.incoming / "invoice.pdf")
    _add_synthetic_service(invoice, "货拉拉 *运输服务*货物运输服务")
    expected_hashes = {sha256_file(trip), sha256_file(invoice)}

    paths, summary = process_workspace(workspace.root, "Synthetic Test")

    assert summary.success_count == 0
    assert summary.review_count >= 1
    assert not list(paths.completed.glob("*.pdf"))
    assert not list(paths.archived.iterdir())
    assert not paths.history.exists() or not paths.history.read_text(encoding="utf-8").strip()
    assert {sha256_file(path) for path in paths.review.rglob("*.pdf")} == expected_hashes
    assert any("货运" in path.read_text(encoding="utf-8") for path in paths.review.rglob("原因.txt"))


@pytest.mark.parametrize("service", [
    "货拉拉", "*运输服务*货物运输服务", "道路货运服务", "拉货服务", "FREIGHT SERVICE",
    "Cargo transportation", "LALAMOVE", "HUOLALA",
])
def test_explicit_freight_signals_require_review_without_mutating_source(
    tmp_path: Path, service: str,
) -> None:
    invoice = make_invoice_pdf(tmp_path / "invoice.pdf")
    _add_synthetic_service(invoice, service)
    original_hash = sha256_file(invoice)

    with pytest.raises(PdfInspectionError, match="货运"):
        inspect_pdf(invoice)

    assert sha256_file(invoice) == original_hash


def test_freight_trip_is_also_rejected(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    trip = make_trip_pdf(workspace.incoming / "trip.pdf")
    # The known ride title must not override conflicting explicit freight text.
    _add_synthetic_service(trip, "货拉拉 货物运输")
    invoice = make_invoice_pdf(workspace.incoming / "invoice.pdf")
    expected_hashes = {sha256_file(trip), sha256_file(invoice)}

    paths, summary = process_workspace(workspace.root, "Synthetic Test")

    assert summary.success_count == 0
    assert {sha256_file(path) for path in paths.review.rglob("*.pdf")} == expected_hashes


@pytest.mark.parametrize("service", ["陌生商家 客运服务", "交通运输服务", "Unknown Merchant RIDE SERVICE"])
def test_unfamiliar_nonfreight_invoice_keeps_existing_pairing(
    tmp_path: Path, service: str,
) -> None:
    trip = inspect_pdf(make_trip_pdf(tmp_path / "trip.pdf"))
    invoice_path = make_invoice_pdf(tmp_path / "invoice.pdf")
    _add_synthetic_service(invoice_path, service)
    invoice = inspect_pdf(invoice_path)

    assert invoice.kind is DocumentKind.INVOICE
    assert invoice.provider is RideProvider.UNKNOWN
    groups, issues = pair_documents([trip, invoice])
    assert len(groups) == 1
    assert not issues


def test_freight_still_has_generic_invoice_structure() -> None:
    # Classification of structure is unchanged; only automatic intake is gated.
    assert classify_text("电子发票 发票号码12345678 货拉拉") is DocumentKind.INVOICE
