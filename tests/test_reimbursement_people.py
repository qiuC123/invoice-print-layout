"""Attribution keeps one project report and never implies money was paid."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.reimbursement_people import (ReimbursementPeople, allocation_items,
    person_allocation, validate_person, current_person_label)
from invoice_print_layout.synthesis_expenses import SynthesisExpenses
from tests.test_amount_pairing import material
from tests.test_material_matching import setup, queued, revisions, expense, snapshots, proof, auto_queued


def people(matcher: Any, project: str) -> tuple[ReimbursementPeople, dict[str, Any], dict[str, Any]]:
    domain = ReimbursementPeople(matcher.store)
    return domain, domain.create(project, 'Synthetic A', 'sender-a')['person'], domain.create(project, 'Synthetic B', 'sender-b')['person']


def assign(domain: ReimbursementPeople, project: str, person: dict[str, Any], *entries: dict[str, Any]) -> list[dict[str, Any]]:
    return domain.assign_materials(project, [entry['id'] for entry in entries], revisions(*entries), person['id'])['entries']


def create(matcher: Any, project: str, entries: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    vouchers = ['q:' + entry['id'] for entry in entries if entry['role'] in {'invoice', 'receipt'}]
    payments = [entry['id'] for entry in entries if entry['role'] == 'payment']
    return matcher.create_expense(vouchers, payments, project, revisions=revisions(*entries),
        fields={'project_type': 'other', 'project_name': '合成测试费用', 'amount': '14', **fields})


def test_registry_stable_external_identity_and_explicit_creation(setup: Any) -> None:
    matcher, project, other = setup
    domain = ReimbursementPeople(matcher.store)
    original = domain.create(project, '昵称', 'sender-stable', '备注')['person']
    repeat = domain.create(project, '新显示名', 'sender-stable')
    assert not repeat['created'] and repeat['person']['id'] == original['id']
    assert domain.create(other, '昵称', 'sender-stable')['person']['id'] != original['id']
    with pytest.raises(ValueError, match='来源身份不同'):
        domain.create(project, '昵称', 'different-sender')
    assert domain.list(project) == [original]
    assert domain.summary(project)['total_cents'] == 0 and not matcher.store.list_items()
    with matcher.store.connect() as db:
        assert validate_person(db, project, original['id'])['reimbursement_person_label'] == '昵称'
        assert current_person_label(db, project, original['id']) == '昵称'
        with pytest.raises(ValueError, match='不属于'):
            validate_person(db, other, original['id'])


def test_material_assign_is_atomic_versioned_and_preserves_sources_amounts(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    entries = [material(matcher, project, 'invoice', 'invoice', '14'), material(matcher, project, 'payment', 'payment', '14')]
    entries[0]['wechat_source'] = {'sender_id': 'original-sender', 'nickname': 'Original nickname'}
    with matcher.store.connect() as db:
        matcher._write_queue(db, entries[0])
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='已变化'):
        domain.assign_materials(project, [entry['id'] for entry in entries], {entries[0]['id']: 1, entries[1]['id']: 99}, person['id'])
    assert snapshots(matcher) == before
    assigned = assign(domain, project, person, *entries)
    assert all(entry['reimbursement_person_id'] == person['id'] and entry['revision'] == 2 and entry['status'] == 'review' for entry in assigned)
    assert all(new['fields'] == old['fields'] and new['digest'] == old['digest'] and Path(new['path']).read_bytes() == Path(old['path']).read_bytes()
               for old, new in zip(entries, assigned))
    assert assigned[0]['wechat_source'] == entries[0]['wechat_source']
    with matcher.store.connect() as db:
        messages = [row['message'] for row in db.execute('SELECT message FROM events ORDER BY id')]
    assert all('未指定 → Synthetic A' in message for message in messages)
    assert not matcher.store.list_items()


def test_material_assign_project_boundaries_linked_and_foreign_person(setup: Any) -> None:
    matcher, project, other = setup
    domain, person, _ = people(matcher, project)
    unassigned = material(matcher, '', 'unassigned', 'invoice', '14')
    assert assign(domain, project, person, unassigned)[0]['project_id'] == project
    foreign = material(matcher, other, 'foreign', 'invoice', '14')
    with pytest.raises(ValueError, match='不属于'):
        assign(domain, project, person, foreign)
    local = material(matcher, project, 'local', 'invoice', '14')
    with pytest.raises(ValueError, match='费用归属人不属于'):
        domain.assign_materials(project, [local['id']], revisions(local), domain.create(other, '其他人员')['person']['id'])
    local.update(item_id='consumed', status='linked')
    with matcher.store.connect() as db:
        matcher._write_queue(db, local)
    with pytest.raises(ValueError, match='未关联'):
        assign(domain, project, person, local)


def test_registration_inherits_one_owner_and_undo_retains_attribution(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    entries = assign(domain, project, person, material(matcher, project, 'invoice', 'invoice', '14'),
                     material(matcher, project, 'payment', 'payment', '14'))
    created = create(matcher, project, entries)
    item = created['item']
    assert item['reimbursement_person_id'] == person['id'] and item['amount_cents'] == 1400
    assert not item['verified'] and item['stage'] == 'draft'
    assert domain.summary(project)['total_cents'] == 1400
    matcher.undo(created['match']['id'], item['version'])
    assert all(matcher.intake.get(entry['id'])['reimbursement_person_id'] == person['id'] for entry in entries)
    assert domain.summary(project)['total_cents'] == 0


@pytest.mark.parametrize('unknown', [False, True])
@pytest.mark.parametrize('mode', ['registration', 'matching'])
def test_cross_person_or_known_unknown_never_silently_merge(setup: Any, unknown: bool, mode: str) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    voucher = assign(domain, project, person, material(matcher, project, 'invoice', 'invoice', '14'))[0]
    payment = material(matcher, project, 'payment', 'payment', '14')
    if not unknown:
        payment = assign(domain, project, second, payment)[0]
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='费用归属人不同'):
        if mode == 'registration':
            create(matcher, project, [voucher, payment])
        else:
            matcher.match('q:' + voucher['id'], [payment['id']], project, revisions=revisions(voucher, payment))
    assert snapshots(matcher) == before


def test_explicit_create_owner_unifies_sources_atomically_and_survives_undo(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    voucher = assign(domain, project, person, material(matcher, project, 'invoice', 'invoice', '14'))[0]
    payment = assign(domain, project, second, material(matcher, project, 'payment', 'payment', '14'))[0]
    result = create(matcher, project, [voucher, payment], reimbursement_person_id=second['id'])
    assert result['item']['reimbursement_person_id'] == second['id']
    matcher.undo(result['match']['id'], result['item']['version'])
    assert all(matcher.intake.get(entry['id'])['reimbursement_person_id'] == second['id'] for entry in [voucher, payment])


def test_explicit_foreign_owner_and_stale_source_cannot_mutate_or_create(setup: Any) -> None:
    matcher, project, other = setup
    domain, person, _ = people(matcher, project)
    voucher = material(matcher, project, 'invoice', 'invoice', '14')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='费用归属人不属于'):
        create(matcher, project, [voucher], reimbursement_person_id=domain.create(other, '其他人')['person']['id'])
    assert snapshots(matcher) == before
    assign(domain, project, person, voucher)
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='版本|变化'):
        create(matcher, project, [voucher], reimbursement_person_id=person['id'])
    assert snapshots(matcher) == before


def test_same_amount_different_people_each_have_one_unique_batch_group(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    for index, owner in enumerate([person, second]):
        assign(domain, project, owner, material(matcher, project, f'invoice-{index}', 'invoice', '14'),
               material(matcher, project, f'payment-{index}', 'payment', '14'))
    preview = matcher.amount_preview(project)
    assert len(preview['groups']) == 2
    assert {group['reimbursement_person_id'] for group in preview['groups']} == {person['id'], second['id']}
    result = matcher.amount_confirm(project, preview['snapshot_token'], 'people-batch-unique-001', True)
    assert len(result['items']) == 2
    summary = domain.summary(project)
    assert summary['total_cents'] == 2800
    assert {row['total_cents'] for row in summary['rows'] if row['person_id']} == {1400}


@pytest.mark.parametrize('unknown_amounts', [['14'], ['8', '6']])
def test_unknown_amount_competitor_is_not_hidden_by_person_partition(setup: Any, unknown_amounts: list[str]) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    assign(domain, project, person, material(matcher, project, 'invoice', 'invoice', '14'),
           material(matcher, project, 'owned-payment', 'payment', '14'))
    for index, amount in enumerate(unknown_amounts):
        material(matcher, project, f'unknown-{index}', 'payment', amount)
    preview = matcher.amount_preview(project)
    assert not preview['groups'] and preview['skipped']
    assert any('费用归属人' in row['reason'] or '歧义' in row['reason'] for row in preview['skipped'])
    assert not matcher.store.list_items()


def test_rules_suggestions_only_same_owner_and_legacy_auto_does_not_cross_unknown(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    assign(domain, project, person, auto_queued(matcher, project, 'invoice', 'invoice', '14'),
           auto_queued(matcher, project, 'owned-payment', 'payment', '14'))
    auto_queued(matcher, project, 'unknown-payment', 'payment', '14')
    board = matcher.board(project)
    assert len(board['suggestions']) == 1 and board['suggestions'][0]['reimbursement_person_id'] == person['id']
    assert board['suggestions'][0]['ambiguous']
    assert matcher.auto_match(project)['matched'] == 0


def test_assign_active_fee_rebases_only_owner_and_undo_keeps_latest_person(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    entries = assign(domain, project, person, queued(matcher, project, 'invoice', 'invoice', '14'),
                     queued(matcher, project, 'payment', 'payment', '14'))
    result = matcher.match('q:' + entries[0]['id'], [entries[1]['id']], project, revisions=revisions(*entries))
    original = result['item']
    changed = domain.assign_expense(original['id'], original['version'], second['id'])['item']
    assert 'Synthetic A → Synthetic B' in changed['events'][0]['message']
    assert changed['amount_cents'] == original['amount_cents'] and changed['category'] == original['category']
    assert changed.get('project_type') == original.get('project_type') and changed['stage'] == original['stage'] and not changed['verified']
    matcher.undo(result['match']['id'], changed['version'])
    assert all(matcher.intake.get(entry['id'])['reimbursement_person_id'] == second['id'] for entry in entries)
    assert matcher.store.get(original['id'])['reimbursement_person_id'] == second['id']


def test_person_edit_preserves_prior_content_guard_and_returned_projection(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    entries = [queued(matcher, project, 'invoice', 'invoice', '14'), queued(matcher, project, 'payment', 'payment', '14')]
    result = matcher.match('q:' + entries[0]['id'], [entries[1]['id']], project, revisions=revisions(*entries))
    item = result['item']
    modified = matcher.store.update(item['id'], {**item, 'amount': '15'})
    changed = domain.assign_expense(item['id'], modified['version'], person['id'])['item']
    with pytest.raises(ValueError, match='后续内容'):
        matcher.undo(result['match']['id'], changed['version'])
    assert matcher.store.get(item['id'])['amount_cents'] == 1500


@pytest.mark.parametrize('stage', ['submitted', 'reimbursed', 'cancelled'])
def test_assign_expense_rejects_non_draft_without_changes(setup: Any, stage: str) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    item = expense(matcher, project, 'invoice')
    with matcher.store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        data['stage'] = stage
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    item = matcher.store.get(item['id'])
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='草稿'):
        domain.assign_expense(item['id'], item['version'], person['id'])
    assert snapshots(matcher) == before


def test_legacy_resolve_inherits_owner_and_append_rejects_other_owner(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    voucher = assign(domain, project, person, queued(matcher, project, 'invoice', 'invoice', '14'))[0]
    resolved = matcher.intake.resolve(voucher['id'], voucher['revision'], voucher['fields'], 'invoice')
    item = matcher.store.get(resolved['item_id'])
    assert item['reimbursement_person_id'] == person['id']
    payment = assign(domain, project, second, queued(matcher, project, 'payment', 'payment', '14'))[0]
    with pytest.raises(ValueError, match='费用归属人不同'):
        matcher.intake.resolve(payment['id'], payment['revision'], payment['fields'], 'payment', item['id'], item['version'])


def test_duplicate_merge_rejects_people_mixture_and_same_person_counts_once(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    left = expense(matcher, project, 'left')
    right = expense(matcher, project, 'right')
    left = domain.assign_expense(left['id'], left['version'], person['id'])['item']
    right = domain.assign_expense(right['id'], right['version'], second['id'])['item']
    review = ExpenseReview(matcher.store)
    with pytest.raises(ValueError, match='费用归属人不同'):
        review.merge_preview([left['id'], right['id']], left['id'])
    right = domain.assign_expense(right['id'], right['version'], person['id'])['item']
    preview = review.merge_preview([left['id'], right['id']], left['id'])
    review.merge(preview['ids'], preview['target'], preview['token'], 'synthetic duplicate confirmed')
    assert domain.summary(project)['total_cents'] == 1400


def test_pure_allocation_disjoint_stage_totals_duplicate_ids_and_inactive_exclusion() -> None:
    base = {'id': 'one', 'stage': 'draft', 'amount_cents': 1400, 'reimbursement_person_id': 'a', 'reimbursement_person_label': 'A'}
    items = [base, dict(base), {**base, 'id': 'ready', 'amount_cents': 200, 'ready': True},
             {**base, 'id': 'package', 'stage': 'submitted', 'amount_cents': 300},
             {**base, 'id': 'unknown', 'reimbursement_person_id': '', 'reimbursement_person_label': 'unused malicious label', 'amount_cents': 100}]
    items.extend({**base, 'id': field, field: value} for field, value in [('deleted', True), ('merged_into', 'one'),
        ('returned_to_synthesis', True), ('synthesis_material_state', 'archived'), ('stage', 'cancelled')])
    result = person_allocation(items)
    assert result['total_cents'] == 2000 and result['expense_count'] == 4 and len(allocation_items(items)) == 4
    assert result['rows'][0]['pending_review_cents'] == 1400 and result['rows'][0]['ready_cents'] == 200 and result['rows'][0]['packaged_cents'] == 300
    assert result['rows'][1]['label'] == '未指定' and result['rows'][1]['total_cents'] == 100
    assert sum(row['pending_review_cents'] + row['ready_cents'] + row['packaged_cents'] for row in result['rows']) == result['total_cents']


def test_qr_replacement_inherits_person_and_existing_other_person_blocks(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    source = assign(domain, project, person, queued(matcher, project, 'QR source', 'other', None))[0]
    payload = proof('downloaded invoice')
    target = matcher.intake.receive('download.pdf', payload, project_id=project, expected_source=source)
    assert target['reimbursement_person_id'] == person['id'] and matcher.intake.get(source['id'])['reimbursement_person_id'] == person['id']
    other_source = assign(domain, project, second, queued(matcher, project, 'other QR source', 'other', None))[0]
    with pytest.raises(ValueError, match='费用归属人不同'):
        matcher.intake.receive('download.pdf', payload, project_id=project, expected_source=other_source)


def test_invalid_project_never_creates_registry_or_assigns_materials(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    local = queued(matcher, project, 'local', 'invoice', '14')
    before = snapshots(matcher)
    for operation in [lambda: domain.create('fake-project', 'Invalid'), lambda: domain.list('fake-project'),
                      lambda: domain.assign_materials('fake-project', [local['id']], revisions(local), person['id'])]:
        with pytest.raises(ValueError, match='项目不存在'):
            operation()
    assert snapshots(matcher) == before
    with matcher.store.connect() as db:
        assert db.execute('SELECT count(*) FROM reimbursement_people WHERE project_id=?', ('fake-project',)).fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['registration', 'matching'])
def test_existing_expense_explicit_person_reassignment_updates_source_queue(setup: Any, mode: str) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    voucher = assign(domain, project, person, queued(matcher, project, 'invoice', 'invoice', '14'))[0]
    source = matcher.intake.resolve(voucher['id'], voucher['revision'], voucher['fields'], 'invoice')
    item = matcher.store.get(source['item_id'])
    payment = assign(domain, project, second, queued(matcher, project, 'payment', 'payment', '14'))[0]
    args = {'expected_versions': {item['id']: item['version']}, 'revisions': revisions(payment),
            'fields': {'reimbursement_person_id': second['id'], 'project_type': 'other', 'project_name': '合成测试费用', 'amount': '14'}}
    if mode == 'registration':
        result = matcher.create_expense(['e:' + item['id']], [payment['id']], project, **args)
    else:
        result = matcher.match(['e:' + item['id']], [payment['id']], project, **args)
    assert result['item']['id'] == item['id'] and result['item']['reimbursement_person_id'] == second['id']
    assert matcher.intake.get(source['id'])['reimbursement_person_id'] == second['id']
    changed = domain.assign_expense(item['id'], result['item']['version'], person['id'])['item']
    matcher.undo(result['match']['id'], changed['version'])
    assert matcher.intake.get(source['id'])['reimbursement_person_id'] == person['id']
    assert matcher.store.get(item['id'])['reimbursement_person_id'] == person['id']


def test_multiple_owned_expenses_form_one_fee_then_undo_restores_same_person(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, second = people(matcher, project)
    members = [expense(matcher, project, 'invoice-10', '10'), expense(matcher, project, 'invoice-4', '4')]
    members = [domain.assign_expense(item['id'], item['version'], person['id'])['item'] for item in members]
    payment = assign(domain, project, person, queued(matcher, project, 'payment', 'payment', '14'))[0]
    result = matcher.match(['e:' + item['id'] for item in members], [payment['id']], project,
        expected_versions={item['id']: item['version'] for item in members}, revisions=revisions(payment))
    assert result['item']['reimbursement_person_id'] == person['id'] and domain.summary(project)['total_cents'] == 1400
    changed = domain.assign_expense(result['item']['id'], result['item']['version'], second['id'])['item']
    matcher.undo(result['match']['id'], changed['version'])
    assert all(matcher.store.get(item['id'])['reimbursement_person_id'] == second['id'] for item in members)
    # Withdrawn original fees are retained as materials, not payable twice.
    assert domain.summary(project)['total_cents'] == 0


def test_amount_edit_archive_restore_keep_fee_person(setup: Any) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    item = expense(matcher, project, 'invoice', '14')
    item = domain.assign_expense(item['id'], item['version'], person['id'])['item']
    helper = SynthesisExpenses(matcher.store)
    item = helper.edit_fields(item['id'], item['version'], project, '15')['item']
    assert item['reimbursement_person_id'] == person['id']
    item = helper.transition(item['id'], item['version'], project, 'archive')['item']
    assert item['reimbursement_person_id'] == person['id'] and domain.summary(project)['total_cents'] == 0
    item = helper.transition(item['id'], item['version'], project, 'restore')['item']
    assert item['reimbursement_person_id'] == person['id'] and item['amount_cents'] == 1500


def test_cross_project_move_cannot_reuse_source_registry_person(setup: Any) -> None:
    matcher, project, other = setup
    domain, person, _ = people(matcher, project)
    item = expense(matcher, project, 'invoice')
    item = domain.assign_expense(item['id'], item['version'], person['id'])['item']
    before = snapshots(matcher)
    with matcher.store.connect() as db:
        with pytest.raises(ValueError, match='原项目'):
            matcher.move_expense(db, item['id'], {'id': other, 'name': 'Other'})
    assert snapshots(matcher) == before


