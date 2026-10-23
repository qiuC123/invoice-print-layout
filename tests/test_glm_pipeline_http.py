from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout import workbench_web
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


class FakeAI:
    enabled = True

    def __init__(self, root: Path):
        self.root = root

    def public_status(self) -> dict[str, Any]:
        return {'enabled': True, 'available': True, 'model': 'glm-5.3-flash', 'message': '已启用'}


@pytest.fixture
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(workbench_web, 'GlmInvoiceAI', FakeAI)
    store = ExpenseStore(tmp_path)
    project = ExpenseProjects(store).registry.create_project('Synthetic GLM HTTP')['id']
    entry = IntakeQueue(store, ai=FakeAI(store.root)).receive(  # type: ignore[arg-type]
        'synthetic.pdf', make_invoice_pdf(tmp_path / 'proof.pdf', amount='50.00').read_bytes(), project_id=project)
    server = workbench_web.make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']

    def post(path: str, body: dict[str, Any], timeout: float = 3) -> dict[str, Any]:
        request = Request(root + '/api/' + path, json.dumps(body).encode(),
                          {'Content-Type': 'application/json', 'X-Workbench-Token': token})
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)  # type: ignore[no-any-return]

    try:
        yield root, post, project, entry, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_status_and_manual_field_protection(web: Any) -> None:
    root, post, project, entry, store = web
    with urlopen(root + '/api/invoice-ai') as response:
        status = json.load(response)
    assert status == {'enabled': True, 'available': True, 'model': 'glm-5.3-flash', 'message': '已启用'}
    with urlopen(root + '/api/material-matching?project=' + project) as response:
        assert json.load(response)['ai'] == status
    result = post('intake/classify', {'id': entry['id'], 'revision': entry['revision'], 'role': 'invoice',
                                    'fields': {'amount': '41.00', 'merchant': 'Synthetic human', 'project_id': project}})
    assert result['manual_role'] is True
    assert result['manual_fields'] == ['amount', 'merchant']
    assert result['fields']['amount'] == '41.00'
    assert not store.list_items()


@pytest.mark.parametrize('route', ['intake/prepare', 'intake/analyze'])
@pytest.mark.parametrize('flag', [None, False, True])
def test_explicit_ai_assistance_is_forwarded_only_when_requested(
        web: Any, monkeypatch: pytest.MonkeyPatch, route: str, flag: bool | None) -> None:
    _, post, _, entry, store = web
    calls: list[bool] = []

    def prepare(queue: IntakeQueue, key: str, *, force_ai: bool = False) -> dict[str, Any]:
        calls.append(force_ai)
        return queue.get(key)

    monkeypatch.setattr(IntakeQueue, 'prepare_batch_entry', prepare)
    body = {'id': entry['id'], **({'force_ai': flag} if flag is not None else {})}
    post(route, body)
    assert calls == [flag is True]
    assert not store.list_items()


@pytest.mark.parametrize('flag', [None, 1, 'true', [], {}])
def test_invalid_ai_assistance_flag_does_not_start_recognition(
        web: Any, monkeypatch: pytest.MonkeyPatch, flag: Any) -> None:
    _, post, _, entry, store = web
    calls: list[str] = []

    def prepare(queue: IntakeQueue, key: str, **_options: Any) -> dict[str, Any]:
        calls.append(key)
        return queue.get(key)

    monkeypatch.setattr(IntakeQueue, 'prepare_batch_entry', prepare)
    with pytest.raises(HTTPError) as error:
        post('intake/prepare', {'id': entry['id'], 'force_ai': flag})
    assert error.value.code == 400
    assert '布尔值' in json.load(error.value)['error']
    assert calls == [] and not store.list_items()


@pytest.mark.parametrize('path', ['intake/prepare', 'intake/analyze', 'material-matching/auto', 'mail'])
def test_model_wait_does_not_block_edits_and_duplicate_request_is_refused(
        web: Any, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    _, post, project, entry, store = web
    entered, release = threading.Event(), threading.Event()

    def prepare(_self: Any, key: str) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return _self.get(key)

    def automatic(_self: Any, key: str) -> dict[str, Any]:
        entered.set()
        assert release.wait(5)
        return {'matched': 0, 'suggested': 0, 'unmatched': 1, 'items': [], 'issues': [], 'conflicts': []}

    monkeypatch.setattr(IntakeQueue, 'prepare_batch_entry', prepare)
    monkeypatch.setattr(workbench_web.MaterialMatching, 'suggest', automatic)
    def receive_mail(_self: Any, _project: str) -> list[Any]:
        return []

    def sync(_store: Any, *, organize: bool = True) -> dict[str, Any]:
        assert organize is False
        return {'message': 'Synthetic mail collected'}

    monkeypatch.setattr(workbench_web, 'sync_mail', sync)
    monkeypatch.setattr(IntakeQueue, 'import_mail', receive_mail)
    if path == 'mail':
        with store.connect() as database:
            queued = json.loads(database.execute('SELECT data FROM intake_queue WHERE id=?', (entry['id'],)).fetchone()['data'])
            queued['source'] = 'mail'
            database.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(queued), entry['id']))
    body = {'project_id': project} if path.endswith('/auto') else {'id': entry['id']}
    results: list[Any] = []

    def run() -> None:
        try:
            results.append(post(path, body, timeout=6))
        except Exception as error:
            results.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(HTTPError) as error:
            post(path, body)
        assert error.value.code in {400, 409}
        assert '没有重复执行' in json.load(error.value)['error']
        changed = post('intake/classify', {'id': entry['id'], 'revision': entry['revision'], 'role': 'invoice',
                                         'fields': {'amount': '42.00'}})
        assert changed['fields']['amount'] == '42.00'
    finally:
        release.set()
        thread.join(5)
    assert len(results) == 1 and isinstance(results[0], dict)
    assert not store.list_items()
