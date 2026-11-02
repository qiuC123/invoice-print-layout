from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import pytest

from invoice_print_layout.agent_jobs import AgentService
from tests.test_invoice_agent import environment
from tests.test_invoice_attention import EVIDENCE, material_question
from tests.test_invoice_assistant import invoice_server


def test_attention_api_read_paging_context_and_token_write(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    att, obj = material_question(environment)
    monkeypatch.setattr(AgentService, 'kick', lambda self: None)
    with invoice_server(att.store.workspace) as client:
        status, page = client.json('GET', '/api/attention?' + urlencode({'project': environment[2], 'offset': 0, 'limit': 1}))
        assert status == 200 and page['total'] == 1
        status, context = client.json('GET', '/api/attention/context?' + urlencode({'object': obj}))
        assert status == 200 and context['fields']['agent_review_reason']
        payload = {'object': obj, 'expected_version': context['version'], 'action': 'not_invoice',
                   'values': {}, 'decided_by': 'user', 'evidence': EVIDENCE}
        assert client.json('POST', '/api/attention/resolve', payload, authenticated=False)[0] == 403
        assert client.json('POST', '/api/attention/resolve', {**payload, 'expected_version': 'stale'})[0] == 400
        status, result = client.json('POST', '/api/attention/resolve', payload)
        assert status == 200 and result['status'] == 'excluded' and not result['verified']
        assert client.json('POST', '/api/attention/resolve', payload)[0] == 400
        assert client.json('GET', '/api/attention')[1]['total'] == 0
        assert client.json('GET', '/api/attention?limit=0')[0] == 404
    assert att.show(obj)['audit'][0]['decided_by'] == 'user'
