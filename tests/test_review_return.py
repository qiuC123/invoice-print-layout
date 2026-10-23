"""Returning reviewed expenses is explicit, atomic, and preserves past files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

import pytest

from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.review_return import return_to_review
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ExpenseStore, str, str]:
    store = ExpenseStore(tmp_path)
    projects = ExpenseProjects(store)
    first = projects.create('Synthetic main project')['id']
    second = projects.create('Synthetic second project')['id']
    return store, first, second


def _ready(store: ExpenseStore, project: str, title: str = 'Synthetic expense', *,
           missing: bool = False) -> dict[str, Any]:
    item = store.create({'title': title, 'project_id': project, 'category': '酒店', 'amount': '18.50'})
    if not missing:
        item = store.add_attachment(item['id'], 'invoice.pdf',
                                    make_invoice_pdf(store.workspace / 'synthetic.pdf').read_bytes(), 'invoice')
    return store.transition(item['id'], 'verify', expected_version=item['version'],
                            acknowledge_missing=missing, expected_missing=item['missing'])


def _raw(store: ExpenseStore, key: str) -> dict[str, Any]:
    with store.connect() as db:
        result: dict[str, Any] = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
    return result


def _edit(store: ExpenseStore, item: dict[str, Any], **fields: Any) -> dict[str, Any]:
    with store.connect() as db:
        data = _raw(store, item['id'])
        data.update(fields)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    return store.get(item['id'])


def _ledger(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('expenses', 'attachments', 'events', 'selections')}


def _return(store: ExpenseStore, project: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return return_to_review(store, [item['id'] for item in items],
                            {item['id']: item['version'] for item in items}, project)


def test_return_preserves_every_business_field_original_and_batch_order(setup: Any) -> None:
    store, project, _ = setup
    first = _edit(store, _ready(store, project, 'First expense'),
                  reimbursement_person_id='synthetic-person', reimbursement_person_label='Synthetic person',
                  confirmation={'reason': 'Earlier amount check'}, note='Existing note')
    second = _ready(store, project, 'Second expense')
    items = [second, first]
    before = {item['id']: _raw(store, item['id']) for item in items}
    attachment_rows = _ledger(store)['attachments']
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in store.files.iterdir()}
    with store.connect() as db:
        db.execute('INSERT INTO selections VALUES (?,?)', ('synthetic-owner', first['id']))
    result = _return(store, project, items)
    assert result['changed'] == 2 and [item['id'] for item in result['items']] == [item['id'] for item in items]
    changing = {'stage', 'verified', 'review_requested', 'updated_at', 'missing_material_acknowledgement'}
    for item in result['items']:
        assert item['stage'] == 'draft' and not item['verified'] and not item['ready']
        assert item['pending_review'] and item['review_requested']
        assert {key: value for key, value in before[item['id']].items() if key not in changing} == {
            key: value for key, value in _raw(store, item['id']).items() if key not in changing}
        assert '批量退回待核对' in item['events'][0]['message']
    assert _ledger(store)['attachments'] == attachment_rows
    assert _ledger(store)['selections'] == [('synthetic-owner', first['id'])]
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())


def test_missing_material_acknowledgement_must_be_confirmed_again(setup: Any) -> None:
    store, project, _ = setup
    item = _ready(store, project, missing=True)
    assert item['ready'] and item['missing_acknowledged']
    returned = _return(store, project, [item])['items'][0]
    assert returned['missing'] == item['missing'] and not returned['complete']
    assert returned['pending_review'] and not returned['missing_acknowledged']
    assert 'missing_material_acknowledgement' not in _raw(store, item['id'])
    with pytest.raises(ValueError, match='补齐材料'):
        store.transition(item['id'], 'verify', expected_version=returned['version'])
    checked = store.transition(item['id'], 'verify', expected_version=returned['version'],
                               acknowledge_missing=True, expected_missing=returned['missing'])
    assert checked['ready'] and checked['missing_acknowledged']


@pytest.mark.parametrize('stage', ['submitted', 'reimbursed'])
@pytest.mark.parametrize('missing', [False, True])
def test_historical_report_files_survive_and_can_be_modified_after_return(
        setup: Any, stage: str, missing: bool) -> None:
    store, project, _ = setup
    item = _ready(store, project, missing=missing)
    files = {'pdf': str(make_invoice_pdf(store.workspace / 'previous-report.pdf')),
             'html': str(store.workspace / 'previous-report.html')}
    Path(files['html']).write_text('<p>Earlier report</p>', encoding='utf-8')
    store.submit_report([item], files)
    item = store.get(item['id'])
    if stage == 'reimbursed':
        item = store.transition(item['id'], stage, expected_version=item['version'])
    original_reports = {path: Path(path).read_bytes() for path in files.values()}
    earlier_events = item['events']
    returned = _return(store, project, [item])['items'][0]
    assert returned['stage'] == 'draft' and returned['pending_review'] and not returned['verified']
    assert returned['report_files'] == files
    assert returned['events'][1:] == earlier_events
    assert '历史报销包保留' in returned['events'][0]['message']
    assert {path: Path(path).read_bytes() for path in files.values()} == original_reports
    edited = store.edit_review_fields(returned['id'], returned['version'], 'living', '住宿费(附清单)', '20.00')
    assert edited['amount_cents'] == 2000 and edited['pending_review']
    assert edited['report_files'] == files


@pytest.mark.parametrize('invalid', ['empty', 'duplicate', 'missing_version', 'extra_version', 'empty_version',
                                   'wrong_version', 'wrong_ids', 'bad_id', 'over_limit', 'versions_not_dict'])
def test_requires_bounded_unique_selection_and_exact_versions(setup: Any, invalid: str) -> None:
    store, project, _ = setup
    items = [_ready(store, project, 'First'), _ready(store, project, 'Second')]
    ids: Any = [item['id'] for item in items]
    versions: Any = {item['id']: item['version'] for item in items}
    if invalid == 'empty':
        ids, versions = [], {}
    elif invalid == 'duplicate':
        ids = [items[0]['id']] * 2
        versions = {items[0]['id']: items[0]['version']}
    elif invalid == 'missing_version':
        versions.pop(items[-1]['id'])
    elif invalid == 'extra_version':
        versions['not-selected'] = 'extra'
    elif invalid == 'empty_version':
        versions[items[-1]['id']] = ''
    elif invalid == 'wrong_version':
        versions[items[-1]['id']] = True
    elif invalid == 'wrong_ids':
        ids = tuple(ids)
    elif invalid == 'bad_id':
        ids[-1] = 3
    elif invalid == 'versions_not_dict':
        versions = []
    else:
        ids = [str(index) for index in range(201)]
        versions = dict.fromkeys(ids, 'version')
    before = _ledger(store)
    with pytest.raises(ValueError, match='1至200'):
        return_to_review(store, ids, versions, project)
    assert _ledger(store) == before


@pytest.mark.parametrize('invalid', ['stale', 'unknown', 'foreign', 'unverified', 'cancelled', 'deleted',
                                   'merged', 'returned', 'archived', 'material_deleted', 'stale_ack'])
def test_invalid_later_expense_rejects_entire_batch_without_writing(setup: Any, invalid: str) -> None:
    store, project, foreign = setup
    first = _ready(store, project, 'First')
    second = _ready(store, project, 'Second', missing=invalid == 'stale_ack')
    ids = [first['id'], second['id']]
    expected = {item['id']: item['version'] for item in (first, second)}
    if invalid == 'stale':
        second = _edit(store, second, note='Changed after selection')
    elif invalid == 'unknown':
        expected['not-found'] = expected.pop(ids[-1])
        ids[-1] = 'not-found'
    elif invalid == 'stale_ack':
        second = _edit(store, second, amount_cents=1900)
        expected[second['id']] = second['version']
    else:
        fields = {'foreign': {'project_id': foreign}, 'unverified': {'verified': False},
                  'cancelled': {'stage': 'cancelled'}, 'deleted': {'deleted': True},
                  'merged': {'merged_into': first['id']}, 'returned': {'returned_to_synthesis': True},
                  'archived': {'synthesis_material_state': 'archived'},
                  'material_deleted': {'synthesis_material_state': 'deleted'}}[invalid]
        second = _edit(store, second, **fields)
        expected[second['id']] = second['version']
    before = _ledger(store)
    with pytest.raises(ValueError):
        return_to_review(store, ids, expected, project)
    assert _ledger(store) == before and store.get(first['id'])['ready']


@pytest.mark.parametrize('project', ['', None, 123, 'not-a-project'])
def test_rejects_invalid_project_without_writes(setup: Any, project: Any) -> None:
    store, current, _ = setup
    item = _ready(store, current)
    before = _ledger(store)
    with pytest.raises(ValueError, match='项目'):
        _return(store, project, [item])
    assert _ledger(store) == before


def test_return_write_failure_rolls_back_first_expense_and_event(setup: Any) -> None:
    store, project, _ = setup
    items = [_ready(store, project, 'First'), _ready(store, project, 'Second')]
    with store.connect() as db:
        db.execute('CREATE TRIGGER fail_second_return BEFORE UPDATE ON expenses WHEN NEW.id=\'' +
                   items[-1]['id'] + "' BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
    before = _ledger(store)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic write failure'):
        _return(store, project, items)
    assert _ledger(store) == before


def test_return_keeps_older_matching_undo_guard_before_changing_review(setup: Any) -> None:
    store, project, _ = setup
    item = _edit(store, _ready(store, project), material_match={'group_id': 'synthetic-old-group'})
    group = {'id': 'synthetic-old-group', 'item_id': item['id'], 'status': 'confirmed', 'queue_before': {}}
    with store.connect() as db:
        db.execute('CREATE TABLE material_matches (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT INTO material_matches VALUES (?,?)', (group['id'], json.dumps(group)))
        original_state = MaterialMatching._member_state(db, item['id'])
    _return(store, project, [item])
    with store.connect() as db:
        updated_group = json.loads(db.execute('SELECT data FROM material_matches').fetchone()['data'])
        assert updated_group['aggregate_after'] == original_state
        with pytest.raises(ValueError, match='后续内容已变化'):
            MaterialMatching._assert_group_snapshot(db, updated_group)


def test_competing_same_version_batches_cannot_return_twice(setup: Any) -> None:
    store, project, _ = setup
    items = [_ready(store, project, 'First'), _ready(store, project, 'Second')]
    barrier = threading.Barrier(2)

    def compete() -> str:
        barrier.wait(timeout=5)
        try:
            _return(store, project, items)
            return 'returned'
        except ValueError:
            return 'stale'

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: compete(), range(2)))
    assert sorted(results) == ['returned', 'stale']
    for item in items:
        current = store.get(item['id'])
        assert current['pending_review'] and not current['ready']
        assert sum('批量退回待核对' in event['message'] for event in current['events']) == 1
