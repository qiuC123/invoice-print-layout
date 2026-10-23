"""Large projects keep scoped legal paths and all out-of-scope competitors."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import random
import time
from typing import Any

import pytest

from invoice_print_layout.agent_candidate_groups import EnumerationBudget, amount_groups
from invoice_print_layout.agent_combinations import extended_candidates, missing_questions, selection
from invoice_print_layout.agent_evidence import material_facts
from invoice_print_layout.agent_jobs import DEFAULT_LIMITS, Jobs, encoded
from invoice_print_layout.agent_relations import RelationTools, candidates
from invoice_print_layout.workbench import ExpenseStore, amount_cents


def original(ident: str, role: str, amount: str, *, goods: str = '住宿服务',
             shop: str = '青山春雨宾馆', category: str = '酒店') -> dict[str, Any]:
    if role == 'invoice':
        text = ('电子发票（普通发票）\n发票号码：' + str(10**19 + sum(map(ord, ident))) +
                '\n价税合计（小写）￥' + amount + '\n*服务*' + goods + '\n销售方名称：' + shop)
    elif role == 'payment':
        text = '支付成功\n商品说明：车站春雨宾馆\n付款金额：' + amount
    elif role in {'purchase', 'order'}:
        text = '购买明细\n商品名称：' + goods + '\n订单已送达\n实付' + amount
    else:
        text = '滴滴出行-行程单\n共1笔行程，合计' + amount + '元'
    return {'id': ident, 'role': role, 'text': text, 'revision': 1,
            'fields': {'amount': amount, 'merchant': shop, 'shop_name': shop, 'category': category,
                       **({'payment_status': 'succeeded'} if role == 'payment' else {})}}


def state(entry: dict[str, Any]) -> dict[str, Any]:
    return {'identity': entry['fields']['merchant'], 'descriptions': [], 'name_conflict': False}


def all_facts(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e['id']: material_facts(e) for e in entries}


def hotel() -> list[dict[str, Any]]:
    return [original('hotel', 'invoice', '1353'), *[
        original('payment-' + str(index), 'payment', str(value)) for index, value in enumerate([400, 300, 500, 153])]]


def unrelated(count: int) -> list[dict[str, Any]]:
    return [original('unrelated-' + str(index), 'invoice', str(2000 + index),
                     goods='螺丝', shop='独立五金公司' + str(index), category='材料采购') for index in range(count)]


@pytest.mark.parametrize('count', [21, 41])
def test_large_unrelated_analysed_pool_keeps_target_local_and_semantic_paths(count: int) -> None:
    target = hotel()
    rows = [*unrelated(count), *target]
    states = {e['id']: state(e) for e in rows}
    scope = {e['id'] for e in target}
    local = extended_candidates(rows, states, scope=scope)
    semantic = candidates(rows, states, all_facts(rows), scope=scope)
    assert len(local) == len(semantic) == 1
    assert selection(local[0]) == selection(semantic[0]) == scope
    assert local[0]['amount_cents'] == 135300 and semantic[0]['strength'] == 'supported'
    assert not semantic[0]['competing']


def test_large_pool_without_scope_still_uses_per_anchor_amount_windows() -> None:
    rows = [*unrelated(41), *hotel()]
    states = {e['id']: state(e) for e in rows}
    assert len(extended_candidates(rows, states)) == 1
    assert len(candidates(rows, states, all_facts(rows))) == 1


@pytest.mark.parametrize('flag,value', [('goods_issues', ['商品行超过20条，尚未完整提取']),
                                       ('source_text_truncated', True), ('goods_text_truncated', True)])
@pytest.mark.parametrize('material_id', ['hotel', 'payment-0'])
def test_partial_goods_or_context_never_establish_semantic_candidates(
        flag: str, value: Any, material_id: str) -> None:
    rows = hotel()
    states = {e['id']:state(e) for e in rows}
    facts = all_facts(rows)
    assert len(candidates(rows, states, facts)) == 1
    facts[material_id][flag] = value
    assert candidates(rows, states, facts) == []
    # A typed/grounded local path uses its existing complete-proof rules and
    # does not need to speculate from the truncated semantic description.
    assert len(extended_candidates(rows, states, scope={e['id'] for e in rows})) == 1


@pytest.mark.parametrize('competitor', ['same_total_payment', 'partial_payment', 'unknown_payment',
                                       'unknown_state_payment', 'same_total_invoice', 'partial_invoice'])
def test_outside_scope_competition_is_not_lost_even_if_analysed(competitor: str) -> None:
    target = hotel()
    if 'invoice' in competitor:
        other = original('outside-invoice', 'invoice', '1353' if 'same_total' in competitor else '400')
    else:
        amount = '1353' if competitor == 'same_total_payment' else '400'
        other = original('outside-payment', 'payment', amount)
        if competitor == 'unknown_payment':
            other['fields']['amount'] = ''
        if competitor == 'unknown_state_payment':
            other['fields']['payment_status'] = ''
            other['text'] = '付款记录，状态未显示'
    rows = [*unrelated(41), *target, other]
    states = {e['id']: state(e) for e in rows if e is not other or competitor != 'unknown_state_payment'}
    scope = {e['id'] for e in target}
    assert extended_candidates(rows, states, scope=scope) == []
    assert candidates(rows, states, all_facts(rows), scope=scope) == []


@pytest.mark.parametrize('in_scope', [False, True])
@pytest.mark.parametrize('amount', ['1353', ''])
def test_blocked_invoice_is_still_a_competitor_inside_and_outside_scope(in_scope: bool, amount: str) -> None:
    target = hotel()
    blocked = original('bad-invoice', 'invoice', amount, goods='住宿服务')
    rows = [*unrelated(41), *target, blocked]
    states = {e['id']:state(e) for e in rows if e is not blocked}
    scope = {e['id'] for e in target} | ({blocked['id']} if in_scope else set())
    assert extended_candidates(rows, states, scope=scope) == []
    assert candidates(rows, states, all_facts(rows), scope=scope) == []


@pytest.mark.parametrize('status', ['failed', 'refunded', 'unpaid', 'cancelled', 'canceled'])
def test_proven_failed_outside_payments_are_not_competitors(status: str) -> None:
    target = hotel()
    other = original('failed', 'payment', '400')
    other['fields']['payment_status'] = status
    rows = [*unrelated(41), *target, other]
    states = {e['id']: state(e) for e in rows if e is not other}
    scope = {e['id'] for e in target}
    assert len(extended_candidates(rows, states, scope=scope)) == 1
    assert len(candidates(rows, states, all_facts(rows), scope=scope)) == 1


def test_unknown_state_but_known_oversized_amount_has_arithmetic_exclusion() -> None:
    target = hotel()
    other = original('oversized-payment', 'payment', '5000')
    other['fields']['payment_status'] = ''
    rows = [*unrelated(41), *target, other]
    states = {e['id']: state(e) for e in rows if e is not other}
    scope = {e['id'] for e in target}
    assert len(extended_candidates(rows, states, scope=scope)) == 1
    assert len(candidates(rows, states, all_facts(rows), scope=scope)) == 1


def test_scope_cannot_hide_a_competing_taxi_trip() -> None:
    invoice = original('ride', 'invoice', '30', goods='客运服务', category='打车')
    invoice['text'] += '\n滴滴出行\n出租车'
    trip = original('trip', 'trip', '30', category='打车')
    competitor = original('outside-trip', 'trip', '30', category='打车')
    rows = [*unrelated(41), invoice, trip, competitor]
    assert not extended_candidates(rows, {e['id']: state(e) for e in rows}, scope={'ride', 'trip'})


def test_nine_positive_part_payments_are_not_rejected_by_the_old_eight_item_cap() -> None:
    target = [original('hotel', 'invoice', '450'), *[
        original('part-' + str(index), 'payment', '50') for index in range(9)]]
    rows = [*unrelated(41), *target]
    states = {e['id']: state(e) for e in rows}
    scope = {e['id'] for e in target}
    local = extended_candidates(rows, states, scope=scope)
    semantic = candidates(rows, states, all_facts(rows), scope=scope)
    assert len(local) == len(semantic) == 1
    assert len(local[0]['payment_ids']) == len(semantic[0]['payment_ids']) == 9


def test_too_many_relevant_candidates_fail_closed_instead_of_taking_first_twenty() -> None:
    rows = [original('hotel', 'invoice', '10'), *[
        original('part-' + str(index), 'payment', '1') for index in range(21)]]
    for seed in [1, 9]:
        random.Random(seed).shuffle(rows)
        states = {e['id']: state(e) for e in rows}
        assert extended_candidates(rows, states) == []
        assert candidates(rows, states, all_facts(rows)) == []


def test_subset_budget_reports_exhaustion_and_never_returns_partial_unique_result() -> None:
    rows = [original(str(index), 'payment', '1') for index in range(20)]
    budget = EnumerationBudget(remaining=100)
    assert amount_groups(rows, 1000, lambda e: amount_cents(e['fields']['amount']), budget) is None
    assert budget.exhausted and budget.remaining < 0


def test_competition_audit_budget_cannot_return_an_earlier_candidate_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import agent_combinations
    rows = [original('invoice-a', 'invoice', '17', shop='独立宾馆甲'),
            original('payment-a', 'payment', '17', shop='独立宾馆甲'),
            original('invoice-b', 'invoice', '29', shop='独立宾馆乙'),
            original('payment-b', 'payment', '29', shop='独立宾馆乙')]
    budget = EnumerationBudget(remaining=5)
    monkeypatch.setattr(agent_combinations, 'EnumerationBudget', lambda: budget)
    assert extended_candidates(rows, {e['id']:state(e) for e in rows}) == []
    assert budget.exhausted


def test_typed_order_contradiction_is_still_rejected_after_windowing() -> None:
    invoice = original('invoice', 'invoice', '17', goods='鼠标', category='材料采购')
    order = original('order', 'purchase', '17', goods='鼠标', category='材料采购')
    invoice['transaction_identifiers'] = [{'kind': 'merchant_order', 'namespace': '合成商店', 'value': 'ORDER1000'}]
    order['transaction_identifiers'] = [{'kind': 'merchant_order', 'namespace': '合成商店', 'value': 'ORDER2000'}]
    rows = [*unrelated(41), invoice, order]
    states = {e['id']: state(e) for e in rows}
    assert not extended_candidates(rows, states, scope={'invoice', 'order'})
    assert not candidates(rows, states, all_facts(rows), scope={'invoice', 'order'})


def running_job(store: ExpenseStore, ids: list[str]) -> tuple[Jobs, dict[str, Any]]:
    jobs = Jobs(store)
    job = {'id': 'task', 'project_id': 'project', 'material_ids': ids, 'state': 'running',
           'use_model': True, 'models': 0, 'tools': 0, 'limits': dict(DEFAULT_LIMITS),
           'elapsed': 0.0, 'run_started': time.time(), 'created_at': time.time(), 'updated_at': time.time()}
    with store.connect() as db:
        db.execute('INSERT INTO invoice_agent_jobs VALUES (?,?,?,?)', ('task', 'project', 'running', encoded(job)))
    return jobs, job


def test_fact_batch_timer_records_one_observation_and_cache_diagnostics(tmp_path: Path) -> None:
    rows = hotel()
    jobs, job = running_job(ExpenseStore(tmp_path), [e['id'] for e in rows])
    tools = RelationTools(jobs)
    first = tools.facts(rows, job_id=job['id'])
    second = tools.facts(rows, job_id=job['id'])
    assert first == second and tools.last_fact_cache['hits'] == len(rows)
    summary = jobs.get(job['id'])['timings']['stages']['facts_parse']
    assert summary['count'] == 2 and summary['cache_hits'] == 1 and summary['cache_misses'] == 1


def test_needs_fact_and_competing_rows_do_not_reserve_model_or_make_requests(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = hotel()
    jobs, job = running_job(ExpenseStore(tmp_path), [e['id'] for e in rows])
    tools = RelationTools(jobs)
    scan = tools.scan(job, 'snapshot', rows, {e['id']: state(e) for e in rows})
    monkeypatch.setattr(tools.ai, '_request', lambda *a, **k: pytest.fail('no useful semantic decision'))
    for change in [{'strength': 'needs_fact'}, {'competing': True}]:
        blocked = copy.deepcopy(scan)
        blocked['candidates'][0].update(change)
        result = tools.investigate(job, 'snapshot', blocked)
        assert result['decisions'] == {} and jobs.get(job['id'])['models'] == 0


def test_oversized_model_payload_is_rejected_before_model_reservation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = hotel()
    jobs, job = running_job(ExpenseStore(tmp_path), [e['id'] for e in rows])
    tools = RelationTools(jobs)
    scan = tools.scan(job, 'snapshot', rows, {e['id']: state(e) for e in rows})
    scan['facts']['hotel']['text'] = 'a' * 55001
    monkeypatch.setattr(tools.ai, '_request', lambda *a, **k: pytest.fail('oversized payload'))
    with pytest.raises(ValueError, match='材料过多'):
        tools.investigate(job, 'snapshot', scan)
    assert jobs.get(job['id'])['models'] == 0


@pytest.mark.parametrize('flag,value', [('goods_issues', ['商品行超过20条，尚未完整提取']),
                                       ('source_text_truncated', True), ('goods_text_truncated', True)])
def test_stale_investigation_candidate_with_partial_facts_never_calls_model(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str, value: Any) -> None:
    rows = hotel()
    jobs, job = running_job(ExpenseStore(tmp_path), [e['id'] for e in rows])
    tools = RelationTools(jobs)
    scan = tools.scan(job, 'snapshot', rows, {e['id']:state(e) for e in rows})
    assert scan['candidates']
    scan['facts']['hotel'][flag] = value
    monkeypatch.setattr(tools.ai, '_request', lambda *a, **k: pytest.fail('partial facts cannot support inference'))
    assert tools.investigate(job, 'snapshot', scan)['decisions'] == {}
    assert jobs.get(job['id'])['models'] == 0


def test_only_actionable_sources_reach_model_and_network_stage_is_measured(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = hotel()
    jobs, job = running_job(ExpenseStore(tmp_path), [e['id'] for e in rows])
    tools = RelationTools(jobs)
    scan = tools.scan(job, 'snapshot', rows, {e['id']: state(e) for e in rows})
    ignored = copy.deepcopy(scan['candidates'][0])
    ignored.update(id='not-investigable', strength='needs_fact')
    scan['candidates'].append(ignored)
    def request(rules: str, payload: str, **kwargs: Any) -> dict[str, Any]:
        content = json.loads(payload)
        assert len(content['candidates']) == 1 and content['candidates'][0]['id'] != 'not-investigable'
        return {'decisions': []}
    monkeypatch.setattr(tools.ai, '_request', request)
    monkeypatch.setattr(tools.ai, 'public_status', lambda: {'model': 'test'})
    tools.investigate(job, 'snapshot', scan)
    current = jobs.get(job['id'])
    assert current['models'] == 1
    assert current['timings']['stages']['relation_model']['model_calls'] == 1


@pytest.mark.parametrize('role,goods', [('trip', ''), ('detail', ''), ('payment', ''), ('purchase', '鼠标'),
                                      ('invoice', '客运服务\n滴滴出行\n出租车'), ('invoice', '超市服务')])
def test_missing_original_flags_require_source_without_model_interpretation(role: str, goods: str) -> None:
    entry = original('one', role, '17', goods=goods)
    questions = missing_questions([entry])
    assert questions and questions[0]['requires_source'] is True


def test_relation_question_and_generic_fallback_do_not_claim_missing_source() -> None:
    rows = [original('invoice', 'invoice', '17', goods='饮用水', category='餐饮'),
            original('payment', 'payment', '17')]
    questions = missing_questions(rows)
    assert all(not row.get('requires_source') for row in questions)
    fallback = missing_questions([original('unknown', 'other', '17')])
    assert not fallback[0].get('requires_source')
