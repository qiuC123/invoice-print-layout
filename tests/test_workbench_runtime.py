import json
from threading import Thread
from urllib.request import urlopen

from invoice_print_layout import workbench_web as web
from invoice_print_layout.workbench_runtime import identity, source_build_id


def test_build_tracks_source_not_workspace_and_does_not_leak_path(tmp_path):
    package = tmp_path/'package'
    package.mkdir()
    code = package/'a.py'
    code.write_text('old')
    before = source_build_id(package)
    code.write_text('new')
    assert source_build_id(package) != before
    before_asset = source_build_id(package)
    (package / 'app.js').write_text('new frontend')
    assert source_build_id(package) != before_asset
    before_native = source_build_id(package)
    native = package / 'report_excel_pdf.ps1'
    native.write_text('native PDF export')
    assert source_build_id(package) != before_native
    before_native = source_build_id(package)
    native.write_text('updated native PDF export')
    assert source_build_id(package) != before_native
    data = identity(tmp_path, before)
    assert str(tmp_path) not in json.dumps(data)
    assert identity(tmp_path/'other', before)['workspace_id'] != data['workspace_id']


def test_runtime_identity_is_frozen_after_server_creation(tmp_path, monkeypatch):
    server = web.make_server(tmp_path, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def read():
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/runtime') as response:
                return json.load(response)
        before = read()
        monkeypatch.setattr(web, 'source_build_id', lambda: 'changed-on-disk')
        monkeypatch.setattr(web, 'STARTUP_BUILD_ID', 'changed-later')
        assert read() == before
        assert before['build_id'] != 'changed-on-disk'
        assert before['ready'] and before['started_at']
        assert before['workspace_id'] == identity(tmp_path, '')['workspace_id']
        assert 'category_suggestion' in before['capabilities']
        assert 'token' not in before
        with urlopen(f'http://127.0.0.1:{server.server_port}/favicon.svg') as response:
            assert response.headers.get_content_type() == 'image/svg+xml'
            assert b'<svg' in response.read()
        with urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
            assert b'href="/favicon.svg"' in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_servers_queue_a_full_page_burst_of_connections(tmp_path):
    # Edge opens ~30 asset connections at once; a backlog of 5 refused some (2026-10-30).
    from invoice_print_layout.phone_link import MobileServer
    server = web.make_server(tmp_path, 0)
    try:
        assert server.request_queue_size >= 64
    finally:
        server.server_close()
    assert MobileServer.request_queue_size >= 64


def test_portless_loopback_origin_is_local_but_foreign_origins_are_not(tmp_path):
    # Edge with some extensions sends "Origin: http://127.0.0.1" on same-origin requests (2026-10-30).
    from http.client import HTTPConnection
    server = web.make_server(tmp_path, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        def get(path, origin):
            connection = HTTPConnection('127.0.0.1', port, timeout=10)
            try:
                connection.request('GET', path, headers={'Origin': origin} if origin else {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        for origin in (None, f'http://127.0.0.1:{port}', 'http://127.0.0.1', 'http://localhost'):
            assert get('/style.css', origin)[0] == 200
            assert get('/api/runtime', origin)[0] == 200
        for origin in ('http://127.0.0.1.evil.test', 'https://127.0.0.1', 'http://example.test', 'null'):
            assert get('/api/runtime', origin)[0] == 403
        token = json.loads(get('/api/state', 'http://127.0.0.1')[1])['token']
        connection = HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            connection.request('POST', '/api/settings/organize', json.dumps({'auto_after_upload': True}),
                               {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1', 'X-Workbench-Token': 'wrong'})
            response = connection.getresponse()
            assert response.status == 403
            response.read()
            connection.request('POST', '/api/settings/organize', json.dumps({'auto_after_upload': True}),
                               {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1', 'X-Workbench-Token': token})
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
