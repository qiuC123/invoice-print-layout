from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from invoice_print_layout.agent_product_rules import inspect_product_rules, inspect_supplementary_invoice


CATEGORIES = {'鼠标': '材料采购', '键盘': '材料采购', '口罩': '材料采购',
              '胶带': '材料采购', '午餐': '餐饮', '矿泉水': '餐饮', '住宿费': '酒店',
              '饮用水': '餐饮', '茶饮料': '餐饮'}


def evidence(names: list[str], *, source_names: list[str] | None = None,
             text: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_names = names if source_names is None else source_names
    source_text = text if text is not None else '商品名称：' + '、'.join(source_names) + '\n实付：48.00'
    goods = [{'name': name, 'raw_name': name, 'quote': '商品名称：' + '、'.join(source_names),
              'material_id': 'purchase', 'sha256': 'a' * 64, 'quantity': '2', 'unit': '件'} for name in names]
    return goods, [{'id': 'purchase', 'sha256': 'a' * 64, 'role': 'purchase',
                    'source_text': source_text, 'source_text_truncated': False,
                    'goods_text_truncated': False, 'goods_complete': True, 'goods_issues': [], 'goods': []}]


@pytest.mark.parametrize('names, category', [
    (['鼠标', '亚克力导视牌'], '材料采购'),
    (['亚克力导视牌', '鼠标'], '材料采购'),
    (['亚克力导视牌'], '材料采购'),
    (['透明亚克力导视牌'], '材料采购'),
    (['办公鼠标'], '材料采购'),
    (['鼠标', '键盘'], '材料采购'),
    (['亚克力导视牌', '亚克力导视牌'], '材料采购'),
    (['住宿费'], '酒店'),
])
def test_complete_exact_product_rules(names: list[str], category: str) -> None:
    goods, sources = evidence(names)
    before = deepcopy((goods, sources, CATEGORIES))
    decision = inspect_product_rules(goods, sources, CATEGORIES)
    assert decision['status'] == 'exact'
    assert decision['category'] == category
    assert decision['coverage_complete'] is True
    assert decision['goods_text'] == '\n'.join(names)
    assert [row['name'] for row in decision['products']] == names
    assert (goods, sources, CATEGORIES) == before


@pytest.mark.parametrize('product', [
    '鼠标蛋糕', '亚克力导视牌安装服务', '亚克力导视牌设计费', '亚克力导视牌租赁费',
    '亚克力导视牌制作服务', '蓝色亚克力导视牌', '亚克力饮料杯', '未知仪器',
    '鼠标及配套安装', '亚克力导视牌（含安装）', '亚克力', '鼠标蛋糕模具',
])
def test_unknown_complete_product_keeps_full_text_for_model(product: str) -> None:
    goods, sources = evidence([product])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['category'] is None
    assert result['coverage_complete'] is True
    assert result['goods_text'] == product


@pytest.mark.parametrize('full, canonical', [
    ('鼠标蛋糕', '鼠标'), ('鼠标安装服务', '鼠标'), ('亚克力导视牌租赁费', '亚克力导视牌'),
])
def test_canonical_short_name_never_hides_real_product(full: str, canonical: str) -> None:
    goods, sources = evidence([canonical], source_names=[full])
    goods[0]['raw_name'] = canonical  # Old records may have lost it here too.
    goods[0]['quote'] = '商品名称：' + canonical
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['category'] is None
    assert result['goods_text'] == full
    assert goods[0]['name'] == canonical


@pytest.mark.parametrize('names', [
    ['鼠标', '亚克力导视牌', '午餐'], ['亚克力导视牌', '矿泉水'],
    ['鼠标', '鼠标蛋糕'], ['鼠标蛋糕', '亚克力导视牌'],
    ['鼠标', '亚克力导视牌安装服务'], ['鼠标', '亚克力导视牌租赁费'],
])
def test_food_and_service_mixtures_need_input(names: list[str]) -> None:
    goods, sources = evidence(names)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input'
    assert result['category'] is None
    assert result['goods_text'] == '\n'.join(names)


@pytest.mark.parametrize('text', [
    '商品名称：鼠标\n商品名称：亚克力导视牌\n实付：48.00',
    '商品名称：\n鼠标\n亚克力导视牌\n实付：48.00',
    '商品明细\n鼠标\n数量：1\n售价：20\n亚克力导视牌\n实付：48.00',
    '商品名称：鼠标\n亚克力导视牌\n实付：48.00',
    '*计算机设备*鼠标\n*文化用品*亚克力导视牌\n价税合计：48.00',
])
def test_multiline_multilabel_and_invoice_tax_prefix(text: str) -> None:
    goods, sources = evidence(['鼠标', '亚克力导视牌'], text=text)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact'
    assert result['goods_text'] == '鼠标\n亚克力导视牌'


@pytest.mark.parametrize('text', [
    '商品名称：鼠标\n未知仪器\n实付：48.00',
    '商品名称：鼠标\n商品名称：未知仪器\n实付：48.00',
    '商品明细\n鼠标\n数量：1\n未知仪器\n实付：48.00',
])
def test_unlabelled_or_later_missing_item_does_not_disappear(text: str) -> None:
    goods, sources = evidence(['鼠标'], text=text)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input'
    assert '漏提' in result['reason']
    assert result['coverage_complete'] is False


def test_one_raw_label_cannot_cover_a_missing_unknown_neighbour() -> None:
    goods, sources = evidence(['鼠标'], source_names=['鼠标', '未知仪器'])
    goods[0]['raw_name'] = '鼠标、未知仪器'
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_canonical_mouse_and_split_cake_continuation_are_not_materials() -> None:
    goods, sources = evidence(['鼠标'], text='商品名称：鼠标\n蛋糕\n实付：48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input'
    assert result['category'] is None


@pytest.mark.parametrize('flag, value', [
    ('source_text_truncated', True), ('goods_text_truncated', True), ('goods_complete', False),
    ('goods_issues', ['商品行超过20条，尚未完整提取']), ('source_text', ''),
])
def test_incomplete_source_needs_input(flag: str, value: Any) -> None:
    goods, sources = evidence(['鼠标', '亚克力导视牌'])
    sources[0][flag] = value
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_unknown_neighbour_reaches_model_with_all_full_goods() -> None:
    goods, sources = evidence(['鼠标', '亚克力导视牌', '未知仪器'])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['goods_text'] == '鼠标\n亚克力导视牌\n未知仪器'


@pytest.mark.parametrize('field, value', [('sha256', 'replaced'), ('material_id', 'other')])
def test_stale_or_wrong_source_binding_is_not_classification_evidence(field: str, value: str) -> None:
    goods, sources = evidence(['鼠标'])
    goods[0][field] = value
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_native_geometry_retains_full_wrapped_name_and_numeric_evidence() -> None:
    goods, sources = evidence(['亚克力导视牌'])
    sources[0]['role'] = 'invoice'
    sources[0]['source_text'] = '*文化用品*亚克力导视牌 2 个 24.00 48.00\n价税合计48.00'
    sources[0]['goods'] = [{'name': '亚克力导视牌', 'raw_name': '*文化用品*亚克力导视牌',
                            'quote': '*文化用品*亚克力导视牌 2 个 24.00 48.00',
                            'method': 'native_geometry', 'quantity': '2', 'amount_cents': 4800}]
    before = deepcopy((goods, sources))
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'exact'
    assert (goods, sources) == before


@pytest.mark.parametrize('label', ['开票人', '收款人', '复核人', '复核'])
def test_inline_invoice_signature_is_not_a_product(label: str) -> None:
    goods, sources = evidence(['鼠标'], text=f'*办公用品*鼠标\n{label}：合成甲')
    sources[0]['role'] = 'invoice'
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact' and result['coverage_complete'] is True
    assert result['goods_text'] == '鼠标'


def test_same_name_in_two_sources_keeps_each_source_and_no_quantity_inference() -> None:
    goods, sources = evidence(['鼠标'])
    second_good, second_source = deepcopy(goods[0]), deepcopy(sources[0])
    second_good.update(material_id='invoice', sha256='b' * 64)
    second_source.update(id='invoice', sha256='b' * 64, role='invoice')
    goods.append(second_good)
    sources.append(second_source)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact'
    assert result['goods_text'] == '鼠标\n鼠标'
    assert [row['material_id'] for row in result['products']] == ['purchase', 'invoice']
    assert [row['quantity'] for row in goods] == ['2', '2']


def test_duplicate_source_products_need_distinct_extracted_records() -> None:
    goods, sources = evidence(['鼠标'], source_names=['鼠标', '鼠标'])
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_payment_cannot_complete_missing_purchase_goods() -> None:
    goods, sources = evidence(['鼠标'])
    sources[0]['role'] = 'payment'
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_payment_product_description_does_not_replace_the_purchase_original() -> None:
    goods, sources = evidence(['鼠标', '亚克力导视牌'])
    sources.append({'id': 'payment', 'sha256': 'c' * 64, 'role': 'payment',
                    'source_text': '商品说明：平台商户', 'goods_complete': False})
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'exact'


def test_source_without_explicit_goods_scope_cannot_use_merchant_nouns() -> None:
    goods, sources = evidence(['鼠标'], text='商家名称：鼠标\n实付：48.00')
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('product', ['鼠标蛋糕', '亚克力导视牌安装费', '亚克力导视牌租赁服务',
                                   '亚克力导视牌设计费', '亚克力导视牌制作服务'])
def test_complete_food_or_explicit_service_cannot_be_model_materials(product: str) -> None:
    goods, sources = evidence([product])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['forbidden_categories'] == ['材料采购']


def test_production_description_is_not_mistaken_for_a_service_fee() -> None:
    goods, sources = evidence(['手工制作亚克力导视牌'])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['forbidden_categories'] == []


def test_transparent_sign_exact_name_can_wrap_at_the_material_boundary() -> None:
    goods, sources = evidence(['透明亚克力', '导视牌'], text='商品名称：透明亚克力\n导视牌\n实付：48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact'
    assert result['goods_text'] == '透明亚克力导视牌'


def test_purchase_shop_block_keeps_full_quoted_product_for_model() -> None:
    goods, sources = evidence(['口罩'], text='订单已送达\n闪购测试便利店\n'
                              '一次性成人口罩 实付￥20\n蓝色独立包装口罩 ￥30\n打包费 ￥1\n实付￥21')
    sources[0]['goods'] = [{'name': '口罩', 'quote': '一次性成人口罩 实付￥20\n蓝色独立包装口罩 ￥30',
                            'method': 'intake_text'}]
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['goods_text'] == '一次性成人口罩\n蓝色独立包装口罩'
    assert '便利店' not in result['goods_text'] and '￥' not in result['goods_text']


def test_purchase_shop_block_keeps_a_proven_computer_word_continuation() -> None:
    goods, sources = evidence(['鼠标'], text='闪购测试便利店\n飞利浦有线鼠标USB笔记本台式 ￥16\n'
                              '电脑商务办公1个B8-3-3 加入购物车\n7天无理由退换\nx1\n打包费 ￥1')
    sources[0]['goods'] = [{'name': '鼠标', 'quote': '飞利浦有线鼠标USB笔记本台式 ￥16', 'method': 'intake_text'}]
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['goods_text'] == '飞利浦有线鼠标USB笔记本台式\n电脑商务办公1个B8-3-3'


def test_unknown_unquoted_neighbour_in_shopping_block_is_not_merged() -> None:
    goods, sources = evidence(['鼠标'], text='闪购测试便利店\n鼠标 ￥16\n未知仪器 ￥32\n打包费 ￥1')
    sources[0]['goods'] = [{'name': '鼠标', 'quote': '鼠标 ￥16', 'method': 'intake_text'}]
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('extra', ['\n商品名称：未知仪器', '\n未知仪器'])
def test_native_geometry_cannot_hide_an_uncovered_product(extra: str) -> None:
    goods, sources = evidence(['鼠标'])
    sources[0].update(role='invoice', source_text='*计算机设备*鼠标' + extra + '\n合计48.00',
                      goods=[{'name': '鼠标', 'quote': '*计算机设备*鼠标', 'method': 'native_geometry'}])
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_native_geometry_cannot_shorten_a_mouse_cake_tax_row() -> None:
    goods, sources = evidence(['鼠标'])
    sources[0].update(role='invoice', source_text='*食品*鼠标蛋糕\n合计48.00',
                      goods=[{'name': '鼠标', 'quote': '*食品*鼠标蛋糕', 'method': 'native_geometry'}])
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_bare_generic_tax_summary_does_not_replace_explicit_product() -> None:
    goods, sources = evidence(['胶带'], text='商品名称：胶带\n*日用杂品*日用百货\n实付：48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact' and result['category'] == '材料采购'
    assert result['goods_text'] == '胶带'


@pytest.mark.parametrize('tail', [
    '商品名称：日用百货', '*日用杂品*日用百货 2 个 5.00', '*日用杂品*日用百货\n2\n5.00',
    '*日用杂品*未知仪器', '*食品*鼠标蛋糕', '*文化用品*亚克力导视牌',
    '*日用杂品*日用百货\n未知仪器',
])
def test_generic_tax_exception_never_hides_a_second_item(tail: str) -> None:
    goods, sources = evidence(['胶带'], text='商品名称：胶带\n' + tail + '\n实付：48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input'
    assert result['coverage_complete'] is False


def test_generic_tax_without_explicit_product_label_stays_unresolved() -> None:
    goods, sources = evidence(['日用百货'], text='*日用杂品*日用百货\n实付：48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['category'] is None


def test_native_tea_tax_columns_and_split_total_are_not_more_products() -> None:
    text = ('电子发票\n项目名称 规格型号 单位 数量 单价 金额 税率/征收率 税额\n'
            '*软饮料*茶饮料 55.64 1% 0.56\n￥55.64 ￥0.56\n计\n伍拾陆圆贰角整\n'
            '价税合计（大写）\n备注')
    goods, sources = evidence(['茶饮料'], text=text)
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact' and result['category'] == '餐饮'
    assert result['goods_text'] == '茶饮料'


def test_single_numeric_specification_is_not_removed_as_invoice_columns() -> None:
    goods, sources = evidence(['鼠标 2.0'], text='*计算机设备*鼠标 2.0\n合计48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['goods_text'] == '鼠标 2.0'


@pytest.mark.parametrize('product, auxiliary', [
    ('饮用水', '*生产生活服务*派送服务'),
    ('茶饮料', '*生产生活服务*物流网络\n次\n1.00\n6%\n配送费'),
])
def test_exact_incidental_delivery_remains_compatible(product: str, auxiliary: str) -> None:
    goods, sources = evidence([product], text='商品名称：' + product + '\n' + auxiliary + '\n实付48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'exact' and result['category'] == '餐饮'
    assert result['goods_text'] == product


@pytest.mark.parametrize('auxiliary', ['*生产生活服务*派送服务',
                                    '*生产生活服务*物流网络\n次\n1.00\n6%\n配送费'])
def test_product_after_incidental_delivery_cannot_disappear(auxiliary: str) -> None:
    goods, sources = evidence(['茶饮料'], text='商品名称：茶饮料\n' + auxiliary + '\n鼠标蛋糕\n实付48.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input' and result['coverage_complete'] is False


def test_native_geometry_incidental_delivery_keeps_extra_unknown_rows_visible() -> None:
    goods, sources = evidence(['饮用水'])
    sources[0].update(role='invoice', source_text='*软饮料*饮用水\n*生产生活服务*派送服务\n未知仪器\n合计48.00',
                      goods=[{'name': '饮用水', 'quote': '*软饮料*饮用水', 'method': 'native_geometry'},
                             {'name': '派送服务', 'quote': '*生产生活服务*派送服务', 'method': 'native_geometry'}])
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_existing_project_detail_header_supports_validated_wrapped_goods() -> None:
    goods, sources = evidence(['不锈钢保温杯', '加厚帆布袋'],
                              text='收据\n项目明细\n不锈钢\n保温杯\n加厚帆布袋\n合计17.00')
    goods[0].update(method='glm_text_grounded', quote='不锈钢\n保温杯', source_ids=[0, 1])
    goods[1].update(method='glm_text_grounded', quote='加厚帆布袋', source_ids=[2])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['coverage_complete'] is True
    assert result['goods_text'] == '不锈钢保温杯\n加厚帆布袋'


@pytest.mark.parametrize('metadata', ['金额17.00', '金额：17.00', '商品金额17.00', '实付金额：17.00'])
def test_explicit_price_metadata_label_does_not_become_a_purchased_item(metadata: str) -> None:
    goods, sources = evidence(['不锈钢保温杯', '加厚帆布袋'],
                              text='商品名称：' + metadata + '\n项目明细\n不锈钢\n保温杯\n加厚帆布袋\n合计17.00')
    goods[0].update(method='glm_text_grounded', quote='不锈钢\n保温杯', source_ids=[0, 1])
    goods[1].update(method='glm_text_grounded', quote='加厚帆布袋', source_ids=[2])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model'
    assert result['goods_text'] == '不锈钢保温杯\n加厚帆布袋'


def test_price_like_product_name_is_not_removed() -> None:
    goods, sources = evidence(['金额牌17号'])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['goods_text'] == '金额牌17号'


@pytest.mark.parametrize('quote, method', [('不锈钢', 'glm_text_grounded'),
                                         ('不锈钢\n保温杯', 'native_text')])
def test_wrapped_goods_need_the_validated_full_completion_span(quote: str, method: str) -> None:
    goods, sources = evidence(['不锈钢保温杯'], text='项目明细\n不锈钢\n保温杯\n合计17.00')
    goods[0].update(method=method, quote=quote)
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


def test_explicit_carrier_label_is_not_a_second_purchased_service() -> None:
    goods, sources = evidence(['顺丰快递费'], text='商品名称：收派服务\n承运方：顺丰速运\n实付17.00')
    goods[0]['raw_name'] = '收派服务'  # Caller qualified this alias to this current source.
    result = inspect_product_rules(goods, sources, {**CATEGORIES, '收派服务': '顺丰'})
    assert result['status'] == 'exact' and result['category'] == '顺丰'
    assert result['goods_text'] == '收派服务'


def test_carrier_metadata_does_not_hide_a_later_unlabelled_product() -> None:
    goods, sources = evidence(['顺丰快递费'], text='商品名称：收派服务\n承运方：顺丰速运\n未知仪器\n实付17.00')
    goods[0]['raw_name'] = '收派服务'
    result = inspect_product_rules(goods, sources, {**CATEGORIES, '收派服务': '顺丰'})
    assert result['status'] == 'needs_input' and result['coverage_complete'] is False


def test_another_carrier_is_not_given_a_courier_category_by_this_module() -> None:
    goods, sources = evidence(['收派服务'], text='商品名称：收派服务\n承运方：中通快递\n实付17.00')
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['category'] is None


def test_two_different_courier_sources_do_not_inherit_one_source_name() -> None:
    goods, sources = evidence(['顺丰快递费'], text='商品名称：收派服务\n承运方：顺丰速运\n实付17.00')
    goods[0]['raw_name'] = '收派服务'
    second_goods, second_sources = evidence(['收派服务'], text='商品名称：收派服务\n承运方：中通快递\n实付17.00')
    second_goods[0].update(material_id='other-carrier', sha256='b' * 64)
    second_sources[0].update(id='other-carrier', sha256='b' * 64)
    result = inspect_product_rules(goods + second_goods, sources + second_sources, CATEGORIES)
    assert result['status'] == 'model' and result['category'] is None


def supplementary_invoice() -> dict[str, Any]:
    return {'id': 'generic-invoice', 'sha256': 'd' * 64, 'role': 'invoice',
            'complete': True, 'goods_complete': True, 'source_text_truncated': False,
            'goods_text_truncated': False, 'goods_issues': [], 'issues': [], 'total_cents': 2460,
            'source_text': ('电子发票（普通发票）\n项目名称\n规格型号\n单位\n数量\n金额\n'
                            '合\n计\n价税合计（大写）\n*日用杂品*日用百货\n1%\n24.36\n0.24\n下载次数：1'),
            'goods': [{'name': '日用百货', 'raw_name': '*日用杂品*日用百货',
                       'quote': '*日用杂品*日用百货 24.36 1% 0.24', 'method': 'native_geometry',
                       'amount_cents': 2436, 'tax_cents': 24, 'is_discount': False}]}


def test_supplementary_invoice_proof_is_non_specific_read_only_and_does_not_admit_it() -> None:
    source = supplementary_invoice()
    before = deepcopy(source)
    proof = inspect_supplementary_invoice(source)
    assert proof is not None
    assert proof['kind'] == 'supplementary_generic_invoice' and proof['non_specific'] is True
    assert proof['quote'] == source['goods'][0]['quote']
    assert proof['material_id'] == source['id'] and proof['sha256'] == source['sha256']
    assert proof['amount_cents'] == proof['item_amount_cents'] + proof['tax_cents'] == 2460
    assert 'category' not in proof and 'admitted' not in proof
    assert source == before
    goods, sources = evidence(['鼠标'])
    assert inspect_product_rules(goods, sources + [source], CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('field, value', [
    ('role', 'purchase'), ('complete', False), ('goods_complete', False),
    ('source_text_truncated', True), ('goods_text_truncated', True),
    ('issues', ['PDF读取不完整']), ('goods_issues', ['商品溢出']),
    ('source_text', ''), ('sha256', 'unvalidated'), ('id', ''),
    ('total_cents', 2461), ('total_cents', 0), ('total_cents', True),
])
def test_incomplete_or_unbound_generic_invoice_proof_is_rejected(field: str, value: Any) -> None:
    source = supplementary_invoice()
    source[field] = value
    assert inspect_supplementary_invoice(source) is None


@pytest.mark.parametrize('field', ['complete', 'goods_complete', 'source_text_truncated',
                                  'goods_text_truncated', 'goods_issues', 'issues'])
def test_supplementary_invoice_requires_explicit_completion_flags(field: str) -> None:
    source = supplementary_invoice()
    source.pop(field)
    assert inspect_supplementary_invoice(source) is None


@pytest.mark.parametrize('field, value', [
    ('method', 'native_text'), ('name', '日用百货安装服务'), ('raw_name', '*服务*日用百货'),
    ('name', '鼠标蛋糕'), ('raw_name', '*日用杂品类*日用百货'),
    ('quote', '*日用杂品*日用百货（含安装） 24.36 1% 0.24'),
    ('amount_cents', 2000), ('tax_cents', -24), ('amount_cents', True), ('is_discount', True),
])
def test_specific_qualified_or_unreconciled_native_row_is_not_generic_support(field: str, value: Any) -> None:
    source = supplementary_invoice()
    source['goods'][0][field] = value
    assert inspect_supplementary_invoice(source) is None


@pytest.mark.parametrize('addition', [
    '\n商品名称：鼠标蛋糕', '\n商品名称：日用百货', '\n购买内容：安装服务',
    '\n商品清单\n未知仪器', '\n项目明细\n鼠标', '\n*食品*鼠标蛋糕',
    '\n*日用杂品*日用百货', '\n备注：另有鼠标',
])
def test_other_product_labels_tax_rows_or_remarks_cannot_be_supplemental_generic(addition: str) -> None:
    source = supplementary_invoice()
    source['source_text'] += addition
    assert inspect_supplementary_invoice(source) is None


@pytest.mark.parametrize('suffix', ['安装服务', '未知仪器', '鼠标蛋糕'])
def test_uncovered_native_tax_continuation_cannot_be_supplementary(suffix: str) -> None:
    source = supplementary_invoice()
    source['source_text'] = '*日用杂品*日用百货\n' + suffix + '\n合计24.60'
    assert inspect_supplementary_invoice(source) is None


def test_another_native_goods_record_is_not_just_a_generic_invoice_summary() -> None:
    source = supplementary_invoice()
    source['goods'].append({'name': '鼠标', 'raw_name': '*计算机设备*鼠标',
                            'quote': '*计算机设备*鼠标 10.00 1% 0.10', 'method': 'native_geometry'})
    assert inspect_supplementary_invoice(source) is None


def test_generic_proof_still_contains_no_decision_when_it_is_the_only_source() -> None:
    source = supplementary_invoice()
    assert inspect_supplementary_invoice(source) is not None
    result = inspect_product_rules([], [source], CATEGORIES)
    assert result['status'] == 'needs_input' and result['category'] is None


@pytest.mark.parametrize('addition', ['未知仪器', '鼠标蛋糕', '安装服务', '另含附件', '下载次数：1 未知仪器'])
def test_unlabelled_text_after_download_footer_cannot_disappear_from_generic_proof(addition: str) -> None:
    source = supplementary_invoice()
    source['source_text'] += '\n' + addition
    assert inspect_supplementary_invoice(source) is None


@pytest.mark.parametrize('tail', ['合计24.60\n未知仪器', '价税合计（小写）：24.60\n鼠标蛋糕',
                                  '备注\n安装服务', '合\n计\n未知物品'])
def test_unlabelled_text_after_totals_or_empty_remark_marker_is_still_checked(tail: str) -> None:
    source = supplementary_invoice()
    source['source_text'] = '*日用杂品*日用百货\n1%\n24.36\n0.24\n' + tail
    assert inspect_supplementary_invoice(source) is None


def test_only_proven_numeric_and_amount_footer_text_can_follow_generic_native_row() -> None:
    source = supplementary_invoice()
    source['source_text'] = ('*日用杂品*日用百货\n1%\n24.36\n0.24\n合\n计\n'
                             '价税合计（小写）：24.60\n贰拾肆圆陆角整\n下载次数：1')
    assert inspect_supplementary_invoice(source) is not None


@pytest.mark.parametrize('full', ['办公鼠标蛋糕', '办公鼠标安装服务', '办公鼠标租赁费', '办公鼠标设计费'])
def test_exact_office_mouse_rule_cannot_swallow_food_or_service_suffix(full: str) -> None:
    goods, sources = evidence(['鼠标'], source_names=[full])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['category'] is None
    assert result['goods_text'] == full and '材料采购' in result['forbidden_categories']


@pytest.mark.parametrize('full', ['办公鼠标垫', '办公鼠标2.0', '办公鼠标一批'])
def test_office_mouse_rule_is_a_whole_name_not_a_prefix(full: str) -> None:
    goods, sources = evidence([full])
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'model' and result['goods_text'] == full


def test_order_status_does_not_become_a_product_or_hide_following_products() -> None:
    goods, sources = evidence(['茶饮料'], text='商品名称：茶饮料\n共6件\n订单已送达\n实付58.30')
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'exact'
    sources[0]['source_text'] = sources[0]['source_text'].replace('订单已送达', '订单已送达\n未知仪器')
    assert inspect_product_rules(goods, sources, CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('field,value', [('complete', False), ('issues', ['原件PDF未能完整读取：页数需要人工检查'])])
def test_explicit_source_failure_cannot_be_rescued_by_complete_goods(field: str, value: Any) -> None:
    goods, sources = evidence(['鼠标'])
    sources[0][field] = value
    result = inspect_product_rules(goods, sources, CATEGORIES)
    assert result['status'] == 'needs_input' and result['coverage_complete'] is False
