"""One grouping and print-order contract for HTML, Excel and material packets."""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from invoice_print_layout.workbench import ExpenseStore

from invoice_print_layout.report_types import (REPORT_LABELS, REPORT_ROWS,
                                               REPORT_SECTIONS as TEMPLATE_SECTIONS,
                                               report_types, validate_report_choice)

SECTIONS = {'高铁': 12, '打车': 13, '酒店': 16, '外卖': 17, '餐饮': 19, '顺丰': 29}
REPORT_SECTIONS = {**TEMPLATE_SECTIONS, 'rail': '高铁', 'taxi': '打车', 'hotel': '酒店', 'takeout': '外卖'}
ROWS = {**REPORT_ROWS, 'sf': 29}


def display_name(item: dict[str, Any]) -> str:
    if item.get('project_type'):
        return validate_report_choice(item['project_type'], item.get('project_name'))['project_name']
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


def _section(item: dict[str, Any]) -> str:
    if item.get('project_type'):
        return validate_report_choice(item['project_type'], item.get('project_name'))['report_section']
    return str(item.get('report_section') or {'材料采购': 'materials', '其他': 'other'}.get(item['category'], ''))


def _fixed_row(item: dict[str, Any], section: str) -> int:
    row = ROWS.get(section, SECTIONS.get(item['category'], 30))
    if not section and item['category'] == '外卖' and re.search('咖啡|奶茶|饮品|饮用水|蜜雪冰城', item['title'] + item['merchant']):
        row = 19
    return row


def snapshot(items: list[dict[str, Any]]) -> dict[str, Any]:
    from invoice_print_layout.reimbursement_people import allocation_items, person_allocation
    items = allocation_items(items)
    buckets: dict[int, dict[tuple[str, str, str], list[dict[str, Any]]]] = defaultdict(dict)
    grouped: dict[tuple[str, str, str], int] = {}
    dynamic_sections = {'materials', 'other'}
    reserved = {_fixed_row(item, _section(item)) for item in items if _section(item) not in dynamic_sections}
    legacy_groups: dict[tuple[str, str, str], int] = {}
    legacy_counts = {'materials': 0, 'other': 0}
    for item in items:
        section = _section(item)
        if section in dynamic_sections and not item.get('project_type'):
            name = display_name(item)
            group = item.get('report_group', '') or ('雨衣采购' if name == '雨衣采购' and section == 'materials' else '')
            key = (str(item.get('project_id') or item['project']), section, group or item['id'])
            if key not in legacy_groups:
                legacy_groups[key] = (24 if section == 'materials' else 30) + min(legacy_counts[section], 4)
                legacy_counts[section] += 1
    reserved.update(legacy_groups.values())
    allocated: set[int] = set()
    for item in items:
        section = _section(item)
        name = display_name(item)
        group = item.get('report_group', '') or ('雨衣采购' if name == '雨衣采购' and section == 'materials' else '')
        if section in dynamic_sections:
            key = (str(item.get('project_id') or item['project']), section,
                   group or (name if item.get('project_type') else item['id']))
            if key not in grouped:
                if key in legacy_groups:
                    grouped[key] = legacy_groups[key]
                else:
                    start, end = (24, 28) if section == 'materials' else (29, 34)
                    grouped[key] = next((row for row in range(start, end + 1) if row not in reserved | allocated), end)
                    allocated.add(grouped[key])
            row = grouped[key]
        else:
            row = _fixed_row(item, section)
            key = (str(item.get('project_id') or item['project']), 'fixed:' + str(row),
                   '' if row in REPORT_LABELS else group or name)
        # Preserve the table's assigned rows, including historical and overflow
        # rows. Within a row keep each project/subitem together in first-seen order.
        buckets[row].setdefault(key, []).append({'id': item['id'], 'name': name,
                                               'amount_cents': item['amount_cents']})
    by_id = {item['id']: item for item in items}
    ordered_items: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    material_groups: list[dict[str, Any]] = []
    for row, groups in sorted(buckets.items()):
        entries: list[dict[str, Any]] = []
        for group in groups.values():
            material_groups.append({'row': row, 'item_ids': [entry['id'] for entry in group]})
            for entry in group:
                entry['detail_row'] = len(ordered_items) + 2
                ordered_items.append(by_id[entry['id']])
                entries.append(entry)
        rows.append({'row': row, 'items': entries,
                     'name': '、'.join(dict.fromkeys(entry['name'] for entry in entries)),
                     'amount_cents': sum(entry['amount_cents'] for entry in entries)})
    items = ordered_items
    allocation = person_allocation(items)
    for person in allocation['rows']:
        person['items'] = [{'id': item['id'], 'title': item['title'], 'display_name': display_name(item),
                            'project': item['project'], 'project_id': item.get('project_id', ''),
                            'category': item['category'], 'project_type': item.get('project_type', ''),
                            'project_name': item.get('project_name', ''), 'merchant': item.get('merchant', ''),
                            'expense_date': item.get('expense_date', ''), 'amount_cents': item['amount_cents'],
                            'amount': f"{item['amount_cents']/100:.2f}"} for item in items
                           if str(item.get('reimbursement_person_id') or '') == person['person_id']]
    return {'items': items, 'total_cents': sum(x['amount_cents'] for x in items),
            'ordered_item_ids': [item['id'] for item in items], 'material_groups': material_groups,
            'person_allocation': allocation,
            'report_types': report_types(), 'row_labels': dict(REPORT_LABELS),
            'rows': rows}


def material_batches(report: dict[str, Any], store: ExpenseStore | None = None) -> list[list[dict[str, Any]]]:
    """Pair full-size native invoices within one category and table subitem."""
    if store is None:
        return [[item] for item in report['items']]
    from invoice_print_layout.invoice_two_up import invoice_pairable
    groups = {key: index for index, group in enumerate(report['material_groups'])
              for key in group['item_ids']}
    batches: list[list[dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    key: tuple[str, str, int] | None = None
    for item in report['items']:
        project = str(item.get('project_id') or item.get('project') or '')
        current = ((project, str(item['category']), groups[item['id']])
                   if project and item['id'] in groups and invoice_pairable(store, item) else None)
        if pending and current != key:
            batches.append(pending); pending = []
        if current is None:
            batches.append([item]); key = None
        else:
            pending.append(item); key = current
            if len(pending) == 2:
                batches.append(pending); pending = []
    if pending:
        batches.append(pending)
    return batches
