"""Versioned local repair of source-bound organizing review markers."""
from __future__ import annotations

import json
from typing import Any

from invoice_print_layout.agent_jobs import Jobs, encoded
from invoice_print_layout.workbench import now


_REPAIR = 'organize-review-repair-v2'
_LEGACY_REPAIR = 'organize-review-repair-v1'
_CONTENT_SUFFIX = '；已组合费用和原件保留，可在人工工作台继续核对'


def repair_legacy_reviews(jobs: Jobs) -> None:
    from invoice_print_layout.agent_tools import InvoiceTools
    from invoice_print_layout.agent_transport import pair_status
    from invoice_print_layout.agent_product_rules import PRODUCT_RULES_VERSION

    tools = InvoiceTools(jobs)
    with jobs.store.connect() as db:
        candidates = db.execute("SELECT id FROM invoice_agent_jobs WHERE state IN ('ready','failed','stopped','cancelled')").fetchall()
    for candidate in candidates:
        with jobs.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            repaired = {row['key'] for row in db.execute(
                'SELECT key FROM invoice_agent_actions WHERE job_id=? AND key IN (?,?)',
                (candidate['id'], _LEGACY_REPAIR, _REPAIR))}
            if _LEGACY_REPAIR in repaired and _REPAIR in repaired:
                continue
            job = json.loads(db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?',
                                        (candidate['id'],)).fetchone()['data'])
            if job['state'] not in {'ready', 'failed', 'stopped', 'cancelled'} or not (
                    job.get('deferred_materials') or job.get('deferred_content')):
                continue
            records = [json.loads(row['data']) for row in db.execute(
                'SELECT data FROM invoice_agent_actions WHERE job_id=?', (job['id'],))]
            records += [row.get('result', {}) for row in job.get('last_results', []) if row.get('tool') == 'ask_user']
            questions = [r['question'] for r in records if isinstance(r.get('question'), dict)]
            reasons = {q['text'] for q in questions if q.get('type') == 'expense_content' and
                       isinstance(q.get('text'), str) and q['text'].endswith(_CONTENT_SUFFIX) and
                       q.get('material_ids') and set(q['material_ids']) <= set(job['material_ids'])}
            cleared_materials: list[str] = []
            legacy_materials = [] if _LEGACY_REPAIR in repaired else job.get('deferred_materials', [])
            for ident in set(legacy_materials) & set(job['material_ids']):
                row = db.execute('SELECT data FROM intake_queue WHERE id=?', (ident,)).fetchone()
                entry = json.loads(row['data']) if row else {}
                if (entry.get('project_id') == job['project_id'] and not entry.get('item_id') and
                        entry.get('status') in {'received', 'review'} and entry.get('agent_review_reason') in reasons):
                    entry.pop('agent_review_reason')
                    db.execute('UPDATE intake_queue SET data=? WHERE id=?', (encoded(entry), ident))
                    cleared_materials.append(ident)
            cleared_items = []
            reviewed_content = False
            for ident in ([] if _REPAIR in repaired else job.get('deferred_content', [])):
                try:
                    _, item, sources, _ = tools.content._load(job, ident)
                    if (not item.get('agent_review_reason') or item.get('deleted') or item.get('merged_into') or
                            item.get('amount_pending') or len(sources) != 2):
                        continue
                    invoice = next((e for e in sources if e['role'] == 'invoice'), None)
                    trip = next((e for e in sources if e['role'] in {'trip', 'trip_sheet'}), None)
                    if invoice is None or trip is None or pair_status(invoice, trip) != 'supported':
                        continue
                    reviewed_content = True
                    state = item.get('agent_content') or {}
                    checked = tools.content._inspect_products(state, sources, item)
                    if (checked['status'] != 'exact' or not checked['coverage_complete'] or
                            checked['category'] != '打车' or item.get('category') != '打车'):
                        continue
                except (ValueError, KeyError, TypeError, OSError):
                    continue  # Changed/missing originals and human edits keep their review marker.
                item.pop('agent_review_reason')
                state.update(status='ready', reason='商品、费用类别和报销名称已准备',
                             product_check=checked, product_rule_revision=PRODUCT_RULES_VERSION,
                             decision={'category': '打车', 'source': 'rules', 'reason': checked['reason']})
                state['base'] = tools.content._base(item)
                item.update(agent_content=state, updated_at=now())
                db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
                cleared_items.append(ident)
            job['deferred_materials'] = [i for i in job.get('deferred_materials', []) if i not in cleared_materials]
            job['deferred_content'] = [i for i in job.get('deferred_content', []) if i not in cleared_items]
            db.execute('UPDATE invoice_agent_jobs SET data=? WHERE id=?', (encoded(job), job['id']))
            receipt: dict[str, Any] = {'material_ids': sorted(cleared_materials), 'item_ids': sorted(cleared_items)}
            if _LEGACY_REPAIR not in repaired:
                db.execute('INSERT INTO invoice_agent_actions VALUES (?,?,?)', (job['id'], _LEGACY_REPAIR, encoded(receipt)))
            if reviewed_content:
                db.execute('INSERT INTO invoice_agent_actions VALUES (?,?,?)', (job['id'], _REPAIR, encoded(receipt)))
