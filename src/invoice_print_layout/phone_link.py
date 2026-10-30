"""Explicitly enabled LAN browser access to the existing desktop ledger."""
from __future__ import annotations

import base64
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from invoice_print_layout.phone_remote import PrivateNetwork

GET_ROUTES = frozenset({'/api/state', '/api/intake', '/api/material-matching',
                        '/api/material-matching/suggestions', '/api/reimbursement-people'})
FILE_PREFIXES = ('/intake-file/', '/intake-page/', '/intake-preview/', '/file/', '/attachment-page/', '/attachment-preview/')
POST_ROUTES = frozenset({'/api/intake/receive', '/api/intake/classify', '/api/intake/analyze',
                         '/api/material-matching/create', '/api/review/edit-fields', '/api/review/confirm',
                         '/api/reimbursement-people/assign-expense', '/api/review/return', '/api/transition'})
COOKIE = 'invoice_phone'
PAIR_SECONDS = 600
SESSION_SECONDS = 12 * 3600
SOCKET_TIMEOUT = 10.0


def lan_address(value: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(value)
        return any(addr in ipaddress.IPv4Network(network) for network in
                   ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except ipaddress.AddressValueError:
        return False


def addresses() -> list[dict[str, str]]:
    """Prefer real interfaces with a gateway, without contacting the internet."""
    found: list[dict[str, str]] = []
    if os.name == 'nt':
        command = ("Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway} | "
                   "ForEach-Object { $n=$_.InterfaceAlias; $_.IPv4Address | "
                   "ForEach-Object { [PSCustomObject]@{address=$_.IPAddress;label=$n} } } | ConvertTo-Json -Compress")
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, text=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            rows = json.loads(result.stdout or '[]')
            if isinstance(rows, dict):
                rows = [rows]
            found = [row for row in rows if isinstance(row, dict) and lan_address(str(row.get('address', '')))
                     and not any(word in str(row.get('label', '')).lower()
                                 for word in ('tun', 'vpn', 'vethernet', 'virtual', 'tailscale'))]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    if not found:
        try:
            found = [{'address': str(row[4][0]), 'label': '本地网络'} for row in
                     socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                     if lan_address(str(row[4][0]))]
        except OSError:
            pass
    return list({row['address']: row for row in found}.values())


def public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items()
                if key not in {'path', 'token', 'source_path', 'workspace'}}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def drain_rejected_body(handler: BaseHTTPRequestHandler) -> None:
    """Consume a bounded normal POST before closing, avoiding Windows TCP reset.

    No JSON is parsed and no business operation runs. An incomplete request is
    only given a short window; it cannot hold the desktop close action forever.
    """
    previous = handler.connection.gettimeout()
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if 0 < length <= 29 * 1024 * 1024:
            handler.connection.settimeout(1.0)
            until = time.monotonic() + 2
            while length > 0 and time.monotonic() < until:
                chunk = handler.rfile.read1(min(length, 65536))
                if not chunk:
                    break
                length -= len(chunk)
    except (ValueError, OSError):
        pass
    finally:
        handler.connection.settimeout(previous)


class MobileServer(ThreadingHTTPServer):
    # Browsers request many page assets at once; the default backlog of 5 refuses some of them.
    request_queue_size = 128


class PhoneLink:
    def __init__(self, handler: type[BaseHTTPRequestHandler], desktop_token: str,
                 local_analyze: Callable[[str], dict[str, Any]], workspace: Path) -> None:
        self.handler = handler
        self.desktop_token = desktop_token
        self.local_analyze = local_analyze
        self.server: ThreadingHTTPServer | None = None
        self.worker: threading.Thread | None = None
        self.address = ''
        self.code = ''
        self.code_until = 0.0
        self.sessions: dict[str, tuple[str, float]] = {}
        self.lock = threading.RLock()
        self.assets = Path(__file__).parent / 'web'
        self.remote = PrivateNetwork(workspace)
        self.mode = 'lan'
        self.origin = ''

    def status(self, *, include_addresses: bool = False) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}
            value: dict[str, Any] = {'enabled': self.server is not None, 'mode': self.mode, 'clients': len(self.sessions),
                                     'expires_in': max(0, int(self.code_until - now))}
            if self.server:
                url = self.origin + '/mobile'
                value.update(address=self.address, port=self.server.server_port, url=url)
                if self.code_until > now:
                    import qrcode
                    from qrcode.image.pil import PilImage
                    output = io.BytesIO()
                    pair_url = url + '#' + self.code
                    qrcode.make(pair_url, image_factory=PilImage, box_size=6, border=4).save(output, format='PNG')
                    value.update(pair_url=pair_url, qr='data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
            if include_addresses:
                value['addresses'] = addresses()
                value['remote'] = self.remote.status()
            return value

    def connect_remote(self) -> dict[str, Any]:
        self.remote.connect()
        return self.status(include_addresses=True)

    def start_remote(self, port: int = 8768) -> dict[str, Any]:
        network = self.remote.status()
        if not network.get('ready'):
            return self.connect_remote()
        result = self._start('127.0.0.1', port, origin=network['origin'], mode='remote')
        try:
            if self.remote.expose(result['port']) != network['origin']:
                raise ValueError('私人网络账号或地址已变化，请刷新后重试')
        except ValueError:
            self.stop()
            raise
        return self.status()

    def start(self, address: str, port: int = 8767) -> dict[str, Any]:
        if not lan_address(address):
            raise ValueError('请选择本机局域网 IPv4 地址')
        if address not in {row['address'] for row in addresses()}:
            raise ValueError('网络地址已变化，请刷新后重新选择')
        return self._start(address, port, mode='lan')

    def _start(self, address: str, port: int, *, origin: str = '', mode: str) -> dict[str, Any]:
        with self.lock:
            if self.server:
                raise ValueError('手机连接已开启；需要更换地址请先关闭')
            controller = self
            base = self.handler

            class MobileHandler(base):  # type: ignore[valid-type, misc]
                phone_csrf = ''
                rfile: Any

                def setup(self) -> None:
                    self.request.settimeout(SOCKET_TIMEOUT)
                    super().setup()

                def allowed(self) -> bool:
                    server = controller.server
                    host = urlparse(controller.origin).netloc
                    request_origin = self.headers.get('Origin')
                    peer_allowed = (self.client_address[0] == '127.0.0.1' if controller.mode == 'remote'
                                    else lan_address(self.client_address[0]))
                    return bool(server and peer_allowed and
                                not self.headers.get('Tailscale-Funnel-Request') and
                                self.headers.get('Host') == host and
                                (request_origin is None or request_origin == controller.origin))

                def session(self) -> bool:
                    try:
                        cookie = SimpleCookie()
                        cookie.load(self.headers.get('Cookie', ''))
                        key = cookie[COOKIE].value if COOKIE in cookie else ''
                    except CookieError:
                        return False
                    with controller.lock:
                        current = controller.sessions.get(key)
                        if not current or current[1] <= time.monotonic():
                            return False
                        self.phone_csrf = current[0]
                    return True

                def respond(self, value: Any, code: int = 200) -> None:
                    value = public_value(value)
                    if isinstance(value, dict) and urlparse(self.path).path == '/api/state' and code == 200:
                        value['token'] = self.phone_csrf
                        value['inbox'] = []
                    super().respond(value, code)

                def denied(self, *, body_read: bool = False) -> None:
                    if self.command == 'POST' and not body_read:
                        drain_rejected_body(self)
                    self.respond({'error': '手机连接已关闭或未配对，请在电脑重新扫码。'}, 403)

                def do_GET(self) -> None:
                    if not self.allowed():
                        self.denied()
                        return
                    path = unquote(urlparse(self.path).path)
                    if path in {'/', '/mobile', '/mobile.js', '/mobile.css'}:
                        name = 'mobile.html' if path in {'/', '/mobile'} else path[1:]
                        mime = 'text/html; charset=utf-8' if name.endswith('.html') else (
                            'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8')
                        self.send((controller.assets / name).read_bytes(), mime)
                        return
                    if not self.session():
                        self.denied()
                    elif path in GET_ROUTES or path.startswith(FILE_PREFIXES):
                        super().do_GET()
                    else:
                        self.respond({'error': '此操作请在电脑端完成'}, 404)

                def do_POST(self) -> None:
                    if not self.allowed() or self.headers.get('Origin') != controller.origin:
                        self.denied()
                        return
                    path = unquote(urlparse(self.path).path)
                    if path != '/api/phone/pair' and not self.session():
                        self.denied()
                        return
                    if path != '/api/phone/pair' and (path not in POST_ROUTES or not secrets.compare_digest(
                            self.headers.get('X-Workbench-Token', ''), self.phone_csrf)):
                        self.denied()
                        return
                    try:
                        length = int(self.headers.get('Content-Length', '0'))
                        limit = 4096 if path == '/api/phone/pair' else 29 * 1024 * 1024
                        if not 0 < length <= limit:
                            raise ValueError('请求大小无效；附件最多20MB')
                        body = json.loads(self.rfile.read(length))
                        if not isinstance(body, dict):
                            raise ValueError('请求格式无效')
                        if path == '/api/phone/pair':
                            supplied = body.get('code')
                            with controller.lock:
                                if (not isinstance(supplied, str) or not controller.code or
                                        time.monotonic() >= controller.code_until or
                                        not secrets.compare_digest(supplied, controller.code) or
                                        len(controller.sessions) >= 10):
                                    self.denied(body_read=True)
                                    return
                                key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                                controller.sessions[key] = (csrf, time.monotonic() + SESSION_SECONDS)
                            payload = b'{"ok":true}'
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.send_header('Content-Length', str(len(payload)))
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('Set-Cookie', f'{COOKIE}={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}')
                            self.end_headers()
                            self.wfile.write(payload)
                            return
                        if path == '/api/transition' and body.get('action') != 'verify':
                            raise ValueError('手机端仅支持核对完成，提交和其他状态操作请在电脑完成')
                        if path == '/api/transition' and (not isinstance(body.get('expected_version'), str) or
                                                         not body['expected_version'].strip()):
                            raise ValueError('费用版本缺失，请刷新后重新核对')
                        if body.get('force_ai'):
                            raise ValueError('手机连接仅使用电脑本地识别')
                        if path == '/api/intake/analyze':
                            self.respond(controller.local_analyze(body['id']))
                            return
                        # Reuse the desktop route and its write lock, validation
                        # and optimistic versions, after checking the phone session.
                        encoded = json.dumps(body).encode()
                        self.rfile = io.BytesIO(encoded)
                        self.headers.replace_header('Content-Length', str(len(encoded)))
                        del self.headers['X-Workbench-Token']
                        self.headers['X-Workbench-Token'] = controller.desktop_token
                        super().do_POST()
                    except (ValueError, KeyError, OSError, TypeError) as exc:
                        self.respond({'error': str(exc)}, 400)

            server = MobileServer((address, port), MobileHandler)
            server.daemon_threads = False
            self.address, self.code = address, secrets.token_urlsafe(32)
            self.mode = mode
            self.origin = origin or f'http://{address}:{server.server_port}'
            self.code_until = time.monotonic() + PAIR_SECONDS
            self.sessions.clear()
            self.server = server
            self.worker = threading.Thread(target=server.serve_forever, daemon=True)
            self.worker.start()
        return self.status()

    def rotate(self) -> dict[str, Any]:
        with self.lock:
            if not self.server:
                raise ValueError('请先开启手机连接')
            self.code, self.code_until = secrets.token_urlsafe(32), time.monotonic() + PAIR_SECONDS
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            server, worker = self.server, self.worker
            remote_active = server is not None and self.mode == 'remote'
            self.server = None
            self.worker = None
            self.code = ''
            self.code_until = 0.0
            self.sessions.clear()
        if server:
            server.shutdown()
            server.server_close()
        if worker:
            worker.join(timeout=5)
        result = self.status()
        if remote_active:
            try:
                self.remote.close()
            except ValueError:
                # Even if the client is unavailable the loopback listener is
                # now gone and all paired sessions have been revoked.
                result['warning'] = '手机入口和会话已关闭；私人网络组件未响应，请刷新后检查连接状态'
        return result
