"""Explicit, versioned corrections and auditable duplicate consolidation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from invoice_print_layout.workbench import ExpenseStore, amount_cents, now

ISSUE_KINDS = ('金额冲突', '商品不明', '疑似重复', '归属不明', '手写难辨')


class ExpenseReview:
    def __init__(self, store: ExpenseStore):
        self.store = store

    def confirm(self, key: str, version: str, values: dict[str, Any]) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self.store.get(key)
            if item['version'] != version or item['stage'] != 'draft':
                raise ValueError('事项已变化或不是草稿，请刷新')
            reason = str(values.get('reason', '')).strip()
            title = str(values.get('title', item['title'])).strip()
            amount = amount_cents(values.get('amount', item['amount']))
            face = amount_cents(values.get('face_amount', item['amount']))
            payments = values.get('payments', [])
            if not isinstance(payments, list) or len(payments) > 50:
                raise ValueError('分笔付款格式无效')
            parts = [amount_cents(v) for v in payments]
            if parts and sum(parts) != amount:
                raise ValueError('分笔付款合计必须等于实际支出')
            if (amount != face or amount != item['amount_cents']) and not reason:
                raise ValueError('金额纠正请填写原因')
            if not title or len(title) > 200 or len(reason) > 2000:
                raise ValueError('购买内容或说明无效')
            row = db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()
            stored = json.loads(row['data'])
            confirmation = {'face_amount_cents': face, 'actual_amount_cents': amount,
                            'payments_cents': parts, 'reason': reason, 'at': now(),
                            'attachment_ids': [a['id'] for a in item['attachments']]}
            stored.update(title=title, amount_cents=amount, confirmation=confirmation, verified=False, updated_at=now())
            stored.setdefault('confirmations', []).append(confirmation)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), key))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (key, now(), '确认购买内容与实际支出：' + reason))
        return self.store.get(key)

    def merge_preview(self, ids: list[str], target: str) -> dict[str, Any]:
        if len(ids) < 2 or len(ids) != len(set(ids)) or target not in ids:
            raise ValueError('请选择至少两笔不同费用及保留的费用')
        items = [self.store.get(key) for key in ids]
        if any(x['stage'] != 'draft' for x in items):
            raise ValueError('仅能合并未提交的草稿')
        if len({x.get('project_id') or x['project'] for x in items}) != 1:
            raise ValueError('请先确认费用属于同一个项目')
        versions = {x['id']: x['version'] for x in items}
        token = hashlib.sha256(json.dumps([target, versions], sort_keys=True).encode()).hexdigest()
        keep = next(x for x in items if x['id'] == target)
        return {'ids': ids, 'target': target, 'token': token, 'amount': keep['amount'],
                'before_cents': sum(x['amount_cents'] for x in items),
                'after_cents': keep['amount_cents'], 'items': items,
                'attachments': [a for x in items for a in x['attachments']]}

    def merge(self, ids: list[str], target: str, token: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError('请填写合并依据')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Retrying a committed request is harmless; source records remain readable.
            previous = [self.store.get(key) for key in ids if key != target]
            if previous and all(x.get('merged_into') == target and x.get('merge_token') == token for x in previous):
                return self.store.get(target)
            preview = self.merge_preview(ids, target)
            if preview['token'] != token:
                raise ValueError('合并预览已过期，请重新审阅')
            for key in ids:
                stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
                if key != target:
                    for a in db.execute('SELECT * FROM attachments WHERE item_id=?', (key,)).fetchall():
                        exists = db.execute('SELECT id FROM attachments WHERE item_id=? AND digest=? AND role=?',
                                            (target, a['digest'], a['role'])).fetchone()
                        if not exists:
                            import uuid
                            db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                                       (uuid.uuid4().hex, target, a['name'], a['role'], a['path'], a['digest']))
                    stored.update(stage='cancelled', merged_into=target, merge_token=token)
                stored.update(verified=False, updated_at=now())
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), key))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (key, now(), '重复费用合并到 ' + target + '：' + reason))
        return self.store.get(target)
