"""Persistent expense matters, independent of transient print batches."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_print_layout.storage import ensure_workspace, read_history
from invoice_print_layout.memory import _record_from_entry
from invoice_print_layout.reliability import save_json

CATEGORIES = ('材料采购', '酒店', '高铁', '外卖', '打车', '顺丰', '其他')
ROLES = {'purchase': '购买明细／收据', 'invoice': '发票', 'payment': '微信／支付宝扣费记录',
         'order': '订单记录', 'trip': '行程单', 'detail': '运单明细', 'other': '待分类材料', 'package': '历史打印包'}
STAGES = {'draft': '待提交', 'submitted': '已提交', 'reimbursed': '已报销', 'cancelled': '已取消'}
MAX_BYTES = 20 * 1024 * 1024


def now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def amount_cents(value: object) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal('999999999') or number != number.quantize(Decimal('.01')):
            raise ValueError('金额必须为非负数，最多两位小数')
        return int(number * 100)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('金额无效') from exc


def evaluate(category: str, roles: set[str], *, legacy: bool = False) -> dict[str, Any]:
    missing: list[str] = []
    if legacy and 'package' in roles:
        return {'missing': [], 'complete': True, 'alternative': False, 'basis': '历史成功打印包；报销状态未确认'}
    if category == '材料采购':
        if 'purchase' not in roles:
            missing.append('购买明细／收据')
        if not roles.intersection({'invoice', 'payment'}):
            missing.append('发票或微信／支付宝扣费记录')
    elif category in {'酒店', '高铁'}:
        if 'invoice' not in roles:
            missing.append('发票' if category == '酒店' else '12306发票')
    elif category in {'打车', '外卖', '顺丰'}:
        second = {'打车': 'trip', '外卖': 'order', '顺丰': 'detail'}[category]
        missing.extend(ROLES[role] for role in ('invoice', second) if role not in roles)
    else:
        missing.append('确认此类别的材料要求（请选择具体类别）')
    return {'missing': missing, 'complete': not missing,
            'alternative': category == '材料采购' and 'invoice' not in roles and 'payment' in roles,
            'basis': '材料存在性检查；金额、内容和真伪需人工核对'}


class ExpenseStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / '工作台'
        self.files = self.root / '附件'
        self.files.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'expenses.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS expenses (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, item_id TEXT REFERENCES expenses(id), name TEXT NOT NULL,
                    role TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
                    UNIQUE(item_id,digest,role));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, item_id TEXT, at TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS selections (owner TEXT PRIMARY KEY, item_id TEXT);
            ''')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, values: dict[str, Any], source: str | None = None) -> dict[str, Any]:
        data = self._validate(values)
        data.update(id=uuid.uuid4().hex[:10], created_at=now(), updated_at=now(), stage='draft', verified=False)
        with self.connect() as db:
            if source:
                previous = db.execute('SELECT id FROM expenses WHERE source=?', (source,)).fetchone()
                if previous:
                    return self.get(previous['id'])
            db.execute('INSERT INTO expenses VALUES (?,?,?)', (data['id'], source, json.dumps(data, ensure_ascii=False)))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (data['id'], now(), '登记事项'))
        return self.get(data['id'])

    def _validate(self, values: dict[str, Any]) -> dict[str, Any]:
        category = str(values.get('category', '其他'))
        if category not in CATEGORIES:
            raise ValueError('请选择支持的费用类别')
        result: dict[str, Any] = {'category': category}
        for key, default in [('title', ''), ('project', '待分配项目'), ('merchant', ''), ('note', ''), ('order_number', '')]:
            result[key] = str(values.get(key, default)).strip()
            if len(result[key]) > (2000 if key == 'note' else 200):
                raise ValueError('文字过长')
        if not result['title']:
            raise ValueError('请填写事项名称')
        result['project'] = result['project'] or '待分配项目'
        result['amount_cents'] = amount_cents(values.get('amount', '0'))
        expense_date = str(values.get('expense_date', date.today().isoformat())).strip()
        result['expense_date'] = date.fromisoformat(expense_date).isoformat() if expense_date else ''
        followup = str(values.get('followup_date', '')).strip()
        result['followup_date'] = date.fromisoformat(followup).isoformat() if followup else ''
        invoice_state = values.get('invoice_state', 'not_requested')
        if invoice_state not in {'not_requested', 'requested', 'unavailable'}:
            raise ValueError('开票状态无效')
        result['invoice_state'] = invoice_state
        return result

    def get(self, item_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
            if row is None:
                raise ValueError('事项不存在')
            data: dict[str, Any] = json.loads(row['data'])
            data['attachments'] = [dict(a) for a in db.execute('SELECT id,name,role,path FROM attachments WHERE item_id=?', (item_id,))]
            data['events'] = [dict(e) for e in db.execute('SELECT at,message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 30', (item_id,))]
        roles = {a['role'] for a in data['attachments'] if Path(a['path']).is_file()}
        for a in data['attachments']:
            a['available'] = Path(a.pop('path')).is_file()
        data.update(evaluate(data['category'], roles, legacy=bool(data.get('legacy'))))
        data['amount'] = f"{data['amount_cents'] / 100:.2f}"
        data['overdue'] = bool(data['followup_date'] and data['followup_date'] <= date.today().isoformat()
                               and data['stage'] == 'draft' and not data['complete'])
        data['ready'] = data['complete'] and data['verified']
        data['waiting_days'] = (date.today() - date.fromisoformat(data['created_at'][:10])).days
        return data

    def list_items(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM expenses ORDER BY rowid DESC')]
        return [self.get(key) for key in ids]

    def update(self, item_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('事项不存在')
            old = json.loads(row['data'])
            changes = self._validate(values)
            data = {**old, **changes, 'updated_at': now()}
            if any(old.get(key) != value for key, value in changes.items()):
                if old['stage'] != 'draft':
                    raise ValueError('请先退回待提交，再修改已提交的事项')
                data['verified'] = False
            if old['category'] != data['category']:
                data['legacy'] = False
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), '更新事项；内容变更后需重新核对'))
        return self.get(item_id)

    def transition(self, item_id: str, action: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(item_id)
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            if action == 'verify':
                if not current['complete'] or data['stage'] != 'draft':
                    raise ValueError('请先补齐材料，并在待提交状态下核对')
                data['verified'] = True
                label = '人工确认材料内容及金额'
            elif action in STAGES:
                if action in {'submitted', 'reimbursed'} and not current['ready']:
                    raise ValueError('请先补齐材料并核对完成')
                if action == 'reimbursed' and data['stage'] != 'submitted':
                    raise ValueError('请先标记已提交，再确认报销到账')
                data['stage'] = action
                label = '状态变更：' + STAGES[action]
            else:
                raise ValueError('不支持的状态操作')
            data['updated_at'] = now()
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), label))
        return self.get(item_id)

    def add_attachment(self, item_id: str, filename: str, payload: bytes, role: str = 'other') -> dict[str, Any]:
        if role not in ROLES or role == 'package':
            raise ValueError('附件用途无效')
        filename = Path(filename.replace('\\', '/')).name
        suffix = Path(filename).suffix.lower()
        if not payload or len(payload) > MAX_BYTES:
            raise ValueError('附件须为1字节至20MB')
        valid = ((suffix == '.pdf' and payload.startswith(b'%PDF-')) or
                 (suffix == '.png' and payload.startswith(b'\x89PNG\r\n\x1a\n')) or
                 (suffix in {'.jpg', '.jpeg'} and payload.startswith(b'\xff\xd8\xff')) or
                 (suffix == '.webp' and payload[:4] == b'RIFF' and payload[8:12] == b'WEBP'))
        if not valid:
            raise ValueError('请上传原始PDF或PNG/JPG/WebP图片，不支持ZIP和可执行文件')
        digest = hashlib.sha256(payload).hexdigest()
        target = self.files / (digest + suffix)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(item_id)
            if current['stage'] != 'draft':
                raise ValueError('请先退回待提交，再追加材料')
            if not target.exists():
                temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                temporary.write_bytes(payload)
                os.replace(temporary, target)
            db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, item_id, filename, role, str(target), digest))
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            data.update(verified=False, updated_at=now())
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), '收到材料：' + filename))
        return self.get(item_id)

    def set_role(self, attachment_id: str, role: str) -> None:
        if role not in ROLES or role == 'package':
            raise ValueError('附件用途无效')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT item_id,role FROM attachments WHERE id=?', (attachment_id,)).fetchone()
            if not row or row['role'] == 'package':
                raise ValueError('此附件不能修改用途')
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (row['item_id'],)).fetchone()['data'])
            if data['stage'] != 'draft':
                raise ValueError('请先退回待提交，再修改材料用途')
            db.execute('UPDATE attachments SET role=? WHERE id=?', (role, attachment_id))
            data.update(verified=False, updated_at=now())
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), row['item_id']))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (row['item_id'], now(), '材料用途改为：' + ROLES[role]))

    def attachment_path(self, key: str) -> tuple[Path, str]:
        with self.connect() as db:
            row = db.execute('SELECT path,name FROM attachments WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('附件不存在')
        path = Path(row['path']).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            raise ValueError('附件不可用')
        return path, row['name']

    def select(self, owner: str, item_id: str | None) -> None:
        if item_id:
            self.get(item_id)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO selections VALUES (?,?)', (owner, item_id))

    def selected(self, owner: str) -> str | None:
        with self.connect() as db:
            row = db.execute('SELECT item_id FROM selections WHERE owner=?', (owner,)).fetchone()
        return str(row['item_id']) if row and row['item_id'] else None

    def import_history(self) -> dict[str, int]:
        """Reference successful originals/print packages; never infer reimbursement."""
        added = skipped = 0
        for record in read_history(ensure_workspace(self.workspace).history):
            output = Path(str(record.get('output', ''))).resolve()
            if not output.is_relative_to(self.workspace) or not output.is_file():
                skipped += 1
                continue
            source = 'history:' + str(output)
            with self.connect() as db:
                existing = db.execute('SELECT data FROM expenses WHERE source=?', (source,)).fetchone()
                if existing and json.loads(existing['data']).get('history_imported'):
                    continue
                # Existing versions used legacy as the completion flag.
                if existing and json.loads(existing['data']).get('legacy'):
                    migrated = json.loads(existing['data'])
                    migrated['history_imported'] = True
                    db.execute('UPDATE expenses SET data=? WHERE source=?', (json.dumps(migrated, ensure_ascii=False), source))
                    continue
            normalized = _record_from_entry(record)
            category = normalized.category or '其他'
            category = {'咖啡': '外卖', '快递': '顺丰', '同城配送': '顺丰'}.get(category, category)
            if category not in CATEGORIES:
                category = '其他'
            try:
                item = self.create({'title': output.stem, 'amount': normalized.amount, 'category': category,
                                    'expense_date': normalized.invoice_date, 'merchant': normalized.provider or '',
                                    'order_number': record.get('order_number', ''),
                                    'note': '历史打印记录导入；这里日期为开票日期，请核对消费日期。未推断报销状态。'}, source)
            except ValueError:
                skipped += 1
                continue
            with self.connect() as db:
                data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
                data['legacy'] = True
                data['history_imported'] = True
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item['id']))
                db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                           (uuid.uuid4().hex, item['id'], output.name, 'package', str(output), hashlib.sha256(output.read_bytes()).hexdigest()))
                archive = Path(str(record.get('archive', ''))).resolve()
                if archive.is_relative_to(self.workspace) and archive.is_dir():
                    for file in archive.iterdir():
                        if file.is_file() and file.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}:
                            role = ('invoice' if '发票' in file.name else 'trip' if '行程' in file.name
                                    else 'detail' if '运单' in file.name else 'other')
                            db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                                       (uuid.uuid4().hex, item['id'], file.name, role, str(file), hashlib.sha256(file.read_bytes()).hexdigest()))
            added += 1
        result = {'added': added, 'skipped': skipped}
        save_json(self.root / 'history-import.json', result)
        return result
