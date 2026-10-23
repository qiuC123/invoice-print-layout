"""The project workspace loads its detail module through the real local server."""

from pathlib import Path
import threading
from urllib.request import urlopen

from invoice_print_layout import workbench_web


def test_agent_detail_assets_are_served_and_manual_entry_is_preserved(tmp_path: Path) -> None:
    server = workbench_web.make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(origin + "/agent") as response:
            page = response.read().decode("utf-8")
        for name, content_type in [
            ("agent-details.js", "text/javascript"), ("agent-details.css", "text/css"),
            ("agent-projects.js", "text/javascript"), ("agent-projects.css", "text/css"),
        ]:
            assert f'/{name}' in page
            with urlopen(origin + "/" + name) as response:
                assert response.headers["Content-Type"].startswith(content_type)
                assert response.read() == (Path(workbench_web.__file__).parent / "web" / name).read_bytes()
        for identifier in ["workspace-materials", "workspace-expenses", "workspace-details", "project-navigation"]:
            assert f'id="{identifier}"' in page
        with urlopen(origin + "/") as response:
            manual = response.read().decode("utf-8")
            assert response.status == 200
            assert manual != page
            assert 'id="list"' in manual
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
