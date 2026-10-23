"""Auxiliary originals need an existing grounded relation and conserved totals."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

import pytest

from invoice_print_layout.agent_product_support import supporting_product_sources
from tests.test_agent_product_rules import supplementary_invoice


CATEGORIES = {'口罩': '材料采购', '茶饮料': '餐饮', '鼠标': '材料采购'}


def fact(ident: str, role: str, total: int, text: str, names: list[str]) -> dict[str, Any]:
    return {'id': ident, 'sha256': hashlib.sha256(ident.encode()).hexdigest(), 'role': role, 'revision': 2,
            'complete': True, 'total_cents': total, 'source_text': text, 'text': text,
            'goods_complete': bool(names), 'goods_issues': [], 'issues': [],
            'source_text_truncated': False, 'goods_text_truncated': False,
            'goods': [{'name': name, 'quote': text.splitlines()[0], 'method': 'intake_text'} for name in names]}


def records(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**deepcopy(row), 'material_id': source['id'], 'sha256': source['sha256']}
            for source in facts for row in source['goods']]


def item_for(facts: list[dict[str, Any]]) -> dict[str, Any]:
    vouchers = [f for f in facts if f['role'] == 'invoice']
    supports = [f for f in facts if f['role'] in {'order', 'purchase'}]
    total = sum(f['total_cents'] for f in vouchers)
    def ref(source: dict[str, Any]) -> dict[str, Any]:
        return {'key': 'q:' + source['id'], 'queue_id': source['id'],
                'role': source['role'], 'amount_cents': source['total_cents']}
    evidence = [{'material_id': source['id'],
                 'quote': source['goods'][0]['quote'] if source['goods'] else source['source_text'].splitlines()[-1],
                 'supports': 'amount' if source in supports else 'goods'} for source in facts]
    return {'id': 'draft', 'stage': 'draft', 'verified': False, 'amount_cents': total,
            'material_match': {'voucher_total_cents': total, 'support_ids': [f['id'] for f in supports],
                'recognition': {'amount_cents': total, 'voucher_total_cents': total,
                                'amount_source': 'vouchers', 'amount_difference_cents': 0, 'payments': [],
                                'vouchers': [ref(f) for f in vouchers], 'supports': [ref(f) for f in supports]}},
            'agent_relation': {'relation_kind': 'model_inference', 'relation_evidence': evidence,
                               'duplicate_views': []}}


def generic_pair() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    purchase = fact('purchase', 'purchase', 2460, '商品名称：口罩\n实付24.60', ['口罩'])
    facts = [purchase, supplementary_invoice()]
    return item_for(facts), facts, records([purchase])


def split_food() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    tea = fact('tea', 'invoice', 5620, '*软饮料*茶饮料 55.64 1% 0.56\n合计56.20', ['茶饮料'])
    delivery = fact('delivery', 'invoice', 210, '*生产生活服务*物流网络\n次\n1.00\n1.98\n1.98\n6%\n0.12\n配送费\n合\n计', [])
    delivery['goods_complete'] = True
    delivery['goods'] = [{'name': '物流网络配送费', 'raw_name': '*生产生活服务*物流网络配送费',
                          'quote': '*生产生活服务*物流网络 次 1.00 1.98 1.98 6% 0.12\n配送费',
                          'method': 'native_geometry', 'amount_cents': 198, 'tax_cents': 12}]
    order = fact('order', 'order', 5830, '订单已送达\n闪购饮品店\n共6件\n价格明细 实付￥58.30', [])
    facts = [order, tea, delivery]
    return item_for(facts), facts, records([tea])


def test_generic_invoice_remains_audited_while_complete_purchase_drives_classification() -> None:
    item, facts, goods = generic_pair()
    before = deepcopy((item, facts, goods))
    plan = supporting_product_sources(item, facts, goods, CATEGORIES)
    assert [f['id'] for f in plan['sources']] == ['purchase']
    assert plan['supplementary'][0]['kind'] == 'supplementary_generic_invoice'
    assert plan['supplementary'][0]['amount_cents'] == item['amount_cents'] == 2460
    assert plan['supplementary'][0]['sha256'] == facts[1]['sha256']
    assert (item, facts, goods) == before and not item['verified']


def test_complete_branded_purchase_can_use_model_after_generic_invoice_role_is_proven() -> None:
    item, facts, goods = generic_pair()
    facts[0]['source_text'] = '商品名称：一次性蓝色口罩\n实付24.60'
    facts[0]['goods'][0]['quote'] = '商品名称：一次性蓝色口罩'
    goods[0]['quote'] = facts[0]['goods'][0]['quote']
    item['agent_relation']['relation_evidence'][0]['quote'] = goods[0]['quote']
    assert supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


def test_explicit_duplicate_purchase_views_conserve_one_voucher_amount() -> None:
    item, facts, goods = generic_pair()
    duplicate = deepcopy(facts[0]); duplicate.update(id='purchase2', sha256='c' * 64)
    facts.append(duplicate); goods.extend(records([duplicate])); item = item_for(facts)
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']
    item['agent_relation']['duplicate_views'] = [['purchase', 'purchase2']]
    assert len(supporting_product_sources(item, facts, goods, CATEGORIES)['sources']) == 2


def shared_amount_views() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    item, facts, goods = generic_pair()
    duplicate = deepcopy(facts[0]); duplicate.update(id='purchase2', sha256='c' * 64)
    facts.append(duplicate); goods.extend(records([duplicate])); item = item_for(facts)
    item['agent_relation']['duplicate_views'] = [['purchase', 'purchase2']]
    for evidence in item['agent_relation']['relation_evidence']:
        if evidence['material_id'] == 'purchase':
            evidence.update(quote='实付24.60', supports='amount')
        elif evidence['material_id'] == 'purchase2':
            evidence.update(quote='商品名称：口罩', supports='goods')
    return item, facts, goods


def test_existing_complete_duplicate_views_can_share_one_current_total_quote() -> None:
    item, facts, goods = shared_amount_views()
    before = deepcopy((item, facts, goods))
    plan = supporting_product_sources(item, facts, goods, CATEGORIES)
    assert [fact['id'] for fact in plan['sources']] == ['purchase', 'purchase2']
    assert len(plan['supplementary']) == 1
    assert (item, facts, goods) == before


@pytest.mark.parametrize('mutation', ['missing_duplicate', 'foreign_duplicate_member', 'outside_amount',
                                      'unequal_amount', 'missing_revision', 'invalid_revision', 'false_amount_tag'])
def test_shared_amount_needs_the_same_complete_proven_duplicate_group(mutation: str) -> None:
    item, facts, goods = shared_amount_views()
    relation = item['agent_relation']
    if mutation == 'missing_duplicate': relation['duplicate_views'] = []
    elif mutation == 'foreign_duplicate_member': relation['duplicate_views'] = [['purchase', 'generic-invoice']]
    elif mutation == 'outside_amount':
        relation['relation_evidence'][0].update(quote='商品名称：口罩', supports='goods')
        facts[1]['text'] = '合计24.60'
        relation['relation_evidence'][1].update(quote='合计24.60', supports='amount')
    elif mutation == 'unequal_amount':
        facts[2]['total_cents'] += 1
        item['material_match']['recognition']['supports'][1]['amount_cents'] += 1
    elif mutation == 'missing_revision': facts[2].pop('revision')
    elif mutation == 'invalid_revision': facts[2]['revision'] = True
    elif mutation == 'false_amount_tag': relation['relation_evidence'][0]['quote'] = '商品名称：口罩'
    plan = supporting_product_sources(item, facts, goods, CATEGORIES)
    assert not plan['supplementary'] and plan['sources'] == facts


def test_independent_order_cannot_borrow_an_invoice_amount_quote() -> None:
    item, facts, goods = split_food()
    item['agent_relation']['relation_evidence'][0]['supports'] = 'merchant'
    facts[1]['text'] = '合计58.30'
    item['agent_relation']['relation_evidence'][1].update(quote='合计58.30', supports='amount')
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


def test_split_food_uses_concrete_invoice_and_audits_both_complete_auxiliary_views() -> None:
    item, facts, goods = split_food()
    before = deepcopy((item, facts, goods))
    plan = supporting_product_sources(item, facts, goods, CATEGORIES)
    assert [f['id'] for f in plan['sources']] == ['tea']
    assert {p['kind'] for p in plan['supplementary']} == {
        'supplementary_order_summary', 'supplementary_delivery_invoice'}
    assert sum(f['total_cents'] for f in facts if f['role'] == 'invoice') == item['amount_cents'] == 5830
    assert all(p['sha256'] and p['material_id'] and p['quote'] and p['reason'] for p in plan['supplementary'])
    assert (item, facts, goods) == before


@pytest.mark.parametrize('case', ['generic', 'split'])
@pytest.mark.parametrize('mutation', ['no_relation', 'wrong_kind', 'wrong_ref', 'missing_ref', 'wrong_total',
                                      'wrong_voucher_sum', 'wrong_support_amount', 'stale_quote', 'missing_quote',
                                      'bad_sha', 'wrong_goods_sha', 'verified'])
def test_missing_or_stale_relation_never_hides_a_source(case: str, mutation: str) -> None:
    item, facts, goods = generic_pair() if case == 'generic' else split_food()
    recognition = item['material_match']['recognition']
    if mutation == 'no_relation': item.pop('agent_relation')
    elif mutation == 'wrong_kind': item['agent_relation']['relation_kind'] = 'unconfirmed'
    elif mutation == 'wrong_ref': recognition['vouchers'][0]['queue_id'] = 'foreign'
    elif mutation == 'missing_ref': recognition['supports'] = []
    elif mutation == 'wrong_total': item['amount_cents'] += 1
    elif mutation == 'wrong_voucher_sum': recognition['voucher_total_cents'] += 1
    elif mutation == 'wrong_support_amount': recognition['supports'][0]['amount_cents'] += 1
    elif mutation == 'stale_quote': item['agent_relation']['relation_evidence'][0]['quote'] = '其他交易'
    elif mutation == 'missing_quote': item['agent_relation']['relation_evidence'] = []
    elif mutation == 'bad_sha': facts[0]['sha256'] = 'not-checked-bytes'
    elif mutation == 'wrong_goods_sha': goods[0]['sha256'] = 'd' * 64
    elif mutation == 'verified': item['verified'] = True
    plan = supporting_product_sources(item, facts, goods, CATEGORIES)
    assert not plan['supplementary'] and plan['sources'] == facts


@pytest.mark.parametrize('flag,value', [('complete', False), ('issues', ['读取不完整']),
                                      ('goods_issues', ['多商品未读完']), ('source_text_truncated', True),
                                      ('goods_text_truncated', True)])
def test_any_source_incompleteness_prevents_split_auxiliary_admission(flag: str, value: Any) -> None:
    item, facts, goods = split_food()
    facts[0][flag] = value
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('addition', ['\n商品清单\n量杯', '\n商品名称：未知仪器', '\n*食品*蛋糕'])
def test_order_with_a_real_product_area_is_never_demoted_to_a_summary(addition: str) -> None:
    item, facts, goods = split_food()
    facts[0]['source_text'] += addition
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


def test_unlabelled_unknown_order_line_between_shop_and_total_stays_visible() -> None:
    item, facts, goods = split_food()
    facts[0]['source_text'] = facts[0]['source_text'].replace('共6件', '共6件\n未知仪器')
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('addition', ['\n未知仪器', ' 未知仪器', '\n备注：另有量杯', '\n下载次数：1\n未知仪器'])
def test_order_footer_or_price_suffix_does_not_hide_an_unlabelled_good(addition: str) -> None:
    item, facts, goods = split_food()
    facts[0]['source_text'] += addition
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('mutation', ['extra_product', 'different_service', 'tax_mismatch', 'not_native', 'discount'])
def test_delivery_invoice_must_be_sole_explicit_reconciled_service(mutation: str) -> None:
    item, facts, goods = split_food()
    delivery = facts[2]
    if mutation == 'extra_product': delivery['source_text'] += '\n商品名称：安装服务'
    elif mutation == 'different_service': delivery['goods'][0]['raw_name'] = '*生产生活服务*货物运输费'
    elif mutation == 'tax_mismatch': delivery['goods'][0]['tax_cents'] = 13
    elif mutation == 'not_native': delivery['goods'][0]['method'] = 'intake_text'
    elif mutation == 'discount': delivery['goods'][0]['is_discount'] = True
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('addition', ['\n未知仪器', '\n下载次数：1\n未知仪器', '\n备注：另有安装服务'])
def test_delivery_footer_does_not_hide_unknown_product_or_service(addition: str) -> None:
    item, facts, goods = split_food()
    facts[2]['source_text'] += addition
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('name', ['未知仪器', '茶饮料', '鼠标蛋糕', '口罩安装服务'])
def test_generic_invoice_does_not_license_an_unknown_food_or_service_as_materials(name: str) -> None:
    item, facts, goods = generic_pair()
    facts[0]['source_text'] = '商品名称：' + name + '\n实付24.60'
    goods[0].update(name=name, quote='商品名称：' + name)
    item['agent_relation']['relation_evidence'][0]['quote'] = goods[0]['quote']
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


def concrete_split_food() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    order = fact('concrete-order', 'order', 5830,
                 '商品名称：茶饮料\n共6件\n订单已送达\n实付58.30\n商家名称：饮品店\n测试材料：[编号省略]', ['茶饮料'])
    tea = fact('concrete-tea', 'invoice', 5620,
               '电子发票（普通发票）\n发票号码：[编号省略]\n价税合计（小写）￥56.20\n'
               '*服务*茶饮料\n商家名称：饮品开票公司\n测试材料：[编号省略]', ['茶饮料'])
    delivery = fact('concrete-delivery', 'invoice', 210,
                    '电子发票（普通发票）\n发票号码：[编号省略]\n价税合计（小写）￥2.10\n'
                    '*服务*物流网络配送费\n商家名称：配送开票公司\n测试材料：[编号省略]', ['物流网络配送费'])
    for source, quote in [(order, '商品名称：茶饮料'), (tea, '*服务*茶饮料'),
                          (delivery, '*服务*物流网络配送费')]:
        source['goods'][0].update(quote=quote, method='native_text')
    facts = [order, tea, delivery]
    item = item_for(facts)
    for row in item['agent_relation']['relation_evidence']:
        row['supports'] = 'goods'  # Existing relation may cite goods, rather than tagging the local total.
    return item, facts, records([order, tea])


def test_complete_concrete_order_remains_a_product_source_in_a_conserved_delivery_split() -> None:
    item, facts, goods = concrete_split_food()
    item['manual_fields'] = ['reimbursement_person_id']
    item['reimbursement_person_id'] = 'manual-owner'
    before = deepcopy((item, facts, goods))
    plan = supporting_product_sources(item, facts, goods, CATEGORIES)
    assert [source['id'] for source in plan['sources']] == ['concrete-order', 'concrete-tea']
    assert {source['kind'] for source in plan['supplementary']} == {
        'supplementary_delivery_invoice', 'local_concrete_order_amount'}
    amount_proof = next(row for row in plan['supplementary'] if row['kind'] == 'local_concrete_order_amount')
    assert amount_proof['quote'] == '实付58.30' and amount_proof['amount_cents'] == 5830
    from invoice_print_layout.agent_product_rules import inspect_product_rules
    checked = inspect_product_rules(goods, plan['sources'], CATEGORIES)
    assert checked['status'] == 'exact' and checked['coverage_complete']
    assert len(checked['products']) == 2 and checked['category'] == '餐饮'
    assert item['amount_cents'] == 5620 + 210 == 5830 and (item, facts, goods) == before


@pytest.mark.parametrize('addition', ['\n未知仪器', '\n鼠标蛋糕', '\n商品名称：鼠标', '\n备注：还含安装'])
def test_concrete_order_status_or_total_cannot_hide_other_content(addition: str) -> None:
    item, facts, goods = concrete_split_food()
    facts[0]['source_text'] = facts[0]['source_text'].replace('订单已送达', '订单已送达' + addition)
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('addition', ['\n未知仪器', ' 未知仪器', '\n下载次数：1\n未知仪器'])
def test_concrete_order_paid_total_and_metadata_have_no_unchecked_tail(addition: str) -> None:
    item, facts, goods = concrete_split_food()
    facts[0]['source_text'] += addition
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('mutation', ['different_product', 'order_amount', 'invoice_amount', 'delivery_amount',
                                      'stale_goods', 'missing_relation', 'verified', 'missing_revision'])
def test_concrete_split_requires_current_goods_identity_and_amount_conservation(mutation: str) -> None:
    item, facts, goods = concrete_split_food()
    if mutation == 'different_product':
        facts[1]['source_text'] = facts[1]['source_text'].replace('茶饮料', '矿泉水')
        facts[1]['goods'][0].update(name='矿泉水', quote='*服务*矿泉水')
        goods[1].update(name='矿泉水', quote='*服务*矿泉水')
    elif mutation == 'order_amount': facts[0]['total_cents'] += 1
    elif mutation == 'invoice_amount': facts[1]['total_cents'] += 1
    elif mutation == 'delivery_amount': facts[2]['total_cents'] += 1
    elif mutation == 'stale_goods': goods[0]['sha256'] = 'f' * 64
    elif mutation == 'missing_relation': item.pop('agent_relation')
    elif mutation == 'verified': item['verified'] = True
    elif mutation == 'missing_revision': facts[2].pop('revision')
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']


@pytest.mark.parametrize('mutation', ['different_service', 'extra_label', 'unlabelled_item', 'tail_item',
                                      'truncated', 'source_issue', 'incomplete', 'repeated_tax'])
def test_single_line_delivery_bill_must_be_the_only_complete_service(mutation: str) -> None:
    item, facts, goods = concrete_split_food()
    delivery = facts[2]
    if mutation == 'different_service':
        delivery['source_text'] = delivery['source_text'].replace('物流网络配送费', '货物运输费')
        delivery['goods'][0].update(name='货物运输费', quote='*服务*货物运输费')
    elif mutation == 'extra_label': delivery['source_text'] += '\n商品名称：安装服务'
    elif mutation == 'unlabelled_item': delivery['source_text'] = delivery['source_text'].replace('*服务*物流网络配送费', '*服务*物流网络配送费\n未知仪器')
    elif mutation == 'tail_item': delivery['source_text'] += '\n鼠标'
    elif mutation == 'truncated': delivery['source_text_truncated'] = True
    elif mutation == 'source_issue': delivery['issues'] = ['原件未完整读取']
    elif mutation == 'incomplete': delivery['complete'] = False
    elif mutation == 'repeated_tax': delivery['source_text'] += '\n*服务*物流网络配送费'
    assert not supporting_product_sources(item, facts, goods, CATEGORIES)['supplementary']
