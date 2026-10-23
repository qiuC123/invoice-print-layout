"""Verify appearance resources through the actual server in both app modes."""
from pathlib import Path
import threading
from urllib.request import urlopen

import pytest

from invoice_print_layout import workbench_web


@pytest.mark.parametrize('mode', ['invoice', 'workbench'])
@pytest.mark.parametrize(('asset', 'content_type'), [
    ('workbench-theme.js', 'text/javascript'), ('workbench-theme.css', 'text/css'),
])
def test_theme_assets_are_served_and_linked(tmp_path: Path, mode: str,
                                          asset: str, content_type: str) -> None:
    server = workbench_web.make_server(tmp_path, 0, mode=mode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/') as response:
            page = response.read().decode('utf-8')
        assert f'/{asset}' in page and 'id="themeSelect"' in page
        with urlopen(base + '/' + asset) as response:
            assert response.status == 200
            assert response.headers['Content-Type'].startswith(content_type)
            assert response.read() == (Path(workbench_web.__file__).parent / 'web' / asset).read_bytes()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
