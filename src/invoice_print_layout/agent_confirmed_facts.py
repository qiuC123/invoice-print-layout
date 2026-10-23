"""Exact-original human facts. Local confirmations add evidence, never a budget.

Use the existing action journal; reading does not migrate the formal ledger.
Only the normal answer path or an authenticated human-history restore writes facts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, TYPE_CHECKING

from invoice_print_layout.workbench import CATEGORIES

if TYPE_CHECKING:
    from invoice_print_layout.agent_jobs import Jobs


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _digests(entries: list[dict[str, Any]]) -> set[str] | None:
    result = set()
    for entry in entries:
        value = entry.get('digest')
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
            return None
        try:
            if hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() != value:
                return None
        except (OSError, KeyError, TypeError):
            return None
        result.add(value)
    return result if len(result) == len(entries) else None


def restore(jobs: Jobs, body: dict[str, Any]) -> dict[str, Any]:
    """Import an explicitly confirmed history record through the local UI token.

    No category, text, role or amount is accepted as a model-derived default.
    A content fact is a source-bound human correction, separately attributed.
    """
    allowed = {'project_id', 'material_ids', 'claim', 'statement', 'provenance',
               'amount_cents', 'goods', 'category', 'content_material_id'}
    if set(body) - allowed:
        raise ValueError('事实恢复包含未支持字段')
    project, ids, claim = body.get('project_id'), body.get('material_ids'), body.get('claim')
    statement, provenance, amount = body.get('statement'), body.get('provenance'), body.get('amount_cents')
    if (not isinstance(project, str) or not project or not isinstance(ids, list) or
            not 1 <= len(ids) <= 20 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or
            claim not in {'same_expense', 'material_content', 'material_purpose'} or
            not isinstance(statement, str) or not 1 <= len(statement.strip()) <= 2000 or
            type(amount) is not int or not 0 < amount < 10**12 or not isinstance(provenance, dict) or
            set(provenance) != {'source', 'reference', 'sha256'} or
            provenance.get('source') not in {'direct_user_reply', 'recorded_user_confirmation'} or
            not isinstance(provenance.get('reference'), str) or not 1 <= len(provenance['reference']) <= 500 or
            not isinstance(provenance.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', provenance['sha256'])):
        raise ValueError('需要明确的人类确认原文、来源及精确原件范围')
    if claim == 'same_expense' and (len(ids) < 2 or any(k in body for k in ('goods', 'category', 'content_material_id'))):
        raise ValueError('同笔关系不能恢复分类或商品默认值')
    if claim == 'material_purpose' and (len(ids) != 1 or any(k in body for k in ('goods', 'category', 'content_material_id'))):
        raise ValueError('用途事实不能恢复分类或凭证豁免')
    if claim == 'material_content':
        goods = body.get('goods')
        if (not isinstance(goods, list) or not 1 <= len(goods) <= 50 or
                any(not isinstance(g, str) or not 1 <= len(g.strip()) <= 200 for g in goods) or
                body.get('category') not in CATEGORIES or body.get('content_material_id') not in ids):
            raise ValueError('商品事实需要明确的用户商品、类别及对应原件')
    with jobs.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        entries = []
        for ident in ids:
            row = db.execute('SELECT data FROM intake_queue WHERE id=?', (ident,)).fetchone()
            entry = json.loads(row['data']) if row else {}
            if (entry.get('project_id') != project or entry.get('status') not in {'received', 'review'} or
                    entry.get('item_id')):
                raise ValueError('事实只能恢复到当前项目尚未关联的原件')
            entries.append(entry)
        values = _digests(entries)
        if not values:
            raise ValueError('事实对应原件已经变化或重复')
        fact = {'claim': claim, 'actor': 'user', 'action': 'confirm_relation',
                'statement': statement.strip(), 'provenance': provenance,
                'material_digests': sorted(values), 'amount_cents': amount,
                'project_id': project, 'recorded_at': time.time()}
        if claim == 'material_content':
            fact.update(goods=list(dict.fromkeys(g.strip() for g in body['goods'])), category=body['category'],
                        content_digest=next(e['digest'] for e in entries if e['id'] == body['content_material_id']))
        fact['fact_id'] = 'source-fact:' + _hash({k: v for k, v in fact.items() if k != 'recorded_at'})
        db.execute('INSERT OR IGNORE INTO invoice_agent_actions VALUES (?,?,?)',
                   ('project-facts:' + project, fact['fact_id'], json.dumps(fact, ensure_ascii=False)))
        fact = json.loads(db.execute('SELECT data FROM invoice_agent_actions WHERE job_id=? AND key=?',
                         ('project-facts:' + project, fact['fact_id'])).fetchone()['data'])
    return dict(fact)


def _records(jobs: Jobs, project: str) -> list[dict[str, Any]]:
    with jobs.store.connect() as db:
        rows = db.execute("SELECT a.data,a.job_id FROM invoice_agent_actions a LEFT JOIN invoice_agent_jobs j "
                          "ON j.id=a.job_id WHERE (a.job_id=? OR (j.project_id=? AND a.key LIKE 'user-confirmation:%')) "
                          "ORDER BY a.rowid DESC LIMIT 2000", ('project-facts:' + project, project)).fetchall()
    result = []
    for row in rows:
        try:
            receipt = json.loads(row['data'])
            if receipt.get('actor') != 'user':
                continue
            if receipt.get('fact_id', '').startswith('source-fact:'):
                if (receipt.get('project_id') != project or receipt.get('provenance', {}).get('source') not in
                        {'direct_user_reply', 'recorded_user_confirmation'} or
                        receipt.get('fact_id') != 'source-fact:' + _hash({k: v for k, v in receipt.items()
                                                          if k not in {'fact_id', 'recorded_at'}})):
                    continue
                result.append(receipt)
                continue
            selected = receipt.get('choice_data') or {}
            proposal = selected.get('proposal') or selected
            ids = {k.removeprefix('q:') for k in proposal.get('voucher_keys', [])}
            ids.update(proposal.get('payment_ids', [])); ids.update(proposal.get('support_ids', []))
            refs = {r['id']: r['digest'] for r in receipt.get('material_refs', []) if r.get('digest')}
            amount = proposal.get('amount_cents') or receipt.get('amount_cents')
            if receipt.get('action') == 'not_same_expense':
                # Negative answers describe the displayed source scope, even
                # though no affirmative proposal was chosen.
                ids = set(refs)
                amount = None
            if (not ids or not ids <= refs.keys() or (receipt.get('action') != 'not_same_expense' and
                    (type(amount) is not int or amount <= 0)) or
                    receipt.get('action') not in {'confirm_relation', 'not_same_expense'}):
                continue
            result.append({'claim': 'same_expense', 'actor': 'user', 'action': receipt['action'],
                           'origin_job_id': row['job_id'],
                           'material_digests': sorted(refs[i] for i in ids), 'amount_cents': amount,
                           'statement': receipt.get('question_text', ''), 'fact_id': receipt['question_id'],
                           'provenance': {'source': 'normal_question_answer', 'reference': receipt['question_id']}})
        except (TypeError, KeyError, ValueError):
            continue
    return result


def relation_fact(jobs: Jobs, job: dict[str, Any], row: dict[str, Any],
                  entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    ids = {k.removeprefix('q:') for k in row.get('voucher_keys', [])}
    ids.update(row.get('payment_ids', [])); ids.update(row.get('support_ids', []))
    selected = [e for e in entries if e['id'] in ids]
    if not ids or len(selected) != len(ids) or any(e.get('project_id') != job['project_id'] for e in selected):
        return None
    values = _digests(selected)
    if not values:
        return None
    for fact in _records(jobs, job['project_id']):
        if (set(fact.get('material_digests', [])) == values and
                (fact.get('action') == 'not_same_expense' or fact.get('amount_cents') == row.get('amount_cents')) and
                fact.get('claim') in {'same_expense', 'material_content'}):
            return fact
    return None


def content_fact(jobs: Jobs, job: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    from invoice_print_layout.intake_queue import IntakeQueue
    if entry.get('project_id') != job['project_id'] or entry.get('id') not in job['material_ids']:
        return None
    queue = IntakeQueue(jobs.store)
    entries = [queue.get(key) for key in job['material_ids']]
    values = _digests(entries)
    if not values:
        return None
    for fact in _records(jobs, job['project_id']):
        if (fact.get('claim') == 'material_content' and fact.get('content_digest') == entry['digest'] and
                set(fact.get('material_digests', [])) <= values):
            return fact
    return None


def verified_content(item: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A human content statement applies to the exact whole expense only."""
    values = _digests(sources)
    if not values:
        return None
    for entry in sources:
        fact = entry.get('confirmed_content')
        if (not isinstance(fact, dict) or fact.get('actor') != 'user' or
                fact.get('claim') != 'material_content' or fact.get('action') != 'confirm_relation' or
                not str(fact.get('fact_id', '')).startswith('source-fact:') or
                fact.get('content_digest') != entry['digest'] or
                set(fact.get('material_digests', [])) != values or
                fact.get('amount_cents') != item.get('amount_cents') or
                fact.get('category') not in CATEGORIES or not fact.get('goods') or
                not fact.get('statement') or not fact.get('provenance')):
            continue
        goods = fact.get('goods')
        if (not isinstance(goods, list) or not 1 <= len(goods) <= 50 or
                any(not isinstance(g, str) or not 1 <= len(g.strip()) <= 200 for g in goods) or
                fact.get('fact_id') != 'source-fact:' + _hash({k: v for k, v in fact.items()
                                                          if k not in {'fact_id', 'recorded_at'}})):
            continue
        if (fact.get('project_id') != item.get('project_id') or
                any(e.get('project_id') != fact['project_id'] for e in sources) or
                fact.get('provenance', {}).get('source') not in {'direct_user_reply', 'recorded_user_confirmation'}):
            continue
        attachments = item.get('attachments')
        if isinstance(attachments, list) and {a.get('id') for a in attachments}:
            # ContentTools loads original sources from the actual attachment rows.
            # Its caller must supply exactly those sources; the stable scope above
            # prevents one confirmed receipt authorizing an extra source.
            if len(attachments) != len(sources):
                continue
            if any(a.get('digest') for a in attachments) and {a.get('digest') for a in attachments} != values:
                continue
        return fact
    return None


def purpose_fact(jobs: Jobs, job: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any] | None:
    values = _digests([entry])
    if not values or entry.get('project_id') != job['project_id']:
        return None
    return next((fact for fact in _records(jobs, job['project_id']) if
        fact.get('claim') == 'material_purpose' and set(fact.get('material_digests', [])) == values), None)
