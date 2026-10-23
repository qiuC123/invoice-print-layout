"""Edit and archive retained invoice expenses without releasing their identity."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from invoice_print_layout.workbench import ExpenseStore, amount_cents, now


def _synthesis_candidate(item: dict[str, Any]) -> bool:
    """Limit material controls to unreviewed drafts with a source voucher."""
    return bool(
        item.get('stage') == 'draft' and not item.get('verified') and
        not item.get('deleted') and not item.get('merged_into') and
        not item.get('legacy') and not item.get('history_imported') and
        not any(attachment.get('role') == 'package' for attachment in item.get('attachments', [])) and
        any(attachment.get('role') in {'invoice', 'receipt'} and attachment.get('available')
            for attachment in item.get('attachments', [])) and
        (item.get('returned_to_synthesis') or not item.get('complete')))


def expense_archive_capabilities(item: dict[str, Any]) -> dict[str, bool]:
    """Describe controls; each mutation also rechecks the database association."""
    eligible = _synthesis_candidate(item)
    state = item.get('synthesis_material_state', '')
    unlinked = eligible and not item.get('material_match')
    return {
        'can_edit': eligible and state not in {'archived', 'deleted'},
        'can_archive': unlinked and state not in {'archived', 'deleted'},
        'can_restore': unlinked and state == 'archived',
        'can_delete': unlinked and state == 'archived',
        'can_restore_deleted': unlinked and state == 'deleted',
    }


class SynthesisExpenses:
    """Keep invoice source, queue linkage and attachments stable for all actions."""

    def __init__(self, store: ExpenseStore):
        self.store = store

    @staticmethod
    def _arguments(item_id: Any, expected_version: Any, project_id: Any) -> None:
        if (not isinstance(item_id, str) or not item_id.strip() or
                not isinstance(expected_version, str) or not expected_version.strip() or
                not isinstance(project_id, str) or not project_id.strip()):
            raise ValueError('请选择材料并提供当前版本及项目')

    def _current(self, db: sqlite3.Connection, item_id: str, expected_version: str,
                 project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        from invoice_print_layout.expense_projects import assert_project_active
        assert_project_active(db, project_id)
        current = self.store.get(item_id)
        if current['version'] != expected_version:
            raise ValueError('费用或材料已变化，请刷新合成台后重试')
        if current.get('project_id') != project_id:
            raise ValueError('材料不属于当前项目，请重新选择')
        row = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise ValueError('事项不存在')
        stored: dict[str, Any] = json.loads(row['data'])
        return current, stored

    @staticmethod
    def _active_group(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
        if item.get('material_match'):
            return True
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='material_matches'").fetchone():
            return False
        for row in db.execute('SELECT data FROM material_matches'):
            group = json.loads(row['data'])
            if group.get('status') != 'undone' and (
                    group.get('item_id') == item['id'] or item['id'] in group.get('member_before', {})):
                return True
        return False

    def edit_fields(self, item_id: Any, expected_version: Any, project_id: Any,
                    amount: Any, title: Any = None) -> dict[str, Any]:
        self._arguments(item_id, expected_version, project_id)
        if amount is None or isinstance(amount, str) and not amount.strip():
            raise ValueError('请确认并填写金额')
        cents = amount_cents(amount)
        if title is not None and not isinstance(title, str):
            raise ValueError('材料名称必须为文字')
        name = title.strip() if isinstance(title, str) else ''
        if len(name) > 200:
            raise ValueError('材料名称最多200字')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current, stored = self._current(db, item_id, expected_version, project_id)
            if not expense_archive_capabilities(current)['can_edit']:
                raise ValueError('仅可编辑合成台中未核对的草稿材料；归档材料请先恢复')
            from invoice_print_layout.material_matching import MaterialMatching
            MaterialMatching.preserve_edit_guard(db, item_id)
            # Old withdrawals are projected from their final event. Materialize
            # that state before this new event replaces the historical signal.
            if current.get('returned_to_synthesis'):
                stored['returned_to_synthesis'] = True
            stored.update(amount_cents=cents, amount_pending=False, verified=False, updated_at=now())
            if name:
                stored.update(title=name, display_name=name)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            message = '材料金额／用途已确认；保留原票和原件，等待合成核对'
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), message))
        return {'item': self.store.get(item_id), 'message': message}

    def transition(self, item_id: Any, expected_version: Any, project_id: Any,
                   action: Any) -> dict[str, Any]:
        self._arguments(item_id, expected_version, project_id)
        actions = {
            'archive': ('can_archive', 'archived', '材料已归档；保留原件和票据身份，不计入报销费用'),
            'restore': ('can_restore', '', '材料已恢复到合成台；等待重新确认合成'),
            'delete': ('can_delete', 'deleted', '材料已移到已删除；原件和票据身份保留，可恢复'),
            'restore-deleted': ('can_restore_deleted', 'archived', '材料已恢复到归档；保留原件和票据身份'),
        }
        if not isinstance(action, str) or action not in actions:
            raise ValueError('材料操作无效')
        capability, state, message = actions[action]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current, stored = self._current(db, item_id, expected_version, project_id)
            if self._active_group(db, current):
                raise ValueError('材料仍在已有组合中，请先撤回组合，再归档或删除')
            if not expense_archive_capabilities(current)[capability]:
                raise ValueError('材料状态不允许此操作，请刷新合成台后重试')
            stored.update(returned_to_synthesis=True, verified=False, updated_at=now())
            if state:
                stored['synthesis_material_state'] = state
            else:
                stored.pop('synthesis_material_state', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), message))
        return {'item': self.store.get(item_id), 'message': message}
