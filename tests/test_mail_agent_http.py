"""Mailbox controls share the existing local-origin and CSRF boundary."""
from pathlib import Path
from typing import Any

import pytest

from tests.test_invoice_assistant import invoice_server


def test_mail_controls_require_token_and_dispatch_exact_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import mail_agent
    calls: list[Any] = []

    class FakeMail:
        def __init__(self, store: Any, agent: Any):
            pass

        def status(self) -> dict[str, Any]:
            return {'enabled': False, 'messages': []}

        def configure(self, body: Any) -> dict[str, Any]:
            calls.append(('configure', body))
            return self.status()

        def collect_now(self) -> dict[str, Any]:
            calls.append(('collect',))
            return self.status()

        def assign(self, key: str, project: str) -> dict[str, Any]:
            calls.append(('assign', key, project))
            return self.status()

        def kick(self) -> None:
            pass

        def close(self) -> None:
            calls.append(('close',))

    monkeypatch.setattr(mail_agent, 'MailAgentService', FakeMail)
    with invoice_server(tmp_path) as client:
        assert client.json('GET', '/api/agent/mail') == (200, {'enabled': False, 'messages': []})
        payload = {'enabled': True, 'since': '2026-10-21', 'interval_seconds': 120,
                   'use_model': True, 'default_project_id': ''}
        for action, body in [('configure', payload), ('collect', {}), ('assign', {'id': 'mail-id', 'project_id': 'project-id'})]:
            path = '/api/agent/mail/' + action
            assert client.json('POST', path, body, authenticated=False)[0] == 403
            assert client.json('POST', path, body)[0] == 200
        assert calls == [('configure', payload), ('collect',), ('assign', 'mail-id', 'project-id')]
        assert client.request('GET', '/')[0] == 200
    assert calls[-1] == ('close',)
