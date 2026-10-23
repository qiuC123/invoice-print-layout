"""Lightweight project directory shared with the existing logistics ledger."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid


class ProjectRegistry:
    """Read and create projects without initializing logistics services or tables."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS projects ('
                       'id TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                       'paused INTEGER NOT NULL DEFAULT 0)')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [{**json.loads(row['payload']), 'paused': bool(row['paused'])}
                    for row in db.execute('SELECT payload,paused FROM projects ORDER BY rowid')]

    def snapshot(self) -> dict[str, Any]:
        return {'projects': self.list()}

    def create_project(self, name: str, site_name: str = '') -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError('项目、会话及人员必须有明确标识')
        if not isinstance(site_name, str) or len(site_name) > 200:
            raise ValueError('工作地点应为200字以内的文本')
        name = name.strip()
        project: dict[str, Any] = {
            'id': uuid.uuid4().hex, 'name': name, 'site_name': site_name.strip(),
            'status': 'draft', **{field: '' for field in (
                'site_id', 'account_id', 'owner_id', 'responsible_id', 'group_id',
                'direct_chat_id', 'supplier_group_id', 'start', 'end')},
        }
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if any(json.loads(row['payload'])['name'] == name
                   for row in db.execute('SELECT payload FROM projects')):
                raise ValueError('项目名称已存在，请选择已有项目或使用不同名称')
            db.execute('INSERT INTO projects(id,payload) VALUES (?,?)',
                       (project['id'], json.dumps(project, ensure_ascii=False)))
        return project
