from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.test_material_matching import queued, snapshots


@pytest.fixture
def rail_web(tmp_path: Path) -> Any:
    matcher = MaterialMatching(ExpenseStore(tmp_path), IntakeQueue(ExpenseStore(tmp_path)))
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic rail project')['id']
    entries = [queued(matcher, project, f'synthetic-rail-payment-{i}', 'payment', str(amount),
                      text='当前状态\n支付成功\n商品\n12306消费\n商户全称\n中国铁路网络有限公司')
               for i, amount in enumerate((357, 403, 25, 25))]
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']

    def get(route: str) -> Any:
        with urlopen(root + route) as response:
            return json.load(response)

    def post(route: str, body: dict[str, Any]) -> Any:
        with urlopen(Request(root + '/api/' + route, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
            return json.load(response)

    try:
        yield matcher, project, entries, get, post, root
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_four_rail_payments_archived_without_changing_expenses(rail_web: Any) -> None:
    matcher, project, entries, get, post, root = rail_web
    before = snapshots(matcher)
    result = post('intake/rail-payments-unused', {'ids': [entry['id'] for entry in entries],
                                              'revisions': {entry['id']: entry['revision'] for entry in entries}})
    assert result['marked'] == 4 and result['unchanged'] == 0
    assert all('path' not in entry for entry in result['entries'])
    after = snapshots(matcher)
    assert all(after[table] == before[table] for table in before if table != 'intake_queue')
    for old, current in zip(entries, result['entries']):
        assert current['status'] == 'unused' and current['revision'] == old['revision'] + 1
        assert current['fields'] == old['fields'] and current['digest'] == old['digest']
        with urlopen(root + '/intake-file/' + old['id']) as response:
            assert hashlib.sha256(response.read()).hexdigest() == old['digest']
    board = get('/api/material-matching?project=' + project)
    assert len(board['unused']) == 4 and not board['payments'] and not board['suggestions']
    again = post('intake/rail-payments-unused', {'ids': [entry['id'] for entry in entries],
                                             'revisions': {entry['id']: entry['revision'] for entry in result['entries']}})
    assert again['marked'] == 0 and again['unchanged'] == 4
    assert snapshots(matcher) == after
    prepared = post('intake/prepare', {'id': entries[0]['id'], 'force_ai': True})
    assert prepared['entry']['status'] == 'unused'
    assert snapshots(matcher) == after


def test_stale_batch_is_atomic(rail_web: Any) -> None:
    matcher, _, entries, _, post, _ = rail_web
    before = snapshots(matcher)
    revisions = {entry['id']: entry['revision'] for entry in entries}
    revisions[entries[-1]['id']] -= 1
    with pytest.raises(HTTPError) as error:
        post('intake/rail-payments-unused', {'ids': list(revisions), 'revisions': revisions})
    assert error.value.code == 400
    assert snapshots(matcher) == before


def test_manual_classification_does_not_reintroduce_rail_payment(rail_web: Any) -> None:
    matcher, project, entries, _, post, _ = rail_web
    result = post('intake/classify', {'id': entries[0]['id'], 'revision': entries[0]['revision'],
                                    'role': 'payment', 'fields': {'amount': '357', 'project_id': project}})
    assert result['status'] == 'unused' and 'path' not in result
    assert not matcher.store.list_items()


def test_single_rail_invoice_is_complete_without_payment(rail_web: Any) -> None:
    matcher, project, _, get, post, _ = rail_web
    invoice = queued(matcher, project, 'synthetic-rail-invoice', 'invoice', '99', text='铁路电子客票')
    invoice['fields'].update(category='高铁', title='铁路车票')
    with matcher.store.connect() as db:
        matcher._write_queue(db, invoice)
    board = get('/api/material-matching?project=' + project)
    assert any(card['key'] == 'q:' + invoice['id'] for card in board['vouchers'])
    result = post('material-matching/create', {'voucher_keys': ['q:' + invoice['id']], 'payment_ids': [],
                  'project_id': project, 'revisions': {invoice['id']: invoice['revision']},
                  'fields': {'project_type': 'transport', 'project_name': '火车、飞机', 'amount': '99'}})
    item = result['item']
    assert item['category'] == '高铁' and item['material_basis'] == 'standard'
    assert item['complete'] and item['pending_review'] and not item['verified']
    assert '付款截图' not in item['missing']
    preview = post('expense-print-preview', {'id': item['id'], 'expected_version': item['version']})
    assert preview['complete'] and preview['page_count'] == 1
