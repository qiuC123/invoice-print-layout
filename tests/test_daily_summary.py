from datetime import date, datetime
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.bot import (
    BatchKind, BotController, BotError, FeishuBotSettings, parse_summary_date,
)
from invoice_print_layout.delivery import create_delivery_bundle, outputs_for_processing_date
from invoice_print_layout.reliability import read_json
from invoice_print_layout.storage import WorkspacePaths, append_history, ensure_workspace
from tests.helpers import make_trip_pdf
from tests.test_bot import FakeGateway, message


def record(paths: WorkspacePaths, name: str, created: str, category: str, amount: str) -> dict[str, Any]:
    output = make_trip_pdf(paths.completed / f"{name}.pdf", amount=amount)
    entry = {
        "created_at": created, "invoice_date": "2026-01-15",
        "invoice_number": name, "category": category, "amount": amount,
        "provider": "Test", "person_name": "Test User", "output": str(output),
    }
    append_history(paths.history, entry)
    return entry


def controller_for(paths: WorkspacePaths, gateway: FakeGateway) -> BotController:
    def forbidden_mail() -> Any:
        raise AssertionError("Summary must not read mail")
    return BotController(paths.root, paths.root / "bot.toml",
                         FeishuBotSettings("cli_abcdefgh1234", "ou_owner"),
                         None, gateway, forbidden_mail)


@pytest.mark.parametrize(("command", "expected"), [
    ("汇总", date(2026, 1, 1)), ("汇总 今天", date(2026, 1, 1)),
    ("汇总 昨天", date(2025, 12, 31)), ("汇总 2024-02-29", date(2024, 2, 29)),
])
def test_summary_date_parser(command: str, expected: date) -> None:
    assert parse_summary_date(command, date(2026, 1, 1)) == expected


@pytest.mark.parametrize("command", ["汇总 2026-02-30", "汇总 2027-01-01", "汇总 2026-9-4", "汇总 上周"])
def test_invalid_summary_date(command: str) -> None:
    with pytest.raises(BotError):
        parse_summary_date(command, date(2026, 9, 6))


def test_daily_selection_and_bundle_preserve_ledger_and_distinguish_dates(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    first = record(paths, "ride", "2026-09-04T00:00:00", "打车", "10.25")
    record(paths, "coffee", "2026-09-04T15:59:59+00:00", "咖啡", "20.50")
    record(paths, "next-day", "2026-09-04T16:00:00+00:00", "打车", "100.00")
    append_history(paths.history, first)  # An identical ledger replay is counted once.
    before = paths.history.read_bytes()
    outputs = outputs_for_processing_date(paths, date(2026, 9, 4))
    assert [path.stem for path in outputs] == ["ride", "coffee"]
    bundle = create_delivery_bundle(paths, outputs, report_date=date(2026, 9, 4),
                                    generated_at=datetime(2026, 9, 6, 12, 0))
    assert bundle.pdf_path.name.startswith("2026-09-04_按处理日_票据汇总_30.75元_")
    markdown = bundle.markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# 2026-09-04")
    assert "生成时间：2026-09-06T12:00:00" in markdown
    assert "不是按消费日期或开票日期筛选" in markdown
    assert "打车：10.25 元" in markdown and "咖啡：20.50 元" in markdown
    assert "总计：30.75 元" in markdown and "1×1" in markdown
    assert paths.history.read_bytes() == before
    with pymupdf.open(bundle.pdf_path) as pdf:
        assert pdf.page_count == 2


def test_summary_preserves_batch_and_deduplicates_message(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    record(paths, "ride", "2026-09-04T12:00:00", "打车", "10.25")
    before = paths.history.read_bytes()
    gateway = FakeGateway()
    controller = controller_for(paths, gateway)
    batch = controller.inbox.start(BatchKind.TAKEOUT)
    incoming = message("om_summary", text="汇总 2026-09-04")
    controller.handle(incoming)
    controller.handle(incoming)
    assert len(gateway.sent_files) == 2 and len(gateway.replies) == 1
    assert "2026-09-04 汇总完成" in gateway.replies[0][1]
    assert controller.inbox.current() == batch
    assert paths.history.read_bytes() == before
    assert len(list(paths.delivery.glob("*.pdf"))) == 1


@pytest.mark.parametrize("command", ["汇总 2026-09-04", "汇总 2026-02-30", "汇总 9999-01-01"])
def test_empty_or_invalid_summary_returns_no_files(tmp_path: Path, command: str) -> None:
    paths = ensure_workspace(tmp_path)
    gateway = FakeGateway()
    controller = controller_for(paths, gateway)
    controller.handle(message("om_empty", text=command))
    assert len(gateway.replies) == 1
    assert not gateway.sent_files
    assert not list(paths.delivery.iterdir())
    assert not list(controller.jobs.glob("*.json"))


def test_missing_old_metadata_is_not_silently_omitted(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    output = make_trip_pdf(paths.completed / "old.pdf")
    append_history(paths.history, {"created_at": "2026-09-04T12:00:00", "output": str(output)})
    gateway = FakeGateway()
    controller = controller_for(paths, gateway)
    controller.handle(message("om_old", text="汇总 2026-09-04"))
    assert "暂无法汇总" in gateway.replies[0][1]
    assert not gateway.sent_files and output.exists()


def test_summary_retry_freezes_date_and_only_sends_missing_file(tmp_path: Path, monkeypatch: Any) -> None:
    paths = ensure_workspace(tmp_path)
    record(paths, "ride", "2026-09-04T12:00:00", "打车", "10.25")
    monkeypatch.setattr("invoice_print_layout.bot.parse_summary_date", lambda _: date(2026, 9, 4))

    class FailMarkdown(FakeGateway):
        def send_file(self, chat_id: str, path: Path) -> None:
            if path.suffix == ".md":
                raise BotError("interrupted")
            super().send_file(chat_id, path)

    failing = FailMarkdown()
    controller = controller_for(paths, failing)
    incoming = message("om_retry_summary", text="汇总 昨天")
    with pytest.raises(BotError, match="interrupted"):
        controller.handle(incoming)
    assert [path.suffix for _, path in failing.sent_files] == [".pdf"]
    record(paths, "later", "2026-09-04T13:00:00", "咖啡", "100.00")
    before = paths.history.read_bytes()
    monkeypatch.setattr("invoice_print_layout.bot.parse_summary_date", lambda _: date(2026, 9, 5))
    gateway = FakeGateway()
    restarted = controller_for(paths, gateway)
    restarted.resume_deliveries()
    restarted.handle(incoming)
    assert [path.suffix for _, path in gateway.sent_files] == [".md"]
    assert "总计：10.25 元" in gateway.sent_files[0][1].read_text(encoding="utf-8")
    assert read_json(restarted._job_path(incoming))["report_date"] == "2026-09-04"
    assert len(list(paths.delivery.glob("*.pdf"))) == 1
    assert paths.history.read_bytes() == before


def test_summary_requires_owner(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    gateway = FakeGateway()
    controller_for(paths, gateway).handle(message("om_not_owner", text="汇总", sender="ou_other"))
    assert gateway.replies[0][1] == "无权使用此机器人"
    assert not gateway.sent_files
