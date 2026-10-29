"""Concurrency and credential-host regression tests using synthetic data only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from invoice_print_layout import glm_invoice_ai as glm
from invoice_print_layout import invoice_ai_settings as settings
from tests.test_glm_invoice_ai import Response
from tests.test_invoice_ai_settings import (
    PAGE_KEY, FILE_KEY, ENV_KEY, isolated_home, provider, write_config, web)  # noqa: F401


def test_blocked_request_does_not_block_status_or_other_requests_and_keeps_snapshot(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, [provider()], enabled=True)
    service = settings.InvoiceAISettings(tmp_path)
    service.update({'action': 'key', 'id': 'custom', 'api_key': PAGE_KEY})
    client = glm.GlmInvoiceAI(tmp_path)
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    calls: list[tuple[str, str, str]] = []
    call_lock = threading.Lock()

    class Opener:
        def open(self, request: Any, *, timeout: int) -> Response:
            payload = json.loads(request.data)
            model = payload['model']
            with call_lock:
                index = len(calls)
                calls.append((model, request.get_header('Authorization'), request.full_url))
            entered[index].set()
            assert release.wait(5), 'test failed to release synthetic network request'
            envelope = {'model': model + '-20261027', 'choices': [
                {'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]}
            return Response(json.dumps(envelope).encode())

    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda *args: Opener())
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(client._request, 'JSON only', 'first', max_tokens=30)
        try:
            assert entered[0].wait(2)
            service.update({'action': 'save', 'provider': {'id': 'custom', 'model': 'second-model'},
                            'api_key': ENV_KEY})
            status = pool.submit(lambda: (client.public_status(), client.enabled, client.model)).result(timeout=1)
            assert status[0]['model'] == status[2] == 'second-model' and status[1]
            second = pool.submit(client._request, 'JSON only', 'second', max_tokens=30)
            assert entered[1].wait(2), 'requests on one shared client must overlap'
        finally:
            release.set()
        first_result, second_result = first.result(timeout=2), second.result(timeout=2)
    assert first_result == second_result == {'ok': True}
    assert isinstance(first_result, glm.AIResponse) and isinstance(second_result, glm.AIResponse)
    assert (first_result.model, first_result.served_model) == ('synthetic-model', 'synthetic-model-20261027')
    assert (second_result.model, second_result.served_model) == ('second-model', 'second-model-20261027')
    assert calls == [('synthetic-model', 'Bearer ' + PAGE_KEY, 'https://synthetic.example/v1/chat/completions'),
                     ('second-model', 'Bearer ' + ENV_KEY, 'https://synthetic.example/v1/chat/completions')]
    assert not hasattr(client, '_in_request') and not hasattr(client, '_served_model')


@pytest.mark.parametrize('source', ['page', 'file', 'environment'])
@pytest.mark.parametrize('route', ['/api/settings/test', '/api/settings/models'])
def test_form_host_change_requires_new_key_before_network(
        web: Any, monkeypatch: pytest.MonkeyPatch, source: str, route: str) -> None:
    http, root = web
    row = provider(secret_env_file='synthetic.env')
    write_config(root, [row])
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    if source == 'page':
        (root / 'invoice-ai-keys.json').write_text(json.dumps({'custom': PAGE_KEY}), encoding='utf-8')
    elif source == 'file':
        (root / 'synthetic.env').write_text('GLM_API_KEY=' + FILE_KEY, encoding='utf-8')
    monkeypatch.setattr(glm.urllib.request, 'build_opener', lambda *args: pytest.fail('credential must not be sent'))
    code, result = http.request(route, {'provider': {**row, 'base_url': 'https://other.example/v1'}})
    assert code == 400 and result == {'error': '地址已变更，请重新填写 API Key'}
    assert all(key not in json.dumps(result) for key in (PAGE_KEY, FILE_KEY, ENV_KEY))
    fresh = settings.InvoiceAISettings(root).client({
        'provider': {**row, 'base_url': 'https://other.example/v1'}, 'api_key': 'synthetic-new-key-0000'})
    assert fresh._key == 'synthetic-new-key-0000'
    assert fresh._endpoint == 'https://other.example/v1/chat/completions'


@pytest.mark.parametrize('source', ['page', 'file', 'environment'])
def test_save_host_change_removes_old_credentials_and_blocks_environment_fallback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    row = provider(secret_env_file='synthetic.env')
    write_config(tmp_path, [row])
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    if source == 'page':
        (tmp_path / 'invoice-ai-keys.json').write_text(json.dumps({'custom': PAGE_KEY}), encoding='utf-8')
    elif source == 'file':
        (tmp_path / 'synthetic.env').write_text('GLM_API_KEY=' + FILE_KEY, encoding='utf-8')
    service = settings.InvoiceAISettings(tmp_path)
    result = service.update({'action': 'save', 'provider': {'id': 'custom', 'base_url': 'https://other.example/v1'}})
    saved = settings.read_config(tmp_path)['providers'][0]
    assert 'secret_env_file' not in saved
    assert 'custom' not in json.loads((tmp_path / 'invoice-ai-keys.json').read_text(encoding='utf-8'))
    assert settings.resolve_key(tmp_path, saved) == ('', 'none')
    assert result['providers'][0]['key_required'] and result['providers'][0]['key_source'] == 'none'
    assert not glm.GlmInvoiceAI(tmp_path).public_status()['available']
    with pytest.raises(settings.KeyHostChanged, match='地址已变更'):
        service.client({'provider': {'id': 'custom'}})
    service.update({'action': 'save', 'provider': {'id': 'custom', 'name': 'Renamed'}})
    assert settings.resolve_key(tmp_path, settings.read_config(tmp_path)['providers'][0]) == ('', 'none')
    result = service.update({'action': 'key', 'id': 'custom', 'api_key': 'synthetic-new-key-0000'})
    assert not result['providers'][0]['key_required']
    assert settings.resolve_key(tmp_path, settings.read_config(tmp_path)['providers'][0])[0] == 'synthetic-new-key-0000'
    service.update({'action': 'clear_key', 'id': 'custom'})
    assert settings.resolve_key(tmp_path, settings.read_config(tmp_path)['providers'][0]) == ('', 'none')


def test_same_host_path_or_port_change_keeps_saved_key(tmp_path: Path) -> None:
    write_config(tmp_path, [provider()])
    service = settings.InvoiceAISettings(tmp_path)
    service.update({'action': 'key', 'id': 'custom', 'api_key': PAGE_KEY})
    row = {**provider(), 'base_url': 'https://SYNTHETIC.example:8443/other'}
    assert service.client({'provider': row})._key == PAGE_KEY
    service.update({'action': 'save', 'provider': row})
    assert service.client({'provider': {'id': 'custom'}})._key == PAGE_KEY


def test_host_change_with_new_page_key_never_reuses_old_local_key_after_clear(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_config(tmp_path, [provider(secret_env_file='synthetic.env')])
    (tmp_path / 'synthetic.env').write_text('GLM_API_KEY=' + FILE_KEY, encoding='utf-8')
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    service = settings.InvoiceAISettings(tmp_path)
    result = service.update({'action': 'save', 'provider': {'id': 'custom', 'base_url': 'https://other.example/v1'},
                             'api_key': PAGE_KEY})
    assert not result['providers'][0]['key_required']
    assert service.client({'provider': {'id': 'custom'}})._key == PAGE_KEY
    service.update({'action': 'clear_key', 'id': 'custom'})
    assert settings.resolve_key(tmp_path, settings.read_config(tmp_path)['providers'][0]) == ('', 'none')


def test_preset_key_cannot_be_previewed_at_another_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / 'home/.config/ai-secrets'
    home.mkdir(parents=True)
    (home / 'deepseek.env').write_text('DEEPSEEK_API_KEY=' + FILE_KEY, encoding='utf-8')
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    with pytest.raises(settings.KeyHostChanged):
        settings.InvoiceAISettings(tmp_path).client({'provider': {'id': 'deepseek', 'base_url': 'https://other.example/v1'}})


@pytest.mark.parametrize('source', ['page', 'file', 'environment'])
def test_persisted_binding_blocks_key_after_direct_configuration_host_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    write_config(tmp_path, [provider(secret_env_file='synthetic.env')])
    monkeypatch.setenv('GLM_API_KEY', ENV_KEY)
    if source == 'file':
        (tmp_path / 'synthetic.env').write_text('GLM_API_KEY=' + FILE_KEY, encoding='utf-8')
    service = settings.InvoiceAISettings(tmp_path)
    service.update({'action': 'key', 'id': 'custom', 'api_key': PAGE_KEY} if source == 'page' else
                   {'action': 'toggle', 'enabled': True})
    client = glm.GlmInvoiceAI(tmp_path)
    assert client.public_status()['available']
    config = settings.read_config(tmp_path)
    config['providers'][0]['base_url'] = 'https://other.example/v1'
    (tmp_path / 'invoice-ai.json').write_text(json.dumps(config), encoding='utf-8')
    assert not client.public_status()['available']
    assert service.public()['providers'][0]['key_required']
