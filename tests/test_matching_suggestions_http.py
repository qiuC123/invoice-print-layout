from __future__ import annotations

import json
import re
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.material_matching_helpers import payment
from tests.test_material_matching import queued, snapshots
from tests.test_matching_suggestions import hotel_group


def test_page_scripts_are_served_including_synthesis(tmp_path: Path) -> None:
    server = make_server(tmp_path, 0, mode='invoice')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root + '/') as response:
            page = response.read().decode('utf-8')
        scripts = re.findall(r'<script src="([^"]+)"', page)
        assert '/synthesis.js' in scripts
        for script in scripts:
            with urlopen(root + script) as response:
                assert response.status == 200
                assert response.headers['Content-Type'].startswith('text/javascript')
                assert response.read()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture(params=[False, True])
def http_matcher(tmp_path: Path, monkeypatch: Any, request: Any) -> Any:
    enabled = request.param

    class NoCallsAI:
        def __init__(self, *args: Any) -> None:
            self.enabled = enabled

        def public_status(self) -> dict[str, Any]:
            return {'enabled': self.enabled, 'available': self.enabled, 'model': 'synthetic-model'}

        def extract(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError('Suggestion scans must never extract or send original material')

        def decide(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError('Suggestion scans must never use a model decision')

    monkeypatch.setattr('invoice_print_layout.workbench_web.GlmInvoiceAI', NoCallsAI)
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic candidate project')['id']
    matcher = MaterialMatching(store, IntakeQueue(store))
    hotel_group(matcher, project)
    server = make_server(tmp_path, 0, mode='invoice')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    root = f'http://127.0.0.1:{server.server_port}'

    def get(route: str) -> Any:
        with urlopen(root + route) as response:
            return json.load(response)

    token = get('/api/state')['token']

    def post(route: str, data: dict[str, Any]) -> Any:
        with urlopen(Request(root + route, data=json.dumps(data).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
            return json.load(response)

    try:
        yield matcher, project, get, post, enabled
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def confirmation(suggestion: dict[str, Any]) -> dict[str, Any]:
    return {'project_id': suggestion['project_id'], 'voucher_keys': suggestion['voucher_keys'],
            'payment_ids': suggestion['payment_ids'], 'revisions': suggestion['revisions'],
            'expected_versions': suggestion['expected_versions'], 'suggestion_id': suggestion['id'],
            'suggestion_token': suggestion['snapshot_token'],
            'fields': {'project_type': '其他', 'project_name': '合成住宿测试', 'amount': '1353.00'}}


def test_formal_get_and_auto_return_same_readonly_candidates_with_ai_on_or_off(http_matcher: Any) -> None:
    matcher, project, get, post, _ = http_matcher
    before = snapshots(matcher)
    board = get('/api/material-matching?project=' + project)
    candidates = get('/api/material-matching/suggestions?project=' + project)
    first = post('/api/material-matching/auto', {'project_id': project})
    second = post('/api/material-matching/auto', {'project_id': project})
    assert board['suggestions'] == candidates['suggestions'] == first['suggestions'] == second['suggestions']
    assert first['suggested'] == 1 and first['matched'] == 0 and first['items'] == [] and first['requests'] == 0
    assert first['suggestions'][0]['needs_name_review']
    assert snapshots(matcher) == before and not matcher.store.list_items()
    assert '"path"' not in json.dumps(board) and '"path"' not in json.dumps(first)


def test_formal_all_project_board_includes_project_specific_suggestions_without_cross_pairing(http_matcher: Any) -> None:
    matcher, project, get, _, _ = http_matcher
    second = ProjectRegistry(matcher.store.workspace / '后勤' / 'tasks.sqlite3').create_project('Second candidate project')['id']
    voucher = queued(matcher, second, 'Second invoice', 'invoice', '1353')
    paid = payment(matcher, second, 'Second whole payment', '1353')
    payment(matcher, '', 'Unassigned whole payment', '1353')
    before = snapshots(matcher)
    board = get('/api/material-matching')
    assert board['suggestion_count'] == 2 and not board['suggestions_truncated']
    assert {candidate['project_id'] for candidate in board['suggestions']} == {project, second}
    candidate = next(candidate for candidate in board['suggestions'] if candidate['project_id'] == second)
    assert candidate['voucher_keys'] == ['q:' + voucher['id']] and candidate['payment_ids'] == [paid['id']]
    assert not any(candidate['ambiguous'] for candidate in board['suggestions'])
    assert snapshots(matcher) == before


def test_formal_candidate_confirmation_enters_review_only_after_manual_post(http_matcher: Any) -> None:
    matcher, project, get, post, _ = http_matcher
    suggestion = get('/api/material-matching/suggestions?project=' + project)['suggestions'][0]
    result = post('/api/material-matching/create', confirmation(suggestion))
    assert result['item']['pending_review'] and not result['item']['verified'] and not result['item']['ready']
    assert result['item']['amount_cents'] == 135300 and len(result['item']['attachments']) == 5
    assert get('/api/material-matching/suggestions?project=' + project)['suggested'] == 0
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as stale:
        post('/api/material-matching/create', confirmation(suggestion))
    assert stale.value.code == 400 and snapshots(matcher) == before


def test_formal_stale_pool_token_and_modified_selection_are_rejected_without_writes(http_matcher: Any) -> None:
    matcher, project, get, post, _ = http_matcher
    suggestion = get('/api/material-matching/suggestions?project=' + project)['suggestions'][0]
    modified = confirmation(suggestion)
    modified['payment_ids'] = modified['payment_ids'][:-1]
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as wrong_selection:
        post('/api/material-matching/create', modified)
    assert wrong_selection.value.code == 400 and snapshots(matcher) == before
    payment(matcher, project, 'Late whole payment', '1353')
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as changed:
        post('/api/material-matching/create', confirmation(suggestion))
    assert changed.value.code == 400 and snapshots(matcher) == before
    current = get('/api/material-matching/suggestions?project=' + project)
    assert current['suggested'] == 2 and all(candidate['ambiguous'] for candidate in current['suggestions'])


def test_mail_post_reports_candidates_without_creating_expenses(http_matcher: Any, monkeypatch: Any) -> None:
    matcher, project, _, post, enabled = http_matcher
    if not enabled:
        return
    monkeypatch.setattr('invoice_print_layout.workbench_web.sync_mail',
                        lambda store, organize=True: {'message': 'Synthetic mail refresh.', 'downloaded': 0})
    before = snapshots(matcher)
    result = post('/api/mail', {'project_id': project})
    assert result['matching']['matched'] == 0 and result['matching']['suggested'] == 1
    assert '待合成候选' in result['message'] and snapshots(matcher) == before


def test_invalid_project_does_not_scan_other_projects(http_matcher: Any) -> None:
    matcher, _, get, post, _ = http_matcher
    before = snapshots(matcher)
    with pytest.raises(HTTPError) as missing:
        get('/api/material-matching/suggestions')
    assert missing.value.code == 404
    with pytest.raises(HTTPError) as invalid:
        post('/api/material-matching/auto', {'project_id': 'nonexistent'})
    assert invalid.value.code == 400 and snapshots(matcher) == before
