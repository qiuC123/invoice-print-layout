"""Stable expense ownership using the existing logistics project registry."""
from __future__ import annotations

import json
import builtins
from datetime import date
import sqlite3
from typing import Any

from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore, now


def project_views(store: ExpenseStore, *, db: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Read-only invoice names/labels; never initialize schema inside a writer."""
    from contextlib import nullcontext
    projects = ProjectRegistry(store.workspace / '后勤' / 'tasks.sqlite3').list()
    with store.connect() if db is None else nullcontext(db) as connection:
        tables = {row['name'] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta = ({row['project_id']: dict(row) for row in connection.execute('SELECT * FROM expense_project_metadata')}
                if 'expense_project_metadata' in tables else {})
        labels = ({row['project_id']: dict(row) for row in connection.execute('SELECT * FROM expense_project_reimbursement')}
                  if 'expense_project_reimbursement' in tables else {})
    return [{**project, 'name': meta.get(project['id'], {}).get('display_name') or project['name'],
             'pinned': bool(meta.get(project['id'], {}).get('pinned', False)),
             'project_revision': meta.get(project['id'], {}).get('revision', 0),
             'start_date': meta.get(project['id'], {}).get('start_date', ''),
             'end_date': meta.get(project['id'], {}).get('end_date', ''),
             'reimbursement_status': labels.get(project['id'], {}).get('status', 'unreimbursed'),
             'reimbursement_revision': labels.get(project['id'], {}).get('revision', 0)}
            for project in projects if not meta.get(project['id'], {}).get('permanently_deleted')]


def assert_project_active(db: sqlite3.Connection, project_id: str) -> None:
    if project_id and db.execute("SELECT 1 FROM sqlite_master WHERE name='expense_project_metadata'").fetchone():
        row = db.execute('SELECT permanently_deleted FROM expense_project_metadata WHERE project_id=?', (project_id,)).fetchone()
        if row and row['permanently_deleted']:
            raise ValueError('发票项目已永久删除，不能重新写入或恢复')


def assert_project_name_not_deleted(db: sqlite3.Connection, name: str) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='expense_project_metadata'").fetchone():
        for row in db.execute('SELECT display_name,deleted_data FROM expense_project_metadata WHERE permanently_deleted=1'):
            if name and name in {row['display_name'], *json.loads(row['deleted_data']).get('names', [])}:
                raise ValueError('该发票项目已永久删除，请选择其他项目')


def assert_material_not_permanently_deleted(db: sqlite3.Connection, digest: str) -> None:
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='expense_project_deleted_materials'").fetchone():
        if db.execute('SELECT 1 FROM expense_project_deleted_materials WHERE digest=?', (digest,)).fetchone():
            raise ValueError('材料所属发票项目已永久删除，原件保留，不能重新导入')


def install_project_guards(db: sqlite3.Connection) -> None:
    """Reject stale writers in the same database transaction as their writes."""
    tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'expense_project_metadata', 'expense_project_deleted_materials'}.issubset(tables):
        return
    project_tables = ('expenses', 'intake_queue', 'material_matches')
    for table in (*project_tables, 'material_creation_batches', 'expense_project_reimbursement'):
        if table not in tables:
            continue
        project = ('NEW.project_id' if table in {'material_creation_batches', 'expense_project_reimbursement'}
                   else "json_extract(NEW.data,'$.project_id')")
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f'CREATE TRIGGER IF NOT EXISTS invoice_project_{table}_{operation.lower()} '
                       f'BEFORE {operation} ON {table} WHEN EXISTS ('
                       'SELECT 1 FROM expense_project_metadata WHERE permanently_deleted=1 '
                       f'AND project_id={project}) BEGIN '
                       "SELECT RAISE(ABORT,'发票项目已永久删除，不能重新写入或恢复'); END")
    for table in ('intake_queue', 'attachments', 'automatic_ride_files'):
        if table not in tables:
            continue
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f'CREATE TRIGGER IF NOT EXISTS invoice_material_{table}_{operation.lower()} '
                       f'BEFORE {operation} ON {table} WHEN EXISTS ('
                       'SELECT 1 FROM expense_project_deleted_materials WHERE digest=NEW.digest) BEGIN '
                       "SELECT RAISE(ABORT,'材料所属发票项目已永久删除，不能重新导入'); END")
    identifiers = {'expenses': ('NEW.id', 'expense_ids'), 'intake_queue': ('NEW.id', 'intake_ids'),
                   'material_matches': ('NEW.id', 'match_ids'), 'events': ('NEW.item_id', 'expense_ids'),
                   'selections': ('NEW.item_id', 'expense_ids'), 'category_attempts': ('NEW.item_id', 'expense_ids')}
    for table, (identity, kind) in identifiers.items():
        if table not in tables:
            continue
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f'CREATE TRIGGER IF NOT EXISTS invoice_identity_{table}_{operation.lower()} '
                       f'BEFORE {operation} ON {table} WHEN EXISTS ('
                       "SELECT 1 FROM expense_project_metadata m,json_each(json_extract(m.deleted_data,'$." + kind + "')) d "
                       f'WHERE m.permanently_deleted=1 AND d.value={identity}) BEGIN '
                       "SELECT RAISE(ABORT,'所属发票项目已永久删除，旧记录不能重新写入'); END")
    if 'expenses' in tables:
        for operation in ('INSERT', 'UPDATE'):
            # A cached name-only history writer has no stable ID to check. An
            # explicitly owned active project may legitimately reuse this label.
            db.execute(f'CREATE TRIGGER IF NOT EXISTS invoice_project_name_expenses_{operation.lower()} '
                       f"BEFORE {operation} ON expenses WHEN COALESCE(json_extract(NEW.data,'$.project_id'),'')='' AND EXISTS ("
                       "SELECT 1 FROM expense_project_metadata m,json_each(json_extract(m.deleted_data,'$.names')) n "
                       "WHERE m.permanently_deleted=1 AND n.value=json_extract(NEW.data,'$.project')) BEGIN "
                       "SELECT RAISE(ABORT,'发票项目名称已永久删除，旧记录不能重新写入'); END")
    if 'expense_preferences' in tables:
        for operation in ('INSERT', 'UPDATE'):
            db.execute(f'CREATE TRIGGER IF NOT EXISTS invoice_identity_preferences_{operation.lower()} '
                       f"BEFORE {operation} ON expense_preferences WHEN json_extract(NEW.data,'$.scope')='item' AND EXISTS ("
                       "SELECT 1 FROM expense_project_metadata m,json_each(json_extract(m.deleted_data,'$.expense_ids')) d "
                       "WHERE m.permanently_deleted=1 AND d.value=json_extract(NEW.data,'$.match')) BEGIN "
                       "SELECT RAISE(ABORT,'所属发票项目已永久删除，旧费用偏好不能重新写入'); END")


class ExpenseProjects:
    def __init__(self, store: ExpenseStore):
        self.store = store
        self.registry = ProjectRegistry(store.workspace / '后勤' / 'tasks.sqlite3')
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS expense_project_reimbursement ('
                       'project_id TEXT PRIMARY KEY, status TEXT NOT NULL, '
                       'revision INTEGER NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS expense_project_metadata ('
                       'project_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT \'\', '
                       'pinned INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, '
                       'permanently_deleted INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, '
                       'deleted_data TEXT NOT NULL DEFAULT \'{}\')')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(expense_project_metadata)')}
            if not {'start_date', 'end_date'}.issubset(columns):
                db.execute('BEGIN IMMEDIATE')
                columns = {row['name'] for row in db.execute('PRAGMA table_info(expense_project_metadata)')}
                for column in ('start_date', 'end_date'):
                    if column not in columns:
                        db.execute(f"ALTER TABLE expense_project_metadata ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE TABLE IF NOT EXISTS expense_project_deleted_materials ('
                       'digest TEXT PRIMARY KEY, project_id TEXT NOT NULL)')
            install_project_guards(db)

    def list(self) -> list[dict[str, Any]]:
        return project_views(self.store)

    def create(self, name: str, site_name: str = '', start_date: str = '', end_date: str = '') -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError('请填写200字以内的项目名称')
        self._validate_period(start_date, end_date)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            projects = project_views(self.store, db=db)
            if any(project['name'] == name.strip() for project in projects):
                raise ValueError('项目名称已存在，请选择已有项目或使用不同名称')
            directory = next((row for row in self.registry.list() if row['name'] == name.strip()), None)
            if directory:
                if not any(row['id'] == directory['id'] for row in projects):
                    raise ValueError('同名项目仍保留在后勤目录，请使用不同名称创建；原发票项目已永久删除')
                raise ValueError('该名称已用于后勤项目目录，请使用不同名称创建发票项目')
            project = self.registry.create_project(name, site_name)
            db.execute('INSERT INTO expense_project_metadata (project_id,updated_at,start_date,end_date) VALUES (?,?,?,?)',
                       (project['id'], now(), start_date, end_date))
        return next(row for row in self.list() if row['id'] == project['id'])

    def _active(self, db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id:
            raise ValueError('请选择有效项目')
        assert_project_active(db, project_id)
        project = next((row for row in project_views(self.store, db=db) if row['id'] == project_id), None)
        if project is None:
            raise ValueError('项目不存在，请刷新后重试')
        return project

    @staticmethod
    def _check_revision(project: dict[str, Any], expected_revision: Any) -> None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('请提供当前发票项目版本')
        if project['project_revision'] != expected_revision:
            raise ValueError('发票项目已变化，请刷新后重试')

    @staticmethod
    def _validate_period(start_date: Any, end_date: Any) -> None:
        for value in (start_date, end_date):
            if not isinstance(value, str):
                raise ValueError('项目日期须为YYYY-MM-DD格式，或留空')
            if value:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError('项目日期须为有效的YYYY-MM-DD格式') from None
        if end_date and not start_date:
            raise ValueError('请先填写项目开始日期')
        if start_date and end_date and start_date > end_date:
            raise ValueError('项目结束日期不能早于开始日期')

    @staticmethod
    def _write_meta(db: sqlite3.Connection, project: dict[str, Any], *, deleted: bool = False,
                    deleted_data: dict[str, Any] | None = None) -> None:
        db.execute('INSERT INTO expense_project_metadata '
                   '(project_id,display_name,pinned,revision,permanently_deleted,updated_at,deleted_data,start_date,end_date) '
                   'VALUES (?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(project_id) DO UPDATE SET display_name=excluded.display_name,pinned=excluded.pinned,'
                   'revision=excluded.revision,permanently_deleted=excluded.permanently_deleted,'
                   'updated_at=excluded.updated_at,deleted_data=excluded.deleted_data,'
                   'start_date=excluded.start_date,end_date=excluded.end_date',
                   (project['id'], project['name'], int(project['pinned']), project['project_revision'] + 1,
                    int(deleted), now(), json.dumps(deleted_data or {}, ensure_ascii=False),
                    project['start_date'], project['end_date']))

    def set_period(self, project_id: str, start_date: str, end_date: str, expected_revision: Any) -> dict[str, Any]:
        self._validate_period(start_date, end_date)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._active(db, project_id)
            self._check_revision(project, expected_revision)
            project.update(start_date=start_date, end_date=end_date)
            self._write_meta(db, project)
            projects = project_views(self.store, db=db)
            saved = next(row for row in projects if row['id'] == project_id)
            overlaps = [row['name'] for row in projects if start_date and row['id'] != project_id
                        and row['reimbursement_status'] == 'unreimbursed' and row['start_date']
                        and (not end_date or row['start_date'] <= end_date)
                        and (not row['end_date'] or start_date <= row['end_date'])]
        return {**saved, 'overlaps': overlaps}

    def rename(self, project_id: str, name: Any, expected_revision: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError('请填写200字以内的项目名称')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._active(db, project_id)
            self._check_revision(project, expected_revision)
            name = name.strip()
            if any(row['id'] != project_id and row['name'] == name for row in project_views(self.store, db=db)):
                raise ValueError('项目名称已存在，请使用不同名称')
            if project['name'] != name:
                project['name'] = name
                self._write_meta(db, project)
        return next(row for row in self.list() if row['id'] == project_id)

    def pin(self, project_id: str, pinned: Any, expected_revision: Any) -> dict[str, Any]:
        if type(pinned) is not bool:
            raise ValueError('请选择有效的置顶状态')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._active(db, project_id)
            self._check_revision(project, expected_revision)
            if project['pinned'] != pinned:
                project['pinned'] = pinned
                self._write_meta(db, project)
        return next(row for row in self.list() if row['id'] == project_id)

    def delete_permanently(self, project_id: str, expected_revision: Any, *, confirmed: Any = False,
                           confirmation_name: Any = '') -> dict[str, Any]:
        """Remove only invoice ledger rows; originals and logistics survive."""
        if confirmed is not True:
            raise ValueError('请在项目永久删除确认框中确认后再执行')
        counts: dict[str, int] = {}
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            install_project_guards(db)
            project = self._active(db, project_id)
            self._check_revision(project, expected_revision)
            if not isinstance(confirmation_name, str) or confirmation_name != project['name']:
                raise ValueError('项目名称或确认内容已变化，请重新确认永久删除')
            tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            registry_projects = self.registry.list()
            canonical = next(row['name'] for row in registry_projects if row['id'] == project_id)
            other_canonical = {row['name'] for row in registry_projects if row['id'] != project_id}
            legacy_names = {canonical} | ({project['name']} if project['name'] not in other_canonical else set())
            expense_ids = {row['id'] for row in db.execute('SELECT id,data FROM expenses')
                           if json.loads(row['data']).get('project_id') == project_id or
                           (not json.loads(row['data']).get('project_id') and
                            json.loads(row['data']).get('project') in legacy_names)}
            groups = ([dict(row) for row in db.execute('SELECT id,data FROM material_matches')]
                      if 'material_matches' in tables else [])
            match_ids = {row['id'] for row in groups if json.loads(row['data']).get('project_id') == project_id or
                         json.loads(row['data']).get('item_id') in expense_ids}
            entries = ([json.loads(row['data']) for row in db.execute('SELECT data FROM intake_queue')]
                       if 'intake_queue' in tables else [])
            intake_ids = {entry['id'] for entry in entries if entry.get('project_id') == project_id or
                          (not entry.get('project_id') and (entry.get('item_id') in expense_ids or
                                                           entry.get('material_match_id') in match_ids))}
            # Unassigned QR carriers can be a source of a now-owned PDF. Follow
            # those links only within this project or its unassigned sources.
            while True:
                added = {entry['id'] for entry in entries if entry.get('project_id', '') in {'', project_id} and
                         (entry.get('replacement_id') in intake_ids or
                          any(key in intake_ids for key in entry.get('source_entry_ids', [])))}
                if added.issubset(intake_ids):
                    break
                intake_ids.update(added)
            digests = {entry['digest'] for entry in entries if entry['id'] in intake_ids}
            for table in ('attachments', 'automatic_ride_files'):
                if table in tables:
                    digests.update(row['digest'] for row in db.execute('SELECT item_id,digest FROM ' + table)
                                   if row['item_id'] in expense_ids)
            self._write_meta(db, project, deleted=True, deleted_data={
                'expense_ids': sorted(expense_ids), 'intake_ids': sorted(intake_ids), 'match_ids': sorted(match_ids),
                'names': sorted({canonical, project['name']})})

            def remove_ids(table: str, column: str, keys: set[str]) -> None:
                if table not in tables:
                    return
                counts[table] = 0
                for key in sorted(keys):
                    counts[table] += db.execute(f'DELETE FROM {table} WHERE {column}=?', (key,)).rowcount

            for table in ('attachments', 'events', 'automatic_ride_files', 'selections', 'category_attempts'):
                remove_ids(table, 'item_id', expense_ids)
            remove_ids('material_matches', 'id', match_ids)
            remove_ids('intake_queue', 'id', intake_ids)
            remove_ids('expenses', 'id', expense_ids)
            remove_ids('material_creation_batches', 'project_id', {project_id})
            remove_ids('expense_project_reimbursement', 'project_id', {project_id})
            remove_ids('reimbursement_people', 'project_id', {project_id})
            if 'expense_preferences' in tables:
                item_rules = {row['id'] for row in db.execute('SELECT id,data FROM expense_preferences')
                              if json.loads(row['data']).get('scope') == 'item' and
                              json.loads(row['data']).get('match') in expense_ids}
                remove_ids('expense_preferences', 'id', item_rules)
            shared: set[str] = set()
            for table in ('attachments', 'automatic_ride_files', 'intake_queue'):
                if table in tables:
                    shared.update(row['digest'] for row in db.execute('SELECT digest FROM ' + table))
            exclusive = digests - shared
            for digest in sorted(exclusive):
                db.execute('INSERT OR IGNORE INTO expense_project_deleted_materials VALUES (?,?)', (digest, project_id))
            counts['retained_original_identities'] = len(exclusive)
        return {'deleted': True, 'id': project_id, 'name': project['name'], 'counts': counts,
                'message': '发票项目及其费用、材料关联已永久删除；原始文件和后勤项目目录保留。'}

    def set_reimbursement_status(self, project_id: str, status: str, expected_revision: int) -> dict[str, Any]:
        """Store an explicit project label independently of logistics and expenses."""
        if not isinstance(project_id, str) or not project_id:
            raise ValueError('请选择有效项目')
        if not isinstance(status, str) or status not in {'unreimbursed', 'reimbursed'}:
            raise ValueError('项目报销分类无效，请选择未报销或已报销')
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('请提供当前项目报销分类版本')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._active(db, project_id)
            row = db.execute('SELECT status,revision FROM expense_project_reimbursement WHERE project_id=?', (project_id,)).fetchone()
            current_status, revision = (row['status'], row['revision']) if row else ('unreimbursed', 0)
            if expected_revision != revision:
                raise ValueError('项目报销分类已变化，请刷新后重试')
            if current_status != status:
                revision += 1
                db.execute('INSERT INTO expense_project_reimbursement VALUES (?,?,?,?) '
                           'ON CONFLICT(project_id) DO UPDATE SET status=excluded.status, '
                           'revision=excluded.revision,updated_at=excluded.updated_at',
                           (project_id, status, revision, now()))
                self._write_meta(db, project)
        return next(row for row in self.list() if row['id'] == project_id)

    def migrate(self) -> dict[str, int]:
        """Only bind unique existing names; preserve unknown legacy labels."""
        projects = self.list()
        active_ids = {project['id'] for project in projects}
        canonical = {project['name']: project['id'] for project in self.registry.list() if project['id'] in active_ids}
        matched = pending = 0
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT id,data FROM expenses').fetchall():
                item = json.loads(row['data'])
                if item.get('project_id'):
                    continue
                matches = [p for p in projects if p['id'] == canonical.get(item['project'])] if item['project'] in canonical else [p for p in projects if p['name'] == item['project']]
                if len(matches) != 1:
                    pending += 1
                    continue
                item['project_id'] = matches[0]['id']
                item.update(verified=False, updated_at=now())
                item.pop('review_requested', None)
                db.execute('UPDATE expenses SET data=? WHERE id=?',
                           (json.dumps(item, ensure_ascii=False), row['id']))
                matched += 1
        return {'matched': matched, 'pending': pending}

    def move(self, ids: builtins.list[str], project_id: str, versions: dict[str, str]) -> dict[str, Any]:
        project = next((p for p in self.list() if p['id'] == project_id), None)
        if (not project or not isinstance(ids, builtins.list) or not ids or
                any(not isinstance(key, str) or not key for key in ids) or
                len(ids) != len(set(ids)) or not isinstance(versions, dict)):
            raise ValueError('请选择有效项目和不重复的事项')
        changed = 0
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._active(db, project_id)
            items = [self.store.get(key) for key in ids]
            if any(item['deleted'] or item['stage'] != 'draft' or item.get('merged_into') for item in items):
                raise ValueError('只能迁移未删除的草稿事项')
            if any(versions.get(item['id']) != item['version'] for item in items):
                raise ValueError('事项已变化，请刷新后重新选择')
            for item in items:
                if item.get('project_id') == project_id and item['project'] == project['name']:
                    continue
                from invoice_print_layout.material_matching import MaterialMatching
                MaterialMatching.move_expense(db, item['id'], project)
                changed += 1
        return {'moved': changed, 'items': [self.store.get(key) for key in ids]}
