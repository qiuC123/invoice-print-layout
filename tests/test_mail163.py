from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any

import keyring
import pytest

from invoice_print_layout.mail163 import (
    MailImportError,
    Mail163Settings,
    configure_mail,
    import_pdf_attachments,
    import_takeout_invoice_links,
    read_mail_settings,
)
from invoice_print_layout.storage import ensure_workspace
from tests.helpers import make_invoice_pdf


class FakeImap:
    def __init__(self, messages: dict[bytes, bytes]) -> None:
        self.messages = messages
        self.login_args: tuple[str, str] | None = None
        self.id_sent = False
        self.readonly: bool | None = None
        self.fetch_queries: list[str] = []
        self.logged_out = False

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.login_args = (user, password)
        return "OK", [b"logged in"]

    def xatom(self, name: str, *args: str) -> tuple[str, list[bytes]]:
        self.id_sent = name == "ID" and bool(args)
        return "OK", [b"ID accepted"]

    def select(
        self, mailbox: str = "INBOX", readonly: bool = False
    ) -> tuple[str, list[bytes]]:
        assert mailbox == "INBOX"
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        assert command == "FETCH"
        uid = args[0]
        query = args[1]
        assert isinstance(uid, bytes)
        assert isinstance(query, str)
        self.fetch_queries.append(query)
        return "OK", [(b"body", self.messages[uid])]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logged out"]


class FailingSelectImap(FakeImap):
    def select(
        self, mailbox: str = "INBOX", readonly: bool = False
    ) -> tuple[str, list[bytes]]:
        self.readonly = readonly
        return "NO", [b"folder unavailable"]


def _mail_with_pdf(pdf_data: bytes) -> bytes:
    message = EmailMessage()
    message["Subject"] = "您的电子发票和行程单"
    message.set_content("附件请查收")
    message.add_attachment(
        pdf_data,
        maintype="application",
        subtype="pdf",
        filename="电子发票.pdf",
    )
    return message.as_bytes()


def _takeout_mail() -> bytes:
    message = EmailMessage()
    message["Subject"] = "淘宝闪购平台订单发票开具完成通知"
    message.set_content("订单号 1000000000000000001")
    message.add_alternative(
        '<p>订单号 1000000000000000001</p>'
        '<a href="https://bucket.oss-cn-zhangjiakou.aliyuncs.com/invoice.jpg?sig=x">发票1</a>'
        '<a href="https://bucket.oss-cn-zhangjiakou.aliyuncs.com/invoice.zip?sig=y">发票2</a>',
        subtype="html",
    )
    return message.as_bytes()


def test_configure_mail_keeps_auth_code_out_of_file(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    captured: dict[str, str] = {}

    def save_password(service: str, username: str, password: str) -> None:
        captured.update(service=service, username=username, password=password)

    monkeypatch.setattr(keyring, "set_password", save_password)
    config = tmp_path / "mail163.toml"

    settings = configure_mail(config, "USER@163.COM", "INBOX", "local-auth-code")

    assert settings.address == "user@163.com"
    assert read_mail_settings(config) == Mail163Settings("user@163.com", "INBOX")
    assert "local-auth-code" not in config.read_text(encoding="utf-8")
    assert captured["password"] == "local-auth-code"


def test_import_is_readonly_and_deduplicates_attachments(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    invoice_path = make_invoice_pdf(tmp_path / "source.pdf")
    raw_mail = _mail_with_pdf(invoice_path.read_bytes())
    client = FakeImap({b"101": raw_mail})

    first = import_pdf_attachments(
        paths,
        Mail163Settings("user@163.com"),
        "local-auth-code",
        imap_factory=lambda *args, **kwargs: client,
    )

    assert first.downloaded == 1
    assert client.login_args == ("user@163.com", "local-auth-code")
    assert client.id_sent
    assert client.readonly is True
    assert client.fetch_queries == ["(BODY.PEEK[])"]
    assert client.logged_out
    assert len(list(paths.incoming.glob("*.pdf"))) == 1

    second_client = FakeImap({b"101": raw_mail})
    second = import_pdf_attachments(
        paths,
        Mail163Settings("user@163.com"),
        "local-auth-code",
        imap_factory=lambda *args, **kwargs: second_client,
    )

    assert second.downloaded == 0
    assert second.duplicates == 1
    assert len(list(paths.incoming.glob("*.pdf"))) == 1


@pytest.mark.parametrize('kind', [None, '顺丰', '同城', '全部'])
def test_courier_mail_isolated_and_retry_deduplicated(tmp_path: Path, kind: str | None) -> None:
    from tests.test_courier import make_courier_pair
    paths = ensure_workspace(tmp_path / 'workspace')
    messages = {b'1': _mail_with_pdf(make_invoice_pdf(tmp_path / 'ride.pdf').read_bytes())}
    for uid, provider in [(b'2', '顺丰'), (b'3', '同城')]:
        mail = EmailMessage()
        mail['Subject'] = '顺丰全电发票出票通知' if provider == '顺丰' else '顺丰同城急送电子发票和存根'
        mail.set_content('附件请查收')
        for file in make_courier_pair(tmp_path / provider, provider):
            mail.add_attachment(file.read_bytes(), maintype='application', subtype='pdf', filename=file.name)
        mail.add_attachment(b'ignored', maintype='application', subtype='xml', filename='invoice.xml')
        messages[uid] = mail.as_bytes()
    destination = tmp_path / 'batch'
    client = FakeImap(messages)
    result = import_pdf_attachments(paths, Mail163Settings('user@163.com'), 'test', days=1,
        courier_kind=None if kind=='全部' else kind, all_invoice_types=kind=='全部', destination=destination, imap_factory=lambda *args, **kwargs: client)
    assert result.downloaded == (5 if kind=='全部' else 1 if kind is None else 4 if kind == '顺丰' else 2)
    assert result.ignored == (0 if kind=='全部' else 1 if kind == '顺丰' else 2)
    assert not list(paths.incoming.iterdir())
    assert client.readonly and set(client.fetch_queries) == {'(BODY.PEEK[])'}
    repeated = import_pdf_attachments(paths, Mail163Settings('user@163.com'), 'test', days=1,
        courier_kind=None if kind=='全部' else kind, all_invoice_types=kind=='全部', destination=destination, imap_factory=lambda *args, **kwargs: FakeImap(messages))
    assert repeated.downloaded == 0 and repeated.duplicates == result.downloaded


def test_import_takeout_links_names_files_with_order_number(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    destination = tmp_path / "batch"
    client = FakeImap({b"201": _takeout_mail()})
    payloads = iter((b"invoice image", b"invoice zip"))

    summary = import_takeout_invoice_links(
        destination,
        paths,
        Mail163Settings("user@163.com"),
        "local-auth-code",
        imap_factory=lambda *args, **kwargs: client,
        downloader=lambda url: next(payloads),
    )

    assert summary.downloaded == 2
    assert sorted(path.name for path in destination.iterdir()) == [
        "1000000000000000001_1.jpg",
        "1000000000000000001_2.zip",
    ]
    assert client.readonly is True
    assert client.fetch_queries == ["(BODY.PEEK[])"]

def test_configure_mail_rejects_unsafe_folder_before_saving_secret(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    called = False

    def save_password(service: str, username: str, password: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(keyring, "set_password", save_password)

    with pytest.raises(MailImportError, match="文件夹"):
        configure_mail(
            tmp_path / "mail163.toml",
            "user@163.com",
            'INBOX\r\nDELETE "*"',
            "local-auth-code",
        )

    assert not called


def test_configure_mail_rejects_backslash_in_address(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    called = False

    def save_password(service: str, username: str, password: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(keyring, "set_password", save_password)

    with pytest.raises(MailImportError, match="完整的 163 邮箱地址"):
        configure_mail(
            tmp_path / "mail163.toml",
            r"user\@163.com",
            "INBOX",
            "local-auth-code",
        )

    assert not called


@pytest.mark.parametrize("auth_code", ["", "short", "has whitespace"])
def test_configure_mail_rejects_invalid_auth_code(
    tmp_path: Path,
    monkeypatch: Any,
    auth_code: str,
) -> None:
    called = False

    def save_password(service: str, username: str, password: str) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(keyring, "set_password", save_password)

    with pytest.raises(MailImportError, match="授权码格式异常"):
        configure_mail(
            tmp_path / "mail163.toml",
            "user@163.com",
            "INBOX",
            auth_code,
        )

    assert not called


def test_read_mail_settings_rejects_invalid_section(tmp_path: Path) -> None:
    config = tmp_path / "mail163.toml"
    config.write_text('mail163 = "invalid"\n', encoding="utf-8")

    with pytest.raises(MailImportError, match="配置不完整"):
        read_mail_settings(config)


def test_connection_failure_logs_out(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path / "workspace")
    client = FailingSelectImap({})

    with pytest.raises(MailImportError, match="只读打开"):
        import_pdf_attachments(
            paths,
            Mail163Settings("user@163.com"),
            "local-auth-code",
            imap_factory=lambda *args, **kwargs: client,
        )

    assert client.logged_out
