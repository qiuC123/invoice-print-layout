"""Project-local reimbursement attribution, independent of extraction and payment."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import builtins
from typing import Any
import uuid

from invoice_print_layout.workbench import ExpenseStore, now, returned_material_state

PERSON_FIELDS = ('reimbursement_person_id', 'reimbursement_person_label')


def _display_label(person: dict[str, Any]) -> str:
    return str(person.get('display_label') or person['label'])


def _canonical_person(db: sqlite3.Connection, project_id: str, person_id: str, *,
                      people: dict[tuple[str, str], dict[str, Any]] | None = None) -> dict[str, Any]:
    seen: set[str] = set()
    while person_id and person_id not in seen:
        seen.add(person_id)
        row = (db.execute('SELECT * FROM reimbursement_people WHERE id=? AND project_id=?', (person_id, project_id)).fetchone()
               if people is None else people.get((project_id, person_id)))
        if row is None:
            raise ValueError('费用归属人不属于当前项目，请重新选择')
        person = dict(row)
        if person.get('state', 'active') != 'merged':
            return person
        person_id = str(person.get('merged_into') or '')
    raise ValueError('人员合并关系已变化，请刷新名单')


def _attribution_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    records = [record]
    records.extend(child for child in record.get('material_members', []) if isinstance(child, dict))
    for recognition in [record.get('recognition'), (record.get('material_match') or {}).get('recognition')]:
        if isinstance(recognition, dict):
            for key in ('vouchers', 'payments', 'supports'):
                records.extend(child for child in recognition.get(key, []) if isinstance(child, dict))
    return records


def _matching_person_records(group: dict[str, Any]) -> list[dict[str, Any]]:
    records = [group]
    if isinstance(group.get('bill_previous'), dict):
        records.append(group['bill_previous'])
    aggregate = group.get('aggregate_after')
    if isinstance(aggregate, dict) and isinstance(aggregate.get('data'), dict):
        records.append(aggregate['data'])
    for key in ('member_before', 'member_after'):
        records.extend(snapshot['data'] for snapshot in group.get(key, {}).values()
                       if isinstance(snapshot.get('data'), dict))
    for key in ('queue_before', 'queue_after'):
        records.extend(snapshot for snapshot in group.get(key, {}).values() if isinstance(snapshot, dict))
    records.extend(record for record in group.get('vouchers', []) if isinstance(record, dict))
    return [child for record in records for child in _attribution_records(record)]


def _document_reference(value: Any, key: str, expected: str) -> bool:
    if isinstance(value, dict):
        return value.get(key) == expected or any(_document_reference(child, key, expected) for child in value.values())
    return isinstance(value, list) and any(_document_reference(child, key, expected) for child in value)


def person_fields(record: dict[str, Any]) -> dict[str, str]:
    """Only explicit domain attribution; never infer an owner from invoice names."""
    person_id = str(record.get('reimbursement_person_id') or '')
    return {field: str(record.get(field) or '') for field in PERSON_FIELDS} if person_id else {}


def people_compatible(records: list[dict[str, Any]]) -> bool:
    return len({str(record.get('reimbursement_person_id') or '') for record in records}) <= 1


def common_person(records: list[dict[str, Any]]) -> dict[str, str]:
    if not people_compatible(records):
        raise ValueError('材料费用归属人不同或尚未指定，请先确认每份材料的费用归属人，再合成同一笔费用')
    return person_fields(records[0]) if records else {}


def current_person_label(db: sqlite3.Connection, project_id: str, person_id: str,
                         fallback: str = '', *,
                         people: dict[tuple[str, str], dict[str, Any]] | None = None) -> str:
    if not person_id or (people is None and not db.execute("SELECT 1 FROM sqlite_master WHERE name='reimbursement_people'").fetchone()):
        return fallback
    try:
        return _display_label(_canonical_person(db, project_id, person_id, people=people))
    except ValueError:
        return fallback


def validate_person(db: sqlite3.Connection, project_id: str, person_id: Any) -> dict[str, str]:
    """Read-only validation for persistent values; blank is explicit unassigned."""
    if not isinstance(person_id, str):
        raise ValueError('请选择有效费用归属人')
    if not person_id:
        return {'reimbursement_person_id': '', 'reimbursement_person_label': ''}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='reimbursement_people'").fetchone():
        raise ValueError('费用归属人不存在，请先在当前项目创建')
    person = _canonical_person(db, project_id, person_id)
    if person.get('state', 'active') != 'active':
        raise ValueError('人员已停用，请先在名单中恢复')
    return {'reimbursement_person_id': person['id'], 'reimbursement_person_label': _display_label(person)}


def allocation_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return distinct active fees, keeping the original dictionaries intact."""
    seen: set[str] = set()
    result = []
    for item in items:
        if (item['id'] in seen or item.get('deleted') or item.get('merged_into') or
                item.get('stage') == 'cancelled' or item.get('returned_to_synthesis') or
                item.get('synthesis_material_state') in {'archived', 'deleted'}):
            continue
        seen.add(item['id'])
        result.append(item)
    return result


def person_allocation(items: list[dict[str, Any]]) -> dict[str, Any]:
    """One expense ID once; exported amounts and attribution share this basis."""
    rows: dict[str, dict[str, Any]] = {}
    for item in allocation_items(items):
        person_id = str(item.get('reimbursement_person_id') or '')
        row = rows.setdefault(person_id, {'person_id': person_id,
            'label': str(item.get('reimbursement_person_label') or '未指定') if person_id else '未指定',
            'expense_count': 0, 'total_cents': 0, 'pending_review_cents': 0, 'ready_cents': 0, 'packaged_cents': 0})
        row['expense_count'] += 1
        row['total_cents'] += item['amount_cents']
        stage = 'packaged_cents' if item.get('stage') in {'submitted', 'reimbursed'} else 'ready_cents' if item.get('ready') else 'pending_review_cents'
        row[stage] += item['amount_cents']
    return {'rows': list(rows.values()), 'expense_count': sum(row['expense_count'] for row in rows.values()),
            'total_cents': sum(row['total_cents'] for row in rows.values()),
            'message': '按费用各计一次；已制包仅表示报销表已生成，不代表公司到账或已转给费用归属人。'}


class ReimbursementPeople:
    """Explicit material/expense assignments and conserved per-person totals."""

    def __init__(self, store: ExpenseStore):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS reimbursement_people (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, label TEXT NOT NULL,
                external_sender_id TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, UNIQUE(project_id,label))''')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS reimbursement_people_external ON reimbursement_people(project_id,external_sender_id) WHERE external_sender_id<>''")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(reimbursement_people)')}
            for column, definition in (
                    ('state', "TEXT NOT NULL DEFAULT 'active'"), ('merged_into', "TEXT NOT NULL DEFAULT ''"),
                    ('revision', 'INTEGER NOT NULL DEFAULT 0'), ('aliases', "TEXT NOT NULL DEFAULT '[]'"),
                    ('display_label', "TEXT NOT NULL DEFAULT ''")):
                if column not in columns:
                    db.execute(f'ALTER TABLE reimbursement_people ADD COLUMN {column} {definition}')

    def _project(self, db: sqlite3.Connection, project_id: Any) -> str:
        from invoice_print_layout.expense_projects import assert_project_active, project_views
        if not isinstance(project_id, str) or not project_id:
            raise ValueError('请先选择当前项目')
        assert_project_active(db, project_id)
        if not any(project['id'] == project_id for project in project_views(self.store, db=db)):
            raise ValueError('项目不存在，请选择当前项目')
        return project_id

    @staticmethod
    def _person(db: sqlite3.Connection, project_id: str, person_id: Any) -> dict[str, Any]:
        if not isinstance(person_id, str):
            raise ValueError('请选择有效费用归属人')
        if not person_id:
            return {'id': '', 'project_id': project_id, 'label': '未指定', 'external_sender_id': '', 'remark': ''}
        person = _canonical_person(db, project_id, person_id)
        if person.get('state', 'active') != 'active':
            raise ValueError('人员已停用，请先在名单中恢复')
        return person

    def _reference_sources(self, db: sqlite3.Connection) -> dict[str, Any]:
        tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        documents = []
        for path in (self.store.root / '导出').glob('*.json'):
            try:
                documents.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, UnicodeError, ValueError):
                continue
        return {'expenses': [json.loads(row['data']) for row in db.execute('SELECT data FROM expenses')],
                'materials': [json.loads(row['data']) for row in db.execute('SELECT data FROM intake_queue')]
                             if 'intake_queue' in tables else [],
                'groups': [json.loads(row['data']) for row in db.execute('SELECT data FROM material_matches')]
                          if 'material_matches' in tables else [], 'documents': documents}

    def _view(self, db: sqlite3.Connection, person: dict[str, Any], *,
              references: dict[str, Any] | None = None) -> dict[str, Any]:
        if references is None:
            references = self._reference_sources(db)
        result = dict(person)
        result['version'] = hashlib.sha256(json.dumps(person, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        result['source_label'] = person['label']
        result['label'] = _display_label(person)
        result['aliases'] = json.loads(person.get('aliases') or '[]')
        identities = {person['id']} if person.get('state') == 'merged' else self._source_ids(db, person['project_id'], person['id'])
        expense_records = references['expenses']
        result['expense_count'] = sum(item.get('reimbursement_person_id') in identities for item in expense_records)
        result['material_count'] = sum(entry.get('reimbursement_person_id') in identities
                                       for entry in references['materials'])
        result['snapshot_reference_count'] = sum(any(record.get('reimbursement_person_id') in identities
            for record in _attribution_records(item)[1:]) for item in expense_records if item.get('project_id') == person['project_id'])
        for group in references['groups']:
            if group.get('project_id') == person['project_id'] and any(
                    record.get('reimbursement_person_id') in identities for record in _matching_person_records(group)):
                result['snapshot_reference_count'] += 1
        for document in references['documents']:
            if (_document_reference(document, 'project_id', person['project_id']) and
                    any(_document_reference(document, 'reimbursement_person_id', identity) or
                        _document_reference(document, 'person_id', identity) for identity in identities)):
                result['snapshot_reference_count'] += 1
        result['reference_count'] = result['expense_count'] + result['material_count'] + result['snapshot_reference_count']
        result['can_retire'] = person.get('state', 'active') == 'active' and not result['reference_count']
        result['can_restore'] = person.get('state') == 'retired'
        return result

    def list(self, project_id: Any, *, retired: bool = False) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            project = self._project(db, project_id)
            rows = db.execute(
                'SELECT * FROM reimbursement_people WHERE project_id=? AND state=? ORDER BY rowid',
                (project, 'retired' if retired else 'active')).fetchall()
            references = self._reference_sources(db) if rows else {}
            return [self._view(db, dict(row), references=references) for row in rows]

    def snapshot(self, project_id: Any) -> dict[str, Any]:
        """Reuse reference scans within this response, never across later requests."""
        with self.store.connect() as db:
            project = self._project(db, project_id)
            rows = db.execute("SELECT * FROM reimbursement_people WHERE project_id=? AND state IN ('active','retired') ORDER BY rowid",
                              (project,)).fetchall()
            references = self._reference_sources(db) if rows else {}
            views = [self._view(db, dict(row), references=references) for row in rows]
        people = [person for person in views if person['state'] == 'active']
        return {'project_id': project, 'people': people,
                'retired_people': [person for person in views if person['state'] == 'retired'],
                'summary': self._summary(project, people)}

    def create(self, project_id: Any, label: Any, external_sender_id: Any = '', remark: Any = '') -> dict[str, Any]:
        if any(not isinstance(value, str) for value in (label, external_sender_id, remark)):
            raise ValueError('费用归属人名称、来源身份和备注必须为文字')
        label, external_sender_id, remark = label.strip(), external_sender_id.strip(), remark.strip()
        if not label or label == '未指定' or len(label) > 100 or len(external_sender_id) > 200 or len(remark) > 200:
            raise ValueError('请填写100字以内的费用归属人名称及200字以内的来源身份和备注')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._project(db, project_id)
            row = db.execute('SELECT * FROM reimbursement_people WHERE project_id=? AND external_sender_id=?',
                             (project, external_sender_id)).fetchone() if external_sender_id else None
            if row is None:
                row = db.execute('SELECT * FROM reimbursement_people WHERE project_id=? AND (label=? OR display_label=?)',
                                 (project, label, label)).fetchone()
            if row:
                previous = dict(row)
                if external_sender_id and previous['external_sender_id'] != external_sender_id:
                    raise ValueError('同名费用归属人的来源身份不同，请区分名称后创建')
                person = self._person(db, project, previous['id'])
                return {'person': self._view(db, person), 'created': False}
            person = {'id': uuid.uuid4().hex, 'project_id': project, 'label': label,
                      'external_sender_id': external_sender_id, 'remark': remark, 'created_at': now()}
            db.execute('INSERT INTO reimbursement_people(id,project_id,label,external_sender_id,remark,created_at) VALUES (?,?,?,?,?,?)', tuple(person.values()))
            person = dict(db.execute('SELECT * FROM reimbursement_people WHERE id=?', (person['id'],)).fetchone())
            result = self._view(db, person)
        return {'person': result, 'created': True}

    def _management_person(self, db: sqlite3.Connection, project_id: str, person_id: Any,
                           expected_version: Any) -> dict[str, Any]:
        if not isinstance(person_id, str) or not person_id or not isinstance(expected_version, str) or not expected_version:
            raise ValueError('请选择人员并提供当前名单版本')
        row = db.execute('SELECT * FROM reimbursement_people WHERE id=? AND project_id=?', (person_id, project_id)).fetchone()
        if row is None:
            raise ValueError('人员不属于当前项目，请重新选择')
        person = dict(row)
        if self._view(db, person)['version'] != expected_version:
            raise ValueError('人员名单已变化，请刷新后重试')
        return person

    def rename(self, project_id: Any, person_id: Any, expected_version: Any, label: Any) -> dict[str, Any]:
        if not isinstance(label, str) or not label.strip() or label.strip() == '未指定' or len(label.strip()) > 100:
            raise ValueError('请填写100字以内的人员姓名')
        label = label.strip()
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._project(db, project_id)
            person = self._management_person(db, project, person_id, expected_version)
            if person['state'] != 'active':
                raise ValueError('仅可修改当前名单中的人员姓名')
            previous_label = _display_label(person)
            if previous_label == label:
                return {'person': self._view(db, person), 'message': '姓名未变化。'}
            for row in db.execute('SELECT id FROM reimbursement_people WHERE project_id=? AND (label=? OR display_label=?)',
                                  (project, label, label)):
                if _canonical_person(db, project, row['id'])['id'] != person_id:
                    raise ValueError('该姓名已有人员记录，请选择已有人员归并')
            aliases = list(dict.fromkeys([*json.loads(person['aliases']), previous_label]))
            db.execute('UPDATE reimbursement_people SET display_label=?,aliases=?,revision=revision+1 WHERE id=?',
                       (label, json.dumps(aliases, ensure_ascii=False), person_id))
            current = dict(db.execute('SELECT * FROM reimbursement_people WHERE id=?', (person_id,)).fetchone())
            self._propagate(db, project, self._source_ids(db, project, person_id), current,
                            '费用归属人更名：' + previous_label + ' → ' + label)
            result = self._view(db, current)
        return {'person': result, 'message': '姓名已修改，当前费用及材料标签已同步；原件和已生成报销包不变。'}

    @staticmethod
    def _source_ids(db: sqlite3.Connection, project_id: str, canonical_id: str) -> set[str]:
        return {row['id'] for row in db.execute('SELECT id FROM reimbursement_people WHERE project_id=?', (project_id,))
                if _canonical_person(db, project_id, row['id'])['id'] == canonical_id}

    def merge(self, project_id: Any, person_id: Any, expected_version: Any,
              target_person_id: Any, target_expected_version: Any) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._project(db, project_id)
            source = self._management_person(db, project, person_id, expected_version)
            target = self._management_person(db, project, target_person_id, target_expected_version)
            if source['id'] == target['id'] or source['state'] != 'active' or target['state'] != 'active':
                raise ValueError('请选择当前项目中另一名有效人员，不能归并自己或已停用人员')
            source_ids = self._source_ids(db, project, source['id'])
            aliases = list(dict.fromkeys([*json.loads(target['aliases']), *json.loads(source['aliases']),
                                         source['label'], _display_label(source)]))
            db.execute('UPDATE reimbursement_people SET aliases=?,revision=revision+1 WHERE id=?',
                       (json.dumps(aliases, ensure_ascii=False), target['id']))
            db.execute("UPDATE reimbursement_people SET state='merged',merged_into=?,revision=revision+1 WHERE id=?",
                       (target['id'], source['id']))
            target = dict(db.execute('SELECT * FROM reimbursement_people WHERE id=?', (target['id'],)).fetchone())
            self._propagate(db, project, source_ids, target, '费用归属人归并：' + _display_label(source) + ' → ' + _display_label(target))
            source = dict(db.execute('SELECT * FROM reimbursement_people WHERE id=?', (source['id'],)).fetchone())
            result, previous = self._view(db, target), self._view(db, source)
        return {'person': result, 'target_person': result, 'source_person': previous,
                'message': '人员已归并，费用各计一次；原外部身份及别名保留，已生成报销包不变。'}

    def retire(self, project_id: Any, person_id: Any, expected_version: Any, *, restore: bool = False) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._project(db, project_id)
            person = self._management_person(db, project, person_id, expected_version)
            view = self._view(db, person)
            if restore:
                if person['state'] != 'retired':
                    raise ValueError('仅可恢复已停用人员')
            elif not view['can_retire']:
                raise ValueError('人员已有费用或材料，请选择已有人员归并；仅空人员可移除')
            db.execute('UPDATE reimbursement_people SET state=?,revision=revision+1 WHERE id=?',
                       ('active' if restore else 'retired', person_id))
            result = self._view(db, dict(db.execute('SELECT * FROM reimbursement_people WHERE id=?', (person_id,)).fetchone()))
        return {'person': result, 'message': '人员已恢复到当前名单。' if restore else '空人员已从名单移除，可在已移除人员中恢复。'}

    def _propagate(self, db: sqlite3.Connection, project_id: str, source_ids: set[str],
                   person: dict[str, Any], message: str) -> None:
        """Change current attribution only, rebasing pristine undo ownership."""
        from invoice_print_layout.material_matching import MaterialMatching
        fields, timestamp = self._fields(person), now()
        expenses = {row['id']: json.loads(row['data']) for row in db.execute('SELECT id,data FROM expenses')
                    if any(record.get('reimbursement_person_id') in source_ids for record in _attribution_records(json.loads(row['data'])))}
        if any(item.get('project_id') != project_id for item in expenses.values()):
            raise ValueError('费用归属记录不属于当前项目，未修改其他项目')
        queues: dict[str, dict[str, Any]] = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
            queues = {row['id']: json.loads(row['data']) for row in db.execute('SELECT id,data FROM intake_queue')
                      if json.loads(row['data']).get('reimbursement_person_id') in source_ids}
            if any(entry.get('project_id') not in {'', project_id} for entry in queues.values()):
                raise ValueError('材料归属记录不属于当前项目，未修改其他项目')
        groups = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='material_matches'").fetchone():
            for row in db.execute('SELECT data FROM material_matches').fetchall():
                group = json.loads(row['data'])
                if group.get('project_id') != project_id or group.get('status') == 'undone':
                    continue
                involved = {group.get('item_id'), *group.get('member_before', {})}.intersection(expenses)
                historical = any(record.get('reimbursement_person_id') in source_ids for record in _matching_person_records(group))
                if not involved and not historical and not set(group.get('queue_before', {})).intersection(queues):
                    continue
                pristine = True
                try:
                    MaterialMatching._assert_group_snapshot(db, group)
                except ValueError:
                    pristine = False
                groups.append((group, pristine))
        events = {}
        for key, data in expenses.items():
            if returned_material_state(db, data):
                data['returned_to_synthesis'] = True
            data.update(updated_at=timestamp)
            for member in _attribution_records(data):
                if member.get('reimbursement_person_id') in source_ids:
                    member.update(**fields)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
            cursor = db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                                (key, timestamp, message + '；金额、分类、日期及核对状态不变'))
            events[key] = dict(db.execute('SELECT * FROM events WHERE id=?', (cursor.lastrowid,)).fetchone())
        for key, entry in queues.items():
            entry.update(**fields, revision=entry['revision'] + 1)
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry, ensure_ascii=False), key))
            if not entry.get('item_id'):
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           ('intake:' + key, timestamp, message + '；原始提交身份及识别字段保留'))
        for group, pristine in groups:
            # Identity changes are intentional even in stale ownership records;
            # their content, revisions and event mismatches remain untouched.
            for record in _matching_person_records(group):
                if record.get('reimbursement_person_id') in source_ids:
                    record.update(**fields)
            if pristine:
                for key, snapshot in [(group['item_id'], group.get('aggregate_after')),
                                      *group.get('member_after', {}).items()]:
                    if snapshot and key in expenses:
                        snapshot['data'] = expenses[key]
                        snapshot['events'].append(events[key])
                for snapshot in group.get('member_before', {}).values():
                    if snapshot['data'].get('reimbursement_person_id') in source_ids:
                        snapshot['data'].update(**fields)
                previous = group.get('bill_previous')
                if previous and previous.get('reimbursement_person_id') in source_ids:
                    previous.update(**fields)
                for key, snapshot in group.get('queue_after', {}).items():
                    if key in queues:
                        snapshot.update(**fields, revision=queues[key]['revision'])
                for snapshot in group.get('queue_before', {}).values():
                    if snapshot.get('reimbursement_person_id') in source_ids:
                        snapshot.update(**fields)
                for member in group.get('vouchers', []):
                    if member.get('reimbursement_person_id') in source_ids:
                        member.update(**fields)
            db.execute('UPDATE material_matches SET data=? WHERE id=?',
                       (json.dumps(group, ensure_ascii=False), group['id']))

    @staticmethod
    def _fields(person: dict[str, Any]) -> dict[str, str]:
        return {'reimbursement_person_id': person['id'],
                'reimbursement_person_label': _display_label(person) if person['id'] else ''}

    def _assignment_person(self, db: sqlite3.Connection, project: str, person_id: Any,
                           target_version: Any, *, required: bool = False) -> dict[str, Any]:
        person = self._person(db, project, person_id)
        if person['id'] and (required or target_version is not None):
            if not isinstance(target_version, str) or not target_version or self._view(db, person)['version'] != target_version:
                raise ValueError('人员名单已变化，请刷新后重试')
        elif not person['id'] and target_version not in (None, ''):
            raise ValueError('未指定人员时无需提供人员版本')
        return person

    def assign_materials(self, project_id: Any, ids: Any, revisions: Any, person_id: Any,
                         target_version: Any = None) -> dict[str, Any]:
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 200 or
                any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids) or not isinstance(revisions, dict) or
                set(revisions) != set(ids) or any(type(value) is not int for value in revisions.values())):
            raise ValueError('请选择不重复的材料并提供对应收件版本')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._project(db, project_id)
            person = self._assignment_person(db, project, person_id, target_version)
            entries = []
            previous_labels: dict[str, str] = {}
            for key in ids:
                row = db.execute('SELECT data FROM intake_queue WHERE id=?', (key,)).fetchone()
                if row is None:
                    raise ValueError('收件不存在')
                entry = json.loads(row['data'])
                if entry.get('project_id') not in {'', project}:
                    raise ValueError('材料不属于当前项目')
                if entry['revision'] != revisions[key]:
                    raise ValueError('材料已变化，请刷新后重新指定费用归属人')
                if entry.get('status') not in {'received', 'review'} or entry.get('item_id'):
                    raise ValueError('只能指定未关联的待处理材料；已创建费用请在待核对中修改费用归属人')
                previous_labels[key] = str(entry.get('reimbursement_person_label') or '未指定')
                entry.update(**self._fields(person), project_id=project, revision=entry['revision'] + 1)
                entries.append(entry)
            for entry in entries:
                db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry, ensure_ascii=False), entry['id']))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           ('intake:' + entry['id'], now(), '指定材料费用归属人：' + previous_labels[entry['id']] + ' → ' + _display_label(person) + '；金额及识别字段不变'))
            public_person = self._view(db, person) if person['id'] else person
        return {'entries': entries, 'person': public_person}

    @staticmethod
    def _validate_expense_assignment(current: dict[str, Any], project: str, expected_version: str) -> None:
        if current.get('project_id') != project:
            raise ValueError('费用不属于当前项目，请重新选择')
        if current['version'] != expected_version:
            raise ValueError('费用已变化，请刷新后重新指定费用归属人')
        if (current['stage'] != 'draft' or current.get('deleted') or current.get('merged_into') or
                current.get('verified') or current.get('legacy') or current.get('history_imported') or
                current.get('returned_to_synthesis') or
                current.get('synthesis_material_state') in {'archived', 'deleted'}):
            raise ValueError('只能修改尚未核对的草稿费用费用归属人')

    def assign_expense(self, item_id: Any, expected_version: Any, person_id: Any,
                       target_version: Any = None) -> dict[str, Any]:
        if not isinstance(item_id, str) or not item_id or not isinstance(expected_version, str) or not expected_version:
            raise ValueError('请选择费用并提供当前版本')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.store.get(item_id)
            project = self._project(db, current.get('project_id'))
            person = self._assignment_person(db, project, person_id, target_version)
            self._validate_expense_assignment(current, project, expected_version)
            self._assign_expense_in_transaction(db, current, person)
            public_person = self._view(db, person) if person['id'] else person
        return {'item': self.store.get(item_id), 'person': public_person}

    def assign_expenses(self, project_id: Any, ids: Any, versions: Any, person_id: Any,
                        target_version: Any = None) -> dict[str, Any]:
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 200 or
                any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids) or
                not isinstance(versions, dict) or set(versions) != set(ids) or
                any(not isinstance(value, str) or not value for value in versions.values())):
            raise ValueError('请选择1至200笔不重复费用，并提供每笔费用的完整当前版本')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            project = self._project(db, project_id)
            person = self._assignment_person(db, project, person_id, target_version, required=True)
            items = [self.store.get(key) for key in ids]
            for item in items:
                self._validate_expense_assignment(item, project, versions[item['id']])
            for item in items:
                self._assign_expense_in_transaction(db, item, person, validate_only=True)
            changed = sum(self._assign_expense_in_transaction(db, item, person) for item in items)
            public_person = self._view(db, person) if person['id'] else person
        return {'items': [self.store.get(key) for key in ids], 'person': public_person, 'changed': changed,
                'message': f'已指定{changed}笔费用的费用归属人；金额和费用笔数不变。' if changed else '所选费用已属于该人员，没有重复修改。'}

    def _assign_expense_in_transaction(self, db: sqlite3.Connection, current: dict[str, Any], person: dict[str, Any],
                                       *, validate_only: bool = False) -> bool:
        from invoice_print_layout.material_matching import MaterialMatching
        item_id, fields = current['id'], self._fields(person)
        stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
        group_id = stored.get('material_match', {}).get('group_id')
        group_row = db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone() if group_id else None
        if group_id and not group_row:
            raise ValueError('材料组合不存在，请刷新后重试')
        group = json.loads(group_row['data']) if group_row else None
        rebase = False
        if group:
            try:
                MaterialMatching._assert_group_snapshot(db, group)
                rebase = True
            except ValueError:
                pass
        member_ids = list(group.get('member_before', {})) if group else []
        states = {key: MaterialMatching._member_state(db, key) for key in dict.fromkeys([item_id, *member_ids])}
        for key, state in states.items():
            if key != item_id and state['data'].get('merged_into') != item_id:
                raise ValueError('合并成员已变化，请刷新后重试')
        entries = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
            entries = [json.loads(row['data']) for row in db.execute('SELECT data FROM intake_queue').fetchall()
                       if json.loads(row['data']).get('item_id') in states]
        for entry in entries:
            if group and entry['id'] in group.get('queue_before', {}) and entry.get('material_match_id') != group_id:
                raise ValueError('组合收件关联已变化，请刷新后重试')
        records = [record for state in states.values() for record in _attribution_records(state['data'])
                   if record is state['data'] or 'reimbursement_person_id' in record]
        records.extend(entries)
        if group and rebase:
            records.extend(record for record in _matching_person_records(group) if 'reimbursement_person_id' in record)
        if all(all(str(record.get(key) or '') == value for key, value in fields.items()) for record in records):
            return False
        if validate_only:
            return True
        MaterialMatching.preserve_edit_guard(db, item_id)
        if group and not group.get('aggregate_after'):
            group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()['data'])
        if returned_material_state(db, stored):
            stored['returned_to_synthesis'] = True
        timestamp = now()
        for key, state in states.items():
            data = dict(state['data'])
            if key == item_id and stored.get('returned_to_synthesis'):
                data['returned_to_synthesis'] = True
            data.update(**fields, updated_at=timestamp)
            for record in _attribution_records(data)[1:]:
                if 'reimbursement_person_id' in record:
                    record.update(**fields)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
            cursor = db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                                (key, timestamp, '指定费用费用归属人：' + str(state['data'].get('reimbursement_person_label') or '未指定') + ' → ' + _display_label(person) + '；金额、分类及核对状态不变'))
            event = dict(db.execute('SELECT * FROM events WHERE id=?', (cursor.lastrowid,)).fetchone())
            if group and rebase:
                for snapshot in [group.get('aggregate_after') if key == item_id else None, group.get('member_after', {}).get(key)]:
                    if snapshot:
                        snapshot['data'].update(**fields, updated_at=timestamp)
                        snapshot['events'].append(event)
                original = group.get('member_before', {}).get(key)
                if original:
                    original['data'].update(**fields)
        for entry in entries:
            entry.update(**fields, revision=entry['revision'] + 1)
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry, ensure_ascii=False), entry['id']))
            if group and rebase:
                expected = group.get('queue_after', {}).get(entry['id'])
                if expected:
                    expected.update(**fields, revision=expected['revision'] + 1)
                original = group.get('queue_before', {}).get(entry['id'])
                if original:
                    original.update(**fields)
        if group and rebase:
            if group.get('bill_previous'):
                group['bill_previous'].update(**fields)
            for record in _matching_person_records(group):
                if 'reimbursement_person_id' in record:
                    record.update(**fields)
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group_id))
        return True

    def summary(self, project_id: Any) -> dict[str, Any]:
        return self._summary(project_id, self.list(project_id))

    def _summary(self, project_id: Any, people: builtins.list[dict[str, Any]]) -> dict[str, Any]:
        rows = {person['id']: {'person_id': person['id'], 'label': person['label'], 'expense_count': 0,
                'total_cents': 0, 'pending_review_cents': 0, 'ready_cents': 0, 'packaged_cents': 0} for person in people}
        rows[''] = {'person_id': '', 'label': '未指定', 'expense_count': 0, 'total_cents': 0,
                    'pending_review_cents': 0, 'ready_cents': 0, 'packaged_cents': 0}
        allocation = person_allocation([item for item in self.store.list_items(include_deleted=True)
                                        if item.get('project_id') == project_id])
        rows.update({row['person_id']: row for row in allocation['rows']})
        return {**allocation, 'project_id': project_id, 'rows': list(rows.values())}
