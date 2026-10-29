"""Synthetic transport exercises durable mailbox boundaries against real stores/jobs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import mail_agent
from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from invoice_print_layout.agent_tools import InvoiceTools
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.mail163 import Mail163Settings
from invoice_print_layout.workbench import ExpenseStore


def pdf(text: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        for index, line in enumerate(text.splitlines()):
            page.insert_text((35, 45 + index * 24), line, fontname='china-s', fontsize=12)
        return document.tobytes()


def message(uid: int, subject: str = '测试项目发票', *, payload: bytes | None = None) -> dict[str, Any]:
    return {'uid': uid, 'subject': subject, 'sender': 'synthetic@example.invalid', 'message_id': f'<{uid}@test>',
            'text': '', 'files': [{'name': 'invoice.pdf', 'payload': payload or pdf('合成发票\n标识' + str(uid)),
                                  'source': 'attachment'}], 'issues': []}


class StubAgent:
    def __init__(self, store: ExpenseStore):
        self.jobs = Jobs(store)
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    store = ExpenseStore(tmp_path)
    project = ExpenseProjects(store).create('测试项目')['id']
    second = ExpenseProjects(store).create('其他项目')['id']
    agent = StubAgent(store)
    settings = [Mail163Settings('synthetic@163.com')]
    results: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def fetch(active: Any, code: str, **kwargs: Any) -> dict[str, Any]:
        assert code == 'secret-not-in-any-receipt'
        calls.append({'settings': active, **kwargs})
        return {'uidvalidity': '100', 'messages': results, 'more': False}

    monkeypatch.setattr(mail_agent, 'read_mail_settings', lambda path: settings[0])
    monkeypatch.setattr(mail_agent, 'read_auth_code', lambda address: 'secret-not-in-any-receipt')
    service = mail_agent.MailAgentService(store, agent, fetcher=fetch)
    monkeypatch.setattr(service, 'kick', lambda: None)
    return service, store, agent, project, second, settings, results, calls


def enable(service: Any, **values: Any) -> None:
    service.configure({'enabled': True, **values})


def run(service: Any) -> None:
    service._run(True)


def test_disabled_default_never_reads_vault_or_network(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _, _, _, _, _, calls = env
    monkeypatch.setattr(mail_agent, 'read_auth_code', lambda address: pytest.fail('Vault read while disabled'))
    assert service.status()['enabled'] is False
    service._run(True)
    assert calls == []
    assert not service.config_path.exists()
    with pytest.raises(ValueError, match='开启'):
        service.collect_now()


def test_unique_project_originals_then_single_job_and_duplicate_poll(env: Any) -> None:
    service, store, agent, project, _, _, results, calls = env
    remote = message(7)
    results.append(remote)
    enable(service, use_model=True)
    run(service)
    visible = service.status()['messages'][0]
    assert visible['project_id'] == project and visible['status'] == 'queued'
    job = agent.jobs.get(visible['job_id'])
    assert job['use_model'] is False and len(job['material_ids']) == 1
    saved = service._message(visible['id'])
    original = Path(saved['files'][0]['path'])
    assert original.read_bytes() == remote['files'][0]['payload']
    entry = IntakeQueue(store).get(job['material_ids'][0])
    assert entry['source'] == 'mail_agent' and entry['batch'] == visible['id']
    run(service)
    assert calls[-1]['after_uid'] == 7
    assert calls[-1]['uidvalidity'] == '100'
    assert len(agent.jobs.list(project)) == 1
    assert len(service._messages()) == len(IntakeQueue(store).list()) == 1
    assert 'secret-not-in-any-receipt' not in service.config_path.read_text(encoding='utf-8')
    assert 'secret-not-in-any-receipt' not in json.dumps(service.status(), ensure_ascii=False)


@pytest.mark.parametrize('subject', ['普通电子发票', '测试项目与其他项目共同采购'])
def test_unknown_or_multiple_projects_wait_for_explicit_assignment(env: Any, subject: str) -> None:
    service, store, agent, project, _, _, results, _ = env
    results.append(message(3, subject))
    enable(service)
    run(service)
    visible = service.status()['messages'][0]
    assert visible['status'] == 'unassigned' and visible['project_id'] == '' and visible['job_id'] == ''
    assert IntakeQueue(store).list() == [] and agent.jobs.list(project) == []
    service.assign(visible['id'], project)
    service._run(False)
    assert service.status()['messages'][0]['job_id']
    assert len(agent.jobs.list(project)) == 1


def test_default_project_is_only_used_when_explicitly_configured(env: Any) -> None:
    service, _, _, _, second, _, results, _ = env
    results.append(message(1, '没有项目名的发票'))
    enable(service, default_project_id=second)
    run(service)
    assert service.status()['messages'][0]['project_id'] == second


@pytest.mark.parametrize('changed', [Mail163Settings('other@163.com'), Mail163Settings('synthetic@163.com', 'Archive')])
def test_new_account_or_folder_cannot_inherit_old_default_project(env: Any, changed: Mail163Settings) -> None:
    service, _, agent, _, second, settings, results, _ = env
    results.append(message(1, '没有项目名的发票'))
    enable(service, default_project_id=second)
    run(service)
    settings[0] = changed
    assert service.status()['default_project_id'] == ''
    results[:] = [message(2, '没有项目名的另一个发票')]
    run(service)
    assert service.status()['messages'][0]['status'] == 'unassigned'
    assert service.status()['messages'][0]['project_id'] == ''
    assert len(agent.jobs.list(second)) == 1
    service.configure({'enabled': True})
    assert service.status()['default_project_id'] == ''


def test_project_in_body_can_route_without_inference(env: Any) -> None:
    service, _, _, project, _, _, results, _ = env
    remote = message(1, '发票')
    remote['text'] = '本次购买属于测试项目，请查收附件。'
    results.append(remote)
    enable(service)
    run(service)
    assert service.status()['messages'][0]['project_id'] == project


def test_account_folder_and_uidvalidity_are_separate_namespaces(env: Any) -> None:
    service, _, agent, project, _, settings, results, calls = env
    results.append(message(100))
    enable(service)
    run(service)
    settings[0] = Mail163Settings('second@163.com')
    results[:] = [message(1)]
    run(service)
    assert calls[-1]['after_uid'] == 0 and calls[-1]['uidvalidity'] == ''
    settings[0] = Mail163Settings('second@163.com', 'Archive')
    run(service)
    assert calls[-1]['after_uid'] == 0 and calls[-1]['uidvalidity'] == ''
    assert len(service._messages()) == 3
    assert len({m['id'] for m in service._messages()}) == 3
    with service.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM mail_agent_accounts').fetchone()[0] == 3
    # All three messages may supplement a queued mailbox task, never a competing job.
    assert len(agent.jobs.list(project)) == 1


def test_uidvalidity_change_reuses_no_old_message_identity(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    results.append(message(1, '无项目发票'))
    enable(service)
    run(service)
    old = service.status()['messages'][0]['id']
    service._fetcher = lambda *args, **kwargs: {'uidvalidity': '200', 'messages': results, 'more': False}
    run(service)
    assert len(service._messages()) == 2
    assert service.status()['messages'][0]['id'] != old


def test_global_content_duplicate_does_not_move_existing_material(env: Any) -> None:
    service, store, agent, project, second, _, results, _ = env
    payload = pdf('旧材料\n实付17.00')
    prior = IntakeQueue(store).receive('old.pdf', payload, project_id=second)
    results.append(message(4, payload=payload))
    enable(service)
    run(service)
    assert service.status()['messages'][0]['status'] == 'duplicate'
    assert IntakeQueue(store).get(prior['id']) == prior
    assert agent.jobs.list(project) == []
    assert len(IntakeQueue(store).list()) == 1


def test_retry_failure_is_bounded_and_error_does_not_leak_secret(env: Any) -> None:
    service, _, _, _, _, _, _, _ = env
    attempts = []

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(1)
        raise RuntimeError('secret-not-in-any-receipt')

    service._fetcher = broken
    enable(service)
    for _ in range(3):
        run(service)
    state = service.status()
    assert state['suspended'] is True and len(attempts) == 3
    assert 'secret-not-in-any-receipt' not in json.dumps(state)
    # A real kick honors suspension, with the only worker allowed being local dispatch.
    service._dispatch_at = time.time() + 100
    service._force = False  # Direct _run above bypassed kick's normal one-shot flag consumption.
    mail_agent.MailAgentService.kick(service)
    assert service._thread is None


def test_failed_poll_recovers_and_keeps_saved_originals(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    remote = message(1, '未分配发票')
    results.append(remote)
    enable(service)
    run(service)
    saved = service._messages()[0]
    service._fetcher = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('bad'))
    run(service)
    assert service.status()['last_error']
    service._fetcher = lambda *a, **k: {'uidvalidity': '100', 'messages': results, 'more': False}
    run(service)
    assert service.status()['last_error'] == '' and not service.status()['suspended']
    assert Path(saved['files'][0]['path']).read_bytes() == remote['files'][0]['payload']
    assert len(service._messages()) == 1


def test_crash_after_receive_before_job_creation_recovers_same_intake(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, agent, project, _, _, results, _ = env
    results.append(message(1))
    enable(service)
    real = agent.jobs.create
    monkeypatch.setattr(agent.jobs, 'create', lambda *a, **k: (_ for _ in ()).throw(OSError('interrupted')))
    run(service)
    first = service._messages()[0]
    assert first['status'] == 'waiting_agent' and len(IntakeQueue(store).list()) == 1
    monkeypatch.setattr(agent.jobs, 'create', real)
    service._message_update(first['id'], retry_at=0)
    service._run(False)
    assert len(IntakeQueue(store).list()) == len(agent.jobs.list(project)) == 1


def test_crash_after_job_commit_before_mail_receipt_recovers_request(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, agent, project, _, _, results, _ = env
    results.append(message(1))
    enable(service)
    real = service._message_update

    def interrupt(key: str, **changes: Any) -> dict[str, Any]:
        if changes.get('job_id'):
            raise OSError('crash after job commit')
        return real(key, **changes)

    monkeypatch.setattr(service, '_message_update', interrupt)
    run(service)
    assert len(agent.jobs.list(project)) == 1
    monkeypatch.setattr(service, '_message_update', real)
    first = service._messages()[0]
    service._message_update(first['id'], retry_at=0)
    service._run(False)
    assert len(agent.jobs.list(project)) == 1
    assert service.status()['messages'][0]['job_id'] == agent.jobs.list(project)[0]['id']


def test_foreign_active_job_waits_then_dispatches_after_it_finishes(env: Any) -> None:
    service, store, agent, project, _, _, results, _ = env
    entry = IntakeQueue(store).receive('manual.pdf', pdf('人工材料'), project_id=project)
    manual = agent.jobs.create(project, [entry['id']], 'manual-request')
    results.append(message(1))
    enable(service)
    run(service)
    assert len(agent.jobs.list(project)) == 1 and service.status()['messages'][0]['status'] == 'waiting_agent'
    assert service._messages()[0]['dispatch_attempts'] == 0
    agent.jobs.stop(manual['id'], 'ready', '人工任务完成')
    service._run(False)
    assert len(agent.jobs.list(project)) == 2


def test_later_mail_supplements_owned_needs_input_without_resetting_budget(env: Any) -> None:
    service, _, agent, project, _, _, results, _ = env
    results.append(message(1))
    enable(service)
    run(service)
    first = agent.jobs.list(project)[0]
    agent.jobs.update(first['id'], state='needs_input', models=2, tools=5, elapsed=12.5,
                      question={'text': '缺付款'}, approval=None)
    results[:] = [message(2, payload=pdf('付款成功\n金额17.00'))]
    run(service)
    jobs = agent.jobs.list(project)
    assert len(jobs) == 1
    assert len(jobs[0]['material_ids']) == 2 and jobs[0]['state'] == 'queued'
    assert (jobs[0]['models'], jobs[0]['tools'], jobs[0]['elapsed']) == (2, 5, 12.5)
    assert jobs[0]['question'] is None
    assert len({m['job_id'] for m in service._messages()}) == 1


def test_mail_cannot_append_to_task_with_existing_expense_effects(env: Any) -> None:
    service, _, agent, project, _, _, results, _ = env
    results.append(message(1))
    enable(service)
    run(service)
    first = agent.jobs.list(project)[0]
    agent.jobs.update(first['id'], state='needs_input', item_ids=['existing-item'])
    results[:] = [message(2)]
    run(service)
    assert len(agent.jobs.list(project)) == 1
    assert service.status()['messages'][0]['status'] == 'waiting_agent'


def test_crash_after_supplement_commit_recovers_even_after_job_finished(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, agent, project, _, _, results, _ = env
    results.append(message(1))
    enable(service)
    run(service)
    first = agent.jobs.list(project)[0]
    agent.jobs.update(first['id'], state='needs_input', models=2, tools=5, elapsed=12.5)
    results[:] = [message(2)]
    real = service._message_update

    def interrupted(key: str, **changes: Any) -> dict[str, Any]:
        if changes.get('job_id'):
            raise OSError('crash after append commit')
        return real(key, **changes)

    monkeypatch.setattr(service, '_message_update', interrupted)
    run(service)
    assert len(agent.jobs.list(project)[0]['material_ids']) == 2
    agent.jobs.update(first['id'], state='ready', item_ids=['new-draft'])
    monkeypatch.setattr(service, '_message_update', real)
    pending = service._messages()[-1]
    service._message_update(pending['id'], retry_at=0)
    service._run(False)
    assert len(agent.jobs.list(project)) == 1
    assert service.status()['messages'][0]['job_id'] == first['id']
    assert service.status()['messages'][0]['status'] == 'ready'


def test_ordinary_mail_is_durably_ignored_without_filling_ui(env: Any) -> None:
    service, _, _, _, _, _, results, calls = env
    ordinary = message(10, '普通无附件通知')
    ordinary['files'] = []
    results.append(ordinary)
    enable(service)
    run(service)
    assert service._messages()[0]['status'] == 'ignored'
    assert service.status()['messages'] == []
    run(service)
    assert calls[-1]['after_uid'] == 10


def test_failed_invoice_link_without_files_stays_visible_for_manual_action(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    remote = message(10)
    remote.update(files=[], issues=['下载链接需要登录，请补传正式PDF'])
    results.append(remote)
    enable(service)
    run(service)
    assert service.status()['messages'][0]['status'] == 'attention'


@pytest.mark.parametrize('issue', ['第二份附件损坏，无法读取', '另一份正式发票领取链接下载失败'])
def test_partial_mail_with_valid_receipt_and_failed_attachment_cannot_create_expense(env: Any, issue: str) -> None:
    service, store, agent, project, _, _, results, _ = env
    original = pdf('收据\n商家名称：合成工具商店\n商品名称：胶带\n实付：17.00')
    remote = message(10, payload=original)
    remote['issues'] = [issue]
    results.append(remote)
    enable(service)
    run(service)
    state = service.status()['messages'][0]
    assert state['status'] == 'attention' and state['issues'] == [issue]
    assert state['material_count'] == 1 and state['job_id'] == ''
    record = service._message(state['id'])
    assert Path(record['files'][0]['path']).read_bytes() == original
    assert agent.jobs.list(project) == [] and IntakeQueue(store).list() == [] and store.list_items() == []
    # The durable cursor acknowledges receipt; it never interprets a partially
    # received email as complete invoice evidence on the next poll.
    run(service)
    assert service.status()['messages'][0]['status'] == 'attention'
    assert agent.jobs.list(project) == []


def test_old_unresolved_mail_remains_visible_ahead_of_recent_completed_notices(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    results[:] = [message(i, '未分配发票') for i in range(1, 36)]
    enable(service)
    run(service)
    all_messages = service._messages()
    oldest = all_messages[0]
    attention = all_messages[1]
    service._message_update(attention['id'], status='attention', issues=['请补传原件'])
    for record in all_messages[2:]:
        service._message_update(record['id'], status='duplicate')
    state = service.status()
    assert len(state['messages']) == 30 and state['unresolved_count'] == 2 and state['has_more']
    assert {row['id'] for row in state['messages'][:2]} == {oldest['id'], attention['id']}
    assert all(row['status'] == 'duplicate' for row in state['messages'][2:])


def test_second_assignment_cannot_race_started_dispatch(env: Any) -> None:
    service, _, _, project, second, _, results, _ = env
    results.append(message(10, '无项目发票'))
    enable(service)
    run(service)
    key = service.status()['messages'][0]['id']
    service.assign(key, project)
    with pytest.raises(ValueError, match='已开始'):
        service.assign(key, second)
    service._run(False)
    assert service.status()['messages'][0]['project_id'] == project


def test_dispatch_rechecks_disabled_state_before_any_intake_mutation(env: Any) -> None:
    service, store, agent, project, _, _, results, _ = env
    results.append(message(10, '无项目发票'))
    enable(service)
    run(service)
    key = service.status()['messages'][0]['id']
    service.assign(key, project)
    service.configure({'enabled': False})
    service._dispatch_pending(service._message(key)['account_id'], service._config())
    assert IntakeQueue(store).list() == [] and agent.jobs.list(project) == []


def test_explicit_since_change_rewinds_only_active_account_with_digest_dedup(env: Any) -> None:
    service, _, agent, project, _, _, results, calls = env
    results.append(message(10))
    enable(service)
    run(service)
    future = (mail_agent.date.fromisoformat(service.status()['since']) + mail_agent.timedelta(days=1)).isoformat()
    service.configure({'since': future})
    run(service)
    assert calls[-1]['after_uid'] == 0 and calls[-1]['since'].isoformat() == future
    assert len(agent.jobs.list(project)) == 1


def test_real_agent_can_run_imported_pair_to_preview_then_confirm(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, store, agent, project, _, _, results, _ = env
    voucher = pdf('收据\n商家名称：合成工具商店\n商品名称：胶带\n实付：17.00\n商家订单号：MAILORDER0001')
    payment = pdf('微信支付\n付款金额：17.00\n当前状态：支付成功\n商家名称：合成工具商店\n商家订单号：MAILORDER0001')
    remote = message(1, payload=voucher)
    remote['files'].append({'name': 'payment.pdf', 'payload': payment, 'source': 'attachment'})
    results.append(remote)
    from invoice_print_layout import report_print_preview
    monkeypatch.setattr(report_print_preview, 'create_report_print_preview', lambda *args, **kwargs: {
        'pdf': 'synthetic.pdf', 'xlsx': 'synthetic.xlsx', 'total_cents': 1700, 'page_count': 2,
        'preview_pages': [], 'ordered_item_ids': []})
    enable(service)
    run(service)
    job = agent.jobs.list(project)[0]
    done = AgentLoop(agent.jobs).run(job['id'], InvoiceTools(agent.jobs))
    assert done['state'] == 'ready', done['message']
    items = store.get_many(done['item_ids'])
    assert len(items) == 1 and items[0]['amount_cents'] == 1700
    assert not items[0]['verified'] and items[0]['stage'] == 'draft'
    assert len(items[0]['attachments']) == 2
    assert service.status()['messages'][0]['status'] == 'ready'


@pytest.mark.parametrize('body', [
    {'enabled': 'true'}, {'use_model': 1}, {'interval_seconds': 0}, {'interval_seconds': 3601},
    {'since': '2000-01-01'}, {'since': 'not-date'}, {'default_project_id': 'missing'}, {'auth_code': 'never-here'},
])
def test_invalid_settings_rejected_without_connection(env: Any, body: dict[str, Any]) -> None:
    service, _, _, _, _, _, _, calls = env
    with pytest.raises(ValueError):
        service.configure(body)
    assert not calls


def test_status_is_bounded_and_has_no_private_original_paths(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    results[:] = [message(i, '未分配发票') for i in range(1, 36)]
    enable(service)
    run(service)
    state = service.status()
    assert len(state['messages']) == 30 and state['messages'] == state['pending']
    assert '邮件Agent原件' not in json.dumps(state, ensure_ascii=False)


def test_account_changed_during_fetch_does_not_commit_or_dispatch(env: Any) -> None:
    service, _, agent, project, _, settings, results, _ = env
    results.append(message(1))

    def changed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        settings[0] = Mail163Settings('changed@163.com')
        return {'uidvalidity': '100', 'messages': results, 'more': False}

    service._fetcher = changed
    enable(service)
    run(service)
    assert service._messages() == [] and agent.jobs.list(project) == []


def test_async_kick_never_blocks_http_and_close_does_not_half_commit_a_message(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _, _, _, _, _, results, _ = env
    gate = threading.Event()
    entered = threading.Event()
    results.append(message(1, '未分配发票'))

    def delayed(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert gate.wait(3)
        return {'uidvalidity': '100', 'messages': results, 'more': False}

    service._fetcher = delayed
    enable(service)
    monkeypatch.setattr(service, 'kick', lambda: mail_agent.MailAgentService.kick(service))
    before = time.monotonic()
    service.kick()
    assert time.monotonic() - before < .5 and entered.wait(2)
    assert service.status()['collecting'] is True
    gate.set()
    service.close()
    assert not service._thread.is_alive()
    assert len(service._messages()) in {0, 1}
    for record in service._messages():
        saved = record['files'][0]
        assert hashlib.sha256(Path(saved['path']).read_bytes()).hexdigest() == saved['digest']


def test_legacy_waiting_mail_follows_startup_completion_without_new_expenses(env: Any) -> None:
    from invoice_print_layout.agent_jobs import AgentService
    service, store, agent, project, _, _, results, _ = env
    results.append(message(1))
    enable(service)
    run(service)
    original = service.status()['messages'][0]
    assert original['job_id']
    job = agent.jobs.get(original['job_id'])
    agent.jobs.stop(job['id'], 'needs_input', '合成对应关系待确认')
    agent.jobs.update(job['id'], question={'type':'material_relation', 'text':'合成对应关系待确认',
                                         'material_ids':job['material_ids']})
    startup = AgentService(store)
    visible = service.status()['messages'][0]
    assert visible['status'] == 'ready' and visible['job_id'] == original['job_id']
    assert visible['material_ids'] == original['material_ids'] and not store.list_items()
    assert startup.jobs.get(job['id'])['remaining_material_count'] == 1
    startup.close()
