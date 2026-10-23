"""Rule rechecks reuse the owned draft and preserve the original task allowance."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_content import ContentTools
from invoice_print_layout.agent_jobs import encoded
from invoice_print_layout.agent_rule_recovery import queue_rule_recheck
from tests.test_invoice_agent import environment
from tests.test_invoice_agent_content import draft


@pytest.fixture
def waiting_rule(environment: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    jobs, tools, _ = environment
    job, ident = draft(environment)
    tools.execute('extract_goods', {'item_id': ident}, job)
    raw, item, _, binding = tools.content._load(job, ident)
    state = item['agent_content']
    state.update(status='needs_input', decision={'category': None, 'source': 'jev', 'confidence': .78},
                 classification_rules_revision='old-rule')
    tools.content._save(job, raw, item, state, binding)
    jobs.update(job['id'], item_ids=[ident], models=1, tools=7, elapsed=1.4,
                question={'text': '分类待确认', 'choices': [], 'material_ids': job['material_ids']})
    job = jobs.stop(job['id'], 'needs_input', '分类尚待确认')
    calls: list[dict[str, Any]] = []

    def plan(self: ContentTools, current: dict[str, Any], item_id: str) -> dict[str, Any]:
        raw, owned, sources, bound = self._load(current, item_id)
        saved = owned['agent_content']
        if (saved['status'] != 'needs_input' or saved['decision'].get('category') is not None or
                saved.get('classification_rules_revision') == 'new-rule'):
            raise ValueError('没有新的精确规则依据')
        result = {'item_id': item_id, 'binding': bound, 'base': self._base(owned),
                  'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'rules_revision': 'new-rule',
                  'proof': {'category': '材料采购', 'basis': 'synthetic exact complete label'},
                  'material_refs': sorted([{'id': e['id'], 'revision': e['revision'], 'digest': e['digest']}
                                           for e in sources], key=lambda ref: ref['id'])}
        calls.append(deepcopy(result))
        return result

    monkeypatch.setattr(ContentTools, 'rule_reassessment_plan', plan, raising=False)
    return jobs, tools, job, ident, calls


def persistent(jobs: Any) -> dict[str, Any]:
    with jobs.store.connect() as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('invoice_agent_jobs', 'invoice_agent_actions', 'invoice_agent_effects',
                              'expenses', 'attachments', 'intake_queue', 'events')}


def test_rule_recheck_queues_same_owned_draft_with_actual_linked_refs(waiting_rule: Any) -> None:
    jobs, tools, before, ident, calls = waiting_rule
    saved = persistent(jobs)
    # Source revisions legitimately changed when the task registered the draft.
    assert before['material_revisions'] != {
        key: tools.intake.get(key)['revision'] for key in before['material_ids']}
    after = queue_rule_recheck(jobs, before)
    assert after['id'] == before['id'] and after['state'] == 'queued'
    assert after['item_ids'] == [ident] and after['queue_revision'] == before['queue_revision'] + 1
    for key in ('models', 'tools', 'elapsed', 'limits', 'seen_results', 'last_results', 'confirmations'):
        assert after[key] == before[key]
    assert after['question'] is None and after['stalls'] == 0
    assert len(calls) == 2 and calls[0] == calls[1] == after['product_rule_resume']
    assert after['material_revisions'] == {ref['id']: ref['revision'] for ref in calls[0]['material_refs']}
    actual = persistent(jobs)
    for table in ('invoice_agent_effects', 'expenses', 'attachments', 'intake_queue', 'events'):
        assert actual[table] == saved[table]
    assert len(actual['invoice_agent_actions']) == len(saved['invoice_agent_actions']) + 1
    with jobs.store.connect() as db:
        receipt = db.execute("SELECT data FROM invoice_agent_actions WHERE key LIKE 'product-rule-recheck:%'").fetchone()
    assert json.loads(receipt['data'])['plan'] == after['product_rule_resume']
    unchanged = persistent(jobs)
    with pytest.raises(ValueError, match='已变化|不能'):
        queue_rule_recheck(jobs, before)
    assert persistent(jobs) == unchanged


def test_consumed_rule_receipt_cannot_enqueue_another_waiting_attempt(waiting_rule: Any) -> None:
    jobs, _, job, _, _ = waiting_rule
    queued = queue_rule_recheck(jobs, job)
    # A recovered wait with identical sources and rule proof does not constitute
    # another opportunity to replenish progress or enqueue indefinitely.
    waiting = jobs.update(queued['id'], state='needs_input', product_rule_resume=None)
    before = persistent(jobs)
    with pytest.raises(ValueError, match='已经按当前规则'):
        queue_rule_recheck(jobs, waiting)
    assert persistent(jobs) == before


@pytest.mark.parametrize('change', ['note', 'file', 'foreign_effect', 'already_current', 'unknown'])
def test_rule_recheck_rejects_changed_or_unproven_content_without_writes(
        waiting_rule: Any, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    jobs, tools, job, ident, _ = waiting_rule
    if change == 'file':
        original = tools.intake.get(job['material_ids'][0])
        Path(original['path']).write_bytes(b'changed synthetic original')
    elif change == 'unknown':
        def unknown(*args: Any) -> Any:
            raise ValueError('完整商品仍含未知项')
        monkeypatch.setattr(ContentTools, 'rule_reassessment_plan', unknown)
    else:
        with tools.store.connect() as db:
            if change == 'foreign_effect':
                db.execute('DELETE FROM invoice_agent_effects WHERE job_id=?', (job['id'],))
            else:
                item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
                if change == 'note':
                    item['note'] = '人工后续用途'
                else:
                    item['agent_content']['classification_rules_revision'] = 'new-rule'
                db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
    before = persistent(jobs)
    with pytest.raises(ValueError):
        queue_rule_recheck(jobs, job)
    assert persistent(jobs) == before


@pytest.mark.parametrize('change', ['models', 'tools', 'seconds', 'approval', 'choice', 'active', 'state'])
def test_rule_recheck_preserves_budget_and_relation_boundaries(waiting_rule: Any, change: str) -> None:
    jobs, _, job, _, calls = waiting_rule
    updates: dict[str, Any] = {}
    if change in ('models', 'tools', 'seconds'):
        updates['elapsed' if change == 'seconds' else change] = job['limits'][change]
    elif change == 'approval':
        updates['approval'] = {'choice': 'same-expense'}
    elif change == 'choice':
        updates['question'] = {'choices': [{'id': 'same-expense'}]}
    elif change == 'state':
        updates['state'] = 'ready'
    elif change == 'active':
        with jobs.store.connect() as db:
            db.execute('INSERT INTO invoice_agent_jobs VALUES (?,?,?,?)',
                       ('other', job['project_id'], 'running', '{}'))
    if updates:
        job = jobs.update(job['id'], **updates)
    before = persistent(jobs)
    with pytest.raises(ValueError):
        queue_rule_recheck(jobs, job)
    assert persistent(jobs) == before
    if change != 'active':
        assert not calls


@pytest.mark.parametrize('change', ['proof', 'job'])
def test_recheck_detects_changes_between_preflight_and_write_transaction(
        waiting_rule: Any, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    jobs, _, job, _, _ = waiting_rule
    original = ContentTools.rule_reassessment_plan
    calls = 0
    def moving(self: ContentTools, current: dict[str, Any], ident: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        result = original(self, current, ident)
        if change == 'proof' and calls == 2:
            result['proof'] = {'category': '其他'}
        if change == 'job' and calls == 1:
            jobs.update(job['id'], message='并发任务变更')
        return result
    monkeypatch.setattr(ContentTools, 'rule_reassessment_plan', moving)
    with pytest.raises(ValueError, match='已变化'):
        queue_rule_recheck(jobs, job)
    with jobs.store.connect() as db:
        assert not db.execute("SELECT 1 FROM invoice_agent_actions WHERE key LIKE 'product-rule-recheck:%'").fetchone()
    assert jobs.get(job['id'])['state'] == 'needs_input'
