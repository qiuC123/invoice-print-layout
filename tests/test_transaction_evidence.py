"""Source-grounded actor extraction and namespace-safe transaction identifiers."""
from __future__ import annotations

from copy import deepcopy
import pytest

from invoice_print_layout.transaction_evidence import (
    extract_transaction_evidence, identifier_key, identifiers_for_record, record_identifier_keys,
)


def test_flash_purchase_and_payment_keep_three_actor_roles_and_distinct_identifiers() -> None:
    order = extract_transaction_evidence(
        '订单已送达\n感谢你对淘宝闪购的信任，期待再次光临\n'
        '店铺：测试便利店（展馆店）\n实付款 ¥17\n订单号\n4006 1234 5678 9012 345 | 复制\n'
        '支付方式\n支付宝\n下单时间\n2026-01-05 16:23:43.650', 'order')
    payment = extract_transaction_evidence(
        '账单详情\n淘宝闪购\n-17.00\n交易成功\n商品说明：测试便利店(展馆店)\n'
        '付款方式：测试银行储蓄卡(1234)\n支付时间：2026-01-05 16:23:47\n'
        '订单号：2026010523001100000000000001\n商家订单号：131606007260105000000000001',
        'payment', {'merchant': '淘宝闪购', 'goods': '测试便利店(展馆店)'})
    assert order['fields'] == {'shopping_platform': '淘宝闪购', 'shop_name': '测试便利店(展馆店)',
                                'payment_channel': '支付宝'}
    assert payment['fields'] == {'shopping_platform': '淘宝闪购', 'shop_name': '测试便利店(展馆店)',
                                  'payment_channel': ''}
    assert order['identifiers'][0]['kind'] == 'shopping_order'
    assert order['identifiers'][0]['namespace'] == '淘宝闪购'
    assert order['identifiers'][0]['value'] == '4006123456789012345'
    assert payment['identifiers'][0]['kind'] == 'payment_order'
    assert payment['identifiers'][0]['namespace'] == ''
    assert payment['identifiers'][1]['kind'] == 'merchant_order'
    assert payment['identifiers'][1]['namespace'] == '淘宝闪购'
    assert identifier_key(order['identifiers'][0])
    assert not identifier_key(payment['identifiers'][0])
    assert '感谢你对淘宝闪购' in order['evidence']['shopping_platform']
    assert payment['evidence']['shop_name'] == '商品说明：测试便利店(展馆店)'


@pytest.mark.parametrize('text', [
    '淘宝闪购\n店铺名称：京东便利店（测试分店）',
    '感谢你对淘宝\n闪购的信任，期待再次光临\n京东便利店（测试分店）',
    '购物平台：淘宝闪购\n商品说明 京东便利店（测试分店）',
])
def test_flash_text_and_branch_heading_do_not_confuse_jd_with_platform(text: str) -> None:
    result = extract_transaction_evidence(text, 'receipt')
    assert result['fields']['shopping_platform'] == '淘宝闪购'
    assert result['fields']['shop_name'] == '京东便利店(测试分店)'


@pytest.mark.parametrize('text', [
    '闪购\n-17.00\n账单详情\n付款方式：测试银行储蓄卡(1234)',
    '京东便利店（测试分店）\n订单号 4006123456789012345',
    '13:57\n订单号：2026010523001100000000000001\n银行卡',
    '商品说明：测试牛奶一盒\n支持支付宝和微信支付',
    '不是淘宝闪购的订单\n请参考淘宝闪购订单格式',
])
def test_platform_and_payment_channel_are_not_guessed_from_icons_ids_or_bank(text: str) -> None:
    result = extract_transaction_evidence(text, 'payment')
    assert result['fields']['shopping_platform'] == ''
    assert result['fields']['payment_channel'] == ''


@pytest.mark.parametrize(('text', 'channel'), [
    ('支付方式\n支付宝', '支付宝'),
    ('付款方式：微信支付', '微信支付'),
    ('支付方式：微信', '微信支付'),
    ('支付渠道：银联', '银联'),
    ('支付宝账单详情\n订单号：202601050000000001', '支付宝'),
    ('支付宝交易号：202601050000000001', '支付宝'),
    ('微信支付订单号：4200000000202601051234567890', '微信支付'),
])
def test_only_explicit_payment_channel_evidence_is_used(text: str, channel: str) -> None:
    result = extract_transaction_evidence(text, 'payment')
    assert result['fields']['payment_channel'] == channel
    assert result['evidence']['payment_channel']


def test_conflicting_actor_names_are_preserved_as_unknown_instead_of_choosing_first() -> None:
    result = extract_transaction_evidence(
        '淘宝闪购\n美团外卖\n店铺：测试便利店(甲店)\n店铺：测试便利店(乙店)\n'
        '付款方式：支付宝\n支付方式：微信支付', 'payment')
    assert result['fields'] == {'shopping_platform': '', 'shop_name': '', 'payment_channel': ''}
    assert result['evidence'] == {'shopping_platform': '', 'shop_name': '', 'payment_channel': ''}


def test_fields_need_complete_source_evidence_and_never_turn_platform_into_shop() -> None:
    fields = {'merchant': '淘宝闪购', 'shop_name': '没有原文的便利店', 'goods': '测试便利店(甲店)'}
    before = deepcopy(fields)
    result = extract_transaction_evidence('淘宝闪购\n商品说明：测试便利店(甲店)', 'payment', fields)
    assert result['fields']['shop_name'] == '测试便利店(甲店)'
    assert fields == before
    other = extract_transaction_evidence('不是测试便利店(甲店)\n测试便利店(甲店一号店)', 'payment', fields)
    assert other['fields']['shop_name'] == ''


def test_source_merchant_heading_is_used_without_inventing_a_platform() -> None:
    result = extract_transaction_evidence('测试餐饮(东店)\n实付17.00', 'receipt',
                                            {'merchant': '测试餐饮(东店)'})
    assert result['fields']['shop_name'] == '测试餐饮(东店)'
    assert result['fields']['shopping_platform'] == ''


def test_order_namespaces_are_separate_from_payment_ids_even_for_identical_values() -> None:
    same_value = '202601050000000001'
    purchase = extract_transaction_evidence('淘宝闪购\n订单号：' + same_value, 'order')['identifiers'][0]
    payment = extract_transaction_evidence('支付宝\n订单号：' + same_value, 'payment')['identifiers'][0]
    transaction = extract_transaction_evidence('支付宝\n交易号：' + same_value, 'payment')['identifiers'][0]
    other_platform = extract_transaction_evidence('美团外卖\n订单号：' + same_value, 'order')['identifiers'][0]
    assert len({identifier_key(item) for item in (purchase, payment, transaction, other_platform)}) == 4
    assert all(identifier_key(item) for item in (purchase, payment, transaction, other_platform))


def test_merchant_order_scope_requires_platform_or_shop_not_only_payment_channel() -> None:
    result = extract_transaction_evidence('支付宝\n商家订单号：202601050000000001', 'payment')
    assert not identifier_key(result['identifiers'][0])
    scoped = extract_transaction_evidence('支付宝\n店铺：测试便利店\n商家订单号：202601050000000001', 'payment')
    assert scoped['identifiers'][0]['namespace'] == '测试便利店'
    assert identifier_key(scoped['identifiers'][0])


@pytest.mark.parametrize(('role', 'text', 'kind'), [
    ('order', '支付宝\n订单号：202601050000000001', 'shopping_order'),
    ('payment', '淘宝闪购\n订单号：202601050000000001', 'payment_order'),
    ('invoice', '淘宝闪购\n订单号：202601050000000001', 'untyped_order'),
    ('other', '淘宝闪购\n订单已送达\n订单号：202601050000000001', 'shopping_order'),
])
def test_order_type_uses_material_role_or_clear_order_page(role: str, text: str, kind: str) -> None:
    result = extract_transaction_evidence(text, role)
    assert result['identifiers'][0]['kind'] == kind


def test_other_numeric_fields_never_become_order_identifiers() -> None:
    result = extract_transaction_evidence(
        '淘宝闪购\n电话：13800138000\n纳税人识别号：91110000TEST000001A\n'
        '发票号码：12345678901234567890\n发票订单号：12345678901234567890\n'
        '合计：17.00\n12345678901234567890\n订单号\n电话：13800138000', 'receipt')
    assert result['identifiers'] == []


@pytest.mark.parametrize('number', ['13800138000', '17.00', '2026-01-05', '0000000000', 'ABCDEF'])
def test_invalid_or_unrelated_number_shapes_do_not_create_identifier_keys(number: str) -> None:
    result = extract_transaction_evidence('淘宝闪购\n订单号：' + number, 'order')
    assert not result['identifiers']
    assert not identifier_key({'kind': 'shopping_order', 'namespace': '淘宝闪购', 'value': number})


def test_different_same_type_numbers_are_retained_without_claiming_unique_scope() -> None:
    result = extract_transaction_evidence(
        '淘宝闪购\n订单号：202601050000000001\n订单号：202601050000000002', 'order')
    assert len(result['identifiers']) == 2
    assert {item['value'] for item in result['identifiers']} == {'202601050000000001', '202601050000000002'}
    assert all(not identifier_key(item) for item in result['identifiers'])


def test_record_helpers_merge_metadata_and_source_while_retaining_original_quotes() -> None:
    item = {'kind': 'shopping_order', 'namespace': '淘宝闪购', 'value': '202601050000000001',
            'label': '订单号', 'quote': ''}
    record = {'text': '淘宝闪购\n订单号：202601050000000001', 'role': 'order',
              'transaction_identifiers': [item], 'fields': {'transaction_identifiers': [deepcopy(item)]}}
    before = deepcopy(record)
    result = identifiers_for_record(record)
    assert len(result) == 1
    assert result[0]['quote'] == '订单号：202601050000000001'
    assert record_identifier_keys(record) == {identifier_key(item)}
    assert record == before


@pytest.mark.parametrize('metadata', [None, 'invalid', {'value': '202601050000000001'},
                                      [None, 7, 'bad', {'kind': [], 'value': '202601050000000001'}]])
def test_malformed_metadata_is_ignored_without_crashing_or_scoping_bare_order_field(metadata: object) -> None:
    record = {'role': 'order', 'text': '淘宝闪购', 'transaction_identifiers': metadata,
              'fields': {'order_number': '202601050000000001', 'transaction_identifiers': metadata}}
    assert identifiers_for_record(record) == []
    assert record_identifier_keys(record) == set()


def test_unscoped_record_identifiers_are_retained_but_never_hard_deduplicated() -> None:
    item = {'kind': 'untyped_order', 'namespace': '', 'value': '202601050000000001',
            'label': '订单号', 'quote': '订单号：202601050000000001'}
    record = {'transaction_identifiers': [item, deepcopy(item)]}
    assert identifiers_for_record(record) == [item]
    assert record_identifier_keys(record) == set()


def test_aggregated_record_retains_two_explicit_payment_identifiers_for_duplicate_checks() -> None:
    record = {'role': 'payment', 'text': '支付宝\n订单号：202601050000000002',
              'transaction_identifiers': [{'kind': 'payment_order', 'namespace': '支付宝',
                                           'value': '202601050000000001', 'quote': '第一份付款材料'}]}
    assert len(identifiers_for_record(record)) == 2
    assert len(record_identifier_keys(record)) == 2


@pytest.mark.parametrize('namespace', ['', '  ', 'unknown', '未知', '支付渠道'])
def test_generic_namespace_placeholders_are_not_hard_duplicate_keys(namespace: str) -> None:
    assert not identifier_key({'kind': 'payment_order', 'namespace': namespace,
                               'value': '202601050000000001'})


def test_namespace_typography_is_normalised_without_merging_id_types() -> None:
    item = {'kind': 'shopping_order', 'namespace': ' 淘宝 闪购 ', 'value': '2026 0105 0000 0000 01'}
    assert identifier_key(item) == identifier_key({**item, 'namespace': '淘宝闪购',
                                                  'value': '202601050000000001'})
def test_flash_badge_joined_to_shop_heading_is_not_part_of_the_shop_name() -> None:
    observed = extract_transaction_evidence(
        '订单已送达\n感谢你对淘宝闪购的信任\n闪购京东便利店（合成湖店）\n实付款￥17', 'order')
    assert observed['fields']['shopping_platform'] == '淘宝闪购'
    assert observed['fields']['shop_name'] == '京东便利店(合成湖店)'
    assert '闪购京东便利店' in observed['evidence']['shop_name']


@pytest.mark.parametrize('name', ['餐饮', '酒店旅游', '付款平台', '某银行',
                                 '携程旅行网', '酒店住宿', '支付宝', '其他'])
def test_generic_category_platform_or_bank_is_not_an_actual_shop(name: str) -> None:
    observed = extract_transaction_evidence('交易成功\n' + name, 'payment', {'merchant': name})
    assert observed['fields']['shop_name'] == ''


@pytest.mark.parametrize('separator', ['丨', '│'])
def test_order_number_copy_separator_from_ocr_is_not_part_of_identifier(separator: str) -> None:
    text = f'淘宝闪购\n订单号 4006123456789012345{separator}复制'
    observed = extract_transaction_evidence(text, 'receipt')
    assert len(observed['identifiers']) == 1
    assert observed['identifiers'][0]['value'] == '4006123456789012345'
    assert observed['identifiers'][0]['namespace'] == '淘宝闪购'
    assert separator + '复制' in observed['identifiers'][0]['quote']


@pytest.mark.parametrize('annotation', ['开票50元', '开票￥50.00元'])
def test_invoice_amount_annotation_is_not_part_of_actual_shop(annotation: str) -> None:
    name = '合成图文印务有限公司 ' + annotation
    observed = extract_transaction_evidence('交易成功\n' + name, 'payment', {'merchant': name})
    assert observed['fields']['shop_name'] == ''

