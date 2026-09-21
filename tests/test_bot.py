from __future__ import annotations

from pathlib import Path
from typing import Any

import keyring
import pytest

from invoice_print_layout.bot import (
    BatchKind,
    BotController,
    BotInbox,
    DownloadedResource,
    FeishuBotSettings,
    IncomingMessage,
    COMMAND_HELP,
    configure_bot,
    read_bot_settings,
)
from invoice_print_layout.mail163 import MailImportSummary
from invoice_print_layout.storage import ensure_workspace, write_person_name
from tests.helpers import make_invoice_pdf, make_trip_pdf


class FakeGateway:
    def __init__(self) -> None:
        self.resources: dict[str, DownloadedResource] = {}
        self.replies: list[tuple[str, str]] = []
        self.sent_files: list[tuple[str, Path]] = []

    def reply_text(self, message_id: str, text: str) -> None:
        self.replies.append((message_id, text))

    def download_resource(
        self,
        message_id: str,
        resource_key: str,
        resource_type: str,
        fallback_name: str,
    ) -> DownloadedResource:
        return self.resources[resource_key]

    def send_file(self, chat_id: str, path: Path) -> None:
        self.sent_files.append((chat_id, path))


def message(
    message_id: str,
    *,
    text: str | None = None,
    message_type: str = "text",
    resource_key: str | None = None,
    file_name: str | None = None,
    sender: str = "ou_owner",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id="oc_chat",
        chat_type="p2p",
        sender_open_id=sender,
        sender_type="user",
        message_type=message_type,
        text=text,
        resource_key=resource_key,
        file_name=file_name,
    )


@pytest.mark.parametrize("command", ["帮助", "指令", "指令合集", "菜单", "?", "？", "help", "/help", "  菜单  "])
def test_help_collection_is_read_only_and_deduplicated(tmp_path: Path, command: str) -> None:
    gateway = FakeGateway()

    def no_mail() -> MailImportSummary:
        raise AssertionError("Help must not import mail")

    controller = BotController(tmp_path, tmp_path / "bot.toml",
                               FeishuBotSettings("cli_abcdefgh1234", "ou_owner"),
                               None, gateway, no_mail)
    batch = controller.inbox.start(BatchKind.TAKEOUT)
    incoming = message("om_help", text=command)
    controller.handle(incoming)
    controller.handle(incoming)
    assert gateway.replies == [("om_help", COMMAND_HELP)]
    assert controller.inbox.current() == batch
    assert gateway.sent_files == []
    for name in ("打车", "外卖", "状态", "处理", "补发", "取消", "记忆", "帮助"):
        assert name + "：" in COMMAND_HELP


def test_help_collection_requires_bound_owner(tmp_path: Path) -> None:
    gateway = FakeGateway()
    controller = BotController(tmp_path, tmp_path / "bot.toml",
                               FeishuBotSettings("cli_abcdefgh1234", "ou_owner"),
                               None, gateway)
    controller.handle(message("om_other_help", text="菜单", sender="ou_other"))
    assert gateway.replies == [("om_other_help", "无权使用此机器人")]


def test_configure_bot_keeps_secret_out_of_file(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    captured: dict[str, str] = {}

    def save_password(service: str, username: str, password: str) -> None:
        captured.update(service=service, username=username, password=password)

    monkeypatch.setattr(keyring, "set_password", save_password)
    config = tmp_path / "feishu_bot.toml"

    settings = configure_bot(config, "cli_abcdefgh1234", "secret-value-123456")

    assert settings == FeishuBotSettings("cli_abcdefgh1234")
    assert read_bot_settings(config) == settings
    assert "secret-value-123456" not in config.read_text(encoding="utf-8")
    assert captured["password"] == "secret-value-123456"


def test_inbox_preserves_batch_and_deduplicates_file(tmp_path: Path) -> None:
    inbox = BotInbox(tmp_path / "workspace")
    batch = inbox.start(BatchKind.TAKEOUT)

    first, duplicate = inbox.add_file(
        DownloadedResource("order.jpg", b"image bytes"),
        "om_1",
    )
    second, duplicate_again = inbox.add_file(
        DownloadedResource("renamed.jpg", b"image bytes"),
        "om_2",
    )

    assert not duplicate
    assert duplicate_again
    assert first == second
    current = inbox.current()
    assert current is not None
    assert len(current.files) == 1
    cancelled = inbox.clear_current("已取消")
    assert cancelled.name == batch.batch_id
    assert (cancelled / "files" / "order.jpg").read_bytes() == b"image bytes"
    assert inbox.current() is None


def test_controller_binds_owner_and_moves_takeout_to_review(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    paths = ensure_workspace(workspace)
    write_person_name(paths.config, "Test User")
    config = paths.root / "feishu_bot.toml"
    settings = FeishuBotSettings("cli_abcdefgh1234")
    gateway = FakeGateway()
    gateway.resources["img_1"] = DownloadedResource("order.jpg", b"image bytes")
    controller = BotController(workspace, config, settings, "123456", gateway)

    controller.handle(message("om_bind", text="绑定 123456"))
    controller.handle(message("om_start", text="外卖"))
    controller.handle(
        message(
            "om_image",
            message_type="image",
            resource_key="img_1",
            file_name="order.jpg",
        )
    )
    controller.handle(message("om_process", text="处理"))

    assert read_bot_settings(config).owner_open_id == "ou_owner"
    review_files = list((paths.review / "淘宝外卖").rglob("order.jpg"))
    assert len(review_files) == 1
    assert any("需要检查" in reply for _, reply in gateway.replies)
    assert controller.inbox.current() is None


def test_controller_accepts_binding_code_without_space(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    paths = ensure_workspace(workspace)
    config = paths.root / "feishu_bot.toml"
    settings = FeishuBotSettings("cli_abcdefgh1234")
    gateway = FakeGateway()
    controller = BotController(workspace, config, settings, "123456", gateway)

    controller.handle(message("om_bind", text="绑定123456"))

    assert read_bot_settings(config).owner_open_id == "ou_owner"
    assert gateway.replies[-1][1].startswith("绑定成功")


def test_controller_processes_ride_pdfs_and_sends_output(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    paths = ensure_workspace(workspace)
    write_person_name(paths.config, "Test User")
    trip = make_trip_pdf(tmp_path / "trip.pdf")
    invoice = make_invoice_pdf(tmp_path / "invoice.pdf")
    gateway = FakeGateway()
    gateway.resources["file_trip"] = DownloadedResource("trip.pdf", trip.read_bytes())
    gateway.resources["file_invoice"] = DownloadedResource(
        "invoice.pdf", invoice.read_bytes()
    )
    settings = FeishuBotSettings("cli_abcdefgh1234", "ou_owner")
    controller = BotController(
        workspace,
        paths.root / "feishu_bot.toml",
        settings,
        None,
        gateway,
    )

    controller.handle(message("om_start", text="打车"))
    controller.handle(
        message(
            "om_trip",
            message_type="file",
            resource_key="file_trip",
            file_name="trip.pdf",
        )
    )
    controller.handle(
        message(
            "om_invoice",
            message_type="file",
            resource_key="file_invoice",
            file_name="invoice.pdf",
        )
    )
    controller.handle(message("om_process", text="处理"))

    assert len(gateway.sent_files) == 2
    assert {path.suffix for _, path in gateway.sent_files} == {".pdf", ".md"}
    assert all(chat_id == "oc_chat" for chat_id, _ in gateway.sent_files)
    assert all(path.is_file() for _, path in gateway.sent_files)


def test_ride_command_imports_and_processes_today_mail(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    paths = ensure_workspace(workspace)
    write_person_name(paths.config, "Test User")
    trip = make_trip_pdf(tmp_path / "trip.pdf")
    invoice = make_invoice_pdf(tmp_path / "invoice.pdf")
    gateway = FakeGateway()

    def import_today_mail() -> MailImportSummary:
        (paths.incoming / "trip.pdf").write_bytes(trip.read_bytes())
        (paths.incoming / "invoice.pdf").write_bytes(invoice.read_bytes())
        return MailImportSummary(downloaded=2)

    settings = FeishuBotSettings("cli_abcdefgh1234", "ou_owner")
    controller = BotController(
        workspace,
        paths.root / "feishu_bot.toml",
        settings,
        None,
        gateway,
        import_today_mail,
    )
    controller.inbox.start(BatchKind.RIDE)

    controller.handle(message("om_mail", text="打车"))

    assert controller.inbox.current() is None
    assert len(gateway.sent_files) == 2
    assert {path.suffix for _, path in gateway.sent_files} == {".pdf", ".md"}
    assert any("今日邮件下载 2 个 PDF" in text for _, text in gateway.replies)
    assert controller.inbox.current() is None
    assert not list(paths.incoming.iterdir())


def test_memory_command_sends_summary_without_deleting_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    paths = ensure_workspace(workspace)
    retained = paths.archived / "retained" / "原始电子发票.pdf"
    retained.parent.mkdir()
    retained.write_bytes(b"invoice")
    gateway = FakeGateway()
    settings = FeishuBotSettings("cli_abcdefgh1234", "ou_owner")
    controller = BotController(
        workspace,
        paths.root / "feishu_bot.toml",
        settings,
        None,
        gateway,
    )

    controller.handle(message("om_memory", text="记忆"))

    assert retained.exists()
    assert gateway.sent_files == [("oc_chat", paths.memory_summary)]
    assert "本次未删除任何文件" in gateway.replies[-1][1]
