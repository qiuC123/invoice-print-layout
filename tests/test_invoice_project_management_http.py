"""Invoice project menus use authenticated, versioned local APIs."""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.workbench_web import make_server
from tests.test_invoice_project_management import managed, tables, original_hashes, registry_state


@pytest.fixture
def project_web(managed: Any, tmp_path: Path) -> Any:
    store, projects, matcher, first, other, target, peer, pending, group = managed
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    def get(route: str) -> Any:
        with urlopen(root + route) as response:
            return json.load(response)
    token = get('/api/state')['token']
    def post(route: str, body: dict[str, Any], credential: str = token) -> Any:
        with urlopen(Request(root + '/api/' + route, data=json.dumps(body).encode(),
                     headers={'Content-Type': 'application/json', 'X-Workbench-Token': credential})) as response:
            return json.load(response)
    try:
        yield store, projects, first, other, target, peer, pending, root, get, post
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_rename_pin_status_delete_complete_project_lifecycle(project_web: Any) -> None:
    store, projects, first, other, target, peer, _, _, get, post = project_web
    originals, logistics = original_hashes(store), registry_state(projects)
    renamed = post('projects/rename', {'id': first['id'], 'name': 'Invoice-only new name', 'expected_revision': 0})
    assert renamed['name'] == 'Invoice-only new name' and renamed['project_revision'] == 1
    state = get('/api/state')
    assert next(item for item in state['items'] if item['id'] == target['id'])['project'] == renamed['name']
    assert next(row for row in state['projects'] if row['id'] == first['id']) == renamed
    pinned = post('projects/pin', {'id': first['id'], 'pinned': True, 'expected_revision': 1})
    assert pinned['pinned'] and pinned['project_revision'] == 2
    reimbursed = post('projects/reimbursement', {'id': first['id'], 'status': 'reimbursed', 'expected_revision': 0})
    assert reimbursed['project_revision'] == 3 and reimbursed['reimbursement_revision'] == 1
    returned = post('projects/reimbursement', {'id': first['id'], 'status': 'unreimbursed', 'expected_revision': 1})
    assert returned['project_revision'] == 4 and returned['reimbursement_revision'] == 2
    result = post('projects/delete-permanently', {'id': first['id'], 'expected_revision': 4,
        'confirmed': True, 'confirmation_name': returned['name']})
    assert result['deleted'] and result['id'] == first['id']
    state = get('/api/state')
    assert [row['id'] for row in state['projects']] == [other['id']]
    assert [item['id'] for item in state['items']] == [peer['id']]
    assert original_hashes(store) == originals and registry_state(projects) == logistics


@pytest.mark.parametrize('route,body', [
    ('projects/rename', {'name': 'New', 'expected_revision': 0}),
    ('projects/pin', {'pinned': True, 'expected_revision': 0}),
    ('projects/delete-permanently', {'confirmed': True, 'confirmation_name': 'Synthetic source', 'expected_revision': 0})])
def test_project_management_requires_workbench_token(project_web: Any, route: str, body: dict[str, Any]) -> None:
    store, _, first, _, _, _, _, _, _, post = project_web
    before = tables(store)
    with pytest.raises(HTTPError) as error:
        post(route, {**body, 'id': first['id']}, '')
    assert error.value.code == 403 and tables(store) == before


def test_specific_delete_confirmation_and_revision_conflicts_are_atomic(project_web: Any) -> None:
    store, _, first, _, _, _, _, _, _, post = project_web
    before = tables(store)
    for extra in ({}, {'confirmed': 'yes'}, {'confirmed': True, 'confirmation_name': 'wrong'},
                  {'confirmed': True, 'confirmation_name': first['name'], 'expected_revision': 99}):
        with pytest.raises(HTTPError) as error:
            post('projects/delete-permanently', {'id': first['id'], 'expected_revision': 0, **extra})
        assert error.value.code == 400
    assert tables(store) == before
    post('projects/pin', {'id': first['id'], 'pinned': True, 'expected_revision': 0})
    pinned = tables(store)
    with pytest.raises(HTTPError) as error:
        post('projects/rename', {'id': first['id'], 'name': 'Stale', 'expected_revision': 0})
    assert error.value.code == 400 and tables(store) == pinned


def test_new_project_cannot_duplicate_display_name_and_deletion_cannot_be_bypassed(project_web: Any) -> None:
    store, _, first, _, _, _, pending, _, get, post = project_web
    renamed = post('projects/rename', {'id': first['id'], 'name': 'Display label', 'expected_revision': 0})
    before = tables(store)
    with pytest.raises(HTTPError) as error:
        post('projects', {'name': renamed['name']})
    assert error.value.code == 400 and tables(store) == before
    post('projects/delete-permanently', {'id': first['id'], 'expected_revision': 1,
                                       'confirmed': True, 'confirmation_name': renamed['name']})
    deleted = tables(store)
    for route, body in [
        ('projects/rename', {'id': first['id'], 'name': 'Resurrection', 'expected_revision': 2}),
        ('projects/pin', {'id': first['id'], 'pinned': True, 'expected_revision': 2}),
        ('projects/reimbursement', {'id': first['id'], 'status': 'reimbursed', 'expected_revision': 0}),
        ('create', {'title': 'Bypass deleted project', 'project_id': first['id'], 'amount': '3'}),
        ('intake/classify', {'id': pending['id'], 'revision': pending['revision'], 'role': 'invoice', 'fields': {}}),
        ('material-matching/create-batch', {'project_id': first['id'], 'idempotency_key': 'old-batch-id', 'groups': []})]:
        with pytest.raises(HTTPError) as error:
            post(route, body)
        assert error.value.code == 400
    for route in ('/api/material-matching?project=', '/api/material-matching/suggestions?project='):
        with pytest.raises(HTTPError) as error:
            get(route + first['id'])
        assert error.value.code == 404
    assert tables(store) == deleted


def test_project_menu_assets_are_served_in_invoice_mode(project_web: Any) -> None:
    *_, root, get, post = project_web
    for name, content_type in [('project-menu.js', 'text/javascript'), ('project-menu.css', 'text/css')]:
        with urlopen(root + '/' + name) as response:
            assert response.headers['Content-Type'].startswith(content_type)
            assert response.read()
