"""Project-scoped manual checklist, independent of the meal scheduling ledger."""
from __future__ import annotations

import uuid
import json
import re
from datetime import date, datetime, timezone
from typing import Any

from invoice_print_layout.logistics import LogisticsStore, identifier


class ProjectTodos:
    def __init__(self, store: LogisticsStore):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS project_todos (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL, note TEXT NOT NULL, due_date TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS project_todos_project ON project_todos(project_id)')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(project_todos)')}
            if 'kind' not in columns:
                db.execute("ALTER TABLE project_todos ADD COLUMN kind TEXT NOT NULL DEFAULT 'temporary'")
                db.execute("ALTER TABLE project_todos ADD COLUMN schedule TEXT NOT NULL DEFAULT '{}'")
            if 'deleted_at' not in columns:
                db.execute("ALTER TABLE project_todos ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE TABLE IF NOT EXISTS photo_schedule_runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT NOT NULL,
                occurrence TEXT NOT NULL, state TEXT NOT NULL, job_id TEXT NOT NULL,
                message TEXT NOT NULL, UNIQUE(task_id,occurrence))''')

    @staticmethod
    def public(row: Any) -> dict[str, Any]:
        item = dict(row)
        item['schedule'] = json.loads(item['schedule'])
        return item

    def list(self, project_id: str) -> list[dict[str, Any]]:
        identifier(project_id)
        with self.store.connect() as db:
            self.store._project(db, project_id)
            items = [self.public(row) for row in db.execute(
                "SELECT * FROM project_todos WHERE project_id=? AND deleted_at='' ORDER BY status DESC, "
                "CASE WHEN due_date='' THEN 1 ELSE 0 END,due_date,created_at,id", (project_id,))]
            for item in items:
                run = db.execute('SELECT * FROM photo_schedule_runs WHERE task_id=? ORDER BY occurrence DESC LIMIT 1', (item['id'],)).fetchone()
                item['last_run'] = dict(run) if run else None
            return items

    @staticmethod
    def validate_schedule(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = body.get('kind', 'temporary')
        if kind not in ('temporary', 'scheduled'):
            raise ValueError('无效任务分类')
        if kind == 'temporary':
            return kind, {}
        config = body.get('schedule', {})
        if not isinstance(config, dict) or type(config.get('enabled')) is not bool:
            raise ValueError('请设置定时任务启用状态')
        times = config.get('times', [])
        if not isinstance(times, list) or not 1 <= len(times) <= 12 or any(
                not isinstance(t, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', t) for t in times):
            raise ValueError('请填写1—12个每日执行时间，格式为HH:MM')
        groups = config.get('group_ids', [])
        if not isinstance(groups, list) or not 1 <= len(groups) <= 20 or any(not isinstance(g, str) or not g for g in groups):
            raise ValueError('请选择照片来源群')
        for key in ('start_on', 'end_on'):
            value = config.get(key, '')
            if not isinstance(value, str) or (value and date.fromisoformat(value).isoformat() != value):
                raise ValueError('请输入有效起止日期')
        if not config.get('start_on') or (config.get('end_on') and config['end_on'] < config['start_on']):
            raise ValueError('请设置有效的执行起止日期')
        return kind, {'enabled': config['enabled'], 'times': sorted(set(times)),
                      'group_ids': sorted(set(groups)), 'start_on': config['start_on'],
                      'end_on': config.get('end_on', ''), 'action': 'read_photos', 'timezone': 'Asia/Shanghai'}

    def save(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        identifier(project_id)
        kind, config = self.validate_schedule(body)
        values = {}
        for key, limit in [('title', 200), ('note', 4000), ('due_date', 10)]:
            value = body.get(key, '')
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError(f'{key}格式或长度不正确')
            values[key] = value.strip()
        if not values['title']:
            raise ValueError('请填写待办事项')
        if values['due_date'] and date.fromisoformat(values['due_date']).isoformat() != values['due_date']:
            raise ValueError('截止日期格式应为YYYY-MM-DD')
        status = body.get('status', 'pending')
        if status not in ('pending', 'done'):
            raise ValueError('无效待办状态')
        if kind == 'scheduled':
            status = 'pending'
        item_id = body.get('id', '')
        now = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as db:
            self.store._project(db, project_id)
            if item_id:
                identifier(item_id)
                row = db.execute("SELECT * FROM project_todos WHERE id=? AND project_id=? AND deleted_at=''",
                                 (item_id, project_id)).fetchone()
                if row is None:
                    raise ValueError('本项目没有这条待办')
                if type(body.get('revision')) is not int or row['revision'] != body['revision']:
                    raise ValueError('待办已更新，请刷新后再修改')
                db.execute('UPDATE project_todos SET title=?,note=?,due_date=?,status=?,revision=revision+1,updated_at=? '
                           'WHERE id=? AND project_id=?',
                           (values['title'], values['note'], values['due_date'], status, now, item_id, project_id))
            else:
                item_id = uuid.uuid4().hex
                db.execute('INSERT INTO project_todos (id,project_id,title,note,due_date,status,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,1,?,?)',
                           (item_id, project_id, values['title'], values['note'], values['due_date'], status, now, now))
            db.execute('UPDATE project_todos SET kind=?,schedule=? WHERE id=?',
                       (kind, json.dumps(config), item_id))
            item = self.public(db.execute('SELECT * FROM project_todos WHERE id=?', (item_id,)).fetchone())
            run = db.execute('SELECT * FROM photo_schedule_runs WHERE task_id=? ORDER BY occurrence DESC LIMIT 1', (item_id,)).fetchone()
            item['last_run'] = dict(run) if run else None
            return item

    def remove(self, project_id: str, item_id: str, revision: int, *, restore: bool = False) -> dict[str, Any]:
        identifier(project_id)
        identifier(item_id)
        with self.store.connect() as db:
            self.store._project(db, project_id)
            row = db.execute('SELECT * FROM project_todos WHERE id=? AND project_id=?', (item_id, project_id)).fetchone()
            if row is None or bool(row['deleted_at']) != restore:
                raise ValueError('待办不存在或状态已变更，请刷新')
            if type(revision) is not int or row['revision'] != revision:
                raise ValueError('待办已更新，请刷新后再修改')
            config = json.loads(row['schedule'])
            if row['kind'] == 'scheduled':
                config['enabled'] = False
            now = datetime.now(timezone.utc).isoformat()
            db.execute('UPDATE project_todos SET deleted_at=?,schedule=?,revision=revision+1,updated_at=? WHERE id=?',
                       ('' if restore else now, json.dumps(config), now, item_id))
            if not restore:
                db.execute("UPDATE photo_schedule_runs SET state='cancelled',message='任务已删除，本次未读取' "
                           "WHERE task_id=? AND state='queued'", (item_id,))
            return self.public(db.execute('SELECT * FROM project_todos WHERE id=?', (item_id,)).fetchone())
