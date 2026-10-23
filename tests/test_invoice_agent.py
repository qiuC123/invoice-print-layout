"""Synthetic originals exercise autonomous actions and every forced stop boundary."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import time
import subprocess
import sys
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.agent_jobs import AgentLoop, AgentService, Jobs
from invoice_print_layout.agent_tools import InvoiceTools, ModelPlanner
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore


def proof(text: str) -> bytes:
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for n, line in enumerate(text.splitlines()):
            page.insert_text((35, 45 + n * 24), line, fontname='china-s', fontsize=12)
        return pdf.tobytes()


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Jobs, InvoiceTools, str]:
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic Agent Project')['id']
    jobs = Jobs(store)
    tools = InvoiceTools(jobs)
    from invoice_print_layout import report_print_preview
    def preview(store: ExpenseStore, ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
        items = store.get_many(ids)
        assert all(not i['verified'] and i['stage'] == 'draft' for i in items)
        return {'pdf': 'synthetic-preview.pdf', 'xlsx': 'synthetic-preview.xlsx',
                'total_cents': sum(i['amount_cents'] for i in items), 'page_count': 2,
                'preview_pages': [], 'ordered_item_ids': ids}
    monkeypatch.setattr(report_print_preview, 'create_report_print_preview', preview)
    return jobs, tools, project


@pytest.fixture
def planner_environment(environment: tuple[Jobs, InvoiceTools, str],
                        monkeypatch: pytest.MonkeyPatch) -> tuple[Jobs, InvoiceTools, str]:
    """Exercise loop/model contracts independently of script routing policy.

    Missing payment facts now go straight to a scripted question.  These tests
    still need actual bounded planner calls, permission checks and cache stalls.
    Preserve the historical initial real inspection/tool slot, then deliberately
    select the model branch rather than weakening any original budget oracle.
    """
    _, tools, _ = environment
    monkeypatch.setattr(tools, 'next_action', lambda job:
                        {'tool': 'inspect_materials', 'arguments': {}} if job['tools'] == 0 else None)
    return environment


def material(tools: InvoiceTools, project: str, role: str, amount: str, marker: str,
             *, text: str = '', received: bool = False, manual: bool = False) -> dict[str, Any]:
    source = text or ('收据\n' if role == 'receipt' else '微信支付\n支付成功\n') + (
        '商家名称：合成工具商店\n金额：' + amount + '\n材料标识：' + marker)
    if role in {'receipt', 'purchase', 'order', 'invoice'} and '商品名称' not in source:
        source += '\n商品名称：胶带'
    entry = tools.intake.receive(marker + '.pdf', proof(source), project_id=project)
    if received:
        return entry
    entry.update(status='review', role=role, revision=1, text=source,
                 fields={'title': marker, 'merchant': '合成工具商店', 'shop_name': '合成工具商店',
                         'amount': amount, 'category': '其他',
                         **({'payment_status': 'succeeded'} if role == 'payment' else {})})
    if manual:
        entry['manual_fields'] = ['amount']
    with tools.store.connect() as db:
        tools.matching._write_queue(db, entry)
    return entry


def start(env: tuple[Jobs, InvoiceTools, str], entries: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    jobs, _, project = env
    return jobs.create(project, [e['id'] for e in entries], 'synthetic-' + str(time.time_ns()), **kwargs)


class Scripted:
    def __init__(self, *actions: dict[str, Any]):
        self.actions = list(actions)
        self.calls = 0

    def choose(self, context: dict[str, Any]) -> dict[str, Any]:
        index = min(self.calls, len(self.actions) - 1)
        self.calls += 1
        return copy.deepcopy(self.actions[index])


def call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {'tool': tool, 'arguments': arguments}


def test_unique_proofs_automatically_create_once_and_preview_without_review(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'order'),
               material(tools, project, 'payment', '17', 'payment')]
    originals = {e['path']: Path(e['path']).read_bytes() for e in entries}
    job = start(environment, entries)
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    assert done['preview']['total_cents'] == 1700 and len(done['item_ids']) == 1
    item = tools.store.get(done['item_ids'][0])
    assert item['amount_cents'] == 1700 and not item['verified'] and item['stage'] == 'draft'
    assert len(item['attachments']) == 2 and len(tools.store.list_items()) == 1
    assert AgentLoop(jobs).run(job['id'], tools) == done
    assert all(Path(p).read_bytes() == b for p, b in originals.items())


def test_complex_discount_is_investigated_and_repaired_by_tools(environment: Any) -> None:
    jobs, tools, project = environment
    order = material(tools, project, 'receipt', '110', 'discount order', text=
                     '收据\n商家名称：合成工具商店\n商品金额110.00\n包装费0.00\n配送费0.00\n优惠10.00\n实付100.00')
    payment = material(tools, project, 'payment', '100', 'discount payment')
    job = start(environment, [order, payment], use_model=True)
    planner = Scripted(call('read_material', id=order['id']), call('reconcile_amount', id=order['id']))
    done = AgentLoop(jobs).run(job['id'], tools, planner)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 10000
    assert planner.calls == done['models'] == 2
    changed = tools.intake.get(order['id'])
    assert changed['fields']['amount'] == '100.00'
    assert changed['agent_amount_evidence'][0]['previous_amount'] == '110'
    assert hashlib.sha256(Path(order['path']).read_bytes()).hexdigest() == order['digest']


@pytest.mark.parametrize('manual,text,role', [
    (True, '商品金额110.00\n优惠10.00\n实付100.00', 'receipt'),
    (False, '商品金额110.00\n优惠9.00\n实付100.00', 'receipt'),
    (False, '商品金额110.00\n实付100.00', 'receipt'),
    (False, '商品金额110.00\n优惠10.00\n实付100.00\n退款成功', 'receipt'),
    (False, '商品金额110.00\n优惠10.00\n实付100.00', 'invoice'),
])
def test_model_cannot_invent_discount_change_manual_amount_or_invoice_face(
        environment: Any, manual: bool, text: str, role: str) -> None:
    jobs, tools, project = environment
    order = material(tools, project, role, '110', 'protected', text=text, manual=manual)
    payment = material(tools, project, 'payment', '100', 'protected payment')
    before = copy.deepcopy(tools.intake.get(order['id']))
    job = start(environment, [order, payment], use_model=True)
    done = AgentLoop(jobs).run(job['id'], tools, Scripted(call('reconcile_amount', id=order['id'])))
    assert done['state'] == 'stopped' and not tools.store.list_items()
    assert tools.intake.get(order['id']) == before


def test_same_price_competition_asks_once_and_answer_does_not_reset_budget(environment: Any) -> None:
    jobs, tools, project = environment
    order = material(tools, project, 'receipt', '17', 'ambiguous order')
    payments = [material(tools, project, 'payment', '17', 'ambiguous ' + str(n)) for n in range(2)]
    job = start(environment, [order, *payments])
    waiting = AgentLoop(jobs).run(job['id'], tools)
    assert waiting['state'] == 'needs_input' and not tools.store.list_items()
    assert len(waiting['question']['choices']) == 2
    choice = waiting['question']['choices'][0]
    resumed = jobs.answer(job['id'], choice['id'])
    assert resumed['tools'] == waiting['tools'] and resumed['elapsed'] >= waiting['elapsed']
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['preview']['total_cents'] == 1700 and len(tools.store.list_items()) == 1
    assert not tools.store.list_items()[0]['verified']


def test_no_progress_repeated_read_stops_and_survives_reopen(planner_environment: Any) -> None:
    jobs, tools, project = environment = planner_environment
    order = material(tools, project, 'receipt', '17', 'unmatched')
    job = start(environment, [order], use_model=True)
    planner = Scripted(call('read_material', id=order['id']))
    done = AgentLoop(jobs).run(job['id'], tools, planner)
    assert done['state'] == 'stopped' and '新证据' in done['message']
    assert done['models'] == 3 and done['tools'] == 4
    assert Jobs(tools.store).get(job['id'])['models'] == 3
    assert not tools.store.list_items()


def test_missing_payment_fact_uses_scripted_question_without_model(environment: Any) -> None:
    jobs, tools, project = environment
    order = material(tools, project, 'receipt', '17', 'missing-payment-script-first')
    planner = Scripted(call('shell', command='must-never-be-called'))
    done = AgentLoop(jobs).run(start(environment, [order], use_model=True)['id'], tools, planner)
    assert done['state'] == 'needs_input' and done['question']['text']
    assert planner.calls == done['models'] == 0
    assert not done['item_ids'] and not tools.store.list_items()


def test_call_budget_stops_even_when_distinct_evidence_keeps_arriving(planner_environment: Any) -> None:
    jobs, tools, project = environment = planner_environment
    rows = [material(tools, project, 'receipt', '17', 'unmatched-' + str(n)) for n in range(4)]
    job = start(environment, rows, use_model=True, limits={'models': 2, 'tools': 10, 'seconds': 30, 'stalls': 2})
    done = AgentLoop(jobs).run(job['id'], tools, Scripted(*(call('read_material', id=e['id']) for e in rows)))
    assert done['state'] == 'stopped' and done['models'] == 2


@pytest.mark.parametrize('action,terminal', [
    (call('shell', command='arbitrary'), 'failed'), ({'tool': 'read_material'}, 'failed'),
    (call('read_material', id='outside'), 'stopped'), (call('create_draft', proposal_id='guessed'), 'failed')])
def test_invalid_tools_and_unvalidated_writes_fail_closed(planner_environment: Any, action: dict[str, Any], terminal: str) -> None:
    jobs, tools, project = environment = planner_environment
    order = material(tools, project, 'receipt', '17', 'invalid-plan')
    job = start(environment, [order], use_model=True)
    done = AgentLoop(jobs).run(job['id'], tools, Scripted(action))
    assert done['state'] == terminal and not tools.store.list_items()


def test_manual_edit_after_validation_is_not_overwritten(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'before-manual'),
               material(tools, project, 'payment', '17', 'before-manual-payment')]
    job = start(environment, entries)
    jobs.update(job['id'], state='running', run_started=time.time())
    job = jobs.get(job['id'])
    proposal = tools._scan(job)['proposals'][0]
    validated = tools.execute('validate_group', {'voucher_keys': proposal['voucher_keys'], 'payment_ids': proposal['payment_ids']}, job)
    job = jobs.update(job['id'], last_results=[{'tool': 'validate_group', 'result': validated}])
    entry = tools.intake.get(entries[0]['id'])
    entry['revision'] += 1
    entry['fields']['amount'] = '18'
    with tools.store.connect() as db:
        tools.matching._write_queue(db, entry)
    with pytest.raises(ValueError, match='变化'):
        tools.execute('create_draft', {'proposal_id': proposal['id']}, job)
    assert not tools.store.list_items() and tools.intake.get(entry['id'])['fields']['amount'] == '18'


def test_atomic_write_receipt_prevents_duplicate_after_lost_loop_reply(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'atomic order'),
               material(tools, project, 'payment', '17', 'atomic payment')]
    job = start(environment, entries)
    job = jobs.update(job['id'], state='running', run_started=time.time())
    proposal = tools._scan(job)['proposals'][0]
    validated = tools._validate(job, {'voucher_keys': proposal['voucher_keys'], 'payment_ids': proposal['payment_ids']})
    job = jobs.update(job['id'], last_results=[{'tool': 'validate_group', 'result': validated}])
    first = tools.execute('create_draft', {'proposal_id': proposal['id']}, job)
    assert tools.execute('create_draft', {'proposal_id': proposal['id']}, job) == first
    assert len(tools.store.list_items()) == 1
    # Simulate process death after commit, before appending the tool result.
    jobs.stop(job['id'], 'queued', 'synthetic interruption')
    done = AgentLoop(jobs).run(job['id'], InvoiceTools(Jobs(tools.store)))
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 1700
    assert len(tools.store.list_items()) == 1


def test_cancelled_task_cannot_be_resurrected_by_late_result(environment: Any) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'cancel')
    job = start(environment, [entry])
    jobs.stop(job['id'], 'cancelled', '已取消')
    assert AgentLoop(jobs).run(job['id'], tools)['state'] == 'cancelled'
    assert jobs.update(job['id'], state='ready')['state'] == 'cancelled'


def test_request_idempotency_and_project_scope(environment: Any) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'scope')
    job = jobs.create(project, [entry['id']], 'request-identity')
    assert jobs.create(project, [entry['id']], 'request-identity')['id'] == job['id']
    with pytest.raises(ValueError, match='编号'):
        jobs.create(project, [entry['id']], 'request-identity', use_model=True)
    with pytest.raises(ValueError, match='已有任务'):
        jobs.create(project, [entry['id']], 'request-other')
    jobs.stop(job['id'], 'cancelled', 'cancel')
    other = ProjectRegistry(tools.store.workspace / '后勤' / 'tasks.sqlite3').create_project('Another synthetic project')['id']
    with pytest.raises(ValueError, match='不属于'):
        jobs.create(other, [entry['id']], 'wrong-project')


def test_recovery_keeps_cumulative_counters_and_elapsed(environment: Any) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'recovery')
    job = start(environment, [entry])
    jobs.update(job['id'], state='running', models=2, tools=5, elapsed=3, run_started=time.time() - 2)
    service = AgentService(tools.store)
    try:
        recovered = service.jobs.get(job['id'])
        assert recovered['state'] == 'queued'
        assert recovered['models'] == 2 and recovered['tools'] == 5 and recovered['elapsed'] >= 5
    finally:
        service.close()


def test_model_adapter_uses_only_bounded_registered_tools(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tools, _ = environment
    planner = ModelPlanner(tools.store.root)
    captured = []
    monkeypatch.setattr(planner.ai, 'public_status', lambda: {'available': True})
    def request(rules: str, content: str, **kw: Any) -> dict[str, Any]:
        captured.append((rules, json.loads(content)))
        return call('ask_user')
    monkeypatch.setattr(planner.ai, '_request', request)
    assert planner.choose({'materials': []}) == call('ask_user')
    assert '不可信' in captured[0][0]
    assert set(captured[0][1]['tools']) == {'read_material','reconcile_amount','validate_group','inspect_materials','investigate_relations',
                                           'ask_user','extract_goods','classify_expense','name_expense'}


def hung_worker(workspace: str, key: str) -> None:
    jobs = Jobs(ExpenseStore(Path(workspace)))
    jobs.update(key, state='running', run_started=time.time())
    time.sleep(60)


def test_supervisor_kills_an_unresponsive_tool_process(environment: Any) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'hard deadline')
    job = start(environment, [entry], limits={'models': 6, 'tools': 24, 'seconds': 1, 'stalls': 2})
    service = AgentService(tools.store, worker=hung_worker)
    start_time = time.monotonic()
    try:
        service.kick()
        while time.monotonic() - start_time < 8 and jobs.get(job['id'])['state'] not in {'stopped', 'failed'}:
            time.sleep(.05)
        result = jobs.get(job['id'])
        assert result['state'] == 'stopped' and '强制停止' in result['message']
        assert time.monotonic() - start_time < 8
    finally:
        service.close()
    assert not service._thread or not service._thread.is_alive()
    assert not tools.store.list_items()


def test_typed_order_ids_resolve_same_price_groups_without_model(environment: Any) -> None:
    jobs, tools, project = environment
    entries = []
    groups = []
    for n in range(2):
        group = []
        for role in ('receipt', 'payment'):
            text = ('收据' if role == 'receipt' else '微信支付\n支付成功') + (
                '\n商家名称：合成工具商店\n金额17.00\n商家订单号：SYNTHETIC000000' + str(n))
            entry = material(tools, project, role, '17', f'identity-{n}-{role}', text=text)
            entries.append(entry)
            group.append(entry)
        groups.append(group)
    class IdentityPlanner:
        calls = 0
        def choose(self, context: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            proposal = context['inspection']['proposals'][0]
            return call('validate_group', voucher_keys=proposal['voucher_keys'], payment_ids=proposal['payment_ids'])
    job = start(environment, entries, use_model=True)
    planner = IdentityPlanner()
    done = AgentLoop(jobs).run(job['id'], tools, planner)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 3400
    expenses = tools.store.list_items()
    assert len(expenses) == 2 and all(e['amount_cents'] == 1700 and not e['verified'] for e in expenses)
    # A unique typed order ID is a local fact, so no model investigation is needed.
    assert planner.calls == done['models'] == 0
    linked_groups = []
    for group in groups:
        linked = {tools.intake.get(e['id'])['item_id'] for e in group}
        assert len(linked) == 1
        ident = linked.pop()
        linked_groups.append(ident)
        with tools.store.connect() as db:
            attached = {row['digest'] for row in db.execute('SELECT digest FROM attachments WHERE item_id=?', (ident,))}
        assert attached == {e['digest'] for e in group}
    assert len(set(linked_groups)) == 2
    assert AgentLoop(jobs).run(job['id'], tools) == done
    assert len(tools.store.list_items()) == 2


def test_more_than_four_proofs_with_explicit_order_are_supported(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '50', 'five-part-order', text=
                        '收据\n商家名称：合成工具商店')]
    for n in range(5):
        entries.append(material(tools, project, 'payment', '10', 'installment-' + str(n), text=
                                '微信支付\n支付成功\n商家名称：合成工具商店\n交易单号：SYNTHETICPAY00000' + str(n)))
    class GroupPlanner:
        def choose(self, context: dict[str, Any]) -> dict[str, Any]:
            proposal = context['inspection']['proposals'][0]
            return call('validate_group', voucher_keys=proposal['voucher_keys'], payment_ids=proposal['payment_ids'])
    job = start(environment, entries, use_model=True)
    done = AgentLoop(jobs).run(job['id'], tools, GroupPlanner())
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 5000
    assert len(tools.store.list_items()[0]['attachments']) == 6


def test_uploaded_native_materials_are_analyzed_and_complete_payment_facts(environment: Any) -> None:
    jobs, tools, project = environment
    order = material(tools, project, 'receipt', '17', 'raw order', received=True, text=
                     '收据\n商家名称：合成工具商店\n商品金额：16.00\n包装费：1.00\n实付：17.00')
    payment = material(tools, project, 'payment', '17', 'raw payment', received=True, text=
                       '微信支付\n付款金额：17.00\n当前状态：支付成功\n商家名称：合成工具商店')
    job = start(environment, [order, payment])
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 1700
    assert done['tools'] == 9 and done['models'] == 0


def test_cancel_after_validation_prevents_any_write(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'cancel order'),
               material(tools, project, 'payment', '17', 'cancel payment')]
    job = start(environment, entries)
    job = jobs.update(job['id'], state='running', run_started=time.time())
    proposal = tools._scan(job)['proposals'][0]
    validated = tools._validate(job, {'voucher_keys': proposal['voucher_keys'], 'payment_ids': proposal['payment_ids']})
    job = jobs.update(job['id'], last_results=[{'tool': 'validate_group', 'result': validated}])
    jobs.stop(job['id'], 'cancelled', 'cancel')
    with pytest.raises(ValueError, match='停止'):
        tools.execute('create_draft', {'proposal_id': proposal['id']}, job)
    assert not tools.store.list_items()


def test_transient_model_error_retries_once_and_persists_spent_budget(planner_environment: Any) -> None:
    from invoice_print_layout.glm_invoice_ai import GlmAIError
    jobs, tools, project = environment = planner_environment
    entry = material(tools, project, 'receipt', '17', 'transient network')
    job = start(environment, [entry], use_model=True)
    class NetworkPlanner:
        calls = 0
        def choose(self, state: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            raise GlmAIError('', failure_code='network_error')
    planner = NetworkPlanner()
    done = AgentLoop(jobs).run(job['id'], tools, planner)
    assert done['state'] == 'failed' and done['models'] == planner.calls == 2
    assert done['model_retries'] == 1
    resumed = jobs.retry(job['id'])
    assert resumed['models'] == 2 and resumed['model_retries'] == 1 and resumed['elapsed'] >= done['elapsed']
    done = AgentLoop(jobs).run(job['id'], tools, Scripted(call('ask_user')))
    assert done['state'] == 'needs_input' and done['models'] == 3


def test_model_schema_error_is_not_automatically_retried(planner_environment: Any) -> None:
    from invoice_print_layout.glm_invoice_ai import GlmAIError
    jobs, tools, project = environment = planner_environment
    entry = material(tools, project, 'receipt', '17', 'schema error')
    job = start(environment, [entry], use_model=True)
    class BadPlanner:
        def choose(self, state: dict[str, Any]) -> dict[str, Any]:
            raise GlmAIError('', failure_code='response_schema')
    done = AgentLoop(jobs).run(job['id'], tools, BadPlanner())
    assert done['state'] == 'failed' and done['models'] == 1 and done['model_retries'] == 0
    assert done['events'][-1]['error'] == 'response_schema'


def test_rejected_plan_returns_feedback_so_agent_can_investigate_and_recover(environment: Any) -> None:
    jobs, tools, project = environment
    order = material(tools, project, 'receipt', '110', 'feedback order',
                     text='收据\n商家名称：合成工具商店\n商品金额110.00\n优惠10.00\n实付100.00')
    payment = material(tools, project, 'payment', '100', 'feedback payment')
    job = start(environment, [order, payment], use_model=True)
    class RecoveringPlanner:
        calls = 0
        def choose(self, context: dict[str, Any]) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                return call('validate_group', voucher_keys=['q:' + order['id']], payment_ids=[payment['id']])
            assert context['recent_tools'][-1]['result']['valid'] is False
            assert context['recent_tools'][-1]['result']['tool_error']
            return call('reconcile_amount', id=order['id'])
    done = AgentLoop(jobs).run(job['id'], tools, RecoveringPlanner())
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 10000 and done['models'] == 2


def worker_with_child(workspace: str, key: str) -> None:
    jobs = Jobs(ExpenseStore(Path(workspace)))
    jobs.update(key, state='running', run_started=time.time())
    marker = Path(workspace) / 'late-child.txt'
    subprocess.Popen([sys.executable, '-c',
                      'import pathlib,time;time.sleep(4);pathlib.Path(' + repr(str(marker)) + ').write_text("late")'])
    time.sleep(60)


@pytest.mark.skipif(os.name != 'nt', reason='Windows process-tree termination')
def test_supervisor_also_terminates_owned_export_children(environment: Any) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'child deadline')
    job = start(environment, [entry], limits={'models': 6, 'tools': 24, 'seconds': 2, 'stalls': 2})
    service = AgentService(tools.store, worker=worker_with_child)
    try:
        service.kick()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and jobs.get(job['id'])['state'] not in {'stopped', 'failed'}:
            time.sleep(.05)
        assert jobs.get(job['id'])['state'] == 'stopped'
        time.sleep(4.2)
        assert not (tools.store.workspace / 'late-child.txt').exists()
    finally:
        service.close()
