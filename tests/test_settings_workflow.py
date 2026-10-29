"""Synthetic settings, organizer and mailbox boundaries without a real vault or IMAP."""
from __future__ import annotations

from http.client import HTTPConnection
import itertools
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from invoice_print_layout import glm_invoice_ai, mail163, mail_agent, phone_link, reliability, workbench_web
from invoice_print_layout.agent_jobs import AgentService
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_settings import WorkbenchSettings
from tests.test_invoice_agent import proof
from tests.test_mail_agent import message


AUTH = 'synthetic-auth-private-1234'
OTHER_AUTH = 'synthetic-other-private-5678'
ADDRESS = 'synthetic-settings@163.com'


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.token = ''
        self.cookie = ''

    def request(self, route: str, body: Any = None, **headers: str) -> tuple[int, Any]:
        connection = HTTPConnection('127.0.0.1', self.port, timeout=15)
        options = {'Content-Type': 'application/json', 'X-Workbench-Token': self.token,
                   'Cookie': self.cookie, 'Origin': f'http://127.0.0.1:{self.port}', **headers}
        connection.request('GET' if body is None else 'POST', route,
                           json.dumps(body).encode() if body is not None else None, options)
        response = connection.getresponse()
        self.cookie = response.getheader('Set-Cookie') or self.cookie
        raw = response.read()
        status = response.status
        mime = response.getheader('Content-Type', '')
        connection.close()
        return status, json.loads(raw) if mime.startswith('application/json') else raw


class FakeImap:
    def __init__(self, owner: FakeFactory) -> None:
        self.owner = owner

    def login(self, address: str, code: str) -> tuple[str, list[bytes]]:
        self.owner.calls.append(('login', address, code))
        if self.owner.error:
            raise OSError('synthetic transport echoed ' + code)
        return 'OK', [b'connected']

    def xatom(self, name: str, *args: str) -> tuple[str, list[bytes]]:
        self.owner.calls.append(('xatom', name, *args))
        return 'OK', [b'ID accepted']

    def select(self, mailbox: str = 'INBOX', readonly: bool = False) -> tuple[str, list[bytes]]:
        self.owner.calls.append(('select', mailbox, readonly))
        return 'OK', [b'0']

    def uid(self, command: str, *args: Any) -> Any:
        pytest.fail('connection test searched or downloaded mail')

    def logout(self) -> tuple[str, list[bytes]]:
        self.owner.calls.append(('logout',))
        return 'BYE', [b'done']


class FakeFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error = False

    def __call__(self, host: str, port: int, **kwargs: Any) -> FakeImap:
        self.calls.append(('connect', host, port, kwargs))
        return FakeImap(self)


@pytest.fixture
def secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    vault: dict[str, str] = {}
    writes: list[tuple[str, str]] = []
    reads: list[str] = []

    def read(address: str) -> str:
        reads.append(address)
        if address not in vault:
            raise mail163.MailImportError('synthetic missing authorization')
        return vault[address]

    def save(address: str, code: str) -> None:
        if not isinstance(code, str) or not 6 <= len(code) <= 64 or any(char.isspace() for char in code):
            raise mail163.MailImportError('synthetic invalid authorization')
        writes.append((address, code))
        vault[address] = code

    monkeypatch.setattr(mail163, 'read_auth_code', read)
    monkeypatch.setattr(mail163, 'save_auth_code', save)
    monkeypatch.setattr(mail_agent, 'read_auth_code', read)
    monkeypatch.setattr(mail163.keyring, 'get_password', lambda *args: pytest.fail('real keyring read'))
    monkeypatch.setattr(mail163.keyring, 'set_password', lambda *args: pytest.fail('real keyring write'))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'synthetic-home'))
    monkeypatch.delenv('GLM_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    return SimpleNamespace(vault=vault, writes=writes, reads=reads)


@pytest.fixture
def web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secrets: Any) -> Any:
    available = [False]
    monkeypatch.setattr(glm_invoice_ai.GlmInvoiceAI, 'public_status', lambda self: {
        'enabled': available[0], 'available': available[0], 'model': 'synthetic-model', 'message': 'synthetic'})
    monkeypatch.setattr(AgentService, 'kick', lambda self: None)
    monkeypatch.setattr(mail_agent.MailAgentService, 'kick', lambda self: None)
    imap = FakeFactory()
    server = workbench_web.make_server(tmp_path, 0, mode='invoice', imap_factory=imap)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server.server_port)
    client.token = client.request('/api/state')[1]['token']
    store = ExpenseStore(tmp_path)
    project = ExpenseProjects(store).create('Synthetic settings project')['id']
    try:
        yield SimpleNamespace(client=client, store=store, project=project, imap=imap,
                              available=available, secrets=secrets, workspace=tmp_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def mail_body(web: Any, **changes: Any) -> dict[str, Any]:
    current = web.client.request('/api/settings')[1]['mail']
    return {'address': ADDRESS, 'folder': 'INBOX', 'auth_code': AUTH, 'enabled': False,
            'interval_seconds': 120, 'since': current['approved_since'], 'default_project_id': '', **changes}


def mail_test_body(web: Any, **changes: Any) -> dict[str, Any]:
    return {key: value for key, value in mail_body(web, **changes).items()
            if key in {'address', 'folder', 'auth_code'}}


def file_snapshot(workspace: Path) -> dict[str, bytes]:
    return {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob('*') if path.is_file()}


def test_organize_defaults_partial_atomic_save_and_readback(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    service = OrganizeSettings(tmp_path)
    assert service.read() == {'auto_after_upload': True, 'use_model': True}
    assert not service.path.exists()
    replaces: list[tuple[Path, Path]] = []
    real_replace = reliability.os.replace

    def replace(source: Any, destination: Any) -> None:
        source, destination = Path(source), Path(destination)
        assert source != destination and json.loads(source.read_text(encoding='utf-8'))['version'] == 1
        replaces.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(reliability.os, 'replace', replace)
    assert service.update({'auto_after_upload': False}) == {'auto_after_upload': False, 'use_model': True}
    assert OrganizeSettings(tmp_path).update({'use_model': False}) == {'auto_after_upload': False, 'use_model': False}
    assert len(replaces) == 2 and not list(tmp_path.glob('*.tmp'))
    assert json.loads(service.path.read_text(encoding='utf-8')) == {
        'version': 1, 'auto_after_upload': False, 'use_model': False}


@pytest.mark.parametrize('bad', [None, 0, 1, 'true', [], {}])
def test_organize_rejects_non_boolean_updates_without_writing(tmp_path: Path, bad: Any) -> None:
    service = OrganizeSettings(tmp_path)
    for key in ('auto_after_upload', 'use_model'):
        with pytest.raises(ValueError):
            service.update({key: bad})
    with pytest.raises(ValueError):
        service.effective_model(bad, False)
    assert not service.path.exists()


@pytest.mark.parametrize('requested,global_model,available', list(itertools.product((False, True), repeat=3)))
def test_manual_jobs_obey_requested_global_and_available(web: Any, requested: bool,
                                                       global_model: bool, available: bool) -> None:
    web.available[0] = available
    code, organize = web.client.request('/api/settings/organize', {'use_model': global_model})
    assert code == 200 and organize['use_model'] is global_model
    entry = IntakeQueue(web.store).receive('synthetic.pdf', proof('合成收据\n实付：17.00'), project_id=web.project)
    code, result = web.client.request('/api/agent/start', {
        'project_id': web.project, 'material_ids': [entry['id']],
        'idempotency_key': 'synthetic-settings-agent-request', 'use_model': requested})
    assert code == 200
    assert result['job']['use_model'] is (requested and global_model and available)
    assert result['job']['state'] == 'queued'


@pytest.mark.parametrize('available,global_model', list(itertools.product((False, True), repeat=2)))
@pytest.mark.parametrize('bad', [None, 0, 1, 'true', [], {}])
def test_manual_job_bad_bool_not_masked_by_disabled_or_missing_model(web: Any, available: bool,
                                                                  global_model: bool, bad: Any) -> None:
    web.available[0] = available
    OrganizeSettings(web.store.root).update({'use_model': global_model})
    entry = IntakeQueue(web.store).receive('synthetic.pdf', proof('合成收据\n实付：17.00'), project_id=web.project)
    code, result = web.client.request('/api/agent/start', {
        'project_id': web.project, 'material_ids': [entry['id']],
        'idempotency_key': 'synthetic-invalid-bool-request', 'use_model': bad})
    assert code == 400 and 'error' in result
    assert AgentService(web.store).jobs.list(web.project) == []


def test_legacy_import_http_entry_is_removed(web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(self: ExpenseStore) -> None:
        pytest.fail('Removed HTTP entry must not import history')

    monkeypatch.setattr(ExpenseStore, 'import_history', forbidden)
    before = file_snapshot(web.workspace)
    code, result = web.client.request('/api/import', {})
    assert code == 400 and result == {'error': '操作不存在'}
    assert file_snapshot(web.workspace) == before


def test_settings_expose_shared_organize_status_and_projects(web: Any) -> None:
    web.available[0] = True
    code, result = web.client.request('/api/settings')
    assert code == 200
    assert result['organize']['auto_after_upload'] and result['organize']['use_model']
    assert result['organize']['model_available']
    selected = next(row for row in result['providers'] if row['id'] == result['active'])
    assert result['organize']['model_label'] == selected['name'] + ' · synthetic-model'
    assert {'id': web.project, 'name': 'Synthetic settings project'} in result['mail']['projects']
    assert result['mail']['auth_saved'] is False
    code, result = web.client.request('/api/settings/organize', {'auto_after_upload': False, 'use_model': False})
    assert code == 200 and not result['auto_after_upload'] and not result['use_model']
    assert web.client.request('/api/agent/mail')[1]['use_model'] is False
    assert web.client.request('/api/settings')[1]['organize']['auto_after_upload'] is False


def test_organize_model_label_is_empty_when_model_unavailable(web: Any) -> None:
    code, result = web.client.request('/api/settings')
    assert code == 200 and result['organize']['model_available'] is False
    assert result['organize']['model_label'] == ''


def test_organize_model_label_preserves_other_providers_key_mask(tmp_path: Path, secrets: Any) -> None:
    store = ExpenseStore(tmp_path)
    model = 'workbuddy/' + AUTH
    config = {'version': 2, 'enabled': True, 'active': 'workbuddy', 'providers': [
        {'id': 'workbuddy', 'name': 'Synthetic gateway', 'kind': 'workbuddy',
         'base_url': 'http://127.0.0.1:3425/v1', 'model': model},
        {'id': 'inactive', 'name': 'Inactive provider', 'kind': 'openai',
         'base_url': 'https://synthetic.example/v1', 'model': 'synthetic-model'}]}
    (store.root / 'invoice-ai.json').write_text(json.dumps(config), encoding='utf-8')
    (store.root / 'invoice-ai-keys.json').write_text(json.dumps({'inactive': AUTH}), encoding='utf-8')
    ai: Any = SimpleNamespace(public_status=lambda: {'available': True, 'model': model})
    mail: Any = SimpleNamespace()
    status = WorkbenchSettings(store, mail, ai).organize_status()
    assert status['model_available']
    assert status['model_label'] == 'Synthetic gateway · workbuddy/••••'
    assert AUTH not in json.dumps(status)


@pytest.mark.parametrize('global_model,available', list(itertools.product((False, True), repeat=2)))
def test_mail_dispatch_obeys_global_model_and_actual_availability(web: Any, global_model: bool,
                                                                 available: bool) -> None:
    web.available[0] = available
    web.secrets.vault[ADDRESS] = AUTH
    mail163.write_mail_settings(web.workspace / 'mail163.toml', mail163.Mail163Settings(ADDRESS))
    agent = AgentService(web.store)
    remote = message(1, 'Synthetic settings project invoice')
    service = mail_agent.MailAgentService(web.store, agent, fetcher=lambda *args, **kwargs: {
        'uidvalidity': '100', 'messages': [remote], 'more': False})
    service.configure({'enabled': True, 'default_project_id': web.project})
    OrganizeSettings(web.store.root).update({'use_model': global_model})
    try:
        service._run(True)
        jobs = agent.jobs.list(web.project)
        assert len(jobs) == 1
        assert jobs[0]['use_model'] is (global_model and available)
        assert service.status()['use_model'] is global_model
    finally:
        service.close()
        agent.close()


def test_legacy_mail_model_configuration_updates_global_setting(web: Any) -> None:
    body = {'enabled': False, 'use_model': False}
    code, result = web.client.request('/api/agent/mail/configure', body)
    assert code == 200 and not result['use_model']
    assert OrganizeSettings(web.store.root).read()['use_model'] is False
    saved = json.loads((web.store.root / 'mail-agent.json').read_text(encoding='utf-8'))
    assert 'use_model' not in saved
    web.client.request('/api/settings/organize', {'use_model': True})
    assert web.client.request('/api/agent/mail')[1]['use_model'] is True


def test_save_mail_uses_private_vault_and_does_not_echo_authorization(web: Any) -> None:
    body = mail_body(web, default_project_id=web.project)
    code, result = web.client.request('/api/settings/mail', body)
    assert code == 200 and result['address'] == ADDRESS and result['folder'] == 'INBOX'
    assert result['auth_saved'] and result['default_project_id'] == web.project
    assert web.secrets.writes == [(ADDRESS, AUTH)]
    assert web.imap.calls == []
    stored = mail163.read_mail_settings(web.workspace / 'mail163.toml')
    assert stored == mail163.Mail163Settings(ADDRESS)
    all_responses = [result, web.client.request('/api/settings')[1], web.client.request('/api/state')[1],
                     web.client.request('/api/agent/mail')[1]]
    assert AUTH not in json.dumps(all_responses)
    assert all(AUTH.encode() not in data for data in file_snapshot(web.workspace).values())
    body.update(auth_code='', folder='Archive', interval_seconds=180)
    code, result = web.client.request('/api/settings/mail', body)
    assert code == 200 and result['folder'] == 'Archive' and result['interval_seconds'] == 180
    assert web.secrets.writes == [(ADDRESS, AUTH)] and result['auth_saved']


def test_changed_address_requires_its_own_saved_or_new_authorization(web: Any) -> None:
    assert web.client.request('/api/settings/mail', mail_body(web))[0] == 200
    body = mail_body(web, address='other-settings@163.com', auth_code='')
    before = file_snapshot(web.workspace)
    code, result = web.client.request('/api/settings/mail', body)
    assert code == 400 and result['error'] == '请填写这个邮箱的客户端授权码'
    assert file_snapshot(web.workspace) == before
    assert web.secrets.writes == [(ADDRESS, AUTH)]
    web.secrets.vault['other-settings@163.com'] = OTHER_AUTH
    code, result = web.client.request('/api/settings/mail', body)
    assert code == 200 and result['address'] == 'other-settings@163.com' and result['auth_saved']
    assert OTHER_AUTH not in json.dumps(result)


@pytest.mark.parametrize('changes', [
    {'address': 'wrong@example.invalid'}, {'folder': 'invalid folder'}, {'enabled': 1},
    {'interval_seconds': 59}, {'interval_seconds': 3601}, {'interval_seconds': True},
    {'since': '2000-01-01'}, {'since': 'not-date'}, {'default_project_id': 'missing'},
])
def test_invalid_mail_save_cannot_write_vault_or_files(web: Any, changes: dict[str, Any]) -> None:
    body = mail_body(web, **changes)
    before = file_snapshot(web.workspace)
    code, result = web.client.request('/api/settings/mail', body)
    assert code == 400
    assert AUTH not in json.dumps(result)
    assert not web.secrets.writes and file_snapshot(web.workspace) == before
    assert web.imap.calls == []


def test_mail_connection_only_login_id_readonly_select_logout_and_no_persistence(web: Any) -> None:
    body = mail_test_body(web, address='preview-settings@163.com', folder='Preview')
    before = file_snapshot(web.workspace)
    code, result = web.client.request('/api/settings/mail/test', body)
    assert code == 200 and result['ok'] and set(result) == {'ok', 'message'}
    assert [call[0] for call in web.imap.calls] == ['connect', 'login', 'xatom', 'select', 'logout']
    assert web.imap.calls[0][1:3] == ('imap.163.com', 993)
    assert web.imap.calls[0][3]['timeout'] == 30 and 'ssl_context' in web.imap.calls[0][3]
    assert web.imap.calls[1] == ('login', 'preview-settings@163.com', AUTH)
    assert web.imap.calls[2][1] == 'ID'
    assert web.imap.calls[3] == ('select', 'Preview', True)
    assert AUTH not in json.dumps(result)
    assert not web.secrets.writes and file_snapshot(web.workspace) == before


def test_mail_connection_blank_authorization_reuses_vault_without_saving(web: Any) -> None:
    web.secrets.vault[ADDRESS] = AUTH
    before = file_snapshot(web.workspace)
    code, result = web.client.request('/api/settings/mail/test', mail_test_body(web, auth_code=''))
    assert code == 200 and result['ok']
    assert web.imap.calls[1] == ('login', ADDRESS, AUTH)
    assert not web.secrets.writes and file_snapshot(web.workspace) == before


def test_mail_connection_transport_echo_never_exposes_authorization(web: Any) -> None:
    web.imap.error = True
    before = file_snapshot(web.workspace)
    code, result = web.client.request('/api/settings/mail/test', mail_test_body(web))
    assert code in {200, 400} and result.get('ok', False) is False
    assert AUTH not in json.dumps(result) and 'synthetic transport echoed' not in json.dumps(result)
    assert [call[0] for call in web.imap.calls] == ['connect', 'login', 'logout']
    assert not web.secrets.writes and file_snapshot(web.workspace) == before


@pytest.mark.parametrize('route', ['/api/settings/organize', '/api/settings/mail', '/api/settings/mail/test'])
def test_workflow_settings_keep_host_origin_and_token_guards(web: Any, route: str) -> None:
    body = {'use_model': False} if route.endswith('/organize') else mail_body(web)
    before = file_snapshot(web.workspace)
    for headers in ({'Host': 'foreign.test'}, {'Origin': 'http://foreign.test'}, {'X-Workbench-Token': 'wrong'}):
        code, result = web.client.request(route, body, **headers)
        assert code == 403 and AUTH not in json.dumps(result)
    assert not web.secrets.writes and web.imap.calls == []
    assert file_snapshot(web.workspace) == before


def test_paired_phone_cannot_use_organize_or_mail_settings(web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    original = phone_link.lan_address
    monkeypatch.setattr(phone_link, 'lan_address', lambda address: address == '127.0.0.1' or original(address))
    monkeypatch.setattr(phone_link, 'addresses', lambda: [{'address': '127.0.0.1', 'label': 'Synthetic LAN'}])
    server_class = phone_link.ThreadingHTTPServer
    monkeypatch.setattr(phone_link, 'ThreadingHTTPServer', lambda address, handler: server_class((address[0], 0), handler))
    code, connection = web.client.request('/api/phone-link/start', {'address': '127.0.0.1'})
    assert code == 200
    phone = Client(connection['port'])
    assert phone.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]})[0] == 200
    phone.token = phone.request('/api/state')[1]['token']
    assert phone.request('/api/settings')[0] == 404
    for route in ('/api/settings/organize', '/api/settings/mail', '/api/settings/mail/test'):
        assert phone.request(route)[0] == 404
        assert phone.request(route, {'auth_code': AUTH})[0] == 403
    assert not web.secrets.writes and not web.imap.calls
