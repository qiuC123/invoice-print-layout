from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.synthesis_expenses import SynthesisExpenses, expense_archive_capabilities
from tests.test_material_creation import confirmed_creation
from tests.test_material_matching import expense, proof, queued, revisions, setup, snapshots


def raw(matcher: MaterialMatching, item_id: str) -> dict[str, Any]:
    with matcher.store.connect() as db:
        return dict(json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data']))


def patch(matcher: MaterialMatching, item_id: str, **changes: Any) -> dict[str, Any]:
    with matcher.store.connect() as db:
        data = raw(matcher, item_id)
        data.update(changes)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item_id))
    return matcher.store.get(item_id)


def retained(matcher: MaterialMatching, project: str, *, old: bool = False) -> dict[str, Any]:
    voucher = queued(matcher, project, 'Retained voucher', 'invoice', '14')
    payment = queued(matcher, project, 'Released payment', 'payment', '14')
    matched = matcher.match('q:' + voucher['id'], [payment['id']], project, revisions=revisions(voucher, payment))
    item = matcher.undo(matched['match']['id'], matched['item']['version'])['item']
    if old:
        with matcher.store.connect() as db:
            data = raw(matcher, item['id'])
            data.pop('returned_to_synthesis')
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
        item = matcher.store.get(item['id'])
    return dict(item)


def originals(matcher: MaterialMatching) -> dict[str, bytes]:
    with matcher.store.connect() as db:
        paths = {row['path'] for row in db.execute('SELECT path FROM attachments')}
        paths.update(json.loads(row['data'])['path'] for row in db.execute('SELECT data FROM intake_queue'))
    return {path: Path(path).read_bytes() for path in paths}


@pytest.mark.parametrize('old', [False, True])
def test_retained_edit_keeps_source_queue_and_originals_and_materializes_old_state(setup: Any, old: bool) -> None:
    matcher, project, _ = setup
    item = retained(matcher, project, old=old)
    before = snapshots(matcher)
    files = originals(matcher)
    old_data = raw(matcher, item['id'])
    result = SynthesisExpenses(matcher.store).edit_fields(item['id'], item['version'], project, '15.40', '  打印  ')
    edited = result['item']
    assert edited['id'] == item['id'] and edited['title'] == edited['display_name'] == '打印'
    assert edited['amount_cents'] == 1540 and not edited['amount_pending'] and not edited['verified']
    assert edited['returned_to_synthesis'] and not edited['ready'] and not edited['pending_review']
    assert edited['version'] != item['version'] and result['message']
    assert raw(matcher, item['id'])['returned_to_synthesis'] is True
    changed = {'title', 'display_name', 'amount_cents', 'amount_pending', 'verified', 'returned_to_synthesis', 'updated_at'}
    assert {key: value for key, value in old_data.items() if key not in changed} == {
        key: value for key, value in raw(matcher, item['id']).items() if key not in changed}
    after = snapshots(matcher)
    assert before['attachments'] == after['attachments'] and before['intake_queue'] == after['intake_queue']
    assert before['material_matches'] == after['material_matches'] and len(after['expenses']) == len(before['expenses']) == 1
    assert before['expenses'][0][1] == after['expenses'][0][1]
    assert originals(matcher) == files
    assert matcher.store.get(item['id'])['returned_to_synthesis'], 'The edit event must not undo the historical projection'


@pytest.mark.parametrize('title', [None, '', '   '])
def test_omitted_name_and_zero_amount_preserve_human_title_and_classification(setup: Any, title: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Already chosen human name')
    item = patch(matcher, item['id'], amount_pending=True, project_type='materials', project_name='Already chosen human name',
                 display_name='Already chosen human name', merchant='Evidenced seller', invoice_number='12345678901234567890')
    result = SynthesisExpenses(matcher.store).edit_fields(item['id'], item['version'], project, '0', title)['item']
    assert result['amount_cents'] == 0 and not result['amount_pending']
    for field in ('title', 'display_name', 'project_type', 'project_name', 'category', 'merchant', 'invoice_number'):
        assert result[field] == item[field]
    assert not result['returned_to_synthesis'] and not result['complete']


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_archive_restore_delete_cycle_supports_withdrawal_schemas_without_releasing_invoice_identity(setup: Any, schema: int) -> None:
    matcher, project, _ = setup
    if schema == 1:
        item = retained(matcher, project, old=True)
    elif schema == 2:
        first, second = expense(matcher, project, 'First member'), expense(matcher, project, 'Second member')
        payment = queued(matcher, project, 'Merged payment', 'payment', '28')
        matched = matcher.match(['e:' + first['id'], 'e:' + second['id']], [payment['id']], project,
                                expected_versions={first['id']: first['version'], second['id']: second['version']},
                                revisions=revisions(payment))
        matcher.undo(matched['match']['id'], matched['item']['version'])
        item = matcher.store.get(first['id'])
    else:
        initial = expense(matcher, project, 'Registration member')
        matched = confirmed_creation(matcher, ['e:' + initial['id']], [], project,
                                     expected_versions={initial['id']: initial['version']})
        item = matcher.undo(matched['match']['id'], matched['item']['version'])['item']
    before = snapshots(matcher)
    files = originals(matcher)
    helper = SynthesisExpenses(matcher.store)
    for action, expected in [('archive', 'archived'), ('delete', 'deleted'),
                             ('restore-deleted', 'archived'), ('restore', '')]:
        assert expense_archive_capabilities(item)[{
            'archive': 'can_archive', 'delete': 'can_delete', 'restore-deleted': 'can_restore_deleted',
            'restore': 'can_restore'}[action]]
        item = helper.transition(item['id'], item['version'], project, action)['item']
        assert item.get('synthesis_material_state', '') == expected and item['returned_to_synthesis']
        assert not item['deleted'] and not item['verified'] and not item['pending_review'] and not item['ready']
    after = snapshots(matcher)
    assert before['attachments'] == after['attachments'] and before['intake_queue'] == after['intake_queue']
    assert before['material_matches'] == after['material_matches'] and len(before['expenses']) == len(after['expenses'])
    assert [row[:2] for row in before['expenses']] == [row[:2] for row in after['expenses']]
    assert originals(matcher) == files and expense_archive_capabilities(item)['can_archive']


def test_incomplete_draft_can_archive_but_only_returns_as_held_material(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Ordinary incomplete invoice')
    matcher.store.request_review([item['id']], {item['id']: item['version']})
    item = matcher.store.get(item['id'])
    assert item['pending_review'] and not item['returned_to_synthesis']
    helper = SynthesisExpenses(matcher.store)
    archived = helper.transition(item['id'], item['version'], project, 'archive')['item']
    assert archived['synthesis_material_state'] == 'archived' and not archived['pending_review']
    restored = helper.transition(item['id'], archived['version'], project, 'restore')['item']
    assert restored['returned_to_synthesis'] and not restored['pending_review'] and not restored['deleted']


@pytest.mark.parametrize('invalid', ['missing_id', 'missing_version', 'missing_project', 'stale', 'other_project',
                                   'empty_amount', 'negative', 'too_precise', 'nan', 'too_large', 'bad_title', 'long_title',
                                   'verified', 'submitted', 'cancelled', 'deleted', 'merged', 'legacy', 'history', 'no_original',
                                   'archived', 'material_deleted', 'complete'])
def test_invalid_edits_fail_without_changes(setup: Any, invalid: str) -> None:
    matcher, project, other = setup
    item = expense(matcher, project, 'Validation invoice')
    field_changes = {'verified': {'verified': True}, 'submitted': {'stage': 'submitted'}, 'cancelled': {'stage': 'cancelled'},
                     'deleted': {'deleted': True}, 'merged': {'merged_into': 'other'}, 'legacy': {'legacy': True},
                     'history': {'history_imported': True}, 'archived': {'synthesis_material_state': 'archived', 'returned_to_synthesis': True},
                     'material_deleted': {'synthesis_material_state': 'deleted', 'returned_to_synthesis': True}}
    if invalid in field_changes:
        item = patch(matcher, item['id'], **field_changes[invalid])
    elif invalid == 'no_original':
        path, _ = matcher.store.attachment_path(item['attachments'][0]['id'])
        path.unlink()
        item = matcher.store.get(item['id'])
    elif invalid == 'complete':
        item = matcher.store.add_attachment(item['id'], 'payment.pdf', proof('Complete payment'), 'payment')
    args: dict[str, Any] = {'item_id': item['id'], 'expected_version': item['version'], 'project_id': project,
                            'amount': '14', 'title': 'Edited'}
    changes = {'missing_id': {'item_id': ''}, 'missing_version': {'expected_version': ''}, 'missing_project': {'project_id': ''},
               'stale': {'expected_version': 'old'}, 'other_project': {'project_id': other}, 'empty_amount': {'amount': ''},
               'negative': {'amount': '-1'}, 'too_precise': {'amount': '1.001'}, 'nan': {'amount': 'NaN'},
               'too_large': {'amount': '1000000000'}, 'bad_title': {'title': {}}, 'long_title': {'title': 'x' * 201}}
    args.update(changes.get(invalid, {}))
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        SynthesisExpenses(matcher.store).edit_fields(**args)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('operation', ['archive', 'restore', 'delete', 'restore-deleted'])
def test_active_group_cannot_be_archived_or_deleted_even_if_material_marker_is_missing(setup: Any, operation: str) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Still linked invoice', 'invoice', '14')
    matched = confirmed_creation(matcher, ['q:' + voucher['id']], [], project, revisions=revisions(voucher))
    item = matched['item']
    if operation != 'archive':
        state = 'deleted' if operation == 'restore-deleted' else 'archived'
        item = patch(matcher, item['id'], synthesis_material_state=state, returned_to_synthesis=True)
    with matcher.store.connect() as db:
        data = raw(matcher, item['id'])
        data.pop('material_match')
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    item = matcher.store.get(item['id'])
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='已有组合'):
        SynthesisExpenses(matcher.store).transition(item['id'], item['version'], project, operation)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('invalid', ['wrong_project', 'stale', 'direct_delete', 'already_archived', 'invalid_action'])
def test_invalid_transitions_fail_atomically(setup: Any, invalid: str) -> None:
    matcher, project, other = setup
    item = retained(matcher, project)
    helper = SynthesisExpenses(matcher.store)
    if invalid == 'already_archived':
        item = helper.transition(item['id'], item['version'], project, 'archive')['item']
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        helper.transition(item['id'], 'stale' if invalid == 'stale' else item['version'],
                          other if invalid == 'wrong_project' else project,
                          'delete' if invalid == 'direct_delete' else 'missing' if invalid == 'invalid_action' else 'archive')
    assert snapshots(matcher) == before


@pytest.mark.parametrize('old_group', [False, True])
def test_edit_incomplete_active_group_prevents_undo_overwriting_manual_fields(setup: Any, old_group: bool) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Incomplete active invoice', 'invoice', '14')
    matched = confirmed_creation(matcher, ['q:' + voucher['id']], [], project, revisions=revisions(voucher))
    if old_group:
        with matcher.store.connect() as db:
            group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (matched['match']['id'],)).fetchone()['data'])
            group.pop('aggregate_after')
            group.pop('queue_after')
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group), group['id']))
    item = matcher.store.get(matched['item']['id'])
    assert expense_archive_capabilities(item)['can_edit'] and not expense_archive_capabilities(item)['can_archive']
    edited = SynthesisExpenses(matcher.store).edit_fields(item['id'], item['version'], project, '15', 'Human correction')['item']
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.undo(matched['match']['id'], edited['version'])
    assert snapshots(matcher) == before and matcher.store.get(item['id'])['title'] == 'Human correction'
