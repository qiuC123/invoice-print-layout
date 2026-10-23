"""Whole-batch attribution changes ownership only, with no partial fee writes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from urllib.error import HTTPError

import pytest

from invoice_print_layout.reimbursement_people import ReimbursementPeople
from tests.test_material_matching import setup, expense, queued, revisions
from tests.test_reimbursement_people import people, assign, create
from tests.test_reimbursement_people_http import attribution_web
from tests.test_reimbursement_people_management import state, current
from tests.test_review_fields_and_withdrawal import hotel_withdrawal


def versions(items: list[dict[str, Any]]) -> dict[str, str]:
    return {item['id']: item['version'] for item in items}


def batch(domain: ReimbursementPeople, project: str, items: list[dict[str, Any]], person: dict[str, Any]) -> dict[str, Any]:
    return domain.assign_expenses(project, [item['id'] for item in items], versions(items), person['id'], person['version'])


def test_batch_assigns_unassigned_fees_once_without_financial_or_original_changes(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    items = [expense(matcher, project, 'unassigned-ten', '10'), expense(matcher, project, 'unassigned-four', '4')]
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in matcher.store.files.iterdir()}
    before = state(matcher)
    result = batch(domain, project, items, person)
    assert result['changed'] == 2 and result['person']['id'] == person['id']
    assert len(matcher.store.list_items()) == 2 and sum(item['amount_cents'] for item in result['items']) == 1400
    for old, new in zip(items, result['items']):
        assert new['id'] == old['id'] and new['reimbursement_person_id'] == person['id']
        for key in ('amount_cents', 'category', 'expense_date', 'verified', 'stage', 'attachments'):
            assert new[key] == old[key]
    after = state(matcher)
    assert before['attachments'] == after['attachments'] and before['people'] == after['people']
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())
    repeated = batch(domain, project, result['items'], current(domain, project, person))
    assert repeated['changed'] == 0 and state(matcher) == after


def test_explicit_unassigned_batch_is_a_noop_and_single_interface_uses_same_contract(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    items = [expense(matcher, project, 'plain-one'), expense(matcher, project, 'plain-two')]
    before = state(matcher)
    result = domain.assign_expenses(project, [item['id'] for item in items], versions(items), '')
    assert result['changed'] == 0 and state(matcher) == before
    single = domain.assign_expense(items[0]['id'], items[0]['version'], person['id'], person['version'])['item']
    before = state(matcher)
    repeated = domain.assign_expense(single['id'], single['version'], person['id'], person['version'])
    assert repeated['item'] == single and state(matcher) == before


@pytest.mark.parametrize('invalid', ['empty', 'duplicate', 'missing_version', 'extra_version', 'empty_version',
                                   'wrong_version_type', 'wrong_ids_type', 'over_limit'])
def test_batch_requires_complete_nonduplicate_bounded_selection(setup: Any, invalid: str) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    items = [expense(matcher, project, 'first'), expense(matcher, project, 'second')]
    ids: Any = [item['id'] for item in items]
    expected: dict[str, Any] = versions(items)
    if invalid == 'empty':
        ids, expected = [], {}
    elif invalid == 'duplicate':
        ids = [items[0]['id'], items[0]['id']]
        expected = {items[0]['id']: items[0]['version']}
    elif invalid == 'missing_version':
        expected.pop(items[-1]['id'])
    elif invalid == 'extra_version':
        expected['unselected'] = 'version'
    elif invalid == 'empty_version':
        expected[items[-1]['id']] = ''
    elif invalid == 'wrong_version_type':
        expected[items[-1]['id']] = 1
    elif invalid == 'wrong_ids_type':
        ids = tuple(ids)
    else:
        ids = [f'fee-{index}' for index in range(201)]
        expected = dict.fromkeys(ids, 'version')
    before = state(matcher)
    with pytest.raises(ValueError, match='1至200'):
        domain.assign_expenses(project, ids, expected, person['id'], person['version'])
    assert state(matcher) == before


@pytest.mark.parametrize('invalid', ['stale', 'unknown', 'foreign', 'verified', 'submitted', 'reimbursed',
                                   'cancelled', 'deleted', 'merged', 'legacy', 'history', 'archived', 'material_deleted'])
def test_one_invalid_later_fee_rejects_the_entire_batch_before_writes(setup: Any, invalid: str) -> None:
    matcher, project, foreign = setup
    domain, person, _ = people(matcher, project)
    items = [expense(matcher, project, 'first-valid'), expense(matcher, foreign if invalid == 'foreign' else project, 'second')]
    expected = versions(items)
    ids = [item['id'] for item in items]
    if invalid == 'stale':
        matcher.store.update(items[-1]['id'], {'note': 'Edited after batch selection'})
    elif invalid == 'unknown':
        ids[-1] = 'unknown-fee'
        expected = {ids[0]: items[0]['version'], ids[-1]: items[-1]['version']}
    elif invalid not in {'foreign'}:
        with matcher.store.connect() as db:
            raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (items[-1]['id'],)).fetchone()['data'])
            if invalid in {'submitted', 'reimbursed', 'cancelled'}:
                raw['stage'] = invalid
            elif invalid == 'verified':
                raw['verified'] = True
            elif invalid == 'merged':
                raw['merged_into'] = 'another-fee'
            elif invalid == 'history':
                raw['history_imported'] = True
            elif invalid in {'archived', 'material_deleted'}:
                raw['synthesis_material_state'] = 'archived' if invalid == 'archived' else 'deleted'
            else:
                raw[invalid] = True
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), items[-1]['id']))
        expected[items[-1]['id']] = matcher.store.get(items[-1]['id'])['version']
    before = state(matcher)
    with pytest.raises(ValueError):
        domain.assign_expenses(project, ids, expected, person['id'], person['version'])
    assert state(matcher) == before and not matcher.store.get(items[0]['id']).get('reimbursement_person_id')


@pytest.mark.parametrize('change', ['missing_version', 'rename', 'merge', 'retire', 'foreign_person'])
def test_changed_target_person_refuses_old_fee_selection(setup: Any, change: str) -> None:
    matcher, project, foreign = setup
    domain, person, target = people(matcher, project)
    items = [expense(matcher, project, 'first'), expense(matcher, project, 'second')]
    target_version: Any = person['version']
    if change == 'missing_version':
        target_version = None
    elif change == 'rename':
        domain.rename(project, person['id'], person['version'], 'New person name')
    elif change == 'merge':
        domain.merge(project, person['id'], person['version'], target['id'], target['version'])
    elif change == 'retire':
        domain.retire(project, person['id'], person['version'])
    else:
        person = domain.create(foreign, 'Foreign target')['person']
        target_version = person['version']
    before = state(matcher)
    with pytest.raises(ValueError):
        domain.assign_expenses(project, [item['id'] for item in items], versions(items), person['id'], target_version)
    assert state(matcher) == before


def test_existing_material_assignment_can_check_target_version_without_breaking_legacy_calls(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    entry = queued(matcher, project, 'material', 'invoice', '14')
    domain.rename(project, person['id'], person['version'], 'Changed target')
    before = state(matcher)
    with pytest.raises(ValueError, match='名单已变化'):
        domain.assign_materials(project, [entry['id']], revisions(entry), person['id'], person['version'])
    assert state(matcher) == before
    # Existing callers that do not carry a person version keep their contract.
    result = domain.assign_materials(project, [entry['id']], revisions(entry), person['id'])
    assert result['entries'][0]['reimbursement_person_label'] == 'Changed target'


def test_two_group_assignment_updates_nested_owners_and_preserves_pristine_undo(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    results = []
    for index in range(2):
        entries = assign(domain, project, source, queued(matcher, project, f'voucher-{index}', 'invoice', '14'),
                         queued(matcher, project, f'payment-{index}', 'payment', '14'))
        results.append(create(matcher, project, entries))
    before_count = len(matcher.store.list_items())
    assigned = batch(domain, project, [result['item'] for result in results], target)
    assert assigned['changed'] == 2 and len(matcher.store.list_items()) == before_count
    assert sum(item['amount_cents'] for item in assigned['items']) == 2800
    for result, item in zip(results, assigned['items']):
        assert all(record['reimbursement_person_id'] == target['id'] for record in
                   item['material_match']['recognition']['vouchers'] + item['material_match']['recognition']['payments'])
        matcher.undo(result['match']['id'], item['version'])
    assert all(entry.get('reimbursement_person_id') == target['id'] for entry in matcher.intake.list())


def test_batch_reassigns_aggregate_members_without_combining_independent_fees(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    members = [expense(matcher, project, 'member-ten', '10'), expense(matcher, project, 'member-four', '4')]
    members = [domain.assign_expense(item['id'], item['version'], source['id'])['item'] for item in members]
    payment = assign(domain, project, source, queued(matcher, project, 'combined-payment', 'payment', '14'))[0]
    result = matcher.match(['e:' + item['id'] for item in members], [payment['id']], project,
                           expected_versions=versions(members), revisions=revisions(payment))
    independent = expense(matcher, project, 'independent-seventeen', '17')
    assigned = batch(domain, project, [result['item'], independent], target)
    assert assigned['changed'] == 2 and [item['amount_cents'] for item in assigned['items']] == [1400, 1700]
    matcher.undo(result['match']['id'], assigned['items'][0]['version'])
    assert all(matcher.store.get(item['id'])['reimbursement_person_id'] == target['id'] for item in members)
    assert matcher.store.get(independent['id'])['amount_cents'] == 1700


def test_stale_group_content_is_not_adopted_by_batch_person_changes(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    entries = assign(domain, project, source, queued(matcher, project, 'voucher', 'invoice', '14'))
    result = create(matcher, project, entries)
    edited = matcher.store.edit_review_fields(result['item']['id'], result['item']['version'], 'other', 'Later manual amount', '20')
    ordinary = expense(matcher, project, 'ordinary-four', '4')
    assigned = batch(domain, project, [edited, ordinary], target)
    before = state(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], assigned['items'][0]['version'])
    assert state(matcher) == before and matcher.store.get(edited['id'])['amount_cents'] == 2000


def test_real_later_sql_sync_failure_rolls_back_the_first_fee_and_its_queue(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    entries = assign(domain, project, source, queued(matcher, project, 'first-voucher', 'invoice', '14'))
    first = create(matcher, project, entries)['item']
    second = expense(matcher, project, 'second-valid', '4')
    with matcher.store.connect() as db:
        db.execute("CREATE TRIGGER fail_second_person BEFORE UPDATE ON expenses WHEN OLD.id='" + second['id'] +
                   "' BEGIN SELECT RAISE(ABORT,'Synthetic second person failure'); END")
    before = state(matcher)
    with pytest.raises(sqlite3.IntegrityError, match='second person failure'):
        batch(domain, project, [first, second], target)
    assert state(matcher) == before


def test_all_group_links_are_validated_before_the_first_fee_write(setup: Any) -> None:
    matcher, project, _ = setup
    domain, _, target = people(matcher, project)
    first = expense(matcher, project, 'first-valid')
    second = expense(matcher, project, 'invalid-group-link')
    with matcher.store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (second['id'],)).fetchone()['data'])
        raw['material_match'] = {'group_id': 'missing-group'}
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), second['id']))
        db.execute("CREATE TRIGGER catch_premature_write BEFORE UPDATE ON expenses WHEN OLD.id='" + first['id'] +
                   "' BEGIN SELECT RAISE(ABORT,'First fee must not be written yet'); END")
    second = matcher.store.get(second['id'])
    before = state(matcher)
    with pytest.raises(ValueError, match='材料组合不存在'):
        batch(domain, project, [first, second], target)
    assert state(matcher) == before


def test_legacy_returned_material_state_cannot_be_changed_from_fee_assignment(setup: Any) -> None:
    matcher, project, _ = setup
    domain, target, _ = people(matcher, project)
    item = hotel_withdrawal(matcher, project)
    with matcher.store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        raw.pop('returned_to_synthesis')
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), item['id']))
    item = matcher.store.get(item['id'])
    before = state(matcher)
    with pytest.raises(ValueError, match='尚未核对的草稿'):
        batch(domain, project, [item], target)
    with pytest.raises(ValueError, match='尚未核对的草稿'):
        domain.assign_expense(item['id'], item['version'], target['id'], target['version'])
    assert state(matcher) == before and matcher.store.get(item['id'])['returned_to_synthesis']


def test_maximum_200_fees_are_distinct_and_attributed_in_one_commit(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    items = [matcher.store.create({'project_id': project, 'title': f'fee-{index}', 'amount': '1'}) for index in range(200)]
    result = batch(domain, project, items, person)
    assert result['changed'] == len(result['items']) == len(matcher.store.list_items()) == 200
    assert len({item['id'] for item in result['items']}) == 200 and sum(item['amount_cents'] for item in result['items']) == 20000


def test_concurrent_duplicate_batch_has_one_commit_and_one_stale_rejection(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    items = [expense(matcher, project, 'first'), expense(matcher, project, 'second')]
    start = threading.Barrier(2)
    def run() -> Any:
        start.wait(timeout=5)
        try:
            return batch(domain, project, items, person)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert len(matcher.store.list_items()) == 2


def test_batch_http_auth_target_and_fee_versions_are_atomic(attribution_web: Any) -> None:
    store, project, other, first, get, post = attribution_web
    second = store.create({'project_id': project, 'title': 'Second unassigned', 'amount': '4'})
    person = post('reimbursement-people/create', {'project_id': project, 'label': 'Existing target'})['person']
    body = {'project_id': project, 'ids': [first['id'], second['id']], 'versions': versions([first, second]),
            'person_id': person['id'], 'target_version': person['version']}
    with pytest.raises(HTTPError) as unauthenticated:
        post('reimbursement-people/assign-expenses', body, '')
    assert unauthenticated.value.code == 403
    with pytest.raises(HTTPError) as foreign:
        post('reimbursement-people/assign-expenses', {**body, 'project_id': other})
    assert foreign.value.code == 400
    result = post('reimbursement-people/assign-expenses', body)
    assert result['changed'] == 2 and [item['id'] for item in result['items']] == body['ids']
    assert all(item['reimbursement_person_id'] == person['id'] and not item['verified'] for item in result['items'])
    with pytest.raises(HTTPError) as stale:
        post('reimbursement-people/assign-expenses', body)
    assert stale.value.code == 400
    assert len(get('/api/state')['items']) == 2
