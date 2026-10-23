"""Durable, bounded invoice-agent execution. No model is trusted to stop itself."""
from __future__ import annotations

import hashlib
import builtins
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import re
import threading
import subprocess
import time
from typing import Any, Callable, Iterator, Protocol
import uuid

from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.glm_invoice_ai import GlmAIError
from invoice_print_layout import agent_metrics

FINAL = {'ready', 'needs_input', 'stopped', 'failed', 'cancelled'}
DEFAULT_LIMITS = {'models': 6, 'tools': 48, 'seconds': 180, 'stalls': 2}
LABELS = {
    'analyze_materials': '本地识别材料', 'inspect_materials': '检查组合与竞争关系',
    'read_material': '查看原文依据', 'reconcile_amount': '复核实付金额组成',
    'investigate_relations': '结合商品、金额和商家调查',
    'validate_group': '验算组合方案', 'create_draft': '整理费用草稿',
    'complete_partial_expense': '按确认补全原费用材料',
    'build_preview': '生成报销预览', 'ask_user': '需要补充事实',
    'extract_goods': '提取原件商品内容', 'classify_expense': '判断费用类别',
    'name_expense': '生成具体报销名称',
}


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ModelBudgetExceeded(ValueError):
    """All model providers share the same durable task budget."""


class Jobs:
    def __init__(self, store: ExpenseStore):
        self.store = store
        self._timing_pending: builtins.list[dict[str, Any]] = []
        self._timing_pending_lock = threading.Lock()
        self._timing_flush_lock = threading.Lock()
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS invoice_agent_jobs
                    (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS invoice_agent_requests
                    (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, job_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS invoice_agent_actions
                    (job_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(job_id,key));
                CREATE TABLE IF NOT EXISTS invoice_agent_effects
                    (job_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(job_id,key));
            """)

    def get(self, key: str) -> dict[str, Any]:
        self.flush_timings()
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        value: dict[str, Any] = json.loads(row['data'])
        return self._timing_snapshot(value)

    def list(self, project: str) -> list[dict[str, Any]]:
        self.flush_timings()
        with self.store.connect() as db:
            return [self._timing_snapshot(json.loads(row['data'])) for row in db.execute(
                'SELECT data FROM invoice_agent_jobs WHERE project_id=? ORDER BY rowid DESC LIMIT 30', (project,))]

    @staticmethod
    def _timing_snapshot(job: dict[str, Any]) -> dict[str, Any]:
        try:
            return agent_metrics.snapshot(job, time.time())
        except Exception:
            # Diagnostics cannot turn an otherwise valid old job into a failure.
            return job

    @staticmethod
    def _timing_transition(job: dict[str, Any], previous: str) -> None:
        try:
            agent_metrics.transition(job, previous, time.time())
        except Exception:
            pass

    def _record_timing(self, key: str, stage: str, seconds: float, details: dict[str, Any],
                       models_before: int | None) -> None:
        models_after = self._timing_model_count(key) if models_before is not None else None
        sample = {'key': key, 'stage': stage, 'seconds': seconds, 'outcome': details['outcome'],
                  'tool': details.get('tool'), 'cache': details.get('cache'),
                  'model_calls': max(0, models_after - models_before)
                  if models_after is not None and models_before is not None else 0}
        with self._timing_pending_lock:
            self._timing_pending.append(sample)
        self.flush_timings()

    def _timing_model_count(self, key: str) -> int | None:
        try:
            with self.store.connect() as db:
                db.execute('PRAGMA busy_timeout=0')
                row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
                return int(json.loads(row['data'])['models']) if row else None
        except Exception:
            return None

    def flush_timings(self) -> None:
        """Try a zero-wait diagnostic write; nested business locks defer it.

        Tool validation may call nested timers while owning BEGIN IMMEDIATE.
        Never inherit the business connection's 15-second write timeout here.
        Completed numeric observations wait in this Jobs instance until an
        outer timer or subsequent read can commit them together.  They never
        join, upgrade, or commit somebody else's business transaction.
        """
        if not self._timing_flush_lock.acquire(blocking=False):
            return
        pending: builtins.list[dict[str, Any]] = []
        try:
            with self._timing_pending_lock:
                pending, self._timing_pending = self._timing_pending, []
            if not pending:
                return
            try:
                with self.store.connect() as db:
                    db.execute('PRAGMA busy_timeout=0')
                    db.execute('BEGIN IMMEDIATE')
                    modified: dict[str, Any] = {}
                    for sample in pending:
                        key = sample['key']
                        if key not in modified:
                            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
                            modified[key] = json.loads(row['data']) if row else None
                        if modified[key] is not None:
                            agent_metrics.record(modified[key], **{k: v for k, v in sample.items() if k != 'key'})
                    for key, job in modified.items():
                        if job is not None:
                            db.execute('UPDATE invoice_agent_jobs SET data=? WHERE id=?', (encoded(job), key))
            except Exception:
                # Preserve order and counts across a nested write lock.  A
                # failed diagnostic transaction rolls back before requeuing.
                with self._timing_pending_lock:
                    self._timing_pending = pending + self._timing_pending
        finally:
            self._timing_flush_lock.release()

    @contextmanager
    def timed(self, key: str, stage: str, *, tool: str | None = None,
              models_before: int | None = None) -> Iterator[dict[str, Any]]:
        """Best-effort monotonic stage timer; callers may label cache/rejection.

        Labels must be fixed code identifiers.  Do not pass material text,
        arguments, model responses, paths, or user answers as labels.
        """
        try:
            started: float | None = time.perf_counter()
        except Exception:
            started = None
        details: dict[str, Any] = {'outcome': 'ok', 'tool': tool}
        if models_before is None and tool is not None:
            models_before = self._timing_model_count(key)
        try:
            yield details
        except BaseException:
            details['outcome'] = 'error'
            raise
        finally:
            try:
                if started is not None:
                    seconds = max(0.0, time.perf_counter() - started)
                    self._record_timing(key, stage, seconds, details, models_before)
            except Exception:
                # No measurement failure may alter the tool result or budgets.
                pass

    def update(self, key: str, **changes: Any) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job = json.loads(row['data'])
            # A late tool/model result cannot resurrect a cancelled or stopped run.
            if job['state'] in {'cancelled', 'stopped'} and changes.get('state', job['state']) != job['state']:
                return job  # type: ignore[no-any-return]
            previous = job['state']
            job.update(changes, updated_at=time.time())
            self._timing_transition(job, previous)
            db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?',
                       (job['state'], encoded(job), key))
        return self._timing_snapshot(job)

    def event(self, key: str, message: str, **extra: Any) -> None:
        job = self.get(key)
        events = job['events'][-79:]
        events.append({'message': message, 'at': time.time(), **extra})
        self.update(key, events=events)

    def reserve_model(self, key: str) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job = json.loads(row['data'])
            if job['state'] != 'running' or not job['use_model']:
                raise ValueError('当前任务未允许模型调用')
            spent = job['elapsed'] + max(0, time.time() - job['run_started'])
            if job['models'] >= job['limits']['models'] or spent >= job['limits']['seconds']:
                raise ModelBudgetExceeded('已达到模型调用或时间上限，现有结果已保留')
            job['models'] += 1
            db.execute('UPDATE invoice_agent_jobs SET data=? WHERE id=?', (encoded(job), key))
        return job  # type: ignore[no-any-return]

    def create(self, project: str, ids: Any, request_id: Any, *, use_model: bool = False,
               options: Any = None, limits: dict[str, int] | None = None) -> dict[str, Any]:
        from invoice_print_layout.expense_projects import ExpenseProjects
        from invoice_print_layout.intake_queue import IntakeQueue
        if not any(p['id'] == project for p in ExpenseProjects(self.store).list()):
            raise ValueError('请选择有效项目')
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 20 or
                any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError('请选择1至20份不重复材料')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id):
            raise ValueError('请提供有效任务编号')
        if type(use_model) is not bool:
            raise ValueError('模型选项无效')
        if options is None:
            options = {}
        if (not isinstance(options, dict) or set(options) - {'person', 'period'} or
                any(not isinstance(v, str) or len(v) > 100 for v in options.values())):
            raise ValueError('报销预览选项无效')
        caps = dict(DEFAULT_LIMITS if limits is None else limits)
        if set(caps) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in caps.values()):
            raise ValueError('任务额度无效')
        fingerprint = digest([project, sorted(ids), use_model, options, caps])
        queue = IntakeQueue(self.store)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM invoice_agent_requests WHERE id=?', (request_id,)).fetchone()
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('任务编号已用于另一组材料')
                return self.get(prior['job_id'])
            if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND state IN ('queued','running')",
                          (project,)).fetchone():
                raise ValueError('当前项目已有任务，请等待或取消后再开始')
            entries = [queue.get(i) for i in ids]
            if any(e['project_id'] != project or e['status'] not in {'received', 'review'} or
                   e.get('item_id') for e in entries):
                raise ValueError('材料已关联、移除或不属于当前项目，请刷新')
            job = {
                'id': uuid.uuid4().hex, 'project_id': project, 'material_ids': ids,
                'material_revisions': {e['id']: e['revision'] for e in entries},
                'state': 'queued', 'use_model': use_model, 'options': options,
                'queue_revision': 0,
                'limits': caps, 'models': 0, 'tools': 0, 'elapsed': 0.0, 'run_started': None,
                'stalls': 0, 'seen_results': [], 'events': [], 'last_results': [],
                'model_retries': 0,
                'item_ids': [], 'preview': None, 'question': None, 'approval': None, 'confirmations': [],
                'created_at': time.time(), 'updated_at': time.time(), 'message': '等待后台整理',
                'timings': agent_metrics.initial(),
            }
            db.execute('INSERT INTO invoice_agent_jobs VALUES (?,?,?,?)',
                       (job['id'], project, 'queued', encoded(job)))
            db.execute('INSERT INTO invoice_agent_requests VALUES (?,?,?)', (request_id, fingerprint, job['id']))
        return job

    def append_materials(self, key: str, ids: builtins.list[str], source_key: str) -> dict[str, Any]:
        """Resume a waiting mail task with new originals and the remaining budget."""
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or not isinstance(source_key, str)
                or not re.fullmatch(r'mail_[A-Za-z0-9_-]{8,100}', source_key)):
            raise ValueError('补充材料或邮件来源无效')
        receipt_key = 'mail-supplement:' + source_key
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job: dict[str, Any] = json.loads(row['data'])
            prior = db.execute('SELECT data FROM invoice_agent_actions WHERE job_id=? AND key=?',
                               (key, receipt_key)).fetchone()
            if prior:
                if json.loads(prior['data'])['material_ids'] != ids:
                    raise ValueError('邮件来源已经用于另一组补充材料')
                return job
            if job['state'] not in {'needs_input', 'queued'} or job['item_ids']:
                raise ValueError('当前任务不能追加材料，请保留已有结果并继续人工整理')
            if (job['models'] >= job['limits']['models'] or job['tools'] >= job['limits']['tools']
                    or job['elapsed'] >= job['limits']['seconds']):
                raise ValueError('任务额度已用完，请在人工工作台继续')
            if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND id<>? "
                          "AND state IN ('queued','running')", (job['project_id'], key)).fetchone():
                raise ValueError('当前项目还有任务正在运行')
            combined = list(dict.fromkeys([*job['material_ids'], *ids]))
            if len(combined) > 20:
                raise ValueError('单个任务最多20份材料，请在人工工作台整理')
            revisions = dict(job['material_revisions'])
            for ident in combined:
                row = db.execute('SELECT data FROM intake_queue WHERE id=?', (ident,)).fetchone()
                entry = json.loads(row['data']) if row else {}
                if (entry.get('project_id') != job['project_id'] or entry.get('item_id')
                        or entry.get('status') not in {'received', 'review'}
                        or (ident in revisions and entry.get('revision') != revisions[ident])):
                    raise ValueError('材料已修改、关联或不属于当前项目，请刷新')
                try:
                    actual = hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest()
                except OSError:
                    raise ValueError('材料原件不可读取') from None
                if actual != entry['digest']:
                    raise ValueError('材料原件已变化')
                revisions[ident] = entry['revision']
            if set(ids) <= set(job['material_ids']):
                raise ValueError('没有新的补充材料')
            previous = job['state']
            job.update(material_ids=combined, material_revisions=revisions, state='queued',
                       queue_revision=job.get('queue_revision', 0) + 1,
                       question=None, approval=None, updated_at=time.time(),
                       message='已收到邮件补充材料，按剩余额度继续整理')
            self._timing_transition(job, previous)
            db.execute('INSERT INTO invoice_agent_actions VALUES (?,?,?)',
                       (key, receipt_key, encoded({'material_ids': ids})))
            db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?',
                       ('queued', encoded(job), key))
        return job

    def cached(self, job_id: str, key: str) -> dict[str, Any] | None:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM invoice_agent_actions WHERE job_id=? AND key=?',
                             (job_id, key)).fetchone()
        return json.loads(row['data']) if row else None

    def cache(self, job_id: str, key: str, value: dict[str, Any]) -> None:
        if key.startswith('user-confirmation:'):
            raise ValueError('用户确认记录不能被工具结果覆盖')
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO invoice_agent_actions VALUES (?,?,?)', (job_id, key, encoded(value)))

    def stop(self, key: str, state: str, message: str) -> dict[str, Any]:
        job = self.get(key)
        elapsed = job['elapsed'] + (max(0.0, time.time() - job['run_started']) if job['run_started'] else 0)
        return self.update(key, state=state, elapsed=elapsed, run_started=None, message=message)

    def stop_dispatch(self, key: str, queue_revision: int, state: str, message: str) -> dict[str, Any]:
        """End only the supervised generation that actually owned the worker.

        A question may be answered before its previous process has exited. The
        accepted answer is already a new queued generation; an old process's
        exit/error/timeout must never fail it or charge it more active time.
        The generation/state check and stop share one write transaction.
        """
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job = json.loads(row['data'])
            if job.get('queue_revision', 0) != queue_revision or job['state'] not in {'queued', 'running'}:
                return self._timing_snapshot(job)
            previous = job['state']
            elapsed = job['elapsed'] + (max(0.0, time.time() - job['run_started']) if job['run_started'] else 0)
            job.update(state=state, elapsed=elapsed, run_started=None, message=message, updated_at=time.time())
            self._timing_transition(job, previous)
            db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?', (state, encoded(job), key))
        return self._timing_snapshot(job)

    def refresh_question(self, key: str, expected_updated_at: Any,
                         factory: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """Explicitly rebuild an unanswered legacy partial draft's question.

        This is a read-only evidence recheck followed by a guarded question
        replacement, not an answer, a retry, or a new task budget.
        """
        if type(expected_updated_at) not in {int, float}:
            raise ValueError('问题版本无效，请刷新')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job: dict[str, Any] = json.loads(row['data'])
            if (job['state'] != 'needs_input' or job.get('run_started') is not None or
                    job.get('updated_at') != expected_updated_at or not job.get('item_ids') or
                    job.get('approval') or (job.get('question') or {}).get('choices')):
                raise ValueError('问题已变化或当前任务不能重新检查，请刷新')
            if (job['models'] >= job['limits']['models'] or job['tools'] >= job['limits']['tools'] or
                    job['elapsed'] >= job['limits']['seconds']):
                raise ValueError('任务额度已用完，请在人工工作台继续')
            if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND id<>? "
                          "AND state IN ('queued','running')", (job['project_id'], key)).fetchone():
                raise ValueError('当前项目已有任务，请等待或取消后再检查')
            question = factory(job)
            if (not question.get('id') or question.get('type') != 'material_relation' or
                    not question.get('material_refs') or not question.get('choices')):
                raise ValueError('当前材料尚不能生成可确认的对应关系，请继续人工整理')
            job.update(question=question, message=question['text'], updated_at=time.time())
            job['events'] = [*job['events'][-79:], {'message': '已重新检查原件，等待确认对应关系',
                                                  'at': time.time()}]
            db.execute('UPDATE invoice_agent_jobs SET data=? WHERE id=?', (encoded(job), key))
        return self._timing_snapshot(job)

    def answer(self, key: str, choice: str, question_id: Any = None) -> dict[str, Any]:
        # The answer and its exact source facts are one durable transaction.
        # Neither a stale browser nor a second click can approve a newer question.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('任务不存在')
            job: dict[str, Any] = json.loads(row['data'])
            question = job.get('question') or {}
            selected = next((c for c in question.get('choices', []) if c.get('id') == choice), None)
            if (job['state'] != 'needs_input' or selected is None or
                    (question.get('id') and question_id != question['id'])):
                raise ValueError('问题已变化或选项无效，请刷新')
            # Answers add a fact, not another budget. A retry cannot reset counters.
            if (job['models'] >= job['limits']['models'] or job['tools'] >= job['limits']['tools'] or
                    job['elapsed'] >= job['limits']['seconds']):
                raise ValueError('任务额度已用完，请在人工工作台继续')
            if db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND id<>? AND state IN ('queued','running')",
                          (job['project_id'], key)).fetchone():
                raise ValueError('当前项目已有任务，请等待或取消后再回答')
            referenced = list(question.get('material_ids', []))
            selection = [k.removeprefix('q:') for k in selected.get('voucher_keys', [])]
            selection.extend(selected.get('payment_ids', []))
            selection.extend(selected.get('support_ids', []))
            if any(not isinstance(i, str) or i not in job['material_ids'] for i in [*referenced, *selection]):
                raise ValueError('确认范围已经变化，请刷新')
            refs = question.get('material_refs')
            if question.get('id'):
                if (not isinstance(refs, list) or not refs or any(not isinstance(r, dict) for r in refs) or
                        any(not isinstance(r.get('id'), str) or type(r.get('revision')) is not int or
                            not isinstance(r.get('digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', r['digest'])
                            for r in refs) or
                        len({r.get('id') for r in refs}) != len(refs) or
                        {r.get('id') for r in refs} != set(referenced) or not set(selection) <= set(referenced)):
                    raise ValueError('问题缺少原件快照，请刷新')
            else:
                # Existing persisted questions retain their original entry point.
                refs = [{'id': i, 'revision': job['material_revisions'].get(i)}
                        for i in sorted(set([*referenced, *selection]))]
            material_refs = []
            for ref in refs:
                material = db.execute('SELECT data FROM intake_queue WHERE id=?', (ref.get('id'),)).fetchone()
                entry = json.loads(material['data']) if material else {}
                owned = entry.get('item_id') in job.get('item_ids', []) and bool(entry.get('item_id'))
                if (not entry or entry.get('project_id') != job['project_id'] or
                        entry.get('revision') != ref.get('revision') or
                        (ref.get('digest') is not None and entry.get('digest') != ref['digest']) or
                        (entry.get('status') not in {'received', 'review'} and not (owned and entry.get('status') == 'linked')) or
                        (entry.get('item_id') and not owned)):
                    raise ValueError('材料已经修改、关联或移除，请刷新后重新确认')
                try:
                    actual_digest = hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest()
                except OSError:
                    raise ValueError('材料原件不可读取，请刷新') from None
                if actual_digest != entry['digest']:
                    raise ValueError('材料原件已经变化，请刷新后重新确认')
                material_refs.append({'id': entry['id'], 'revision': entry['revision'], 'digest': entry['digest']})
            ident = question.get('id') or digest(question)
            receipt_key = 'user-confirmation:' + ident
            if db.execute('SELECT 1 FROM invoice_agent_actions WHERE job_id=? AND key=?', (key, receipt_key)).fetchone():
                raise ValueError('这个问题已经回答，请刷新')
            action = selected.get('action', 'confirm_relation')
            if action not in {'confirm_relation', 'not_same_expense'}:
                raise ValueError('问题选项无效，请刷新')
            receipt = {'question_id': ident, 'snapshot': question['snapshot'], 'choice': choice,
                       'choice_data': selected, 'action': action, 'material_refs': material_refs,
                       'question_type': question.get('type'),
                       'amount_cents': (selected.get('proposal') or selected).get('amount_cents'),
                       'question_text': question.get('text', ''), 'answered_at': time.time(), 'actor': 'user'}
            db.execute('INSERT INTO invoice_agent_actions VALUES (?,?,?)', (key, receipt_key, encoded(receipt)))
            declined = action == 'not_same_expense'
            previous = job['state']
            job.update(state='stopped' if declined else 'queued',
                       queue_revision=job.get('queue_revision', 0) + (0 if declined else 1),
                       approval=None if declined else {'choice': choice, 'snapshot': question['snapshot'],
                                                       'question_id': ident, 'choice_data': selected},
                       confirmations=[*job.get('confirmations', []), receipt], question=None, stalls=0,
                       message='已记录不是同一笔，请补充订单明细或在人工工作台继续' if declined else '已收到补充，继续生成报销预览',
                       updated_at=time.time())
            self._timing_transition(job, previous)
            job['events'] = [*job['events'][-79:], {'message': '已记录用户否定对应关系' if declined else
                                                  '已记录当前材料属于同一笔费用', 'at': time.time()}]
            db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?', (job['state'], encoded(job), key))
        return job

    def _partial_progress_retry(self, job: dict[str, Any], revisions: dict[str, int]) -> str | None:
        """One explicit continuation of a committed, confirmed partial completion.

        Its new originals are already linked, so their revisions no longer
        change. Revalidate the authored content rather than demanding a fake
        edit to release an old no-progress stop. No budget is replenished.
        """
        if job.get('stalls', 0) < job['limits']['stalls']:
            return None
        with self.store.connect() as db:
            effects = [(r['key'], json.loads(r['data'])) for r in db.execute(
                "SELECT key,data FROM invoice_agent_effects WHERE job_id=? AND key LIKE 'partial-expense:%'", (job['id'],))]
            if len(effects) != 1:
                return None
            effect_key, effect = effects[0]
            receipt = next((r for r in job.get('confirmations', []) if
                r.get('actor') == 'user' and r.get('action') == 'confirm_relation' and
                r.get('choice_data', {}).get('proposal', {}).get('type') == 'complete_partial_expense' and
                effect_key == 'partial-expense:' + str(r['choice_data']['proposal'].get('id'))), None)
            if (not receipt or effect.get('revisions') != revisions or
                    effect.get('completion_id') != effect_key or job.get('item_ids') != [effect.get('item_id')]):
                return None
            retry_key = 'partial-progress-retry:' + str(receipt['question_id'])
            if db.execute('SELECT 1 FROM invoice_agent_actions WHERE job_id=? AND key=?', (job['id'], retry_key)).fetchone():
                return None
        from invoice_print_layout.agent_tools import InvoiceTools
        _, item, _, _ = InvoiceTools(self).content._load(job, effect['item_id'])
        if ((item.get('agent_relation') or {}).get('confirmation_id') != receipt['question_id'] or
                (item.get('agent_content') or {}).get('status') not in {'pending', 'extracted', 'classified'}):
            return None
        return retry_key

    def retry(self, key: str) -> dict[str, Any]:
        from invoice_print_layout.intake_queue import IntakeQueue
        job = self.get(key)
        if job['state'] == 'needs_input':
            from invoice_print_layout.agent_rule_recovery import queue_rule_recheck
            return queue_rule_recheck(self, job)
        if job['state'] not in {'failed', 'stopped'}:
            raise ValueError('任务目前不能重试')
        if (job['models'] >= job['limits']['models'] or job['tools'] >= job['limits']['tools'] or
                job['elapsed'] >= job['limits']['seconds']):
            raise ValueError('任务额度已用完，请在人工工作台继续')
        queue = IntakeQueue(self.store)
        entries = [queue.get(i) for i in job['material_ids']]
        if any(e['project_id'] != job['project_id'] for e in entries):
            raise ValueError('材料项目已变化')
        revisions = {e['id']: e['revision'] for e in entries}
        partial_retry = self._partial_progress_retry(job, revisions) if job['state'] == 'stopped' else None
        if job['state'] == 'stopped' and revisions == job['material_revisions'] and partial_retry is None:
            raise ValueError('尚无新增依据，请先补充或修正材料')
        # An explicit retry refreshes facts but never grants a fresh budget.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT state FROM invoice_agent_jobs WHERE id=?', (key,)).fetchone()
            if current is None or current['state'] not in {'failed', 'stopped'} or db.execute(
                    "SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND id<>? AND state IN ('queued','running')",
                    (job['project_id'], key)).fetchone():
                raise ValueError('项目任务已变化，请刷新')
            if partial_retry:
                db.execute('INSERT INTO invoice_agent_actions VALUES (?,?,?)',
                           (key, partial_retry, encoded({'reason': '用户确认补全后继续内容步骤；累计额度不变'})))
            job.update(state='queued', material_revisions=revisions, approval=None, question=None,
                       queue_revision=job.get('queue_revision', 0) + 1,
                       stalls=0, message='按剩余额度重试', updated_at=time.time())
            db.execute('UPDATE invoice_agent_jobs SET state=?,data=? WHERE id=?', ('queued', encoded(job), key))
        return job


class Toolbox(Protocol):
    def fingerprint(self, job: dict[str, Any]) -> str: ...
    def context(self, job: dict[str, Any]) -> dict[str, Any]: ...
    def next_action(self, job: dict[str, Any]) -> dict[str, Any] | None: ...
    def execute(self, name: str, arguments: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]: ...


class Planner(Protocol):
    def choose(self, state: dict[str, Any]) -> dict[str, Any]: ...


class AgentLoop:
    def __init__(self, jobs: Jobs):
        self.jobs = jobs

    def run(self, key: str, tools: Toolbox, planner: Planner | None = None) -> dict[str, Any]:
        job = self.jobs.get(key)
        if job['state'] != 'queued':
            return job
        self.jobs.update(key, state='running', run_started=time.time(), message='正在自动整理')
        while True:
            job = self.jobs.get(key)
            if job['state'] != 'running':
                return job
            elapsed = job['elapsed'] + time.time() - job['run_started']
            if elapsed >= job['limits']['seconds'] or job['tools'] >= job['limits']['tools']:
                return self.jobs.stop(key, 'stopped', '已达到任务额度，现有结果已保留，可打开人工工作台继续')
            try:
                with self.jobs.timed(key, 'script_plan', models_before=job['models']):
                    action = tools.next_action(job)
                if action is None:
                    if planner is None or not job['use_model']:
                        action = {'tool': 'ask_user', 'arguments': {}}
                    else:
                        if job['models'] >= job['limits']['models']:
                            # Use only a remaining tool slot to explain the missing
                            # fact. This grants neither a new model call nor a
                            # resumable answer after the durable budget is spent.
                            self.jobs.update(key, model_budget_exhausted=True)
                            self.jobs.event(key, '模型调用已达上限，保留具体补充问题')
                            action = {'tool': 'ask_user', 'arguments': {}}
                        else:
                            # Persist before network IO, including malformed/failed replies.
                            job = self.jobs.reserve_model(key)
                            self.jobs.event(key, '模型正在选择调查工具')
                            with self.jobs.timed(key, 'model_context'):
                                context = tools.context(job)
                            with self.jobs.timed(key, 'model_plan', models_before=job['models'] - 1):
                                action = planner.choose(context)
                if (not isinstance(action, dict) or set(action) != {'tool', 'arguments'} or
                        action['tool'] not in LABELS or not isinstance(action['arguments'], dict)):
                    raise ValueError('模型返回了无效的工具请求')
                name, args = action['tool'], action['arguments']
                self.jobs.update(key, tools=job['tools'] + 1, message=LABELS[name])
                self.jobs.event(key, LABELS[name], tool=name)
                # Source facts alone omit two observations that can change a
                # read-only verdict: a new model assessment and a user's answer.
                # Keep the source fingerprint stable; bind cached decisions to
                # their current observation context without granting more budget.
                with self.jobs.timed(key, 'fingerprint'):
                    action_key = digest([name, args, tools.fingerprint(job), job.get('approval'),
                                         job['models'] if name in {'inspect_materials', 'ask_user'} else None])
                with self.jobs.timed(key, 'tool', tool=name) as timing:
                    cached = self.jobs.cached(key, action_key)
                    timing['cache'] = 'hit' if cached is not None else 'miss'
                    if cached is not None:
                        result = cached
                    else:
                        try:
                            result = tools.execute(name, args, job)
                        except ValueError as exc:
                            if name not in {'validate_group', 'reconcile_amount', 'read_material', 'investigate_relations',
                                            'inspect_materials', 'extract_goods', 'classify_expense', 'name_expense'}:
                                raise
                            # A rejected investigation is an observation for the model,
                            # never an authorization to write or a reason to reset budgets.
                            timing['outcome'] = 'rejected'
                            result = {'valid': False, 'tool_error': str(exc),
                                      'next_tools': ['read_material', 'reconcile_amount', 'ask_user']}
                if cached is None:
                    self.jobs.cache(key, action_key, result)
                latest = self.jobs.get(key)
                if latest['state'] != 'running':
                    return latest
                # Identical goods in two different expenses are separate progress.
                signature = digest([name, args, result])
                progressed = signature not in latest['seen_results']
                stalls = 0 if progressed else latest['stalls'] + 1
                seen = [*latest['seen_results'], signature] if progressed else latest['seen_results']
                history = [*latest['last_results'][-7:], {'tool': name, 'result': result}]
                changes: dict[str, Any] = {'stalls': stalls, 'seen_results': seen, 'last_results': history}
                if result.get('item_id'):
                    changes['item_ids'] = sorted(set(latest['item_ids']) | {result['item_id']})
                    changes['approval'] = None
                if result.get('revisions'):
                    changes['material_revisions'] = {**latest['material_revisions'], **result['revisions']}
                if name == 'build_preview':
                    changes['preview'] = result['preview']
                    changes['item_ids'] = result.get('item_ids', latest['item_ids'])
                    changes['expenses'] = result.get('expenses', [])
                self.jobs.update(key, **changes)
                self.jobs.event(key, '验算未通过，继续查找依据' if result.get('tool_error') else
                                '已复用已有结果' if cached else '步骤完成', tool=name)
                if name == 'ask_user':
                    if latest.get('model_budget_exhausted'):
                        question = {**result['question'], 'choices': [], 'budget_exhausted': True}
                        self.jobs.update(key, question=question)
                        return self.jobs.stop(key, 'stopped', '已达到模型调用上限；' + question['text'] +
                                              '。现有结果已保留，请在人工工作台补充')
                    self.jobs.update(key, question=result['question'])
                    return self.jobs.stop(key, 'needs_input', result['question']['text'])
                if name == 'build_preview':
                    unresolved = result.get('unresolved', [])
                    self.jobs.update(key, question=result.get('question'))
                    return self.jobs.stop(key, 'needs_input' if unresolved else 'ready',
                                          '预览已准备；仍有材料需要补充' if unresolved else '报销预览已准备，请人工核对')
                if stalls >= job['limits']['stalls']:
                    return self.jobs.stop(key, 'stopped', '连续调查没有获得新证据，已停止重复尝试')
            except ModelBudgetExceeded as exc:
                return self.jobs.stop(key, 'stopped', str(exc))
            except GlmAIError as exc:
                failed = self.jobs.get(key)
                self.jobs.event(key, '模型请求未完成', error=exc.failure_code)
                if exc.failure_code == 'network_error' and failed.get('model_retries', 0) < 1:
                    self.jobs.update(key, model_retries=failed.get('model_retries', 0) + 1)
                    continue
                return self.jobs.stop(key, 'failed', '模型请求未完成，已有结果保留，可按剩余额度重试')
            except (ValueError, KeyError, TypeError, OSError) as exc:
                # Do not echo model text or transport details into the UI.
                self.jobs.event(key, '步骤未完成；原件和已有结果保留', error=type(exc).__name__)
                return self.jobs.stop(key, 'failed', '处理未完成，请刷新材料后重试，或打开人工工作台')


def run_worker(workspace: str, key: str) -> None:
    from invoice_print_layout.agent_tools import InvoiceTools, ModelPlanner
    store = ExpenseStore(Path(workspace))
    jobs = Jobs(store)
    try:
        tools = InvoiceTools(jobs)
        AgentLoop(jobs).run(key, tools, ModelPlanner(store.root))
    except Exception:
        jobs.stop(key, 'failed', '后台处理未完成，原件和已有结果保留')


def terminate_worker(process: Any) -> None:
    """Kill our Windows worker tree, including a blocked OCR/export subprocess."""
    if os.name == 'nt' and process.pid and process.is_alive():
        try:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.is_alive():
        process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join()


class AgentService:
    """One supervised child process: even an unresponsive tool has a hard deadline."""
    def __init__(self, store: ExpenseStore, *, worker: Callable[[str, str], None] = run_worker):
        self.jobs = Jobs(store)
        self._worker = worker
        self._closed = threading.Event()
        self._kick_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        with store.connect() as db:
            rows = db.execute("SELECT id FROM invoice_agent_jobs WHERE state='running'").fetchall()
        for row in rows:
            job = self.jobs.get(row['id'])
            self.jobs.stop(job['id'], 'queued', '上次处理被中断，按剩余额度继续')

    def kick(self) -> None:
        with self._kick_lock:
            if not self._closed.is_set() and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._work, daemon=True, name='invoice-agent')
                self._thread.start()

    def _idle_or_resume(self) -> bool:
        """Hand an empty worker back to kick without losing a new enqueue."""
        with self._kick_lock:
            with self.jobs.store.connect() as db:
                queued = db.execute("SELECT 1 FROM invoice_agent_jobs WHERE state='queued' LIMIT 1").fetchone()
            if queued is None:
                # A concurrent kick either sees the row during this final
                # check, or sees None here and starts its replacement thread.
                self._thread = None
                return False
        return True

    def _work(self) -> None:
        while not self._closed.is_set():
            with self.jobs.store.connect() as db:
                row = db.execute("SELECT id,data FROM invoice_agent_jobs WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                if self._idle_or_resume():
                    continue
                return
            key = row['id']
            queue_revision = json.loads(row['data']).get('queue_revision', 0)
            process = multiprocessing.get_context('spawn').Process(
                target=self._worker, args=(str(self.jobs.store.workspace), key), daemon=True)
            launched = time.time()
            started = False
            try:
                process.start()
                started = True
                while process.is_alive():
                    process.join(.15)
                    job = self.jobs.get(key)
                    if job['state'] == 'running':
                        # A queued mail supplement may have been accepted
                        # before this child started. Only a running state can
                        # establish that the child consumed that generation.
                        queue_revision = job.get('queue_revision', 0)
                    spent = job['elapsed'] + (time.time() - (job['run_started'] or launched)
                                             if job['state'] in {'queued', 'running'} else 0)
                    if self._closed.is_set() or job['state'] == 'cancelled' or spent >= job['limits']['seconds']:
                        terminate_worker(process)
                        if job['state'] not in FINAL:
                            self.jobs.stop_dispatch(key, queue_revision, 'queued' if self._closed.is_set() else 'stopped',
                                                    '后台已关闭，稍后按剩余额度继续' if self._closed.is_set() else '任务超时已强制停止，已有结果保留')
                        break
                job = self.jobs.get(key)
                if job['state'] == 'running':
                    queue_revision = job.get('queue_revision', 0)
                if job['state'] in {'running', 'queued'} and not self._closed.is_set():
                    self.jobs.stop_dispatch(key, queue_revision, 'failed', '后台被中断，已有结果保留，可在人工工作台继续')
            except Exception:
                self.jobs.stop_dispatch(key, queue_revision, 'failed', '后台无法继续，已有结果保留，请打开人工工作台')
            finally:
                if started and process.is_alive():
                    terminate_worker(process)

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(7)
