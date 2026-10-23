"""Explicit legacy-question refresh preserves normal answer and task boundaries."""
from __future__ import annotations

import copy
import time
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from invoice_print_layout.agent_tools import InvoiceTools, TOOL_GUIDE
from tests.test_invoice_agent import environment  # noqa: F401
from tests.test_invoice_agent_confirmations import question, persisted_confirmation_state
from tests.test_invoice_agent_relations import relation_env, model_answer  # noqa: F401
from tests.test_invoice_agent_combinations import entry, invoice_text, register


def legacy_partial(env: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    jobs, _, _ = env
    waiting, fresh = question(env)
    fresh['choices'][0]['proposal']['type'] = 'complete_partial_expense'
    waiting = jobs.update(waiting['id'], item_ids=['original-draft'],
                          question={'text': '仍有付款需要对应', 'choices': [], 'snapshot': 'old-build'},
                          preview={'total_cents': 1700, 'pdf': 'partial.pdf'})
    return waiting, fresh


def test_refresh_is_only_a_question_then_normal_answer(environment: Any) -> None:
    jobs, tools, _ = environment
    waiting, fresh = legacy_partial(environment)
    jobs.cache(waiting['id'], 'old-investigation', {'decision': 'reject'})
    before = copy.deepcopy(waiting)
    refreshed = jobs.refresh_question(waiting['id'], waiting['updated_at'], lambda current: fresh)
    assert refreshed['state'] == 'needs_input' and refreshed['question'] == fresh
    for key in ('models', 'tools', 'elapsed', 'limits', 'item_ids', 'preview', 'queue_revision', 'confirmations'):
        assert refreshed[key] == before[key]
    assert refreshed['approval'] is None
    assert jobs.cached(waiting['id'], 'old-investigation') == {'decision': 'reject'}
    assert not any(c['actor'] == 'user' for c in refreshed['confirmations'])
    resumed = jobs.answer(waiting['id'], fresh['choices'][0]['id'], fresh['id'])
    assert resumed['state'] == 'queued' and resumed['confirmations'][0]['actor'] == 'user'
    assert tools.next_action(resumed) == {'tool': 'complete_partial_expense', 'arguments': {}}
    assert [resumed[key] for key in ('models', 'tools', 'elapsed')] == [before[key] for key in ('models', 'tools', 'elapsed')]
    assert 'complete_partial_expense' not in TOOL_GUIDE


@pytest.mark.parametrize('mutation', ['stale', 'running', 'run_started', 'answerable', 'empty_items', 'approval',
                                      'models', 'tools', 'elapsed', 'other_task'])
def test_refresh_rejects_before_factory_without_business_change(environment: Any, mutation: str) -> None:
    jobs, tools, _ = environment
    waiting, fresh = legacy_partial(environment)
    changes: dict[str, Any] = {}
    expected = waiting['updated_at']
    if mutation == 'stale':
        expected -= 1
    elif mutation == 'running':
        changes['state'] = 'running'
    elif mutation == 'run_started':
        changes['run_started'] = 1
    elif mutation == 'answerable':
        changes['question'] = fresh
    elif mutation == 'empty_items':
        changes['item_ids'] = []
    elif mutation == 'approval':
        changes['approval'] = {'choice': 'old'}
    elif mutation in {'models', 'tools', 'elapsed'}:
        changes[mutation] = waiting['limits']['seconds' if mutation == 'elapsed' else mutation]
    elif mutation == 'other_task':
        with tools.store.connect() as db:
            db.execute('INSERT INTO invoice_agent_jobs VALUES (?,?,?,?)',
                       ('other', waiting['project_id'], 'running', '{}'))
    if changes:
        waiting = jobs.update(waiting['id'], **changes)
        expected = waiting['updated_at']
    before = persisted_confirmation_state(jobs, waiting['id'])
    def forbidden(current: dict[str, Any]) -> dict[str, Any]:
        pytest.fail('Invalid refresh must not inspect or synthesize a question')
    with pytest.raises(ValueError):
        jobs.refresh_question(waiting['id'], expected, forbidden)
    assert persisted_confirmation_state(jobs, waiting['id']) == before


def test_refresh_factory_source_failure_does_not_change_question(environment: Any) -> None:
    jobs, _, _ = environment
    waiting, _ = legacy_partial(environment)
    before = persisted_confirmation_state(jobs, waiting['id'])
    def changed_source(current: dict[str, Any]) -> dict[str, Any]:
        raise ValueError('原件或费用已变化')
    with pytest.raises(ValueError, match='原件或费用'):
        jobs.refresh_question(waiting['id'], waiting['updated_at'], changed_source)
    assert persisted_confirmation_state(jobs, waiting['id']) == before


def test_http_refresh_is_guarded_and_does_not_kick_or_answer(environment: Any, monkeypatch: Any) -> None:
    from tests.test_invoice_assistant import invoice_server
    from invoice_print_layout.agent_jobs import AgentService
    jobs, tools, _ = environment
    waiting, fresh = legacy_partial(environment)
    calls: list[str] = []
    monkeypatch.setattr(InvoiceTools, 'refresh_question', lambda self, job: fresh)
    monkeypatch.setattr(AgentService, 'kick', lambda self: calls.append(self.jobs.get(waiting['id'])['state']))
    with invoice_server(tools.store.workspace) as client:
        body = {'id': waiting['id'], 'expected_updated_at': waiting['updated_at']}
        assert client.json('POST', '/api/agent/refresh-question', body, authenticated=False)[0] == 403
        assert client.json('POST', '/api/agent/refresh-question', {'id': waiting['id']})[0] == 400
        assert client.json('POST', '/api/agent/refresh-question', {**body, 'expected_updated_at': 0})[0] == 400
        before_kicks = len(calls)
        code, result = client.json('POST', '/api/agent/refresh-question', body)
        assert code == 200 and result['job']['question'] == fresh
        assert result['job']['state'] == 'needs_input' and result['job']['confirmations'] == []
        assert set(calls[before_kicks:]) <= {'needs_input'}
        assert client.json('POST', '/api/agent/refresh-question', body)[0] == 400
        code, result = client.json('POST', '/api/agent/answer', {
            'id': waiting['id'], 'choice': fresh['choices'][0]['id'], 'question_id': fresh['id']})
        assert code == 200 and result['job']['state'] == 'queued' and 'queued' in calls[before_kicks:]
        assert result['job']['approval']['choice_data']['proposal']['type'] == 'complete_partial_expense'


@pytest.mark.parametrize('decision', ['reject', 'needs_fact'])
def test_model_cannot_preempt_complete_hotel_candidate_with_invoice_subset(
        relation_env: Any, monkeypatch: Any, decision: str) -> None:
    jobs, tools, project = relation_env
    invoice = entry(relation_env, 'invoice', '300', invoice_text('住宿服务') + '\n销售方名称：青山春雨宾馆',
                    category='酒店', shop='青山春雨宾馆')
    payments = [entry(relation_env, 'payment', str(amount), '支付成功\n商品说明 车站春雨宾馆', shop='合成旅行平台')
                for amount in [100, 200]]
    monkeypatch.setattr(tools.relations.ai, '_request', lambda rules, payload, **kw: model_answer(payload, status=decision))
    job = jobs.create(project, [e['id'] for e in [invoice, *payments]], 'hotel-model-' + decision, use_model=True)
    waiting = AgentLoop(jobs).run(job['id'], tools)
    assert waiting['state'] == 'needs_input' and waiting['models'] == 1
    assert not tools.store.list_items() and waiting['item_ids'] == []
    assert len([c for c in waiting['question']['choices'] if c['action'] == 'confirm_relation']) == 1
    q = waiting['question']
    resumed = jobs.answer(job['id'], q['choices'][0]['id'], q['id'])
    done = AgentLoop(jobs).run(resumed['id'], tools)
    assert done['state'] == 'ready' and done['models'] == waiting['models']
    assert done['preview']['total_cents'] == 30000
    assert len(tools.store.list_items()[0]['attachments']) == 3


def test_complete_hotel_invoice_without_payment_stays_automatic(relation_env: Any, monkeypatch: Any) -> None:
    jobs, tools, project = relation_env
    invoice = entry(relation_env, 'invoice', '300', invoice_text('住宿服务'), category='酒店', shop='合成春雨宾馆')
    monkeypatch.setattr(tools.relations.ai, '_request', lambda *args, **kw: pytest.fail('Invoice alone stays local'))
    job = jobs.create(project, [invoice['id']], 'hotel-without-payment', use_model=True)
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    assert done['preview']['total_cents'] == 30000 and len(tools.store.list_items()[0]['attachments']) == 1


def test_real_helper_http_refresh_then_answer_completes_same_job(environment: Any, monkeypatch: Any) -> None:
    """The handler calls the actual helper under its write transaction, not a fake factory."""
    from tests.test_invoice_assistant import invoice_server
    from invoice_print_layout.agent_jobs import AgentService
    jobs, tools, project = environment
    invoice = entry(environment, 'invoice', '300', invoice_text('住宿服务') + '\n销售方名称：青山春雨宾馆',
                    category='酒店', shop='青山春雨宾馆')
    payments = [entry(environment, 'payment', str(amount), '支付成功\n商品说明 车站春雨宾馆', shop='合成旅行平台')
                for amount in [100, 100, 100]]
    job = jobs.create(project, [e['id'] for e in [invoice, *payments]], 'legacy-hotel-http')
    job = jobs.update(job['id'], state='running', run_started=time.time())
    proposal = next(p for p in tools._scan(job)['proposals'] if not p['payment_ids'])
    item = register(environment, job, proposal)
    jobs.update(job['id'], item_ids=[item['id']], models=1, tools=11,
                question={'text': '旧版仍缺付款对应事实', 'snapshot': 'old-build', 'choices': []})
    waiting = jobs.stop(job['id'], 'needs_input', '旧版部分预览')
    budget = {k: waiting[k] for k in ('models', 'tools', 'elapsed', 'limits')}
    monkeypatch.setattr(AgentService, 'kick', lambda self: None)
    with invoice_server(tools.store.workspace) as client:
        started = time.monotonic()
        code, result = client.json('POST', '/api/agent/refresh-question', {
            'id': waiting['id'], 'expected_updated_at': waiting['updated_at']})
        assert code == 200, result
        assert time.monotonic() - started < 10, 'Read-only helper must not wait on a nested business write lock'
        refreshed = result['job']
        assert refreshed['state'] == 'needs_input' and refreshed['confirmations'] == []
        assert {k: refreshed[k] for k in budget} == budget
        q = refreshed['question']
        choice = q['choices'][0]
        assert choice['proposal']['type'] == 'complete_partial_expense'
        assert len(q['material_refs']) == 4 and len(choice['payment_ids']) == 3
        assert tools.store.get(item['id'])['attachments'] == item['attachments']
        code, result = client.json('POST', '/api/agent/answer', {
            'id': job['id'], 'choice': choice['id'], 'question_id': q['id']})
        assert code == 200 and result['job']['state'] == 'queued'
        assert {k: result['job'][k] for k in budget} == budget
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready', (done['message'], done['tools'], done['last_results'])
    assert done['item_ids'] == [item['id']] and done['preview']['total_cents'] == 30000
    assert done['models'] == budget['models'] and done['tools'] > budget['tools']
    finished = tools.store.get(item['id'])
    assert len(tools.store.list_items()) == 1 and len(finished['attachments']) == 4
    assert not finished['verified'] and finished['stage'] == 'draft'
    assert finished['agent_relation']['confirmation_id'] == q['id']
