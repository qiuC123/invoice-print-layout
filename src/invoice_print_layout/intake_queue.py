"""Durable local intake; association is atomic and never implies verification."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid

import pymupdf

from invoice_print_layout.receipt_intake import validate_image, parse_receipt, read_receipt
from invoice_print_layout.workbench import ExpenseStore, MAX_BYTES, ROLES, now, amount_cents


class IntakeQueue:
    def __init__(self, store: ExpenseStore):
        self.store = store
        self.folder = store.root / '统一收件'
        self.folder.mkdir(exist_ok=True)
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intake_queue (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')

    def list(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM intake_queue ORDER BY rowid DESC')]

    def get(self, key: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM intake_queue WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('收件不存在')
            return json.loads(row['data'])  # type: ignore[no-any-return]

    def receive(self, name: str, payload: bytes, *, source: str = 'upload', project_id: str = '', batch: str = '') -> dict[str, Any]:
        name = Path(name.replace('\\', '/')).name
        if not payload or len(payload) > MAX_BYTES:
            raise ValueError('附件须为20MB以内文件')
        suffix = Path(name).suffix.lower()
        if suffix == '.pdf':
            with pymupdf.open(stream=payload, filetype='pdf') as pdf:  # type: ignore[no-untyped-call]
                if pdf.needs_pass or not pdf.page_count:
                    raise ValueError('PDF加密或没有页面')
        else:
            validate_image(name, payload)
        if project_id:
            from invoice_print_layout.expense_projects import ExpenseProjects
            if not any(p['id'] == project_id for p in ExpenseProjects(self.store).list()):
                raise ValueError('项目不存在')
        digest = hashlib.sha256(payload).hexdigest()
        target = self.folder / (digest + suffix)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT data FROM intake_queue WHERE digest=?', (digest,)).fetchone()
            if previous:
                return {**json.loads(previous['data']), 'duplicate': True}
            attached = db.execute('SELECT item_id FROM attachments WHERE digest=?', (digest,)).fetchone()
            if not target.exists():
                temp = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                try:
                    temp.write_bytes(payload)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            data: dict[str, Any] = {'id': uuid.uuid4().hex, 'digest': digest, 'name': name, 'path': str(target),
                    'source': source, 'batch': batch, 'project_id': project_id, 'received_at': now(),
                    'status': 'duplicate' if attached else 'received', 'revision': 0,
                    'item_id': attached['item_id'] if attached else '', 'fields': {}, 'role': 'other',
                    'issues': [], 'text': '', 'result': '重复文件' if attached else '已保存，等待识别'}
            db.execute('INSERT INTO intake_queue VALUES (?,?,?)', (data['id'], digest, json.dumps(data, ensure_ascii=False)))
        return data

    def analyze(self, key: str) -> dict[str, Any]:
        data = self.get(key)
        if data['status'] in {'linked', 'duplicate'}:
            return data
        path = Path(data['path'])
        if path.suffix == '.pdf':
            from invoice_print_layout.automatic_rides import organize_rides
            organize_rides(self.store, folder=self.folder)
            self.reconcile()
            data = self.get(key)
            if data['status'] in {'linked', 'duplicate'}:
                return data
        try:
            if path.suffix == '.pdf':
                with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
                    text = '\n'.join(page.get_text() for page in list(pdf)[:10])
                from invoice_print_layout.takeout import OcrLine
                lines = [OcrLine(line, 0, i * 20, 500, i * 20 + 15) for i, line in enumerate(text.splitlines())]
                parsed = parse_receipt(lines)
                compact = re.sub(r'\s+', '', text)
                number = re.search(r'发票号码[:：]?(\d{8,30})', compact)
                if number:
                    parsed['role'] = 'invoice'
                    parsed['fields']['invoice_number'] = number.group(1)
                amount = re.search(r'(?:小写|价税合计)[)）:：]*[¥￥]?(\d+\.\d{2})', compact)
                if amount:
                    parsed['fields']['amount'] = amount.group(1)
                if '铁路电子客票' in compact or '铁路客运' in compact:
                    parsed['fields']['category'] = '高铁'
                    parsed['fields']['title'] = '铁路车票'
                elif '住宿' in compact or '客房费' in compact:
                    parsed['fields']['category'] = '酒店'
                    parsed['fields']['title'] = '酒店住宿'
            else:
                lines = read_receipt(path.read_bytes(), path.suffix)
                parsed = parse_receipt(lines)
                text = '\n'.join(line.text for line in lines)
            fields = parsed['fields']
            issues = []
            if not fields.get('amount'):
                issues.append('金额冲突')
            if fields.get('category') == '其他':
                issues.append('商品不明')
            if not text.strip():
                issues.append('手写难辨')
            if not data['project_id']:
                issues.append('归属不明')
            data.update(fields=fields, role=parsed['role'], text=text[:24000], issues=issues,
                        status='review', result='待确认' if issues else '识别完成')
        except (ValueError, OSError, RuntimeError) as exc:
            data.update(status='review', issues=['手写难辨'], result='识别未完成：' + str(exc)[:200])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(key)
            if current['revision'] != data['revision']:
                raise ValueError('收件已变化，请刷新')
            data['revision'] += 1
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
        return self.organize(key)

    def organize(self, key: str) -> dict[str, Any]:
        data = self.get(key)
        if data['status'] != 'review':
            return data
        fields = data['fields']
        order = fields.get('order_number', '')
        items = self.store.list_items()
        exact = [x for x in items if order and x['order_number'] == order]
        invoice = fields.get('invoice_number')
        if invoice:
            related = [q for q in self.list() if q['id'] != key and q.get('fields', {}).get('invoice_number') == invoice and q.get('item_id')]
            ids = {q['item_id'] for q in related}
            exact = [x for x in items if x['id'] in ids]
        candidates = [x for x in items if fields.get('merchant') and x['merchant'] == fields['merchant']
                      and x['amount'] == fields.get('amount') and x['stage'] != 'cancelled']
        equal_amount = bool(len(exact) == 1 and fields.get('amount') not in (None, '') and
                            exact[0]['amount_cents'] == amount_cents(fields['amount']))
        if len(exact) == 1 and exact[0]['stage'] == 'draft' and equal_amount and (not data['project_id'] or data['project_id'] == exact[0].get('project_id')):
            return self.resolve(key, data['revision'], fields, data['role'], exact[0]['id'], exact[0]['version'])
        if exact or candidates:
            data['issues'] = list(dict.fromkeys(data['issues'] + ['疑似重复']))
            if exact and not equal_amount:
                data['issues'] = list(dict.fromkeys(data['issues'] + ['金额冲突']))
            data['candidates'] = [x['id'] for x in exact or candidates]
            with self.store.connect() as db:
                db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
            return data
        # Standalone invoices / orders only; receipts and payments need explicit grouping.
        if not data['issues'] and (order or fields.get('invoice_number')) and data['role'] in {'invoice', 'order', 'purchase'}:
            return self.resolve(key, data['revision'], fields, data['role'])
        return data

    def resolve(self, key: str, revision: int, fields: dict[str, Any], role: str,
                item_id: str = '', expected_version: str = '') -> dict[str, Any]:
        if role not in ROLES or role == 'package':
            raise ValueError('材料用途无效')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = self.get(key)
            if data['status'] in {'linked', 'duplicate'}:
                return data
            if data['revision'] != revision:
                raise ValueError('收件已变化，请刷新')
            if item_id:
                item = self.store.get(item_id)
                if item['version'] != expected_version or item['stage'] != 'draft':
                    raise ValueError('所属费用已变化或不是草稿，请刷新')
                stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
                result = '补充材料'
            else:
                if fields.get('amount') in ('', None):
                    raise ValueError('请确认支出金额')
                if fields.get('title') in (None, '', '待核对购买凭证'):
                    raise ValueError('请确认具体购买内容')
                stored = self.store._validate({**fields, 'project_id': fields.get('project_id', data['project_id']),
                                                'expense_date': fields.get('expense_date', '')})
                item_id = uuid.uuid4().hex[:10]
                stored.update(id=item_id, created_at=now(), stage='draft', verified=False)
                db.execute('INSERT INTO expenses VALUES (?,?,?)', (item_id, 'intake:' + key, json.dumps(stored, ensure_ascii=False)))
                result = '新增费用'
            db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, item_id, data['name'], role, data['path'], data['digest']))
            stored.update(verified=False, updated_at=now())
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), '统一收件：' + result))
            data.update(item_id=item_id, status='linked', revision=revision + 1, issues=[], result=result, role=role)
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
        return data

    def import_mail(self, project_id: str = '') -> dict[str, int]:
        counts = {'received': 0, 'duplicate': 0}
        for path in sorted((self.store.root / '邮件收件').glob('*.pdf')):
            result = self.receive(path.name, path.read_bytes(), source='mail', project_id=project_id)
            counts['duplicate' if result.get('duplicate') or result['status'] == 'duplicate' else 'received'] += 1
        return counts

    def reconcile(self) -> None:
        """Reflect pairing done by the existing ride organizer in the durable queue."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT data FROM intake_queue').fetchall():
                data = json.loads(row['data'])
                if data['status'] in {'linked', 'duplicate'}:
                    continue
                attached = db.execute('SELECT item_id FROM attachments WHERE digest=? UNION SELECT item_id FROM automatic_ride_files WHERE digest=?', (data['digest'], data['digest'])).fetchone()
                if attached:
                    if data['project_id']:
                        from invoice_print_layout.expense_projects import ExpenseProjects
                        project = next((p for p in ExpenseProjects(self.store).list() if p['id'] == data['project_id']), None)
                        stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (attached['item_id'],)).fetchone()['data'])
                        if project and stored['stage'] == 'draft' and stored['project'] == '待分配项目':
                            stored.update(project_id=project['id'], project=project['name'])
                            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), attached['item_id']))
                    data.update(status='linked', item_id=attached['item_id'], issues=[], result='打车配套材料已整理', revision=data['revision'] + 1)
                    db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), data['id']))
