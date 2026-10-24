"""Conservative shared *tax class* product parsing; mixed/unknown classes abstain."""
from __future__ import annotations

import json
import re
from typing import Any
import unicodedata

MAPPING = {'住宿服务': '酒店', '铁路客运': '高铁', '铁路旅客运输服务': '高铁', '铁路客运服务': '高铁',
           **dict.fromkeys(('五金', '五金产品', '劳保用品', '劳动保护用品', '日用杂品', '文具', '文具用品',
                            '电线电缆', '纸制品', '办公用品', '计算机外围设备'), '材料采购')}


def tax_rows(text: str) -> list[dict[str, str]]:
    result = []
    for raw in text.splitlines():
        value = unicodedata.normalize('NFKC', raw).strip()
        match = re.match(r'^\*([^*\n]{1,50})\*([^*]*)$', value)
        if match:
            result.append({'tax': re.sub(r'\s+', '', match[1]), 'name': match[2].strip(), 'raw': raw.strip()})
    return result


def classify_tax(text: str) -> dict[str, Any]:
    rows = tax_rows(text)
    classes = sorted({row['tax'] for row in rows})
    values = {MAPPING.get(label, '') for label in classes}
    category = next(iter(values)) if len(values) == 1 and '' not in values else ''
    if category == '材料采购' and any(re.search(r'安装|维修|租赁|人工|施工|服务费', row['name']) for row in rows):
        category = ''
    candidates = ['餐饮', '外卖'] if any('餐饮' in label for label in classes) else []
    return {'category': category, 'tax_classes': classes, 'candidates': candidates,
            'source': 'tax_class', 'quotes': [row['raw'] for row in rows]}


def human_category(store: Any, entry: dict[str, Any], fields: dict[str, Any], text: str) -> tuple[str, bool]:
    """Read matching explicit experience without updating its usage or business state."""
    from invoice_print_layout import expense_classifier as classifier
    from invoice_print_layout.automatic_category import digest
    evidence = {'purpose': classifier.clean(fields.get('note', '')),
                'goods': classifier.goods_from_rows(text.splitlines())}
    normalized = {key: re.sub(r'\s+', '', value).casefold() for key, value in evidence.items()}
    key = digest([normalized, classifier.MODEL, classifier.CRITERIA, classifier.RULES])
    choices: set[str] = set()
    with store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'category_experience' in tables:
            disabled = ('category_experience_disabled' in tables and db.execute(
                'SELECT 1 FROM category_experience_disabled WHERE evidence_key=?', (key,)).fetchone())
            row = db.execute('SELECT * FROM category_experience WHERE evidence_key=?', (key,)).fetchone()
            if row and not disabled:
                if row['conflict']:
                    return '', True
                if row['origin'] == 'human':
                    choices.add(row['category'])
        if 'expense_preferences' in tables:
            for row in db.execute('SELECT data FROM expense_preferences'):
                rule = json.loads(row[0])
                if not rule.get('enabled') or rule.get('kind') != 'category':
                    continue
                if ((rule['scope'] == 'item' and rule['match'] in {entry['id'], entry.get('item_id')}) or
                        (rule['scope'] == 'exact_content' and rule['match'] == evidence['goods'] and
                         rule.get('purpose') == evidence['purpose'])):
                    choices.add(rule['value'])
    return (next(iter(choices)), True) if len(choices) == 1 else ('', bool(choices))
