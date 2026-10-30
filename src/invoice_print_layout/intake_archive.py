"""Versioned material archive, retaining original files and duplicate identities."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from invoice_print_layout.intake_usage import rail_payment_not_required
from invoice_print_layout.workbench import ExpenseStore, ROLES, now


INACTIVE_STATUSES = frozenset({'archived', 'deleted', 'unused'})
ARCHIVE_ROLES = frozenset(ROLES) - {'package'}


def reject_inactive_digest(db: sqlite3.Connection, digest: str) -> None:
    """Old attachment/receipt routes must honor the durable intake archive."""
    from invoice_print_layout.expense_projects import assert_material_not_permanently_deleted
    assert_material_not_permanently_deleted(db, digest)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_queue'").fetchone():
        return
    row = db.execute('SELECT data FROM intake_queue WHERE digest=?', (digest,)).fetchone()
    entry = json.loads(row['data']) if row else {}
    if entry.get('duplicate_of'):
        raise ValueError('材料是发票图片重复件，不能单独关联费用；请使用对应发票')
    if entry.get('status') in INACTIVE_STATUSES:
        raise ValueError('材料已归档、删除或无需使用，不能关联费用；请先在归档区处理')


def archive_capabilities(entry: dict[str, Any], *, linked: bool = False) -> dict[str, bool]:
    """Only unattached intake cards expose archive actions; expenses never do."""
    safe = (not linked and not entry.get('duplicate_of') and not entry.get('item_id') and not entry.get('material_match_id') and
            not entry.get('replacement_id') and entry.get('qr_status') != 'converted' and
            entry.get('role', 'other') in ARCHIVE_ROLES)
    status = entry.get('status')
    rail = rail_payment_not_required(entry) or status == 'unused'
    previous = entry.get('archived_from') or {}
    return {'can_archive': safe and status in {'received', 'review'} and not rail,
            'can_restore': safe and status == 'archived' and not rail and
                           previous.get('status') in {'received', 'review'},
            'can_delete': safe and (status in {'archived', 'unused'} or rail and status in {'received', 'review'}),
            'can_restore_deleted': safe and status == 'deleted' and
                                   (entry.get('deleted_from') or {}).get('status') in {'archived', 'unused'}}


class IntakeArchive:
    def __init__(self, store: ExpenseStore):
        self.store = store

    @staticmethod
    def _read(db: sqlite3.Connection, key: str) -> dict[str, Any]:
        row = db.execute('SELECT data FROM intake_queue WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('收件不存在')
        return dict(json.loads(row['data']))

    def transition(self, key: str, revision: int, project_id: str, action: str) -> dict[str, Any]:
        if (not isinstance(key, str) or not key or ':' in key or
                type(revision) is not int or revision < 0 or not isinstance(project_id, str)):
            raise ValueError('请选择材料并提供对应收件版本及项目')
        if action not in {'archive', 'restore', 'delete', 'restore-deleted'}:
            raise ValueError('材料操作无效')
        # Project lookup is local and has no effect on the reimbursement label.
        if project_id:
            from invoice_print_layout.expense_projects import ExpenseProjects
            if not any(project['id'] == project_id for project in ExpenseProjects(self.store).list()):
                raise ValueError('项目不存在')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            entry = self._read(db, key)
            if entry['revision'] != revision:
                raise ValueError('材料已变化，请刷新合成台后重试')
            if entry.get('project_id', '') not in {'', project_id}:
                raise ValueError('材料属于另一项目，请切换项目后处理')
            linked = bool(db.execute(
                'SELECT 1 FROM attachments WHERE digest=? UNION SELECT 1 FROM automatic_ride_files WHERE digest=?',
                (entry['digest'], entry['digest'])).fetchone())
            if linked or entry.get('item_id') or entry.get('material_match_id'):
                raise ValueError('材料已关联费用，不能归档、删除或恢复')
            if entry.get('replacement_id') or entry.get('qr_status') == 'converted' or entry['status'] == 'converted':
                raise ValueError('二维码已对应正式发票，请处理对应发票材料')
            if entry.get('role', 'other') not in ARCHIVE_ROLES:
                raise ValueError('材料用途无效，不能执行归档操作')
            caps = archive_capabilities(entry)
            if not caps['can_' + ('restore_deleted' if action == 'restore-deleted' else action)]:
                if action == 'restore' and (entry['status'] == 'unused' or rail_payment_not_required(entry)):
                    raise ValueError('铁路支付截图不参与合成，归档后只能保留或删除')
                raise ValueError('材料状态已变化，请刷新后在对应材料区操作')
            if action == 'archive':
                entry['archived_from'] = {'status': entry['status'], 'result': entry.get('result', '')}
                entry.update(status='archived', archived_at=now(), result='材料已归档，不参与合成或计入费用')
                message = '材料已归档；原件保留，可恢复到合成台。'
            elif action == 'restore':
                previous = entry.pop('archived_from')
                entry.update(status=previous['status'], result=previous.get('result', ''))
                entry.pop('archived_at', None)
                message = '材料已恢复到合成台；保留已有识别结果。'
            elif action == 'delete':
                prior_status = 'unused' if rail_payment_not_required(entry) else entry['status']
                entry['deleted_from'] = {'status': prior_status, 'result': entry.get('result', '')}
                entry.update(status='deleted', deleted_at=now(), result='材料已删除，原件保留，可恢复到归档')
                message = '材料已移入已删除；原件保留，可恢复到归档。'
            else:
                previous = entry.pop('deleted_from')
                entry.update(status=previous['status'], result=previous.get('result', ''))
                entry.pop('deleted_at', None)
                message = '材料已恢复到归档；未进入合成材料池。'
            entry['revision'] += 1
            db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                       (json.dumps(entry, ensure_ascii=False), key))
        return {'entry': {name: value for name, value in entry.items() if name != 'path'}, 'message': message}
