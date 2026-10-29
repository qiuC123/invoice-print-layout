"""Leave unresolved facts on their existing records without blocking the job."""
from __future__ import annotations

import json
import time
from typing import Any

from invoice_print_layout.agent_jobs import Jobs, encoded
from invoice_print_layout.workbench import now


def leave_for_review(jobs: Jobs, key: str, question: dict[str, Any], *, finish: bool = False) -> dict[str, Any]:
    with jobs.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        job = json.loads(row['data'])
        if job['state'] not in {'running', 'needs_input'}:
            return jobs._timing_snapshot(job)
        items = set(job['item_ids'])
        for effect in db.execute('SELECT data FROM invoice_agent_effects WHERE job_id=?', (key,)):
            ident = json.loads(effect['data']).get('item_id')
            if ident:
                items.add(ident)
        content_ids = set(job.get('deferred_content', []))
        material_ids = set(job.get('deferred_materials', []))
        reason = str(question.get('text') or '材料缺少明确的金额或对应关系')
        for ident in items:
            row = db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()
            item = json.loads(row['data']) if row else {}
            content = item.get('agent_content') or {}
            if (item.get('project_id') != job['project_id'] or item.get('stage') != 'draft' or
                    item.get('verified') or item.get('deleted') or item.get('merged_into')):
                continue
            if content.get('status') == 'needs_input' or (
                    question.get('type') == 'expense_content' and content.get('status') != 'ready'):
                item.update(agent_review_reason=content.get('reason') or reason,
                            review_requested=True, updated_at=now())
                if content:
                    from invoice_print_layout.agent_content import ContentTools
                    content['base'] = ContentTools._base(item)
                db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
                content_ids.add(ident)
        targets = set(question.get('material_ids') or []) if question.get('type') != 'expense_content' else set()
        remaining = []
        for ident in job['material_ids']:
            row = db.execute('SELECT data FROM intake_queue WHERE id=?', (ident,)).fetchone()
            entry = json.loads(row['data']) if row else {}
            if (entry.get('project_id') != job['project_id'] or entry.get('item_id') or
                    entry.get('status') not in {'received', 'review'}):
                continue
            remaining.append(ident)
            if ident in targets or finish:
                entry['agent_review_reason'] = entry.get('agent_review_reason') or reason
                db.execute('UPDATE intake_queue SET data=? WHERE id=?', (encoded(entry), ident))
                material_ids.add(ident)
        previous = job['state']
        job.update(deferred_content=sorted(content_ids), deferred_materials=sorted(material_ids),
                   question=None, approval=None, updated_at=time.time())
        if finish:
            if job.get('run_started') is not None:
                job['elapsed'] += max(0, time.time() - job['run_started'])
            job.update(state='ready', run_started=None, item_ids=sorted(items),
                       remaining_material_ids=remaining, remaining_material_count=len(remaining),
                       message=f'已生成 {len(items)} 笔费用，{len(remaining)} 份材料留在材料合成台')
            jobs._timing_transition(job, previous)
        db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?', (job['state'], encoded(job), key))
    return jobs._timing_snapshot(job)
