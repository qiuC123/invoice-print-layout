"""Public tests use synthetic interfaces, never the runner's Wi-Fi or PowerShell probes."""
import pytest

from invoice_print_layout import phone_link


@pytest.fixture(autouse=True)
def synthetic_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    # Address/cache tests override this default with their own explicit interfaces.
    monkeypatch.setattr(phone_link, 'addresses', lambda: [])
