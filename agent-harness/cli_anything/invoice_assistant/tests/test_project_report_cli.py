import io
import json

from click.testing import CliRunner
import pytest

from cli_anything.invoice_assistant import invoice_assistant_cli as command
from cli_anything.invoice_assistant.core import reports
from cli_anything.invoice_assistant.utils.client import CLIError


class ReportClient:
    def __init__(self, config=None, result=None):
        self.operations = {}
        self.posts = []
        self.result = result or {'ok': True, 'status': 'complete', 'files': {'pdf': 'synthetic.pdf'},
                                 'expense_count': 1, 'total_cents': 1234, 'expense_ids': ['ready']}

    def project(self, project_id):
        if project_id != 'p':
            raise CLIError('项目不存在', 'PROJECT_NOT_FOUND', 4)
        return {'id': project_id}

    def state(self):
        return {'items': [{'id': 'ready', 'project_id': 'p', 'ready': True, 'stage': 'draft'},
                          {'id': 'pending', 'project_id': 'p', 'ready': False, 'stage': 'draft'},
                          {'id': 'foreign', 'project_id': 'other', 'ready': True, 'stage': 'draft'}]}

    def post(self, path, body):
        self.posts.append((path, body))
        return self.result


def test_report_command_preserves_project_version_and_request_id():
    client = ReportClient()
    result = reports.create(client, 'p', 'request-123', 'version-123')
    assert result['expense_count'] == 1
    assert client.posts == [('/api/reports/create', {'project_id': 'p', 'request_id': 'request-123',
                                                    'expected_version': 'version-123'})]


def test_report_create_dry_run_and_invalid_key_do_not_post():
    client = ReportClient()
    result = reports.create(client, 'p', 'dry-123', dry_run=True)
    assert result['expense_ids'] == ['ready'] and result['dry_run']
    with pytest.raises(CLIError):
        reports.create(client, 'p', '../invalid')
    assert not client.posts


def test_report_business_failure_keeps_code_and_retryable(monkeypatch):
    client = ReportClient(result={'ok': False, 'error': {'code': 'version_changed', 'message': '项目已变化',
                                                       'retryable': False}})
    with pytest.raises(CLIError) as caught:
        reports.create(client, 'p', 'failed-123')
    assert caught.value.code == 'version_changed' and caught.value.exit_code == 4
    assert caught.value.retryable is False
    output = io.StringIO()
    monkeypatch.setattr(command.click, 'echo', lambda value: output.write(value))
    command.error(caught.value, True)
    assert json.loads(output.getvalue())['error']['retryable'] is False


def test_report_unknown_delivery_is_explicitly_safe_to_retry_same_key(monkeypatch):
    client = ReportClient()

    def uncertain(*_):
        raise CLIError('unknown', 'RESULT_UNCERTAIN', 6)

    monkeypatch.setattr(client, 'post', uncertain)
    with pytest.raises(CLIError) as caught:
        reports.create(client, 'p', 'timeout-123')
    assert caught.value.retryable and caught.value.exit_code == 6


@pytest.mark.parametrize('result', [{'ok': False, 'error': 'invalid'}, {'status': 'unknown'}])
def test_report_malformed_receipt_cannot_become_success(result):
    with pytest.raises(CLIError) as caught:
        reports.create(ReportClient(result=result), 'p', 'invalid-response')
    assert caught.value.code == 'INVALID_RESPONSE'


@pytest.mark.parametrize('global_project', [True, False])
def test_report_create_accepts_global_or_command_project(tmp_path, monkeypatch, global_project):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    client = ReportClient()
    monkeypatch.setattr(command, 'Client', lambda _: client)
    args = ['--json', '--url', 'http://127.0.0.1:19876', '--workspace-id', 'a' * 64]
    if global_project:
        args += ['--project', 'p']
    args += ['reports', 'create']
    if not global_project:
        args += ['--project', 'p']
    args += ['--request-id', 'click-123', '--expected-version', 'v1']
    result = CliRunner().invoke(command.cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['expense_count'] == 1
    assert client.posts[0][1]['project_id'] == 'p'


def test_installed_report_command_uses_native_token_and_structured_failure(invoke, native):
    response, result = invoke(native, 'reports', 'create', '--project', native['project'],
                              '--request-id', 'native-empty-001', check=False, project=False)
    assert response.returncode == 4
    assert result['error']['code'] == 'nothing_to_package'
    assert result['error']['retryable'] is False
    assert 'token' not in response.stdout
