"""Explicit, source-bound rechecks of a waiting draft under newer local rules."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from invoice_print_layout.agent_jobs import Jobs, digest, encoded


def _business_version(job: dict[str, Any]) -> str:
    # Read-time timing projections are diagnostics, not a new task permission.
    return digest({key: value for key, value in job.items() if key != 'timings'})


def _eligible(job: dict[str, Any]) -> str:
    items = job.get('item_ids')
    if (job.get('state') != 'needs_input' or job.get('run_started') is not None or
            job.get('approval') or (job.get('question') or {}).get('choices') or
            not isinstance(items, list) or len(items) != 1 or
            not isinstance(items[0], str) or not items[0]):
        raise ValueError('当前任务不能按新规则继续，请刷新或在人工工作台处理')
    if (job['models'] >= job['limits']['models'] or job['tools'] >= job['limits']['tools'] or
            job['elapsed'] >= job['limits']['seconds']):
        raise ValueError('任务额度已用完，请在人工工作台继续')
    return items[0]


def _checked_plan(plan: Any, job: dict[str, Any], item_id: str) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get('item_id') != item_id:
        raise ValueError('新规则检查范围无效')
    if any(not isinstance(plan.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', plan[key])
           for key in ('binding', 'base', 'raw_sha256')):
        raise ValueError('新规则检查缺少费用和原件快照')
    if (not isinstance(plan.get('rules_revision'), str) or not plan['rules_revision'] or
            not isinstance(plan.get('proof'), (dict, str)) or not plan['proof']):
        raise ValueError('新规则检查缺少明确分类依据')
    refs = plan.get('material_refs')
    if (not isinstance(refs, list) or not refs or
            any(not isinstance(ref, dict) or not isinstance(ref.get('id'), str) or
                type(ref.get('revision')) is not int or ref['revision'] < 0 or
                not isinstance(ref.get('digest'), str) or
                not re.fullmatch(r'[0-9a-f]{64}', ref['digest']) for ref in refs) or
            len({ref['id'] for ref in refs}) != len(refs) or
            {ref['id'] for ref in refs} != set(job['material_ids'])):
        raise ValueError('新规则检查必须覆盖当前任务的完整原件')
    # Reject malformed/non-JSON evidence before opening the write transaction.
    return json.loads(encoded(plan))  # type: ignore[no-any-return]


def queue_rule_recheck(jobs: Jobs, job: dict[str, Any]) -> dict[str, Any]:
    """Queue one unchanged owned draft; never grant a fact or another budget.

    ContentTools owns the source-grounded rule proof and human-edit guards.
    Repeat clicks are rejected without mutation after the first queue commit.
    """
    from invoice_print_layout.agent_tools import InvoiceTools

    with jobs.store.connect() as db:
        row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (job.get('id'),)).fetchone()
    if row is None:
        raise ValueError('任务不存在')
    original: dict[str, Any] = json.loads(row['data'])
    if _business_version(original) != _business_version(job):
        raise ValueError('任务已变化，请刷新后重新检查')
    item_id = _eligible(original)
    tools = InvoiceTools(jobs)
    plan = _checked_plan(tools.content.rule_reassessment_plan(original, item_id), original, item_id)
    action_key = 'product-rule-recheck:' + digest([
        item_id, plan['rules_revision'], plan['binding'], plan['proof']])

    with jobs.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (original['id'],)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        current: dict[str, Any] = json.loads(row['data'])
        if _business_version(current) != _business_version(original):
            raise ValueError('任务已变化，请刷新后重新检查')
        _eligible(current)
        if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND id<>? "
                      "AND state IN ('queued','running')", (current['project_id'], current['id'])).fetchone():
            raise ValueError('当前项目还有任务正在运行，请稍后再检查')
        if db.execute('SELECT 1 FROM invoice_agent_actions WHERE job_id=? AND key=?',
                      (current['id'], action_key)).fetchone():
            raise ValueError('这些原件已经按当前规则检查，请在人工工作台继续')
        # No task/material mutations precede the second proof. It rechecks
        # actual bytes and the authored expense while this writer owns the DB.
        observed = _checked_plan(tools.content.rule_reassessment_plan(current, item_id), current, item_id)
        if observed != plan:
            raise ValueError('检查期间费用、原件或规则已变化，请刷新')
        for ref in plan['material_refs']:
            source = db.execute('SELECT data FROM intake_queue WHERE id=?', (ref['id'],)).fetchone()
            entry = json.loads(source['data']) if source else {}
            if (entry.get('project_id') != current['project_id'] or entry.get('item_id') != item_id or
                    entry.get('status') != 'linked' or entry.get('revision') != ref['revision'] or
                    entry.get('digest') != ref['digest']):
                raise ValueError('原件归属或版本已变化，请刷新')
        if not any(json.loads(row['data']).get('item_id') == item_id for row in db.execute(
                'SELECT data FROM invoice_agent_effects WHERE job_id=?', (current['id'],))):
            raise ValueError('只能继续当前任务自己创建的费用')
        expense = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
        item = json.loads(expense['data']) if expense else {}
        if (item.get('project_id') != current['project_id'] or item.get('stage') != 'draft' or
                item.get('verified') or item.get('deleted') or item.get('merged_into')):
            raise ValueError('费用已修改或核对，请在人工工作台继续')
        stamp = time.time()
        receipt = {'kind': 'local_rule_recheck', 'plan': plan, 'requested_at': stamp,
                   'previous_job_version': _business_version(current),
                   'previous_material_revisions': current['material_revisions']}
        db.execute('INSERT INTO invoice_agent_actions VALUES (?,?,?)',
                   (current['id'], action_key, encoded(receipt)))
        current.update(state='queued', queue_revision=current.get('queue_revision', 0) + 1,
                       material_revisions={ref['id']: ref['revision'] for ref in plan['material_refs']},
                       product_rule_resume=plan, stalls=0, question=None,
                       message='已按新规则重新检查商品，继续生成报销预览', updated_at=stamp)
        jobs._timing_transition(current, 'needs_input')
        current['events'] = [*current['events'][-79:], {
            'message': '已排队重新检查商品分类；原件和累计额度保留', 'at': stamp}]
        db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?',
                   ('queued', encoded(current), current['id']))
    return jobs._timing_snapshot(current)
