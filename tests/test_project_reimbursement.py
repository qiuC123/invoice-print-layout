from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf
import pytest

from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server


@pytest.fixture
def projects(tmp_path: Path) -> ExpenseProjects:
    directory = ExpenseProjects(ExpenseStore(tmp_path))
    directory.registry.create_project('Synthetic source', 'Synthetic site')
    directory.registry.create_project('Synthetic other')
    return directory


def unchanged_business(directory: ExpenseProjects) -> dict[str, Any]:
    with directory.store.connect() as db:
        names = [row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                 "AND name NOT IN ('expense_project_reimbursement','expense_project_metadata') ORDER BY name")]
        tables = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                  for table in names}
    return {'tables': tables, 'projects': directory.registry.list(),
            'registry_sha256': hashlib.sha256(directory.registry.path.read_bytes()).hexdigest(),
            'originals': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in directory.store.files.iterdir() if path.is_file()}}


def test_default_unreimbursed_without_deriving_from_expense_stage_and_round_trip(projects: ExpenseProjects) -> None:
    raw = projects.registry.list()
    listed = projects.list()
    assert listed == [{**project, 'reimbursement_status': 'unreimbursed', 'reimbursement_revision': 0,
                      'pinned': False, 'project_revision': 0, 'start_date': '', 'end_date': ''} for project in raw]
    first = raw[0]
    item = projects.store.create({'title': 'Closed expense', 'amount': '12.34', 'project_id': first['id'],
                                  'material_basis': 'payment_only'})
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((30, 40), 'Synthetic payment proof')
        item = projects.store.add_attachment(item['id'], 'payment.pdf', pdf.tobytes(), 'payment')
    for action in ['verify', 'submitted', 'reimbursed']:
        item = projects.store.transition(item['id'], action)
    assert projects.list()[0]['reimbursement_status'] == 'unreimbursed'
    business = unchanged_business(projects)
    saved = projects.set_reimbursement_status(first['id'], 'reimbursed', 0)
    assert saved == {**first, 'reimbursement_status': 'reimbursed', 'reimbursement_revision': 1,
                     'pinned': False, 'project_revision': 1, 'start_date': '', 'end_date': ''}
    assert unchanged_business(projects) == business and projects.store.get(item['id']) == item
    restarted = ExpenseProjects(ExpenseStore(projects.store.workspace))
    assert restarted.list()[0] == saved
    returned = restarted.set_reimbursement_status(first['id'], 'unreimbursed', 1)
    assert returned == {**first, 'reimbursement_status': 'unreimbursed', 'reimbursement_revision': 2,
                        'pinned': False, 'project_revision': 2, 'start_date': '', 'end_date': ''}
    assert projects.list()[1] == listed[1]
    assert unchanged_business(projects) == business and projects.store.get(item['id']) == item


def test_marking_project_does_not_submit_or_verify_its_draft(projects: ExpenseProjects) -> None:
    first = projects.list()[0]
    draft = projects.store.create({'title': 'Still a draft', 'amount': '14', 'project_id': first['id']})
    business = unchanged_business(projects)
    projects.set_reimbursement_status(first['id'], 'reimbursed', 0)
    assert projects.store.get(draft['id']) == draft
    assert draft['stage'] == 'draft' and not draft['verified']
    assert unchanged_business(projects) == business


def test_same_status_same_revision_is_side_effect_free_and_stale_retry_is_rejected(projects: ExpenseProjects) -> None:
    first = projects.list()[0]
    assert projects.set_reimbursement_status(first['id'], 'unreimbursed', 0) == first
    with projects.store.connect() as db:
        assert not list(db.execute('SELECT * FROM expense_project_reimbursement'))
    saved = projects.set_reimbursement_status(first['id'], 'reimbursed', 0)
    with projects.store.connect() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM expense_project_reimbursement')]
    assert projects.set_reimbursement_status(first['id'], 'reimbursed', 1) == saved
    with pytest.raises(ValueError, match='已变化'):
        projects.set_reimbursement_status(first['id'], 'reimbursed', 0)
    with projects.store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM expense_project_reimbursement')] == before


@pytest.mark.parametrize('status', ['', 'draft', 'submitted', 'unknown', True, None, ['reimbursed']])
def test_invalid_status_does_not_touch_business(projects: ExpenseProjects, status: Any) -> None:
    business = unchanged_business(projects)
    with pytest.raises(ValueError, match='分类无效'):
        projects.set_reimbursement_status(projects.list()[0]['id'], status, 0)
    assert unchanged_business(projects) == business


@pytest.mark.parametrize('revision', [None, '', '0', 0.0, -1, True, False])
def test_invalid_revision_requires_an_explicit_integer(projects: ExpenseProjects, revision: Any) -> None:
    business = unchanged_business(projects)
    with pytest.raises(ValueError, match='版本'):
        projects.set_reimbursement_status(projects.list()[0]['id'], 'reimbursed', revision)
    assert unchanged_business(projects) == business


@pytest.mark.parametrize('project_id', ['missing', '', None, ['missing']])
def test_unknown_or_invalid_project_is_rejected(projects: ExpenseProjects, project_id: Any) -> None:
    business = unchanged_business(projects)
    with pytest.raises(ValueError, match='项目'):
        projects.set_reimbursement_status(project_id, 'reimbursed', 0)
    assert unchanged_business(projects) == business


def test_concurrent_writers_with_same_revision_have_one_winner(projects: ExpenseProjects) -> None:
    barrier = threading.Barrier(2)
    project_id = projects.list()[0]['id']
    business = unchanged_business(projects)

    def mark() -> str:
        barrier.wait(timeout=5)
        try:
            projects.set_reimbursement_status(project_id, 'reimbursed', 0)
            return 'saved'
        except ValueError as exc:
            assert '已变化' in str(exc)
            return 'stale'

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: mark(), range(2))) == ['saved', 'stale']
    assert projects.list()[0]['reimbursement_revision'] == 1
    assert unchanged_business(projects) == business


def test_manual_project_reimbursement_http_security_validation_and_persistence(projects: ExpenseProjects) -> None:
    project_id = projects.list()[0]['id']
    server = make_server(projects.store.workspace, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        initial = json.load(response)
    token = initial['token']
    business = unchanged_business(projects)

    def post(body: dict[str, Any], *, supplied_token: str = token, origin: str | None = None) -> dict[str, Any]:
        headers = {'Content-Type': 'application/json', 'X-Workbench-Token': supplied_token}
        if origin:
            headers['Origin'] = origin
        with urlopen(Request(root + '/api/projects/reimbursement', data=json.dumps(body).encode(), headers=headers)) as response:
            return json.load(response)

    request = {'id': project_id, 'status': 'reimbursed', 'expected_revision': 0}
    try:
        for security in [{'supplied_token': ''}, {'supplied_token': 'wrong'}, {'origin': 'https://outside.invalid'}]:
            with pytest.raises(HTTPError) as error:
                post(request, **security)
            assert error.value.code == 403
        for body in [{key: value for key, value in request.items() if key != 'expected_revision'},
                     {**request, 'status': 'draft'}, {**request, 'expected_revision': True},
                     {**request, 'expected_revision': 99}, {**request, 'id': 'unknown'}]:
            with pytest.raises(HTTPError) as error:
                post(body)
            assert error.value.code == 400
        saved = post(request, origin=root)
        assert saved['id'] == project_id and saved['reimbursement_status'] == 'reimbursed'
        assert saved['reimbursement_revision'] == 1 and saved['status'] == 'draft'
        assert post({**request, 'expected_revision': 1}) == saved
        with urlopen(root + '/api/state') as response:
            current = json.load(response)
        assert current['projects'][0] == saved
        assert current['projects'][1]['reimbursement_status'] == 'unreimbursed'
        assert unchanged_business(projects) == business
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert ExpenseProjects(ExpenseStore(projects.store.workspace)).list()[0] == saved
    assert ProjectRegistry(projects.registry.path).list() == business['projects']
