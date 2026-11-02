"""F23-B uses only temporary workspaces, synthetic originals and loopback receivers."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any

import pytest

from invoice_print_layout import invoice_attention as module
from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.agent_review import leave_for_review
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.invoice_attention import Attention
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.project_events import ProjectEvents
from tests.test_invoice_agent import environment, material, start
from tests.test_auto_verify import prepared


EVIDENCE = {'method': '原件逐字段核实', 'reference': 'synthetic.pdf 第1页', 'statement': '合成原件明确列出这些事实。'}


def material_question(env: Any, **changes: Any) -> tuple[Attention, str]:
    jobs, tools, project = env
    entry = material(tools, project, 'receipt', '17.00', 'attention-synthetic')
    entry.update(changes)
    with tools.store.connect() as db:
        tools.matching._write_queue(db, entry)
    job = start(env, [entry])
    jobs.update(job['id'], state='running', run_started=time.time())
    leave_for_review(jobs, job['id'], {'text': '合成材料需要查证', 'material_ids': [entry['id']]}, finish=True)
    return Attention(tools.store), 'invoice-material:' + entry['id']


def item_question(env: Any) -> tuple[Attention, str]:
    jobs, tools, _ = env
    store, ident = prepared(env)
    with store.connect() as db:
        item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        job_id = item['agent_content']['job_id']
        item['agent_content'].update(status='needs_input', reason='合成内容需要查证')
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(item), ident))
    job = jobs.update(job_id, state='running', run_started=time.time())
    leave_for_review(jobs, job_id, {'type': 'expense_content', 'text': '具体内容待确认',
                                  'material_ids': job['material_ids']}, finish=True)
    return Attention(store), 'invoice-item:' + ident


def answer(att: Attention, obj: str, action: str, values: Any = None, actor: str = 'codex', **extra: Any) -> dict[str, Any]:
    return att.resolve({'object': obj, 'expected_version': att.show(obj)['version'], 'action': action,
                        'values': values or {}, 'decided_by': actor, 'evidence': EVIDENCE, **extra})


def events(att: Attention) -> list[dict[str, Any]]:
    with att.store.connect() as db:
        return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM invoice_event_outbox ORDER BY rowid')]


@pytest.mark.parametrize('kind', ['material', 'item'])
def test_structured_record_context_and_event(environment: Any, kind: str) -> None:
    att, obj = material_question(environment) if kind == 'material' else item_question(environment)
    context = att.show(obj)
    record = context['record']
    assert record['object'] == obj and record['project_id'] == environment[2]
    assert record['reason_code'] and record['question'] and record['missing']
    assert all(set(row) == {'material_id', 'reason', 'requires_source'} for row in record['missing'])
    assert not record['model_attempted'] and record['model_calls'] == 0
    assert context['fields']['agent_review_reason'] and context['materials']
    assert record['version'] == context['version']
    event = next(e for e in events(att) if e['object'] == obj)
    assert re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', event['id'])
    assert event['type'] == 'invoice.needs_attention' and event['data_class'] == 'invoice'
    assert event['source'] == 'invoice-workbench' and event['summary']
    assert event['ref']['object_type'] == obj.split(':')[0]
    assert event['ref']['object_id'] == obj.split(':')[1]
    assert event['ref']['version'] == record['version'] and event['ref']['emission'] == 1
    assert len(event['ref']['workspace_id']) == 64


def test_same_state_dedup_and_object_cap_rolling_day(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    att, obj = material_question(environment)
    jobs = environment[0]
    record = att.show(obj)['record']
    for index in range(5):
        jobs.update(record['job_id'], state='running', run_started=clock[0])
        leave_for_review(jobs, record['job_id'], record['question'], finish=True)
    assert len(events(att)) == 1
    for index in range(3):
        with att.store.connect() as db:
            kind, entry, _ = module._load(db, obj)
            entry['revision'] += 1
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
            module.capture(att.store, db, kind, entry, jobs.get(record['job_id']), record['question'])
    assert len(events(att)) == 3
    assert att.show(obj)['record']['event']['suppressed']
    assert att.list()['total'] == 1
    clock[0] += 86400
    with att.store.connect() as db:
        kind, entry, _ = module._load(db, obj)
        module.capture(att.store, db, kind, entry, jobs.get(record['job_id']), record['question'])
    assert len(events(att)) == 4 and events(att)[-1]['ref']['emission'] == 4


def test_record_and_event_roll_back_with_business(environment: Any) -> None:
    att, obj = material_question(environment)
    before = events(att)
    with pytest.raises(RuntimeError), att.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        kind, data, _ = module._load(db, obj)
        data['revision'] += 1
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data), data['id']))
        module.capture(att.store, db, kind, data, environment[0].get(att.show(obj)['record']['job_id']), {'text': '新问题'})
        raise RuntimeError('synthetic rollback')
    assert events(att) == before


@pytest.mark.parametrize('failure,code', [('failed', 'job_failed'), ('seconds', 'job_timeout'),
                                        ('tools', 'budget_exhausted'), ('models', 'budget_exhausted'),
                                        ('model_flag', 'model_budget_exhausted')])
def test_job_failure_fields_dedup_and_delivery(environment: Any, failure: str, code: str) -> None:
    att, obj = material_question(environment)
    jobs = environment[0]
    job = jobs.get(att.show(obj)['record']['job_id'])
    jobs.update(job['id'], state='running', run_started=time.time())
    changes: dict[str, Any] = {'state': 'failed' if failure == 'failed' else 'stopped', 'message': '合成失败原因'}
    if failure == 'seconds':
        changes['elapsed'] = job['limits']['seconds']
    elif failure in {'tools', 'models'}:
        changes[failure] = job['limits'][failure]
    elif failure == 'model_flag':
        changes.update(state='running', model_budget_exhausted=True)
    jobs.update(job['id'], **changes)
    jobs.update(job['id'], **changes)
    emitted = [e for e in events(att) if e['type'] == 'invoice.job_failed']
    assert len(emitted) == 1
    event = emitted[0]
    assert event['object'] == 'invoice-job:' + job['id'] and event['data_class'] == 'invoice'
    assert event['ref']['job_id'] == job['id'] and event['ref']['project_id'] == environment[2]
    assert event['ref']['reason_code'] == code and event['ref']['reason'] == '合成失败原因'
    delivered = []
    sender = ProjectEvents(att.store, sender=lambda url, payload: delivered.append(payload))
    sender.deliver_pending()
    sender.close()
    assert event in delivered


def test_stop_dispatch_emits_only_for_owned_generation(environment: Any) -> None:
    att, obj = material_question(environment)
    jobs = environment[0]
    job_id = att.show(obj)['record']['job_id']
    jobs.update(job_id, state='queued')
    jobs.stop_dispatch(job_id, 1, 'failed', '旧进程')
    assert not [e for e in events(att) if e['type'] == 'invoice.job_failed']
    jobs.stop_dispatch(job_id, 0, 'failed', '合成后台失败')
    assert len([e for e in events(att) if e['type'] == 'invoice.job_failed']) == 1


def test_pagination_and_project_filter(environment: Any) -> None:
    att, obj = material_question(environment)
    jobs, tools, _ = environment
    other = ExpenseProjects(att.store).create('另一个合成项目')['id']
    second, _ = material_question((jobs, tools, other))
    assert att.list(limit=1)['total'] == 2 and att.list(limit=1)['has_more']
    assert len(att.list(offset=1, limit=1)['items']) == 1
    assert att.list(offset=2)['items'] == []
    assert [r['object'] for r in att.list(environment[2])['items']] == [obj]
    assert second.list(other)['total'] == 1
    for args in ({'offset': -1}, {'limit': 0}, {'limit': 1001}):
        with pytest.raises(ValueError):
            att.list(**args)


@pytest.mark.parametrize('change', ['version', 'source', 'fields'])
def test_stale_answers_cannot_write(environment: Any, change: str) -> None:
    att, obj = material_question(environment)
    context = att.show(obj)
    expected = context['version']
    if change == 'version':
        expected = 'stale'
    elif change == 'source':
        Path(context['fields']['path']).write_bytes(b'synthetic changed source')
    else:
        with att.store.connect() as db:
            data = context['fields']
            data['fields']['amount'] = '18.00'
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data), data['id']))
    with pytest.raises(ValueError, match='version_changed'):
        answer(att, obj, 'not_reimbursable', expected_version=expected)
    assert not att.show(obj)['audit']


@pytest.mark.parametrize('field,value', [('amount', '17.00'), ('expense_date', '2030-01-02'),
                                       ('category', '材料采购'), ('title', '合成胶带'), ('merchant', '合成商店')])
@pytest.mark.parametrize('kind', ['material', 'item'])
def test_set_fields_every_supported_field(environment: Any, field: str, value: str, kind: str) -> None:
    att, obj = material_question(environment) if kind == 'material' else item_question(environment)
    result = answer(att, obj, 'set_fields', {field: value})
    context = att.show(obj)
    fields = context['fields'].get('fields', context['fields'])
    assert fields[field] == value and not context['fields'].get('agent_review_reason')
    assert result['job_id'] and result['verified'] is False
    assert environment[0].get(result['job_id'])['state'] == 'queued'
    if kind == 'item' and field in {'title', 'category', 'expense_date', 'merchant'}:
        done = AgentLoop(environment[0]).run(result['job_id'], environment[1])
        assert done['state'] == 'ready', done
        after = att.store.get(obj.split(':')[1])
        assert after[field] == value
        if field == 'category':
            assert after['agent_content']['decision']['source'] == 'codex'


@pytest.mark.parametrize('kind', ['material', 'item'])
def test_project_change_keeps_sources_with_new_project(environment: Any, kind: str) -> None:
    att, obj = material_question(environment) if kind == 'material' else item_question(environment)
    project = ExpenseProjects(att.store).create('合成目标项目')['id']
    result = answer(att, obj, 'set_fields', {'project_id': project})
    assert environment[0].get(result['job_id'])['project_id'] == project
    assert att.show(obj)['fields']['project_id'] == project
    assert all(e['project_id'] == project for e in att.show(obj)['materials'])


@pytest.mark.parametrize('action', ['not_invoice', 'not_reimbursable'])
@pytest.mark.parametrize('actor', ['codex', 'user'])
def test_exclusion_preserves_original_and_attributes_audit(environment: Any, action: str, actor: str) -> None:
    att, obj = material_question(environment)
    original = Path(att.show(obj)['fields']['path']).read_bytes()
    result = answer(att, obj, action, actor=actor)
    context = att.show(obj)
    assert result['status'] == 'excluded' and not result['job_id']
    assert context['fields']['status'] == 'archived'
    assert Path(context['fields']['path']).read_bytes() == original
    assert att.list()['total'] == 0
    audit = context['audit'][0]
    assert audit['decided_by'] == actor and audit['executed_by'] == 'codex' and audit['evidence'] == EVIDENCE
    assert not context['fields'].get('verified')


@pytest.mark.parametrize('actor', ['codex', 'user'])
def test_content_answer_resumes_real_loop_and_automatic_verification(environment: Any, actor: str) -> None:
    att, obj = item_question(environment)
    origin = next(e for e in att.show(obj)['materials'] if e['role'] == 'receipt')
    result = answer(att, obj, 'confirm_content', {'goods': ['胶带'], 'category': '材料采购', 'content_material_id': origin['id']}, actor)
    assert not att.store.get(obj.split(':')[1])['verified']
    OrganizeSettings(att.store.root).update({'auto_verify': True})
    done = AgentLoop(environment[0]).run(result['job_id'], environment[1])
    item = att.store.get(obj.split(':')[1])
    assert done['state'] == 'ready' and item['verified'], (done, item)
    assert item['verification']['mode'] == 'automatic'
    assert item['agent_content']['decision']['source'] == ('codex' if actor == 'codex' else 'human')
    assert [e for e in events(att) if e['type'] == 'invoice.project_ready']


@pytest.mark.parametrize('values', [{'amount': 'NaN'}, {'amount': '-1'}, {'amount': ''}, {'expense_date': '2030-02-30'},
                                  {'category': 'guess'}, {'project_id': 'missing'}, {'verified': True}, {}])
def test_invalid_financial_fields_roll_back(environment: Any, values: Any) -> None:
    att, obj = material_question(environment)
    before = att.show(obj)
    with pytest.raises(ValueError):
        answer(att, obj, 'set_fields', values)
    assert att.show(obj) == before


def test_unrelated_field_conflicts_and_human_events_are_not_overwritten(environment: Any) -> None:
    att, obj = material_question(environment, field_conflicts=[{'field': 'amount'}, {'field': 'merchant'}])
    answer(att, obj, 'set_fields', {'amount': '17.00'})
    assert att.show(obj)['fields']['field_conflicts'] == [{'field': 'merchant'}]
    item = att.store.create({'title': '合成人工费用', 'project_id': environment[2], 'amount': '1', 'category': '酒店'})
    assert '登记事项' in [e['message'] for e in item['events']]
    from tests.test_invoice_agent import proof
    att.store.add_attachment(item['id'], 'synthetic-human-invoice.pdf', proof('合成人工发票'), 'invoice')
    human = att.store.transition(item['id'], 'verify')
    assert human['verified'] and human['verification']['mode'] == 'human'
    with att.store.connect() as db:
        assert not db.execute('SELECT 1 FROM invoice_attention_audit WHERE object=?', ('invoice-item:' + item['id'],)).fetchone()


@pytest.mark.parametrize('actor', ['codex', 'user'])
def test_select_real_generated_candidate_and_resume(environment: Any, actor: str) -> None:
    from tests.test_agent_confirmed_facts import pair
    jobs, tools, _ = environment
    rows = pair(environment)
    for row in rows:
        row['field_sources'] = {'amount': 'native_pdf'}
        with tools.store.connect() as db:
            tools.matching._write_queue(db, row)
    job = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert job['state'] == 'ready' and not job['item_ids']
    att = Attention(tools.store)
    obj = 'invoice-material:' + rows[0]['id']
    context = att.show(obj)
    assert context['record']['reason_code'] == 'material_relation'
    choice = next(c for c in context['candidates'] if c['action'] == 'confirm_relation')
    result = answer(att, obj, 'choose_candidate', {'candidate_id': choice['id']}, actor)
    OrganizeSettings(att.store.root).update({'auto_verify': True})
    done = AgentLoop(jobs).run(result['job_id'], tools)
    assert done['state'] == 'ready' and len(done['item_ids']) == 1, done
    item = tools.store.get(done['item_ids'][0])
    assert item['verified'], item
    assert att.list()['total'] == 0
    for row in rows:
        assert att.show('invoice-material:' + row['id'])['audit'][0]['decided_by'] == actor


def test_real_agent_field_conflict_answer_resumes(environment: Any) -> None:
    jobs, tools, project = environment
    rows = [material(tools, project, 'receipt', '17', 'conflict-receipt'),
            material(tools, project, 'payment', '17', 'conflict-payment')]
    for row in rows:
        row['field_sources'] = {'amount': 'native_pdf'}
        if row['role'] == 'receipt':
            row['field_conflicts'] = [{'field': 'amount', 'local': '17', 'ai': '18'}]
            row['issues'] = ['AI与原件字段冲突：amount']
            row['ai_analysis'] = {'status': 'review'}
        with tools.store.connect() as db:
            tools.matching._write_queue(db, row)
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'ready'
    att = Attention(tools.store)
    obj = 'invoice-material:' + rows[0]['id']
    assert att.show(obj)['record']['reason_code'] == 'field_conflicts'
    result = answer(att, obj, 'set_fields', {'amount': '17.00'})
    assert att.show(obj)['fields']['ai_analysis']['status'] == 'resolved'
    assert jobs.get(result['job_id'])['state'] == 'queued'
    assert set(jobs.get(result['job_id'])['material_ids']) == {e['id'] for e in rows}
    OrganizeSettings(att.store.root).update({'auto_verify': True})
    done = AgentLoop(jobs).run(result['job_id'], tools)
    assert done['state'] == 'ready' and len(done['item_ids']) == 1, done
    assert att.store.get(done['item_ids'][0])['verified']


@pytest.mark.parametrize('body', [{'action': []}, {'decided_by': []}, {'evidence': {}},
                                 {'values': []}, {'action': 'verify'}, {'expected_version': ''}])
def test_malformed_resolution_is_rejected_without_writing(environment: Any, body: Any) -> None:
    att, obj = material_question(environment)
    with pytest.raises(ValueError):
        att.resolve({'object': obj, 'expected_version': att.show(obj)['version'], 'action': 'not_invoice',
                     'values': {}, 'decided_by': 'codex', 'evidence': EVIDENCE, **body})
    assert not att.show(obj)['audit']


def test_job_failures_obey_same_rolling_cap(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    att, obj = material_question(environment)
    jobs = environment[0]
    job_id = att.show(obj)['record']['job_id']
    for generation in range(4):
        jobs.update(job_id, state='queued', queue_revision=generation)
        jobs.stop_dispatch(job_id, generation, 'failed', '合成后台失败')
    failures = [e for e in events(att) if e['type'] == 'invoice.job_failed']
    assert len(failures) == 3
    clock[0] += 86400
    jobs.update(job_id, state='queued', queue_revision=4)
    jobs.stop_dispatch(job_id, 4, 'failed', '合成后台失败')
    assert len([e for e in events(att) if e['type'] == 'invoice.job_failed']) == 4


def test_resolving_during_failed_http_send_does_not_resurrect_event(environment: Any) -> None:
    att, obj = material_question(environment)
    def sender(url: str, payload: Any) -> None:
        assert payload['object'] == obj
        answer(att, obj, 'not_reimbursable')
        raise ConnectionError('synthetic failed delivery')
    outbox = ProjectEvents(att.store, sender=sender)
    try:
        outbox.deliver_pending()
    finally:
        outbox.close()
    with att.store.connect() as db:
        assert db.execute('SELECT status FROM invoice_event_outbox').fetchone()[0] == 'superseded'


def test_project_ready_coalescing_does_not_supersede_attention(environment: Any) -> None:
    from tests.test_project_events import fee
    att, obj = material_question(environment)
    fee(att.store, environment[2])
    with att.store.connect() as db:
        rows = list(db.execute("SELECT payload FROM invoice_event_outbox WHERE status='pending'"))
    assert {json.loads(row[0])['type'] for row in rows} == {'invoice.needs_attention', 'invoice.project_ready'}


@pytest.mark.parametrize('change', ['revision', 'bytes', 'reserved'])
def test_candidate_rechecks_all_related_originals(environment: Any, change: str) -> None:
    from tests.test_agent_confirmed_facts import pair
    jobs, tools, _ = environment
    rows = pair(environment)
    AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    att = Attention(tools.store)
    obj = 'invoice-material:' + rows[0]['id']
    choice = next(c for c in att.show(obj)['candidates'] if c['action'] == 'confirm_relation')
    if change in {'revision', 'reserved'}:
        with att.store.connect() as db:
            peer = tools.intake.get(rows[1]['id'])
            if change == 'revision':
                peer['revision'] += 1
            else:
                peer['material_match_id'] = 'synthetic-reserved-group'
            tools.matching._write_queue(db, peer)
    else:
        Path(rows[1]['path']).write_bytes(b'synthetic changed related source')
    with pytest.raises(ValueError):
        answer(att, obj, 'choose_candidate', {'candidate_id': choice['id']})
    assert not att.show(obj)['audit']


def test_multi_object_question_has_stable_event_versions(environment: Any) -> None:
    from tests.test_agent_confirmed_facts import pair
    jobs, tools, _ = environment
    rows = pair(environment)
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    att = Attention(tools.store)
    question = att.show('invoice-material:' + rows[0]['id'])['record']['question']
    assert len(events(att)) == 2
    for row in rows:
        context = att.show('invoice-material:' + row['id'])
        assert context['record']['version'] == context['version']
    jobs.update(done['id'], state='running', run_started=time.time())
    leave_for_review(jobs, done['id'], question, finish=True)
    assert len(events(att)) == 2


@pytest.mark.parametrize('changes', [{'status': 'archived'}, {'status': 'linked', 'item_id': 'synthetic-existing'},
                                   {'material_match_id': 'synthetic-group'}])
def test_materials_handled_elsewhere_are_no_longer_actionable(environment: Any, changes: Any) -> None:
    att, obj = material_question(environment)
    with att.store.connect() as db:
        _, data, _ = module._load(db, obj)
        data.update(changes)
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data), data['id']))
    assert att.list()['total'] == 0
    assert att.show(obj)['status'] == 'superseded'
    with pytest.raises(ValueError):
        answer(att, obj, 'not_invoice')
    assert not att.show(obj)['audit']
