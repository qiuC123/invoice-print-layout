from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.automatic_rides import organize_rides
from invoice_print_layout.storage import append_history
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import inbox_files, make_server
from tests.helpers import make_invoice_pdf, make_trip_pdf


def pair(store: ExpenseStore, suffix: str = '', amount: str = '123.45', uid: str = '1') -> tuple[Path, Path]:
    folder = store.root / '邮件收件'
    folder.mkdir(exist_ok=True)
    trip = make_trip_pdf(folder / f'trip{suffix}.pdf', amount=amount)
    invoice = make_invoice_pdf(folder / f'invoice{suffix}.pdf', amount=amount,
                               invoice_number='2600000000000000000' + (suffix or '1'))
    with pymupdf.open(invoice) as doc:
        doc[0].insert_text((72, 155), 'DIDI TRAVEL')
        doc.saveIncr()
    if suffix:
        with pymupdf.open(trip) as doc:
            doc[0].insert_text((72, 280), 'ROUTE ' + suffix)
            doc.saveIncr()
    for path in (trip, invoice):
        append_history(store.workspace / 'mail_imports.jsonl', {'attachment_hash': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'message_uid': uid, 'imported_at': '2026-09-25'})
    return trip, invoice


def test_pair_creates_one_expense_preserves_originals_and_is_idempotent(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    trip, invoice = pair(store)
    payloads = [p.read_bytes() for p in (trip, invoice)]
    assert organize_rides(store)['created'] == 1
    item = store.list_items()[0]
    assert item['amount'] == '123.45' and item['category'] == '打车'
    assert item['project'] == '待分配项目' and item['expense_date'] == ''
    assert item['complete'] and not item['verified'] and item['stage'] == 'draft'
    assert {a['role'] for a in item['attachments']} == {'invoice', 'trip'}
    assert organize_rides(store)['created'] == 0
    assert len(store.list_items()) == 1 and not inbox_files(store)
    assert payloads == [p.read_bytes() for p in (trip, invoice)]


def test_different_emails_same_amount_do_not_pair(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    trip, invoice = pair(store)
    (store.workspace / 'mail_imports.jsonl').unlink()
    for uid, p in enumerate((trip, invoice)):
        append_history(store.workspace / 'mail_imports.jsonl', {'attachment_hash': hashlib.sha256(p.read_bytes()).hexdigest(), 'message_uid': str(uid)})
    assert organize_rides(store)['created'] == 0
    assert len(inbox_files(store)) == 2


def test_ambiguous_pairs_and_missing_counterparts_wait(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    trip, invoice = pair(store)
    invoice.unlink()
    assert organize_rides(store)['created'] == 0
    pair(store)
    pair(store, '2')
    assert organize_rides(store)['created'] == 0
    assert len(inbox_files(store)) == 4


def test_supplements_existing_trip_without_new_expense(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    trip, invoice = pair(store)
    item = store.create({'title': 'Existing ride', 'category': '打车', 'amount': '123.45'})
    store.add_attachment(item['id'], trip.name, trip.read_bytes(), 'trip')
    result = organize_rides(store)
    assert result['created'] == 0 and result['supplemented'] == 1
    assert len(store.list_items()) == 1 and store.get(item['id'])['complete']


def test_reexported_invoice_and_trip_not_counted_twice_even_after_reimbursement(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    trip, invoice = pair(store)
    organize_rides(store)
    item = store.list_items()[0]
    for action in ('verify', 'submitted', 'reimbursed'):
        store.transition(item['id'], action)
    before = store.get(item['id'])
    # Change PDF metadata (different bytes, identical invoice number/trip content).
    for p in (trip, invoice):
        with pymupdf.open(p) as doc:
            doc.set_metadata({'title': 'new export'})
            doc.saveIncr()
    result = organize_rides(store)
    assert result['created'] == 0 and result['duplicates'] == 2
    assert store.get(item['id']) == before
    assert not inbox_files(store)


def test_same_invoice_number_changed_amount_is_not_silently_ignored(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    pair(store)
    organize_rides(store)
    pair(store, amount='99.00')
    result = organize_rides(store)
    assert result['created'] == 0 and any('冲突' in x for x in result['issues'].values())
    assert store.list_items()[0]['amount'] == '123.45'


def test_creation_and_attachment_links_roll_back_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    pair(store)
    import invoice_print_layout.automatic_rides as rides
    def fail(*args: object) -> None:
        raise OSError('disk write failed')
    with monkeypatch.context() as m:
        m.setattr(rides.os, 'replace', fail)
        with pytest.raises(OSError):
            organize_rides(store)
    assert store.list_items() == [] and len(inbox_files(store)) == 2
    assert organize_rides(store)['created'] == 1


def test_server_start_organizes_existing_inbox(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    pair(store)
    server = make_server(tmp_path, 0)
    server.server_close()
    assert len(store.list_items()) == 1 and not inbox_files(store)
