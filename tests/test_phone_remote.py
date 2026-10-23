from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from invoice_print_layout import phone_link
from invoice_print_layout.phone_remote import PrivateNetwork, private_origin
from invoice_print_layout.workbench_web import make_server
from tests.test_phone_link import Client, pair, test_shared_ledger_upload_edit_verify_and_stale_guard as shared_flow

ORIGIN = 'http://invoice-desktop.tailtest.ts.net'


def running() -> dict[str, Any]:
    return {'BackendState': 'Running', 'Self': {'DNSName': 'invoice-desktop.tailtest.ts.net.',
                                              'TailscaleIPs': ['100.80.1.2', 'fd7a:115c:a1e0::1']}}


@pytest.mark.parametrize('name,ip,state', [
    ('invoice.example.com', '100.80.1.2', 'Running'),
    ('invoice.tailtest.ts.net.evil.test', '100.80.1.2', 'Running'),
    ('invoice.tailtest.ts.net', '8.8.8.8', 'Running'),
    ('invoice.tailtest.ts.net', '192.168.1.2', 'Running'),
    ('invoice.tailtest.ts.net', '100.80.1.2', 'NeedsLogin'),
    ('invoice.tailtest.ts.net', 'invalid', 'Running'),
])
def test_only_confirmed_private_node_origin_is_accepted(name: str, ip: str, state: str) -> None:
    assert private_origin(running()) == ORIGIN
    with pytest.raises(ValueError):
        private_origin({'BackendState': state, 'Self': {'DNSName': name, 'TailscaleIPs': [ip]}})


def test_private_network_is_opt_in_and_preserves_pending_login(tmp_path: Path, monkeypatch: Any) -> None:
    network = PrivateNetwork(tmp_path)
    assert network.status() == {'installed': False, 'state': 'NotInstalled', 'ready': False}
    with pytest.raises(ValueError, match='prepare-phone-remote'):
        network.connect()
    network.cli.parent.mkdir(parents=True)
    network.cli.touch()
    network.daemon.touch()
    pending = {'BackendState': 'NeedsLogin', 'AuthURL': 'https://login.tailscale.com/a/1234abc'}
    monkeypatch.setattr(network, 'raw_status', lambda: pending)
    monkeypatch.setattr(network, 'background', lambda *_: pytest.fail('Must not replace an active device login'))
    assert network.connect()['auth_url'] == pending['AuthURL']
    pending['AuthURL'] = 'https://login.tailscale.com.evil.test/a/123'
    assert 'auth_url' not in network.status()
    assert PrivateNetwork(tmp_path / 'second').pipe != network.pipe


def test_private_forward_is_exact_and_funnel_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    network = PrivateNetwork(tmp_path)
    monkeypatch.setattr(network, 'raw_status', running)
    expected: dict[str, Any] = {'TCP': {'80': {'HTTP': True}}, 'Web': {
        'invoice-desktop.tailtest.ts.net:80': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8768'}}}}}
    config: dict[str, Any] = {}
    calls = []
    public = False

    def run(*args: str) -> str:
        nonlocal config
        calls.append(args)
        if args == ('serve', 'status', '--json'):
            return json.dumps(config)
        if args == ('serve', '--http=80', 'off'):
            config = {}
        else:
            assert args == ('serve', '--bg', '--http=80', 'http://127.0.0.1:8768')
            config = dict(expected)
            if public:
                config['AllowFunnel'] = {'invoice-desktop.tailtest.ts.net:80': True}
        return ''

    monkeypatch.setattr(network, 'run', run)
    assert network.expose(8768) == ORIGIN
    assert config == expected
    assert network.expose(8768) == ORIGIN  # Recover the same mapping after an app crash.
    network.close()
    public = True
    with pytest.raises(ValueError, match='预期不符'):
        network.expose(8768)
    assert not config and calls[-1] == ('serve', '--http=80', 'off')
    config = {'TCP': {'443': {'HTTPS': True}}}
    before = len(calls)
    with pytest.raises(ValueError, match='已有转发'):
        network.expose(8768)
    assert len(calls) == before + 1  # Unknown settings were not overwritten.


def test_logged_in_windows_profile_survives_login_client_exit(tmp_path: Path, monkeypatch: Any) -> None:
    network = PrivateNetwork(tmp_path)
    network.cli.parent.mkdir(parents=True)
    network.cli.touch()
    network.daemon.touch()
    state = {'BackendState': 'NoState'}
    monkeypatch.setattr(network, 'raw_status', lambda: state)

    class Finished:
        def poll(self) -> int:
            return 0

    def login(executable: Path, args: list[str], name: str) -> Any:
        assert executable == network.cli and name == 'login'
        # Native Windows detaches the user's profile on the last local client
        # disconnect unless RunAsDaemon is enabled. The real registered node
        # reproduced this; don't use a live account in the automated suite.
        if '--unattended=true' in args:
            state.update(running())
        return Finished()

    monkeypatch.setattr(network, 'background', login)
    assert network.connect()['ready'] is True
    assert network.login is not None and network.login.poll() == 0
    assert network.status()['ready'] is True


class RemoteClient(Client):
    def request(self, path: str, data: Any = None, **headers: str) -> tuple[int, Any]:
        return super().request(path, data, **{'Host': ORIGIN.removeprefix('http://'), 'Origin': ORIGIN, **headers})


@pytest.fixture
def remote_clients(tmp_path: Path, monkeypatch: Any) -> Any:
    class Network:
        ready = True
        closed = 0
        fails = False

        def __init__(self, _: Path) -> None:
            pass

        def status(self) -> dict[str, Any]:
            return {'installed': True, 'state': 'Running' if self.ready else 'NeedsLogin',
                    'ready': self.ready, 'origin': ORIGIN}

        def connect(self) -> dict[str, Any]:
            return self.status()

        def expose(self, port: int) -> str:
            assert port > 0
            if self.fails:
                raise ValueError('Synthetic private forwarding failure')
            return ORIGIN

        def close(self) -> None:
            Network.closed += 1

    monkeypatch.setattr(phone_link, 'PrivateNetwork', Network)
    monkeypatch.setattr(phone_link, 'addresses', lambda: [])
    real_server = phone_link.ThreadingHTTPServer
    monkeypatch.setattr(phone_link, 'ThreadingHTTPServer', lambda addr, handler: real_server((addr[0], 0), handler))
    server = make_server(tmp_path, 0, mode='invoice')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    desktop = Client(server.server_port)
    desktop.token = desktop.request('/api/state')[1]['token']
    try:
        assert desktop.request('/api/phone-link')[1]['enabled'] is False
        status, connection = desktop.request('/api/phone-link/start', {'mode': 'remote'})
        assert status == 200 and connection['mode'] == 'remote'
        assert connection['url'] == ORIGIN + '/mobile'
        assert connection['address'] == '127.0.0.1'
        yield desktop, RemoteClient(connection['port']), connection, tmp_path, Network
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_remote_origin_pairing_and_management_isolation(remote_clients: Any) -> None:
    desktop, mobile, connection, _, _ = remote_clients
    assert mobile.request('/mobile')[0] == 200
    assert mobile.request('/api/state')[0] == 403
    assert mobile.request('/mobile', Host='127.0.0.1:' + str(mobile.port))[0] == 403
    assert mobile.request('/mobile', Origin='https://evil.test')[0] == 403
    assert mobile.request('/mobile', **{'Tailscale-Funnel-Request': '?1'})[0] == 403
    assert mobile.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]},
                          Origin='https://evil.test', **{'X-Forwarded-Proto': 'http'})[0] == 403
    pair(mobile, connection)
    assert mobile.token != desktop.token
    assert mobile.request('/api/phone-link')[0] == 404
    assert mobile.request('/api/phone-link/connect', {})[0] == 403
    assert mobile.request('/api/runtime')[0] == 404
    assert mobile.request('/api/state', Origin='null')[0] == 403
    assert Client(desktop.port).request('/api/phone-link/connect', {})[0] == 403


def test_remote_shared_ledger_flow_and_versions(remote_clients: Any, monkeypatch: Any) -> None:
    shared_flow(remote_clients[:4], monkeypatch)


def test_remote_close_and_failed_reopen_leave_no_access(remote_clients: Any) -> None:
    desktop, mobile, connection, _, network = remote_clients
    pair(mobile, connection)
    assert desktop.request('/api/phone-link/stop', {})[1]['enabled'] is False
    assert network.closed == 1
    with pytest.raises(OSError):
        mobile.request('/api/state')
    network.fails = True
    assert desktop.request('/api/phone-link/start', {'mode': 'remote'})[0] == 400
    assert desktop.request('/api/phone-link')[1]['enabled'] is False
    network.fails = False
    new = desktop.request('/api/phone-link/start', {'mode': 'remote'})[1]
    mobile.port = new['port']
    assert mobile.request('/api/state')[0] == 403
    assert mobile.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]})[0] == 403
    pair(mobile, new)
    assert len(desktop.request('/api/state')[1]['items']) == 0


def test_remote_does_not_expose_until_account_is_connected(remote_clients: Any) -> None:
    desktop, _, _, _, network = remote_clients
    desktop.request('/api/phone-link/stop', {})
    network.ready = False
    result = desktop.request('/api/phone-link/start', {'mode': 'remote'})
    assert result[0] == 200 and result[1]['enabled'] is False
    assert result[1]['remote']['state'] == 'NeedsLogin'
