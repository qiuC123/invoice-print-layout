"""Authenticated workbench actions keep archive and project boundaries intact."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.workbench_web import make_server
from tests.test_material_matching import queued, setup, snapshots
from invoice_print_layout.material_matching import MaterialMatching


@pytest.fixture
def archive_web(setup: tuple[MaterialMatching, str, str], tmp_path: Path) -> Any:
    matcher, project, other = setup
    source = queued(matcher, project, 'archive-http', 'invoice', '31.80')
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']

    def get(route: str) -> Any:
        with urlopen(root + route) as response:
            return json.load(response)

    def post(route: str, body: dict[str, Any], credential: str | None = None) -> Any:
        with urlopen(Request(root + '/api/' + route, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json',
                                     'X-Workbench-Token': token if credential is None else credential})) as response:
            return json.load(response)

    try:
        yield matcher, project, other, source, root, get, post
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_full_archive_delete_restore_http_lifecycle_and_source_access(archive_web: Any) -> None:
    matcher, project, _, source, root, get, post = archive_web
    before = snapshots(matcher)
    entry = source
    for verb, status, lane in [('archive', 'archived', 'archived'), ('delete', 'deleted', 'deleted'),
                              ('restore-deleted', 'archived', 'archived'), ('restore', 'review', 'vouchers')]:
        result = post('intake/' + verb, {'id': entry['id'], 'revision': entry['revision'], 'project_id': project})
        entry = result['entry']
        assert 'path' not in entry and entry['status'] == status and result['message']
        board = get('/api/material-matching?project=' + project)
        assert [card['id'] for card in board[lane]] == [source['id']]
        assert '"path"' not in json.dumps(board)
        with urlopen(root + '/intake-file/' + source['id']) as response:
            assert hashlib.sha256(response.read()).hexdigest() == source['digest']
    after = snapshots(matcher)
    assert all(after[table] == before[table] for table in before if table != 'intake_queue')
    assert entry['fields'] == source['fields'] and entry['revision'] == source['revision'] + 4


@pytest.mark.parametrize('verb', ['archive', 'restore', 'delete', 'restore-deleted'])
def test_archive_mutations_require_token(archive_web: Any, verb: str) -> None:
    matcher, project, _, source, _, _, post = archive_web
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as error:
        post('intake/' + verb, {'id': source['id'], 'revision': source['revision'], 'project_id': project}, '')
    assert error.value.code == 403 and snapshots(matcher) == before


@pytest.mark.parametrize('verb', ['archive', 'restore', 'delete', 'restore-deleted'])
def test_other_project_actions_reject_before_mutation(archive_web: Any, verb: str) -> None:
    matcher, project, other, source, _, _, post = archive_web
    if verb != 'archive':
        source = post('intake/archive', {'id': source['id'], 'revision': source['revision'], 'project_id': project})['entry']
    if verb == 'restore-deleted':
        source = post('intake/delete', {'id': source['id'], 'revision': source['revision'], 'project_id': project})['entry']
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as error:
        post('intake/' + verb, {'id': source['id'], 'revision': source['revision'], 'project_id': other})
    assert error.value.code == 400 and snapshots(matcher) == before


@pytest.mark.parametrize('deleted', [False, True])
def test_http_legacy_claim_classify_resolve_prepare_cannot_reactivate(archive_web: Any, deleted: bool) -> None:
    matcher, project, _, source, _, _, post = archive_web
    source = post('intake/archive', {'id': source['id'], 'revision': source['revision'], 'project_id': project})['entry']
    if deleted:
        source = post('intake/delete', {'id': source['id'], 'revision': source['revision'], 'project_id': project})['entry']
    before = snapshots(matcher)
    for route, body in [
        ('intake/classify', {'id': source['id'], 'revision': source['revision'], 'role': 'invoice', 'fields': {}}),
        ('intake/resolve', {'id': source['id'], 'revision': source['revision'], 'role': 'invoice', 'fields': source['fields']}),
        ('intake/claim', {'id': source['id'], 'project_id': project})]:
        with pytest.raises(HTTPError) as error:
            post(route, body)
        assert error.value.code == 400
    assert post('intake/prepare', {'id': source['id'], 'force_ai': True})['entry']['status'] == source['status']
    assert post('intake/analyze', {'id': source['id']})['status'] == source['status']
    assert snapshots(matcher) == before


def test_delete_requires_archive_and_stale_replay_has_no_side_effect(archive_web: Any) -> None:
    matcher, project, _, source, _, _, post = archive_web
    body = {'id': source['id'], 'revision': source['revision'], 'project_id': project}
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as error:
        post('intake/delete', body)
    assert error.value.code == 400 and snapshots(matcher) == before
    post('intake/archive', body)
    archived = snapshots(matcher)
    for verb in ('archive', 'delete', 'restore'):
        with pytest.raises(HTTPError) as error:
            post('intake/' + verb, body)
        assert error.value.code == 400
    assert snapshots(matcher) == archived
