"""Company-template choices shared by material registration and report grouping.

Labels follow the private template's A/B8:B34, with layout whitespace removed.
This module contains no private worksheet data, identities or expense records.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .report_choice_rules import printing_report_suggestion as printing_report_suggestion
from .report_choice_rules import material_report_suggestion as material_report_suggestion


FIXED_ITEMS: tuple[tuple[str, str, int, str, str], ...] = (
    ('expansion', '场地费用(消费)', 8, '其他', 'venue'),
    ('expansion', '施工押金(可退)', 9, '其他', 'deposit'),
    ('transport', '货车去程运费', 10, '其他', 'freight_out'),
    ('transport', '货车回程运费', 11, '其他', 'freight_back'),
    ('transport', '火车、飞机', 12, '高铁', 'rail'),
    ('transport', '打的(出租车、滴滴等)', 13, '打车', 'taxi'),
    ('transport', '公交(地铁、公交、大巴等)', 14, '其他', 'bus'),
    ('transport', '货拉拉(附行程单、报销凭证)', 15, '其他', 'lalamove'),
    ('living', '住宿费(附清单)', 16, '酒店', 'hotel'),
    ('living', '员工统一购餐费(附清单)', 17, '外卖', 'takeout'),
    ('living', '生活补助费用(附清单)', 18, '其他', 'living_allowance'),
    ('living', '现场饮用水、夜宵', 19, '餐饮', 'dining'),
    ('personnel', '工厂搭建人工数', 20, '其他', 'build_workers'),
    ('personnel', '工厂撤展人工数', 21, '其他', 'dismantle_workers'),
    ('personnel', '外请人员金额(附清单)', 22, '其他', 'external_workers'),
    ('personnel', '保洁费(附清单)', 23, '其他', 'cleaning'),
)
_TYPE_INFO = (
    ('expansion', '拓展', 8, 9), ('transport', '车费', 10, 15),
    ('living', '生活费', 16, 19), ('personnel', '人员', 20, 23),
    ('materials', '现场购材料', 24, 28), ('other', '其他', 29, 34),
)
REPORT_TYPES: list[dict[str, Any]] = [
    {'code': code, 'label': label, 'names': [name for kind, name, *_ in FIXED_ITEMS if kind == code],
     'editable_name': code in {'materials', 'other'}, 'rows': list(range(start, end + 1)),
     'items': [{'name': name, 'row': row, 'category': category, 'report_section': section}
               for kind, name, row, category, section in FIXED_ITEMS if kind == code]}
    for code, label, start, end in _TYPE_INFO
]
REPORT_ROWS = {section: row for _, _, row, _, section in FIXED_ITEMS}
REPORT_LABELS = {row: name for _, name, row, _, _ in FIXED_ITEMS}
REPORT_SECTIONS = {section: name for _, name, _, _, section in FIXED_ITEMS}
REPORT_SECTIONS.update(materials='材料采购', sf='顺丰同城', other='其他')


def report_types() -> list[dict[str, Any]]:
    """Return an isolated, JSON-serializable hierarchy for the board API."""
    return deepcopy(REPORT_TYPES)


def validate_report_choice(project_type: Any, project_name: Any) -> dict[str, str]:
    if not isinstance(project_type, str) or not project_type.strip():
        raise ValueError('请选择项目类型')
    raw_type = project_type.strip()
    choice = next((item for item in REPORT_TYPES if raw_type in {item['code'], item['label']}), None)
    if choice is None:
        raise ValueError('项目类型不在报销表分类中')
    if not isinstance(project_name, str) or not project_name.strip():
        raise ValueError('请填写或选择项目名称')
    name = project_name.strip()
    if len(name) > 200:
        raise ValueError('项目名称不能超过200字')
    code = str(choice['code'])
    if choice['editable_name']:
        category, section = ('材料采购', 'materials') if code == 'materials' else ('其他', 'other')
    else:
        fixed = next((item for item in choice['items'] if name == item['name']), None)
        if fixed is None:
            raise ValueError('项目名称必须选择该类型在报销表中的固定子项')
        category, section = fixed['category'], fixed['report_section']
    return {'project_type': code, 'project_name': name, 'category': category,
            'report_section': section, 'report_group': '', 'display_name': name, 'title': name}


def _legacy_name(item: dict[str, Any]) -> str:
    name = str(item.get('display_name') or item.get('title') or '').strip().split(' · ')[-1]
    if name.startswith('雨衣'):
        return '雨衣采购'
    if name.startswith('口罩'):
        return '口罩采购'
    return name


def infer_report_choice(item: dict[str, Any]) -> dict[str, str]:
    """Suggest template choices from existing fields without mutating the item."""
    if item.get('project_type') and item.get('project_name'):
        return validate_report_choice(item['project_type'], item['project_name'])
    description_choice = material_report_suggestion(item)
    if description_choice:
        return validate_report_choice(description_choice['project_type'], description_choice['project_name'])
    section = str(item.get('report_section') or '')
    fixed = next((row for row in FIXED_ITEMS if row[4] == section), None)
    if fixed is None and not section:
        category = str(item.get('category') or '')
        section = {'高铁': 'rail', '打车': 'taxi', '酒店': 'hotel', '外卖': 'takeout',
                   '餐饮': 'dining', '材料采购': 'materials', '顺丰': 'sf'}.get(category, 'other')
        if section == 'takeout' and re.search('咖啡|奶茶|饮品|饮用水|蜜雪冰城',
                                            str(item.get('title') or '') + str(item.get('merchant') or '')):
            section = 'dining'
        fixed = next((row for row in FIXED_ITEMS if row[4] == section), None)
    if fixed is not None:
        return validate_report_choice(fixed[0], fixed[1])
    name = _legacy_name(item)
    if section == 'sf' or (section == 'other' and '顺丰' in str(item.get('merchant') or '')):
        name = '顺丰同城'
    kind = 'materials' if section == 'materials' else 'other'
    if not name:
        return {'project_type': kind, 'project_name': '', 'category': '材料采购' if kind == 'materials' else '其他',
                'report_section': kind, 'report_group': '', 'display_name': '', 'title': ''}
    return validate_report_choice(kind, name[:200])


def legacy_auto_category_report_choice(item: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, str]:
    """Repair a proven old registration default in a read view, never the ledger.

    Early automatic classification deliberately changed only ``category``. An
    untouched registration could consequently retain its original Other row.
    Require the original recognition snapshot and the complete edit history;
    a merchant name or a truncated display event list is never sufficient.
    """
    matching = item.get('material_match')
    if not isinstance(matching, dict) or matching.get('mode') != 'registration':
        return {}
    recognition = matching.get('recognition')
    if not isinstance(recognition, dict) or not isinstance(recognition.get('report_choice'), dict):
        return {}
    original = recognition['report_choice']
    if original.get('project_type') != 'other' or item.get('category') in (None, '', '其他'):
        return {}
    try:
        original_choice = validate_report_choice(original.get('project_type'), original.get('project_name'))
    except ValueError:
        return {}
    fields = ('project_type', 'project_name', 'report_section', 'display_name', 'title', 'report_group')
    if any(item.get(field) != original_choice[field] or original.get(field) != original_choice[field]
           for field in fields):
        return {}
    messages = [str(event.get('message') or '') for event in events]
    if any(message.startswith(('更新事项', '编辑报销项目类型', '批量修改报销项目类型', '确认购买内容与实际支出：'))
           for message in messages):
        return {}
    category = str(item['category'])
    if '自动分类已保存：' + category + '；材料和金额仍需核对' not in messages:
        return {}
    # Only the saved category and original name participate. In particular, a
    # hotel-looking merchant must never turn an Other expense into lodging.
    if category not in {'材料采购', '酒店', '高铁', '外卖', '餐饮', '打车', '顺丰'}:
        return {}
    name = _legacy_name(item)
    candidate = infer_report_choice({'category': category, 'title': name})
    return candidate if candidate.get('category') == category else {}


_EDIT_PREFIXES = ('更新事项', '编辑报销项目类型', '批量修改报销项目类型', '确认购买内容与实际支出：')
_AUTO_CATEGORIES = {'材料采购', '酒店', '高铁', '外卖', '餐饮', '打车', '顺丰'}


def _exact_report_choice(value: Any, *, ignore_category: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    try:
        choice = validate_report_choice(value.get('project_type'), value.get('project_name'))
    except ValueError:
        return {}
    if any(value.get(field) != expected for field, expected in choice.items()
           if not (ignore_category and field == 'category')):
        return {}
    return choice


def registration_manual_report_choice(item: dict[str, Any], events: list[dict[str, Any]],
                                      group: Any) -> dict[str, str]:
    """Prove a changed registration choice from its saved, matching undo snapshot.

    Recognition is a suggestion, whereas aggregate_after records the submitted
    form. Neither a friendly title nor the verification feedback's ``manual``
    status proves a choice by itself. Unknown histories fail closed.
    """
    matching = item.get('material_match')
    if (not isinstance(matching, dict) or matching.get('mode') != 'registration' or
            not isinstance(group, dict) or group.get('schema_version') != 3 or
            group.get('mode') != 'registration' or group.get('status') != 'provisional' or
            not matching.get('group_id') or group.get('id') != matching.get('group_id') or
            group.get('item_id') != item.get('id') or group.get('project_id') != item.get('project_id')):
        return {}
    aggregate = group.get('aggregate_after')
    if not isinstance(aggregate, dict):
        return {}
    initial = aggregate.get('data')
    if (not isinstance(initial, dict) or initial.get('id') != item.get('id') or
            initial.get('project_id') != item.get('project_id')):
        return {}
    choice = _exact_report_choice(initial)
    current = _exact_report_choice(item, ignore_category=True)
    if not choice or choice != current:
        return {}
    initial_matching = initial.get('material_match')
    if (not isinstance(initial_matching, dict) or initial_matching.get('mode') != 'registration' or
            initial_matching.get('group_id') != matching['group_id']):
        return {}
    original: dict[str, str] = {}
    for source in (matching, initial_matching, group):
        recognition = source.get('recognition')
        candidate = _exact_report_choice(recognition.get('report_choice')) if isinstance(recognition, dict) else {}
        if not candidate or original and candidate != original:
            return {}
        original = candidate
    if choice == original:
        return {}
    initial_events = aggregate.get('events')
    if not isinstance(initial_events, list) or not initial_events or len(initial_events) > len(events):
        return {}
    if (not isinstance(initial_events[-1], dict) or
            initial_events[-1].get('message') != '材料已登记；金额及对应关系待人工核对'):
        return {}
    for saved, live in zip(initial_events, events):
        if (not isinstance(saved, dict) or not isinstance(live, dict) or
                any(saved.get(field) != live.get(field) for field in ('id', 'at', 'message'))):
            return {}
    # A reused expense can already have edits before this explicit registration.
    # The exact prefix above binds those old actions to the saved form; only
    # actions after that form was confirmed can make its current choice unclear.
    messages = [str(event.get('message') or '') for event in events[len(initial_events):]]
    if any(message.startswith(_EDIT_PREFIXES) for message in messages):
        return {}
    neutral = ('指定费用费用归属人：', '费用归属人更名：', '费用归属人归并：',
               '人工确认材料内容及金额', '批量退回待核对；', '状态变更：',
               '报销包制作成功，自动标记已提交：', '收到材料：', '材料用途改为：',
               '确认进入待核对；', '删除事项；', '恢复已删除事项；')
    for message in messages:
        if (message.startswith(neutral) or
                any(message == '自动分类已保存：' + category + '；材料和金额仍需核对'
                    for category in _AUTO_CATEGORIES)):
            continue
        return {}
    return choice


def explicit_manual_report_choice(item: dict[str, Any], events: list[dict[str, Any]],
                                  group: Any) -> dict[str, str]:
    """Expose only proven manual classification for automatic-category guards."""
    choice = registration_manual_report_choice(item, events, group)
    if choice:
        return choice
    current = _exact_report_choice(item)
    if not current:
        return {}
    messages = [str(event.get('message') or '') for event in events]
    for message in reversed(messages):
        if message.startswith(('更新事项', '确认购买内容与实际支出：')):
            return {}
        if message.startswith(('编辑报销项目类型', '批量修改报销项目类型')):
            return current
    return {}


def legacy_manual_category_report_choice(item: dict[str, Any], events: list[dict[str, Any]],
                                         group: Any) -> dict[str, str]:
    """Restore an explicit registration choice only in a read view."""
    choice = registration_manual_report_choice(item, events, group)
    category = item.get('category')
    if not choice or category == choice['category'] or not isinstance(category, str) or category not in _AUTO_CATEGORIES:
        return {}
    initial_events = group['aggregate_after']['events']
    automatic = [str(event.get('message') or '') for event in events[len(initial_events):]
                 if str(event.get('message') or '').startswith('自动分类已保存：')]
    expected = '自动分类已保存：' + str(category) + '；材料和金额仍需核对'
    return choice if automatic and automatic[-1] == expected else {}
