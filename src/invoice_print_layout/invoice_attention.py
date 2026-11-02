"""Source-bound questions, local Codex answers and transactional rescheduling."""
from __future__ import annotations

from datetime import datetime, timezone
import builtins
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, TYPE_CHECKING

from invoice_print_layout.workbench import CATEGORIES, amount_cents, now
from invoice_print_layout.workbench_runtime import identity

if TYPE_CHECKING:
    from invoice_print_layout.workbench import ExpenseStore


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def install(db: sqlite3.Connection) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS invoice_attention
            (object TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS invoice_attention_project ON invoice_attention(status,project_id);
        CREATE TABLE IF NOT EXISTS invoice_attention_audit
            (id INTEGER PRIMARY KEY, object TEXT NOT NULL, at TEXT NOT NULL, data TEXT NOT NULL);
    ''')


def emit(store: ExpenseStore, db: sqlite3.Connection, event_type: str, obj: str,
         version: str, reason: str, summary: str, ref: dict[str, Any]) -> dict[str, Any]:
    at = time.time()
    workspace_id = identity(store.workspace, '', 'invoice')['workspace_id']
    event_id = 'invoice-attention-' + _hash([workspace_id, obj, version, reason])
    previous = db.execute('SELECT payload FROM invoice_event_outbox WHERE id=?', (event_id,)).fetchone()
    if previous:
        return {'event_id': event_id, 'emission': json.loads(previous['payload'])['ref']['emission'], 'suppressed': False}
    count = db.execute("SELECT COUNT(*) FROM invoice_event_outbox WHERE json_extract(payload,'$.object')=? "
                       'AND created>?', (obj, at - 86400)).fetchone()[0]
    if count >= 3:
        return {'event_id': None, 'suppressed': True, 'suppression_reason': 'three_in_24h'}
    sequence = db.execute("SELECT COUNT(*) FROM invoice_event_outbox WHERE json_extract(payload,'$.object')=?",
                          (obj,)).fetchone()[0] + 1
    ref = {**ref, 'workspace_id': workspace_id, 'version': version, 'reason_code': reason, 'emission': sequence}
    payload = {'id': event_id, 'source': 'invoice-workbench', 'type': event_type, 'object': obj,
               'data_class': 'invoice', 'summary': summary[:500], 'ref': ref,
               'occurred_at': datetime.fromtimestamp(at, timezone.utc).isoformat()}
    db.execute('INSERT INTO invoice_event_outbox '
               '(id,project_id,version,created,retry_at,status,payload) VALUES (?,?,?,?,?,?,?)',
               (event_id, ref.get('project_id', ''), version, at, at, 'pending', _json(payload)))
    return {'event_id': event_id, 'emission': sequence, 'suppressed': False}


def job_failure(store: ExpenseStore, db: sqlite3.Connection, job: dict[str, Any], previous: str) -> None:
    reason = ('model_budget_exhausted' if job.get('model_budget_exhausted') else
              'job_failed' if job['state'] == 'failed' else
              'job_timeout' if job['state'] == 'stopped' and (
                  job['elapsed'] >= job['limits']['seconds'] or '超时' in job.get('message', '')) else
              'budget_exhausted' if job['state'] == 'stopped' and (
                  job['tools'] >= job['limits']['tools'] or job['models'] >= job['limits']['models']) else '')
    if not reason or (job['state'] == previous and not job.get('model_budget_exhausted')):
        return
    version = _hash([job['id'], job.get('queue_revision', 0), reason])
    emit(store, db, 'invoice.job_failed', 'invoice-job:' + job['id'], version, reason,
         job.get('message') or '整理任务未完成',
         {'job_id': job['id'], 'project_id': job['project_id'], 'state': job['state'],
          'reason': job.get('message', '')})


def _load(db: sqlite3.Connection, obj: str) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    kind, _, ident = obj.partition(':')
    if kind not in {'invoice-item', 'invoice-material'} or not ident or ':' in ident:
        raise ValueError('疑点对象无效')
    table = 'expenses' if kind == 'invoice-item' else 'intake_queue'
    row = db.execute(f'SELECT data FROM {table} WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('疑点对象不存在')
    data = json.loads(row['data'])
    entries = [json.loads(row['data']) for row in db.execute(
        "SELECT data FROM intake_queue WHERE json_extract(data,'$.item_id')=? ORDER BY id", (ident,))] if (
        kind == 'invoice-item') else [data]
    return kind, data, entries


def _editable(kind: str, data: dict[str, Any]) -> bool:
    if data.get('deleted') or data.get('merged_into') or data.get('verified'):
        return False
    if kind == 'invoice-item':
        return bool(data.get('stage') == 'draft' and not data.get('returned_to_synthesis') and
                    data.get('synthesis_material_state') not in {'archived', 'deleted'})
    return bool(data.get('status') in {'received', 'review'} and not data.get('item_id') and
                not data.get('material_match_id') and not data.get('duplicate_of'))


def _source(entry: dict[str, Any], store: ExpenseStore) -> dict[str, Any]:
    path = Path(entry['path']).resolve()
    actual = ''
    if path.is_relative_to(store.workspace):
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    return {'id': entry['id'], 'revision': entry['revision'], 'digest': entry['digest'], 'actual_digest': actual}


def _version(store: ExpenseStore, db: sqlite3.Connection, obj: str, record: dict[str, Any]) -> str:
    kind, data, entries = _load(db, obj)
    ids = set(record.get('question', {}).get('material_ids') or []) - {e['id'] for e in entries}
    for ident in sorted(ids):
        row = db.execute('SELECT data FROM intake_queue WHERE id=?', (ident,)).fetchone()
        if row:
            entries.append(json.loads(row['data']))
    attachments = [dict(row) for row in db.execute(
        'SELECT id,role,digest,path FROM attachments WHERE item_id=? ORDER BY id', (data['id'],))] if kind == 'invoice-item' else []
    question = {k: v for k, v in record.get('question', {}).items() if k not in {'id', 'snapshot'}}
    return _hash([{k: v for k, v in data.items() if k != 'updated_at'},
                  [{k: v for k, v in e.items() if k not in {'updated_at', 'agent_review_reason'}} for e in entries],
                  [_source(e, store) for e in entries], attachments, question,
                  record.get('reason_code'), record.get('status')])


def capture(store: ExpenseStore, db: sqlite3.Connection, kind: str, data: dict[str, Any],
            job: dict[str, Any], question: dict[str, Any]) -> None:
    obj = kind + ':' + data['id']
    fields = data.get('fields') or data
    content = data.get('agent_content') or {}
    code = question.get('reason_code') or (
            ('content_missing' if not content.get('goods') else 'category_uncertain') if kind == 'invoice-item' else
            'material_relation' if question.get('choices') else
            'field_conflicts' if data.get('field_conflicts') else
            'amount_missing' if not fields.get('amount') else
            'model_unavailable' if job.get('model_unavailable') else 'material_facts_missing')
    missing = question.get('missing') or data.get('issues') or [content.get('reason') or data['agent_review_reason']]
    missing = [{'material_id': m.get('material_id') if isinstance(m, dict) else data['id'] if kind == 'invoice-material' else None,
                'reason': m.get('reason', '') if isinstance(m, dict) else str(m),
                'requires_source': bool(m.get('requires_source')) if isinstance(m, dict) else False} for m in missing]
    record = {'object': obj, 'object_type': kind, 'object_id': data['id'], 'project_id': data.get('project_id', ''),
              'job_id': job['id'], 'status': 'open', 'reason_code': code,
              'reason': data['agent_review_reason'], 'question': question,
              'missing': missing,
              'model_attempted': bool(job['models']), 'model_calls': job['models'],
              'model_unavailable': bool(job.get('model_unavailable')),
              'candidates': question.get('choices') or [], 'updated_at': now()}
    record['version'] = _version(store, db, obj, record)
    record['event'] = emit(store, db, 'invoice.needs_attention', obj, record['version'], str(code), record['reason'],
                           {key: record[key] for key in ('object_type', 'object_id', 'project_id', 'job_id')})
    db.execute('INSERT INTO invoice_attention VALUES (?,?,?,?) ON CONFLICT(object) DO UPDATE SET '
               'project_id=excluded.project_id,status=excluded.status,data=excluded.data',
               (obj, record['project_id'], 'open', _json(record)))


class Attention:
    def __init__(self, store: ExpenseStore):
        from invoice_print_layout.agent_jobs import Jobs
        from invoice_print_layout.intake_queue import IntakeQueue
        self.store = store
        self.jobs = Jobs(store)
        IntakeQueue(store)

    @staticmethod
    def _record(db: sqlite3.Connection, obj: str) -> dict[str, Any]:
        row = db.execute('SELECT data FROM invoice_attention WHERE object=?', (obj,)).fetchone()
        if row is None:
            raise ValueError('疑点不存在')
        result: dict[str, Any] = json.loads(row['data'])
        return result

    def list(self, project: str = '', *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('分页参数无效')
        with self.store.connect() as db:
            rows = db.execute("SELECT a.object,a.data,COALESCE(e.data,q.data) AS current FROM invoice_attention a "
                              "LEFT JOIN expenses e ON a.object='invoice-item:'||e.id "
                              "LEFT JOIN intake_queue q ON a.object='invoice-material:'||q.id "
                              "WHERE a.status='open' AND (?='' OR json_extract(COALESCE(e.data,q.data),'$.project_id')=?) "
                              "AND COALESCE(json_extract(COALESCE(e.data,q.data),'$.agent_review_reason'),'')<>'' "
                              'ORDER BY a.rowid', (project, project)).fetchall()
            rows = [row for row in rows if _editable(row['object'].partition(':')[0], json.loads(row['current']))]
            records = [json.loads(row['data']) for row in rows[offset:offset + limit]]
            for record in records:
                record['version'] = _version(self.store, db, record['object'], record)
        return {'status': 'complete', 'items': records, 'total': len(rows), 'offset': offset,
                'limit': limit, 'has_more': offset + limit < len(rows)}

    def show(self, obj: str) -> dict[str, Any]:
        with self.store.connect() as db:
            record = self._record(db, obj)
            kind, data, entries = _load(db, obj)
            status = record['status']
            if status == 'open' and (not data.get('agent_review_reason') or not _editable(kind, data)):
                status = 'superseded'
            if kind == 'invoice-item':
                data = self.store._read_item(db, db.execute('SELECT id,source,data FROM expenses WHERE id=?',
                                                           (data['id'],)).fetchone())
            related_ids = record.get('question', {}).get('material_ids') or []
            related = [json.loads(row['data']) for ident in related_ids if ident not in {e['id'] for e in entries}
                       for row in db.execute('SELECT data FROM intake_queue WHERE id=?', (ident,))]
            history = [json.loads(row['data']) for row in db.execute(
                'SELECT data FROM invoice_attention_audit WHERE object=? ORDER BY id', (obj,))]
            version = _version(self.store, db, obj, record)
        return {'status': status, 'object': obj, 'version': version, 'record': record,
                'fields': data, 'materials': entries, 'related_materials': related,
                'candidates': record['candidates'], 'audit': history}

    def resolve(self, body: dict[str, Any]) -> dict[str, Any]:
        from invoice_print_layout.agent_jobs import new_job, DEFAULT_LIMITS
        allowed = {'object', 'expected_version', 'action', 'values', 'decided_by', 'evidence'}
        obj, action, actor = body.get('object'), body.get('action'), body.get('decided_by')
        values, evidence = body.get('values', {}), body.get('evidence')
        if (set(body) - allowed or not isinstance(obj, str) or not isinstance(body.get('expected_version'), str) or
                not body['expected_version'] or not isinstance(action, str) or
                action not in {'set_fields', 'choose_candidate', 'confirm_content',
                                                               'not_invoice', 'not_reimbursable'} or
                not isinstance(actor, str) or actor not in {'codex', 'user'} or not isinstance(values, dict) or
                not isinstance(evidence, dict) or set(evidence) != {'method', 'reference', 'statement'} or
                any(not isinstance(v, str) or not 1 <= len(v.strip()) <= 2000 for v in evidence.values())):
            raise ValueError('需要有效动作、版本、决定者和完整依据（method/reference/statement）')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            record = self._record(db, obj)
            if record['status'] != 'open' or _version(self.store, db, obj, record) != body['expected_version']:
                raise ValueError('version_changed：疑点或原件已变化，请重新查看上下文')
            kind, data, entries = _load(db, obj)
            if not data.get('agent_review_reason') or not _editable(kind, data):
                raise ValueError('对象已处理、关联或不再是可修改的草稿')
            if any(not (source := _source(e, self.store))['actual_digest'] or
                   source['actual_digest'] != source['digest'] for e in entries):
                raise ValueError('原件不可读取或已变化')
            parent = json.loads(db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (record['job_id'],)).fetchone()['data'])
            if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND state IN ('queued','running')",
                          (data['project_id'],)).fetchone():
                raise ValueError('项目正在整理，请等待任务结束后重新查看')
            before = _json(data)
            if action == 'set_fields':
                self._set_fields(db, kind, data, entries, values, str(actor))
            elif action == 'choose_candidate':
                entries = self._choose(db, record, values, str(actor), evidence)
            elif action == 'confirm_content':
                self._content(db, data, entries, values, str(actor), evidence)
            else:
                if kind != 'invoice-material' or values or data.get('material_match_id'):
                    raise ValueError('只能对尚未关联的材料标记不是发票或不报')
                data.update(status='archived', archived_from={'status': data['status'], 'result': data.get('result', '')},
                            result='不是发票，保留原件' if action == 'not_invoice' else '明确不报，保留原件')
            data.pop('agent_review_reason', None)
            data.pop('review_requested', None)
            data['updated_at'] = now()
            if kind == 'invoice-material':
                data['revision'] = json.loads(before)['revision'] + 1
                entries = [data] if action != 'choose_candidate' else [data if e['id'] == data['id'] else e for e in entries]
            else:
                data.pop('agent_content', None)
                data['verified'] = False
                if action == 'set_fields':
                    from invoice_print_layout.auto_verify import source_signature
                    data['attention_fields'] = {'values': values, 'decided_by': actor,
                                                'source_signature': source_signature(entries)}
            table = 'expenses' if kind == 'invoice-item' else 'intake_queue'
            db.execute(f'UPDATE {table} SET data=? WHERE id=?', (_json(data), data['id']))
            if kind == 'invoice-material' and action not in {'not_invoice', 'not_reimbursable', 'choose_candidate'}:
                # Recheck the original question's scope, never gather later arrivals from the project pool.
                scope = set(record['question'].get('material_ids', [])) & set(parent['material_ids'])
                for ident in sorted(scope - {data['id']}):
                    _, peer, _ = _load(db, 'invoice-material:' + ident)
                    if peer['project_id'] == data['project_id'] and _editable('invoice-material', peer):
                        if _source(peer, self.store)['actual_digest'] != peer['digest']:
                            raise ValueError('同一问题中的原件已变化，请重新整理')
                        peer.pop('agent_review_reason', None)
                        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (_json(peer), ident))
                        entries.append(peer)
            job = None
            if action not in {'not_invoice', 'not_reimbursable'}:
                project = data['project_id']
                if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND state IN ('queued','running')",
                              (project,)).fetchone():
                    raise ValueError('目标项目正在整理，请稍后重试')
                job = new_job(project, entries, use_model=parent['use_model'], options=parent['options'], limits=DEFAULT_LIMITS)
                job.update(parent_job_id=parent['id'], attention_object=obj)
                if kind == 'invoice-item':
                    from invoice_print_layout.agent_content import ContentTools
                    job['item_ids'] = [data['id']]
                    db.execute('INSERT INTO invoice_agent_effects VALUES (?,?,?)',
                               (job['id'], 'attention-resume', _json({'item_id': data['id'], 'content_base': ContentTools._base(data)})))
                    self._refresh_candidate(db, data, entries, job)
                db.execute('INSERT INTO invoice_agent_jobs VALUES (?,?,?,?)',
                           (job['id'], project, 'queued', _json(job)))
            record.update(status='excluded' if action in {'not_invoice', 'not_reimbursable'} else 'resolved', updated_at=now())
            audit = {'object': obj, 'action': action, 'values': values, 'decided_by': actor, 'executed_by': 'codex',
                     'evidence': evidence, 'expected_version': body['expected_version'],
                     'before': json.loads(before), 'after': data, 'at': now(), 'job_id': job['id'] if job else None}
            db.execute('INSERT INTO invoice_attention_audit(object,at,data) VALUES (?,?,?)', (obj, now(), _json(audit)))
            db.execute('UPDATE invoice_attention SET status=?,data=? WHERE object=?', (record['status'], _json(record), obj))
            affected = {obj} | ({'invoice-material:' + e['id'] for e in entries} if kind == 'invoice-material' else set())
            for related_obj in affected:
                db.execute("UPDATE invoice_event_outbox SET status='superseded' WHERE status IN ('pending','sending') "
                           "AND json_extract(payload,'$.object')=?", (related_obj,))
                if related_obj != obj:
                    db.execute("UPDATE invoice_attention SET status='resolved',data=json_set(data,'$.status','resolved') WHERE object=?",
                               (related_obj,))
                    db.execute('INSERT INTO invoice_attention_audit(object,at,data) VALUES (?,?,?)',
                               (related_obj, now(), _json({**audit, 'object': related_obj, 'resolved_with': obj,
                                                          'action': 'recheck_related_materials' if action != 'choose_candidate' else action})))
            label = ('Codex查证写回' if actor == 'codex' else '用户飞书确认，由Codex写回')
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (data['id'] if kind == 'invoice-item' else None, now(),
                        label + '：' + str(action) + '；办法：' + evidence['method']))
        return {'status': record['status'], 'object': obj, 'job_id': job['id'] if job else None,
                'decided_by': actor, 'executed_by': 'codex', 'verified': False}

    def _set_fields(self, db: sqlite3.Connection, kind: str, data: dict[str, Any], entries: builtins.list[dict[str, Any]],
                    values: dict[str, Any], actor: str) -> None:
        allowed = {'amount', 'expense_date', 'category', 'project_id', 'title', 'merchant'}
        if not values or set(values) - allowed or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values.values()):
            raise ValueError('只支持明确的金额、日期、类别、项目、名称和商家字段')
        if 'amount' in values and amount_cents(values['amount']) <= 0:
            raise ValueError('金额必须为正数')
        if 'category' in values and values['category'] not in CATEGORIES:
            raise ValueError('费用类别无效')
        if 'expense_date' in values and datetime.strptime(values['expense_date'], '%Y-%m-%d').strftime('%Y-%m-%d') != values['expense_date']:
            raise ValueError('日期须为YYYY-MM-DD')
        if 'project_id' in values:
            from invoice_print_layout.expense_projects import project_views
            if not any(p['id'] == values['project_id'] for p in project_views(self.store, db=db)):
                raise ValueError('项目不存在')
            if kind == 'invoice-item' and (data.get('reimbursement_person_id') or any(e.get('reimbursement_person_id') for e in entries)):
                raise ValueError('带所属人的费用请在页面调整项目和人员')
        if kind == 'invoice-item':
            merged = {**data, 'amount': '' if data.get('amount_pending') else f"{data['amount_cents']/100:.2f}", **values}
            if 'amount' in values:
                merged['amount_pending'] = False
            data.update(self.store._validate(merged))
            self._clear_conflicts(data, values)
            if any(k in values for k in ('amount', 'project_id')):
                if 'amount' in values and len(entries) != 1:
                    purchase = sum(amount_cents(e.get('fields', {}).get('amount')) for e in entries
                                   if e['role'] in {'invoice', 'receipt'})
                    payment = sum(amount_cents(e.get('fields', {}).get('amount')) for e in entries if e['role'] == 'payment')
                    if purchase != data['amount_cents'] or payment and payment != data['amount_cents']:
                        raise ValueError('多份原件不能支持这个总额，请在页面逐份补正材料')
                for entry in entries:
                    source_values = {k: v for k, v in values.items() if k == 'project_id' or k == 'amount' and len(entries) == 1}
                    if source_values:
                        self._material_fields(entry, source_values, actor)
                        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (_json(entry), entry['id']))
        else:
            if 'project_id' in values and data.get('reimbursement_person_id'):
                raise ValueError('带所属人的材料请在页面调整项目和人员')
            self._material_fields(data, values, actor)

    @staticmethod
    def _clear_conflicts(entry: dict[str, Any], fields: dict[str, Any]) -> None:
        had_conflicts = bool(entry.get('field_conflicts'))
        entry['field_conflicts'] = [c for c in entry.get('field_conflicts', []) if c.get('field') not in fields]
        from invoice_print_layout.intake_queue import FIELD_LABELS
        entry['issues'] = [issue for issue in entry.get('issues', []) if not any(
            issue in {'AI与原件字段冲突：' + field, 'AI字段未能在提取出的文字中核对：' + field,
                      'AI与本地识别的' + FIELD_LABELS.get(field, field) + '不同，请对照原件确认。'}
            for field in fields)]
        if had_conflicts and not entry['field_conflicts'] and not entry['issues']:
            ai = entry.get('ai_analysis') or {}
            if ai.get('status') == 'review':
                entry['ai_analysis'] = {**ai, 'status': 'resolved', 'resolution': 'attention_answer'}

    @staticmethod
    def _material_fields(entry: dict[str, Any], values: dict[str, Any], actor: str) -> None:
        project = values.get('project_id')
        fields = {k: v for k, v in values.items() if k != 'project_id'}
        entry['fields'] = {**entry.get('fields', {}), **fields}
        entry['field_sources'] = {**entry.get('field_sources', {}), **{k: 'attention:' + actor for k in fields}}
        entry['manual_fields'] = sorted(set(entry.get('manual_fields', [])) | set(fields))
        Attention._clear_conflicts(entry, fields)
        if project:
            entry['project_id'] = project
        entry['revision'] += 1

    def _choose(self, db: sqlite3.Connection, record: dict[str, Any], values: dict[str, Any], actor: str,
                evidence: dict[str, str]) -> builtins.list[dict[str, Any]]:
        if set(values) != {'candidate_id'}:
            raise ValueError('请选择当前上下文的candidate_id')
        choice = next((c for c in record['candidates'] if c.get('id') == values['candidate_id']), None)
        if not choice or choice.get('action', 'confirm_relation') != 'confirm_relation':
            raise ValueError('候选无效；不报请使用not_reimbursable')
        proposal = choice.get('proposal') or choice
        ids = {key.removeprefix('q:') for key in proposal.get('voucher_keys', [])}
        ids.update(proposal.get('payment_ids', [])); ids.update(proposal.get('support_ids', []))
        if not ids or record['object_id'] not in ids:
            raise ValueError('候选未包含当前对象')
        refs = {r['id']: r for r in record['question'].get('material_refs', [])}
        entries = []
        for ident in sorted(ids):
            _, entry, _ = _load(db, 'invoice-material:' + ident)
            if (ident not in refs or entry['revision'] != refs[ident]['revision'] or entry['digest'] != refs[ident]['digest'] or
                    not _editable('invoice-material', entry) or
                    entry['project_id'] != record['project_id'] or
                    _source(entry, self.store)['actual_digest'] != entry['digest']):
                raise ValueError('候选材料已变化或缺少原件快照，请重新整理')
            entries.append(entry)
        self._fact(db, record['project_id'], entries, 'same_expense', proposal.get('amount_cents'), actor, evidence)
        for entry in entries:
            entry.pop('agent_review_reason', None)
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (_json(entry), entry['id']))
            db.execute("UPDATE invoice_attention SET status='resolved',data=json_set(data,'$.status','resolved') WHERE object=?",
                       ('invoice-material:' + entry['id'],))
        return entries

    def _content(self, db: sqlite3.Connection, data: dict[str, Any], entries: builtins.list[dict[str, Any]],
                 values: dict[str, Any], actor: str, evidence: dict[str, str]) -> None:
        if (set(values) != {'goods', 'category', 'content_material_id'} or not isinstance(values['category'], str) or
                values['category'] not in CATEGORIES or
                not isinstance(values['goods'], list) or not 1 <= len(values['goods']) <= 50 or
                any(not isinstance(g, str) or not 1 <= len(g.strip()) <= 200 for g in values['goods']) or
                values['content_material_id'] not in {e['id'] for e in entries}):
            raise ValueError('需提供具体商品、有效类别及对应原件ID')
        amount = data.get('amount_cents')
        if amount is None:
            amount = amount_cents(data.get('fields', {}).get('amount'))
        fact = self._fact(db, data['project_id'], entries, 'material_content', amount, actor, evidence,
                          goods=values['goods'], category=values['category'],
                          content_digest=next(e['digest'] for e in entries if e['id'] == values['content_material_id']))
        for entry in entries:
            if entry['id'] == values['content_material_id']:
                entry.update(confirmed_content=fact, revision=entry['revision'] + 1)
                db.execute('UPDATE intake_queue SET data=? WHERE id=?', (_json(entry), entry['id']))

    @staticmethod
    def _fact(db: sqlite3.Connection, project: str, entries: builtins.list[dict[str, Any]], claim: str, amount: Any,
              actor: str, evidence: dict[str, str], **extra: Any) -> dict[str, Any]:
        from invoice_print_layout.agent_confirmed_facts import _hash as fact_hash
        if type(amount) is not int or amount <= 0:
            raise ValueError('确认事实需要原件支持的明确金额')
        fact = {'claim': claim, 'actor': actor, 'action': 'confirm_relation', 'statement': evidence['statement'],
                'provenance': {'source': 'codex_evidence' if actor == 'codex' else 'recorded_user_confirmation',
                               'reference': evidence['reference'], 'sha256': _hash(evidence), 'method': evidence['method']},
                'material_digests': sorted(e['digest'] for e in entries), 'amount_cents': amount,
                'project_id': project, 'recorded_at': time.time(), **extra}
        fact['fact_id'] = 'source-fact:' + fact_hash({k: v for k, v in fact.items() if k != 'recorded_at'})
        db.execute('INSERT OR IGNORE INTO invoice_agent_actions VALUES (?,?,?)',
                   ('project-facts:' + project, fact['fact_id'], _json(fact)))
        return fact

    @staticmethod
    def _refresh_candidate(db: sqlite3.Connection, data: dict[str, Any], entries: builtins.list[dict[str, Any]], job: dict[str, Any]) -> None:
        from invoice_print_layout.auto_verify import source_signature
        candidate = data.get('auto_verification_candidate') or {}
        if candidate.get('automatic') and set(candidate.get('source_ids', [])) == {e['id'] for e in entries}:
            candidate['source_signature'] = source_signature(entries)
            candidate['amount_cents'] = data['amount_cents']
            data['auto_verification_candidate'] = candidate
            db.execute('UPDATE expenses SET data=? WHERE id=?', (_json(data), data['id']))
            # A changed aggregate is authorized only by this exact answer, like the original registration.
            from invoice_print_layout.agent_content import ContentTools
            db.execute('UPDATE invoice_agent_effects SET data=? WHERE job_id=? AND key=?',
                       (_json({'item_id': data['id'], 'content_base': ContentTools._base(data)}), job['id'], 'attention-resume'))
