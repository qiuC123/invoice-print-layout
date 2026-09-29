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
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
