"""Mail QR preparation uses synthetic originals and fake tax HTTP only."""
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout import invoice_claim, mail_search
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from tests.test_batch_invoice_claim import JIANGXI, image_bytes, invoice_bytes, tax_responses
from tests.test_mail_agent import env as env, enable, message, run


def receive_image(env: Any, route: str = 'name', *, mixed: bool = False) -> dict[str, Any]:
    service, store, _, project, _, _, results, _ = env
    remote = message(1, '测试项目发票' if route == 'name' else '普通发票', payload=image_bytes())
    remote['files'][0]['name'] = 'qr.png'
    remote['sent_date'] = '2026-10-12'
    if mixed:
        remote['files'].append({'name': 'ordinary.pdf', 'payload': invoice_bytes(), 'source': 'attachment'})
    if route == 'date':
        projects = ExpenseProjects(store)
        current = next(p for p in projects.list() if p['id'] == project)
        projects.set_period(project, '2026-10-01', '2026-10-31', current['project_revision'])
    results.append(remote)
    enable(service, **({'default_project_id': project} if route == 'default' else {}))
    run(service)
    if route == 'assign':
        unassigned = service.status()['messages'][0]
        assert unassigned['status'] == 'unassigned' and not IntakeQueue(store).list()
        service.assign(unassigned['id'], project)
        service._run(False)
    return service._messages()[0]


def no_analysis(*args: Any, **kwargs: Any) -> Any:
    pytest.fail('Mail QR preparation must not run OCR, PDF analysis or AI')


@pytest.mark.parametrize('route', ['name', 'date', 'default', 'assign'])
def test_mail_qr_replaces_image_in_job_for_every_assignment_path(
        env: Any, monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda payload: JIANGXI)
    monkeypatch.setattr(IntakeQueue, '_analyze_batch_image', no_analysis)
    monkeypatch.setattr(IntakeQueue, '_analyze_batch_pdf', no_analysis)
    content = invoice_bytes()
    calls = tax_responses(monkeypatch, pdf=content)
    record = receive_image(env, route)
    service, store, agent, project, *_ = env
    queue = IntakeQueue(store)
    source = next(row for row in queue.list() if row['name'] == 'qr.png')
    target = queue.get(source['replacement_id'])
    assert source['status'] == source['qr_status'] == 'converted'
    assert Path(source['path']).read_bytes() == image_bytes()
    assert Path(record['files'][0]['path']).read_bytes() == image_bytes()
    assert Path(target['path']).read_bytes() == content
    assert target['source_entry_id'] == source['id'] and target['source_entry_ids'] == [source['id']]
    assert target['project_id'] == project and target['fields'] == {} and target['status'] == 'received'
    job = agent.jobs.get(record['job_id'])
    assert job['material_ids'] == record['material_ids'] == [target['id']]
    assert source['id'] not in job['material_ids'] and len(calls) == 4
    before = queue.list()
    run(service)
    assert len(calls) == 4 and len(agent.jobs.list(project)) == 1 and queue.list() == before
    assert not store.list_items()


@pytest.mark.parametrize('value', ['https://unsupported.example/qr', 'weixin://wxpay/payment', 'not-a-url'])
def test_unsupported_mail_qr_stays_claim_pending_outside_agent(
        env: Any, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda payload: value)
    monkeypatch.setattr(invoice_claim, 'request', no_analysis)
    monkeypatch.setattr(IntakeQueue, '_analyze_batch_image', no_analysis)
    record = receive_image(env)
    service, store, agent, project, *_ = env
    queue = IntakeQueue(store)
    source = queue.list()[0]
    assert source['qr_status'] == 'manual' and source['status'] == 'review'
    assert source['issues'] == ['领票待处理'] and source['fields'] == {}
    assert record['status'] == 'attention' and not record['job_id'] and not record['material_ids']
    assert not agent.jobs.list(project) and not record['dispatch_attempts']
    board = MaterialMatching(store, queue).board(project)
    assert any(card['id'] == source['id'] and card['kind'] == 'qr_claim' for card in board['other'])
    before = queue.get(source['id'])
    run(service)
    assert queue.get(source['id']) == before


def test_image_without_qr_keeps_existing_received_analysis_path(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda payload: '')
    monkeypatch.setattr(invoice_claim, 'claim', no_analysis)
    monkeypatch.setattr(IntakeQueue, '_analyze_batch_image', no_analysis)
    record = receive_image(env)
    _, store, agent, project, *_ = env
    source = IntakeQueue(store).list()[0]
    assert source['status'] == 'received' and source['fields'] == {} and not source.get('qr_status')
    assert agent.jobs.list(project)[0]['material_ids'] == [source['id']] == record['material_ids']
    assert Path(source['path']).read_bytes() == image_bytes()


@pytest.mark.parametrize('mixed', [False, True])
def test_failed_claim_preserves_original_and_does_not_retry_or_block_other_proofs(
        env: Any, monkeypatch: pytest.MonkeyPatch, mixed: bool) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda payload: JIANGXI)
    calls: list[str] = []

    def fail(endpoint: str, *args: Any, **kwargs: Any) -> bytes:
        calls.append(endpoint)
        raise OSError('Synthetic tax endpoint unavailable')

    monkeypatch.setattr(invoice_claim, 'request', fail)
    record = receive_image(env, mixed=mixed)
    service, store, agent, project, *_ = env
    queue = IntakeQueue(store)
    source = next(row for row in queue.list() if row['name'] == 'qr.png')
    assert source['qr_status'] == 'manual' and Path(source['path']).read_bytes() == image_bytes()
    assert record['dispatch_attempts'] == 0
    if mixed:
        job = agent.jobs.get(record['job_id'])
        assert len(job['material_ids']) == 1
        assert queue.get(job['material_ids'][0])['name'] == 'ordinary.pdf'
    else:
        assert record['status'] == 'attention' and not agent.jobs.list(project)
    # A crash/wait before the mail receipt must not re-run a failed QR claim.
    service._message_update(record['id'], status='waiting_agent', job_id='', retry_at=0)
    service._run(False)
    run(service)
    assert calls == ['queryFpjcxxb'] and queue.get(source['id']) == source
    assert len(agent.jobs.list(project)) == int(mixed)
    assert not service._message(record['id'])['dispatch_attempts']


def test_crash_after_qr_conversion_recovers_pdf_without_reclaiming(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda payload: JIANGXI)
    calls = tax_responses(monkeypatch)
    service, store, agent, project, *_ = env
    real_create = agent.jobs.create
    monkeypatch.setattr(agent.jobs, 'create', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('Synthetic interruption')))
    record = receive_image(env)
    assert record['status'] == 'waiting_agent' and record['dispatch_attempts'] == 1
    queue = IntakeQueue(store)
    source = next(row for row in queue.list() if row['name'] == 'qr.png')
    assert source['status'] == 'converted' and len(calls) == 4
    monkeypatch.setattr(agent.jobs, 'create', real_create)
    service._message_update(record['id'], retry_at=0)
    service._run(False)
    assert agent.jobs.list(project)[0]['material_ids'] == [source['replacement_id']]
    assert len(queue.list()) == 2 and len(calls) == 4


def test_claimed_pdf_in_other_project_remains_manual_without_reassigning(
        env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, store, agent, project, second, *_ = env
    queue = IntakeQueue(store)
    content = invoice_bytes()
    prior = queue.receive('existing.pdf', content, project_id=second)
    monkeypatch.setattr(mail_search, 'qr_value', lambda payload: JIANGXI)
    tax_responses(monkeypatch, pdf=content)
    record = receive_image(env)
    source = next(row for row in queue.list() if row['name'] == 'qr.png')
    assert source['qr_status'] == 'manual' and '其他项目' in source['result']
    assert queue.get(prior['id']) == prior and not agent.jobs.list(project)
    assert record['status'] == 'attention' and not record['dispatch_attempts']
