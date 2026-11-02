"""Public tests use synthetic interfaces and never post events to the user's agent."""
from typing import Any
from urllib.parse import urlparse

import pytest

from invoice_print_layout import phone_link, project_events

pytest_plugins = ('tests.powershell_contracts',)


@pytest.fixture(autouse=True)
def isolate_personal_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    original = project_events.post_event

    def synthetic_only(url: str, payload: dict[str, Any]) -> None:
        if urlparse(url).port == 25820:
            raise ConnectionError('Synthetic tests require an explicit temporary event receiver')
        original(url, payload)

    monkeypatch.setattr(project_events, 'post_event', synthetic_only)


@pytest.fixture(autouse=True)
def synthetic_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    # Address/cache tests override this default with their own explicit interfaces.
    monkeypatch.setattr(phone_link, 'addresses', lambda: [])
