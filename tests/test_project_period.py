"""Project periods remain invoice metadata and only suggest overlapping projects."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from typing import Any

import pytest

from invoice_print_layout.expense_projects import ExpenseProjects, project_views
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore


@pytest.fixture
def projects(tmp_path: Path) -> ExpenseProjects:
    return ExpenseProjects(ExpenseStore(tmp_path))


def metadata(projects: ExpenseProjects) -> list[tuple[Any, ...]]:
    with projects.store.connect() as db:
        return [tuple(row) for row in db.execute('SELECT * FROM expense_project_metadata ORDER BY rowid')]


def test_old_metadata_migration_is_idempotent_and_preserves_existing_rows(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    registry = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3')
    first = registry.create_project('Synthetic old project', 'Synthetic old site')
    deleted = registry.create_project('Synthetic old tombstone')
    with store.connect() as db:
        db.execute('CREATE TABLE expense_project_metadata ('
                   'project_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT \'\', '
                   'pinned INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, '
                   'permanently_deleted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, '
                   'deleted_data TEXT NOT NULL DEFAULT \'{}\')')
        db.executemany('INSERT INTO expense_project_metadata VALUES (?,?,?,?,?,?,?)', [
            (first['id'], 'Synthetic display name', 1, 7, 0, '2026-10-01T00:00:00', '{}'),
            (deleted['id'], 'Synthetic deleted name', 0, 3, 1, '2026-10-02T00:00:00', '{"names":["old"]}')])
        db.execute('CREATE TABLE synthetic_peer (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT INTO synthetic_peer VALUES (?,?)', ('untouched', 'synthetic payload'))
        before = [tuple(row) for row in db.execute('SELECT * FROM expense_project_metadata ORDER BY rowid')]
    raw_registry = registry.list()
    assert project_views(store)[0]['start_date'] == ''
    with store.connect() as db:
        assert len(list(db.execute('PRAGMA table_info(expense_project_metadata)'))) == 7
    directory = ExpenseProjects(store)
    ExpenseProjects(store)
    ExpenseProjects(ExpenseStore(tmp_path))
    with store.connect() as db:
        columns = {row['name']: dict(row) for row in db.execute('PRAGMA table_info(expense_project_metadata)')}
        assert len(columns) == 9
        for name in ('start_date', 'end_date'):
            assert columns[name]['type'] == 'TEXT' and columns[name]['notnull'] == 1
            assert columns[name]['dflt_value'] == "''"
        after = [tuple(row) for row in db.execute('SELECT * FROM expense_project_metadata ORDER BY rowid')]
        assert [row[:7] for row in after] == before
        assert [row[7:] for row in after] == [('', ''), ('', '')]
        assert [tuple(row) for row in db.execute('SELECT * FROM synthetic_peer')] == [('untouched', 'synthetic payload')]
    assert registry.list() == raw_registry
    assert [(row['id'], row['start_date'], row['end_date']) for row in directory.list()] == [(first['id'], '', '')]


@pytest.mark.parametrize('start,end', [('', ''), ('2026-10-01', ''),
                                       ('2026-10-01', '2026-10-01'), ('2024-02-29', '2026-10-31')])
def test_create_period_persists_without_changing_registry_contract(projects: ExpenseProjects,
                                                                  start: str, end: str) -> None:
    created = projects.create('Synthetic period project', 'Synthetic site', start, end)
    assert created['start_date'] == start and created['end_date'] == end
    assert created['project_revision'] == 0
    assert created['site_name'] == 'Synthetic site'
    raw = projects.registry.list()[0]
    assert 'start_date' not in raw and 'end_date' not in raw
    assert raw['start'] == raw['end'] == ''
    restarted = ExpenseProjects(ExpenseStore(projects.store.workspace))
    assert restarted.list() == [created]
    with projects.store.connect() as db:
        row = db.execute('SELECT start_date,end_date,revision FROM expense_project_metadata WHERE project_id=?',
                         (created['id'],)).fetchone()
        assert tuple(row) == (start, end, 0)


INVALID_PERIODS = [
    (None, ''), (True, ''), (1, ''), ([], ''), ('2026-10-01', None), ('2026-10-01', False),
    ('', '2026-10-01'), ('2026-10-02', '2026-10-01'), ('2026-1-01', ''), ('20261001', ''),
    ('2026-10-01 ', ''), (' 2026-10-01', ''), ('2026-10-01T00:00:00', ''),
    ('2026-02-29', ''), ('2026-13-01', ''), ('2026-00-01', ''), ('2026-10-01', '2026-10-32'),
]


@pytest.mark.parametrize('start,end', INVALID_PERIODS)
def test_invalid_period_rejected_before_create_or_update_writes(projects: ExpenseProjects,
                                                              start: Any, end: Any) -> None:
    existing = projects.create('Synthetic existing project', start_date='2026-10-01')
    before, raw_registry = metadata(projects), projects.registry.list()
    with pytest.raises(ValueError):
        projects.create('Synthetic invalid project', start_date=start, end_date=end)
    with pytest.raises(ValueError):
        projects.set_period(existing['id'], start, end, existing['project_revision'])
    assert metadata(projects) == before and projects.registry.list() == raw_registry


def test_edit_repeat_and_clear_each_advance_revision_without_touching_other_projects(projects: ExpenseProjects) -> None:
    current = projects.create('Synthetic current', start_date='2026-10-01', end_date='2026-10-05')
    other = projects.create('Synthetic other', start_date='2026-12-01')
    raw_registry = projects.registry.list()
    updated = projects.set_period(current['id'], '2026-10-02', '2026-10-06', 0)
    assert updated['project_revision'] == 1 and updated['overlaps'] == []
    same = projects.set_period(current['id'], '2026-10-02', '2026-10-06', 1)
    assert same['project_revision'] == 2
    cleared = projects.set_period(current['id'], '', '', 2)
    assert cleared['project_revision'] == 3 and cleared['start_date'] == cleared['end_date'] == ''
    assert cleared['overlaps'] == []
    cleared_again = projects.set_period(current['id'], '', '', 3)
    assert cleared_again['project_revision'] == 4
    assert next(row for row in projects.list() if row['id'] == other['id']) == other
    assert projects.registry.list() == raw_registry


@pytest.mark.parametrize('revision', [None, True, False, '0', 0.0, -1, 999])
def test_invalid_or_stale_revision_cannot_change_period(projects: ExpenseProjects, revision: Any) -> None:
    current = projects.create('Synthetic current')
    before = metadata(projects)
    with pytest.raises(ValueError):
        projects.set_period(current['id'], '2026-10-01', '', revision)
    assert metadata(projects) == before


@pytest.mark.parametrize('identity', ['', 'missing', None, ['missing']])
def test_missing_project_cannot_gain_period_metadata(projects: ExpenseProjects, identity: Any) -> None:
    before = metadata(projects)
    with pytest.raises(ValueError):
        projects.set_period(identity, '2026-10-01', '', 0)
    assert metadata(projects) == before


def test_rename_pin_reimbursement_and_delete_preserve_period(projects: ExpenseProjects) -> None:
    current = projects.create('Synthetic current', start_date='2026-10-01', end_date='2026-10-05')
    raw_registry = projects.registry.list()
    renamed = projects.rename(current['id'], 'Synthetic renamed', 0)
    pinned = projects.pin(current['id'], True, 1)
    marked = projects.set_reimbursement_status(current['id'], 'reimbursed', 0)
    for result in (renamed, pinned, marked):
        assert (result['start_date'], result['end_date']) == ('2026-10-01', '2026-10-05')
    assert marked['project_revision'] == 3
    projects.delete_permanently(current['id'], 3, confirmed=True, confirmation_name='Synthetic renamed')
    with projects.store.connect() as db:
        deleted = db.execute('SELECT start_date,end_date,permanently_deleted FROM expense_project_metadata '
                             'WHERE project_id=?', (current['id'],)).fetchone()
        assert tuple(deleted) == ('2026-10-01', '2026-10-05', 1)
    assert projects.registry.list() == raw_registry
    with pytest.raises(ValueError):
        projects.set_period(current['id'], '2026-10-02', '', 4)


def test_period_uses_shared_revision_after_other_project_edits(projects: ExpenseProjects) -> None:
    current = projects.create('Synthetic current')
    projects.pin(current['id'], True, 0)
    with pytest.raises(ValueError, match='已变化'):
        projects.set_period(current['id'], '2026-10-01', '', 0)
    saved = projects.set_period(current['id'], '2026-10-01', '', 1)
    assert saved['project_revision'] == 2 and saved['pinned']


def test_overlaps_are_inclusive_advisory_and_exclude_reimbursed_or_deleted_projects(projects: ExpenseProjects) -> None:
    current = projects.create('Synthetic current')
    projects.create('Synthetic touches start', start_date='2026-09-01', end_date='2026-10-01')
    projects.create('Synthetic touches end', start_date='2026-10-10', end_date='2026-11-01')
    projects.create('Synthetic ongoing', start_date='2026-09-01')
    projects.create('Synthetic later', start_date='2026-12-01', end_date='2026-12-31')
    projects.create('Synthetic unknown')
    reimbursed = projects.create('Synthetic reimbursed', start_date='2026-10-01')
    projects.set_reimbursement_status(reimbursed['id'], 'reimbursed', 0)
    removed = projects.create('Synthetic deleted', start_date='2026-10-01')
    projects.delete_permanently(removed['id'], 0, confirmed=True, confirmation_name=removed['name'])
    saved = projects.set_period(current['id'], '2026-10-01', '2026-10-10', 0)
    assert saved['overlaps'] == ['Synthetic touches start', 'Synthetic touches end', 'Synthetic ongoing']
    assert saved['start_date'] == '2026-10-01' and saved['end_date'] == '2026-10-10'
    ongoing = projects.set_period(current['id'], '2026-11-02', '', 1)
    assert ongoing['overlaps'] == ['Synthetic ongoing', 'Synthetic later']
    assert projects.set_period(current['id'], '', '', 2)['overlaps'] == []


def test_concurrent_period_updates_with_one_revision_have_one_winner(projects: ExpenseProjects) -> None:
    current = projects.create('Synthetic current')
    barrier = threading.Barrier(2)

    def update(start: str) -> str:
        barrier.wait(timeout=5)
        try:
            projects.set_period(current['id'], start, '', 0)
            return 'saved'
        except ValueError as error:
            assert '已变化' in str(error)
            return 'stale'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ('2026-10-01', '2026-10-02'))) == ['saved', 'stale']
    assert projects.list()[0]['project_revision'] == 1


def test_existing_schema_can_initialize_inside_an_existing_writer(projects: ExpenseProjects) -> None:
    current = projects.create('Synthetic current', start_date='2026-10-01')
    with projects.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE expense_project_metadata SET pinned=pinned WHERE project_id=?', (current['id'],))
        nested = ExpenseProjects(projects.store)
        assert nested.list() == [current]
