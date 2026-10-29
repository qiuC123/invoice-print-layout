"""Project period HTTP contracts against synthetic workspace and fake credentials."""
from typing import Any

import pytest

from invoice_print_layout import mail163, mail_agent, phone_link
from invoice_print_layout.agent_jobs import AgentService
from invoice_print_layout.expense_projects import ExpenseProjects
from tests.test_mail_agent import message
from tests.test_settings_workflow import Client, secrets, web  # noqa: F401


def period(project: dict[str, Any], start: str = '', end: str = '') -> dict[str, Any]:
    return {'project_id': project['id'], 'start_date': start, 'end_date': end,
            'expected_revision': project['project_revision']}


def test_period_create_edit_conflict_clear_and_state(web: Any) -> None:
    code, first = web.client.request('/api/projects', {
        'name': 'Synthetic period first', 'start_date': '2026-10-01', 'end_date': '2026-10-31'})
    assert code == 200 and first['start_date'] == '2026-10-01' and first['end_date'] == '2026-10-31'
    code, second = web.client.request('/api/projects', {'name': 'Synthetic period second'})
    assert code == 200 and second['start_date'] == second['end_date'] == ''
    body = period(second, '2026-10-15')
    code, edited = web.client.request('/api/projects/period', body)
    assert code == 200 and edited['overlaps'] == ['Synthetic period first'] and edited['rerouted'] == 0
    assert edited['project_revision'] == second['project_revision'] + 1
    assert web.client.request('/api/projects/period', body)[0] == 400
    code, cleared = web.client.request('/api/projects/period', period(edited))
    assert code == 200 and cleared['start_date'] == cleared['end_date'] == '' and cleared['overlaps'] == []
    projects = web.client.request('/api/state')[1]['projects']
    assert next(row for row in projects if row['id'] == second['id'])['project_revision'] == cleared['project_revision']


@pytest.mark.parametrize('start,end', [('', '2026-10-31'), ('2026-02-30', ''),
                                    ('2026-11-02', '2026-11-01'), ('20261001', '')])
def test_period_http_rejects_invalid_dates_without_changing_project(web: Any, start: str, end: str) -> None:
    project = next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project)
    assert web.client.request('/api/projects/period', period(project, start, end))[0] == 400
    current = next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project)
    assert current == project


def test_period_save_reroutes_pending_mail_and_preserves_expenses(web: Any) -> None:
    settings = mail163.Mail163Settings('synthetic-settings@163.com')
    mail163.write_mail_settings(web.workspace / 'mail163.toml', settings)
    service = mail_agent.MailAgentService(web.store, AgentService(web.store))
    config = service._config()
    account = service._account(settings, config)
    remote = message(1, 'Synthetic message without project name')
    remote['sent_date'] = '2026-10-15'
    service._persist_message(account, remote, config)
    assert service.status()['messages'][0]['status'] == 'unassigned'
    expense = web.store.create({'title': 'Existing synthetic expense', 'amount': '12', 'project_id': web.project})
    project = next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project)
    code, result = web.client.request('/api/projects/period', period(project, '2026-10-01', '2026-10-31'))
    assert code == 200 and result['rerouted'] == 1
    routed = service.status()['messages'][0]
    assert routed['project_id'] == web.project and routed['status'] == 'waiting_agent' and routed['route_reason'] == 'date'
    assert web.store.get(expense['id']) == expense


def test_period_endpoint_keeps_token_host_origin_guards(web: Any) -> None:
    project = next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project)
    for headers in ({'Host': 'foreign.test'}, {'Origin': 'http://foreign.test'}, {'X-Workbench-Token': 'wrong'}):
        assert web.client.request('/api/projects/period', period(project, '2026-10-01'), **headers)[0] == 403
    assert next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project) == project


def test_paired_phone_cannot_modify_project_period(web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    original = phone_link.lan_address
    monkeypatch.setattr(phone_link, 'lan_address', lambda address: address == '127.0.0.1' or original(address))
    monkeypatch.setattr(phone_link, 'addresses', lambda: [{'address': '127.0.0.1', 'label': 'Synthetic LAN'}])
    server_class = phone_link.ThreadingHTTPServer
    monkeypatch.setattr(phone_link, 'ThreadingHTTPServer', lambda address, handler: server_class((address[0], 0), handler))
    code, connection = web.client.request('/api/phone-link/start', {'address': '127.0.0.1'})
    assert code == 200
    phone = Client(connection['port'])
    assert phone.request('/api/phone/pair', {'code': connection['pair_url'].split('#')[1]})[0] == 200
    phone.token = phone.request('/api/state')[1]['token']
    project = next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project)
    assert phone.request('/api/projects/period', period(project, '2026-10-01'))[0] == 403
    assert next(row for row in ExpenseProjects(web.store).list() if row['id'] == web.project) == project
