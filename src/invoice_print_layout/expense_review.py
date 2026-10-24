"""Explicit, versioned corrections and auditable duplicate consolidation."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from invoice_print_layout.workbench import ExpenseStore, amount_cents, now

ISSUE_KINDS = ('金额冲突', '商品不明', '疑似重复', '归属不明', '手写难辨')


class ExpenseReview:
    def __init__(self, store: ExpenseStore):
        self.store = store

    @staticmethod
    def _type_fields(data: dict[str, Any], project_type: str, fixed_name: Any,
                     editable_name: bool) -> dict[str, str]:
        from invoice_print_layout.report_types import validate_report_choice
        name = data.get('project_name') if editable_name else fixed_name
        if editable_name and (name is None or isinstance(name, str) and not name.strip()):
            name = data.get('title')
        choice = validate_report_choice(project_type, name)
        choice.pop('title')
        # Free-form names are each expense's original name, including any
        # intentional whitespace. Validation never truncates or renames them.
        if editable_name and isinstance(name, str):
            choice.update(project_name=name, display_name=name)
        return choice

    def batch_project_type(self, project_id: Any, ids: Any, versions: Any,
                           project_type: Any, project_name: Any = None) -> dict[str, Any]:
        """Change only classification, with one transaction for the selection."""
        from invoice_print_layout.expense_projects import assert_project_active, project_views
        from invoice_print_layout.material_matching import MaterialMatching
        from invoice_print_layout.report_types import REPORT_TYPES
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 200 or
                any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids) or
                not isinstance(versions, dict) or set(versions) != set(ids) or
                any(not isinstance(value, str) or not value for value in versions.values())):
            raise ValueError('请选择1至200笔不重复费用，并提供每笔费用的完整当前版本')
        target = next((item for item in REPORT_TYPES if isinstance(project_type, str) and
                       project_type.strip() in {item['code'], item['label']}), None)
        if target is None:
            raise ValueError('请选择有效的报销项目类型')
        editable_name = bool(target['editable_name'])
        if editable_name and project_name is not None:
            raise ValueError('该类型保留每笔费用原名称，不能提供统一项目名称')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not isinstance(project_id, str) or not project_id:
                raise ValueError('请先选择当前项目')
            assert_project_active(db, project_id)
            if not any(project['id'] == project_id for project in project_views(self.store, db=db)):
                raise ValueError('当前项目不存在，请重新选择')
            items = [self.store.get(key) for key in ids]
            plans: list[tuple[str, dict[str, Any], dict[str, str], dict[str, Any] | None, bool]] = []
            # Validate every expense and all undo relationships before writing
            # either an expense or an older group's missing snapshot.
            for item in items:
                if item.get('project_id') != project_id:
                    raise ValueError('费用不属于当前项目，请重新选择')
                if item['version'] != versions[item['id']]:
                    raise ValueError('费用已变化，请刷新后重新选择')
                if (item['stage'] != 'draft' or item.get('verified') or item.get('deleted') or
                        item.get('merged_into') or item.get('legacy') or item.get('history_imported') or
                        item.get('returned_to_synthesis') or
                        item.get('synthesis_material_state') in {'archived', 'deleted'}):
                    raise ValueError('只能批量修改当前项目尚未核对的草稿费用类型')
                data = MaterialMatching._member_state(db, item['id'])['data']
                fields = self._type_fields(data, str(target['code']), project_name, editable_name)
                group, pristine = self._classification_group(db, data, project_id)
                if group and pristine:
                    for original in group.get('member_before', {}).values():
                        self._type_fields(original['data'], str(target['code']), project_name, editable_name)
                plans.append((item['id'], data, fields, group, pristine))
            changed = 0
            timestamp = now()
            for key, data, fields, group, pristine in plans:
                if all(data.get(field, '') == value for field, value in fields.items()):
                    continue
                MaterialMatching.preserve_edit_guard(db, key)
                if group and not group.get('aggregate_after'):
                    group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (group['id'],)).fetchone()['data'])
                from invoice_print_layout.recognition_corrections import record_expense
                record_expense(db, key, data, {**data, **fields})
                data.update(**fields, updated_at=timestamp)
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
                cursor = db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                                    (key, timestamp, '批量修改报销项目类型：' + str(target['label']) + '；原名称、金额及核对状态保留'))
                if group and pristine:
                    snapshot = group['aggregate_after']
                    snapshot['data'].update(**fields, updated_at=timestamp)
                    snapshot['events'].append(dict(db.execute('SELECT * FROM events WHERE id=?', (cursor.lastrowid,)).fetchone()))
                    # Undo restores each member's own name with the new type;
                    # recognition evidence and original titles stay unchanged.
                    for original in group.get('member_before', {}).values():
                        original['data'].update(self._type_fields(original['data'], str(target['code']), project_name, editable_name))
                    db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group['id']))
                changed += 1
        return {'items': [self.store.get(key) for key in ids], 'changed': changed,
                'message': f'已修改{changed}笔费用的报销项目类型；每笔原名称及金额保留。' if changed else '所选费用已使用该类型，没有重复修改。'}

    @staticmethod
    def _classification_group(db: sqlite3.Connection, data: dict[str, Any],
                              project_id: str) -> tuple[dict[str, Any] | None, bool]:
        from invoice_print_layout.material_matching import MaterialMatching
        group_id = (data.get('material_match') or {}).get('group_id')
        if not group_id:
            return None, False
        row = db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()
        if row is None:
            raise ValueError('材料组合不存在，请刷新后重试')
        group = json.loads(row['data'])
        if group['item_id'] != data['id'] or group['project_id'] != project_id or group['status'] == 'undone':
            raise ValueError('费用的材料组合已变化，请刷新后重试')
        for key in group.get('queue_before', {}):
            entry = MaterialMatching._queue(db, key)
            if entry.get('item_id') != data['id'] or entry.get('material_match_id') != group_id:
                raise ValueError('组合收件关联已变化，请刷新后重试')
        try:
            MaterialMatching._assert_group_snapshot(db, group)
            return group, True
        except ValueError:
            # A prior manual edit must continue to block undo. The batch may
            # change type, but must never adopt that older changed content.
            return group, False

    def confirm(self, key: str, version: str, values: dict[str, Any]) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self.store.get(key)
            if item.get('returned_to_synthesis'):
                raise ValueError('材料已撤回，请先在合成台重新确认创建')
            if item.get('deleted'):
                raise ValueError('事项已删除，请先恢复再确认')
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
            from invoice_print_layout.recognition_corrections import record_expense
            record_expense(db, key, stored, {**stored, 'title': title, 'amount_cents': amount})
            stored.update(title=title, amount_cents=amount, confirmation=confirmation, verified=False, updated_at=now())
            stored.pop('review_requested', None)
            stored.setdefault('confirmations', []).append(confirmation)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), key))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (key, now(), '确认购买内容与实际支出：' + reason))
        return self.store.get(key)

    def merge_preview(self, ids: list[str], target: str) -> dict[str, Any]:
        if len(ids) < 2 or len(ids) != len(set(ids)) or target not in ids:
            raise ValueError('请选择至少两笔不同费用及保留的费用')
        items = [self.store.get(key) for key in ids]
        if any(item.get('returned_to_synthesis') for item in items):
            raise ValueError('材料已撤回，请先在合成台重新确认创建')
        if any(item.get('deleted') for item in items):
            raise ValueError('已删除事项不能参与合并，请先恢复')
        if any(x['stage'] != 'draft' for x in items):
            raise ValueError('仅能合并未提交的草稿')
        if len({x.get('project_id') or x['project'] for x in items}) != 1:
            raise ValueError('请先确认费用属于同一个项目')
        from invoice_print_layout.reimbursement_people import common_person
        common_person(items)
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
            if any(self.store.get(key).get('returned_to_synthesis') for key in ids):
                raise ValueError('材料已撤回，请先在合成台重新确认创建')
            # Retrying a committed request is harmless; source records remain readable.
            if any(self.store.get(key).get('deleted') for key in ids):
                raise ValueError('已删除事项不能参与合并，请先恢复')
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
                stored.pop('review_requested', None)
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), key))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (key, now(), '重复费用合并到 ' + target + '：' + reason))
        return self.store.get(target)
