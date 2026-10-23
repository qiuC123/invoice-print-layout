"""A whole native dining tax row is script evidence, not a merchant-name guess."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from invoice_print_layout.agent_product_rules import inspect_product_rules
from tests.test_agent_product_rules import CATEGORIES


def dining() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = '*生产生活服务*餐饮服务'
    row = {'name': '餐饮服务', 'raw_name': raw, 'method': 'native_geometry',
           'quote': raw + ' 1 930.693069306931 930.69 1% 9.31',
           'amount_cents': 93069, 'tax_cents': 931, 'is_discount': False}
    source = {'id': 'dining-source', 'sha256': 'a' * 64, 'role': 'invoice', 'complete': True,
              'goods_complete': True, 'source_text_truncated': False, 'goods_text_truncated': False,
              'issues': [], 'goods_issues': [], 'total_cents': 94000, 'goods': [row],
              'source_text': ('电子发票（普通发票）\n项目名称\n规格型号\n单位\n数量\n金额\n'
                              '价税合计（小写）\n¥940.00\n' + raw +
                              '\n1%\n930.69\n9.31\n930.693069306931\n1')}
    return [{**deepcopy(row), 'material_id': source['id'], 'sha256': source['sha256']}], [source]


def test_complete_native_dining_tax_row_is_exact_local_category_and_retains_details() -> None:
    goods, sources = dining()
    before = deepcopy((goods, sources, CATEGORIES))
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact' and result['category'] == '餐饮'
    assert result['coverage_complete'] is True and result['goods_text'] == '餐饮服务'
    assert '材料采购' in result['forbidden_categories']
    proof = result['qualified_service_sources'][0]
    assert proof['material_id'] == sources[0]['id'] and proof['sha256'] == sources[0]['sha256']
    assert proof['amount_cents'] == proof['item_amount_cents'] + proof['tax_cents'] == 94000
    assert proof['quote'] == sources[0]['goods'][0]['quote']
    assert (goods, sources, CATEGORIES) == before


@pytest.mark.parametrize('field,value', [
    ('role', 'purchase'), ('complete', False), ('goods_complete', False),
    ('source_text_truncated', True), ('goods_text_truncated', True),
    ('issues', ['原件未完整读取']), ('goods_issues', ['缺少商品行']),
    ('sha256', 'invalid-digest'), ('total_cents', 95000),
])
def test_incomplete_or_unbound_source_does_not_get_a_dining_shortcut(field: str, value: Any) -> None:
    goods, sources = dining()
    sources[0][field] = value
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] != 'exact' and result['category'] is None
    assert not result['qualified_service_sources']


@pytest.mark.parametrize('field,value', [
    ('method', 'native_text'), ('amount_cents', 92069), ('tax_cents', 1931),
    ('quote', '*生产生活服务*餐饮服务 1 930.69 1% 19.31'),
    ('raw_name', '*租赁服务*餐饮服务'),
])
def test_tax_geometry_amounts_and_quote_are_required_not_just_the_name(field: str, value: Any) -> None:
    goods, sources = dining()
    sources[0]['goods'][0][field] = value
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] != 'exact' and result['category'] is None
    assert not result['qualified_service_sources']


def test_valid_geometry_numbers_must_really_exist_in_the_full_native_source() -> None:
    goods, sources = dining()
    sources[0]['source_text'] = sources[0]['source_text'].replace('930.693069306931', '900.00')
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] != 'exact'


@pytest.mark.parametrize('suffix', ['安装服务', '租赁费', '蛋糕'])
def test_dining_word_never_swallows_an_actual_product_or_service_suffix(suffix: str) -> None:
    goods, sources = dining()
    for row in [goods[0], sources[0]['goods'][0]]:
        for field in ('name', 'raw_name', 'quote'):
            row[field] = row[field].replace('餐饮服务', '餐饮服务' + suffix)
    sources[0]['source_text'] = sources[0]['source_text'].replace('餐饮服务', '餐饮服务' + suffix)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] != 'exact' and result['category'] is None
    assert result['goods_text'] == '餐饮服务' + suffix


@pytest.mark.parametrize('extra', ['未知仪器', '安装服务', '商品名称：鼠标', '合计940.00\n未知用途服务'])
def test_unknown_lines_after_native_tax_row_cannot_be_hidden_as_a_footer(extra: str) -> None:
    goods, sources = dining()
    sources[0]['source_text'] += '\n' + extra
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] != 'exact' and result['category'] is None


def test_explicit_dining_and_physical_material_mixture_stops_instead_of_becoming_materials() -> None:
    goods, sources = dining()
    extra = {'name': '鼠标', 'raw_name': '*计算机设备*鼠标', 'method': 'native_geometry',
             'quote': '*计算机设备*鼠标 10.00 1% 0.10', 'amount_cents': 1000, 'tax_cents': 10}
    sources[0]['goods'].append(extra)
    sources[0]['source_text'] += '\n' + extra['quote']
    goods.append({**extra, 'material_id': sources[0]['id'], 'sha256': sources[0]['sha256']})
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input' and result['category'] is None
    assert '材料采购' in result['forbidden_categories']


def test_plain_order_description_and_restaurant_merchant_do_not_prove_a_native_dining_invoice() -> None:
    goods = [{'name': '餐饮服务', 'quote': '商品名称：餐饮服务',
              'material_id': 'order', 'sha256': 'b' * 64}]
    sources = [{'id': 'order', 'sha256': 'b' * 64, 'role': 'purchase', 'complete': True,
                'source_text': '商家名称：合成餐饮有限公司\n商品名称：餐饮服务\n实付940.00',
                'source_text_truncated': False, 'goods_text_truncated': False,
                'goods_complete': True, 'goods_issues': [], 'issues': [], 'goods': []}]
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['category'] is None
    assert not result['qualified_service_sources']
