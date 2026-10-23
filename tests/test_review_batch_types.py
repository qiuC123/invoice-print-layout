"""Bulk template classification keeps each expense and its original evidence."""
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

from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.material_matching import MaterialMatching
from tests.test_material_creation import confirmed_creation
from tests.test_material_matching import setup, expense, queued, revisions, snapshots
from tests.test_reimbursement_people_http import attribution_web
from tests.test_review_fields_and_withdrawal import hotel_withdrawal, raw


def versions(items: list[dict[str, Any]]) -> dict[str, str]:
    return {item['id']: item['version'] for item in items}


def change(matcher: MaterialMatching, project: str, items: list[dict[str, Any]],
           kind: str = 'materials', name: Any = None) -> dict[str, Any]:
    return ExpenseReview(matcher.store).batch_project_type(
        project, [item['id'] for item in items], versions(items), kind, name)


def write_raw(matcher: MaterialMatching, item: dict[str, Any], **fields: Any) -> dict[str, Any]:
    with matcher.store.connect() as db:
        data = raw(matcher, item['id'])
        data.update(fields)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    return matcher.store.get(item['id'])


def test_editable_type_keeps_each_name_amount_identity_approval_and_original(setup: Any) -> None:
    matcher, project, _ = setup
    items = [expense(matcher, project, 'Original nails', '12.30'), expense(matcher, project, 'Original tape', '17')]
    items[0] = write_raw(matcher, items[0], project_type='other', project_name=' nails procurement ',
                         confirmation={'reason': 'Original manual approval'}, note='Saved note')
    before = snapshots(matcher)
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in matcher.store.files.iterdir()}
    result = change(matcher, project, items)
    assert result['changed'] == 2 and len(matcher.store.list_items()) == 2
    assert [item['project_name'] for item in result['items']] == [' nails procurement ', 'Original tape']
    assert sum(item['amount_cents'] for item in result['items']) == 2930
    changed_fields = {'project_type', 'project_name', 'category', 'report_section', 'report_group', 'display_name', 'updated_at'}
    for old, new in zip(items, result['items']):
        assert new['project_type'] == 'materials' and new['category'] == '材料采购' and new['report_section'] == 'materials'
        old_raw = json.loads(next(row[2] for row in before['expenses'] if row[0] == old['id']))
        new_raw = raw(matcher, new['id'])
        assert {key: value for key, value in old_raw.items() if key not in changed_fields} == {
            key: value for key, value in new_raw.items() if key not in changed_fields}
        assert new['attachments'] == old['attachments'] and not new['verified'] and new['stage'] == 'draft'
    after = snapshots(matcher)
    assert before['attachments'] == after['attachments'] and before['intake_queue'] == after['intake_queue']
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())


def test_fixed_detail_changes_template_names_but_preserves_distinct_original_titles(setup: Any) -> None:
    matcher, project, _ = setup
    items = [expense(matcher, project, 'One rail original', '12'), expense(matcher, project, 'Another rail original', '25')]
    result = change(matcher, project, items, 'transport', '火车、飞机')
    assert [item['title'] for item in result['items']] == ['One rail original', 'Another rail original']
    assert all(item['project_name'] == item['display_name'] == '火车、飞机' for item in result['items'])
    assert all(item['category'] == '高铁' and item['report_section'] == 'rail' for item in result['items'])
    assert all(item['material_basis'] == 'invoice_payment' for item in result['items'])


@pytest.mark.parametrize('stored_name', [None, '', '  '])
def test_missing_name_falls_back_to_original_title_without_filling_pending_amount(setup: Any, stored_name: Any) -> None:
    matcher, project, _ = setup
    item = matcher.store.create({'title': 'Unpriced original material', 'project_id': project, 'category': '其他', 'amount': None})
    item = write_raw(matcher, item, project_name=stored_name)
    result = change(matcher, project, [item])['items'][0]
    assert result['title'] == result['project_name'] == 'Unpriced original material'
    assert result['amount_pending'] and result['amount_cents'] == 0 and result['amount'] == item['amount']
    assert result['expense_date'] == item['expense_date'] and result['material_basis'] == item['material_basis']


def test_unchanged_type_is_a_true_noop_with_no_events_or_new_versions(setup: Any) -> None:
    matcher, project, _ = setup
    item = change(matcher, project, [expense(matcher, project, 'Same selected type')])['items'][0]
    before = snapshots(matcher)
    result = change(matcher, project, [item])
    assert result['changed'] == 0 and result['items'] == [item] and snapshots(matcher) == before


@pytest.mark.parametrize('invalid', ['empty', 'duplicate', 'missing_version', 'extra_version', 'empty_version',
                                   'wrong_version', 'wrong_ids', 'bad_id', 'over_limit'])
def test_requires_complete_nonduplicate_selection_and_versions(setup: Any, invalid: str) -> None:
    matcher, project, _ = setup
    items = [expense(matcher, project, 'First selected'), expense(matcher, project, 'Second selected')]
    ids: Any = [item['id'] for item in items]
    expected: dict[str, Any] = versions(items)
    if invalid == 'empty':
        ids, expected = [], {}
    elif invalid == 'duplicate':
        ids = [items[0]['id']] * 2
        expected = {items[0]['id']: items[0]['version']}
    elif invalid == 'missing_version':
        expected.pop(items[-1]['id'])
    elif invalid == 'extra_version':
        expected['unselected'] = 'version'
    elif invalid == 'empty_version':
        expected[items[-1]['id']] = ''
    elif invalid == 'wrong_version':
        expected[items[-1]['id']] = 1
    elif invalid == 'wrong_ids':
        ids = tuple(ids)
    elif invalid == 'bad_id':
        ids[-1] = 3
    else:
        ids = [str(index) for index in range(201)]
        expected = dict.fromkeys(ids, 'version')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='1至200'):
        ExpenseReview(matcher.store).batch_project_type(project, ids, expected, 'materials')
    assert snapshots(matcher) == before


@pytest.mark.parametrize('invalid', ['stale', 'unknown', 'foreign', 'verified', 'submitted', 'reimbursed',
                                   'cancelled', 'deleted', 'merged', 'legacy', 'history', 'archived', 'material_deleted', 'held'])
def test_any_invalid_later_fee_rejects_whole_batch_without_changes(setup: Any, invalid: str) -> None:
    matcher, project, foreign = setup
    first = expense(matcher, project, 'Valid first')
    second = expense(matcher, foreign if invalid == 'foreign' else project, 'Invalid later')
    if invalid == 'held':
        second = hotel_withdrawal(matcher, project)
    elif invalid not in {'stale', 'unknown', 'foreign'}:
        fields = ({'stage': invalid} if invalid in {'submitted', 'reimbursed', 'cancelled'} else
                  {'verified': True} if invalid == 'verified' else
                  {'merged_into': first['id']} if invalid == 'merged' else
                  {'history_imported': True} if invalid == 'history' else
                  {'synthesis_material_state': 'deleted' if invalid == 'material_deleted' else 'archived'}
                  if invalid in {'archived', 'material_deleted'} else {invalid: True})
        second = write_raw(matcher, second, **fields)
    ids = [first['id'], second['id']]
    expected = versions([first, second])
    if invalid == 'stale':
        matcher.store.update(second['id'], {'note': 'Later manual edit'})
    elif invalid == 'unknown':
        expected['unknown-id'] = expected.pop(ids[-1])
        ids[-1] = 'unknown-id'
    before = snapshots(matcher)
    with pytest.raises((ValueError, KeyError)):
        ExpenseReview(matcher.store).batch_project_type(project, ids, expected, 'materials')
    assert snapshots(matcher) == before


@pytest.mark.parametrize('kind,name', [('invalid', None), ('living', None), ('living', ''),
                                     ('transport', 'Arbitrary old name'), ('materials', 'One unified name'), ('other', '')])
def test_fixed_detail_must_be_valid_and_editable_type_cannot_unify_names(setup: Any, kind: str, name: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Original expense')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        change(matcher, project, [item], kind, name)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('invalid_name', ['x' * 201, ['not a name'], ''])
def test_any_invalid_member_name_is_not_truncated_or_replaced(setup: Any, invalid_name: Any) -> None:
    matcher, project, _ = setup
    first = expense(matcher, project, 'Valid name')
    second = expense(matcher, project, 'Later invalid name')
    second = write_raw(matcher, second, project_name=invalid_name, title='' if invalid_name == '' else second['title'])
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        change(matcher, project, [first, second])
    assert snapshots(matcher) == before


@pytest.mark.parametrize('project_id', ['', None, 'unknown-project'])
def test_requires_explicit_existing_project(setup: Any, project_id: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Current project fee')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        change(matcher, project_id, [item])
    assert snapshots(matcher) == before


def test_accepts_exactly_two_hundred_fees_without_creating_or_combining_fees(setup: Any) -> None:
    matcher, project, _ = setup
    items = [matcher.store.create({'title': f'Separate original {index}', 'project_id': project,
                                  'category': '其他', 'amount': '1'}) for index in range(200)]
    result = change(matcher, project, items)
    assert result['changed'] == 200 and len(result['items']) == len(matcher.store.list_items()) == 200
    assert sum(item['amount_cents'] for item in result['items']) == 20000
    assert len({item['project_name'] for item in result['items']}) == 200


@pytest.mark.parametrize('legacy_snapshot', [False, True])
def test_pristine_single_match_undo_keeps_changed_type_and_original_title(setup: Any, legacy_snapshot: bool) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Unchanged original invoice')
    payment = queued(matcher, project, 'Matched payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment))
    if legacy_snapshot:
        with matcher.store.connect() as db:
            group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (result['match']['id'],)).fetchone()['data'])
            group.pop('aggregate_after')
            group.pop('queue_after')
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group), group['id']))
    edited = change(matcher, project, [result['item']])['items'][0]
    restored = matcher.undo(result['match']['id'], edited['version'])['item']
    assert restored['project_type'] == 'materials' and restored['project_name'] == restored['title'] == item['title']
    assert restored['returned_to_synthesis'] and not restored['pending_review'] and restored['amount_cents'] == 1400
    assert matcher.intake.get(payment['id'])['status'] == 'review'


def test_reused_registration_undo_preserves_new_fixed_type_and_original_title(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Original pre-registration title')
    result = confirmed_creation(matcher, ['e:' + item['id']], [], project,
                                expected_versions={item['id']: item['version']},
                                fields={'project_type': 'other', 'project_name': 'Confirmed registration name'})
    edited = change(matcher, project, [result['item']], 'living', '现场饮用水、夜宵')['items'][0]
    restored = matcher.undo(result['match']['id'], edited['version'])['item']
    assert restored['project_type'] == 'living' and restored['project_name'] == '现场饮用水、夜宵'
    assert restored['title'] == item['title'] and restored['amount_cents'] == item['amount_cents']
    assert restored['attachments'] == item['attachments'] and restored['returned_to_synthesis']


def test_aggregate_undo_restores_each_member_with_own_name_and_new_type(setup: Any) -> None:
    matcher, project, _ = setup
    members = [expense(matcher, project, 'Ten original', '10'), expense(matcher, project, 'Four original', '4')]
    payment = queued(matcher, project, 'Combined payment', 'payment', '14')
    result = matcher.match(['e:' + item['id'] for item in members], [payment['id']], project,
                           expected_versions=versions(members), revisions=revisions(payment))
    edited = change(matcher, project, [result['item']])['items'][0]
    matcher.undo(result['match']['id'], edited['version'])
    restored = [matcher.store.get(item['id']) for item in members]
    assert [item['amount_cents'] for item in restored] == [1000, 400]
    assert [item['title'] for item in restored] == ['Ten original', 'Four original']
    assert [item['project_name'] for item in restored] == ['Ten original', 'Four original']
    assert all(item['project_type'] == 'materials' and item['returned_to_synthesis'] for item in restored)


def test_prior_manual_content_change_still_blocks_undo_after_bulk_type_change(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Later edited invoice')
    payment = queued(matcher, project, 'Later edited payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment))
    manual = matcher.store.edit_review_fields(item['id'], result['item']['version'], 'other', 'Later manual amount', '20')
    edited = change(matcher, project, [manual])['items'][0]
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], edited['version'])
    assert snapshots(matcher) == before and matcher.store.get(item['id'])['amount_cents'] == 2000


def test_later_group_corruption_is_validated_before_any_first_fee_write(setup: Any) -> None:
    matcher, project, _ = setup
    first = expense(matcher, project, 'First valid fee')
    second = expense(matcher, project, 'Later missing group')
    second = write_raw(matcher, second, material_match={'group_id': 'nonexistent-group'})
    with matcher.store.connect() as db:
        db.execute("CREATE TRIGGER prohibit_first_write BEFORE UPDATE ON expenses WHEN OLD.id='" + first['id'] +
                   "' BEGIN SELECT RAISE(ABORT,'Should validate before writing'); END")
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='材料组合不存在'):
        change(matcher, project, [first, second])
    assert snapshots(matcher) == before


def test_actual_second_sql_failure_rolls_back_expenses_events_and_match_snapshot(setup: Any) -> None:
    matcher, project, _ = setup
    first = expense(matcher, project, 'First matched fee')
    payment = queued(matcher, project, 'First matched proof', 'payment', '14')
    first = matcher.match('e:' + first['id'], [payment['id']], project,
                          expected_version=first['version'], revisions=revisions(payment))['item']
    second = expense(matcher, project, 'Second failing fee')
    with matcher.store.connect() as db:
        db.execute("CREATE TRIGGER fail_second_type BEFORE UPDATE ON expenses WHEN OLD.id='" + second['id'] +
                   "' BEGIN SELECT RAISE(ABORT,'Synthetic second type failure'); END")
    before = snapshots(matcher)
    with pytest.raises(sqlite3.IntegrityError, match='second type failure'):
        change(matcher, project, [first, second])
    assert snapshots(matcher) == before


def test_competing_requests_cannot_apply_an_outdated_batch_twice(setup: Any) -> None:
    matcher, project, _ = setup
    items = [expense(matcher, project, 'Concurrent first'), expense(matcher, project, 'Concurrent second')]
    barrier = threading.Barrier(2)
    def submit() -> str:
        barrier.wait(timeout=5)
        try:
            change(matcher, project, items)
            return 'changed'
        except ValueError as error:
            assert '已变化' in str(error)
            return 'stale'
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(outcomes) == ['changed', 'stale']
    with matcher.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE message LIKE '批量修改报销项目类型：%'").fetchone()[0] == 2


def test_authenticated_http_contract_preserves_fields_and_rejects_stale_foreign_requests(attribution_web: Any) -> None:
    store, project, foreign, first, _, post = attribution_web
    second = store.create({'project_id': project, 'title': 'Another distinct title', 'category': '其他', 'amount': None})
    items = [store.get(first['id']), second]
    body = {'project_id': project, 'ids': [item['id'] for item in items], 'versions': versions(items), 'project_type': 'materials'}
    with pytest.raises(HTTPError) as forbidden:
        post('review/batch-project-type', body, credential='invalid')
    assert forbidden.value.code == 403
    with pytest.raises(HTTPError) as wrong_project:
        post('review/batch-project-type', {**body, 'project_id': foreign})
    assert wrong_project.value.code == 400
    result = post('review/batch-project-type', body)
    assert result['changed'] == 2 and len(result['items']) == 2
    assert [item['title'] for item in result['items']] == [first['title'], second['title']]
    assert result['items'][1]['amount_pending'] and result['items'][1]['amount_cents'] == 0
    with pytest.raises(HTTPError) as stale:
        post('review/batch-project-type', body)
    assert stale.value.code == 400
    assert store.get(first['id']) == result['items'][0] and store.get(second['id']) == result['items'][1]
