from __future__ import annotations

from typing import Any
from urllib.error import HTTPError

import pytest

from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.reimbursement_people import ReimbursementPeople
from tests.test_material_matching import setup
from tests.test_reimbursement_people_http import attribution_web


def test_main_person_set_replace_clear_and_preserve_project_metadata(setup: Any) -> None:
    matcher, project, _ = setup
    people = ReimbursementPeople(matcher.store)
    first = people.create(project, 'Synthetic first')['person']
    second = people.create(project, 'Synthetic second')['person']
    initial = people.snapshot(project)
    assert initial['main_person_id'] == ''
    selected = people.set_main_person(project, first['id'], initial['project_revision'], first['version'])
    assert selected['main_person_id'] == first['id']
    assert selected['project_revision'] == initial['project_revision'] + 1
    repeated = people.set_main_person(project, first['id'], selected['project_revision'], first['version'])
    assert repeated['project_revision'] == selected['project_revision']
    replaced = people.set_main_person(project, second['id'], selected['project_revision'], second['version'])
    metadata = people.projects.set_period(project, '2026-11-01', '', replaced['project_revision'])
    assert metadata['main_person_id'] == second['id']
    assert people.projects.rename(project, 'Synthetic renamed', metadata['project_revision'])['main_person_id'] == second['id']
    current = people.snapshot(project)
    cleared = people.set_main_person(project, '', current['project_revision'])
    assert cleared['main_person_id'] == ''
    assert not matcher.store.list_items() and not matcher.intake.list()


def test_retire_clears_main_person_and_restore_does_not_restore_default(setup: Any) -> None:
    matcher, project, _ = setup
    people = ReimbursementPeople(matcher.store)
    person = people.create(project, 'Synthetic owner')['person']
    selected = people.set_main_person(project, person['id'], 0, person['version'])
    retired = people.retire(project, person['id'], person['version'])['person']
    snapshot = people.snapshot(project)
    assert snapshot['main_person_id'] == ''
    assert snapshot['project_revision'] == selected['project_revision'] + 1
    people.retire(project, person['id'], retired['version'], restore=True)
    assert people.snapshot(project)['main_person_id'] == ''


def test_merge_main_person_follows_canonical_target(setup: Any) -> None:
    matcher, project, _ = setup
    people = ReimbursementPeople(matcher.store)
    source = people.create(project, 'Synthetic source')['person']
    target = people.create(project, 'Synthetic target')['person']
    third = people.create(project, 'Synthetic third')['person']
    people.set_main_person(project, source['id'], 0, source['version'])
    merged = people.merge(project, source['id'], source['version'], target['id'], target['version'])
    assert people.snapshot(project)['main_person_id'] == target['id']
    people.merge(project, target['id'], merged['person']['version'], third['id'], third['version'])
    assert people.snapshot(project)['main_person_id'] == third['id']


def test_main_person_rejects_stale_project_person_foreign_and_inactive(setup: Any) -> None:
    matcher, project, other = setup
    people = ReimbursementPeople(matcher.store)
    source = people.create(project, 'Synthetic source')['person']
    foreign = people.create(other, 'Synthetic foreign')['person']
    selected = people.set_main_person(project, source['id'], 0, source['version'])
    before = people.snapshot(project)
    with pytest.raises(ValueError, match='已变化'):
        people.set_main_person(project, '', 0)
    with pytest.raises(ValueError, match='当前项目'):
        people.set_main_person(project, foreign['id'], selected['project_revision'], foreign['version'])
    with pytest.raises(ValueError, match='版本'):
        people.set_main_person(project, '', True)
    assert people.snapshot(project) == before
    renamed = people.rename(project, source['id'], source['version'], 'Synthetic renamed')['person']
    with pytest.raises(ValueError, match='已变化'):
        people.set_main_person(project, source['id'], selected['project_revision'], source['version'])
    retired = people.retire(project, renamed['id'], renamed['version'])['person']
    with pytest.raises(ValueError, match='在用'):
        people.set_main_person(project, retired['id'], people.snapshot(project)['project_revision'], retired['version'])


def test_main_person_schema_migration_is_idempotent(setup: Any) -> None:
    matcher, project, _ = setup
    people = ReimbursementPeople(matcher.store)
    person = people.create(project, 'Synthetic owner')['person']
    people.set_main_person(project, person['id'], 0, person['version'])
    ExpenseProjects(matcher.store)
    ExpenseProjects(matcher.store)
    assert ReimbursementPeople(matcher.store).snapshot(project)['main_person_id'] == person['id']


def test_main_person_migrates_existing_metadata_without_replacing_dates(setup: Any) -> None:
    matcher, project, _ = setup
    projects = ExpenseProjects(matcher.store)
    projects.set_period(project, '2026-11-01', '2026-11-02', 0)
    with matcher.store.connect() as db:
        db.execute('ALTER TABLE expense_project_metadata DROP COLUMN main_person_id')
    migrated = ExpenseProjects(matcher.store)
    saved = next(row for row in migrated.list() if row['id'] == project)
    assert saved['main_person_id'] == ''
    assert (saved['start_date'], saved['end_date'], saved['project_revision']) == ('2026-11-01', '2026-11-02', 1)


def test_main_person_http_requires_token_and_current_versions(attribution_web: Any) -> None:
    store, project, _, expense, get, post = attribution_web
    person = post('reimbursement-people/create', {'project_id': project, 'label': 'Synthetic owner'})['person']
    before = store.get(expense['id'])
    snapshot = get('/api/reimbursement-people?project=' + project)
    body = {'project_id': project, 'person_id': person['id'],
            'expected_revision': snapshot['project_revision'], 'expected_version': person['version']}
    with pytest.raises(HTTPError) as denied:
        post('reimbursement-people/main-person', body, '')
    assert denied.value.code == 403
    assert get('/api/reimbursement-people?project=' + project)['main_person_id'] == ''
    selected = post('reimbursement-people/main-person', body)
    assert selected['main_person_id'] == person['id']
    with pytest.raises(HTTPError) as stale:
        post('reimbursement-people/main-person', {**body, 'person_id': ''})
    assert stale.value.code == 400
    assert get('/api/reimbursement-people?project=' + project)['main_person_id'] == person['id']
    assert store.get(expense['id']) == before
