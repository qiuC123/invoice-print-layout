import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.workbench_web import make_server


def test_site_photos_only_serves_admitted_project_files(tmp_path: Path) -> None:
    server = make_server(tmp_path, 0)
    store = LogisticsStore(tmp_path / '后勤' / 'tasks.sqlite3')
    first = store.create_project('南昌测试')
    second = store.create_project('其他测试')
    folder = tmp_path / '现场进度' / first['id']
    folder.mkdir(parents=True)
    (folder / '现场进度资料.html').write_text('<h1>南昌照片</h1>', encoding='utf-8')
    (folder / 'approved.png').write_bytes(b'synthetic image')
    (folder / 'unreviewed.png').write_bytes(b'not admitted')
    (folder / '照片台账.json').write_text(json.dumps({'project_id': first['id'], 'photos': [{'relative_path': 'approved.png'}]}), encoding='utf-8')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}/site-photos/'
    try:
        with urlopen(base + first['id'] + '/index.html') as response:
            assert '南昌照片' in response.read().decode()
        with urlopen(base + first['id'] + '/approved.png') as response:
            assert response.read() == b'synthetic image'
        with urlopen(base + second['id'] + '/index.html') as response:
            assert '尚无' in response.read().decode()
        for suffix in [first['id']+'/unreviewed.png', second['id']+'/approved.png', first['id']+'/照片台账.json', first['id']+'/%2e%2e/secret.png', 'missing/index.html']:
            from urllib.parse import quote
            with pytest.raises(HTTPError) as error:
                urlopen(base + quote(suffix, safe='/%'))
            assert error.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
