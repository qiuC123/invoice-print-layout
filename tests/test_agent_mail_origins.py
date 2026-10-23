"""Authority mail rows, scoped identities and both copies of original bytes bind origins."""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_mail_origins import load_trusted_mail_origins, mail_origin_fingerprint
from invoice_print_layout.agent_transport import MailOrigin
from invoice_print_layout.mail_agent import _hash
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.intake_queue import IntakeQueue
from tests.test_mail_agent import env as producer_env, enable, message, run


@pytest.fixture
def env(tmp_path: Path) -> tuple[ExpenseStore, list[dict[str, Any]], dict[str, Any]]:
    store = ExpenseStore(tmp_path)
    address, folder, epoch, uid = 'origin-test@163.com', 'INBOX', '20260001', 42
    account = _hash(['imap.163.com', address, folder])
    key = _hash([account, epoch, uid])
    original_folder = store.root / '邮件Agent原件' / key
    original_folder.mkdir(parents=True)
    intake_folder = store.root / '统一收件'
    intake_folder.mkdir()
    entries, files = [], []
    for index in range(2):
        body = b'%PDF-synthetic-provenance-only-' + str(index).encode()
        digest = hashlib.sha256(body).hexdigest()
        original = original_folder / (digest + '.pdf')
        original.write_bytes(body)
        current = intake_folder / (digest + '.pdf')
        current.write_bytes(body)
        entries.append({'id': f'entry-{index}', 'revision': 1, 'digest': digest,
                        'source': 'mail_agent', 'batch': key, 'project_id': 'test-project',
                        'path': str(current), 'role': 'invoice' if index == 0 else 'trip'})
        files.append({'name': f'任意文件名-{index}.pdf', 'digest': digest, 'path': str(original)})
    message = {'id': key, 'account_id': account, 'uidvalidity': epoch, 'uid': uid,
               'message_id': '<test-origin@example.invalid>', 'project_id': 'test-project',
               'source_issues': [], 'material_ids': [e['id'] for e in entries], 'files': files}
    with store.connect() as db:
        db.executescript('''CREATE TABLE mail_agent_accounts
            (id TEXT PRIMARY KEY,address TEXT NOT NULL,folder TEXT NOT NULL,
             uidvalidity TEXT NOT NULL,last_uid INTEGER NOT NULL,data TEXT NOT NULL);
            CREATE TABLE mail_agent_messages
            (id TEXT PRIMARY KEY,account_id TEXT NOT NULL,uidvalidity TEXT NOT NULL,
             uid INTEGER NOT NULL,data TEXT NOT NULL,UNIQUE(account_id,uidvalidity,uid));
            CREATE TABLE intake_queue(id TEXT PRIMARY KEY,data TEXT NOT NULL);''')
        db.execute('INSERT INTO mail_agent_accounts VALUES (?,?,?,?,?,?)',
                   (account, address, folder, epoch, uid, json.dumps({'since': '2030-01-01'})))
        db.execute('INSERT INTO mail_agent_messages VALUES (?,?,?,?,?)',
                   (key, account, epoch, uid, json.dumps(message, ensure_ascii=False)))
        for entry in entries:
            db.execute('INSERT INTO intake_queue VALUES (?,?)', (entry['id'], json.dumps(entry)))
    return store, entries, message


def save_message(store: ExpenseStore, message: dict[str, Any]) -> None:
    with store.connect() as db:
        db.execute('UPDATE mail_agent_messages SET data=?', (json.dumps(message),))


def test_authoritative_scope_and_two_byte_copies_load_frozen_origins_without_writes(env: Any) -> None:
    store, entries, message = env
    before = hashlib.sha256(store.database.read_bytes()).hexdigest()
    origins = load_trusted_mail_origins(store, entries)
    assert set(origins) == {e['id'] for e in entries}
    assert all(isinstance(o, MailOrigin) for o in origins.values())
    a, b = origins.values()
    assert a.account_id == b.account_id == message['account_id'] and a.mailbox == 'INBOX'
    assert a.uidvalidity == b.uidvalidity == message['uidvalidity'] and a.uid == b.uid == 42
    assert a.attachment_digest == entries[0]['digest'] and b.attachment_digest == entries[1]['digest']
    with pytest.raises(FrozenInstanceError):
        a.uid = 43
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == before


def test_real_mail_producer_rows_and_normal_intake_membership_are_accepted(producer_env: Any) -> None:
    service, store, _, project, _, _, incoming, calls = producer_env
    incoming.append(message(7))
    enable(service, use_model=False)
    run(service)  # Synthetic fetcher + kick stub: no mailbox/model/OCR execution.
    saved = service._messages()[0]
    assert saved['project_id'] == project and calls
    entries = [IntakeQueue(store).get(key) for key in saved['material_ids']]
    origins = load_trusted_mail_origins(store, entries)
    assert len(origins) == len(entries) == 1
    assert origins[entries[0]['id']].account_id == saved['account_id']
    assert origins[entries[0]['id']].uid == saved['uid'] == 7
    assert origins[entries[0]['id']].attachment_digest == saved['files'][0]['digest']


def test_fingerprint_stable_for_order_and_processing_status_but_changes_with_scope(env: Any) -> None:
    store, entries, message = env
    origins = load_trusted_mail_origins(store, entries)
    first = mail_origin_fingerprint(origins)
    assert first == mail_origin_fingerprint(dict(reversed(list(origins.items()))))
    message.update(status='ready', retry_at=12.5, job_id='dynamic-job', received_at=99999)
    save_message(store, message)
    assert mail_origin_fingerprint(load_trusted_mail_origins(store, entries)) == first
    changed = dict(origins)
    changed['entry-0'] = replace(changed['entry-0'], uid=43)
    assert mail_origin_fingerprint(changed) != first
    assert mail_origin_fingerprint({}) != first


def test_missing_mail_tables_do_not_migrate_or_write(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    before = store.database.read_bytes()
    assert load_trusted_mail_origins(store, [{'id': 'not-present'}]) == {}
    assert store.database.read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('id', '0' * 64), ('account_id', '1' * 64), ('uidvalidity', '20260002'),
    ('uid', 43), ('uid', True), ('project_id', 'different-project'),
    ('message_id', None), ('mailbox', 'Archive'), ('folder', 'Archive'),
    ('source_issues', ['one attachment was rejected']), ('material_ids', []),
    ('material_ids', ['entry-0', 'entry-0', 'entry-1']),
])
def test_message_json_cannot_disagree_with_authoritative_scope_or_membership(env: Any, field: str, value: Any) -> None:
    store, entries, message = env
    message[field] = value
    save_message(store, message)
    result = load_trusted_mail_origins(store, entries)
    if field == 'material_ids' and value == ['entry-0', 'entry-0', 'entry-1']:
        assert set(result) == {'entry-1'}
    else:
        assert not result


@pytest.mark.parametrize('table,column,value', [
    ('mail_agent_accounts', 'folder', 'Archive'),
    ('mail_agent_accounts', 'address', 'different@163.com'),
    ('mail_agent_accounts', 'uidvalidity', '20260002'),
    ('mail_agent_accounts', 'uidvalidity', ''),
    ('mail_agent_messages', 'uidvalidity', '20260002'),
    ('mail_agent_messages', 'uid', 43),
    ('mail_agent_messages', 'account_id', '0' * 64),
])
def test_sql_scope_columns_cannot_disagree_with_ids_or_json(env: Any, table: str, column: str, value: Any) -> None:
    store, entries, _ = env
    with store.connect() as db:
        db.execute(f'UPDATE {table} SET {column}=?', (value,))
    assert not load_trusted_mail_origins(store, entries)


def test_account_state_json_may_not_shadow_scope_columns(env: Any) -> None:
    store, entries, _ = env
    with store.connect() as db:
        db.execute('UPDATE mail_agent_accounts SET data=?', (json.dumps({'folder': 'Archive'}),))
    assert not load_trusted_mail_origins(store, entries)


@pytest.mark.parametrize('defect', ['changed_original', 'missing_original', 'wrong_path', 'duplicate_digest', 'invalid_digest'])
def test_entire_message_original_manifest_must_remain_intact(env: Any, defect: str) -> None:
    store, entries, message = env
    original = Path(message['files'][0]['path'])
    if defect == 'changed_original':
        original.write_bytes(b'replaced original')
    elif defect == 'missing_original':
        original.unlink()
    elif defect == 'wrong_path':
        # Equal bytes at the intake copy are not the producer's original path.
        message['files'][0]['path'] = entries[0]['path']
    elif defect == 'duplicate_digest':
        message['files'].append(dict(message['files'][0]))
    else:
        message['files'][0]['digest'] = 'not-a-digest'
    save_message(store, message)
    assert not load_trusted_mail_origins(store, entries)


def test_current_original_is_rehashed_on_every_load(env: Any) -> None:
    store, entries, _ = env
    assert len(load_trusted_mail_origins(store, entries)) == 2
    Path(entries[0]['path']).write_bytes(b'replaced current intake original')
    assert set(load_trusted_mail_origins(store, entries)) == {'entry-1'}


@pytest.mark.parametrize('field,value', [('project_id', 'other-project'), ('source', 'upload'),
    ('batch', '0' * 64), ('revision', 2), ('digest', '0' * 64), ('path', 'missing.pdf')])
def test_entry_snapshot_must_match_current_intake_record(env: Any, field: str, value: Any) -> None:
    store, entries, _ = env
    entries[0][field] = value
    assert set(load_trusted_mail_origins(store, entries)) == {'entry-1'}


def test_fake_mail_context_upload_batch_and_legacy_uid_never_supply_authority(env: Any) -> None:
    store, entries, message = env
    for entry in entries:
        entry.update(source='upload', mail_context={'message_uid': '42'},
                     mail_origin={'verified': True, 'account_id': message['account_id']})
        with store.connect() as db:
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
    assert not load_trusted_mail_origins(store, entries)
    # A legacy history-like JSON cannot substitute for the scoped mail producer.
    save_message(store, {'message_uid': '42', 'subject': '发票', 'attachment_hash': entries[0]['digest']})
    assert not load_trusted_mail_origins(store, entries)


def test_message_id_header_can_be_empty_when_uid_scope_is_complete(env: Any) -> None:
    store, entries, message = env
    message['message_id'] = ''
    save_message(store, message)
    assert len(load_trusted_mail_origins(store, entries)) == 2


def test_repeated_input_id_and_malformed_authority_json_fail_closed(env: Any) -> None:
    store, entries, _ = env
    assert set(load_trusted_mail_origins(store, [*entries, copy.deepcopy(entries[0])])) == {'entry-1'}
    with store.connect() as db:
        db.execute('UPDATE mail_agent_messages SET data=?', ('not-json',))
    assert not load_trusted_mail_origins(store, entries)
