"""The opt-in agent page shares the ledger but retains local-origin protections."""
from __future__ import annotations

import base64
from pathlib import Path
import time
from typing import Any

from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.project_registry import ProjectRegistry
from tests.test_invoice_agent import proof
from tests.test_invoice_assistant import invoice_server


def test_agent_and_manual_pages_are_both_available(tmp_path: Path) -> None:
    with invoice_server(tmp_path) as client:
        status, page = client.request('GET', '/agent')
        assert status == 200 and '人工工作台'.encode() in page and b'href="/"' in page
        status, manual = client.request('GET', '/')
        assert status == 200 and b'id="manualAgent"' in manual and b'id="materialMatching"' in manual
        assert b'id="openManualAgent"' not in manual
        assert b'/manual-agent.js' in manual and b'id="list"' in manual
        assert b'href="/agent"' not in manual
        for path in ('/agent.js', '/agent.css'):
            assert client.request('GET', path)[0] == 200


def test_agent_csrf_project_scope_and_background_intake(tmp_path: Path) -> None:
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic HTTP Agent')['id']
    with invoice_server(tmp_path) as client:
        status, entry = client.json('POST', '/api/intake/receive', {
            'name': 'synthetic-order.pdf', 'data': base64.b64encode(proof(
                '收据\n商家名称：合成工具商店\n商品金额：17.00\n实付：17.00')).decode(), 'project_id': project})
        assert status == 200
        payload = {'project_id': project, 'material_ids': [entry['id']], 'idempotency_key': 'http-agent-request'}
        assert client.json('POST', '/api/agent/start', payload, authenticated=False)[0] == 403
        wrong = {**payload, 'project_id': 'not-a-project'}
        assert client.json('POST', '/api/agent/start', wrong)[0] == 400
        status, result = client.json('POST', '/api/agent/start', payload)
        assert status == 200
        key = result['job']['id']
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            status, result = client.json('GET', '/api/agent/job?id=' + key)
            assert status == 200
            job: dict[str, Any] = result['job']
            if job['state'] not in {'queued', 'running'}:
                break
            time.sleep(.1)
        assert job['state'] == 'ready' and job['tools'] >= 2 and job['models'] == 0
        assert not ExpenseStore(tmp_path).list_items()
        status, history = client.json('GET', '/api/agent/jobs?project=' + project)
        assert status == 200 and history['jobs'][0]['id'] == key
        assert client.json('POST', '/api/agent/start', payload)[1]['job']['id'] == key
        assert client.json('POST', '/api/agent/answer', {'id': key, 'choice': 'invented'})[0] == 400
        assert client.json('GET', '/api/agent/job?id=missing')[0] == 404
