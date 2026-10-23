from __future__ import annotations

import io
import zipfile
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from invoice_print_layout import mail_agent_source as source
from invoice_print_layout.mail163 import Mail163Settings, MailImportError
from tests.helpers import make_invoice_pdf
from tests.test_mail163 import FakeImap


class SourceImap(FakeImap):
    def __init__(self, messages: dict[bytes, bytes], *, epoch: str = '20261021',
                 returned_uids: bytes | None = None) -> None:
        super().__init__(messages)
        self.epoch = epoch
        self.returned_uids = returned_uids
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.declared_sizes: dict[bytes, int] = {}
        self.failed_uid: bytes = b''

    def response(self, name: str) -> tuple[str, list[bytes]]:
        assert name == 'UIDVALIDITY'
        return name, [self.epoch.encode('ascii')]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append((command, args))
        if command == 'SEARCH':
            return 'OK', [self.returned_uids if self.returned_uids is not None else b' '.join(self.messages)]
        assert command == 'FETCH'
        uid, query = args
        if uid == self.failed_uid:
            return 'NO', [b'private server diagnostic must not leak']
        self.fetch_queries.append(query)
        if query == '(RFC822.SIZE)':
            length = self.declared_sizes.get(uid, len(self.messages[uid]))
            return 'OK', [uid + b' (RFC822.SIZE ' + str(length).encode('ascii') + b')']
        assert query == '(BODY.PEEK[])'
        return 'OK', [(uid + b' (BODY[] {' + str(len(self.messages[uid])).encode('ascii') + b'}', self.messages[uid]), b')']


def email(*, subject: str = '电子发票', body: str = '请查收附件', html: str = '',
          attachments: list[tuple[str, bytes]] | None = None) -> bytes:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'synthetic@example.test'
    msg['Message-ID'] = '<synthetic-message@example.test>'
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype='html')
    for name, data in attachments or []:
        msg.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    return msg.as_bytes()


def image_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(stream, format='PNG')
    return stream.getvalue()


def fetch(client: SourceImap, **kwargs: Any) -> dict[str, Any]:
    return source.fetch_messages(Mail163Settings('synthetic@163.com'), 'synthetic-private-credential',
                                 since=date(2026, 10, 21), imap_factory=lambda *args, **kw: client, **kwargs)


def test_original_attachment_and_ordinary_mail_are_read_only_and_have_provenance(tmp_path: Path) -> None:
    pdf = make_invoice_pdf(tmp_path / 'synthetic.pdf').read_bytes()
    image = image_bytes()
    client = SourceImap({b'13': email(subject='普通通知', body='普通信件也应推进UID'),
                         b'12': email(attachments=[('电子发票.pdf', pdf), ('购买凭证.png', image)])})
    result = fetch(client)
    assert result['uidvalidity'] == '20261021'
    assert result['more'] is False
    assert [msg['uid'] for msg in result['messages']] == [12, 13]
    invoice, ordinary = result['messages']
    assert invoice['subject'] == '电子发票' and invoice['sender'] == 'synthetic@example.test'
    assert invoice['message_id'] == '<synthetic-message@example.test>'
    assert [file['payload'] for file in invoice['files']] == [pdf, image]
    assert {file['source'] for file in invoice['files']} == {'mail_attachment'}
    assert not invoice['issues']
    assert ordinary['files'] == [] and ordinary['issues'] == []
    assert '普通信件也应推进UID' in ordinary['text']
    assert client.readonly and client.id_sent and client.logged_out
    assert all(command in {'SEARCH', 'FETCH'} for command, _ in client.calls)
    assert client.fetch_queries == ['(RFC822.SIZE)', '(BODY.PEEK[])'] * 2


def test_empty_incremental_range_does_not_refetch_highest_old_uid() -> None:
    client = SourceImap({b'9': email()}, returned_uids=b'9')
    result = fetch(client, after_uid=9, uidvalidity=client.epoch)
    assert result['messages'] == [] and result['more'] is False
    assert client.calls == [('SEARCH', (None, 'SINCE', '21-Oct-2026', 'UID', '10:*'))]
    assert client.logged_out


def test_new_epoch_restarts_with_approved_date_not_old_account_uid() -> None:
    client = SourceImap({b'2': email(subject='新epoch'), b'1': email(subject='新epoch')}, epoch='91')
    result = fetch(client, after_uid=9000, uidvalidity='90')
    assert result['uidvalidity'] == '91'
    assert [msg['uid'] for msg in result['messages']] == [1, 2]
    assert client.calls[0] == ('SEARCH', (None, 'SINCE', '21-Oct-2026', 'UID', '1:*'))


def test_limit_applies_to_actual_body_fetches_and_numeric_sorting() -> None:
    client = SourceImap({b'100': email(), b'11': email(), b'2': email(), b'1': email()})
    result = fetch(client, limit=2)
    assert [msg['uid'] for msg in result['messages']] == [1, 2]
    assert result['more'] is True
    assert [args[0] for command, args in client.calls if command == 'FETCH'] == [b'1', b'1', b'2', b'2']


def test_fetch_failure_aborts_batch_and_diagnostic_is_sanitized() -> None:
    client = SourceImap({b'1': email(), b'2': email()})
    client.failed_uid = b'2'
    with pytest.raises(MailImportError) as raised:
        fetch(client)
    assert 'synthetic-private-credential' not in str(raised.value)
    assert 'private server' not in str(raised.value)
    assert raised.value.__suppress_context__ is True
    assert client.logged_out


def test_connect_failure_is_sanitized() -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OSError('private password and signed URL must never leak')
    with pytest.raises(MailImportError) as raised:
        source.fetch_messages(Mail163Settings('synthetic@163.com'), 'private', since=date(2026, 10, 21),
                              imap_factory=broken)
    assert 'signed URL' not in str(raised.value) and 'password' not in str(raised.value)


@pytest.mark.parametrize('epoch', ['', '0', 'garbage', '4294967296'])
def test_missing_or_bad_uidvalidity_never_fetches_mail(epoch: str) -> None:
    client = SourceImap({b'1': email()}, epoch=epoch)
    with pytest.raises(MailImportError):
        fetch(client)
    assert client.calls == [] and client.logged_out


@pytest.mark.parametrize('uids', [b'1 bad 2', b'0', b'-1', b'4294967296'])
def test_invalid_server_uid_never_becomes_cursor(uids: bytes) -> None:
    client = SourceImap({b'1': email()}, returned_uids=uids)
    with pytest.raises(MailImportError):
        fetch(client)
    assert len(client.calls) == 1 and client.logged_out


def test_oversized_message_does_not_fetch_body() -> None:
    client = SourceImap({b'1': email()})
    client.declared_sizes[b'1'] = source.MAX_MESSAGE_BYTES + 1
    result = fetch(client)
    assert result['messages'][0]['files'] == []
    assert '30MB' in result['messages'][0]['issues'][0]
    assert client.fetch_queries == ['(RFC822.SIZE)']


def test_size_response_missing_aborts_batch() -> None:
    class MissingSize(SourceImap):
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == 'FETCH' and args[1] == '(RFC822.SIZE)':
                return 'OK', [b'not a size']
            return super().uid(command, *args)
    client = MissingSize({b'1': email()})
    with pytest.raises(MailImportError):
        fetch(client)
    assert '(BODY.PEEK[])' not in client.fetch_queries


def test_malicious_links_not_downloaded_and_safe_link_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_invoice_pdf(tmp_path / 'synthetic.pdf').read_bytes()
    safe = 'https://bucket.oss-cn-shanghai.aliyuncs.com/invoice.pdf?Signature=PRIVATE_SYNTHETIC'
    calls: list[str] = []
    def downloader(url: str) -> bytes:
        calls.append(url)
        return pdf
    monkeypatch.setattr(source, 'download', downloader)
    html = ('<a href="http://127.0.0.1/invoice.pdf">发票</a>'
            '<a href="https://fapiao.com.evil.test/invoice.pdf">发票</a>'
            '<a href="https://fapiao.com@evil.test/download">发票</a>'
            f'<a href="{safe}">发票</a>')
    result = fetch(SourceImap({b'1': email(html=html)}))
    assert calls == [safe]
    assert result['messages'][0]['files'] == [{'name': '邮件发票.pdf', 'payload': pdf, 'source': 'mail_link'}]
    assert 'PRIVATE_SYNTHETIC' not in str(result['messages'][0]['issues'])


def test_download_failure_requires_action_and_hides_signed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = 'https://bucket.oss-cn-shanghai.aliyuncs.com/invoice.pdf?Signature=PRIVATE_SYNTHETIC'
    def failed(link: str) -> bytes:
        raise ValueError('broken download ' + link)
    monkeypatch.setattr(source, 'download', failed)
    result = fetch(SourceImap({b'1': email(body='查看电子发票 ' + url)}))
    message = result['messages'][0]
    assert not message['files'] and message['issues']
    assert 'PRIVATE_SYNTHETIC' not in str(message['issues'])
    assert 'PRIVATE_SYNTHETIC' not in message['text']
    assert '[邮件链接]' in message['text']


def test_unsupported_invoice_link_requires_action_but_is_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, 'download', lambda _: pytest.fail('unsupported URL requested'))
    result = fetch(SourceImap({b'1': email(html='<a href="https://login.example.test/invoice">查看发票</a>')}))
    assert result['messages'][0]['files'] == [] and result['messages'][0]['issues']


def test_attachment_file_count_limit_and_no_partial_zip(tmp_path: Path) -> None:
    pdf = make_invoice_pdf(tmp_path / 'synthetic.pdf').read_bytes()
    too_many = email(attachments=[(f'电子发票{idx}.pdf', pdf) for idx in range(source.MAX_FILES + 1)])
    result = fetch(SourceImap({b'1': too_many}))
    assert result['messages'][0]['files'] == [] and result['messages'][0]['issues']
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('../good.pdf', pdf)
        archive.writestr('../broken.png', b'not an image')
    result = fetch(SourceImap({b'1': email(attachments=[('电子发票.zip', stream.getvalue())])}))
    assert result['messages'][0]['files'] == [] and result['messages'][0]['issues']


def test_excessive_links_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    def failed(url: str) -> bytes:
        calls.append(url)
        return b'invalid'
    monkeypatch.setattr(source, 'download', failed)
    html = ''.join(f'<a href="https://bucket.oss.example.aliyuncs.com/invoice{idx}.pdf">发票</a>'
                   for idx in range(source.MAX_LINKS + 3))
    result = fetch(SourceImap({b'1': email(html=html)}))
    assert len(calls) == source.MAX_LINKS
    assert result['messages'][0]['issues']


def test_body_fetch_failure_does_not_return_prior_success() -> None:
    class BodyFailure(SourceImap):
        def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
            if command == 'FETCH' and args == (b'2', '(BODY.PEEK[])'):
                return 'NO', [b'private diagnostic']
            return super().uid(command, *args)
    client = BodyFailure({b'1': email(), b'2': email()})
    with pytest.raises(MailImportError):
        fetch(client)
    assert client.logged_out


def test_zip_file_count_and_aggregate_material_bytes_are_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_invoice_pdf(tmp_path / 'synthetic.pdf').read_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        # Each member remains a readable PDF while its digest differs.
        for idx in range(source.MAX_FILES + 1):
            archive.writestr(f'{idx}.pdf', pdf + f'\n% member {idx}'.encode())
    result = fetch(SourceImap({b'1': email(attachments=[('发票.zip', stream.getvalue())])}))
    assert result['messages'][0]['files'] == [] and result['messages'][0]['issues']
    monkeypatch.setattr(source, 'MAX_MATERIAL_BYTES', len(pdf) + 1)
    result = fetch(SourceImap({b'1': email(attachments=[('发票.pdf', pdf), ('发票2.pdf', pdf + b'\n% other')])}))
    assert len(result['messages'][0]['files']) == 1 and result['messages'][0]['issues']


def test_mime_parts_over_limit_never_partially_collects() -> None:
    msg = EmailMessage()
    msg['Subject'] = '电子发票'
    msg.make_mixed()
    for _ in range(source.MAX_MIME_PARTS):
        part = EmailMessage()
        part.set_content('synthetic')
        msg.attach(part)
    result = fetch(SourceImap({b'1': msg.as_bytes()}))
    assert result['messages'][0]['files'] == [] and result['messages'][0]['issues']


def test_maximum_uid_has_no_invalid_next_range() -> None:
    client = SourceImap({})
    result = fetch(client, after_uid=source.MAX_UID, uidvalidity=client.epoch)
    assert result['messages'] == [] and not result['more']
    assert client.calls == [] and client.logged_out


def test_batch_byte_limit_returns_only_acknowledgeable_uids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_invoice_pdf(tmp_path / 'synthetic.pdf').read_bytes()
    monkeypatch.setattr(source, 'MAX_BATCH_BYTES', len(pdf) + 1)
    client = SourceImap({b'1': email(attachments=[('发票1.pdf', pdf)]),
                         b'2': email(attachments=[('发票2.pdf', pdf)]), b'3': email()})
    response = fetch(client)
    assert [m['uid'] for m in response['messages']] == [1]
    assert response['more'] is True
    again = fetch(client, after_uid=1, uidvalidity=client.epoch)
    assert [m['uid'] for m in again['messages']] == [2, 3]


@pytest.mark.parametrize('changes', [{'limit': 0}, {'limit': 51}, {'limit': True}, {'after_uid': -1}, {'uidvalidity': 'invalid'}])
def test_invalid_bounds_are_rejected_before_connect(changes: dict[str, Any]) -> None:
    client = SourceImap({})
    with pytest.raises(MailImportError):
        fetch(client, **changes)
    assert client.login_args is None
