"""Synthetic service tests must not post their events to the user's agent."""
from typing import Any
from urllib.parse import urlparse

import pytest

from invoice_print_layout import project_events


@pytest.fixture(autouse=True)
def isolate_personal_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    original = project_events.post_event

    def synthetic_only(url: str, payload: dict[str, Any]) -> None:
        if urlparse(url).port == 25820:
            raise ConnectionError('Synthetic tests require an explicit temporary event receiver')
        original(url, payload)

    monkeypatch.setattr(project_events, 'post_event', synthetic_only)
