"""Private workspace AI settings, migration and explicit synthetic diagnostics."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import re
import shlex
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from PIL import Image

from invoice_print_layout.glm_invoice_ai import (
    GlmInvoiceAI, GlmAIError, MODEL, _json_object, _usable_key, MAX_TEXT_CHARS)
from invoice_print_layout.reliability import save_json

BUILTINS = frozenset({'workbuddy', 'glm', 'deepseek'})


class KeyHostChanged(ValueError):
    def __init__(self) -> None:
        super().__init__('地址已变更，请重新填写 API Key')


def presets(*, discover_files: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {'id': 'workbuddy', 'name': 'WorkBuddy 本机', 'kind': 'workbuddy',
         'base_url': 'http://127.0.0.1:3425/v1', 'model': 'workbuddy/glm-5.3-flash'},
        {'id': 'glm', 'name': '智谱官方', 'kind': 'openai',
         'base_url': 'https://open.bigmodel.cn/api/paas/v4', 'model': MODEL, 'key_variable': 'GLM_API_KEY'},
        {'id': 'deepseek', 'name': 'DeepSeek 官方', 'kind': 'openai',
         'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-flash', 'key_variable': 'DEEPSEEK_API_KEY'}]
    if discover_files:
        for row in rows[1:]:
            try:
                path = Path.home() / '.config' / 'ai-secrets' / (row['id'] + '.env')
                if path.is_file():
                    row['secret_env_file'] = str(path)
            except (OSError, RuntimeError):
                pass
    return rows


def endpoint(row: dict[str, Any]) -> str:
    base = row.get('base_url')
    if not isinstance(base, str) or not re.fullmatch(r'[\x21-\x7e]{1,2048}', base):
        raise ValueError('AI地址无效')
    url = urlsplit(base)
    if (url.username or url.password or url.query or url.fragment or not url.hostname
            or url.port == 0 or '..' in url.path or '%' in base or '\\' in base):
        raise ValueError('AI地址无效')
    if row.get('kind') == 'workbuddy':
        if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?(?:/v1)?/?', base):
            raise ValueError('WorkBuddy仅支持本机HTTP回环')
        base = base.rstrip('/') + ('' if url.path.rstrip('/') == '/v1' else '/v1')
    elif row.get('kind') != 'openai' or (url.scheme != 'https' and not (
            url.scheme == 'http' and url.hostname in {'localhost', '127.0.0.1', '::1'})):
        raise ValueError('远程AI地址必须使用HTTPS')
    return base.rstrip('/') + '/chat/completions'


def validate_provider(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError('供应商配置无效')
    row = {key: value[key] for key in ('id', 'name', 'kind', 'base_url', 'model',
                                      'secret_env_file', 'key_variable', 'local_key_host', 'page_key_host') if key in value}
    for key, pattern in [('id', r'[A-Za-z0-9_-]{1,64}'), ('name', r'[^\x00-\x1f]{1,100}'),
                         ('model', r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}'),
                         ('key_variable', r'[A-Za-z_][A-Za-z0-9_]{0,99}')]:
        if key == 'key_variable' and key not in row:
            continue
        if not isinstance(row.get(key), str) or not re.fullmatch(pattern, row[key]):
            raise ValueError('供应商字段无效')
    if row.get('kind') == 'workbuddy' and not re.fullmatch(r'workbuddy/[A-Za-z0-9][A-Za-z0-9_.:-]*', row['model']):
        raise ValueError('WorkBuddy模型名无效')
    if 'secret_env_file' in row and not isinstance(row['secret_env_file'], str):
        raise ValueError('密钥文件配置无效')
    for key in ('local_key_host', 'page_key_host'):
        if key in row and (not isinstance(row[key], str) or not re.fullmatch(r'[A-Za-z0-9.:-]*', row[key])):
            raise ValueError('密钥主机配置无效')
    endpoint(row)
    return row


def read_object(path: Path, limit: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    if path.stat().st_size > limit:
        raise ValueError('AI配置过大')
    return _json_object(path.read_text(encoding='utf-8-sig'))


def read_config(root: Path) -> dict[str, Any]:
    path = root / 'invoice-ai.json'
    if not path.is_file():
        return {'version': 2, 'enabled': False, 'active': 'glm', 'providers': presets()}
    value = read_object(path)
    if type(value.get('enabled')) is not bool:
        raise ValueError('AI开关无效')
    if 'version' not in value:
        selected = value.get('provider', 'glm')
        if selected not in {'glm', 'workbuddy'}:
            raise ValueError('供应商无效')
        rows = presets()
        # Legacy configs without a file stay environment-only.
        rows[1].pop('secret_env_file', None)
        if isinstance(value.get('secret_env_file'), str):
            rows[1]['secret_env_file'] = value['secret_env_file']
        elif selected == 'glm' and 'secret_env_file' in value:
            raise ValueError('密钥文件配置无效')
        row = next(row for row in rows if row['id'] == selected)
        row['model'] = value.get('model', MODEL if selected == 'glm' else '')
        if 'base_url' in value:
            row['base_url'] = value['base_url']
        value = {'version': 2, 'enabled': value['enabled'], 'active': selected, 'providers': rows}
    if value.get('version') != 2 or not isinstance(value.get('providers'), list) or not 1 <= len(value['providers']) <= 30:
        raise ValueError('AI配置版本无效')
    rows = [validate_provider(row) for row in value['providers']]
    ids = [row['id'] for row in rows]
    if len(set(ids)) != len(ids) or value.get('active') not in ids:
        raise ValueError('当前供应商无效')
    return {'version': 2, 'enabled': value['enabled'], 'active': value['active'], 'providers': rows}


def resolve_key(root: Path, row: dict[str, Any], keys: dict[str, Any] | None = None) -> tuple[str, str]:
    if row['kind'] == 'workbuddy':
        return 'magpie', 'not_required'
    if keys is None:
        path = root / 'invoice-ai-keys.json'
        keys = read_object(path, 256 * 1024) if path.is_file() else {}
    key = keys.get(row['id'], '')
    if key and row.get('page_key_host', urlsplit(row['base_url']).hostname) != urlsplit(row['base_url']).hostname:
        return '', 'none'
    source = 'page'
    if not key:
        if row.get('local_key_host', urlsplit(row['base_url']).hostname) != urlsplit(row['base_url']).hostname:
            return '', 'none'
        source = 'file'
        variable = row.get('key_variable', 'GLM_API_KEY')
        filename = row.get('secret_env_file', '')
        if filename:
            path = Path(filename)
            if not path.is_absolute():
                path = root / path
            if path.is_file():
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError('密钥文件过大')
                found = set()
                for line in path.read_text(encoding='utf-8-sig').splitlines():
                    match = re.fullmatch(r'\s*(?:export\s+)?' + re.escape(variable) + r'\s*=\s*(.*?)\s*', line)
                    if match:
                        tokens = shlex.split(match[1], comments=True, posix=True)
                        if len(tokens) != 1 or not _usable_key(tokens[0]):
                            raise ValueError('密钥文件无效')
                        found.add(tokens[0])
                if len(found) > 1:
                    raise ValueError('密钥文件无效')
                key = next(iter(found), '')
        if not key:
            source = 'environment'
            key = os.environ.get(variable, '').strip()
    if not isinstance(key, str) or (key and not _usable_key(key)):
        raise ValueError('密钥格式无效')
    return key, source if key else 'none'


class InvoiceAISettings:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()

    def public(self) -> dict[str, Any]:
        config = read_config(self.root)
        keypath = self.root / 'invoice-ai-keys.json'
        keys = read_object(keypath, 256 * 1024) if keypath.is_file() else {}
        rows = []
        secrets = [key for key in keys.values() if isinstance(key, str) and _usable_key(key)]
        for row in config['providers']:
            try:
                key, source = resolve_key(self.root, row, keys)
            except (OSError, ValueError, UnicodeError):
                key, source = '', 'none'
            if key:
                secrets.append(key)
            rows.append({**{k: row[k] for k in ('id', 'name', 'kind', 'base_url', 'model')},
                         'builtin': row['id'] in BUILTINS, 'key_source': source,
                         'key_last4': key[-4:] if source == 'page' else '',
                         'key_required': row['kind'] != 'workbuddy' and not key and
                             any(row.get(field, urlsplit(row['base_url']).hostname) != urlsplit(row['base_url']).hostname
                                 for field in ('local_key_host', 'page_key_host'))})
        result = {**config, 'providers': rows}
        # Even a provider echo placed in a display field must not reveal a key.
        raw = json.dumps(result, ensure_ascii=False)
        for key in secrets:
            raw = raw.replace(key, '••••')
        return dict(json.loads(raw))

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            config = read_config(self.root)
            keypath = self.root / 'invoice-ai-keys.json'
            keys = read_object(keypath, 256 * 1024) if keypath.is_file() else {}
            action = body.get('action')
            identity = body.get('id')
            if action == 'save':
                value = body.get('provider')
                if not isinstance(value, dict):
                    raise ValueError('供应商无效')
                identity = value.get('id') or uuid.uuid4().hex
                old: dict[str, Any] = next((r for r in config['providers'] if r['id'] == identity), {})
                preset = next((r for r in presets() if r['id'] == identity), {})
                row = validate_provider({**preset, **old, **value, 'id': identity})
                baseline = old or preset
                host = urlsplit(row['base_url']).hostname
                original_host = urlsplit(baseline['base_url']).hostname if baseline else ''
                row['local_key_host'] = baseline.get('local_key_host', original_host)
                row['page_key_host'] = baseline.get('page_key_host', original_host)
                if original_host != host:
                    keys.pop(identity, None)
                if row['local_key_host'] != host:
                    row.pop('secret_env_file', None)
                config['providers'] = [row if r['id'] == identity else r for r in config['providers']]
                if not old:
                    if len(config['providers']) >= 30:
                        raise ValueError('供应商数量过多')
                    config['providers'].append(row)
            elif action == 'toggle':
                if type(body.get('enabled')) is not bool:
                    raise ValueError('开关无效')
                config['enabled'] = body['enabled']
            elif action in {'activate', 'delete', 'key', 'clear_key'}:
                if not isinstance(identity, str) or identity not in {r['id'] for r in config['providers']}:
                    raise ValueError('供应商不存在')
                if action == 'activate':
                    config['active'] = identity
                elif action == 'delete':
                    if identity in BUILTINS:
                        raise ValueError('内置供应商不能删除')
                    config['providers'] = [r for r in config['providers'] if r['id'] != identity] or presets()
                    if config['active'] == identity:
                        config['active'] = config['providers'][0]['id']
                    keys.pop(identity, None)
                elif action == 'clear_key':
                    keys.pop(identity, None)
            else:
                raise ValueError('设置操作无效')
            key = body.get('api_key', '')
            if not isinstance(key, str) or (key and not _usable_key(key)):
                raise ValueError('密钥格式无效')
            if key and action in {'save', 'key'}:
                if not isinstance(identity, str):
                    raise ValueError('供应商无效')
                key_row = next(r for r in config['providers'] if r['id'] == identity)
                if key_row['kind'] == 'workbuddy':
                    raise ValueError('WorkBuddy无需保存密钥')
                keys[identity] = key
                key_row['page_key_host'] = urlsplit(key_row['base_url']).hostname
            for row in config['providers']:
                row.setdefault('local_key_host', urlsplit(row['base_url']).hostname)
                if keys.get(row['id']):
                    row.setdefault('page_key_host', urlsplit(row['base_url']).hostname)
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / 'invoice-ai.json'
            backup = path.with_suffix('.json.bak')
            if path.exists() and not backup.exists():
                # Exclusive creation preserves the first original exactly.
                with backup.open('xb') as stream:
                    stream.write(path.read_bytes())
            save_json(keypath, keys)
            save_json(path, config)
            return self.public()

    def client(self, body: dict[str, Any]) -> GlmInvoiceAI:
        config = read_config(self.root)
        value = body.get('provider')
        if not isinstance(value, dict):
            raise ValueError('供应商无效')
        old: dict[str, Any] = next((r for r in config['providers'] if r['id'] == value.get('id')), {})
        preset = next((r for r in presets() if r['id'] == value.get('id')), {})
        row = validate_provider({**preset, **old, **value, 'id': value.get('id') or 'preview'})
        key = body.get('api_key', '')
        if not isinstance(key, str) or (key and not _usable_key(key)):
            raise ValueError('密钥格式无效')
        baseline = old or preset
        if not key and row['kind'] != 'workbuddy':
            if not baseline or urlsplit(baseline['base_url']).hostname != urlsplit(row['base_url']).hostname:
                raise KeyHostChanged()
            row['local_key_host'] = baseline.get('local_key_host', urlsplit(baseline['base_url']).hostname)
            row['page_key_host'] = baseline.get('page_key_host', urlsplit(baseline['base_url']).hostname)
            if any(row[field] != urlsplit(row['base_url']).hostname for field in ('local_key_host', 'page_key_host')) and not resolve_key(self.root, row)[0]:
                raise KeyHostChanged()
        client = GlmInvoiceAI(self.root)
        client._root = None  # A form preview is isolated from live config refresh.
        client.enabled = True
        client.provider = row['id']
        client.kind = row['kind']
        client.model = row['model']
        client._endpoint = endpoint(row)
        client._key = 'magpie' if row['kind'] == 'workbuddy' else key or resolve_key(self.root, row)[0]
        return client

    def test(self, body: dict[str, Any]) -> dict[str, Any]:
        client = self.client(body)
        output = io.BytesIO()
        Image.new('RGB', (24, 24), 'red').save(output, format='PNG')
        image = 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')
        results: dict[str, Any] = {}
        for kind, content in [('text', 'Synthetic connection test. Reply with {"ok":true}.'),
                              ('image', [{'type': 'text', 'text': 'Read this image. Reply with {"color":"red"}.'},
                                         {'type': 'image_url', 'image_url': {'url': image}}])]:
            started = time.monotonic()
            result: dict[str, Any] = {'ok': False, 'model': ''}
            try:
                answer = client._request('Return only JSON.', content, max_tokens=1024)  # type: ignore[arg-type]
                result['ok'] = answer.get('ok') is True if kind == 'text' else answer.get('color') == 'red'
                result['model'] = getattr(answer, 'served_model', '')
                if not result['ok']:
                    result['error'] = '模型未通过合成内容校验'
            except GlmAIError as error:
                result['error'] = str(error)
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
            results[str(kind)] = result
        return {'ok': all(row['ok'] for row in results.values()), **results}

    def models(self, body: dict[str, Any]) -> dict[str, Any]:
        value = body.get('provider')
        if not isinstance(value, dict):
            raise ValueError('供应商无效')
        # Model discovery precedes model selection; GET sends no model.
        client = self.client({**body, 'provider': {**value, 'model': 'workbuddy/models'}})
        if not client._key:
            raise ValueError('密钥未配置')
        value = client._network(client._endpoint.removesuffix('/chat/completions') + '/models', None, timeout=10)
        rows = value.get('data')
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError('模型列表无效')
        supplied = body.get('api_key', '')
        ids = sorted({r['id'] for r in rows if isinstance(r, dict) and isinstance(r.get('id'), str)
                      and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}', r['id'])
                      and client._key not in r['id'] and (not supplied or supplied not in r['id'])})
        return {'models': ids}
