"""Public tests use synthetic interfaces and never post events to the user's agent."""
from collections.abc import Iterator

import pytest

from invoice_print_layout import phone_link
from tests.event_safety import guard_personal_agent

pytest_plugins = ('tests.powershell_contracts',)


@pytest.fixture(autouse=True)
def isolate_personal_agent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    blocked: list[tuple] = []
    guard_personal_agent(monkeypatch, blocked)
    yield
    assert not blocked, f'Test attempted real event delivery: {blocked}'


@pytest.fixture(autouse=True)
def synthetic_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    # Address/cache tests override this default with their own explicit interfaces.
    monkeypatch.setattr(phone_link, 'addresses', lambda: [])
