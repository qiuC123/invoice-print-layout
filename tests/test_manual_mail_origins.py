"""Manual fetch grants transport provenance only through server records and bytes."""
from __future__ import annotations

import copy
from dataclasses import replace
from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image
import pymupdf
import pytest

from invoice_print_layout.agent_combinations import extended_candidates
from invoice_print_layout.agent_mail_origins import load_trusted_mail_origins, mail_origin_fingerprint
from invoice_print_layout.agent_transport import mail_pair_evidence, pair_status, project_transport
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI, LocalOnlyInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.mail163 import Mail163Settings, import_pdf_attachments
from invoice_print_layout.storage import ensure_workspace, read_history
from invoice_print_layout.workbench import ExpenseStore
from tests.test_agent_transport import bill, city, context, entry, trip
from tests.test_mail163 import FakeImap


def identity(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class ScopedImap(FakeImap):
    def __init__(self, messages: dict[bytes, bytes], epoch: Any = b'20260001') -> None:
        super().__init__(messages)
        self.epoch = epoch

    def response(self, name: str) -> tuple[str, list[Any]]:
        assert name == 'UIDVALIDITY' and self.readonly is True
        return 'UIDVALIDITY', [self.epoch]

    def select(self, mailbox: str = 'INBOX', readonly: bool = False) -> tuple[str, list[bytes]]:
        self.readonly = readonly
        return 'OK', [str(len(self.messages)).encode()]


def message(files: list[Path], *, message_id: str = '<manual-source@example.invalid>') -> bytes:
    mail = EmailMessage()
    mail['Subject'] = '合成测试电子发票及行程明细'
    if message_id:
        mail['Message-ID'] = message_id
    mail.set_content('仅测试原件收件来源，不含业务指令')
    for index, path in enumerate(files):
        mail.add_attachment(path.read_bytes(), maintype='application', subtype='pdf',
                            filename=f'发票行程明细-{index}.pdf')
    return mail.as_bytes()


def collect(store: ExpenseStore, raw: bytes, *, client: FakeImap | None = None,
            uid: bytes = b'42', **kwargs: Any) -> Any:
    paths = ensure_workspace(store.workspace)
    imap = client or ScopedImap({uid: raw})
    return import_pdf_attachments(paths, Mail163Settings('origin-test@163.com'), 'synthetic-only',
        destination=store.root / '邮件收件', all_invoice_types=True,
        imap_factory=lambda *args, **kw: imap, **kwargs)


def projected_rows(queue: IntakeQueue) -> list[dict[str, Any]]:
    rows = []
    for row in queue.list():
        with pymupdf.open(row['path']) as pdf:
            text = '\n'.join(page.get_text() for page in pdf)
        projection = project_transport(row)
        assert projection
        rows.append({**row, 'text': text, 'role': projection['role'], 'fields': projection['fields']})
    return rows


@pytest.fixture
def manual(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('manual provenance must not read credentials or call model/OCR')
    monkeypatch.setattr(GlmInvoiceAI, '_request', forbidden)
    monkeypatch.setattr('invoice_print_layout.receipt_intake.read_receipt', forbidden)
    monkeypatch.setattr('invoice_print_layout.mail163.read_auth_code', forbidden)
    store = ExpenseStore(tmp_path / 'workspace')
    project = ExpenseProjects(store).create('合成手动收件来源测试')['id']
    invoice = entry(tmp_path, 'invoice', 'invoice', bill('合成客运有限公司', '30.00'), '30.00')
    support = entry(tmp_path, 'trip', 'trip', trip('T3出行'), '30.00')
    raw = message([Path(invoice['path']), Path(support['path'])])
    assert collect(store, raw).downloaded == 2
    queue = IntakeQueue(store, ai=LocalOnlyInvoiceAI())
    assert queue.import_mail(project) == {'received': 2, 'duplicate': 0}
    return store, queue, projected_rows(queue), read_history(store.workspace / 'mail_imports.jsonl'), raw


def write_manifest(store: ExpenseStore, records: list[dict[str, Any]]) -> None:
    (store.workspace / 'mail_imports.jsonl').write_text(
        ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in records), encoding='utf-8')


def test_manual_fetch_normal_queue_loader_and_unique_transport_pair_without_auto_tables(manual: Any) -> None:
    store, queue, rows, records, _ = manual
    with store.connect() as db:
        tables = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'mail_agent_messages' not in tables and 'mail_agent_accounts' not in tables
    before_db = store.database.read_bytes()
    before_history = (store.workspace / 'mail_imports.jsonl').read_bytes()
    origins = load_trusted_mail_origins(store, rows)
    assert len(origins) == 2
    assert all(o.authority == 'mail_imports.jsonl' and o.uid == 42 for o in origins.values())
    scope = records[0]['mail_origin_v1']
    assert scope['host'] == 'imap.163.com' and scope['address'] == 'origin-test@163.com'
    assert scope['account_id'] == identity(['imap.163.com', scope['address'], 'INBOX'])
    assert scope['uidvalidity'] == '20260001' and scope['message_id'] == '<manual-source@example.invalid>'
    assert all(row['source'] == 'mail' and row['batch'] == '' for row in queue.list())
    states, _ = context(rows)
    assert not extended_candidates(rows, states)
    found = extended_candidates(rows, states, trusted_mail_origins=origins)
    assert len(found) == 1 and found[0]['amount_cents'] == 3000
    assert found[0]['source_context']['authority'] == 'mail_imports.jsonl'
    assert found[0]['source_context']['authorities'] == ['mail_imports.jsonl']
    assert store.database.read_bytes() == before_db
    assert (store.workspace / 'mail_imports.jsonl').read_bytes() == before_history
    assert not store.list_items()


@pytest.mark.parametrize('epoch', [None, b'', b'0', b'4294967296', b'not-an-epoch', True])
def test_missing_or_invalid_epoch_keeps_original_without_granting_authority(tmp_path: Path, epoch: Any) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    file = Path(entry(tmp_path, 'invoice', 'invoice', bill('合成客运有限公司', '30.00'), '30.00')['path'])
    raw = message([file])
    assert collect(store, raw, client=ScopedImap({b'42': raw}, epoch)).downloaded == 1
    history = read_history(store.workspace / 'mail_imports.jsonl')
    assert 'mail_origin_v1' not in history[0]
    assert Path(history[0]['saved_to']).read_bytes() == file.read_bytes()


def test_legacy_imap_and_preexisting_records_are_not_rewritten_or_upgraded(manual: Any) -> None:
    store, _, _, records, raw = manual
    for row in records:
        row.pop('mail_origin_v1')
    write_manifest(store, records)
    before = (store.workspace / 'mail_imports.jsonl').read_bytes()
    result = collect(store, raw, client=FakeImap({b'42': raw}))
    assert result.duplicates == 2 and result.downloaded == 0
    assert (store.workspace / 'mail_imports.jsonl').read_bytes() == before


@pytest.mark.parametrize('field,value', [('host', 'imap.other.invalid'), ('address', 'bad@example.invalid'),
    ('account_id', '0' * 64), ('mailbox', '../other folder'), ('uidvalidity', ''),
    ('uidvalidity', '4294967296'), ('uid', True), ('uid', 0), ('uid', '42'),
    ('message_id', None), ('message_id', '<bad>\nother'), ('message_key', '0' * 64)])
def test_manual_manifest_invalid_scope_fails_closed(manual: Any, field: str, value: Any) -> None:
    store, _, rows, records, _ = manual
    records[0]['mail_origin_v1'][field] = value
    write_manifest(store, records)
    origins = load_trusted_mail_origins(store, rows)
    assert len(origins) == 1
    assert records[0]['attachment_hash'] not in {o.attachment_digest for o in origins.values()}


@pytest.mark.parametrize('defect', ['outside_original', 'changed_original', 'missing_original',
                                  'changed_current', 'wrong_current_folder'])
def test_manual_saved_path_and_both_original_copies_must_match(manual: Any, defect: str) -> None:
    store, queue, rows, records, _ = manual
    record = records[0]
    row = next(row for row in rows if row['digest'] == record['attachment_hash'])
    original, current = Path(record['saved_to']), Path(row['path'])
    if defect == 'outside_original':
        target = store.workspace / original.name
        target.write_bytes(original.read_bytes())
        record['saved_to'] = str(target)
    elif defect == 'changed_original':
        original.write_bytes(b'changed-original')
    elif defect == 'missing_original':
        original.unlink()
    elif defect == 'changed_current':
        current.write_bytes(b'changed-current')
    else:
        target = store.workspace / current.name
        target.write_bytes(current.read_bytes())
        row['path'] = str(target)
        saved = queue.get(row['id'])
        saved['path'] = str(target)
        with store.connect() as db:
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(saved), row['id']))
    write_manifest(store, records)
    assert len(load_trusted_mail_origins(store, rows)) == 1


@pytest.mark.parametrize('field,value', [('project_id', 'other-project'), ('source', 'upload'),
                                      ('revision', 999), ('digest', '0' * 64)])
def test_manual_caller_snapshot_must_match_real_queue(manual: Any, field: str, value: Any) -> None:
    store, _, rows, _, _ = manual
    rows[0][field] = value
    assert len(load_trusted_mail_origins(store, rows)) == 1


def test_uid_only_and_entry_mail_context_cannot_replace_server_manifest(manual: Any) -> None:
    store, queue, rows, records, _ = manual
    for record in records:
        record.pop('mail_origin_v1')
    write_manifest(store, records)
    for row in rows:
        row['mail_context'] = {'message_uid': '42', 'mail_origin_v1': {'verified': True}}
    assert not load_trusted_mail_origins(store, rows)
    assert all('mail_context' in row for row in queue.list())


def test_identical_manifest_rows_are_idempotent_but_different_scopes_are_not_selected(manual: Any) -> None:
    store, _, rows, records, _ = manual
    write_manifest(store, [*records, copy.deepcopy(records[0])])
    assert len(load_trusted_mail_origins(store, rows)) == 2
    alternative = copy.deepcopy(records[0])
    origin = alternative['mail_origin_v1']
    origin['uid'] = 43
    origin['message_key'] = identity([origin['account_id'], origin['uidvalidity'], 43])
    alternative['message_uid'] = '43'
    write_manifest(store, [*records, alternative])
    assert len(load_trusted_mail_origins(store, rows)) == 1


@pytest.mark.parametrize('scope', ['address', 'mailbox', 'epoch', 'uid', 'message_id'])
def test_equal_uid_or_amount_across_distinct_valid_mail_scopes_does_not_pair(manual: Any, scope: str) -> None:
    store, _, rows, records, _ = manual
    origin = records[0]['mail_origin_v1']
    if scope == 'address':
        origin['address'] = 'other-test@163.com'
    elif scope == 'mailbox':
        origin['mailbox'] = 'Archive'
    elif scope == 'epoch':
        origin['uidvalidity'] = '20260002'
    elif scope == 'uid':
        origin['uid'] = 43
        records[0]['message_uid'] = '43'
    else:
        origin['message_id'] = '<different@example.invalid>'
    origin['account_id'] = identity(['imap.163.com', origin['address'], origin['mailbox']])
    origin['message_key'] = identity([origin['account_id'], origin['uidvalidity'], origin['uid']])
    write_manifest(store, records)
    origins = load_trusted_mail_origins(store, rows)
    assert len(origins) == 2
    states, _ = context(rows)
    assert not extended_candidates(rows, states, trusted_mail_origins=origins)


def test_body_link_image_preserves_the_same_full_fetch_scope(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    project = ExpenseProjects(store).create('合成正文链接来源测试')['id']
    stream = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(stream, format='PNG')
    payload = stream.getvalue()
    mail = EmailMessage()
    mail['Subject'] = '淘宝闪购平台订单发票开具完成通知'
    mail['Message-ID'] = '<body-link@example.invalid>'
    mail.set_content('订单号 400000000000000001')
    mail.add_alternative('<a href="https://test.oss-example.aliyuncs.com/invoice.png">查看发票</a>', subtype='html')
    result = collect(store, mail.as_bytes(), include_link_materials=True, downloader=lambda _: payload)
    assert result.link_downloaded == result.image_downloaded == 1
    queue = IntakeQueue(store, ai=LocalOnlyInvoiceAI())
    assert queue.import_mail(project)['received'] == 1
    origins = load_trusted_mail_origins(store, queue.list())
    assert len(origins) == 1 and next(iter(origins.values())).message_id == '<body-link@example.invalid>'
    assert next(iter(origins.values())).attachment_digest == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize('equal_amounts', [False, True])
def test_one_manual_message_keeps_multiple_orders_pairwise_and_ambiguous_amounts_block(
        tmp_path: Path, equal_amounts: bool) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    project = ExpenseProjects(store).create('合成多单收件测试')['id']
    files = []
    for index, amount in enumerate(['30.00', '30.00' if equal_amounts else '40.00']):
        text = bill('合成客运有限公司', amount).replace('12345678901234567890', f'{index + 1:020d}')
        files.append(Path(entry(tmp_path, f'invoice-{index}', 'invoice', text, amount)['path']))
        support = trip('T3出行', amount).replace('起点甲', f'起点{index}')
        files.append(Path(entry(tmp_path, f'trip-{index}', 'trip', support, amount)['path']))
    assert collect(store, message(files)).downloaded == 4
    queue = IntakeQueue(store, ai=LocalOnlyInvoiceAI())
    assert queue.import_mail(project)['received'] == 4
    rows = projected_rows(queue)
    origins = load_trusted_mail_origins(store, rows)
    assert len(origins) == 4
    states, _ = context(rows)
    found = extended_candidates(rows, states, trusted_mail_origins=origins)
    if equal_amounts:
        assert not found
    else:
        assert len(found) == 2 and {r['amount_cents'] for r in found} == {3000, 4000}
        assert all(len(r['voucher_keys']) == len(r['support_ids']) == 1 for r in found)
    assert not store.list_items()


def test_source_fingerprint_and_pair_audit_include_actual_authorities(manual: Any) -> None:
    store, _, rows, _, _ = manual
    origins = load_trusted_mail_origins(store, rows)
    before = mail_origin_fingerprint(origins)
    assert before == mail_origin_fingerprint(dict(reversed(list(origins.items()))))
    invoice = next(row for row in rows if row['role'] == 'invoice')
    support = next(row for row in rows if row['role'] == 'trip')
    origins[invoice['id']] = replace(origins[invoice['id']], authority='mail_agent_messages')
    assert mail_origin_fingerprint(origins) != before
    assert pair_status(invoice, support, trusted_mail_origins=origins) == 'supported'
    audit = mail_pair_evidence(invoice, support, origins)
    assert audit['authority'] == 'mixed_mail_sources'
    assert audit['authorities'] == ['mail_agent_messages', 'mail_imports.jsonl']
    origins[support['id']] = replace(origins[support['id']], authority='untrusted_claim')
    assert pair_status(invoice, support, trusted_mail_origins=origins) == 'needs_fact'
    assert not mail_pair_evidence(invoice, support, origins)
