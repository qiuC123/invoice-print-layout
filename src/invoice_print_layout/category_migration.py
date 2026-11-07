"""One-time category rename; original evidence and human names are never rewritten."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

MIGRATION = '2026-11-07-express-category'
JSON_COLUMNS = (('expenses', 'data'), ('intake_queue', 'data'),
                ('material_matches', 'data'), ('category_attempts', 'result'),
                ('expense_preferences', 'data'))


def renamed_fields(value: Any, *, legacy: bool = False) -> Any:
    old_category, new_category = ('快递', '顺丰') if legacy else ('顺丰', '快递')
    old_section, new_section = ('express', 'sf') if legacy else ('sf', 'express')
    if isinstance(value, list):
        return [renamed_fields(item, legacy=legacy) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: renamed_fields(item, legacy=legacy) for key, item in value.items()}
    for key in ('category', 'managed_category', 'goods_category'):
        if result.get(key) == old_category:
            result[key] = new_category
    if result.get('report_section') == old_section:
        result['report_section'] = new_section
    if result.get('kind') == 'category' and result.get('value') == old_category:
        result['value'] = new_category
    if result.get('kind') == 'report_section' and result.get('value') == old_section:
        result['value'] = new_section
    return result


def migrate_categories(database: Path) -> None:
    with closing(sqlite3.connect(database, timeout=15)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'workbench_migrations' in tables and db.execute(
                'SELECT 1 FROM workbench_migrations WHERE name=?', (MIGRATION,)).fetchone():
            return
        updates: list[tuple[str, str, int, str]] = []
        for table, column in JSON_COLUMNS:
            if table not in tables:
                continue
            for rowid, raw in db.execute(f'SELECT rowid,{column} FROM {table}'):
                original = json.loads(raw)
                renamed = renamed_fields(original)
                if original != renamed:
                    updates.append((table, column, rowid, json.dumps(renamed, ensure_ascii=False)))
        experience = 'category_experience' in tables and db.execute(
            "SELECT 1 FROM category_experience WHERE category='顺丰' LIMIT 1").fetchone()
        backup_path = ''
        if updates or experience:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            folder = database.parent.parent / 'backups' / (MIGRATION + '-' + stamp + '-' + uuid.uuid4().hex[:8])
            folder.mkdir(parents=True, exist_ok=False)
            target = folder / database.name
            # A separate reader can back up the pre-migration state while this
            # writer holds the lock; backing up the write transaction would wait.
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(target)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('类别迁移备份校验失败；未修改类别')
            backup_path = str(target)
        db.execute('CREATE TABLE IF NOT EXISTS workbench_migrations '
                   '(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, backup_path TEXT NOT NULL)')
        for table, column, rowid, raw in updates:
            db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (raw, rowid))
        if experience:
            db.execute("UPDATE category_experience SET category='快递' WHERE category='顺丰'")
        db.execute('INSERT INTO workbench_migrations VALUES (?,?,?)',
                   (MIGRATION, datetime.now().isoformat(timespec='seconds'), backup_path))
