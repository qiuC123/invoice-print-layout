from pathlib import Path
from typing import Any
import json
import os

import pymupdf
import pytest

from invoice_print_layout.file_cache import file_digest
from invoice_print_layout.ride_proofs import native_trip_pdf
from invoice_print_layout.courier_proofs import native_waybill_pdf
from invoice_print_layout.workbench import ExpenseStore, expense_identifier_keys
from tests.helpers import make_trip_pdf
from tests.test_courier import make_courier_pair
from tests.test_material_matching import setup, proof
from tests.test_missing_material_confirmation import _expense, _acknowledge, _proof


def test_digest_cache_uses_file_identity_even_when_size_and_time_are_restored(tmp_path: Path, monkeypatch: Any) -> None:
    path = tmp_path / 'original.bin'
    path.write_bytes(b'original')
    original = Path.read_bytes
    calls = []

    def read(file: Path) -> bytes:
        calls.append(file)
        return original(file)

    monkeypatch.setattr(Path, 'read_bytes', read)
    old = file_digest(path)
    assert file_digest(path) == old and len(calls) == 1
    stat = path.stat()
    replacement = tmp_path / 'replacement.bin'
    replacement.write_bytes(b'replaced')
    os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(replacement, path)
    assert file_digest(path) != old and len(calls) == 2
    path.unlink()
    with pytest.raises(OSError):
        file_digest(path)


def test_native_pdf_caches_do_not_reparse_or_accept_replaced_originals(tmp_path: Path, monkeypatch: Any) -> None:
    trip = make_trip_pdf(tmp_path / 'trip.pdf')
    invoice, waybill = make_courier_pair(tmp_path, '顺丰')
    # The fixture returns detail first in some adapters; choose by actual proof.
    waybill = next(path for path in (invoice, waybill) if native_waybill_pdf(path))
    paths = [(trip, native_trip_pdf), (waybill, native_waybill_pdf)]
    for path, check in paths:
        digest = file_digest(path)
        assert check(path, digest)
        with monkeypatch.context() as patch:
            def forbidden(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError('warm proof must not reopen PDF')
            patch.setattr(pymupdf, 'open', forbidden)
            assert check(path, digest)
        path.write_bytes(proof('replacement without proof text'))
        assert not check(path, digest)
        assert not check(path)


def test_cached_missing_source_still_invalidates_review_when_replaced(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = _acknowledge(store, _expense(store))
    assert store.get(item['id'])['missing_acknowledged']
    assert store.get(item['id'])['ready']
    path, _ = store.attachment_path(item['attachments'][0]['id'])
    replacement = tmp_path / 'replacement.pdf'
    replacement.write_bytes(_proof('Replacement after warm cache'))
    os.replace(replacement, path)
    refreshed = store.get(item['id'])
    assert not refreshed['ready'] and not refreshed['missing_acknowledged']
    assert refreshed['version'] != item['version']
    with pytest.raises(ValueError, match='已变化'):
        _acknowledge(store, item)


def test_identifier_cache_preserves_fresh_linked_intake_and_defensive_sets(setup: Any, monkeypatch: Any) -> None:
    import invoice_print_layout.transaction_evidence as evidence
    matcher, project, _ = setup
    item = matcher.store.create({'title': 'Synthetic identity', 'category': '其他', 'amount': '17'})
    entry = matcher.intake.receive('proof.pdf', proof('identity'), project_id=project)
    entry.update(item_id=item['id'], text='淘宝闪购\n订单号：4006506215532850730', role='order')
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    calls = []
    original = evidence.record_identifier_keys

    def count(data: dict[str, Any]) -> set[str]:
        calls.append(data)
        return original(data)

    monkeypatch.setattr(evidence, 'record_identifier_keys', count)
    with matcher.store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()[0])
        first = expense_identifier_keys(db, item['id'], data)
        assert first
        expense_identifier_keys(db, item['id'], data).clear()
        assert expense_identifier_keys(db, item['id'], data) == first
        assert len(calls) == 2
        entry['text'] = entry['text'].replace('2850730', '2850731')
        matcher._write_queue(db, entry)
        assert expense_identifier_keys(db, item['id'], data) != first
        assert len(calls) == 3
        data['note'] = 'New stored data hash'
        expense_identifier_keys(db, item['id'], data)
        assert len(calls) == 4


def test_board_lists_expenses_once_and_event_index_exists(setup: Any, monkeypatch: Any) -> None:
    matcher, project, _ = setup
    original = matcher.store.list_items
    calls = []

    def count(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(matcher.store, 'list_items', count)
    matcher.board(project)
    assert calls == [{'include_deleted': True}]
    with matcher.store.connect() as db:
        assert [row['name'] for row in db.execute('PRAGMA index_info(events_item_id_id)')] == ['item_id', 'id']
