"""Conservative, source-bound automatic review of program-validated drafts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.workbench import ExpenseStore, amount_cents, now

RULE_VERSION = 'auto-verify-v1'


def source_signature(entries: list[dict[str, Any]]) -> str:
    fields = ('id', 'digest', 'revision', 'role', 'status', 'project_id', 'item_id', 'fields', 'text', 'field_sources',
              'manual_fields', 'confirmed_content', 'field_conflicts', 'issues', 'ai_analysis',
              'agent_review_reason', 'review_requested', 'recognition_status', 'ambiguous', 'needs_info')
    return hashlib.sha256(json.dumps(
        [{key: entry.get(key) for key in fields} for entry in sorted(entries, key=lambda value: value['id'])],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _blocked(value: dict[str, Any]) -> bool:
    return bool(any(value.get(key) for key in ('agent_review_reason', 'field_conflicts', 'review_requested',
                 'ambiguous', 'needs_info')) or value.get('ai_analysis', {}).get('status') in {'review', 'failed'}
                or value.get('recognition_status') in {'review', 'failed'})


def _expense_signature(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({key: value for key, value in data.items()
        if key not in {'verification', 'verified', 'updated_at'}}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def automatic_verification_valid(store: ExpenseStore, db: sqlite3.Connection, data: dict[str, Any]) -> bool:
    verification = data.get('verification') or {}
    if verification.get('mode') != 'automatic' or data.get('stage') != 'draft':
        return True
    if verification.get('expense_signature') != _expense_signature(data):
        return False
    attachments = [dict(record) for record in db.execute(
        'SELECT id,role,path,digest FROM attachments WHERE item_id=? ORDER BY id', (data['id'],))]
    if verification.get('attachments') != [{key: attachment[key] for key in ('id', 'role', 'digest')}
                                            for attachment in attachments]:
        return False
    entries = [json.loads(record['data']) for record in db.execute(
        "SELECT data FROM intake_queue WHERE json_extract(data,'$.item_id')=?", (data['id'],))]
    if verification.get('source_signature') != source_signature(entries):
        return False
    for attachment in attachments:
        path = Path(attachment['path']).resolve()
        try:
            if not path.is_relative_to(store.workspace) or hashlib.sha256(path.read_bytes()).hexdigest() != attachment['digest']:
                return False
        except OSError:
            return False
    return {entry['digest'] for entry in entries} == {attachment['digest'] for attachment in attachments}


def assess(store: ExpenseStore, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """A failed check is an ordinary pending draft, with no mutation or model call."""
    data = json.loads(row['data'])
    checks: dict[str, bool] = {}
    checks['draft'] = bool(data.get('stage') == 'draft' and not any(data.get(key) for key in
        ('verified', 'deleted', 'merged_into', 'returned_to_synthesis', 'legacy', 'history_imported')) and
        data.get('synthesis_material_state') not in {'archived', 'deleted'})
    item = store._read_item(db, row)
    checks['materials'] = bool(item['complete'] and not item.get('missing') and not item.get('returned_to_synthesis'))
    candidate = data.get('auto_verification_candidate') or {}
    tables = {record['name'] for record in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    entries = [json.loads(record['data']) for record in db.execute(
        "SELECT data FROM intake_queue WHERE json_extract(data,'$.item_id')=?", (data['id'],))] if 'intake_queue' in tables else []
    attachments = [dict(record) for record in db.execute(
        'SELECT id,role,path,digest FROM attachments WHERE item_id=? ORDER BY id', (data['id'],))]
    signature = source_signature(entries)
    checks['association'] = bool(candidate.get('automatic') is True and candidate.get('proof') and entries and
        candidate.get('source_signature') == signature and set(candidate.get('source_ids', [])) ==
        {entry['id'] for entry in entries} and {entry['digest'] for entry in entries} ==
        {attachment['digest'] for attachment in attachments} and not candidate.get('ambiguous'))
    sources = []
    for entry in entries:
        origin = (entry.get('field_sources') or {}).get('amount', '')
        if not origin and any(evidence.get('source_sha256') == entry['digest'] and
                evidence.get('fields', {}).get('amount') == entry.get('fields', {}).get('amount')
                for evidence in entry.get('agent_local_evidence', [])):
            origin = 'agent_local_evidence'
        if entry.get('role') in {'invoice', 'receipt', 'payment'}:
            try:
                cents = amount_cents(entry.get('fields', {}).get('amount'))
            except ValueError:
                cents = -1
            sources.append({'id': entry['id'], 'sha256': entry['digest'], 'field': 'amount',
                            'source': origin, 'amount_cents': cents, 'role': entry.get('role')})
    total = data.get('amount_cents')
    checks['amount'] = bool(type(total) is int and total > 0 and not data.get('amount_pending') and sources and
        candidate.get('amount_cents') == total and all(source['source'] and source['amount_cents'] > 0 for source in sources) and
        sum(source['amount_cents'] for source in sources if source['role'] in {'invoice', 'receipt'}) == total and
        (not any(source['role'] == 'payment' for source in sources) or
         sum(source['amount_cents'] for source in sources if source['role'] == 'payment') == total))
    content = data.get('agent_content') or {}
    job = db.execute('SELECT state FROM invoice_agent_jobs WHERE id=?', (content.get('job_id'),)).fetchone() if (
        'invoice_agent_jobs' in tables) else None
    checks['organizer_finished'] = bool(job and job['state'] == 'ready')
    from invoice_print_layout.agent_content import ContentTools
    checks['category'] = bool(data.get('category') in {'材料采购', '酒店', '高铁', '餐饮', '外卖', '打车', '顺丰'} and
        content.get('status') == 'ready' and content.get('base') == ContentTools._base(data) and
        content.get('decision', {}).get('category') == data.get('category'))
    # Only this exact registration's generic flag may be discharged. A later
    # human edit/return has an audit event and must remain pending.
    generic = data.get('auto_registration_review')
    latest_event = db.execute('SELECT MAX(id) FROM events WHERE item_id=?', (data['id'],)).fetchone()[0]
    generic_review = bool(generic and generic.get('event_id') == latest_event and
                          generic.get('job_id') == content.get('job_id'))
    pending = {**data, 'review_requested': bool(data.get('review_requested') and not generic_review)}
    checks['no_questions'] = bool(not _blocked(pending) and all(not _blocked(entry) and not entry.get('issues') and
        entry.get('status') in {'linked', 'matched'} for entry in entries) and
        not content.get('reason_code') and content.get('status') == 'ready')
    if 'category_attempts' in tables:
        attempt = db.execute('SELECT result FROM category_attempts WHERE item_id=?', (data['id'],)).fetchone()
        if attempt and json.loads(attempt['result']).get('status') in {'needs_info', 'error', 'stale'}:
            checks['no_questions'] = False
    checks['originals'] = True
    for attachment in attachments:
        path = Path(attachment['path']).resolve()
        try:
            if not path.is_relative_to(store.workspace) or hashlib.sha256(path.read_bytes()).hexdigest() != attachment['digest']:
                checks['originals'] = False
        except OSError:
            checks['originals'] = False
    return {'eligible': all(checks.values()), 'rule_version': RULE_VERSION, 'checks': checks,
            'sources': sources, 'source_signature': signature, 'relation_proof': candidate.get('proof', ''),
            'attachments': [{key: attachment[key] for key in ('id', 'role', 'digest')} for attachment in attachments]}


def scan(store: ExpenseStore, item_ids: list[str] | None = None) -> list[str]:
    if not OrganizeSettings(store.root).read()['auto_verify']:
        return []
    changed = []
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("SELECT id,source,data FROM expenses WHERE json_extract(data,'$.stage')='draft' "
                          "AND COALESCE(json_extract(data,'$.verified'),0)=0 "
                          "AND json_extract(data,'$.auto_verification_candidate.automatic')=1").fetchall()
        for row in rows:
            if item_ids is not None and row['id'] not in item_ids:
                continue
            decision = assess(store, db, row)
            if not decision['eligible']:
                continue
            data = json.loads(row['data'])
            data.update(verified=True, verification={'mode': 'automatic', 'at': now(), **decision}, updated_at=now())
            data.pop('review_requested', None)
            data.pop('auto_registration_review', None)
            data['verification']['expense_signature'] = _expense_signature(data)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), row['id']))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (row['id'], now(), '自动核对：' + RULE_VERSION + '；材料、金额来源、唯一关联、类别及疑点检查通过'))
            changed.append(row['id'])
    return changed
