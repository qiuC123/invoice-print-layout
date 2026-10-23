"""Concise automatic headings never replace source evidence or a person's choice."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from invoice_print_layout.agent_expense_naming import concise_expense_name
from tests.test_invoice_agent import environment
from tests.test_invoice_agent_content import draft


@pytest.mark.parametrize('names,category,expected', [
    (['雨衣', '携式加厚雨披1个雨季出行透明长'], '材料采购', '雨衣采购'),
    (['一次性成人雨衣', '加厚透明雨披'], '材料采购', '雨衣采购'),
    (['雨衣', '雨衣收纳袋'], '材料采购', '雨衣、雨衣收纳袋采购'),
    (['雨衣', '胶带'], '材料采购', '雨衣、胶带采购'),
    (['鼠标垫', '鼠标'], '材料采购', '鼠标垫、鼠标采购'),
    (['鼠标', '键盘', '亚克力导视牌'], '材料采购', '鼠标、键盘、亚克力导视牌采购'),
    (['胶带', '扎带', '螺丝', '螺母', '扳手'], '材料采购', '胶带、扎带、螺丝等采购'),
    (['磁吸双面导向立牌'], '材料采购', '磁吸双面导向立牌采购'),
    (['合成高精度可调支架AB-4000-X超长型'], '材料采购', '合成高精度可调支架AB-4000-X超长型采购'),
    (['口罩', '口罩'], '材料采购', '口罩采购'),
    (['住宿费'], '酒店', '住宿费'),
    (['铁路退票费'], '高铁', '铁路退票费'),
    (['顺丰快递费'], '顺丰', '顺丰快递费'),
    (['打印制作费'], '其他', '打印制作费'),
    (['茶饮料'], '餐饮', '茶饮料'),
    ([], '材料采购', ''),
])
def test_concrete_short_names_preserve_models_and_distinct_goods(
        names: list[str], category: str, expected: str) -> None:
    original = names[:]
    assert concise_expense_name(names, category) == expected
    assert names == original
    assert concise_expense_name(names, category) == expected


def classified_rainwear(env: Any) -> tuple[Any, dict[str, Any], str, list[dict[str, Any]]]:
    jobs, tools, _ = env
    job, ident = draft(env)
    tools.execute('extract_goods', {'item_id': ident}, job)
    raw, item, _, binding = tools.content._load(job, ident)
    state = item['agent_content']
    original = state['goods'][0]
    state['goods'] = [dict(original, name='雨衣', raw_name='一次性成人雨衣便', quote='商品名称：一次性成人雨衣便'),
                      dict(original, name='携式加厚雨披1个雨季出行透明长',
                           raw_name='携式加厚雨披1个雨季出行透明长',
                           quote='携式加厚雨披1个雨季出行透明长 x33')]
    state.update(status='classified', decision={'category': '材料采购', 'source': 'local'})
    tools.content._save(job, raw, item, state, binding, {'category': '材料采购'})
    return tools, job, ident, deepcopy(state['goods'])


def test_naming_changes_heading_only_and_keeps_complete_goods_evidence(environment: Any) -> None:
    tools, job, ident, goods = classified_rainwear(environment)
    before = tools.store.get(ident)
    result = tools.execute('name_expense', {'item_id': ident}, job)
    after = tools.store.get(ident)
    assert result['title'] == after['title'] == after['display_name'] == '雨衣采购'
    assert after['project_name'] == '雨衣采购'
    assert after['agent_content']['goods'] == goods
    for key in ('amount_cents', 'category', 'attachments', 'stage', 'verified'):
        assert after[key] == before[key]
    assert not after['verified'] and after['stage'] == 'draft'
    assert tools.execute('name_expense', {'item_id': ident}, job)['title'] == '雨衣采购'
    assert tools.store.get(ident)['agent_content']['goods'] == goods


def test_persons_preferred_name_is_not_shortened(environment: Any) -> None:
    tools, job, ident, goods = classified_rainwear(environment)
    raw, item, _, binding = tools.content._load(job, ident)
    state = item['agent_content']
    chosen = '人工指定' + '现场劳保防护用品及其他物资报销名称' * 11
    assert 180 < len(chosen) <= 200
    state['preferences'] = {'changes': {'display_name': chosen}, 'conflicts': []}
    tools.content._save(job, raw, item, state, binding)
    tools.execute('name_expense', {'item_id': ident}, job)
    after = tools.store.get(ident)
    assert after['title'] == after['display_name'] == chosen
    assert after['agent_content']['goods'] == goods


def test_long_multiple_names_omit_whole_labels_without_losing_evidence(environment: Any) -> None:
    tools, job, ident, _ = classified_rainwear(environment)
    raw, item, _, binding = tools.content._load(job, ident)
    state = item['agent_content']
    labels = [brand + '可调节磁吸展示支架' * 7 + '-AB4000-X' for brand in ('甲牌', '乙牌', '丙牌')]
    assert all(70 <= len(label) <= 80 for label in labels)
    original = state['goods'][0]
    state['goods'] = [dict(original, name=label, raw_name=label, quote='商品：' + label) for label in labels]
    goods = deepcopy(state['goods'])
    tools.content._save(job, raw, item, state, binding)
    expected = labels[0] + '等采购'
    assert concise_expense_name(labels, '材料采购') == expected
    tools.execute('name_expense', {'item_id': ident}, job)
    after = tools.store.get(ident)
    assert after['title'] == after['display_name'] == expected
    assert after['agent_content']['goods'] == goods


def test_single_oversized_name_requires_a_short_manual_name_without_slicing() -> None:
    label = '完整型号' * 51
    with pytest.raises(ValueError, match='人工工作台'):
        concise_expense_name([label], '材料采购')


def test_manual_edit_cannot_be_overwritten_by_a_shorter_name(environment: Any) -> None:
    tools, job, ident, goods = classified_rainwear(environment)
    with tools.store.connect() as db:
        import json
        item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        item['title'] = '人工现场项目甲雨衣费用'
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(item, ensure_ascii=False), ident))
    with pytest.raises(ValueError, match='人工修改'):
        tools.execute('name_expense', {'item_id': ident}, job)
    after = tools.store.get(ident)
    assert after['title'] == '人工现场项目甲雨衣费用'
    assert after['agent_content']['goods'] == goods
