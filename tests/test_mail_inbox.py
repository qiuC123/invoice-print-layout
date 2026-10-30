"""Synthetic inbox originals and dismissal through the real local HTTP boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout import mail_agent
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.mail163 import Mail163Settings
from tests.test_invoice_assistant import invoice_server
from tests.test_mail_agent import env as env, enable, message, run


def received(env: Any) -> dict[str, Any]:
    service, _, _, _, _, _, results, _ = env
    remote = message(1, '待选项目的合成邮件')
    remote['sent_date'] = '2026-10-12'
    results.append(remote)
    enable(service)
    run(service)
    return service._messages()[0]


def test_attachment_names_and_bytes_without_paths(env: Any) -> None:
    record = received(env)
    service = env[0]
    visible = service.status()['messages'][0]
    assert visible['attachments'] == [{'name': 'invoice.pdf', 'index': 0}]
    assert visible['material_count'] == 1
    assert str(service.originals) not in json.dumps(service.status(), ensure_ascii=False)
    payload, name = service.attachment(record['id'], 0)
    assert (payload, name) == (env[6][0]['files'][0]['payload'], 'invoice.pdf')


@pytest.mark.parametrize('index', [-1, 1, True, '0', None])
def test_attachment_rejects_invalid_index(env: Any, index: Any) -> None:
    record = received(env)
    with pytest.raises(ValueError, match='序号'):
        env[0].attachment(record['id'], index)


@pytest.mark.parametrize('escape', [False, True])
def test_attachment_rejects_digest_change_and_path_escape(env: Any, escape: bool) -> None:
    record = received(env)
    service = env[0]
    file = record['files'][0]
    if escape:
        outside = service.store.root / 'outside.pdf'
        outside.write_bytes(b'outside')
        file.update(path=str(service.originals / '..' / outside.name), digest=hashlib.sha256(b'outside').hexdigest())
        service._message_update(record['id'], files=[file])
    else:
        Path(file['path']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='路径|变化'):
        service.attachment(record['id'], 0)


def test_dismiss_preserves_original_excludes_count_and_skips_reroute_dispatch(env: Any) -> None:
    record = received(env)
    service, store, agent, project, *_ = env
    original = Path(record['files'][0]['path'])
    before = original.read_bytes()
    assert service.status()['unresolved_count'] == 1
    state = service.dismiss(record['id'])
    assert state['messages'][0]['status'] == 'dismissed' and state['unresolved_count'] == 0
    assert service._message(record['id']) == {**record, 'status': 'dismissed'}
    projects = ExpenseProjects(store)
    current = next(row for row in projects.list() if row['id'] == project)
    projects.set_period(project, '2026-10-01', '2026-10-31', current['project_revision'])
    assert service.reroute_unassigned() == 0
    service._run(False)
    run(service)
    assert service._message(record['id'])['status'] == 'dismissed'
    assert not agent.jobs.list(project) and not IntakeQueue(store).list()
    assert original.read_bytes() == before
    assert service.attachment(record['id'], 0)[0] == before
    with pytest.raises(ValueError):
        service.assign(record['id'], project)


@pytest.mark.parametrize('changes', [
    {'job_id': 'started'}, {'material_ids': ['received']}, {'project_id': 'assigned'},
    {'status': 'waiting_agent'}, {'status': 'attention'}, {'status': 'dismissed'},
])
def test_dismiss_rejects_started_or_ineligible_mail(env: Any, changes: dict[str, Any]) -> None:
    record = received(env)
    service = env[0]
    before = service._message_update(record['id'], **changes)
    with pytest.raises(ValueError, match='不能忽略'):
        service.dismiss(record['id'])
    assert service._message(record['id']) == before


def test_foreign_account_cannot_read_or_dismiss(env: Any) -> None:
    record = received(env)
    env[5][0] = Mail163Settings('other@163.com')
    for call in [lambda: env[0].attachment(record['id'], 0), lambda: env[0].dismiss(record['id'])]:
        with pytest.raises(ValueError, match='另一邮箱'):
            call()


def test_inbox_http_tokens_origins_types_and_invalid_originals(
        env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = received(env)
    service = env[0]
    monkeypatch.setattr(mail_agent, 'MailAgentService', lambda store, agent: service)
    url = '/api/agent/mail/attachment?id=' + record['id'] + '&index=0'
    with invoice_server(tmp_path / 'http') as client:
        assert client.json('GET', '/api/agent/mail')[1]['messages'][0]['attachments'] == [
            {'name': 'invoice.pdf', 'index': 0}]
        assert client.request('GET', url, authenticated=False) == (200, Path(record['files'][0]['path']).read_bytes())
        for index in ['-1', '1', 'bad', '']:
            assert client.request('GET', url.rsplit('=', 1)[0] + '=' + index)[0] == 404
        for headers in [{'Origin': 'https://outside.invalid'}, {'Host': 'outside.invalid'}]:
            client.connection.request('GET', url, headers=headers)
            response = client.connection.getresponse()
            assert response.status == 403
            response.read()
        for name, content_type, disposition in [('invoice.pdf', 'application/pdf', 'inline'),
                                               ('image.PNG', 'image/png', 'inline'),
                                               ('file.html', 'application/octet-stream', 'attachment')]:
            file = {**record['files'][0], 'name': name}
            service._message_update(record['id'], files=[file])
            client.connection.request('GET', url)
            response = client.connection.getresponse()
            assert response.status == 200 and response.getheader('Content-Type') == content_type
            assert response.getheader('Content-Disposition').startswith(disposition + ';')
            assert response.getheader('X-Content-Type-Options') == 'nosniff'
            response.read()
        outside = service.store.root / 'outside.pdf'
        outside.write_bytes(b'outside')
        service._message_update(record['id'], files=[{'name': 'outside.pdf', 'path': str(outside),
                                                     'digest': hashlib.sha256(b'outside').hexdigest()}])
        assert client.request('GET', url)[0] == 404
        service._message_update(record['id'], files=record['files'])
        Path(record['files'][0]['path']).write_bytes(b'changed')
        assert client.request('GET', url)[0] == 404
        assert client.json('POST', '/api/agent/mail/dismiss', {'id': record['id']}, authenticated=False)[0] == 403
        assert service._message(record['id'])['status'] == 'unassigned'
        assert client.json('POST', '/api/agent/mail/dismiss', {'id': record['id']})[1]['messages'][0]['status'] == 'dismissed'
        assert client.json('POST', '/api/agent/mail/dismiss', {'id': record['id']})[0] == 400
        assert client.json('POST', '/api/agent/mail/dismiss', {'id': 1})[0] == 400
