"""A user's missing fact is bound to unchanged originals and never refreshes a budget."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from tests.test_invoice_agent import environment, material, start  # noqa: F401


def persisted_confirmation_state(jobs: Jobs, key: str) -> tuple[str, list[tuple[str, str]]]:
    """Compare durable bytes, including receipts, separately from live timers."""
    with jobs.store.connect() as db:
        row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
        assert row is not None
        receipts = [(str(value['key']), str(value['data'])) for value in db.execute(
            "SELECT key,data FROM invoice_agent_actions WHERE job_id=? AND key LIKE 'user-confirmation:%' ORDER BY key",
            (key,))]
    return str(row['data']), receipts


def business_state(value: dict[str, Any]) -> dict[str, Any]:
    return {key: data for key, data in value.items() if key != 'timings'}


def question(env: Any, *, legacy: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    jobs, tools, project = env
    entries = [material(tools, project, 'receipt', '17', 'confirmed order'),
               material(tools, project, 'payment', '17', 'confirmed payment')]
    job = start(env, entries)
    choice = {'id': 'same-expense', 'label': '确认同一笔', 'voucher_keys': ['q:' + entries[0]['id']],
              'payment_ids': [entries[1]['id']], 'support_ids': [], 'amount_cents': 1700,
              'action': 'confirm_relation', 'claim': 'same_expense', 'proposal': {'title': '胶带采购'}}
    q: dict[str, Any] = {'text': '这两份材料是否属于同一笔胶带采购？',
                         'snapshot': tools.fingerprint(job), 'choices': [choice],
                         'material_ids': [e['id'] for e in entries]}
    if not legacy:
        q.update(id='synthetic-question-one', type='material_relation',
                 material_refs=[{k: e[k] for k in ('id', 'revision', 'digest')} for e in entries])
    waiting = jobs.update(job['id'], state='needs_input', question=q, models=1, tools=2, elapsed=1.25)
    return waiting, q


@pytest.mark.parametrize('legacy', [False, True])
def test_confirmation_is_durable_resumes_preview_and_keeps_budget(environment: Any, legacy: bool) -> None:
    jobs, tools, _ = environment
    waiting, q = question(environment, legacy=legacy)
    resumed = jobs.answer(waiting['id'], 'same-expense', q.get('id'))
    assert resumed['state'] == 'queued' and resumed['question'] is None
    assert [resumed[k] for k in ('models', 'tools', 'elapsed')] == [1, 2, 1.25]
    assert resumed['approval']['choice_data'] == q['choices'][0]
    receipt = resumed['confirmations'][0]
    assert receipt['actor'] == 'user' and receipt['choice_data'] == q['choices'][0]
    assert len(receipt['material_refs']) == 2
    assert Jobs(tools.store).get(waiting['id'])['confirmations'] == [receipt]
    action_key = 'user-confirmation:' + receipt['question_id']
    assert jobs.cached(waiting['id'], action_key) == receipt
    with pytest.raises(ValueError, match='不能被工具结果覆盖'):
        jobs.cache(waiting['id'], action_key, {'invented': True})
    with pytest.raises(ValueError, match='问题已变化'):
        jobs.answer(waiting['id'], 'same-expense', q.get('id'))
    done = AgentLoop(jobs).run(waiting['id'], tools)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 1700
    assert done['models'] == 1 and done['confirmations'] == [receipt]
    assert len(tools.store.list_items()) == 1
    assert not tools.store.list_items()[0]['verified']
    assert tools.store.list_items()[0]['stage'] == 'draft'


@pytest.mark.parametrize('answer_id', [None, 'old-question', [], 1])
def test_modern_question_requires_its_exact_id(environment: Any, answer_id: Any) -> None:
    jobs, _, _ = environment
    waiting, _ = question(environment)
    before = copy.deepcopy(jobs.get(waiting['id']))
    durable_before = persisted_confirmation_state(jobs, waiting['id'])
    with pytest.raises(ValueError, match='问题已变化'):
        jobs.answer(waiting['id'], 'same-expense', answer_id)
    after = jobs.get(waiting['id'])
    assert business_state(after) == business_state(before)
    assert persisted_confirmation_state(jobs, waiting['id']) == durable_before
    assert after['timings']['current_user_wait_seconds'] >= before['timings']['current_user_wait_seconds']


@pytest.mark.parametrize('mutation', ['revision', 'digest', 'bytes', 'removed', 'project'])
def test_changed_material_cannot_be_confirmed(environment: Any, mutation: str) -> None:
    jobs, tools, _ = environment
    waiting, q = question(environment)
    entry = tools.intake.get(q['material_ids'][0])
    if mutation == 'revision':
        entry['revision'] += 1
    elif mutation == 'digest':
        entry['digest'] = '0' * 64
    elif mutation == 'bytes':
        Path(entry['path']).write_bytes(b'changed original')
    elif mutation == 'removed':
        entry['status'] = 'unused'
    else:
        entry['project_id'] = ''
    if mutation != 'bytes':
        with tools.store.connect() as db:
            tools.matching._write_queue(db, entry)
    with pytest.raises(ValueError, match='已经|原件'):
        jobs.answer(waiting['id'], 'same-expense', q['id'])
    assert jobs.get(waiting['id'])['state'] == 'needs_input'
    assert jobs.get(waiting['id']).get('confirmations', []) == []


def test_another_active_project_job_blocks_answer(environment: Any) -> None:
    jobs, tools, project = environment
    waiting, q = question(environment)
    other = material(tools, project, 'receipt', '18', 'another pending order')
    active = start(environment, [other])
    with pytest.raises(ValueError, match='当前项目已有任务'):
        jobs.answer(waiting['id'], 'same-expense', q['id'])
    assert jobs.get(active['id'])['state'] == 'queued'
    assert not jobs.get(waiting['id'])['confirmations']


@pytest.mark.parametrize('counter', ['models', 'tools', 'elapsed'])
def test_answer_never_revives_exhausted_budget(environment: Any, counter: str) -> None:
    jobs, _, _ = environment
    waiting, q = question(environment)
    cap = waiting['limits']['seconds' if counter == 'elapsed' else counter]
    jobs.update(waiting['id'], **{counter: cap})
    with pytest.raises(ValueError, match='任务额度已用完'):
        jobs.answer(waiting['id'], 'same-expense', q['id'])
    assert jobs.get(waiting['id'])[counter] == cap
    assert not jobs.get(waiting['id'])['confirmations']


def test_declining_is_recorded_once_and_does_not_repeat_the_question(environment: Any) -> None:
    jobs, tools, _ = environment
    waiting, q = question(environment)
    q['choices'] = [{'id': 'different-orders', 'label': '不是同一笔', 'action': 'not_same_expense'}]
    jobs.update(waiting['id'], question=q)
    declined = jobs.answer(waiting['id'], 'different-orders', q['id'])
    assert declined['state'] == 'stopped' and declined['question'] is None and declined['approval'] is None
    assert '补充订单明细' in declined['message']
    assert [declined[k] for k in ('models', 'tools', 'elapsed')] == [1, 2, 1.25]
    assert declined['confirmations'][0]['action'] == 'not_same_expense'
    durable_declined = persisted_confirmation_state(jobs, waiting['id'])
    assert business_state(AgentLoop(jobs).run(waiting['id'], tools)) == business_state(declined)
    assert persisted_confirmation_state(jobs, waiting['id']) == durable_declined
    assert not tools.store.list_items()
    with tools.store.connect() as db:
        receipts = db.execute("SELECT data FROM invoice_agent_actions WHERE job_id=? AND key LIKE 'user-confirmation:%'",
                              (waiting['id'],)).fetchall()
    assert len(receipts) == 1 and json.loads(receipts[0]['data']) == declined['confirmations'][0]


@pytest.mark.parametrize('mutation', ['missing_digest', 'missing_ref', 'duplicate_ref'])
def test_modern_question_requires_complete_material_snapshots(environment: Any, mutation: str) -> None:
    jobs, _, _ = environment
    waiting, q = question(environment)
    if mutation == 'missing_digest':
        q['material_refs'][0].pop('digest')
    elif mutation == 'missing_ref':
        q['material_refs'].pop()
    else:
        q['material_refs'].append(q['material_refs'][0])
    jobs.update(waiting['id'], question=q)
    with pytest.raises(ValueError, match='原件快照'):
        jobs.answer(waiting['id'], 'same-expense', q['id'])


def test_http_answer_forwards_question_identity(tmp_path: Path) -> None:
    from invoice_print_layout.agent_tools import InvoiceTools
    from invoice_print_layout.project_registry import ProjectRegistry
    from invoice_print_layout.workbench import ExpenseStore
    from tests.test_invoice_assistant import invoice_server
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic confirmation HTTP')['id']
    jobs = Jobs(store)
    waiting, q = question((jobs, InvoiceTools(jobs), project))
    with invoice_server(tmp_path) as client:
        body = {'id': waiting['id'], 'choice': 'same-expense'}
        assert client.json('POST', '/api/agent/answer', body)[0] == 400
        assert client.json('POST', '/api/agent/answer', {**body, 'question_id': 'stale'})[0] == 400
        code, response = client.json('POST', '/api/agent/answer', {**body, 'question_id': q['id']})
        assert code == 200 and response['job']['state'] == 'queued'
        assert response['job']['approval']['question_id'] == q['id']
        assert response['job']['confirmations'][0]['choice_data'] == q['choices'][0]


def test_rejected_relation_investigation_is_an_observation(environment: Any, monkeypatch: Any) -> None:
    from tests.test_invoice_agent import Scripted, call
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'unresolved relation'),
               material(tools, project, 'payment', '18', 'unresolved mismatched payment')]
    job = start(environment, entries, use_model=True)
    execute = tools.execute
    def rejecting(name: str, args: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        if name == 'investigate_relations':
            raise ValueError('需要更多商品对应依据')
        return execute(name, args, current)
    monkeypatch.setattr(tools, 'execute', rejecting)
    done = AgentLoop(jobs).run(job['id'], tools, Scripted(call('investigate_relations'), call('ask_user')))
    assert done['state'] == 'needs_input'
    observations = [r for r in done['last_results'] if r['tool'] == 'investigate_relations']
    # Until an investigation obtains new facts, its rejection remains bounded.
    assert observations and done['models'] <= job['limits']['models']
    assert not tools.store.list_items()


def test_inspection_cache_reads_new_model_observation_without_changing_sources(environment: Any) -> None:
    from tests.test_invoice_agent import Scripted, call
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'model observation cache')
    job = start(environment, [entry], use_model=True)
    class Observations:
        def __init__(self) -> None:
            self.actions = [call('inspect_materials'), None, call('inspect_materials'), call('ask_user')]
            self.inspections = 0
            self.assessment = 'before-model'

        def fingerprint(self, current: dict[str, Any]) -> str:
            return 'unchanged-source-fingerprint'

        def context(self, current: dict[str, Any]) -> dict[str, Any]:
            return {}

        def next_action(self, current: dict[str, Any]) -> dict[str, Any] | None:
            return self.actions.pop(0)

        def execute(self, name: str, args: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
            if name == 'inspect_materials':
                self.inspections += 1
                return {'assessment': self.assessment}
            if name == 'investigate_relations':
                self.assessment = 'after-model'
                return {'assessment': self.assessment}
            return {'question': {'text': '还缺一个事实', 'snapshot': self.fingerprint(current), 'choices': []}}
    observations = Observations()
    done = AgentLoop(jobs).run(job['id'], observations, Scripted(call('investigate_relations')))
    inspected = [r['result'] for r in done['last_results'] if r['tool'] == 'inspect_materials']
    assert observations.inspections == 2 and inspected == [{'assessment': 'before-model'}, {'assessment': 'after-model'}]
    assert done['models'] == 1 and done['state'] == 'needs_input'
    assert tools.intake.get(entry['id'])['revision'] == entry['revision']


def test_answer_changes_validation_cache_but_identical_observations_are_reused(environment: Any) -> None:
    from tests.test_invoice_agent import call
    jobs, _, _ = environment
    waiting, q = question(environment)
    jobs.update(waiting['id'], state='queued')
    class Validation:
        def __init__(self) -> None:
            self.actions = [call('validate_group', voucher_keys=q['choices'][0]['voucher_keys'],
                                 payment_ids=q['choices'][0]['payment_ids'])] * 2 + [call('ask_user')]
            self.validations = 0

        def fingerprint(self, current: dict[str, Any]) -> str:
            return q['snapshot']

        def context(self, current: dict[str, Any]) -> dict[str, Any]:
            return {}

        def next_action(self, current: dict[str, Any]) -> dict[str, Any] | None:
            return self.actions.pop(0)

        def execute(self, name: str, args: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
            if name == 'validate_group':
                self.validations += 1
                return {'valid': bool(current.get('approval'))}
            return {'question': q}
    validation = Validation()
    first = AgentLoop(jobs).run(waiting['id'], validation)
    assert validation.validations == 1 and first['state'] == 'needs_input'
    resumed = jobs.answer(waiting['id'], 'same-expense', q['id'])
    validation.actions = [call('validate_group', voucher_keys=q['choices'][0]['voucher_keys'],
                               payment_ids=q['choices'][0]['payment_ids']), call('ask_user')]
    done = AgentLoop(jobs).run(waiting['id'], validation)
    assert validation.validations == 2
    validations = [r['result'] for r in done['last_results'] if r['tool'] == 'validate_group']
    assert validations[-1]['valid'] and not validations[0]['valid']
    assert done['models'] == resumed['models'] == first['models']
