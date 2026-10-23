import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class CLIError(Exception):
    def __init__(self, message, code='INVALID_REQUEST', exit_code=2):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict): raise ValueError()
        return value
    except (OSError, ValueError): raise CLIError('JSON文件不存在或格式无效。', 'INVALID_JSON', 4) from None


def configuration(url=None, workspace_id=None):
    file = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'InvoiceAssistantCLI/config.json'
    config = read_json(file) if file.exists() else {}
    selected_url = url or os.environ.get('INVOICE_ASSISTANT_URL') or config.get('url', 'http://127.0.0.1:8765')
    selected_id = workspace_id or os.environ.get('INVOICE_ASSISTANT_WORKSPACE_ID') or config.get('workspace_id')
    if url and url.rstrip('/') != str(config.get('url', '')).rstrip('/') and not workspace_id and not os.environ.get('INVOICE_ASSISTANT_WORKSPACE_ID'):
        raise CLIError('切换URL时请同时指定--workspace-id，避免连接另一账本。', 'WORKSPACE_REQUIRED', 4)
    if not isinstance(selected_id, str) or not re.fullmatch(r'[a-f0-9]{64}', selected_id):
        raise CLIError('请先安装配置，或指定原服务的--workspace-id。', 'WORKSPACE_REQUIRED', 4)
    return {'url': local_url(selected_url), 'workspace_id': selected_id}


def local_url(value):
    if not isinstance(value, str): raise CLIError('服务地址无效。')
    parsed = urlsplit(value)
    try:
        local = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname or '').is_loopback
        port = parsed.port
    except ValueError: local, port = False, None
    if (not local or not port or parsed.scheme != 'http' or parsed.username or parsed.password
        or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise CLIError('只允许带端口的本机HTTP根地址。', 'INVALID_URL', 2)
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl): return None


class Client:
    def __init__(self, config):
        self.base = local_url(config['url'])
        self.workspace_id = config['workspace_id']
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self._state = None
        self.operations = {'recognition_requests': 0, 'classification_requests': 0, 'glm_calls': 0, 'jev_calls': 0,
                           'draft_requests': 0, 'report_render_requests': 0, 'downloads': 0}

    def request(self, path, body=None, token=None, binary=False):
        headers = {'Accept': 'application/json'}
        if body is not None: headers.update({'Content-Type': 'application/json', 'Origin': self.base, 'X-Workbench-Token': token or ''})
        req = Request(self.base + path, data=canonical(body).encode() if body is not None else None, headers=headers)
        try:
            with self.opener.open(req, timeout=180 if body is not None else 30) as response:
                raw = response.read(64 * 1024 * 1024 + 1)
            if len(raw) > 64 * 1024 * 1024: raise ValueError()
            if binary: return raw
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError()
            return value
        except HTTPError as exc:
            if body is not None and exc.code == 403: raise PermissionError() from None
            if body is not None and (exc.code >= 500 or exc.code == 408):
                raise CLIError('原服务未确认操作结果；先查询现状，未自动重发。', 'RESULT_UNCERTAIN', 6) from None
            try: message = str(json.loads(exc.read(65536)).get('error', ''))[:500]
            except (ValueError, UnicodeError): message = ''
            raise CLIError(message or f'原服务返回HTTP {exc.code}。', 'HTTP_' + str(exc.code), 4) from None
        except (OSError, TimeoutError):
            if body is not None: raise CLIError('原服务操作结果未确认；先查询现状，未自动重发。', 'RESULT_UNCERTAIN', 6) from None
            raise CLIError('未连接原发票助手；请先使用原工具启动服务。', 'SERVICE_UNAVAILABLE', 1) from None
        except (ValueError, UnicodeError):
            if body is not None: raise CLIError('原服务响应无效，操作结果待核对；未自动重发。', 'RESULT_UNCERTAIN', 6) from None
            raise CLIError('原服务响应无效。', 'INVALID_RESPONSE', 1) from None

    def runtime(self):
        value = self.request('/api/runtime')
        if not isinstance(value, dict) or value.get('app_id') not in {'invoice-print-layout.invoice-assistant', 'invoice-print-layout.workbench'} or value.get('ready') is not True:
            raise CLIError('目标端口不是就绪的原发票服务。', 'SERVICE_IDENTITY', 4)
        if value.get('workspace_id') != self.workspace_id:
            raise CLIError('原服务工作区与配置不一致；拒绝连接另一账本。', 'WORKSPACE_MISMATCH', 4)
        return value

    def state(self, refresh=False):
        if refresh or self._state is None:
            self.runtime()
            value = self.request('/api/state')
            if not isinstance(value, dict) or not isinstance(value.get('items'), list) or not isinstance(value.get('projects'), list):
                raise CLIError('原服务状态格式无效。', 'INVALID_RESPONSE', 1)
            self._state = value
        return self._state

    def get(self, path):
        self.runtime()
        return self.request(path)

    def project(self, project_id):
        if not project_id: raise CLIError('此命令必须指定稳定项目ID：--project。')
        project = next((p for p in self.state()['projects'] if p.get('id') == project_id), None)
        if not project or project.get('deleted'): raise CLIError('项目不存在或已删除。', 'PROJECT_NOT_FOUND', 4)
        return project

    def post(self, path, body):
        # The server is the sole writer. Retry only a definite pre-dispatch 403.
        if path == '/api/intake/prepare':
            self.operations['recognition_requests'] += 1
            self.operations['glm_calls'] = self.operations['jev_calls'] = None
        elif path == '/api/category/auto':
            self.operations['classification_requests'] += 1
            self.operations['jev_calls'] = None
        elif path == '/api/material-matching/create-batch': self.operations['draft_requests'] += 1
        for attempt in range(2):
            self.runtime()
            state = self.state(refresh=attempt > 0)
            try:
                result = self.request(path, body, state.get('token'))
                self._state = None
                return result
            except PermissionError:
                if attempt: raise CLIError('原服务令牌校验失败。', 'AUTH_REJECTED', 4) from None
