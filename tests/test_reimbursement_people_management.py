from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from urllib.error import HTTPError

import pytest

from invoice_print_layout.reimbursement_people import ReimbursementPeople, validate_person, _matching_person_records
from invoice_print_layout.report_snapshot import snapshot
from tests.test_reimbursement_people import people, assign, create
from tests.test_reimbursement_people_http import attribution_web
from tests.test_material_matching import setup, expense, queued, revisions, snapshots
from tests.test_amount_pairing import material


def state(matcher: Any) -> dict[str, Any]:
    result = snapshots(matcher)
    with matcher.store.connect() as db:
        result['people'] = [tuple(row) for row in db.execute('SELECT * FROM reimbursement_people ORDER BY rowid')]
    return result


def current(domain: ReimbursementPeople, project: str, person: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in domain.list(project) if row['id'] == person['id'])


def merge(domain: ReimbursementPeople, project: str, source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    return domain.merge(project, source['id'], source['version'], target['id'], target['version'])


def test_rename_keeps_identity_original_nickname_finances_and_stage_for_all_current_records(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, _ = people(matcher, project)
    entries = assign(domain, project, source, material(matcher, project, 'source-invoice', 'invoice', '14'))
    entries[0]['wechat_source'] = {'sender_id': 'sender-a', 'nickname': 'Original external nickname'}
    with matcher.store.connect() as db:
        matcher._write_queue(db, entries[0])
    result = create(matcher, project, entries)
    # A current registered draft and a historical submitted fee share the same person.
    historical = matcher.store.create({'project_id': project, 'title': 'Historical hotel', 'amount': '31',
                                      'category': '酒店', 'expense_date': '2026-01-02',
                                      'reimbursement_person_id': source['id']})
    with matcher.store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (historical['id'],)).fetchone()['data'])
        data.update(stage='submitted', verified=True, confirmation={'reason': 'Prior review'}, report_files={'pdf': 'frozen.pdf'})
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), historical['id']))
        before = {row['id']: json.loads(row['data']) for row in db.execute('SELECT id,data FROM expenses')}
    updated = domain.rename(project, source['id'], source['version'], 'Formal person')['person']
    assert updated['id'] == source['id'] and updated['external_sender_id'] == source['external_sender_id']
    assert source['label'] in updated['aliases'] and updated['version'] != source['version']
    with matcher.store.connect() as db:
        for row in db.execute('SELECT id,data FROM expenses'):
            data = json.loads(row['data'])
            assert data['reimbursement_person_label'] == 'Formal person'
            for field in ('amount_cents', 'category', 'expense_date', 'verified', 'stage', 'confirmation', 'report_files'):
                assert data.get(field) == before[row['id']].get(field)
    source_queue = matcher.intake.get(entries[0]['id'])
    assert source_queue['reimbursement_person_label'] == 'Formal person'
    assert source_queue['wechat_source'] == entries[0]['wechat_source'] and source_queue['fields'] == entries[0]['fields']
    assert matcher.store.get(historical['id'])['stage'] == 'submitted'
    # A pristine registration remains undoable and restores the current canonical label.
    item = matcher.store.get(result['item']['id'])
    matcher.undo(result['match']['id'], item['version'])
    assert matcher.intake.get(entries[0]['id'])['reimbursement_person_label'] == 'Formal person'


def test_merge_external_sender_chain_reuses_canonical_person_and_preserves_source_rows(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    item = expense(matcher, project, 'Source fee', '14')
    item = domain.assign_expense(item['id'], item['version'], source['id'])['item']
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in matcher.store.files.iterdir()}
    result = merge(domain, project, source, target)
    assert result['source_person']['state'] == 'merged' and result['source_person']['merged_into'] == target['id']
    assert result['source_person']['external_sender_id'] == source['external_sender_id']
    assert result['source_person']['label'] == source['label'] and source['label'] in result['person']['aliases']
    canonical = domain.create(project, 'Different incoming nickname', source['external_sender_id'])
    assert not canonical['created'] and canonical['person']['id'] == target['id']
    assert matcher.store.get(item['id'])['reimbursement_person_id'] == target['id']
    third = domain.create(project, 'Third canonical', 'sender-c')['person']
    merge(domain, project, result['person'], third)
    assert domain.create(project, 'Yet another nickname', source['external_sender_id'])['person']['id'] == third['id']
    with matcher.store.connect() as db:
        assert validate_person(db, project, source['id'])['reimbursement_person_id'] == third['id']
    assert [person['id'] for person in domain.list(project)] == [third['id']]
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items())


def test_canonical_person_can_rename_to_merged_source_alias_without_overwriting_source_evidence(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    result = merge(domain, project, source, target)
    renamed = domain.rename(project, target['id'], result['person']['version'], source['label'])['person']
    assert renamed['id'] == target['id'] and renamed['label'] == source['label']
    assert renamed['source_label'] == target['label']
    assert domain.create(project, 'Changed source nickname', source['external_sender_id'])['person']['id'] == target['id']
    with matcher.store.connect() as db:
        source_row = dict(db.execute('SELECT * FROM reimbursement_people WHERE id=?', (source['id'],)).fetchone())
    assert source_row['label'] == source['label'] and source_row['external_sender_id'] == source['external_sender_id']
    assert len(domain.list(project)) == 1


@pytest.mark.parametrize('operation', ['rename', 'merge'])
def test_actual_expense_reassignment_then_old_person_management_keeps_group_pristine(setup: Any, operation: str) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    third = domain.create(project, 'Third canonical')['person']
    entries = assign(domain, project, source, queued(matcher, project, 'source-invoice', 'invoice', '14'))
    result = create(matcher, project, entries)
    reassigned = domain.assign_expense(result['item']['id'], result['item']['version'], target['id'])['item']
    with matcher.store.connect() as db:
        group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (result['match']['id'],)).fetchone()['data'])
    assert all(record['reimbursement_person_id'] == target['id'] for record in _matching_person_records(group)
               if record.get('reimbursement_person_id'))
    if operation == 'rename':
        domain.rename(project, source['id'], source['version'], 'Old source renamed')
    else:
        merge(domain, project, source, third)
    current_item = matcher.store.get(reassigned['id'])
    assert current_item['reimbursement_person_id'] == target['id']
    matcher.undo(result['match']['id'], current_item['version'])
    assert matcher.intake.get(entries[0]['id'])['reimbursement_person_id'] == target['id']


@pytest.mark.parametrize('historical_state', ['verified', 'submitted', 'reimbursed', 'deleted', 'returned', 'archived'])
def test_merge_covers_all_persistent_expense_states_without_financial_changes(setup: Any, historical_state: str) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    item = expense(matcher, project, 'Retained historical source', '14')
    item = domain.assign_expense(item['id'], item['version'], source['id'])['item']
    with matcher.store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        if historical_state in {'submitted', 'reimbursed'}:
            data['stage'] = historical_state
            data['verified'] = True
        elif historical_state == 'verified':
            data['verified'] = True
        elif historical_state == 'deleted':
            data['deleted'] = True
        elif historical_state == 'returned':
            data['returned_to_synthesis'] = True
        else:
            data.update(synthesis_material_state='archived', returned_to_synthesis=True)
        before = dict(data)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    merge(domain, project, source, target)
    with matcher.store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
    assert data['reimbursement_person_id'] == target['id'] and data['reimbursement_person_label'] == target['label']
    ignored = {'reimbursement_person_id', 'reimbursement_person_label', 'updated_at'}
    assert {key: value for key, value in data.items() if key not in ignored} == {
        key: value for key, value in before.items() if key not in ignored}


def test_pristine_multi_group_members_and_nested_attribution_restore_canonical_owner(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    members = [expense(matcher, project, 'source-10', '10'), expense(matcher, project, 'source-4', '4')]
    members = [domain.assign_expense(item['id'], item['version'], source['id'])['item'] for item in members]
    payment = assign(domain, project, source, queued(matcher, project, 'pay-14', 'payment', '14'))[0]
    result = matcher.match(['e:' + item['id'] for item in members], [payment['id']], project,
                           expected_versions={item['id']: item['version'] for item in members}, revisions=revisions(payment))
    merge(domain, project, source, target)
    aggregate = matcher.store.get(result['item']['id'])
    matcher.undo(result['match']['id'], aggregate['version'])
    assert all(matcher.store.get(item['id'])['reimbursement_person_id'] == target['id'] for item in members)
    assert matcher.intake.get(payment['id'])['reimbursement_person_id'] == target['id']
    assert sum(item['amount_cents'] for item in matcher.store.list_items() if item['stage'] == 'draft') == 1400


def test_later_manual_edit_stays_a_mismatch_after_person_merge(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    entries = assign(domain, project, source, queued(matcher, project, 'source-invoice', 'invoice', '14'))
    result = create(matcher, project, entries)
    edited = matcher.store.edit_review_fields(result['item']['id'], result['item']['version'], 'other', 'Later human choice', '20')
    merge(domain, project, source, target)
    current_item = matcher.store.get(edited['id'])
    before = state(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], current_item['version'])
    assert state(matcher) == before and current_item['amount_cents'] == 2000 and current_item['title'] == 'Later human choice'


def test_empty_person_retire_restore_is_reversible_and_not_implicitly_revived(setup: Any) -> None:
    matcher, project, _ = setup
    domain = ReimbursementPeople(matcher.store)
    person = domain.create(project, '本人', 'empty-stable-sender')['person']
    assert person['can_retire'] and not person['reference_count']
    retired = domain.retire(project, person['id'], person['version'])['person']
    assert retired['state'] == 'retired' and retired['can_restore'] and not domain.list(project)
    assert domain.list(project, retired=True) == [retired]
    with pytest.raises(ValueError, match='停用'):
        domain.create(project, 'Changed incoming nickname', person['external_sender_id'])
    with matcher.store.connect() as db:
        with pytest.raises(ValueError, match='停用'):
            validate_person(db, project, person['id'])
    restored = domain.retire(project, retired['id'], retired['version'], restore=True)['person']
    assert restored['id'] == person['id'] and restored['state'] == 'active' and restored['revision'] == 2


@pytest.mark.parametrize('reference', ['expense', 'queue', 'match_snapshot', 'report_snapshot'])
def test_retire_rejects_any_real_reference_including_frozen_history(setup: Any, reference: str) -> None:
    matcher, project, _ = setup
    domain, person, _ = people(matcher, project)
    if reference == 'expense':
        matcher.store.create({'project_id': project, 'title': 'Owned expense', 'amount': '14', 'reimbursement_person_id': person['id']})
    elif reference == 'queue':
        assign(domain, project, person, queued(matcher, project, 'Owned material', 'invoice', '14'))
    elif reference == 'match_snapshot':
        group = {'id': 'synthetic-history', 'project_id': project, 'status': 'undone',
                 'queue_before': {'old-material': {'reimbursement_person_id': person['id'], 'reimbursement_person_label': person['label']}}}
        with matcher.store.connect() as db:
            db.execute('INSERT INTO material_matches VALUES (?,?)', (group['id'], json.dumps(group)))
    else:
        folder = matcher.store.root / '导出'
        folder.mkdir(exist_ok=True)
        (folder / 'frozen-report-snapshot.json').write_text(json.dumps({'project_id': project,
            'person_allocation': {'rows': [{'person_id': person['id'], 'label': person['label']}]}}), encoding='utf-8')
    view = current(domain, project, person)
    assert view['reference_count'] and not view['can_retire']
    before = state(matcher)
    with pytest.raises(ValueError, match='已有费用或材料'):
        domain.retire(project, person['id'], view['version'])
    assert state(matcher) == before


def test_unrelated_name_strings_and_foreign_snapshots_do_not_prevent_empty_retirement(setup: Any) -> None:
    matcher, project, other = setup
    domain, person, _ = people(matcher, project)
    group = {'id': 'foreign-history', 'project_id': other, 'status': 'undone',
             'queue_before': {'old': {'reimbursement_person_id': person['id']}}}
    with matcher.store.connect() as db:
        db.execute('INSERT INTO material_matches VALUES (?,?)', (group['id'], json.dumps(group)))
    folder = matcher.store.root / '导出'
    folder.mkdir(exist_ok=True)
    (folder / 'unrelated.json').write_text(json.dumps({'project_id': project, 'label': person['label'], 'remark': person['id']}), encoding='utf-8')
    assert current(domain, project, person)['can_retire']
    assert domain.retire(project, person['id'], person['version'])['person']['state'] == 'retired'


def test_management_rejects_cross_project_self_stale_target_and_duplicate_name(setup: Any) -> None:
    matcher, project, other = setup
    domain, source, target = people(matcher, project)
    foreign = domain.create(other, 'Foreign canonical')['person']
    before = state(matcher)
    operations: list[Callable[[], Any]] = [lambda: merge(domain, project, source, source), lambda: merge(domain, project, source, foreign),
                  lambda: domain.rename(project, source['id'], 'stale', 'Changed'),
                  lambda: domain.merge(project, source['id'], source['version'], target['id'], 'stale'),
                  lambda: domain.rename(project, source['id'], source['version'], target['label'])]
    for operation in operations:
        with pytest.raises(ValueError):
            operation()
        assert state(matcher) == before


def test_real_intermediate_sql_failure_rolls_back_people_expenses_queue_and_groups(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    entries = assign(domain, project, source, queued(matcher, project, 'source-1', 'invoice', '14'),
                     queued(matcher, project, 'source-2', 'payment', '14'))
    create(matcher, project, entries)
    with matcher.store.connect() as db:
        db.execute("CREATE TRIGGER fail_person_sync BEFORE UPDATE ON intake_queue WHEN OLD.id='" + entries[-1]['id'] +
                   "' BEGIN SELECT RAISE(ABORT,'Synthetic person propagation failure'); END")
    before = state(matcher)
    with pytest.raises(sqlite3.IntegrityError, match='propagation failure'):
        merge(domain, project, source, target)
    assert state(matcher) == before


def test_mutually_concurrent_merges_cannot_create_an_alias_cycle(setup: Any) -> None:
    matcher, project, _ = setup
    domain, first, second = people(matcher, project)
    start = threading.Barrier(2)
    def run(pair: tuple[dict[str, Any], dict[str, Any]]) -> Any:
        start.wait(timeout=5)
        try:
            return merge(domain, project, pair[0], pair[1])
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [(first, second), (second, first)]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert len(domain.list(project)) == 1
    assert domain.create(project, 'Any nickname', first['external_sender_id'])['person']['id'] == domain.create(
        project, 'Other nickname', second['external_sender_id'])['person']['id']


def test_merge_invalidates_old_amount_preview_and_new_equal_amounts_are_ambiguous(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    for index, person in enumerate((source, target)):
        assign(domain, project, person, material(matcher, project, f'voucher-{index}', 'invoice', '14'),
               material(matcher, project, f'payment-{index}', 'payment', '14'))
    preview = matcher.amount_preview(project)
    assert len(preview['groups']) == 2
    merge(domain, project, source, target)
    before = state(matcher)
    with pytest.raises(ValueError, match='变化'):
        matcher.amount_confirm(project, preview['snapshot_token'], 'person-old-amount-preview', True)
    assert not matcher.amount_preview(project)['groups'] and state(matcher) == before


def test_generated_snapshot_files_remain_byte_identical_after_rename_and_merge(setup: Any) -> None:
    matcher, project, _ = setup
    domain, source, target = people(matcher, project)
    item = expense(matcher, project, 'Frozen source fee', '14')
    item = domain.assign_expense(item['id'], item['version'], source['id'])['item']
    folder = matcher.store.root / '导出'
    folder.mkdir(exist_ok=True)
    saved = folder / 'report-snapshot.json'
    saved.write_text(json.dumps(snapshot([item]), ensure_ascii=False), encoding='utf-8')
    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    source = domain.rename(project, source['id'], source['version'], 'Canonical formal name')['person']
    merge(domain, project, source, target)
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == digest
    assert json.loads(saved.read_text(encoding='utf-8'))['person_allocation']['rows'][0]['person_id'] == item['reimbursement_person_id']


def test_management_http_auth_versions_retired_and_existing_selection(attribution_web: Any) -> None:
    store, project, other, item, get, post = attribution_web
    source = post('reimbursement-people/create', {'project_id': project, 'label': 'Incoming nickname', 'external_sender_id': 'source-sender'})['person']
    target = post('reimbursement-people/create', {'project_id': project, 'label': 'Already known person'})['person']
    empty = post('reimbursement-people/create', {'project_id': project, 'label': '本人'})['person']
    for route, extra in [('rename', {'label': 'Formal source'}), ('merge', {'target_person_id': target['id'], 'target_expected_version': target['version']}),
                         ('retire', {}), ('restore', {})]:
        with pytest.raises(HTTPError) as unauthorized:
            post('reimbursement-people/' + route, {'project_id': project, 'person_id': source['id'],
                 'expected_version': source['version'], **extra}, '')
        assert unauthorized.value.code == 403
    source = post('reimbursement-people/rename', {'project_id': project, 'person_id': source['id'],
                  'expected_version': source['version'], 'label': 'Formal source'})['person']
    assigned = post('reimbursement-people/assign-expense', {'id': item['id'], 'expected_version': item['version'], 'person_id': source['id']})['item']
    result = post('reimbursement-people/merge', {'project_id': project, 'person_id': source['id'], 'expected_version': source['version'],
                  'target_person_id': target['id'], 'target_expected_version': target['version']})
    assert result['person']['id'] == target['id'] and store.get(item['id'])['amount_cents'] == assigned['amount_cents']
    replay = post('reimbursement-people/create', {'project_id': project, 'label': 'New nickname', 'external_sender_id': 'source-sender'})
    assert not replay['created'] and replay['person']['id'] == target['id']
    retired = post('reimbursement-people/retire', {'project_id': project, 'person_id': empty['id'], 'expected_version': empty['version']})['person']
    listed = get('/api/reimbursement-people?project=' + project)
    assert [person['id'] for person in listed['people']] == [target['id']]
    assert listed['retired_people'][0]['id'] == retired['id']
    with pytest.raises(HTTPError) as stale:
        post('reimbursement-people/restore', {'project_id': project, 'person_id': retired['id'], 'expected_version': empty['version']})
    assert stale.value.code == 400
    assert post('reimbursement-people/restore', {'project_id': project, 'person_id': retired['id'], 'expected_version': retired['version']})['person']['id'] == empty['id']
    assert get('/api/reimbursement-people?project=' + other)['people'] == []
