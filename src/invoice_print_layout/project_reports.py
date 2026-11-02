"""Idempotent project packages, rendered outside the expense transaction."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
import uuid

from invoice_print_layout import reimbursement
from invoice_print_layout.report_excel import TEMPLATE_NAME, spreadsheet_runtime, validate_template
from invoice_print_layout.workbench import ExpenseStore


class ProjectReportError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def _install(db: sqlite3.Connection) -> None:
    db.execute('CREATE TABLE IF NOT EXISTS project_report_requests ('
               'request_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, '
               'status TEXT NOT NULL, data TEXT NOT NULL)')


def _saved(db: sqlite3.Connection, request_id: str, project_id: str) -> dict[str, Any] | None:
    row = db.execute('SELECT * FROM project_report_requests WHERE request_id=?', (request_id,)).fetchone()
    if row is None:
        return None
    if row['project_id'] != project_id:
        raise ProjectReportError('request_id_conflict', '该请求编号已经用于另一个项目')
    if row['status'] == 'complete':
        return {**json.loads(row['data'])['result'], 'reused': True}
    return None


@contextmanager
def _render_lock(root: Path) -> Iterator[bool]:
    # The OS releases this lock on process exit; a stale running row is safe to resume.
    with (root / '.project-report.lock').open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def complete_request(store: ExpenseStore, db: sqlite3.Connection, request_id: str,
                     snapshots: list[dict[str, Any]], files: dict[str, str]) -> None:
    """Called inside submit_report before writes; receipt and submission commit together."""
    from invoice_print_layout.project_events import project_snapshot

    row = db.execute('SELECT * FROM project_report_requests WHERE request_id=?', (request_id,)).fetchone()
    if row is None or row['status'] != 'running':
        raise ProjectReportError('request_id_conflict', '出包请求已变化，请查询原请求结果')
    data = json.loads(row['data'])
    current = project_snapshot(store, db, row['project_id'])
    if (current['version'] != data['version'] or
            {item['id']: item['version'] for item in snapshots} != data['versions']):
        raise ProjectReportError('version_changed', '制作期间项目费用或材料已变化，请刷新项目后重试')
    result = {'ok': True, 'status': 'complete', 'request_id': request_id, 'project_id': row['project_id'],
              'version': data['version'], 'files': files, 'expense_ids': data['expense_ids'],
              'expense_count': len(data['expense_ids']), 'total_cents': data['total_cents'], 'reused': False}
    data['result'] = result
    db.execute("UPDATE project_report_requests SET status='complete',data=? WHERE request_id=?",
               (json.dumps(data, ensure_ascii=False), request_id))


def _failure(project_id: Any, request_id: Any, error: ProjectReportError) -> dict[str, Any]:
    return {'ok': False, 'status': 'failed', 'project_id': project_id, 'request_id': request_id,
            'error': {'code': error.code, 'message': str(error), 'retryable': error.retryable}}


def _report_error(error: Exception) -> ProjectReportError:
    if isinstance(error, ProjectReportError):
        return error
    message = str(error)
    if '制作期间' in message or '已变化' in message:
        return ProjectReportError('version_changed', message)
    if '模板' in message:
        return ProjectReportError('template_invalid', message, True)
    if '运行环境' in message:
        return ProjectReportError('runtime_unavailable', message, True)
    if 'Excel' in message:
        return ProjectReportError('excel_error', message, True)
    if isinstance(error, (sqlite3.Error, OSError)):
        return ProjectReportError('storage_error', '出包存储操作未完成，请重试同一请求编号', True)
    return ProjectReportError('report_error', message or '出包校验未通过')


def create_project_report(store: ExpenseStore, project_id: Any, request_id: Any,
                          expected_version: Any = None) -> dict[str, Any]:
    from invoice_print_layout.project_events import project_snapshot

    if (not isinstance(project_id, str) or not project_id or len(project_id) > 128 or
            not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request_id) or
            expected_version is not None and
            (not isinstance(expected_version, str) or not expected_version or len(expected_version) > 256)):
        return _failure(project_id, request_id, ProjectReportError('invalid_request', '项目、请求编号或版本无效'))
    reserved = False
    try:
        with store.connect() as db:
            _install(db)
            saved = _saved(db, request_id, project_id)
        if saved is not None:
            return saved
        with _render_lock(store.root) as acquired:
            if not acquired:
                return _failure(project_id, request_id, ProjectReportError('in_progress', '已有项目正在出包，请稍后重试同一请求编号', True))
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                saved = _saved(db, request_id, project_id)
                if saved is not None:
                    return saved
                try:
                    batch = project_snapshot(store, db, project_id)
                except ValueError as exc:
                    raise ProjectReportError('project_not_found', str(exc)) from exc
                if expected_version is not None and expected_version != batch['version']:
                    raise ProjectReportError('version_changed', '项目可出包费用已变化，请刷新项目后重新选择出包')
                if not batch['expense_ids']:
                    raise ProjectReportError('nothing_to_package', '该项目没有已核对、未出包的费用')
                if len(batch['expense_ids']) > 200:
                    raise ProjectReportError('too_many_expenses', '项目费用超过现有报销包200笔上限，请在工作台分批制作')
                batch['attempt_id'] = uuid.uuid4().hex
                db.execute('INSERT INTO project_report_requests VALUES (?,?,?,?) '
                           'ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,data=excluded.data',
                           (request_id, project_id, 'running', json.dumps(batch, ensure_ascii=False)))
            reserved = True
            template = store.root / TEMPLATE_NAME
            if not template.is_file():
                raise ProjectReportError('template_missing', '请先在工作台上传公司报销模板.xlsx', True)
            try:
                validate_template(template)
            except (ValueError, OSError) as exc:
                raise ProjectReportError('template_invalid', str(exc), True) from exc
            try:
                spreadsheet_runtime()
            except (ValueError, OSError) as exc:
                raise ProjectReportError('runtime_unavailable', str(exc), True) from exc
            reimbursement.create_report(store, batch['expense_ids'], include_table=True, submit_request_id=request_id)
            with store.connect() as db:
                result = _saved(db, request_id, project_id)
            if result is None:
                raise ProjectReportError('report_error', '报销包没有提交回执，请查询项目状态')
            return {**result, 'reused': False}
    except Exception as exc:
        error = _report_error(exc)
        result = _failure(project_id, request_id, error)
        if reserved:
            try:
                with store.connect() as db:
                    saved = _saved(db, request_id, project_id)
                    if saved is not None:
                        return saved
                    db.execute("UPDATE project_report_requests SET status='failed',data=json_set(data,'$.failure',json(?)) "
                               "WHERE request_id=? AND status='running' AND json_extract(data,'$.attempt_id')=?",
                               (json.dumps(result['error'], ensure_ascii=False), request_id, batch['attempt_id']))
            except sqlite3.Error:
                pass
        return result
