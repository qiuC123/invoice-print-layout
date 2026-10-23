"""Serve the mailbox controls with the real Agent page and preserve the manual route."""

from pathlib import Path
import threading
from urllib.request import urlopen

from invoice_print_layout import workbench_web


def test_real_server_serves_agent_mail_controls_and_original_manual_page(tmp_path: Path) -> None:
    server = workbench_web.make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(origin + "/agent") as response:
            page = response.read().decode("utf-8")
        assert 'id="mail-heading"' in page
        assert 'id="mail-configure"' in page
        assert 'id="manual" href="/"' in page
        assert 'type="password"' not in page
        for asset, content_type in [("agent.js", "text/javascript"), ("agent.css", "text/css")]:
            assert f'/{asset}' in page
            with urlopen(origin + "/" + asset) as response:
                assert response.headers["Content-Type"].startswith(content_type)
                assert response.read() == (Path(workbench_web.__file__).parent / "web" / asset).read_bytes()
        with urlopen(origin + "/") as response:
            assert response.status == 200
            assert response.read() != page.encode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
