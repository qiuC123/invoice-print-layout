"""Wrapped native names keep exact specs; only bound footer fields are excluded."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from invoice_print_layout.agent_product_rules import inspect_product_rules, native_wrapped_product
from tests.test_agent_product_rules import CATEGORIES


def water() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    full = '娃哈哈饮用水596ml*12瓶'
    delivery = {'name': '派送服务', 'raw_name': '*生产生活服务*派送服务', 'method': 'native_geometry',
                'quote': '*生产生活服务*派送服务 无 次 1 7.55 7.55 6% 0.45',
                'amount_cents': 755, 'tax_cents': 45, 'is_discount': False}
    product = {'name': full, 'raw_name': '*软饮料*' + full, 'method': 'native_geometry',
               'quote': '*软饮料*娃哈哈饮用水59 596ml*12 组 2 11.325 22.65 13% 2.95\n6ml*12瓶',
               'quantity': '2', 'unit': '组', 'amount_cents': 2265, 'tax_cents': 295, 'is_discount': False}
    discount = {**deepcopy(product), 'quote': '*软饮料*娃哈哈饮用水59 -3.54 13% -0.46\n6ml*12瓶',
                'amount_cents': -354, 'tax_cents': -46, 'is_discount': True}
    source = {'id': 'water-source', 'sha256': 'a' * 64, 'role': 'invoice', 'complete': True,
              'goods_complete': True, 'goods_issues': [], 'issues': [], 'total_cents': 2960,
              'source_text_truncated': False, 'goods_text_truncated': False,
              'source_text': ('电子发票（普通发票）\n项目名称\n规格型号\n单位\n数量\n金额\n合\n计\n'
                              '价税合计（大写）\n开票人：\n*生产生活服务*派送服务\n无\n次\n1\n7.55\n7.55\n6%\n0.45\n'
                              '*软饮料*娃哈哈饮用水59\n596ml*12\n组\n2\n11.325\n22.65\n13%\n2.95\n6ml*12瓶\n'
                              '*软饮料*娃哈哈饮用水59\n-3.54\n13%\n-0.46\n6ml*12瓶\n'
                              '¥26.66\n¥2.94\n¥29.60\n贰拾玖圆陆角\n测试开票人'),
              'goods': [delivery, product, discount]}
    source['native_metadata'] = [
        {'kind': kind, 'value': value, 'quote': label + '\n' + value, 'page': 1,
         'bbox': [20.0, y, 160.0, y + 10], 'method': 'native_geometry', 'sha256': source['sha256']}
        for kind, label, value, y in [('invoice_total_words', '价税合计（大写）', '贰拾玖圆陆角', 280.0),
                                      ('issuer', '开票人：', '测试开票人', 368.0)]]
    goods = [{**deepcopy(row), 'name': '饮用水', 'raw_name': full,
              'material_id': source['id'], 'sha256': source['sha256']} for row in [product, discount]]
    return goods, [source]


def test_real_shape_wrap_spec_and_two_label_bound_footer_values_preserve_complete_coverage() -> None:
    goods, sources = water()
    before = deepcopy((goods, sources))
    assert native_wrapped_product(sources[0]['goods'][1], sources[0]['source_text']) == '娃哈哈饮用水596ml*12瓶'
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['coverage_complete'] is True and result['status'] == 'exact'
    assert result['category'] == '餐饮'
    assert [row['name'] for row in result['products']] == ['娃哈哈饮用水596ml*12瓶'] * 2
    assert sum(row['amount_cents'] + row['tax_cents'] for row in sources[0]['goods']) == 2960
    assert goods[0]['quantity'] == '2' and goods[1]['is_discount'] is True
    assert (goods, sources) == before


@pytest.mark.parametrize('extra', ['安装服务', '租赁费', '未知仪器'])
def test_a_word_in_specification_columns_cannot_be_ignored(extra: str) -> None:
    _, sources = water()
    row = sources[0]['goods'][1]
    row['quote'] = row['quote'].replace('596ml*12 ', '596ml*12' + extra + ' ')
    text = sources[0]['source_text'].replace('596ml*12\n', '596ml*12' + extra + '\n')
    assert native_wrapped_product(row, text) == ''


def test_specification_must_be_in_the_complete_raw_name_not_just_look_numeric() -> None:
    _, sources = water()
    row = sources[0]['goods'][1]
    row['quote'] = row['quote'].replace('596ml*12 ', '600ml*12 ')
    text = sources[0]['source_text'].replace('596ml*12\n', '600ml*12\n')
    assert native_wrapped_product(row, text) == ''


def test_no_native_metadata_means_dangling_name_and_uppercase_amount_remain_uncovered() -> None:
    goods, sources = water()
    sources[0].pop('native_metadata')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input' and result['coverage_complete'] is False


@pytest.mark.parametrize('field,value', [('method', 'intake_text'), ('sha256', 'b' * 64),
    ('kind', 'untrusted_footer'), ('page', 0), ('bbox', [0, 0, 0, 0]),
    ('quote', '商品名称：\n测试开票人'), ('value', '测试开票人未知')])
def test_unbound_or_wrong_footer_field_cannot_exempt_a_text_row(field: str, value: Any) -> None:
    goods, sources = water()
    sources[0]['native_metadata'][1][field] = value
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input' and result['coverage_complete'] is False


@pytest.mark.parametrize('defect', ['missing_field', 'duplicate_kind', 'repeated_value'])
def test_missing_ambiguous_or_repeated_footer_evidence_stays_visible(defect: str) -> None:
    goods, sources = water()
    if defect == 'missing_field':
        sources[0]['native_metadata'].pop()
    elif defect == 'duplicate_kind':
        sources[0]['native_metadata'].append(deepcopy(sources[0]['native_metadata'][1]))
    else:
        sources[0]['source_text'] += '\n测试开票人'
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('extra', ['未知仪器', '安装服务', '租赁服务'])
def test_a_real_extra_row_after_bound_metadata_is_still_an_uncovered_product(extra: str) -> None:
    goods, sources = water()
    sources[0]['source_text'] += '\n' + extra
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input' and result['coverage_complete'] is False


def test_footer_value_with_unknown_trailing_text_is_not_a_labelled_field_match() -> None:
    goods, sources = water()
    sources[0]['source_text'] = sources[0]['source_text'].replace('测试开票人', '测试开票人安装服务')
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('suffix', ['蛋糕', '安装服务', '租赁费'])
def test_complete_wrapped_food_or_service_qualifier_is_retained_not_shortened(suffix: str) -> None:
    goods, sources = water()
    for row in sources[0]['goods'][1:]:
        for field in ('name', 'raw_name', 'quote'):
            row[field] = row[field].replace('12瓶', '12瓶' + suffix)
    for row in goods:
        row['raw_name'] += suffix
        row['quote'] = row['quote'].replace('12瓶', '12瓶' + suffix)
    sources[0]['source_text'] = sources[0]['source_text'].replace('12瓶', '12瓶' + suffix)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['coverage_complete'] is True
    assert result['goods_text'] == '\n'.join(['娃哈哈饮用水596ml*12瓶' + suffix] * 2)
    assert result['status'] != 'exact' and '材料采购' in result['forbidden_categories']
