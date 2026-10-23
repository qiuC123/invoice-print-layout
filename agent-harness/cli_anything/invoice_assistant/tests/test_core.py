import copy
import io
import json
from pathlib import Path
from urllib.error import HTTPError
import pytest

from cli_anything.invoice_assistant.core import workflow as flow, reports
from cli_anything.invoice_assistant.utils.client import Client, CLIError, configuration, digest, local_url


class Fake:
    workspace_id = 'a' * 64
    def __init__(self, rows=(), queue=(), classification=None):
        self.rows, self.queue = list(rows), list(queue)
        self.classification = classification or {}; self.posts = []
    def project(self, pid):
        if pid != 'p': raise CLIError('Wrong project')
        return {'id': pid}
    def state(self, **_): return {'items': self.rows, 'classification': self.classification}
    def get(self, path): return {'queue': self.queue}
    def post(self, path, body):
        self.posts.append((path, body))
        if path == '/api/intake/prepare': return {'entry': {'id': body['id'], 'project_id': 'p', 'fields': {'amount': '1'}, 'status': 'review'}}
        return {'status': 'needs_info'}


def test_loopback_root_only_and_no_redirect_configuration():
    assert local_url('http://127.0.0.1:8765/') == 'http://127.0.0.1:8765'
    for url in ['https://127.0.0.1:8765', 'http://example.com:8765', 'http://u:p@localhost:8765', 'http://localhost:8765/api', 'http://localhost:8765?x=1']:
        with pytest.raises(CLIError): local_url(url)


def test_configuration_bom_and_url_change_needs_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    folder = tmp_path/'InvoiceAssistantCLI'; folder.mkdir()
    (folder/'config.json').write_text(json.dumps({'url': 'http://127.0.0.1:8765', 'workspace_id': 'a'*64}), encoding='utf-8-sig')
    assert configuration()['workspace_id'] == 'a'*64
    with pytest.raises(CLIError): configuration('http://127.0.0.1:8766')


def test_wrong_runtime_identity_stops_before_state(monkeypatch):
    client = Client({'url': 'http://localhost:8765', 'workspace_id': 'a'*64})
    calls = []
    def request(path): calls.append(path); return {'app_id': 'invoice-print-layout.invoice-assistant', 'ready': True, 'workspace_id': 'b'*64}
    monkeypatch.setattr(client, 'request', request)
    with pytest.raises(CLIError) as caught: client.state()
    assert caught.value.code == 'WORKSPACE_MISMATCH' and calls == ['/api/runtime']


def test_saved_recognition_is_reused_even_when_processing_missing_enabled():
    client = Fake(queue=[{'id': 'q', 'project_id': 'p', 'status': 'linked', 'fields': {'amount': '7'}, 'field_sources': {'amount': 'human'}}])
    before = copy.deepcopy(client.queue)
    body = flow.prepare(client, 'p', ['q'], recognize_missing=True)
    assert body['reused'] and not client.posts and client.queue == before


def test_missing_recognition_is_explicit_and_dry_run_never_posts():
    client = Fake(queue=[{'id': 'q', 'project_id': 'p', 'status': 'received', 'fields': {}}])
    assert flow.prepare(client, 'p', ['q'])['needs_recognition'] == ['q'] and not client.posts
    assert flow.prepare(client, 'p', ['q'], True, True)['would_recognize'] == ['q'] and not client.posts
    flow.prepare(client, 'p', ['q'], True)
    assert client.posts == [('/api/intake/prepare', {'id': 'q', 'force_ai': False})]


def test_manual_other_reviewed_submitted_and_cached_categories_protected():
    rows = [{'id': str(i), 'project_id': 'p', 'category': '其他', **extra} for i,extra in enumerate([
        {'manual_report_choice': {'project_type': 'other', 'project_name': 'User choice'}}, {'verified': True}, {'stage': 'submitted'}, {}])]
    client = Fake(rows=rows, classification={'3': {'status': 'saved', 'category': '其他', 'pending': False}})
    before = copy.deepcopy(rows)
    result = flow.classify(client, 'p', ['0','1','2','3'], True)
    assert result['reused'] and not client.posts and rows == before


def test_foreign_project_ids_fail_before_requests():
    client = Fake(queue=[{'id': 'foreign', 'project_id': 'other', 'status': 'review', 'fields': {'amount': '7'}}])
    with pytest.raises(CLIError): flow.prepare(client, 'p', ['foreign'], True)
    assert not client.posts


def test_unknown_amount_and_returned_materials_do_not_inflate_totals():
    result = flow.summary([{'amount_cents': 1200}, {'amount_cents': 900, 'amount_pending': True}, {'amount_cents': 2000, 'returned_to_synthesis': True}])
    assert result['known_total_cents'] == 1200 and result['amount_pending'] == 1 and result['expenses'] == 2


def test_post_500_is_uncertain_and_never_retried(monkeypatch):
    client = Client({'url': 'http://localhost:8765', 'workspace_id': 'a'*64}); calls = []
    monkeypatch.setattr(client, 'runtime', lambda: {})
    monkeypatch.setattr(client, 'state', lambda **_: {'token': 'synthetic-token'})
    class Opener:
        def open(self, req, **kwargs):
            calls.append(req)
            raise HTTPError(req.full_url, 500, 'error', {}, io.BytesIO(b'{}'))
    client.opener = Opener()
    with pytest.raises(CLIError) as caught: client.post('/api/intake/receive', {})
    assert caught.value.code == 'RESULT_UNCERTAIN' and len(calls) == 1


def test_only_pre_dispatch_403_refreshes_once(monkeypatch):
    client = Client({'url': 'http://localhost:8765', 'workspace_id': 'a'*64}); calls=[]; refresh=[]
    monkeypatch.setattr(client, 'runtime', lambda: {})
    monkeypatch.setattr(client, 'state', lambda refresh=False: {'token': str(refresh)})
    def request(path, body=None, token=None):
        calls.append(token)
        if len(calls) == 1: raise PermissionError()
        return {'ok': True}
    monkeypatch.setattr(client, 'request', request)
    assert client.post('/api/intake/receive', {})['ok'] and calls == ['False','True']


def test_post_invalid_json_shape_is_uncertain(monkeypatch):
    client=Client({'url':'http://localhost:8765','workspace_id':'a'*64});calls=[]
    monkeypatch.setattr(client,'runtime',lambda: {})
    monkeypatch.setattr(client,'state',lambda **_: {'token':'synthetic'})
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*_):self.close()
    class Opener:
        def open(self,*args,**kwargs):calls.append(1);return Response(b'[]')
    client.opener=Opener()
    with pytest.raises(CLIError) as caught:client.post('/api/intake/receive',{})
    assert caught.value.code=='RESULT_UNCERTAIN' and len(calls)==1


def test_report_paths_and_single_writer_lock(tmp_path):
    for value in ['../bad.pdf', 'C:bad.pdf', 'a/b.pdf', 'a\\b.pdf', 'bad.xlsx']:
        with pytest.raises(CLIError): reports.filename(value, 'pdf')
    with reports.output_lock(tmp_path/'output'):
        with pytest.raises(CLIError) as caught:
            with reports.output_lock(tmp_path/'output'): pass
        assert caught.value.exit_code == 5


def test_cached_report_damage_does_not_trigger_generation(tmp_path):
    request={'files':{'md':'record.md'}};content=b'original snapshot'
    body={'schema':reports.SCHEMA,'request':request,'signature':digest(request),'artifacts':[{'kind':'md','name':'record.md','size':len(content),'sha256':digest(content)}]}
    (tmp_path/'record.md').write_bytes(content);(tmp_path/'invoice-cli-result.json').write_text(json.dumps(body),encoding='utf-8')
    assert reports.read(tmp_path)['signature']==body['signature']
    (tmp_path/'record.md').write_bytes(b'changed')
    with pytest.raises(CLIError) as caught: reports.read(tmp_path)
    assert caught.value.code=='RESULT_DAMAGED'
