from __future__ import annotations

import copy
import json
from urllib.error import HTTPError, URLError

import pytest

from invoice_print_layout import workbuddy_probe as wb
from invoice_print_layout.construction_reports import read_object, write_json


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.online = True
        self.send_error = None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path == '/localassistant':
            return {'online': self.online}
        if method == 'POST':
            if self.send_error:
                raise self.send_error
            return {'message_id': 'sent / 1'}
        assert path == '/localassistant/message?message_id=sent+%2F+1'
        return {'messages': self.messages}


def valid_reply(folder):
    request = read_object(folder / 'request.json')
    return wb.reply_header(request) | {'records': [
        {'photo_id': row['photo_id'], 'role': row['provided_role']} for row in wb.RECORDS
    ]}


def message(value, **changes):
    result = {'message_id': 'reply-1', 'role': 'assistant', 'msg_type': 'text',
              'content': [json.dumps(value, ensure_ascii=False)]}
    result.update(changes)
    return result


def test_dispatch_poll_resume_and_verified_reuse(tmp_path):
    wb.prepare(tmp_path)
    api = FakeAPI()
    assert wb.dispatch(tmp_path, api)['status'] == 'waiting'
    assert wb.dispatch(tmp_path, api)['status'] == 'waiting'
    assert len([call for call in api.calls if call[0] == 'POST']) == 1
    assert wb.poll(tmp_path, api)['status'] == 'waiting'
    api.messages = [message(valid_reply(tmp_path))]
    state = wb.poll(tmp_path, api)
    assert state['status'] == 'validated'
    assert state['native_task_completion_verified'] is False
    calls = len(api.calls)
    files = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.glob('*.json')}
    assert wb.dispatch(tmp_path, api) == wb.poll(tmp_path, api) == state
    assert len(api.calls) == calls
    assert files == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.glob('*.json')}
    write_json(tmp_path / 'result.json', {'changed': True})
    with pytest.raises(wb.ProbeError, match='result_changed'):
        wb.poll(tmp_path, api)


def test_offline_and_missing_auth_never_dispatch(tmp_path, monkeypatch):
    wb.prepare(tmp_path)
    api = FakeAPI()
    api.online = False
    with pytest.raises(wb.ProbeError, match='assistant_offline'):
        wb.dispatch(tmp_path, api)
    assert len(api.calls) == 1
    assert wb.load(tmp_path)[0]['status'] == 'prepared'
    monkeypatch.delenv(wb.TOKEN_ENV, raising=False)
    monkeypatch.setattr(wb, 'build_opener', lambda *a: pytest.fail('must not access network'))
    with pytest.raises(wb.ProbeError, match='needs_authorization'):
        wb.dispatch(tmp_path)
    assert wb.load(tmp_path)[0]['status'] == 'prepared'


@pytest.mark.parametrize('interrupted', [False, True])
def test_uncertain_send_cannot_repeat(tmp_path, interrupted):
    wb.prepare(tmp_path)
    api = FakeAPI()
    api.send_error = KeyboardInterrupt() if interrupted else TimeoutError('secret in exception')
    with pytest.raises(KeyboardInterrupt if interrupted else wb.ProbeError):
        wb.dispatch(tmp_path, api)
    state = wb.load(tmp_path)[0]
    assert state['status'] == ('sending' if interrupted else 'dispatch_unknown')
    assert 'secret' not in json.dumps(state)
    with pytest.raises(wb.ProbeError, match='do_not_resend'):
        wb.dispatch(tmp_path, api)
    assert len([call for call in api.calls if call[0] == 'POST']) == 1


@pytest.mark.parametrize('mutation', ['task', 'nonce', 'hash', 'duplicate', 'missing', 'role', 'extra', 'synthetic'])
def test_bad_receipts_stay_waiting(tmp_path, mutation):
    wb.prepare(tmp_path)
    api = FakeAPI()
    wb.dispatch(tmp_path, api)
    value = valid_reply(tmp_path)
    if mutation in ('task', 'nonce', 'hash'):
        value[{'task': 'task_id', 'nonce': 'nonce', 'hash': 'input_sha256'}[mutation]] = 'other'
    elif mutation == 'duplicate':
        value['records'][1] = copy.deepcopy(value['records'][0])
    elif mutation == 'missing':
        value['records'].pop()
    elif mutation == 'role':
        value['records'][0]['role'] = '搭建工人'
    elif mutation == 'extra':
        value['records'][0]['name'] = 'unexpected'
    else:
        value['synthetic'] = 1
    api.messages = [message(value)]
    assert wb.poll(tmp_path, api)['status'] == 'waiting'
    assert not (tmp_path / 'result.json').exists()


def test_user_echo_partial_and_permission_request_are_not_completion(tmp_path):
    wb.prepare(tmp_path)
    api = FakeAPI()
    wb.dispatch(tmp_path, api)
    good = valid_reply(tmp_path)
    api.messages = [message(good, role='user'), message(good, msg_type='permission_response'),
                    message(good, content=['{"task_id":']), message(good, message_id='sent / 1')]
    assert wb.poll(tmp_path, api)['status'] == 'waiting'
    assert not (tmp_path / 'result.json').exists()


def test_input_change_and_real_data_rejected(tmp_path):
    wb.prepare(tmp_path)
    with pytest.raises(wb.ProbeError, match='already_exists'):
        wb.prepare(tmp_path)
    request = read_object(tmp_path / 'request.json')
    request['records'][0]['photo_id'] = 'REAL-001'
    write_json(tmp_path / 'request.json', request)
    with pytest.raises(wb.ProbeError, match='request_changed'):
        wb.load(tmp_path)
    state = read_object(tmp_path / 'state.json')
    state['request_sha256'] = wb.digest(request)
    write_json(tmp_path / 'state.json', state)
    with pytest.raises(wb.ProbeError, match='synthetic_fixture_required'):
        wb.load(tmp_path)


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size):
        return self.raw[:size]


def test_http_contract_and_redirect_refusal(monkeypatch):
    monkeypatch.setenv(wb.TOKEN_ENV, 'example-only-token')
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == wb.BASE_URL + '/localassistant'
            assert request.get_header('Authorization') == 'Bearer example-only-token'
            assert timeout == 20
            return FakeResponse(b'{"code":0,"data":{"online":true}}')
    def build(handler):
        assert isinstance(handler, wb.NoRedirect)
        assert handler.redirect_request(None, None, 302, '', {}, 'https://unrelated.example') is None
        return Opener()
    monkeypatch.setattr(wb, 'build_opener', build)
    assert wb.online() == {'status': 'online'}
    with pytest.raises(wb.ProbeError, match='unsupported_endpoint'):
        wb.request_api('GET', 'https://unrelated.example')


@pytest.mark.parametrize('error,reason', [
    (HTTPError('url', 401, 'token=example', {}, None), 'needs_authorization'),
    (HTTPError('url', 403, 'token=example', {}, None), 'needs_authorization'),
    (HTTPError('url', 302, 'token=example', {}, None), 'http_302'),
    (URLError('token=example'), 'transport_or_response_error'),
])
def test_http_failures_redact_server_details(monkeypatch, error, reason):
    monkeypatch.setenv(wb.TOKEN_ENV, 'example-only-token')
    class Opener:
        def open(self, *a, **kw):
            raise error
    monkeypatch.setattr(wb, 'build_opener', lambda *a: Opener())
    with pytest.raises(wb.ProbeError) as caught:
        wb.online()
    assert str(caught.value) == reason


@pytest.mark.parametrize('raw', [b'{}', b'{"code":false,"data":{}}',
                                b'{"code":1,"msg":"secret"}', b'{"code":0,"data":[]}'])
def test_malformed_api_response_rejected(monkeypatch, raw):
    monkeypatch.setenv(wb.TOKEN_ENV, 'example-only-token')
    class Opener:
        def open(self, *a, **kw):
            return FakeResponse(raw)
    monkeypatch.setattr(wb, 'build_opener', lambda *a: Opener())
    with pytest.raises(wb.ProbeError):
        wb.online()
