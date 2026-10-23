"""The enhanced original desk loads its bounded organizer in the same page."""
from pathlib import Path
import threading
from urllib.request import urlopen

from invoice_print_layout import workbench_web


def test_original_workbench_serves_embedded_agent_assets(tmp_path: Path) -> None:
    server = workbench_web.make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f'http://127.0.0.1:{server.server_port}'
        with urlopen(origin + '/') as response:
            page = response.read().decode('utf-8')
        assert 'id="manualAgent"' in page and 'id="list"' in page and 'id="materialMatching"' in page
        assert 'href="/agent"' not in page
        assert 'id="openManualAgent"' not in page
        for name in ['manual-agent.js', 'manual-agent-bridge.js', 'manual-agent.css']:
            assert '/' + name in page
            with urlopen(origin + '/' + name) as response:
                assert response.status == 200
                assert response.read() == (Path(workbench_web.__file__).parent / 'web' / name).read_bytes()
                assert response.headers['Content-Type'].startswith('text/css' if name.endswith('.css') else 'text/javascript')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
