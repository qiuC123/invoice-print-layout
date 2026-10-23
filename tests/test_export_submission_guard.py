"""Export eligibility is enforced for synthetic HTTP and domain callers."""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Any, Iterator
from contextlib import contextmanager

import pymupdf
import pytest

from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from invoice_print_layout.workbench_web import make_server


def matter(store: ExpenseStore, label: str, *, verified: bool) -> dict[str, Any]:
    item = store.create({'title': label, 'category': '酒店', 'amount': '14.00'})
    with pymupdf.open() as document:  # type: ignore[no-untyped-call]
        document.new_page().insert_text((60, 100), label)
        payload = document.tobytes()
    store.add_attachment(item['id'], 'synthetic.pdf', payload, 'invoice')
    if verified:
        store.transition(item['id'], 'verify')
    return store.get(item['id'])


def ledger_rows(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                for name in ('expenses', 'attachments', 'events', 'selections', 'automatic_ride_files')}


@contextmanager
def http_server(store: ExpenseStore) -> Iterator[tuple[int, str]]:
    server = make_server(store.workspace, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection('127.0.0.1', server.server_port)
    try:
        connection.request('GET', '/api/state')
        response = connection.getresponse()
        assert response.status == 200
        token = json.loads(response.read())['token']
        yield server.server_port, token
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post_export(port: int, token: str, ids: list[str]) -> tuple[int, dict[str, Any]]:
    connection = HTTPConnection('127.0.0.1', port)
    try:
        connection.request('POST', '/api/export', body=json.dumps({'ids': ids}),
                           headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('invalid', ['unverified', 'missing', 'cancelled', 'deleted'])
def test_direct_and_http_export_reject_mixed_unready_selection_without_partial_output(
        tmp_path: Path, invalid: str) -> None:
    store = ExpenseStore(tmp_path)
    ready = matter(store, 'REVIEWED SYNTHETIC SOURCE', verified=True)
    if invalid == 'missing':
        blocked = store.create({'title': 'MISSING SYNTHETIC SOURCE', 'category': '酒店', 'amount': '14.00'})
    else:
        blocked = matter(store, 'BLOCKED SYNTHETIC SOURCE', verified=False)
        if invalid == 'cancelled':
            blocked = store.transition(blocked['id'], 'cancelled')
        elif invalid == 'deleted':
            blocked = store.remove(blocked['id'], blocked['version'])
    ids = [ready['id'], blocked['id']]
    before = ledger_rows(store)
    originals = {file.name: file.read_bytes() for file in store.files.iterdir()}
    with pytest.raises(ValueError, match='材料齐全且已核对'):
        export_items(store, ids)
    with http_server(store) as (port, token):
        status, result = post_export(port, token, ids)
    assert status == 400 and '材料齐全且已核对' in result['error']
    assert ledger_rows(store) == before
    assert {file.name: file.read_bytes() for file in store.files.iterdir()} == originals
    folder = store.root / '导出'
    assert not folder.exists() or not list(folder.iterdir())


@pytest.mark.parametrize('stage', ['draft', 'submitted', 'reimbursed'])
def test_http_export_retains_reviewed_and_historical_stage_without_submitting(
        tmp_path: Path, stage: str) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store, 'REVIEWED SYNTHETIC SOURCE', verified=True)
    if stage != 'draft':
        item = store.transition(item['id'], 'submitted')
        if stage == 'reimbursed':
            item = store.transition(item['id'], 'reimbursed')
    before = ledger_rows(store)
    with http_server(store) as (port, token):
        status, result = post_export(port, token, [item['id']])
    assert status == 200 and set(result) == {'pdf', 'md'}
    with pymupdf.open(store.root / '导出' / result['pdf']) as output:  # type: ignore[no-untyped-call]
        assert len(output) == 1 and 'REVIEWED SYNTHETIC SOURCE' in output[0].get_text()
    assert ledger_rows(store) == before
    saved = store.get(item['id'])
    assert saved['stage'] == stage and saved['verified']
