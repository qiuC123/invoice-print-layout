"""Bound repeated scans while preserving fresh projections and write guards."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from invoice_print_layout.reimbursement_people import ReimbursementPeople
from tests.test_material_matching import (setup, expense, queued, numbered_group,
                                         numbered_support, proof, snapshots)


def test_expense_list_shares_database_and_material_scan_without_stale_files(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    store = matcher.store
    items = [expense(matcher, project, f'Synthetic {i}') for i in range(12)]
    for item in items:
        queued(matcher, project, item['id'], 'other', None, item_id=item['id'])
    expected = [store.get(item['id']) for item in reversed(items)]
    queries: list[str] = []
    connections = 0
    connect = store.connect

    @contextmanager
    def traced_connect() -> Iterator[sqlite3.Connection]:
        nonlocal connections
        connections += 1
        with connect() as db:
            db.set_trace_callback(queries.append)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', traced_connect)
        assert store.list_items(include_deleted=True) == expected
    assert connections == 1
    assert sum(query == 'SELECT data FROM intake_queue' for query in queries) == 1

    first = items[0]
    with store.connect() as db:
        record = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (first['id'],)).fetchone()['data'])
        record['note'] = 'Changed after the first read'
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(record), first['id']))
    current = next(item for item in store.list_items() if item['id'] == first['id'])
    assert current['version'] != first['version'] and current['note'] == record['note']
    path, _ = store.attachment_path(first['attachments'][0]['id'])
    path.unlink()
    current = next(item for item in store.list_items() if item['id'] == first['id'])
    assert not current['attachments'][0]['available'] and not current['complete']


def test_people_response_shares_export_reads_and_rechecks_references(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    domain = ReimbursementPeople(matcher.store)
    people = [domain.create(project, f'Synthetic {i}')['person'] for i in range(3)]
    exports = matcher.store.root / '导出'
    exports.mkdir(exist_ok=True)
    document = exports / 'synthetic.json'
    document.write_text(json.dumps({'project_id': project, 'person_id': people[0]['id']}), encoding='utf-8')
    expected = {'project_id': project, 'people': domain.list(project),
                'retired_people': domain.list(project, retired=True), 'summary': domain.summary(project)}
    read_text = Path.read_text
    reads: list[Path] = []

    def tracked_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.parent == exports:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', tracked_read)
        assert domain.snapshot(project) == expected
    assert reads == [document]
    assert not expected['people'][0]['can_retire']
    document.write_text(json.dumps({'project_id': project, 'person_id': people[1]['id']}), encoding='utf-8')
    fresh = domain.snapshot(project)
    assert fresh['people'][0]['can_retire'] and not fresh['people'][1]['can_retire']
    renamed = domain.rename(project, people[0]['id'], fresh['people'][0]['version'], 'New synthetic name')
    current = domain.snapshot(project)['people'][0]
    assert current['label'] == 'New synthetic name' and current['version'] == renamed['person']['version']


def test_requested_expense_views_preserve_order_versions_and_fresh_originals(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    store = matcher.store
    items = [expense(matcher, project, f'Batch read {number}') for number in range(8)]
    for item in items:
        queued(matcher, project, item['id'], 'other', None, item_id=item['id'])
    keys = [items[6]['id'], items[0]['id'], items[6]['id'], items[3]['id']]
    expected = [store.get(key) for key in keys]
    queries: list[str] = []
    connections = 0
    connect = store.connect

    @contextmanager
    def traced_connect() -> Iterator[sqlite3.Connection]:
        nonlocal connections
        connections += 1
        with connect() as db:
            db.set_trace_callback(queries.append)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', traced_connect)
        assert store.get_many(keys) == expected
    assert connections == 1
    assert sum(query == 'SELECT data FROM intake_queue' for query in queries) == 1
    assert store.get_many([]) == []
    with pytest.raises(ValueError, match='事项不存在'):
        store.get_many([keys[0], 'nonexistent-expense'])
    first = items[0]
    with store.connect() as db:
        record = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (first['id'],)).fetchone()['data'])
        record['note'] = 'Updated between separate batch reads'
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(record), first['id']))
    fresh = store.get_many(keys)
    assert fresh == [store.get(key) for key in keys]
    assert fresh[1]['version'] != expected[1]['version'] and fresh[1]['note'] == record['note']
    original, _ = store.attachment_path(first['attachments'][0]['id'])
    original.unlink()
    assert not store.get_many([first['id']])[0]['attachments'][0]['available']


def test_board_reuses_expense_views_but_keeps_fresh_version_and_original_guards(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    first = numbered_group(matcher, project, 'First group', '12345678901234567890')
    second = numbered_group(matcher, project, 'Second group', '22345678901234567890')
    numbered_support(matcher, project, 'First detail', '12345678901234567890')
    numbered_support(matcher, project, 'Second detail', '22345678901234567890')
    expected = matcher.board(project)
    assert len(expected['supplement_candidates']) == 2
    get = matcher.store.get
    get_calls = 0

    def tracked_get(item_id: str) -> dict[str, Any]:
        nonlocal get_calls
        get_calls += 1
        return get(item_id)

    before = snapshots(matcher)
    with monkeypatch.context() as patch:
        patch.setattr(matcher.store, 'get', tracked_get)
        assert matcher.board(project) == expected
    assert get_calls == 0
    assert snapshots(matcher) == before

    with matcher.store.connect() as db:
        record = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (first['item']['id'],)).fetchone()['data'])
        record['note'] = 'Edited after the board read'
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(record), first['item']['id']))
    path, _ = matcher.store.attachment_path(second['item']['attachments'][0]['id'])
    path.write_bytes(proof('Changed synthetic original'))
    fresh = matcher.board(project)
    assert not fresh['supplement_candidates']
    group = next(group for group in fresh['matches'] if group['item_id'] == first['item']['id'])
    assert group['expected_version'] == matcher.store.get(first['item']['id'])['version']
    assert group['expected_version'] != first['item']['version']
