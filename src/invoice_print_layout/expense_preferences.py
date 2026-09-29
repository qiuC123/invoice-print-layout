"""Editable, scoped user rules; no implicit merchant-wide learning."""
from __future__ import annotations

import json
import uuid
from typing import Any

from invoice_print_layout.workbench import ExpenseStore, CATEGORIES, now
from invoice_print_layout.report_snapshot import REPORT_SECTIONS

KINDS = ('category', 'display_name', 'report_section', 'report_group', 'material_basis')


class Preferences:
    def __init__(self, store: ExpenseStore):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS expense_preferences (id TEXT PRIMARY KEY, data TEXT NOT NULL)')

    def list(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            return [json.loads(x['data']) for x in db.execute('SELECT data FROM expense_preferences ORDER BY rowid DESC')]

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        kind, scope = values.get('kind'), values.get('scope')
        if kind not in KINDS or scope not in {'item', 'exact_content', 'receipt_default'}:
            raise ValueError('请选择经验种类及适用范围')
        if kind == 'material_basis' and scope != 'item':
            raise ValueError('材料例外仅限本笔')
        if scope == 'receipt_default' and (kind != 'category' or values.get('value') != '材料采购'):
            raise ValueError('线下收据默认只用于无明确商品时先归材料采购')
        match = str(values.get('match', '')).strip()
        purpose = str(values.get('purpose', '')).strip()
        value = str(values.get('value', '')).strip()
        if not match or not value or max(len(match), len(value), len(purpose)) > 200:
            raise ValueError('请填写200字以内的匹配内容和结果')
        if scope == 'exact_content' and (not purpose or len(match) < 2):
            raise ValueError('复用经验必须指定明确商品及用途，不能仅填写商家')
        if kind == 'category' and value not in CATEGORIES:
            raise ValueError('费用类别无效')
        if kind == 'report_section' and value not in REPORT_SECTIONS:
            raise ValueError('报表栏目无效')
        if kind == 'material_basis' and value not in {'standard', 'payment_only'}:
            raise ValueError('材料要求无效')
        if scope == 'item':
            self.store.get(match)
        key = str(values.get('id') or uuid.uuid4().hex)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT data FROM expense_preferences WHERE id=?', (key,)).fetchone()
            previous = json.loads(old['data']) if old else {}
            if old and values.get('revision') != previous['revision']:
                raise ValueError('经验已变化，请刷新')
            data = {'id': key, 'kind': kind, 'scope': scope, 'match': match, 'purpose': purpose,
                    'value': value, 'enabled': values.get('enabled', True) is True,
                    'source': 'human', 'updated_at': now(), 'last_used': previous.get('last_used', ''),
                    'revision': previous.get('revision', 0) + 1}
            db.execute('INSERT OR REPLACE INTO expense_preferences VALUES (?,?)', (key, json.dumps(data, ensure_ascii=False)))
        return data

    def apply(self, item: dict[str, Any], *, goods: str = '', purpose: str = '', receipt: bool = False) -> dict[str, Any]:
        matches = [r for r in self.list() if r['enabled'] and
                   ((r['scope'] == 'item' and r['match'] == item['id']) or
                    (r['scope'] == 'exact_content' and goods and purpose and r['match'] == goods and r['purpose'] == purpose) or
                    (r['scope'] == 'receipt_default' and receipt and goods.strip() in {'', '日用百货'} and not purpose.strip() and item['category'] == '其他'))]
        changes: dict[str, Any] = {}
        conflicts = []
        for kind in KINDS:
            rules = [r for r in matches if r['kind'] == kind]
            values = {r['value'] for r in rules}
            if len(values) > 1:
                conflicts.append(kind)
            elif values:
                changes[kind] = next(iter(values))
                with self.store.connect() as db:
                    for rule in rules:
                        rule['last_used'] = now()
                        db.execute('UPDATE expense_preferences SET data=? WHERE id=?', (json.dumps(rule, ensure_ascii=False), rule['id']))
        return {'changes': changes, 'conflicts': conflicts}
