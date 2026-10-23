"""Authenticated routes expose bulk return and the actual final print artifact."""
from __future__ import annotations

from typing import Any

import pytest

from invoice_print_layout import workbench_web
from tests.test_invoice_assistant import invoice_server
from tests.test_review_return import _ledger, _ready, setup


def test_http_return_checks_token_scope_and_versions_before_returning_whole_batch(setup: Any) -> None:
    store, project, foreign = setup
    items = [_ready(store, project, 'First reviewed fee'), _ready(store, project, 'Second reviewed fee', missing=True)]
    body = {'project_id': project, 'ids': [item['id'] for item in items],
            'versions': {item['id']: item['version'] for item in items}}
    with invoice_server(store.workspace) as client:
        before = _ledger(store)
        assert client.json('POST', '/api/review/return', body, authenticated=False)[0] == 403
        assert _ledger(store) == before
        assert client.json('POST', '/api/review/return', {**body, 'project_id': foreign})[0] == 400
        assert _ledger(store) == before
        assert client.json('POST', '/api/review/return', {**body, 'versions': {}})[0] == 400
        assert _ledger(store) == before
        status, result = client.json('POST', '/api/review/return', body)
        assert status == 200 and result['changed'] == 2
        assert [item['id'] for item in result['items']] == body['ids']
        assert all(item['pending_review'] and not item['ready'] and not item['verified'] for item in result['items'])
        after = _ledger(store)
        assert client.json('POST', '/api/review/return', body)[0] == 400
        assert _ledger(store) == after


def test_http_report_preview_calls_readonly_final_layout_helper_with_current_build(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import report_print_preview
    store, project, _ = setup
    item = _ready(store, project)
    options = {'person': 'Synthetic reviewer', 'payment': 'personal'}
    calls = []
    response = {'combined_pdf': 'synthetic-final.pdf', 'pdf': 'synthetic-final.pdf',
                'combined_preview_pages': ['table.png', 'invoice.png'],
                'preview_pages': ['table.png', 'invoice.png'],
                'combined_page_sizes': [[842, 595], [595, 842]], 'table_page_count': 1, 'page_count': 2}

    def fake_preview(current_store: Any, ids: Any, actual_options: Any, *, build_id: Any) -> dict[str, Any]:
        calls.append((current_store.workspace, ids, actual_options, build_id))
        return response

    monkeypatch.setattr(report_print_preview, 'create_report_print_preview', fake_preview)
    with invoice_server(store.workspace) as client:
        before = _ledger(store)
        body = {'ids': [item['id']], 'options': options}
        assert client.json('POST', '/api/report-preview', body, authenticated=False)[0] == 403
        assert calls == [] and _ledger(store) == before
        status, result = client.json('POST', '/api/report-preview', body)
        assert status == 200 and result == response
        assert calls == [(store.workspace, [item['id']], options, workbench_web.STARTUP_BUILD_ID)]
        assert _ledger(store) == before


def test_http_formal_report_includes_the_same_table_as_final_preview(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import reimbursement
    store, project, _ = setup
    item = _ready(store, project)
    calls = []
    response = {'pdf': 'final.pdf', 'md': 'details.md', 'xlsx': 'table.xlsx'}

    def fake_report(current_store: Any, ids: Any, options: Any, *, include_table: bool = False) -> dict[str, str]:
        calls.append((current_store.workspace, ids, options, include_table))
        return response

    monkeypatch.setattr(reimbursement, 'create_report', fake_report)
    with invoice_server(store.workspace) as client:
        body = {'ids': [item['id']], 'options': {'person': 'Synthetic'}}
        assert client.json('POST', '/api/report', body, authenticated=False)[0] == 403
        assert calls == []
        status, result = client.json('POST', '/api/report', body)
        assert status == 200 and result == response
        assert calls == [(store.workspace, body['ids'], body['options'], True)]


def test_new_filter_and_report_assets_are_available_in_invoice_mode(setup: Any) -> None:
    store, _, _ = setup
    with invoice_server(store.workspace) as client:
        for path in ['/multi-filter.js', '/multi-filter.css', '/report-preview.css']:
            status, payload = client.request('GET', path)
            assert status == 200 and payload.strip()
            # Verify this asset response is usable CSS/JS, never a JSON 404 body.
            assert not payload.startswith(b'{"error"')
