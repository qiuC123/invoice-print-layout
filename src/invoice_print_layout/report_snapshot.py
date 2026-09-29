"""One grouping contract for HTML, Excel and material manifests."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any

SECTIONS = {'高铁': 12, '打车': 13, '酒店': 16, '外卖': 17, '餐饮': 19, '顺丰': 29}
REPORT_SECTIONS = {'rail': '高铁', 'taxi': '打车', 'hotel': '酒店', 'takeout': '外卖',
                   'dining': '现场饮用水、夜宵', 'materials': '材料采购', 'sf': '顺丰同城', 'other': '其他'}
ROWS = {'rail': 12, 'taxi': 13, 'hotel': 16, 'takeout': 17, 'dining': 19, 'sf': 29}


def display_name(item: dict[str, Any]) -> str:
    if item.get('display_name'):
        return str(item['display_name'])
    title = item['title'].split(' · ')[-1]
    if title.startswith('雨衣'):
        return '雨衣采购'
    if title.startswith('口罩'):
        return '口罩采购'
    if item['category'] == '顺丰':
        return '顺丰同城'
    return str(title)


def snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    grouped: dict[tuple[str, str, str], int] = {}
    counters = {'materials': 0, 'other': 0}
    for index, item in enumerate(items):
        section = item.get('report_section') or {'材料采购': 'materials', '其他': 'other'}.get(item['category'], '')
        name = display_name(item)
        group = item.get('report_group', '') or ('雨衣采购' if name == '雨衣采购' and section == 'materials' else '')
        if section in counters:
            key = (str(item.get('project_id') or item['project']), section, group or item['id'])
            if key not in grouped:
                grouped[key] = (24 if section == 'materials' else 30) + min(counters[section], 4)
                counters[section] += 1
            row = grouped[key]
        else:
            row = ROWS.get(section, SECTIONS.get(item['category'], 30))
            if not section and item['category'] == '外卖' and re.search('咖啡|奶茶|饮品|饮用水|蜜雪冰城', item['title'] + item['merchant']):
                row = 19
        buckets[row].append({'id': item['id'], 'name': name, 'amount_cents': item['amount_cents'], 'detail_row': index + 2})
    return {'items': items, 'total_cents': sum(x['amount_cents'] for x in items),
            'rows': [{'row': row, 'items': group, 'name': '、'.join(dict.fromkeys(x['name'] for x in group)),
                      'amount_cents': sum(x['amount_cents'] for x in group)} for row, group in sorted(buckets.items())]}
