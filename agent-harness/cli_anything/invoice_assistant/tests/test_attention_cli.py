import json

from click.testing import CliRunner
import pytest

from cli_anything.invoice_assistant import invoice_assistant_cli as command
from cli_anything.invoice_assistant.core import attention, workflow
from cli_anything.invoice_assistant.utils.client import CLIError


class AttentionClient:
    def __init__(self):
        self.operations = {}
        self.posts = []
        self.gets = []

    def project(self, project_id):
        assert project_id == 'p'

    def get(self, path):
        self.gets.append(path)
        if '/context?' in path:
            return {'status': 'open', 'version': 'v1', 'record': {'project_id': 'p'},
                    'fields': {'agent_review_reason': '合成疑点', 'agent_content': {'status': 'needs_input'},
                               'field_conflicts': [{'field': 'amount'}]}}
        return {'status': 'complete', 'items': [], 'total': 0, 'has_more': False}

    def post(self, path, body):
        self.posts.append((path, body))
        return {'status': 'resolved', 'job_id': 'new-job', 'verified': False}


REQUEST = {'action': 'set_fields', 'values': {'amount': '17.00'}, 'decided_by': 'codex',
           'evidence': {'method': '原件逐字段核实', 'reference': 'synthetic.pdf', 'statement': '合成原件明确17元'}}


def test_attention_paging_and_context_keep_project_boundary():
    client = AttentionClient()
    attention.list_attention(client, 'p', 3, 7)
    assert client.gets == ['/api/attention?project=p&offset=3&limit=7']
    assert attention.context(client, 'p', 'invoice-material:m')['fields']['field_conflicts']
    with pytest.raises(CLIError, match='不属于'):
        attention.context(client, 'other', 'invoice-material:m')


@pytest.mark.parametrize('dry', [False, True])
def test_attention_resolution_preserves_guard_and_provenance(dry):
    client = AttentionClient()
    result = attention.resolve(client, 'p', 'invoice-material:m', 'v1', REQUEST, dry)
    if dry:
        assert result['dry_run'] and not client.posts
    else:
        assert result['job_id'] == 'new-job'
        assert client.posts == [('/api/attention/resolve', {**REQUEST, 'object': 'invoice-material:m', 'expected_version': 'v1'})]


@pytest.mark.parametrize('change', ['version', 'override'])
def test_stale_or_overridden_version_is_rejected(change):
    client = AttentionClient()
    with pytest.raises(CLIError):
        attention.resolve(client, 'p', 'invoice-material:m', 'stale' if change == 'version' else 'v1',
                          {**REQUEST, **({'expected_version': 'v2'} if change == 'override' else {})})
    assert not client.posts


@pytest.mark.parametrize('name', ['material', 'expense'])
def test_existing_cli_projection_keeps_attention_context(name):
    row = {'id': 'm', 'agent_review_reason': '合成疑点', 'agent_content': {'status': 'needs_input'},
           'field_conflicts': [{'field': 'amount'}]}
    result = workflow.material_view(row) if name == 'material' else workflow.expense_view(row, {})
    assert all(result[key] == row[key] for key in ('agent_review_reason', 'agent_content', 'field_conflicts'))


@pytest.mark.parametrize('operation', ['list', 'show', 'resolve'])
def test_click_command_dispatch(tmp_path, monkeypatch, operation):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    client = AttentionClient()
    monkeypatch.setattr(command, 'Client', lambda _: client)
    args = ['--json', '--url', 'http://127.0.0.1:19876', '--workspace-id', 'a' * 64, '--project', 'p', 'attention', operation]
    if operation == 'list':
        args += ['--offset', '3', '--limit', '7']
    else:
        args += ['invoice-material:m']
    if operation == 'resolve':
        request = tmp_path / 'answer.json'
        request.write_text(json.dumps(REQUEST), encoding='utf-8')
        args += ['--expected-version', 'v1', '--request', str(request)]
    result = CliRunner().invoke(command.cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['ok']
    assert bool(client.posts) == (operation == 'resolve')


def test_resolve_command_requires_expected_version(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(command, 'Client', lambda _: AttentionClient())
    result = CliRunner().invoke(command.cli, ['--json', '--url', 'http://127.0.0.1:19876', '--workspace-id', 'a' * 64,
                                            'attention', 'resolve', 'invoice-material:m'])
    assert result.exit_code == 2 and '--expected-version' in result.output


def test_installed_attention_list_uses_original_synthetic_service(invoke, native):
    response, result = invoke(native, 'attention', 'list', '--limit', '1')
    assert response.returncode == 0 and result['total'] == 0 and not result['items']


def test_queued_attention_does_not_claim_zero_model_calls(monkeypatch):
    from cli_anything.invoice_assistant.utils.client import Client
    client = Client({'url': 'http://127.0.0.1:19876', 'workspace_id': 'a' * 64})
    monkeypatch.setattr(client, 'runtime', lambda: {})
    monkeypatch.setattr(client, 'state', lambda **kwargs: {'token': 'synthetic-token'})
    monkeypatch.setattr(client, 'request', lambda *args: {'status': 'resolved', 'job_id': 'synthetic-job'})
    client.post('/api/attention/resolve', {})
    assert client.operations['glm_calls'] is None and client.operations['jev_calls'] is None
