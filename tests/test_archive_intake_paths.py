"""Synthetic proofs cannot bypass archive state through legacy import paths."""
from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from invoice_print_layout.intake_archive import IntakeArchive
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.receipt_intake import save_receipt
from invoice_print_layout.storage import append_history, ensure_workspace
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


def picture(color: str = 'white') -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (40, 80), color).save(stream, format='PNG')
    return stream.getvalue()


def archive(queue: IntakeQueue, name: str, payload: bytes, status: str) -> dict[str, Any]:
    entry = queue.receive(name, payload)
    actions = IntakeArchive(queue.store)
    entry = actions.transition(entry['id'], entry['revision'], '', 'archive')['entry']
    if status == 'deleted':
        entry = actions.transition(entry['id'], entry['revision'], '', 'delete')['entry']
    return queue.get(entry['id'])


def ledger_state(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('expenses', 'attachments', 'events', 'intake_queue')}


def values() -> dict[str, str]:
    return {'title': 'Synthetic purchase', 'category': '材料采购', 'amount': '24.60'}


@pytest.mark.parametrize('status', ['archived', 'deleted'])
def test_attachment_rejects_inactive_digest_without_touching_matter(tmp_path: Path, status: str) -> None:
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store)
    item = store.create(values())
    payload = picture()
    entry = archive(queue, 'original.png', payload, status)
    before = ledger_state(store)
    with pytest.raises(ValueError, match='归档'):
        store.add_attachment(item['id'], 'renamed.png', payload, 'payment')
    assert ledger_state(store) == before
    assert not list(store.files.iterdir())
    assert Path(entry['path']).read_bytes() == payload


@pytest.mark.parametrize('status', ['archived', 'deleted'])
def test_receipt_rejects_inactive_digest_without_partial_registration(tmp_path: Path, status: str) -> None:
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store)
    payload = picture()
    entry = archive(queue, 'receipt.png', payload, status)
    before = ledger_state(store)
    with pytest.raises(ValueError, match='归档'):
        save_receipt(store, 'renamed.png', payload, values(), 'purchase')
    assert ledger_state(store) == before
    assert not list(store.files.iterdir())
    assert Path(entry['path']).read_bytes() == payload


@pytest.mark.parametrize('status', ['archived', 'deleted'])
@pytest.mark.parametrize('blocked_file', ['package', 'original'])
def test_history_skips_entire_inactive_record_and_reports_other_imports(
        tmp_path: Path, status: str, blocked_file: str) -> None:
    paths = ensure_workspace(tmp_path)
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store)
    output = make_invoice_pdf(paths.completed / 'blocked.pdf')
    original_folder = paths.archived / 'blocked'
    original_folder.mkdir()
    original = original_folder / 'receipt.png'
    original.write_bytes(picture())
    blocked = output if blocked_file == 'package' else original
    entry = archive(queue, blocked.name, blocked.read_bytes(), status)
    append_history(paths.history, {'output': str(output), 'archive': str(original_folder),
                                  'amount': '24.60', 'category': '材料采购', 'invoice_date': '2026-09-01'})
    before = ledger_state(store)
    assert store.import_history() == {'added': 0, 'skipped': 1}
    assert ledger_state(store) == before
    assert Path(entry['path']).read_bytes() == blocked.read_bytes()
    receipt = json.loads((store.root / 'history-import.json').read_text(encoding='utf-8'))
    assert receipt == {'added': 0, 'skipped': 1}

    ordinary = make_invoice_pdf(paths.completed / 'ordinary.pdf', invoice_number='22222222222222222222')
    ordinary_folder = paths.archived / 'ordinary'
    ordinary_folder.mkdir()
    (ordinary_folder / 'detail.png').write_bytes(picture('blue'))
    append_history(paths.history, {'output': str(ordinary), 'archive': str(ordinary_folder),
                                  'amount': '18.20', 'category': '其他', 'invoice_date': '2026-09-02'})
    assert store.import_history() == {'added': 1, 'skipped': 1}
    item = store.list_items()[0]
    assert item['title'] == 'ordinary' and item['amount'] == '18.20'
    assert len(item['attachments']) == 2 and len(item['events']) == 1
    assert item['stage'] == 'draft' and not item['verified'] and item['legacy']
    assert queue.get(entry['id']) == entry
    assert store.import_history() == {'added': 0, 'skipped': 1}


def test_normal_intake_proofs_still_allow_direct_attachment_and_receipt(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store)
    item = store.create(values())
    attachment = picture()
    queue.receive('pending.png', attachment)
    saved = store.add_attachment(item['id'], 'pending.png', attachment, 'purchase')
    assert len(saved['attachments']) == 1
    receipt = picture('blue')
    queue.receive('new.png', receipt)
    created = save_receipt(store, 'new.png', receipt, values(), 'purchase')['item']
    assert len(created['attachments']) == 1 and len(store.list_items()) == 2


def test_history_keeps_existing_source_fields_and_event_contract(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    store = ExpenseStore(tmp_path)
    IntakeQueue(store)
    output = make_invoice_pdf(paths.completed / 'ordinary.pdf')
    item = store.create({'title': 'Existing title', 'category': '其他', 'amount': '9.10',
                         'note': 'Existing note'}, source='history:' + str(output.resolve()))
    with store.connect() as db:
        before = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
    append_history(paths.history, {'output': str(output), 'amount': '24.60', 'category': '材料采购',
                                  'invoice_date': '2026-09-01'})
    assert store.import_history() == {'added': 1, 'skipped': 0}
    with store.connect() as db:
        after = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
    assert after == {**before, 'legacy': True, 'history_imported': True}
    saved = store.get(item['id'])
    assert len(saved['events']) == 1 and len(saved['attachments']) == 1


def test_history_attachment_failure_rolls_back_its_matter_and_event(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    store = ExpenseStore(tmp_path)
    IntakeQueue(store)
    output = make_invoice_pdf(paths.completed / 'ordinary.pdf')
    append_history(paths.history, {'output': str(output), 'amount': '24.60', 'category': '材料采购',
                                  'invoice_date': '2026-09-01'})
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_history_attachment BEFORE INSERT ON attachments "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic attachment failure'); END")
    before = ledger_state(store)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic attachment failure'):
        store.import_history()
    assert ledger_state(store) == before and output.is_file()
