from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.helpers import make_invoice_pdf, make_trip_pdf


@pytest.fixture
def store(tmp_path: Path) -> ExpenseStore:
    result = ExpenseStore(tmp_path)
    registry = LogisticsStore(tmp_path / '后勤' / 'tasks.sqlite3')
    registry.create_project('Review project')
    registry.create_project('Other project')
    return result


def expense(store: ExpenseStore, title: str, project: str = 'Review project') -> dict[str, Any]:
    return store.create({'title': title, 'project': project, 'category': '餐饮',
                         'amount': '15.00', 'material_basis': 'invoice_payment'})


def invoice(store: ExpenseStore, item_id: str, tmp_path: Path) -> dict[str, Any]:
    payload = make_invoice_pdf(tmp_path / f'{item_id}-invoice.pdf').read_bytes()
    return store.add_attachment(item_id, 'invoice.pdf', payload, 'invoice')


def payment(store: ExpenseStore, item_id: str, tmp_path: Path) -> dict[str, Any]:
    payload = make_trip_pdf(tmp_path / f'{item_id}-payment.pdf').read_bytes()
    return store.add_attachment(item_id, 'payment.pdf', payload, 'payment')


def versions(items: list[dict[str, Any]]) -> dict[str, str]:
    return {item['id']: item['version'] for item in items}


def request(store: ExpenseStore, items: list[dict[str, Any]]) -> None:
    store.request_review([item['id'] for item in items], versions(items))


def test_batch_request_preserves_missing_materials_and_persists_queue(store: ExpenseStore, tmp_path: Path) -> None:
    first = invoice(store, expense(store, 'Invoice without payment')['id'], tmp_path)
    second = expense(store, 'No original materials')
    original_path, _ = store.attachment_path(first['attachments'][0]['id'])
    original_payload = original_path.read_bytes()
    assert not first['pending_review'] and not second['pending_review']

    request(store, [first, second])

    reopened = ExpenseStore(tmp_path)
    for before in (first, second):
        after = reopened.get(before['id'])
        assert after['review_requested'] and after['pending_review']
        assert after['stage'] == 'draft' and not after['verified'] and not after['ready']
        assert after['missing'] == before['missing'] and not after['complete']
        assert after['attachments'] == before['attachments']
        assert after['amount_cents'] == before['amount_cents']
        assert len(after['events']) == len(before['events']) + 1
    assert original_path.read_bytes() == original_payload


def test_complete_unverified_items_are_pending_review_without_manual_request(store: ExpenseStore, tmp_path: Path) -> None:
    item = invoice(store, expense(store, 'Complete materials')['id'], tmp_path)
    item = payment(store, item['id'], tmp_path)
    assert item['complete'] and item['pending_review']
    assert not item.get('review_requested', False) and not item['verified'] and not item['ready']
    request(store, [item])
    assert store.get(item['id']) == item
    verified = store.transition(item['id'], 'verify')
    assert verified['ready'] and not verified['pending_review']
    submitted = store.transition(item['id'], 'submitted')
    assert submitted['stage'] == 'submitted' and not submitted['pending_review']


def test_requested_queue_does_not_hide_unavailable_originals_or_allow_submission(store: ExpenseStore, tmp_path: Path) -> None:
    item = invoice(store, expense(store, 'Missing original file')['id'], tmp_path)
    request(store, [item])
    path, _ = store.attachment_path(item['attachments'][0]['id'])
    path.unlink()
    current = store.get(item['id'])
    assert current['pending_review'] and current['review_requested']
    assert current['missing'] == ['发票或收据', '付款截图']
    assert not current['complete'] and not current['ready'] and not current['verified']
    for action in ('verify', 'submitted', 'reimbursed'):
        with pytest.raises(ValueError):
            store.transition(item['id'], action)
    assert store.get(item['id']) == current


@pytest.mark.parametrize('failure', ['stale_version', 'missing_version', 'unknown_id', 'other_project',
                                    'verified', 'cancelled', 'submitted', 'reimbursed'])
def test_batch_validation_is_atomic(store: ExpenseStore, tmp_path: Path, failure: str) -> None:
    first = expense(store, 'First still missing')
    second = expense(store, 'Second', 'Other project' if failure == 'other_project' else 'Review project')
    if failure in {'verified', 'submitted', 'reimbursed'}:
        second = invoice(store, second['id'], tmp_path)
        second = payment(store, second['id'], tmp_path)
        second = store.transition(second['id'], 'verify')
        if failure != 'verified':
            second = store.transition(second['id'], 'submitted')
        if failure == 'reimbursed':
            second = store.transition(second['id'], 'reimbursed')
    elif failure == 'cancelled':
        second = store.transition(second['id'], 'cancelled')
    ids = [first['id'], second['id']]
    expected = versions([first, second])
    if failure == 'stale_version':
        store.update(second['id'], {**second, 'note': 'Edited after selection'})
    elif failure == 'missing_version':
        expected.pop(second['id'])
    elif failure == 'unknown_id':
        ids[-1] = 'missing-id'
        expected['missing-id'] = second['version']
    before = store.list_items()

    with pytest.raises(ValueError):
        store.request_review(ids, expected)

    assert store.list_items() == before


@pytest.mark.parametrize('ids', [[], ['duplicate', 'duplicate']])
def test_batch_requires_nonempty_distinct_selection(store: ExpenseStore, ids: list[str]) -> None:
    item = expense(store, 'Unselected expense')
    selected = [item['id'] for _ in ids]
    before = store.list_items()
    with pytest.raises(ValueError):
        store.request_review(selected, versions([item]))
    assert store.list_items() == before


def test_repeated_request_is_idempotent_even_in_a_mixed_batch(store: ExpenseStore) -> None:
    first = expense(store, 'Already queued')
    second = expense(store, 'Newly queued')
    request(store, [first])
    first = store.get(first['id'])
    request(store, [first])
    assert store.get(first['id']) == first

    request(store, [first, second])
    assert store.get(first['id']) == first
    after = store.get(second['id'])
    assert after['review_requested'] and after['pending_review']
    assert len(after['events']) == len(second['events']) + 1


def test_stale_version_is_rejected_for_an_already_requested_item(store: ExpenseStore) -> None:
    item = expense(store, 'Selected before queue action')
    request(store, [item])
    current = store.get(item['id'])
    with pytest.raises(ValueError):
        request(store, [item])
    assert store.get(item['id']) == current


def test_content_change_clears_manual_queue_but_preserves_missing_items(store: ExpenseStore) -> None:
    item = expense(store, 'Queued then edited')
    request(store, [item])
    current = store.get(item['id'])
    edited = store.update(item['id'], {**current, 'note': 'Payment correspondence changed',
                                      'expected_version': current['version']})
    assert not edited.get('review_requested', False) and not edited['pending_review']
    assert edited['missing'] == current['missing'] and not edited['complete']
    assert not edited['verified'] and not edited['ready']
    request(store, [edited])
    assert store.get(item['id'])['pending_review']


@pytest.mark.parametrize('role', ['other', 'payment'])
def test_new_attachment_clears_manual_request_and_recomputes_queue(store: ExpenseStore, tmp_path: Path, role: str) -> None:
    item = invoice(store, expense(store, 'Queued before new evidence')['id'], tmp_path)
    request(store, [item])
    payload = make_trip_pdf(tmp_path / 'new-payment.pdf').read_bytes()
    after = store.add_attachment(item['id'], 'new-payment.pdf', payload, role)
    assert not after.get('review_requested', False)
    assert after['complete'] == (role == 'payment')
    assert after['pending_review'] == (role == 'payment')
    assert after['missing'] == ([] if role == 'payment' else ['付款截图'])
    assert not after['verified'] and not after['ready']


def test_attachment_role_change_clears_manual_queue(store: ExpenseStore, tmp_path: Path) -> None:
    item = invoice(store, expense(store, 'Queue then reclassify')['id'], tmp_path)
    request(store, [item])
    store.set_role(item['attachments'][0]['id'], 'other')
    after = store.get(item['id'])
    assert not after.get('review_requested', False) and not after['pending_review']
    assert after['missing'] == ['发票或收据', '付款截图']
    assert not after['verified'] and not after['ready']


def test_clearing_manual_request_keeps_complete_unverified_item_pending(store: ExpenseStore, tmp_path: Path) -> None:
    item = invoice(store, expense(store, 'Complete and explicitly queued')['id'], tmp_path)
    item = payment(store, item['id'], tmp_path)
    request(store, [item])
    item = store.get(item['id'])
    edited = store.update(item['id'], {**item, 'title': 'Changed complete item'})
    assert not edited.get('review_requested', False)
    assert edited['pending_review'] and edited['complete']
    assert not edited['verified'] and not edited['ready']


def test_normal_update_cannot_forge_material_completeness_or_review_queue(store: ExpenseStore) -> None:
    item = expense(store, 'No supporting evidence')
    edited = store.update(item['id'], {**item, 'complete': True, 'verified': True,
                                      'review_requested': True, 'pending_review': True})
    assert not edited.get('review_requested', False) and not edited['pending_review']
    assert not edited['complete'] and not edited['verified'] and not edited['ready']
    assert edited['missing'] == ['发票或收据', '付款截图']


def test_http_review_request_uses_selected_ids_versions_and_existing_token(store: ExpenseStore, tmp_path: Path) -> None:
    first = expense(store, 'HTTP selected missing materials')
    second = expense(store, 'HTTP unselected expense')
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root + '/api/state') as response:
            token = json.load(response)['token']
        body = {'ids': [first['id']], 'versions': versions([first])}
        with pytest.raises(HTTPError):
            urlopen(Request(root + '/api/review/request', data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json'}))
        assert store.get(first['id']) == first

        headers = {'Content-Type': 'application/json', 'X-Workbench-Token': token}
        with urlopen(Request(root + '/api/review/request', data=json.dumps(body).encode(), headers=headers)) as response:
            result = json.load(response)
        assert result['changed'] == 1 and len(result['items']) == 1
        current = store.get(first['id'])
        assert result['items'][0] == current
        assert current['pending_review'] and not current['complete'] and not current['verified']
        assert store.get(second['id']) == second

        body['versions'] = versions([current])
        with urlopen(Request(root + '/api/review/request', data=json.dumps(body).encode(), headers=headers)) as response:
            assert json.load(response)['changed'] == 0
        assert store.get(first['id']) == current

        stale_batch = {'ids': [second['id'], first['id']], 'versions': versions([second, first])}
        with pytest.raises(HTTPError) as rejected:
            urlopen(Request(root + '/api/review/request', data=json.dumps(stale_batch).encode(), headers=headers))
        assert rejected.value.code == 400
        assert store.get(first['id']) == current and store.get(second['id']) == second
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_intake_project_reconciliation_clears_queue_only_when_ownership_changes(store: ExpenseStore, tmp_path: Path) -> None:
    project = next(project for project in ExpenseProjects(store).list() if project['name'] == 'Review project')
    payload = make_invoice_pdf(tmp_path / 'reconciled-invoice.pdf').read_bytes()
    queue = IntakeQueue(store)
    queue.receive('invoice.pdf', payload, project_id=project['id'])
    item = store.create({'title': 'Unassigned invoice', 'category': '餐饮', 'amount': '15',
                         'material_basis': 'invoice_payment'})
    item = store.add_attachment(item['id'], 'invoice.pdf', payload, 'invoice')
    request(store, [item])
    queued = store.get(item['id'])
    assert queued['project'] == '待分配项目' and queued['review_requested']

    queue.reconcile()

    assigned = store.get(item['id'])
    assert assigned['project_id'] == project['id'] and assigned['project'] == project['name']
    assert not assigned['review_requested'] and not assigned['pending_review']
    assert assigned['missing'] == queued['missing'] == ['付款截图']
    assert assigned['attachments'] == queued['attachments']
    assert not assigned['complete'] and not assigned['verified'] and not assigned['ready']

    request(store, [assigned])
    requested_again = store.get(item['id'])
    queue.reconcile()
    assert store.get(item['id']) == requested_again


def test_legacy_project_migration_clears_queue_only_when_binding_changes(store: ExpenseStore) -> None:
    item = expense(store, 'Legacy project label', 'Later registered project')
    assert item['project_id'] == ''
    request(store, [item])
    queued = store.get(item['id'])
    project = ExpenseProjects(store).registry.create_project('Later registered project')

    assert ExpenseProjects(store).migrate() == {'matched': 1, 'pending': 0}

    assigned = store.get(item['id'])
    assert assigned['project_id'] == project['id'] and assigned['project'] == project['name']
    assert not assigned['review_requested'] and not assigned['pending_review']
    assert assigned['missing'] == queued['missing'] == ['发票或收据', '付款截图']
    assert assigned['attachments'] == queued['attachments']
    assert not assigned['complete'] and not assigned['verified'] and not assigned['ready']

    request(store, [assigned])
    requested_again = store.get(item['id'])
    assert ExpenseProjects(store).migrate() == {'matched': 0, 'pending': 0}
    assert store.get(item['id']) == requested_again
