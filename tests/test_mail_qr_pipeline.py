"""Mail body QR images enter the existing single-proof pipeline without fee writes."""
from datetime import date, timedelta
from email.message import EmailMessage
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout import mail163, mail_search, workbench_web
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.reliability import read_json, save_json
from invoice_print_layout.storage import append_history, ensure_workspace
from invoice_print_layout.workbench import ExpenseStore
from tests.test_batch_invoice_claim import JIANGXI, seller_column_invoice_bytes, tax_responses
from tests.test_glm_pipeline_http import FakeAI, web
from tests.test_mail163 import FakeImap
from tests.helpers import make_invoice_pdf


def qr_image() -> bytes:
    import cv2
    symbol = cv2.QRCodeEncoder_create().encode(JIANGXI)
    image = cv2.resize(symbol, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(image, 32, 32, 32, 32, cv2.BORDER_CONSTANT, value=255)
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    return encoded.tobytes()


def test_import_mail_images_preserves_provenance_not_ocr_fields(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    paths = ensure_workspace(tmp_path)
    folder = store.root/'邮件收件'; folder.mkdir()
    image = qr_image(); file = folder/'synthetic-qr.PNG'; file.write_bytes(image)
    (folder/'unrelated.txt').write_text('do not receive', encoding='utf-8')
    append_history(paths.mail_history, {'attachment_hash': hashlib.sha256(image).hexdigest(),
                                      'message_uid': '42', 'subject': '合成发票通知',
                                      'order_number': '400000000000000001', 'source_kind': 'mail_link',
                                      'source_url': 'https://storage.example/image.png?secret=synthetic'})
    queue = IntakeQueue(store)
    assert queue.import_mail() == {'received': 1, 'duplicate': 0}
    entry = queue.list()[0]
    assert entry['source'] == 'mail' and entry['fields'] == {}
    assert entry['mail_context']['order_number'] == '400000000000000001'
    assert 'source_url' not in entry['mail_context']
    original = queue.get(entry['id'])
    assert queue.import_mail() == {'received': 0, 'duplicate': 1}
    assert queue.get(entry['id']) == original and file.read_bytes() == image
    assert not store.list_items()


def setup_mail(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> FakeImap:
    message = EmailMessage(); message['Subject'] = '淘宝闪购平台订单发票开具完成通知'
    message.set_content('查看电子发票文件')
    message.add_alternative('<p>订单号 400000000000000001</p><a href="https://bucket.oss.example.aliyuncs.com/manualInvoice/qr.jpg?signature=PRIVATE_SYNTHETIC">查看电子发票文件</a>', subtype='html')
    imap = FakeImap({b'42': message.as_bytes()})
    monkeypatch.setattr(mail163.imaplib, 'IMAP4_SSL', lambda *args, **kwargs: imap)
    monkeypatch.setattr(mail163, 'read_mail_settings', lambda _: mail163.Mail163Settings('test@163.com'))
    monkeypatch.setattr(mail163, 'read_auth_code', lambda _: 'synthetic')
    monkeypatch.setattr(mail163, '_download_takeout_url', lambda _: payload)
    return imap


@pytest.mark.parametrize('enabled', [True, False])
def test_http_mail_qr_to_pdf_is_local_idempotent_and_does_not_create_expenses(
        web: Any, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    root, post, project, original, store = web
    monkeypatch.setattr(FakeAI, 'enabled', enabled)
    monkeypatch.setattr(FakeAI, 'extract', lambda *_args, **_kwargs: pytest.fail('Clear native PDF invoked cloud extraction'), raising=False)
    image = qr_image(); imap = setup_mail(monkeypatch, image)
    calls = tax_responses(monkeypatch, pdf=seller_column_invoice_bytes())
    original_row = IntakeQueue(store).get(original['id'])
    result = post('mail', {'project_id': project, 'material_only': True}, timeout=15)
    assert result['downloaded'] == result['image_downloaded'] == result['link_downloaded'] == 1
    assert result['link_failures'] == [] and 'PRIVATE_SYNTHETIC' not in json.dumps(result)
    assert result['intake']['converted_to_pdf'] == 1 and result['intake']['claim_pending'] == 0
    assert imap.readonly is True and set(imap.fetch_queries) == {'(BODY.PEEK[])'}
    queue = IntakeQueue(store)
    source = next(entry for entry in queue.list() if entry.get('qr_status') == 'converted')
    pdf = queue.get(source['replacement_id'])
    assert Path(source['path']).read_bytes() == image
    assert pdf['source_entry_id'] == source['id'] and pdf['source_entry_ids'] == [source['id']]
    assert pdf['project_id'] == source['project_id'] == project
    assert pdf['fields']['amount'] == '14.00' and pdf['fields']['merchant'] == '测试销售公司'
    assert pdf.get('ai_analysis', {}).get('status') == ('not_needed' if enabled else None)
    assert source['mail_context']['message_uid'] == '42'
    assert len(calls) == 4 and not store.list_items()
    snapshot = queue.list()
    repeated = post('mail', {'project_id': project, 'material_only': True}, timeout=15)
    assert repeated['downloaded'] == 0 and repeated['duplicates'] == 1
    assert repeated['intake']['converted_to_pdf'] == 0 and len(calls) == 4
    assert queue.list() == snapshot and queue.get(original['id']) == original_row
    assert not store.list_items()


def test_failed_link_keeps_retry_window_and_private_url_out_of_http_feedback(
        web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, post, project, _, store = web
    monkeypatch.setattr(FakeAI, 'enabled', False)
    setup_mail(monkeypatch, qr_image())
    earlier = (date.today()-timedelta(days=2)).isoformat()
    cursor = store.root/'mail-sync.json'; save_json(cursor, {'date': earlier})
    monkeypatch.setattr(mail163, '_download_takeout_url', lambda _: (_ for _ in ()).throw(OSError('raw secret URL PRIVATE_SYNTHETIC')))
    result = post('mail', {'project_id': project, 'material_only': True})
    assert result['downloaded'] == 0 and len(result['link_failures']) == 1
    assert 'PRIVATE_SYNTHETIC' not in json.dumps(result) and 'source_url' not in json.dumps(result)
    assert read_json(cursor) == {'date': earlier}
    assert read_json(store.root/'mail-link-failures.json')['failures']
    assert not store.list_items()


def test_first_failed_collection_remembers_initial_window_for_next_day(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    monkeypatch.setattr(mail163, 'read_mail_settings', lambda _: mail163.Mail163Settings('test@163.com'))
    monkeypatch.setattr(mail163, 'read_auth_code', lambda _: 'synthetic')
    days: list[int] = []
    def importing(*args: Any, **kwargs: Any) -> mail163.MailImportSummary:
        days.append(kwargs['days'])
        assert kwargs['include_link_materials'] is True
        return mail163.MailImportSummary(link_failures=[{'reason': 'expired', 'source_url': 'synthetic'}])
    monkeypatch.setattr(mail163, 'import_pdf_attachments', importing)
    today = date.today()
    class Tomorrow(date):
        @classmethod
        def today(cls) -> date:
            return today + timedelta(days=1)
    workbench_web.sync_mail(store, organize=False)
    assert read_json(store.root/'mail-sync.json') == {'date': today.isoformat(), 'incomplete': True}
    monkeypatch.setattr(workbench_web, 'date', Tomorrow)
    workbench_web.sync_mail(store, organize=False)
    assert days == [1, 2]


def test_legacy_pdf_collection_cannot_advance_unresolved_link_retry_window(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    monkeypatch.setattr(mail163, 'read_mail_settings', lambda _: mail163.Mail163Settings('test@163.com'))
    monkeypatch.setattr(mail163, 'read_auth_code', lambda _: 'synthetic')
    earlier = (date.today()-timedelta(days=2)).isoformat()
    cursor = store.root/'mail-sync.json'; save_json(cursor, {'date': earlier})
    monkeypatch.setattr(mail163, 'import_pdf_attachments', lambda *args, **kwargs: mail163.MailImportSummary(link_failures=[{'reason': 'expired'}]))
    workbench_web.sync_mail(store, organize=False)
    assert read_json(cursor) == {'date': earlier}
    monkeypatch.setattr(mail163, 'import_pdf_attachments', lambda *args, **kwargs: mail163.MailImportSummary(downloaded=1))
    monkeypatch.setattr(workbench_web, 'organize_rides', lambda *_: {'created': 0, 'supplemented': 0, 'duplicates': 0})
    workbench_web.sync_mail(store, organize=True)
    assert read_json(cursor) == {'date': earlier}, 'legacy collection must not skip unresolved earlier body links'
    workbench_web.sync_mail(store, organize=False)
    assert read_json(cursor)['date'] == date.today().isoformat()
    assert read_json(store.root/'mail-link-failures.json')['failures'] == []


def test_invalid_saved_image_does_not_block_later_valid_mail_material(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    folder = store.root/'邮件收件'; folder.mkdir()
    broken = folder/'a-broken.png'; broken.write_bytes(b'not a valid image')
    valid = make_invoice_pdf(folder/'z-good.pdf', amount='18.00')
    queue = IntakeQueue(store)
    result = queue.import_mail()
    assert result['received'] == 1 and result['failed'] == 1
    assert result['failures'][0]['name'] == broken.name
    assert len(queue.list()) == 1 and queue.list()[0]['name'] == valid.name
    assert broken.read_bytes() == b'not a valid image'
    assert not store.list_items()


def test_manual_qr_claim_reason_visible_without_exposing_saved_signed_image_url(
        web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.request import urlopen
    root, post, project, _, store = web
    monkeypatch.setattr(FakeAI, 'enabled', False)
    setup_mail(monkeypatch, qr_image())
    tax_responses(monkeypatch, first=b'{"code":"expired"}')
    result = post('mail', {'project_id': project, 'material_only': True}, timeout=15)
    assert result['intake']['claim_pending'] == 1 and result['intake']['converted_to_pdf'] == 0
    with urlopen(root+'/api/material-matching?project='+project) as response:
        board = json.load(response)
    qr = next(card for card in board['other'] if card['kind'] == 'qr_claim')
    assert '打开领票入口' in qr['claim_message'] and qr['amount_cents'] is None
    assert 'PRIVATE_SYNTHETIC' not in json.dumps(board) and not store.list_items()
