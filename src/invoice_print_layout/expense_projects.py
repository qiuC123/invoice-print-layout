"""Stable expense ownership using the existing logistics project registry."""
from __future__ import annotations

import json
import builtins
from typing import Any

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.workbench import ExpenseStore, now


class ExpenseProjects:
    def __init__(self, store: ExpenseStore):
        self.store = store
        self.registry = LogisticsStore(store.workspace / '后勤' / 'tasks.sqlite3')

    def list(self) -> list[dict[str, Any]]:
        return self.registry.snapshot()['projects']  # type: ignore[no-any-return]

    def migrate(self) -> dict[str, int]:
        """Only bind unique existing names; preserve unknown legacy labels."""
        projects = self.list()
        matched = pending = 0
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,data FROM expenses').fetchall():
                item = json.loads(row['data'])
                if item.get('project_id'):
                    continue
                matches = [p for p in projects if p['name'] == item['project']]
                if len(matches) != 1:
                    pending += 1
                    continue
                item['project_id'] = matches[0]['id']
                db.execute('UPDATE expenses SET data=? WHERE id=?',
                           (json.dumps(item, ensure_ascii=False), row['id']))
                matched += 1
        return {'matched': matched, 'pending': pending}

    def move(self, ids: builtins.list[str], project_id: str, versions: dict[str, str]) -> dict[str, Any]:
        project = next((p for p in self.list() if p['id'] == project_id), None)
        if not project or not ids or len(ids) != len(set(ids)):
            raise ValueError('请选择有效项目和不重复的事项')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            items = [self.store.get(key) for key in ids]
            if any(item['stage'] != 'draft' for item in items):
                raise ValueError('只能移动待提交事项')
            if any(versions.get(item['id']) != item['version'] for item in items):
                raise ValueError('事项已变化，请刷新后重新选择')
            for item in items:
                row = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()
                data = json.loads(row['data'])
                if data.get('project_id') == project_id and data['project'] == project['name']:
                    continue
                data.update(project_id=project_id, project=project['name'], updated_at=now(), verified=False)
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item['id']))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           (item['id'], now(), '批量归属项目：' + project['name']))
        return {'moved': len(items)}
