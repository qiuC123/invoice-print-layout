"""HTTP helpers shared by outbound API clients."""
from __future__ import annotations

from typing import Any
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None
