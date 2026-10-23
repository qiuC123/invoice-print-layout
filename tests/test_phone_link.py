from __future__ import annotations

import base64
from http.client import HTTPConnection
import json
import socket
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest

from invoice_print_layout import phone_link
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.helpers import make_invoice_pdf
import pymupdf


class Client:
    def __init__(self, port: int):
        self.port = port
        self.cookie = ''
        self.token = ''

    def request(self, path: str, data: Any = None, **headers: str) -> tuple[int, Any]:
        conn = HTTPConnection('127.0.0.1', self.port, timeout=30)
        options = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}',
                   'Cookie': self.cookie, 'X-Workbench-Token': self.token, **headers}
        conn.request('GET' if data is None else 'POST', path,
                     json.dumps(data).encode() if data is not None else None, options)
        response = conn.getresponse()
        if response.getheader('Set-Cookie'):
            self.cookie = response.getheader('Set-Cookie') or ''
        payload = response.read()
        mime = response.getheader('Content-Type', '')
        conn.close()
        return response.status, json.loads(payload) if mime.startswith('application/json') else payload


@pytest.fixture
def clients(tmp_path: Path, monkeypatch: Any) -> Any:
    # Test the real second HTTP listener on loopback, independently of the
    # developer's Wi-Fi interface, port and firewall state.
    original = phone_link.lan_address
    monkeypatch.setattr(phone_link, 'lan_address', lambda ip: ip == '127.0.0.1' or original(ip))
    monkeypatch.setattr(phone_link, 'addresses', lambda: [{'address': '127.0.0.1', 'label': 'Test LAN'}])
    real_server = phone_link.ThreadingHTTPServer
    monkeypatch.setattr(phone_link, 'ThreadingHTTPServer', lambda addr, handler: real_server((addr[0], 0), handler))
    server = make_server(tmp_path, 0, mode='invoice')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    desktop = Client(server.server_port)
    desktop.token = desktop.request('/api/state')[1]['token']
    try:
        assert desktop.request('/api/phone-link')[1]['enabled'] is False
        status, connection = desktop.request('/api/phone-link/start', {'address': '127.0.0.1'})
        assert status == 200
        yield desktop, Client(connection['port']), connection, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def pair(client: Client, connection: dict[str, Any]) -> None:
    assert client.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]}) == (200, {'ok': True})
    assert 'HttpOnly' in client.cookie and 'SameSite=Strict' in client.cookie
    client.token = client.request('/api/state')[1]['token']


def test_default_address_scope() -> None:
    for ip in ('10.1.2.3', '192.168.31.4', '172.24.80.1'):
        assert phone_link.lan_address(ip)
    for ip in ('0.0.0.0', '127.0.0.1', '8.8.8.8', '::1', '169.254.1.2', '100.64.1.2', 'invalid'):
        assert not phone_link.lan_address(ip)


def test_pairing_qr_and_session_isolation(clients: Any) -> None:
    desktop, mobile, connection, _ = clients
    assert mobile.request('/api/state')[0] == 403
    assert mobile.request('/api/phone/pair', {'code': 'invalid'})[0] == 403
    assert mobile.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]}, Origin='http://evil.test')[0] == 403
    image = np.frombuffer(base64.b64decode(connection['qr'].split(',')[1]), dtype=np.uint8)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.imdecode(image, cv2.IMREAD_COLOR))
    assert decoded == connection['pair_url']
    pair(mobile, connection)
    assert mobile.token != desktop.token
    assert desktop.request('/api/phone-link')[1]['clients'] == 1
    assert mobile.request('/api/state', Host='evil.test')[0] == 403
    assert mobile.request('/api/state', Origin='http://evil.test')[0] == 403
    assert mobile.request('/api/phone-link')[0] == 404
    assert mobile.request('/api/runtime')[0] == 404
    assert mobile.request('/api/shutdown', {})[0] == 403
    assert mobile.request('/api/mail', {})[0] == 403
    assert mobile.request('/api/transition', {'action': 'submitted'})[0] == 400
    assert mobile.request('/api/intake/receive', {}, **{'X-Workbench-Token': desktop.token})[0] == 403
    assert mobile.request('/api/intake/receive', {}, Origin='null')[0] == 403
    assert mobile.request('/api/intake/analyze', {'id': 'none', 'force_ai': True})[0] == 400


def test_rotating_qr_and_closing_invalidates_connections(clients: Any) -> None:
    desktop, mobile, old, _ = clients
    pair(mobile, old)
    new = desktop.request('/api/phone-link/rotate', {})[1]
    assert new['pair_url'] != old['pair_url']
    other = Client(mobile.port)
    assert other.request('/api/phone/pair', {'code': old['pair_url'].split('#')[1]})[0] == 403
    assert mobile.request('/api/state')[0] == 200  # Existing phone stays connected.
    pair(other, new)
    assert desktop.request('/api/phone-link/stop', {})[1]['enabled'] is False
    with pytest.raises(OSError):
        mobile.request('/api/state')
    status, restarted = desktop.request('/api/phone-link/start', {'address': '127.0.0.1'})
    assert status == 200
    mobile.port = restarted['port']
    assert mobile.request('/api/state')[0] == 403
    assert mobile.request('/api/phone/pair', {'code': new['pair_url'].split('#')[1]})[0] == 403
    assert desktop.request('/api/state')[0] == 200


def test_expired_pair_code_and_session_cannot_read(clients: Any, monkeypatch: Any) -> None:
    _, mobile, connection, _ = clients
    pair(mobile, connection)
    now = time.monotonic()
    monkeypatch.setattr(phone_link, 'time', SimpleNamespace(monotonic=lambda: now + phone_link.PAIR_SECONDS + 1))
    other = Client(mobile.port)
    assert other.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]})[0] == 403
    assert mobile.request('/api/state')[0] == 200
    monkeypatch.setattr(phone_link, 'time', SimpleNamespace(monotonic=lambda: now + phone_link.SESSION_SECONDS + 1))
    assert mobile.request('/api/state')[0] == 403


def test_idle_unpaired_connection_does_not_hang_stop(clients: Any, monkeypatch: Any) -> None:
    desktop, mobile, _, _ = clients
    monkeypatch.setattr(phone_link, 'SOCKET_TIMEOUT', 0.25)
    with socket.create_connection(('127.0.0.1', mobile.port)) as idle:
        idle.sendall(b'GET /api/state HTTP/1.1\r\n')  # Incomplete headers.
        started = time.monotonic()
        assert desktop.request('/api/phone-link/stop', {})[1]['enabled'] is False
        assert time.monotonic() - started < 3


def test_shared_ledger_upload_edit_verify_and_stale_guard(clients: Any, monkeypatch: Any) -> None:
    desktop, mobile, connection, root = clients
    pair(mobile, connection)
    project = desktop.request('/api/projects', {'name': 'Synthetic phone flow'})[1]['id']
    invoice = make_invoice_pdf(root / 'invoice.pdf', amount='15.00').read_bytes()
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((30, 50), '交易成功\n实付款：15.00\n收款方：Synthetic shop', fontname='china-s', fontsize=16)
        payment = doc.tobytes()
    received = []
    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('Phone recognition must stay local')
    monkeypatch.setattr('invoice_print_layout.glm_invoice_ai.GlmInvoiceAI.extract', no_network)
    monkeypatch.setattr('invoice_print_layout.glm_invoice_ai.GlmInvoiceAI.__init__', no_network)
    monkeypatch.setattr('invoice_print_layout.invoice_claim.claim', no_network)
    monkeypatch.setattr('invoice_print_layout.automatic_rides.organize_rides', no_network)
    for role, data in [('invoice', invoice), ('payment', payment)]:
        status, entry = mobile.request('/api/intake/receive', {'name': role + '.pdf', 'project_id': project,
                                        'data': base64.b64encode(data).decode()})
        assert status == 200 and 'path' not in entry
        analyzed = mobile.request('/api/intake/analyze', {'id': entry['id']})
        assert analyzed[0] == 200 and 'path' not in analyzed[1]
        assert not desktop.request('/api/state')[1]['items']  # Recognition creates no expense.
        status, entry = mobile.request('/api/intake/classify', {'id': entry['id'], 'revision': analyzed[1]['revision'],
                                     'role': role, 'fields': {'amount': '15.00', 'merchant': 'Synthetic shop',
                                                             'payment_status': 'succeeded', 'title': role}})
        assert status == 200
        received.append(entry)
    suggestions = mobile.request('/api/material-matching/suggestions?project=' + project)[1]['suggestions']
    assert len(suggestions) == 1 and not suggestions[0]['ambiguous']
    suggestion = suggestions[0]
    body = {'project_id': project, 'voucher_keys': suggestion['voucher_keys'], 'payment_ids': suggestion['payment_ids'],
            'revisions': suggestion['revisions'], 'expected_versions': suggestion['expected_versions'],
            'suggestion_id': suggestion['id'], 'suggestion_token': suggestion['snapshot_token'],
            'fields': {'project_type': 'other', 'project_name': 'Phone expense', 'amount': '15.00'}}
    status, result = mobile.request('/api/material-matching/create', body)
    assert status == 200
    item = result['item']
    assert not item['verified'] and item['pending_review'] and item['complete']
    assert len(item['attachments']) == 2
    assert desktop.request('/api/state')[1]['items'][0]['id'] == item['id']
    # A desktop edit is immediately visible after a phone refresh. Stale saves fail.
    status, edited = desktop.request('/api/review/edit-fields', {'id': item['id'], 'expected_version': item['version'],
                                          'project_type': 'other', 'project_name': 'Desktop correction', 'amount': '15.00'})
    assert status == 200
    assert mobile.request('/api/state')[1]['items'][0]['project_name'] == 'Desktop correction'
    assert mobile.request('/api/transition', {'id': item['id'], 'action': 'verify'})[0] == 400
    assert mobile.request('/api/review/edit-fields', {'id': item['id'], 'expected_version': item['version'],
                         'project_type': 'other', 'project_name': 'Stale', 'amount': '16.00'})[0] == 400
    status, verified = mobile.request('/api/transition', {'id': item['id'], 'expected_version': edited['version'], 'action': 'verify'})
    assert status == 200 and verified['verified']
    assert desktop.request('/api/state')[1]['items'][0]['ready']
    assert mobile.request('/api/transition', {'id': item['id'], 'expected_version': verified['version'], 'action': 'submitted'})[0] == 400
    for entry, original in zip(received, [invoice, payment]):
        assert mobile.request('/intake-file/' + entry['id'])[1] == original
    for attachment in item['attachments']:
        assert mobile.request('/attachment-preview/' + attachment['id'])[0] == 200
        assert b'page=0' in mobile.request('/attachment-preview/' + attachment['id'])[1]
    assert len(ExpenseStore(root).list_items()) == 1


def test_expired_code_and_session_are_rejected(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(phone_link, 'PAIR_SECONDS', -1)
    monkeypatch.setattr(phone_link, 'SESSION_SECONDS', -1)
    monkeypatch.setattr(phone_link, 'addresses', lambda: [{'address': '192.168.1.1', 'label': 'test'}])
    controller = phone_link.PhoneLink(object, '', lambda _: {}, tmp_path)  # type: ignore[arg-type]
    controller.sessions = {'old': ('token', time.monotonic() - 1)}
    assert controller.status()['clients'] == 0
    assert controller.status()['expires_in'] == 0
    assert controller.stop()['enabled'] is False
    with pytest.raises(ValueError):
        controller.start('8.8.8.8')
    with pytest.raises(ValueError):
        controller.start('192.168.99.1')
