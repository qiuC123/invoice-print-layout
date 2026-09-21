from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import invoice_print_layout.workflow as workflow_module
from invoice_print_layout.models import OutcomeStatus
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.workflow import process_workspace
from tests.helpers import (
    make_caocao_trip_pdf,
    make_generic_trip_pdf,
    make_invoice_pdf,
    make_trip_pdf,
    make_xiangdao_invoice_pdf,
    make_xiangdao_trip_pdf,
)


def test_success_archives_originals_and_duplicate_is_flagged(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_trip_pdf(workspace.incoming / "trip.pdf")
    make_invoice_pdf(workspace.incoming / "invoice.pdf")

    paths, first = process_workspace(workspace.root, "Test User")

    assert first.success_count == 1
    assert not list(paths.incoming.glob("*.pdf"))
    outputs = list(paths.completed.glob("*.pdf"))
    archives = [path for path in paths.archived.iterdir() if path.is_dir()]
    assert len(outputs) == 1
    assert len(archives) == 1
    assert outputs[0].name == "2026-01-15_滴滴_123.45元_Test User.pdf"
    assert (archives[0] / "原始电子发票.pdf").exists()
    assert (archives[0] / "原始行程单.pdf").exists()
    history_entry = json.loads(paths.history.read_text(encoding="utf-8").splitlines()[0])
    assert history_entry["record_version"] == 2
    assert history_entry["category"] == "打车"
    assert history_entry["provider"] == "滴滴"
    assert history_entry["amount"] == "123.45"
    assert history_entry["person_name"] == "Test User"
    assert paths.memory_summary.is_file()
    assert "123.45 元" in paths.memory_summary.read_text(encoding="utf-8")

    shutil.copy2(archives[0] / "原始电子发票.pdf", paths.incoming / "invoice-again.pdf")
    shutil.copy2(archives[0] / "原始行程单.pdf", paths.incoming / "trip-again.pdf")
    _, second = process_workspace(workspace.root, "Test User")

    assert second.duplicate_count == 1
    assert second.count(OutcomeStatus.SUCCESS) == 0
    assert len(outputs) == 1
    assert any((paths.review / "重复件").iterdir())


def test_ambiguous_pair_moves_all_files_to_review(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_trip_pdf(workspace.incoming / "trip-1.pdf")
    make_trip_pdf(workspace.incoming / "trip-2.pdf")
    make_invoice_pdf(workspace.incoming / "invoice.pdf")

    paths, summary = process_workspace(workspace.root, "Test User")

    assert summary.review_count == 1
    assert not list(paths.incoming.glob("*.pdf"))
    issue_dirs = [path for path in paths.review.iterdir() if path.is_dir()]
    assert len(issue_dirs) == 1
    assert len(list(issue_dirs[0].glob("*.pdf"))) == 3
    assert (issue_dirs[0] / "原因.txt").exists()


def test_caocao_output_uses_provider_name(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_caocao_trip_pdf(workspace.incoming / "trip.pdf")
    make_invoice_pdf(
        workspace.incoming / "invoice.pdf",
        amount="52.14",
        provider="caocao",
    )

    paths, summary = process_workspace(workspace.root, "Test User")

    assert summary.success_count == 1
    assert [path.name for path in paths.completed.glob("*.pdf")] == [
        "2026-01-15_曹操_52.14元_Test User.pdf"
    ]


def test_xiangdao_output_uses_provider_name(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_xiangdao_trip_pdf(workspace.incoming / "trip.pdf")
    make_xiangdao_invoice_pdf(workspace.incoming / "invoice.pdf")

    paths, summary = process_workspace(workspace.root, "Test User")

    assert summary.success_count == 1
    assert [path.name for path in paths.completed.glob("*.pdf")] == [
        "2026-09-04_享道_17.72元_Test User.pdf"
    ]


def test_structural_unknown_provider_is_processed_with_generic_name(
    tmp_path: Path,
) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_generic_trip_pdf(workspace.incoming / "trip.pdf")
    make_invoice_pdf(
        workspace.incoming / "invoice.pdf",
        amount="44.60",
        invoice_number="10000000000000000044",
    )

    paths, summary = process_workspace(workspace.root, "Test User")

    assert summary.success_count == 1
    assert [path.name for path in paths.completed.glob("*.pdf")] == [
        "2026-01-15_网约车_44.60元_Test User.pdf"
    ]


def test_history_failure_rolls_back_output_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_trip_pdf(workspace.incoming / "trip.pdf")
    make_invoice_pdf(workspace.incoming / "invoice.pdf")

    def fail_history_write(*args: object, **kwargs: object) -> None:
        raise OSError("history unavailable")

    monkeypatch.setattr(workflow_module, "append_history", fail_history_write)
    paths, summary = process_workspace(workspace.root, "Test User")

    assert summary.review_count == 1
    assert not list(paths.completed.glob("*.pdf"))
    assert not list(paths.archived.iterdir())
    assert not list(paths.incoming.glob("*.pdf"))


def test_corrupt_history_leaves_incoming_files_untouched(tmp_path: Path) -> None:
    workspace = ensure_workspace(tmp_path / "workspace")
    make_trip_pdf(workspace.incoming / "trip.pdf")
    make_invoice_pdf(workspace.incoming / "invoice.pdf")
    workspace.history.write_text("not-json\n", encoding="utf-8")

    paths, summary = process_workspace(workspace.root, "Test User")

    assert summary.review_count == 1
    assert len(list(paths.incoming.glob("*.pdf"))) == 2
    assert not list(paths.completed.glob("*.pdf"))
    assert "待处理文件未移动" in paths.latest_report.read_text(encoding="utf-8")
