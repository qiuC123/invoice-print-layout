"""Independent invoice mode uses the same ledger without starting logistics work."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from typing import Any, Iterator
from urllib.parse import quote

import pymupdf
import pytest

from invoice_print_layout import workbench_web as web
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_runtime import identity, source_build_id
from tests.helpers import make_invoice_pdf, make_trip_pdf


class Client:
    def __init__(self, port: int):
        self.connection = HTTPConnection('127.0.0.1', port, timeout=10)
        self.token = ''

    def request(self, method: str, path: str, value: dict[str, Any] | None = None,
                *, authenticated: bool = True) -> tuple[int, bytes]:
        headers = {'Content-Type': 'application/json'}
        if authenticated and self.token:
            headers['X-Workbench-Token'] = self.token
        self.connection.request(method, path, json.dumps(value) if value is not None else None, headers)
        response = self.connection.getresponse()
        return response.status, response.read()

    def json(self, method: str, path: str, value: dict[str, Any] | None = None,
             *, authenticated: bool = True) -> tuple[int, Any]:
        status, payload = self.request(method, path, value, authenticated=authenticated)
        return status, json.loads(payload)


@contextmanager
def invoice_server(workspace: Path) -> Iterator[Client]:
    server = web.make_server(workspace, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server.server_port)
    try:
        status, state = client.json('GET', '/api/state')
        assert status == 200
        client.token = state['token']
        yield client
    finally:
        client.connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_cold_invoice_process_does_not_load_or_initialize_logistics(tmp_path: Path) -> None:
    # A fresh process catches eager imports that monkeypatching already-loaded modules misses.
    probe = '''
import json, sys, threading
from pathlib import Path
from urllib.request import urlopen
from invoice_print_layout.workbench_web import make_server
server = make_server(Path(sys.argv[1]), 0, mode='invoice')
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    server.service_actions()
    with urlopen(f'http://127.0.0.1:{server.server_port}/api/state') as response:
        assert json.load(response)['app_mode'] == 'invoice'
    print(json.dumps(sorted(sys.modules)))
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
'''
    result = subprocess.run([sys.executable, '-c', probe, str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    for module in ('logistics', 'project_todos', 'site_photos', 'daily_reports',
                   'photo_schedule', 'wechat_inbox', 'photo_wechat_bridge'):
        assert f'invoice_print_layout.{module}' not in loaded
    assert tables(tmp_path / '后勤' / 'tasks.sqlite3') == {'projects'}
    assert not (tmp_path / '现场进度').exists()
    assert not (tmp_path / '照片管理').exists()


def test_invoice_startup_and_read_do_not_migrate_or_reorganize_existing_expenses(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout.logistics import LogisticsStore
    from invoice_print_layout.expense_projects import ExpenseProjects

    registry = LogisticsStore(tmp_path / '后勤' / 'tasks.sqlite3')
    project = registry.create_project('Synthetic legacy project')
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Existing complete expense', 'category': '酒店',
                         'project_id': project['id'], 'amount': '123.45'})
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf').read_bytes()
    store.add_attachment(item['id'], 'invoice.pdf', invoice, 'invoice')
    store.transition(item['id'], 'verify')
    # Legacy name-only ownership is a temptation for ExpenseProjects.migrate().
    with store.connect() as db:
        row = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()
        raw = json.loads(row['data'])
        raw['project_id'] = ''
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), item['id']))
    before = store.list_items()
    with store.connect() as db:
        before_business = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                           for table in ('expenses', 'attachments', 'events')}
    before_projects = registry.snapshot()['projects']
    before_tables = tables(registry.path)
    inbox = store.root / '邮件收件'
    inbox.mkdir(exist_ok=True)
    received = inbox / 'unorganized-invoice.pdf'
    received.write_bytes(invoice)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail('Read-only invoice startup invoked an expense migration/import/organizer')

    monkeypatch.setattr(web, 'organize_rides', forbidden)
    monkeypatch.setattr(ExpenseProjects, 'migrate', forbidden)
    monkeypatch.setattr(ExpenseStore, 'import_history', forbidden)
    with invoice_server(tmp_path) as client:
        for _ in range(2):
            status, state = client.json('GET', '/api/state')
            assert status == 200 and state['items'] == before
            assert state['projects'] == [{**project, 'reimbursement_status': 'unreimbursed',
                                          'reimbursement_revision': 0, 'pinned': False,
                                          'project_revision': 0, 'start_date': '', 'end_date': ''}
                                         for project in before_projects]
            assert state['app_mode'] == 'invoice' and state['logistics_available'] is False
        assert client.json('GET', '/api/intake')[0] == 200
    assert store.list_items() == before
    with store.connect() as db:
        assert {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('expenses', 'attachments', 'events')} == before_business
    assert tables(registry.path) == before_tables
    assert received.read_bytes() == invoice


def test_invoice_project_creation_is_shared_and_does_not_create_logistics_tables(tmp_path: Path) -> None:
    with invoice_server(tmp_path) as client:
        assert client.json('POST', '/api/projects', {'name': 'Synthetic project'}, authenticated=False)[0] == 403
        status, project = client.json('POST', '/api/projects', {
            'name': 'Synthetic project', 'site_name': 'Synthetic site'})
        assert status == 200 and project['status'] == 'draft' and len(project['id']) == 32
        assert project['name'] == 'Synthetic project' and project['site_name'] == 'Synthetic site'
        # The invoice facade adds view-only menu metadata to its response; the
        # shared directory retains exactly its original project payload shape.
        invoice_fields = {'pinned', 'project_revision', 'reimbursement_status', 'reimbursement_revision',
                          'start_date', 'end_date'}
        listed_project = {key: value for key, value in project.items() if key not in invoice_fields}
        assert project == {**listed_project, 'pinned': False, 'project_revision': 0,
                           'reimbursement_status': 'unreimbursed', 'reimbursement_revision': 0,
                           'start_date': '', 'end_date': ''}
        assert client.json('GET', '/api/state')[1]['projects'] == [
            {**listed_project, 'reimbursement_status': 'unreimbursed', 'reimbursement_revision': 0,
             'pinned': False, 'project_revision': 0, 'start_date': '', 'end_date': ''}]
        assert client.json('POST', '/api/projects', {'name': 'Synthetic project'})[0] == 400
        assert client.json('POST', '/api/projects', {'name': 'Other', 'site_name': ['invalid']})[0] == 400
        status, expense = client.json('POST', '/api/create', {
            'title': 'New expense', 'category': '餐饮', 'project_id': project['id'],
            'amount': '15.00', 'material_basis': 'invoice_payment'})
        assert status == 200 and expense['project_id'] == project['id']
        assert expense['project'] == project['name']
        assert client.json('POST', '/api/create', {
            'title': 'Bad ownership', 'category': '餐饮', 'project_id': 'unknown'})[0] == 400
    assert tables(tmp_path / '后勤' / 'tasks.sqlite3') == {'projects'}
    # Opening the original workstation registry sees the exact same project identity.
    from invoice_print_layout.logistics import LogisticsStore
    full = LogisticsStore(tmp_path / '后勤' / 'tasks.sqlite3')
    assert full.snapshot()['projects'] == [listed_project]
    assert ExpenseStore(tmp_path).get(expense['id'])['project_id'] == project['id']


def test_invoice_upload_review_and_material_checks_preserve_originals(tmp_path: Path) -> None:
    original = make_invoice_pdf(tmp_path / 'invoice.pdf').read_bytes()
    payment = make_trip_pdf(tmp_path / 'payment.pdf').read_bytes()
    with invoice_server(tmp_path) as client:
        project = client.json('POST', '/api/projects', {'name': 'Review project'})[1]
        status, item = client.json('POST', '/api/create', {
            'title': 'Meal with payment', 'category': '餐饮', 'amount': '123.45',
            'project_id': project['id'], 'material_basis': 'invoice_payment'})
        assert status == 200 and item['missing'] == ['发票或收据', '付款截图']
        status, item = client.json('POST', '/api/upload', {
            'id': item['id'], 'name': 'invoice.pdf', 'role': 'invoice',
            'data': base64.b64encode(original).decode()})
        assert status == 200 and item['missing'] == ['付款截图']
        assert client.request('GET', '/file/' + item['attachments'][0]['id']) == (200, original)
        before = item
        status, queued = client.json('POST', '/api/review/request', {
            'ids': [item['id']], 'versions': {item['id']: item['version']}})
        assert status == 200 and queued['changed'] == 1
        current = client.json('GET', '/api/state')[1]['items'][0]
        assert current['pending_review'] and current['review_requested']
        assert current['missing'] == before['missing'] and current['attachments'] == before['attachments']
        assert not current['complete'] and not current['verified'] and not current['ready']
        for action in ('verify', 'submitted'):
            assert client.json('POST', '/api/transition', {'id': item['id'], 'action': action})[0] == 400
        assert client.json('POST', '/api/report-preview', {'ids': [item['id']]})[0] == 400
        status, supplemented = client.json('POST', '/api/upload', {
            'id': item['id'], 'name': 'payment.pdf', 'role': 'payment',
            'data': base64.b64encode(payment).decode()})
        assert status == 200 and supplemented['complete'] and supplemented['pending_review']
        assert not supplemented['review_requested'] and not supplemented['verified']
        status, verified = client.json('POST', '/api/transition', {
            'id': item['id'], 'action': 'verify', 'expected_version': supplemented['version']})
        assert status == 200 and verified['ready'] and not verified['pending_review']
    reopened = ExpenseStore(tmp_path).get(item['id'])
    assert reopened['ready'] and reopened['stage'] == 'draft'
    store = ExpenseStore(tmp_path)
    expected_payloads = {'invoice': original, 'payment': payment}
    assert {attachment['role'] for attachment in reopened['attachments']} == set(expected_payloads)
    for attachment in reopened['attachments']:
        assert store.attachment_path(attachment['id'])[0].read_bytes() == expected_payloads[attachment['role']]
    assert tables(tmp_path / '后勤' / 'tasks.sqlite3') == {'projects'}


def test_invoice_report_preview_produces_downloadable_pdf_without_submitting(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import invoice_print_layout.reimbursement as report
    import invoice_print_layout.report_print_preview as print_preview
    from invoice_print_layout.report_excel import TEMPLATE_NAME

    # Exercise the real PDF layout/export. The private Excel template/runtime is out of scope.
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))
    def synthetic_excel(_template: Path, items: list[dict[str, Any]], _options: dict[str, str], output: Path, *,
                        summary_pdf: Path | None = None) -> None:
        output.write_bytes(b'synthetic spreadsheet')
        if summary_pdf is not None:
            with pymupdf.open() as document:
                page = document.new_page(width=842, height=595)
                page.insert_text((30, 30), 'SYNTHETIC COMPANY TABLE')
                page.insert_text((30, 60), f"Total {sum(item['amount_cents'] for item in items) / 100:.2f}")
                document.save(summary_pdf)
    monkeypatch.setattr(report, 'create_excel', synthetic_excel)
    monkeypatch.setattr(print_preview, 'create_excel', synthetic_excel)
    with invoice_server(tmp_path) as client:
        store = ExpenseStore(tmp_path)
        (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
        item = store.create({'title': 'Synthetic lodging', 'category': '酒店', 'amount': '123.45'})
        store.add_attachment(item['id'], 'invoice.pdf',
                             make_invoice_pdf(tmp_path / 'invoice.pdf').read_bytes(), 'invoice')
        store.transition(item['id'], 'verify')
        before = store.list_items()
        status, result = client.json('POST', '/api/report-preview', {
            'ids': [item['id']], 'options': {'person': 'Synthetic', 'payment': 'personal'}})
        assert status == 200 and len(result['preview_pages']) == result['page_count'] == 2
        assert result['table_page_count'] == 1 and result['combined_pdf'] == result['pdf']
        assert result['combined_preview_pages'] == result['preview_pages']
        status, pdf_bytes = client.request('GET', '/export/' + quote(result['pdf']))
        assert status == 200 and pdf_bytes.startswith(b'%PDF-')
        with pymupdf.open(stream=pdf_bytes, filetype='pdf') as document:
            assert len(document) == 2 and '123.45' in document[0].get_text()
            assert '123.45' in document[1].get_text()
        status, png_bytes = client.request('GET', '/export/' + quote(result['preview_pages'][0]))
        assert status == 200 and png_bytes.startswith(b'\x89PNG\r\n\x1a\n')
        assert store.list_items() == before
        assert before[0]['stage'] == 'draft'


def test_invoice_mode_rejects_logistics_routes_and_assets(tmp_path: Path) -> None:
    unavailable = ('/logistics', '/photos', '/api/logistics', '/api/logistics/inbox',
                   '/api/logistics/todos', '/api/photos', '/api/photos/settings',
                   '/api/daily-reports', '/daily-report/p/key/file.pdf',
                   '/photo-file/p/key', '/site-photos/p/index.html',
                   '/logistics.js', '/logistics.css', '/photos.js', '/photos.css',
                   '/daily-reports.js', '/%61pi/logistics')
    with invoice_server(tmp_path) as client:
        for route in unavailable:
            assert client.json('GET', route)[0] == 404, route
            assert client.json('POST', route, {})[0] == 404, route
        for route in ('/', '/app.js', '/style.css', '/intake.js', '/receipt.js', '/report.js'):
            assert client.request('GET', route)[0] == 200, route
        status, page = client.request('GET', '/')
        assert status == 200 and '发票助手' in page.decode()
    assert tables(tmp_path / '后勤' / 'tasks.sqlite3') == {'projects'}


def test_runtime_mode_identity_prevents_reusing_the_wrong_application(tmp_path: Path) -> None:
    full = identity(tmp_path, 'same-build')
    invoice = identity(tmp_path, 'same-build', mode='invoice')
    assert full['app_mode'] == 'workbench' and invoice['app_mode'] == 'invoice'
    assert full['workspace_id'] == invoice['workspace_id'] and full['build_id'] == invoice['build_id']
    assert full['app_id'] != invoice['app_id'] == 'invoice-print-layout.invoice-assistant'
    assert {'expense_projects', 'expense_review', 'unified_intake', 'report_snapshot'}.issubset(invoice['capabilities'])
    assert {'logistics_projects', 'photo_management'}.issubset(full['capabilities'])
    assert not {'logistics_projects', 'photo_management'}.intersection(invoice['capabilities'])
    assert str(tmp_path) not in json.dumps(invoice)
    with invoice_server(tmp_path) as client:
        status, runtime = client.json('GET', '/api/runtime')
        assert status == 200 and runtime['ready'] and runtime['started_at']
        assert runtime['app_id'] == invoice['app_id'] and runtime['app_mode'] == 'invoice'
        assert runtime['capabilities'] == invoice['capabilities'] and 'token' not in runtime


def test_invalid_application_mode_is_rejected_before_workspace_mutation(tmp_path: Path) -> None:
    workspace = tmp_path / 'unused'
    with pytest.raises(ValueError):
        web.make_server(workspace, 0, mode='unknown')
    assert not workspace.exists()


@pytest.mark.skipif(os.name != 'nt', reason='The local launcher owns a Windows workspace byte lock')
@pytest.mark.parametrize('mode', ['invoice', 'workbench'])
def test_cli_only_imports_history_in_full_workbench_mode(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    imports: list[Path] = []
    calls: list[tuple[Path, int, str]] = []
    lifecycle: list[str] = []

    class SyntheticServer:
        def serve_forever(self) -> None:
            lifecycle.append('serve')

        def server_close(self) -> None:
            lifecycle.append('close')

    def make_server(workspace: Path, port: int, *, mode: str) -> SyntheticServer:
        calls.append((workspace, port, mode))
        return SyntheticServer()

    monkeypatch.setattr(web, 'make_server', make_server)
    monkeypatch.setattr(ExpenseStore, 'import_history', lambda self: imports.append(self.workspace))
    monkeypatch.setattr(sys, 'argv', ['invoice-assistant', '--workspace', str(tmp_path),
                                    '--port', '18776', '--mode', mode])
    web.main()
    assert calls == [(tmp_path, 18776, mode)] and lifecycle == ['serve', 'close']
    assert imports == ([tmp_path.resolve()] if mode == 'workbench' else [])
    assert (tmp_path / '工作台' / 'expenses.sqlite3').exists() == (mode == 'workbench')


@pytest.mark.parametrize('case,target_mode', [
    ('switch_to_invoice', 'invoice'), ('switch_to_workbench', 'workbench'),
    ('cold', 'invoice'), ('foreign', 'invoice'), ('workspace', 'invoice'),
])
def test_launcher_mode_switch_and_identity_guards_without_touching_a_live_service(
        tmp_path: Path, case: str, target_mode: str) -> None:
    powershell = shutil.which('powershell.exe')
    if not powershell:
        pytest.skip('Windows PowerShell unavailable')
    root = Path(__file__).resolve().parents[1]
    ready = {'ready': True, 'started_at': '2026-01-01T00:00:00Z'}
    target = {**identity(tmp_path, source_build_id(), mode=target_mode), **ready}
    initial_mode = 'invoice' if case in {'switch_to_workbench', 'workspace'} else 'workbench'
    initial = {**identity(tmp_path, source_build_id(), mode=initial_mode), **ready}
    if case == 'foreign':
        initial['app_id'] = 'unrelated-app'
    elif case == 'workspace':
        initial['workspace_id'] = 'different-workspace'
    initial_path, target_path = tmp_path / 'initial.json', tmp_path / 'target.json'
    initial_path.write_text(json.dumps(initial), encoding='utf-8')
    target_path.write_text(json.dumps(target), encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    harness = r'''$ErrorActionPreference = 'Stop'
$global:phase = 'initial'
$global:launched = $false
$global:shutdowns = 0
$global:launches = [Collections.Generic.List[object]]::new()
function Invoke-WebRequest {
    param($Uri, [switch]$UseBasicParsing, $TimeoutSec, $Method, $Headers, $ContentType, $Body)
    if (-not $UseBasicParsing.IsPresent) { throw 'Basic parsing required' }
    if ($Uri -eq 'http://127.0.0.1:@@PORT@@/api/state') {
        return [PSCustomObject]@{Content='{"token":"synthetic-token"}'}
    }
    if ($Uri -eq 'http://127.0.0.1:@@PORT@@/api/shutdown') {
        if ($Method -ne 'Post' -or $Headers['X-Workbench-Token'] -ne 'synthetic-token' -or $Body -ne '{}') { throw 'Invalid shutdown request' }
        $global:shutdowns++
        $global:phase = 'closed'
        return [PSCustomObject]@{Content='{"ok":true}'}
    }
    if ($Uri -ne 'http://127.0.0.1:@@PORT@@/api/runtime') { throw 'Wrong endpoint' }
    if ($global:launched) { return [PSCustomObject]@{Content=(Get-Content -Raw -LiteralPath '@@TARGET@@')} }
    if ($global:phase -eq 'closed' -or '@@CASE@@' -eq 'cold') { throw 'Offline' }
    return [PSCustomObject]@{Content=(Get-Content -Raw -LiteralPath '@@INITIAL@@')}
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle)
    $global:launches.Add([PSCustomObject]@{FilePath=$FilePath;WindowStyle=$WindowStyle;Arguments=$ArgumentList})
    $global:launched = $true
}
function Start-Sleep { param($Seconds) }
$failure = $null
try { & '@@LAUNCHER@@' -Workspace '@@WORKSPACE@@' -Port @@PORT@@ -Mode '@@MODE@@' -NoBrowser } catch { $failure = $_.Exception.Message }
[PSCustomObject]@{Shutdowns=$global:shutdowns;Launches=@($global:launches.ToArray());Failure=$failure} | ConvertTo-Json -Depth 6 -Compress
'''
    replacements = {'PORT': str(port), 'TARGET': str(target_path), 'INITIAL': str(initial_path),
                    'CASE': case, 'LAUNCHER': str(root / 'scripts/start-workbench.ps1'),
                    'WORKSPACE': str(tmp_path), 'MODE': target_mode}
    for key, value in replacements.items():
        harness = harness.replace(f'@@{key}@@', value.replace("'", "''"))
    script = tmp_path / 'launcher-harness.ps1'
    script.write_text(harness, encoding='utf-8-sig')
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(script)],
                            capture_output=True, text=True, timeout=40, check=True, cwd=root)
    data = json.loads(result.stdout)
    if case in {'foreign', 'workspace'}:
        assert data['Failure'] and data['Shutdowns'] == 0 and data['Launches'] == []
    else:
        assert data['Failure'] is None
        assert data['Shutdowns'] == (0 if case == 'cold' else 1)
        assert len(data['Launches']) == 1
        launch = data['Launches'][0]
        assert launch['FilePath'].endswith('pythonw.exe') and launch['WindowStyle'] == 'Hidden'
        arguments = launch['Arguments']
        assert arguments[arguments.index('--mode') + 1] == target_mode
        assert arguments[arguments.index('--port') + 1] == str(port)
    assert not (tmp_path / '工作台').exists()
