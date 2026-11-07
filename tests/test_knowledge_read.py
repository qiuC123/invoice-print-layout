"""Consolidation reads synthetic workspaces through the real HTTP and CLI path."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

from invoice_print_layout import knowledge_read as read, workbench_web as web
from invoice_print_layout.expense_preferences import Preferences
from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.invoice_attention import Attention
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_runtime import identity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent-harness'))
from click.testing import CliRunner
from cli_anything.invoice_assistant import invoice_assistant_cli as cli
from cli_anything.invoice_assistant.utils.client import Client, CLIError


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'config'))
    monkeypatch.delenv('INVOICE_ASSISTANT_URL', raising=False)
    monkeypatch.delenv('INVOICE_ASSISTANT_WORKSPACE_ID', raising=False)
    monkeypatch.setattr(web, 'GlmInvoiceAI', lambda _: LocalOnlyInvoiceAI())
    server = web.make_server(tmp_path / 'workspace', 0, mode='invoice')
    monkeypatch.setattr(server, 'service_actions', lambda: None)
    store = ExpenseStore(tmp_path / 'workspace')
    with store.connect() as db:
        for index, status in enumerate(('open', 'resolved', 'excluded', 'superseded'), 1):
            obj = f'invoice-item:synthetic-{index}'
            project = '' if index == 1 else 'synthetic-project'
            record = {'object': obj, 'project_id': project, 'status': status, 'reason': 'Synthetic question'}
            db.execute('INSERT INTO invoice_attention VALUES (?,?,?,?)', (obj, project, status, json.dumps(record)))
        for ident in (2, 5, 9):
            data = {'before': {'project_id': 'synthetic-project'}, 'after': {}, 'decided_by': 'user',
                    'evidence': {'method': 'Synthetic check', 'statement': 'Synthetic evidence'}, 'extra': ['retained']}
            db.execute('INSERT INTO invoice_attention_audit VALUES (?,?,?,?)',
                       (ident, 'invoice-item:synthetic-1', f'2000-01-{ident:02d}', json.dumps(data)))
            db.execute('INSERT INTO recognition_corrections VALUES (?,?,?,?,?,?,?)',
                       (ident, 'synthetic-material', 'title', 'null', json.dumps('Synthetic correction'), 'manual',
                        f'2000-01-{ident:02d}'))
        for ident, deleted, project in (('synthetic-a', False, ''), ('synthetic-b', True, 'synthetic-project')):
            expense = {'id': ident, 'title': 'Synthetic expense', 'deleted': deleted, 'project_id': project,
                       'category': '其他', 'verified': False}
            db.execute('INSERT INTO expenses VALUES (?,?,?)', (ident, None, json.dumps(expense)))
        for ident in ('synthetic-a', 'synthetic-b', 'synthetic-orphan'):
            db.execute('INSERT INTO category_attempts VALUES (?,?,?,?)',
                       (ident, 'synthetic-input', 'synthetic-evidence', json.dumps({'status': 'manual', 'source': 'human'})))
        db.execute('INSERT INTO category_experience VALUES (?,?,?,?)', ('synthetic-evidence', '其他', 'human', 0))
        db.execute('INSERT INTO category_experience_disabled VALUES (?)', ('synthetic-evidence',))
        db.execute('INSERT INTO expense_preferences VALUES (?,?)', ('synthetic-rule', json.dumps(
            {'id': 'synthetic-rule', 'enabled': False, 'last_used': '2000-01-01', 'reason': 'Synthetic rule'})))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client({'url': f'http://127.0.0.1:{server.server_port}',
                     'workspace_id': identity(store.workspace, '', 'invoice')['workspace_id']})
    try:
        yield store, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def invoke(client, *args):
    result = CliRunner().invoke(cli.cli, ['--json', '--url', client.base, '--workspace-id', client.workspace_id, *args])
    assert result.exit_code == 0, result.output + repr(result.exception)
    return json.loads(result.output)


@pytest.mark.parametrize('command', ['audit', 'corrections'])
def test_id_cursor_pages_include_gaps_and_empty_tail(source, command):
    _, client = source
    first = invoke(client, command, 'list', '--after-id', '2', '--limit', '1')
    assert first['total'] == 2 and first['has_more'] and first['next_after_id'] == 5
    row = first['items'][0]
    if command == 'audit':
        assert row['id'] == 5 and row['at'] == '2000-01-05'
        assert row['project_id'] == 'synthetic-project' and row['decided_by'] == 'user'
        assert row['data']['extra'] == ['retained'] and row['evidence']['method'] == 'Synthetic check'
    else:
        assert row['material_id'] == 'synthetic-material' and row['field'] == 'title'
        assert row['old_value'] is None and row['new_value'] == 'Synthetic correction' and row['source'] == 'manual'
    second = invoke(client, command, 'list', '--after-id', '5', '--limit', '1')
    assert second['next_after_id'] == 9 and not second['has_more']
    tail = invoke(client, command, 'list', '--after-id', '9')
    assert tail['items'] == [] and tail['next_after_id'] == 9 and not tail['has_more']


def test_classification_snapshot_includes_unassigned_deleted_and_missing_expenses(source):
    store, client = source
    first = invoke(client, 'classification', 'list', '--limit', '1')
    assert first['total'] == 3 and first['has_more']
    assert first['items'][0]['expense']['project_id'] == ''
    rest = invoke(client, 'classification', 'list', '--offset', '1', '--fingerprint', first['fingerprint'])
    assert rest['items'][0]['expense']['deleted'] is True and rest['items'][1]['expense'] is None
    assert set(first['items'][0]) == {'item_id', 'input_key', 'evidence_key', 'result', 'expense'}
    assert first['fingerprint'] == rest['fingerprint']
    assert read.fingerprint({**first['fingerprints'], **rest['fingerprints']}) == first['fingerprint']
    with store.connect() as db:
        db.execute("UPDATE expenses SET data=json_set(data,'$.verified',1) WHERE id='synthetic-a'")
    changed = invoke(client, 'classification', 'list')
    assert changed['fingerprints']['synthetic-a'] != first['fingerprints']['synthetic-a']
    assert changed['fingerprint'] != first['fingerprint']
    with pytest.raises(CLIError):
        client.get('/api/knowledge/classification?offset=1&fingerprint=' + first['fingerprint'])
    with store.connect() as db:
        db.execute("UPDATE category_attempts SET input_key='synthetic-new-input' WHERE item_id='synthetic-a'")
        db.execute("DELETE FROM category_attempts WHERE item_id='synthetic-orphan'")
    deleted = invoke(client, 'classification', 'list')
    assert set(changed['fingerprints']) - set(deleted['fingerprints']) == {'synthetic-orphan'}
    assert deleted['fingerprints']['synthetic-a'] != changed['fingerprints']['synthetic-a']


def test_attention_all_uses_stored_status_and_stable_keys(source):
    _, client = source
    result = invoke(client, 'attention', 'list', '--all')
    assert {row['status'] for row in result['items']} == {'open', 'resolved', 'excluded', 'superseded'}
    assert result['items'][0]['project_id'] == ''
    assert set(result['fingerprints']) == {row['object'] for row in result['items']}
    page = client.get('/api/attention?all=1&project=synthetic-project&limit=1')
    assert page['total'] == 3 and page['has_more']
    assert client.get('/api/attention')['items'] == []  # Default still only shows actionable open objects.


@pytest.mark.parametrize('command,key', [('experience', 'synthetic-evidence'), ('preferences', 'synthetic-rule')])
def test_preferences_snapshot_is_stable_and_detects_overwrite(source, command, key):
    store, client = source
    initial = invoke(client, command, 'list')
    assert initial['items'][0]['enabled'] == 0
    assert initial['fingerprint'] == invoke(client, command, 'list')['fingerprint']
    assert set(initial['fingerprints']) == {key}
    with store.connect() as db:
        if command == 'experience':
            db.execute('DELETE FROM category_experience_disabled')
        else:
            db.execute("UPDATE expense_preferences SET data=json_set(data,'$.enabled',1)")
    changed = invoke(client, command, 'list')
    assert changed['fingerprint'] != initial['fingerprint'] and changed['items'][0]['enabled'] == 1


def test_every_new_cli_read_preserves_database_and_uses_no_mutating_store(source, monkeypatch):
    store, client = source
    before = hashlib.sha256(store.database.read_bytes()).hexdigest()
    with closing(sqlite3.connect(store.database)) as db:
        dump = list(db.iterdump())
    def forbidden(*args, **kwargs):
        raise AssertionError('Read-only command used a writable store or POST')
    with monkeypatch.context() as guard:
        guard.setattr(ExpenseStore, 'connect', forbidden)
        guard.setattr(Preferences, 'apply', forbidden)
        guard.setattr(Preferences, 'list', forbidden)
        guard.setattr(Attention, '__init__', forbidden)
        guard.setattr(Client, 'post', forbidden)
        for command in ('audit', 'corrections', 'classification', 'experience', 'preferences', 'attention'):
            result = invoke(client, command, 'list', *(['--all'] if command == 'attention' else []))
            assert all(count == 0 for count in result['operations'].values())
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(store.database)) as db:
        assert list(db.iterdump()) == dump


def test_read_connection_enforces_read_only_and_does_not_create_missing_database(tmp_path, source):
    store, _ = source
    with read.connect(store.database) as db, pytest.raises(sqlite3.OperationalError, match='readonly'):
        db.execute('DELETE FROM category_attempts')
    absent = tmp_path / 'absent.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        with read.connect(absent):
            pass
    assert not absent.exists()


@pytest.mark.parametrize('path', ['/api/knowledge/audit?after_id=-1', '/api/knowledge/corrections?limit=0',
                                '/api/knowledge/classification?offset=-1', '/api/attention?all=1&limit=1001'])
def test_http_rejects_invalid_paging(source, path):
    with pytest.raises(CLIError) as error:
        source[1].get(path)
    assert error.value.code == 'HTTP_404'


@pytest.mark.parametrize('command', ['audit', 'corrections', 'classification', 'experience', 'preferences', 'attention'])
def test_cli_runtime_binding_blocks_every_new_command(tmp_path, monkeypatch, command):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    calls = []
    def wrong_runtime(self, path, *args, **kwargs):
        calls.append(path)
        return {'app_id': 'invoice-print-layout.invoice-assistant', 'ready': True, 'workspace_id': 'b' * 64}
    monkeypatch.setattr(Client, 'request', wrong_runtime)
    result = CliRunner().invoke(cli.cli, ['--json', '--workspace-id', 'a' * 64, command, 'list',
                                        *(['--all'] if command == 'attention' else [])])
    assert isinstance(result.exception, CLIError) and result.exception.code == 'WORKSPACE_MISMATCH'
    assert calls == ['/api/runtime']


@pytest.mark.parametrize('command', ['audit', 'corrections', 'classification', 'experience', 'preferences'])
def test_workspace_exports_reject_project_filter(source, command):
    _, client = source
    result = CliRunner().invoke(cli.cli, ['--json', '--url', client.base, '--workspace-id', client.workspace_id,
                                        '--project', 'synthetic-project', command, 'list'])
    assert isinstance(result.exception, CLIError) and '省略--project' in str(result.exception)
