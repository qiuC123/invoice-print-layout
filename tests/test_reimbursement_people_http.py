"""Authenticated person attribution keeps amounts and source originals independent."""
from __future__ import annotations
import base64
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
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server

@pytest.fixture
def attribution_web(tmp_path: Path) -> Any:
    store = ExpenseStore(tmp_path)
    projects = ExpenseProjects(store)
    project = projects.registry.create_project('Synthetic shared project')['id']
    other = projects.registry.create_project('Other synthetic project')['id']
    expense = store.create({'project_id': project, 'title': 'One actual expense', 'category': '酒店', 'amount': '12.30'})
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    def get(path: str) -> Any:
        with urlopen(root + path) as response: return json.load(response)
    token = get('/api/state')['token']
    def post(path: str, body: dict[str, Any], credential: str = token) -> Any:
        with urlopen(Request(root + '/api/' + path, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': credential})) as response:
            return json.load(response)
    try:
        yield store, project, other, expense, get, post
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

def test_attribution_and_conserved_summary_http(attribution_web: Any) -> None:
    store, project, other, expense, get, post = attribution_web
    person = post('reimbursement-people/create', {'project_id': project, 'label': 'Synthetic payer', 'external_sender_id': 'stable-source-a'})['person']
    before = store.get(expense['id'])
    response = post('reimbursement-people/assign-expense', {'id': expense['id'], 'expected_version': before['version'], 'person_id': person['id']})
    item = response['item']
    assert item['reimbursement_person_id'] == person['id']
    assert item['reimbursement_person_label'] == 'Synthetic payer'
    assert (item['amount_cents'], item['category'], item['stage'], item['verified']) == (1230, before['category'], before['stage'], before['verified'])
    result = get('/api/reimbursement-people?project=' + project)
    assert 'settlement' not in result
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='reimbursement_settlement'").fetchone()
    own = next(row for row in result['summary']['rows'] if row['person_id'] == person['id'])
    assert own['total_cents'] == own['pending_review_cents'] == 1230
    assert result['summary']['total_cents'] == 1230
    assert get('/api/reimbursement-people?project=' + other)['summary']['total_cents'] == 0
    with pytest.raises(HTTPError) as stale:
        post('reimbursement-people/assign-expense', {'id': expense['id'], 'expected_version': before['version'], 'person_id': ''})
    assert stale.value.code == 400 and store.get(expense['id'])['reimbursement_person_id'] == person['id']

def test_material_assignment_does_not_create_expense_or_replace_source(attribution_web: Any) -> None:
    store, project, _, _, get, post = attribution_web
    person = post('reimbursement-people/create', {'project_id': project, 'label': 'Source owner'})['person']
    with pymupdf.open() as document:
        document.new_page().insert_text((60, 100), 'SYNTHETIC ORIGINAL')
        payload = document.tobytes()
    entry = post('intake/receive', {'project_id': project, 'name': 'Original source.pdf', 'batch': 'stable-source', 'data': base64.b64encode(payload).decode()})
    before = len(store.list_items())
    post('reimbursement-people/assign-materials', {'project_id': project, 'ids': [entry['id']], 'revisions': {entry['id']: entry['revision']}, 'person_id': person['id']})
    received = next(row for row in get('/api/intake')['queue'] if row['id'] == entry['id'])
    assert received['reimbursement_person_id'] == person['id']
    assert received['name'] == entry['name'] and received['batch'] == 'stable-source'
    assert received['digest'] == hashlib.sha256(payload).hexdigest()
    assert not received['item_id'] and len(store.list_items()) == before

def test_cross_project_person_cannot_be_saved_or_forged(attribution_web: Any) -> None:
    store, project, other, expense, _, post = attribution_web
    person = post('reimbursement-people/create', {'project_id': other, 'label': 'Other project payer'})['person']
    original = store.get(expense['id'])
    with pytest.raises(HTTPError) as invalid:
        post('reimbursement-people/assign-expense', {'id': expense['id'], 'expected_version': original['version'], 'person_id': person['id']})
    assert invalid.value.code == 400 and store.get(expense['id'])['version'] == original['version']
    with pytest.raises(ValueError, match='当前项目'):
        store.create({'project_id': project, 'title': 'Invalid attribution', 'amount': '5', 'reimbursement_person_id': person['id']})
    assert len(store.list_items()) == 1

def test_project_permanent_deletion_removes_only_its_person_registry(attribution_web: Any) -> None:
    store, project, other, _, get, post = attribution_web
    person = post('reimbursement-people/create', {'project_id': project, 'label': 'To remove'})['person']
    retained = post('reimbursement-people/create', {'project_id': other, 'label': 'Retained payer'})['person']
    current = next(row for row in get('/api/state')['projects'] if row['id'] == project)
    post('projects/delete-permanently', {'id': project, 'expected_revision': current['project_revision'],
                                      'confirmed': True, 'confirmation_name': current['name']})
    with store.connect() as db:
        assert not db.execute('SELECT 1 FROM reimbursement_people WHERE id=?', (person['id'],)).fetchone()
        assert db.execute('SELECT 1 FROM reimbursement_people WHERE id=?', (retained['id'],)).fetchone()
    assert get('/api/reimbursement-people?project=' + other)['people'][0]['id'] == retained['id']
    with pytest.raises(HTTPError) as deleted:
        post('reimbursement-people/create', {'project_id': project, 'label': 'Cannot revive'})
    assert deleted.value.code == 400

def test_unassigned_legacy_projection_does_not_invalidate_a_noop_update(attribution_web: Any) -> None:
    store, project, _, expense, get, _ = attribution_web
    with pymupdf.open() as document:
        document.new_page().insert_text((60, 100), 'SYNTHETIC HOTEL INVOICE')
        payload = document.tobytes()
    store.add_attachment(expense['id'], 'Hotel invoice.pdf', payload, 'invoice')
    store.transition(expense['id'], 'verify')
    with store.connect() as db:
        original = db.execute('SELECT data FROM expenses WHERE id=?', (expense['id'],)).fetchone()['data']
    current = store.get(expense['id'])
    assert current['ready'] and not current.get('reimbursement_person_id')
    get('/api/reimbursement-people?project=' + project)
    with store.connect() as db:
        assert db.execute('SELECT data FROM expenses WHERE id=?', (expense['id'],)).fetchone()['data'] == original
    updated = store.update(expense['id'], {**current})
    assert updated['ready'] and updated['verified'] and updated['amount_cents'] == 1230

@pytest.mark.parametrize('route,extra', [
    ('create', {'label': 'Blocked payer'}), ('assign-materials', {'ids': [], 'revisions': {}, 'person_id': ''}),
    ('assign-expense', {'id': 'unknown', 'expected_version': '', 'person_id': ''})])
def test_person_mutations_require_existing_workbench_token(attribution_web: Any, route: str, extra: dict[str, Any]) -> None:
    _, project, _, _, get, post = attribution_web
    before = get('/api/reimbursement-people?project=' + project)
    with pytest.raises(HTTPError) as denied:
        post('reimbursement-people/' + route, {'project_id': project, **extra}, '')
    assert denied.value.code == 403
    assert get('/api/reimbursement-people?project=' + project) == before
