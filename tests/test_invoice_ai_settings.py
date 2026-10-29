from __future__ import annotations

import base64
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import threading
from typing import Any

from PIL import Image
import pytest

from invoice_print_layout import glm_invoice_ai as glm
from invoice_print_layout import invoice_ai_settings as settings
from invoice_print_layout import phone_link
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.test_glm_invoice_ai import Opener, Response


PAGE_KEY = 'synthetic-page-secret-1234'
FILE_KEY = 'synthetic-file-secret-5678'
ENV_KEY = 'synthetic-env-secret-9012'


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    monkeypatch.delenv('GLM_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)


def write_config(root: Path, rows: list[dict[str, Any]], *, active: str = 'custom', enabled: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'invoice-ai.json').write_text(json.dumps({
        'version': 2, 'enabled': enabled, 'active': active, 'providers': rows}), encoding='utf-8')


def provider(base: str = 'https://synthetic.example/v1', **changes: Any) -> dict[str, Any]:
    return {'id': 'custom', 'name': 'Synthetic provider', 'kind': 'openai',
            'base_url': base, 'model': 'synthetic-model', **changes}


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
        content_type = response.getheader('Content-Type', '')
        connection.close()
        return status, json.loads(raw) if content_type.startswith('application/json') else raw


@pytest.fixture
def web(tmp_path: Path) -> Any:
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server.server_port)
    client.token = client.request('/api/state')[1]['token']
    try:
        yield client, ExpenseStore(tmp_path).root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def stub() -> Any:
    state: dict[str, Any] = {'calls': [], 'status': 200, 'model': 'synthetic-model-20261027'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            state['calls'].append(('GET', self.path, self.headers.get('Authorization')))
            self.reply({'data': state.get('models', [
                {'id': 'synthetic-model'}, {'id': 'other'}, {'id': 'other'},
                {'id': PAGE_KEY}, {'id': 'invalid model'}, {'id': 5}])})

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(('POST', self.path, self.headers.get('Authorization'), payload))
            content = payload['messages'][1]['content']
            if isinstance(content, list):
                image = base64.b64decode(content[1]['image_url']['url'].split(',')[1])
                with Image.open(io.BytesIO(image)) as picture:
                    assert picture.format == 'PNG' and picture.size == (24, 24)
                answer = {'color': 'red'}
            else:
                answer = {'ok': True}
            self.reply({'model': state['model'], 'choices': [{'finish_reason': 'stop',
                        'message': {'content': json.dumps(answer)}}]})

        def reply(self, value: Any) -> None:
            raw = (state.get('raw') or json.dumps(value).encode())
            self.send_response(state['status'])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            if state['status'] == 302:
                self.send_header('Location', '/credential-target')
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize('legacy', [
    {'enabled': True, 'secret_env_file': 'glm.env'},
    {'enabled': False, 'provider': 'glm', 'model': 'glm-other', 'secret_env_file': 'glm.env'},
    {'enabled': True, 'provider': 'workbuddy', 'model': 'workbuddy/glm-5.3-flash',
     'secret_env_file': 'glm.env'},
])
def test_legacy_migration_is_read_only_until_first_atomic_save(tmp_path: Path, legacy: dict[str, Any]) -> None:
    path = tmp_path / 'invoice-ai.json'
    original = json.dumps(legacy, indent=2).encode()
    path.write_bytes(original)
    config = settings.read_config(tmp_path)
    assert path.read_bytes() == original and not path.with_suffix('.json.bak').exists()
    assert config['version'] == 2 and config['active'] == legacy.get('provider', 'glm')
    official = next(row for row in config['providers'] if row['id'] == 'glm')
    assert official['secret_env_file'] == 'glm.env'
    service = settings.InvoiceAISettings(tmp_path)
    service.update({'action': 'toggle', 'enabled': False})
    assert json.loads(path.read_text(encoding="utf-8"))['version'] == 2
    assert path.with_suffix('.json.bak').read_bytes() == original
    service.update({'action': 'toggle', 'enabled': True})
    assert path.with_suffix('.json.bak').read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))


def test_presets_use_only_existing_home_secret_files(tmp_path: Path) -> None:
    home = tmp_path / 'home' / '.config' / 'ai-secrets'
    home.mkdir(parents=True)
    (home / 'deepseek.env').write_text('DEEPSEEK_API_KEY=' + FILE_KEY)
    rows = {row['id']: row for row in settings.presets()}
    assert 'secret_env_file' not in rows['glm']
    assert rows['deepseek']['secret_env_file'] == str(home / 'deepseek.env')
    assert rows['deepseek']['key_variable'] == 'DEEPSEEK_API_KEY'
    assert rows['deepseek']['model'] == 'deepseek-flash'
    assert rows['deepseek']['base_url'] == 'https://api.deepseek.com/v1'


def test_adding_builtin_to_minimal_v2_restores_private_preset_defaults(tmp_path: Path) -> None:
    home = tmp_path / 'home' / '.config' / 'ai-secrets'
    home.mkdir(parents=True)
    (home / 'deepseek.env').write_text('DEEPSEEK_API_KEY=' + FILE_KEY)
    workbuddy = next(row for row in settings.presets() if row['id'] == 'workbuddy')
    write_config(tmp_path, [workbuddy], active='workbuddy')
    service = settings.InvoiceAISettings(tmp_path)
    public = service.update({'action': 'save', 'provider': {
        'id': 'deepseek', 'name': 'DeepSeek official', 'kind': 'openai',
        'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-flash'}})
    row = next(row for row in settings.read_config(tmp_path)['providers'] if row['id'] == 'deepseek')
    assert row['key_variable'] == 'DEEPSEEK_API_KEY'
    assert row['secret_env_file'] == str(home / 'deepseek.env')
    assert settings.resolve_key(tmp_path, row) == (FILE_KEY, 'file')
    preview = service.client({'provider': {'id': 'deepseek'}})
    assert preview._key == FILE_KEY
    assert FILE_KEY not in json.dumps(public) and str(home) not in json.dumps(public)


def test_legacy_direct_deepseek_activation_reuses_default_secret_file(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / 'home' / '.config' / 'ai-secrets'
    home.mkdir(parents=True)
    (home / 'deepseek.env').write_text('DEEPSEEK_API_KEY=' + FILE_KEY)
    legacy = {'enabled': True, 'provider': 'workbuddy', 'model': 'workbuddy/glm-5.3-flash',
              'secret_env_file': 'original-glm.env'}
    (tmp_path / 'invoice-ai.json').write_text(json.dumps(legacy), encoding='utf-8')
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda *args: pytest.fail('unexpected network'))
    public = settings.InvoiceAISettings(tmp_path).update({'action': 'activate', 'id': 'deepseek'})
    assert public['active'] == 'deepseek'
    rows = {row['id']: row for row in settings.read_config(tmp_path)['providers']}
    assert rows['deepseek']['secret_env_file'] == str(home / 'deepseek.env')
    assert rows['glm']['secret_env_file'] == 'original-glm.env'
    client = glm.GlmInvoiceAI(tmp_path)
    assert client.public_status()['available']
    assert client._key == FILE_KEY
    assert FILE_KEY not in json.dumps(public)


def test_key_embedded_in_model_never_appears_in_public_status(tmp_path: Path) -> None:
    write_config(tmp_path, [provider(model=PAGE_KEY)])
    (tmp_path / 'invoice-ai-keys.json').write_text(json.dumps({'custom': PAGE_KEY}), encoding='utf-8')
    client = glm.GlmInvoiceAI(tmp_path)
    assert PAGE_KEY not in json.dumps(client.public_status())
    assert PAGE_KEY not in json.dumps(settings.InvoiceAISettings(tmp_path).public())


def test_disabled_model_metadata_never_leaks_or_loads_credentials(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, [provider(model=PAGE_KEY)], enabled=False)
    (tmp_path / 'invoice-ai-keys.json').write_text(json.dumps({'custom': PAGE_KEY}), encoding='utf-8')
    monkeypatch.setattr(settings, 'resolve_key', lambda *args: pytest.fail('disabled client read credentials'))
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda *args: pytest.fail('unexpected network'))
    client = glm.GlmInvoiceAI(tmp_path)
    status = client.public_status()
    decision = client.decide({}, [])
    assert not status['enabled'] and not status['available']
    assert decision['option_id'] == 'review'
    assert PAGE_KEY not in json.dumps([status, decision])


def test_keys_priority_clear_and_blank_preserves_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    row = provider(secret_env_file='secret.env', key_variable='DEEPSEEK_API_KEY')
    write_config(tmp_path, [row])
    (tmp_path / 'secret.env').write_text('DEEPSEEK_API_KEY=' + FILE_KEY)
    monkeypatch.setenv('DEEPSEEK_API_KEY', ENV_KEY)
    service = settings.InvoiceAISettings(tmp_path)
    assert settings.resolve_key(tmp_path, row) == (FILE_KEY, 'file')
    service.update({'action': 'key', 'id': 'custom', 'api_key': PAGE_KEY})
    assert settings.resolve_key(tmp_path, row) == (PAGE_KEY, 'page')
    result = service.update({'action': 'save', 'provider': row, 'api_key': ''})
    assert result['providers'][0]['key_last4'] == '1234'
    assert json.loads((tmp_path / 'invoice-ai-keys.json').read_text(encoding="utf-8")) == {'custom': PAGE_KEY}
    assert PAGE_KEY not in (tmp_path / 'invoice-ai.json').read_text(encoding="utf-8")
    for key in (PAGE_KEY, FILE_KEY, ENV_KEY, 'secret.env'):
        assert key not in json.dumps(result)
    service.update({'action': 'clear_key', 'id': 'custom'})
    assert settings.resolve_key(tmp_path, row) == (FILE_KEY, 'file')
    (tmp_path / 'secret.env').unlink()
    assert settings.resolve_key(tmp_path, row) == (ENV_KEY, 'environment')


@pytest.mark.parametrize('bad', ['$(echo x)', 'two words', 'short', 'secret\nline', ['secret']])
def test_bad_key_does_not_write_configuration(tmp_path: Path, bad: Any) -> None:
    write_config(tmp_path, [provider()])
    before = (tmp_path / 'invoice-ai.json').read_bytes()
    with pytest.raises(ValueError):
        settings.InvoiceAISettings(tmp_path).update({'action': 'key', 'id': 'custom', 'api_key': bad})
    assert (tmp_path / 'invoice-ai.json').read_bytes() == before
    assert not (tmp_path / 'invoice-ai-keys.json').exists()


def test_add_edit_activate_delete_and_builtins(tmp_path: Path) -> None:
    service = settings.InvoiceAISettings(tmp_path)
    result = service.update({'action': 'save', 'provider': {**provider(), 'id': ''}, 'api_key': PAGE_KEY})
    custom = next(row for row in result['providers'] if not row['builtin'])
    identity = custom['id']
    assert identity and result['active'] == 'glm'
    service.update({'action': 'save', 'provider': {'id': identity, 'name': 'Renamed'}})
    result = service.update({'action': 'activate', 'id': identity})
    assert result['active'] == identity
    result = service.update({'action': 'delete', 'id': identity})
    assert identity not in {row['id'] for row in result['providers']}
    assert result['active'] in {row['id'] for row in result['providers']}
    assert identity not in json.loads((tmp_path / 'invoice-ai-keys.json').read_text(encoding="utf-8"))
    service.update({'action': 'save', 'provider': provider(id='inactive'), 'api_key': PAGE_KEY})
    result = service.update({'action': 'delete', 'id': 'inactive'})
    assert 'inactive' not in {row['id'] for row in result['providers']}
    assert 'inactive' not in json.loads((tmp_path / 'invoice-ai-keys.json').read_text(encoding='utf-8'))
    for builtin in ('glm', 'workbuddy', 'deepseek'):
        with pytest.raises(ValueError):
            service.update({'action': 'delete', 'id': builtin})


def test_deleting_only_custom_provider_restores_valid_presets(tmp_path: Path) -> None:
    write_config(tmp_path, [provider()])
    service = settings.InvoiceAISettings(tmp_path)
    service.update({'action': 'key', 'id': 'custom', 'api_key': PAGE_KEY})
    result = service.update({'action': 'delete', 'id': 'custom'})
    assert {row['id'] for row in result['providers']} == settings.BUILTINS
    assert result['active'] in settings.BUILTINS
    assert not json.loads((tmp_path / 'invoice-ai-keys.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('kind,base,accepted', [
    ('workbuddy', 'http://127.0.0.1:3425/v1', True),
    ('workbuddy', 'http://localhost:3425', True),
    ('workbuddy', 'https://localhost:3425/v1', False),
    ('workbuddy', 'http://example.test/v1', False),
    ('workbuddy', 'http://127.0.0.2/v1', False),
    ('openai', 'https://example.test/v1', True),
    ('openai', 'http://127.0.0.1:9999/v1', True),
    ('openai', 'http://[::1]:9999/v1', True),
    ('openai', 'http://localhost:9999/v1', True),
    ('openai', 'http://example.test/v1', False),
    ('openai', 'https://user:secret@example.test/v1', False),
    ('openai', 'https://example.test/v1?token=secret', False),
    ('openai', 'https://example.test/v1#secret', False),
    ('openai', 'https://example.test:65536/v1', False),
    ('openai', 'http://localhost:0/v1', False),
])
def test_endpoint_boundaries(kind: str, base: str, accepted: bool) -> None:
    row = provider(base, kind=kind, model='workbuddy/synthetic-model' if kind == 'workbuddy' else 'synthetic-model')
    if accepted:
        assert settings.validate_provider(row)
        assert settings.endpoint(row).endswith('/chat/completions')
    else:
        with pytest.raises(ValueError):
            settings.validate_provider(row)


@pytest.mark.parametrize('base,thinking,no_proxy', [
    ('https://open.bigmodel.cn/api/paas/v4', True, False),
    ('https://api.deepseek.com/v1', False, False),
    ('http://localhost:9999/v1', False, True),
    ('http://127.0.0.1:9999/v1', False, True),
])
def test_payload_thinking_and_proxy_are_host_specific(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     base: str, thinking: bool, no_proxy: bool) -> None:
    write_config(tmp_path, [provider(base)])
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    response = {'model': 'synthetic-model', 'choices': [{'finish_reason': 'stop',
                 'message': {'content': '{"ok":true}'}}]}
    opener = Opener(Response(json.dumps(response).encode()))
    handlers_seen: list[Any] = []
    def build(*handlers: Any) -> Opener:
        handlers_seen.extend(handlers)
        return opener
    monkeypatch.setattr(glm.urllib.request, 'build_opener', build)
    client = glm.GlmInvoiceAI(tmp_path)
    assert client._request('test', 'synthetic', max_tokens=20) == {'ok': True}
    assert len(opener.calls) == 1
    assert ('thinking' in json.loads(opener.calls[0][0].data)) is thinking
    assert any(isinstance(handler, glm._NoRedirect) for handler in handlers_seen)
    assert any(isinstance(handler, glm.urllib.request.ProxyHandler) and handler.proxies == {}
               for handler in handlers_seen) is no_proxy


def test_hot_update_existing_client_model_key_and_enabled(tmp_path: Path, stub: Any) -> None:
    base, state = stub
    write_config(tmp_path, [provider(base)], enabled=False)
    service = settings.InvoiceAISettings(tmp_path)
    client = glm.GlmInvoiceAI(tmp_path)
    assert not client.enabled
    service.update({'action': 'key', 'id': 'custom', 'api_key': PAGE_KEY})
    service.update({'action': 'toggle', 'enabled': True})
    assert client.enabled and client.public_status()['available']
    assert client._request('JSON only', 'synthetic', max_tokens=100) == {'ok': True}
    service.update({'action': 'save', 'provider': {'id': 'custom', 'model': 'other-model'}, 'api_key': ENV_KEY})
    state['model'] = 'other-model-20261027'
    assert client.model == 'other-model'
    assert client._request('JSON only', 'synthetic', max_tokens=100) == {'ok': True}
    assert state['calls'][-1][2] == 'Bearer ' + ENV_KEY
    assert state['calls'][-1][3]['model'] == 'other-model'
    service.update({'action': 'toggle', 'enabled': False})
    with pytest.raises(glm.GlmAIError) as error:
        client._request('JSON only', 'synthetic', max_tokens=100)
    assert error.value.failure_code == 'config_unavailable'
    assert len(state['calls']) == 2


@pytest.mark.parametrize('served,accepted', [
    ('synthetic-model', True), ('synthetic-model-20261027', True),
    ('synthetic-model:20261027', True), ('unrelated', False),
    ('synthetic-modelish', False), (None, False),
])
def test_openai_returned_model_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         served: Any, accepted: bool) -> None:
    write_config(tmp_path, [provider()])
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    value = {'model': served, 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
    opener = Opener(Response(json.dumps(value).encode()))
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda *args: opener)
    client = glm.GlmInvoiceAI(tmp_path)
    if accepted:
        assert client._request('test', 'synthetic', max_tokens=20) == {'ok': True}
    else:
        with pytest.raises(glm.GlmAIError) as error:
            client._request('test', 'synthetic', max_tokens=20)
        assert error.value.failure_code == 'response_schema'


def test_real_http_diagnostics_models_and_preview_do_not_save(web: Any, stub: Any) -> None:
    client, root = web
    base, state = stub
    row = provider(base)
    status, saved = client.request('/api/settings', {'action': 'save', 'provider': row, 'api_key': PAGE_KEY})
    assert status == 200
    before = {path.name: path.read_bytes() for path in root.glob('invoice-ai*')}
    status, result = client.request('/api/settings/test', {'provider': row, 'api_key': ''})
    assert status == 200 and result['ok']
    assert result['text']['ok'] and result['image']['ok']
    assert result['text']['model'] == state['model']
    assert all(result[k]['elapsed_ms'] >= 0 for k in ('text', 'image'))
    assert len(state['calls']) == 2
    assert all(call[2] == 'Bearer ' + PAGE_KEY for call in state['calls'])
    status, models = client.request('/api/settings/models', {'provider': row})
    assert status == 200 and models == {'models': ['other', 'synthetic-model']}
    assert state['calls'][-1][:2] == ('GET', '/v1/models')
    assert before == {path.name: path.read_bytes() for path in root.glob('invoice-ai*')}
    assert PAGE_KEY not in json.dumps([saved, result, models, client.request('/api/state')[1]])


@pytest.mark.parametrize('kind,credential', [('openai', PAGE_KEY), ('workbuddy', 'magpie')])
def test_model_list_accepts_blank_model(web: Any, stub: Any, kind: str, credential: str) -> None:
    client, root = web
    base, state = stub
    state['models'] = [{'id': 'synthetic-model'}, {'id': 'other'}]
    before = {path.name: path.read_bytes() for path in root.glob('invoice-ai*')}
    body: dict[str, Any] = {'provider': provider(base, kind=kind, model='')}
    if kind == 'openai':
        body['api_key'] = PAGE_KEY
    status, result = client.request('/api/settings/models', body)
    assert status == 200 and result == {'models': ['other', 'synthetic-model']}
    assert len(state['calls']) == 1
    assert state['calls'][0] == ('GET', '/v1/models', 'Bearer ' + credential)
    assert before == {path.name: path.read_bytes() for path in root.glob('invoice-ai*')}


@pytest.mark.parametrize('status,raw', [
    (401, None), (302, None), (200, b'not-json:' + PAGE_KEY.encode()),
    (200, b'X' * (glm.MAX_RESPONSE_BYTES + 1)),
], ids=['unauthorized', 'redirect', 'invalid-json', 'oversized'])
def test_diagnostics_errors_are_bounded_safe_and_not_retried(web: Any, stub: Any,
                                                           status: int, raw: bytes | None) -> None:
    client, _ = web
    base, state = stub
    state.update(status=status, raw=raw)
    request = {'provider': provider(base), 'api_key': PAGE_KEY}
    code, result = client.request('/api/settings/test', request)
    assert code == 200 and result['ok'] is False
    assert result['text']['ok'] is False and result['image']['ok'] is False
    assert len(state['calls']) == 2
    code, model_result = client.request('/api/settings/models', request)
    assert code == 400 and set(model_result) == {'error'}
    assert len(state['calls']) == 3
    assert PAGE_KEY not in json.dumps([result, model_result])
    assert 'not-json' not in json.dumps([result, model_result])


@pytest.mark.parametrize('route', ['/api/settings', '/api/settings/test', '/api/settings/models'])
def test_settings_writes_reject_foreign_origin_host_and_token(web: Any, route: str) -> None:
    client, _ = web
    body = {'action': 'toggle', 'enabled': True, 'provider': provider(), 'api_key': PAGE_KEY}
    for headers in ({'Origin': 'http://foreign.test'}, {'Host': 'foreign.test'}, {'X-Workbench-Token': 'wrong'}):
        status, value = client.request(route, body, **headers)
        assert status == 403 and PAGE_KEY not in json.dumps(value)
    if route == '/api/settings':
        assert client.request(route, Origin='http://foreign.test')[0] == 403
        assert client.request(route, Host='foreign.test')[0] == 403


@pytest.mark.parametrize('body', [
    {'action': 'save', 'provider': provider('http://foreign.test/v1'), 'api_key': PAGE_KEY},
    {'action': 'save', 'provider': provider(), 'api_key': PAGE_KEY + ' bad'},
    {'action': 'delete', 'id': PAGE_KEY},
    {'action': PAGE_KEY},
])
def test_settings_invalid_input_never_echoes_secret(web: Any, body: dict[str, Any]) -> None:
    client, _ = web
    status, result = client.request('/api/settings', body)
    assert status == 400 and set(result) == {'error'}
    assert PAGE_KEY not in json.dumps(result)


def test_paired_phone_cannot_access_settings_or_key_files(web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    desktop, _ = web
    original = phone_link.lan_address
    monkeypatch.setattr(phone_link, 'lan_address', lambda ip: ip == '127.0.0.1' or original(ip))
    monkeypatch.setattr(phone_link, 'addresses', lambda: [{'address': '127.0.0.1', 'label': 'Test LAN'}])
    server_class = phone_link.ThreadingHTTPServer
    monkeypatch.setattr(phone_link, 'ThreadingHTTPServer', lambda addr, handler: server_class((addr[0], 0), handler))
    status, connection = desktop.request('/api/phone-link/start', {'address': '127.0.0.1'})
    assert status == 200
    phone = Client(connection['port'])
    assert phone.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]})[0] == 200
    phone.token = phone.request('/api/state')[1]['token']
    for route in ('/api/settings', '/invoice-ai.json', '/invoice-ai-keys.json'):
        assert phone.request(route)[0] == 404
    for route in ('/api/settings', '/api/settings/test', '/api/settings/models'):
        assert phone.request(route, {'provider': provider(), 'api_key': PAGE_KEY})[0] == 403


def test_model_list_has_ten_second_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, [provider()])
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    opener = Opener(Response(b'{"data":[{"id":"one"}]}'))
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda *args: opener)
    assert settings.InvoiceAISettings(tmp_path).models({'provider': provider()}) == {'models': ['one']}
    assert opener.calls[0][1] == 10
    assert opener.calls[0][0].get_method() == 'GET'
