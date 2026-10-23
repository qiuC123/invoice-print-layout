"""Source-shaped regressions for table wraps, shopping rows and delivery scenes.

These are synthetic texts/geometry receipts, never private original documents.
No OCR, provider, business API or real ledger is used.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_content import CONTENT_CATEGORIES, goods_from_text
from invoice_print_layout.agent_product_rules import inspect_product_rules
from invoice_print_layout.agent_product_support import supporting_product_sources
from tests.test_agent_product_support import concrete_split_food, split_food
from tests.test_invoice_agent import environment
from tests.test_invoice_agent import material, start
from tests.test_invoice_agent_content import draft


def native_service(head: str, canonical: str, *, carrier: str = '') -> tuple[list[dict[str, Any]], dict[str, Any]]:
    name = head + '费'
    quote = '*生产生活服务*' + head + ' 无 次 1 40.5660377358 40.57 6% 2.43\n费'
    row = {'name': name, 'raw_name': '*生产生活服务*' + name, 'quote': quote,
           'method': 'native_geometry', 'quantity': '1', 'unit': '次', 'amount_cents': 4057,
           'tax_cents': 243, 'bbox': [10, 100, 580, 125], 'page': 1}
    source = {'id': 'invoice', 'sha256': 'a' * 64, 'role': 'invoice', 'complete': True,
              'total_cents': 4300, 'revision': 1, 'issues': [], 'goods_issues': [],
              'goods_complete': True, 'goods_text_truncated': False, 'source_text_truncated': False,
              'source_text': '电子发票（普通发票）\n销售方：' + carrier + '\n项目名称\n规格型号\n'
                             '*生产生活服务*' + head + '\n无\n次\n1\n40.5660377358\n40.57\n6%\n2.43\n费\n价税合计43.00',
              'goods': [row]}
    return [{**deepcopy(row), 'name': canonical, 'raw_name': name,
             'material_id': 'invoice', 'sha256': 'a' * 64}], source


@pytest.mark.parametrize('head,canonical,category', [
    ('收派服务', '顺丰快递费', '顺丰'), ('打印制作', '打印制作费', '其他'),
])
def test_native_name_column_wrap_covers_intervening_numeric_columns(
        head: str, canonical: str, category: str) -> None:
    goods, source = native_service(head, canonical)
    before = deepcopy((goods, source))
    result = inspect_product_rules(goods, [source], {head + '费': category})
    assert result['status'] == 'exact' and result['category'] == category
    assert result['coverage_complete'] and result['goods_text'] == head + '费'
    assert (goods, source) == before


@pytest.mark.parametrize('mutation', ['hidden_item', 'hidden_tax_row', 'changed_raw', 'changed_quote',
                                    'missing_token', 'extra_qualifier', 'incomplete', 'source_error', 'extra_page'])
def test_native_wrap_does_not_license_missing_products_or_service_qualifiers(mutation: str) -> None:
    goods, source = native_service('打印制作', '打印制作费')
    if mutation == 'hidden_item':
        source['source_text'] = source['source_text'].replace('\n价税', '\n未知设备\n价税')
    elif mutation == 'hidden_tax_row':
        source['source_text'] += '\n*硬件*鼠标蛋糕 1 20.00 1% 0.20'
    elif mutation == 'changed_raw':
        source['goods'][0]['raw_name'] = '*生产生活服务*打印制作'
    elif mutation == 'changed_quote':
        source['goods'][0]['quote'] += '\n安装服务'
    elif mutation == 'missing_token':
        source['source_text'] = source['source_text'].replace('40.5660377358', '40.50')
    elif mutation == 'extra_qualifier':
        source['source_text'] = source['source_text'].replace('\n费\n价税', '\n安装服务费\n价税')
        source['goods'][0].update(name='打印制作安装服务费', raw_name='*生产生活服务*打印制作安装服务费',
                                  quote=source['goods'][0]['quote'].replace('\n费', '\n安装服务费'))
    elif mutation == 'incomplete':
        source['complete'] = False
    elif mutation == 'source_error':
        source['issues'] = ['原件未完整读取']
    else:
        source['source_text'] += '\n项目名称\n*设备*安装租赁服务'
    result = inspect_product_rules(goods, [source], {'打印制作费': '其他'})
    assert result['status'] != 'exact'
    if mutation != 'extra_qualifier':
        assert result['status'] == 'needs_input'


def courier_detail() -> dict[str, Any]:
    text = ('1-1\n顺丰电子发票 — 运单明细\n#\n运单号码\n寄件时间\n寄方\n寄件地\n收方\n收件地\n运单金额(元)\n'
            '1\n[编号省略]\n2026/02/25\n甲方\n苏州市\n乙方\n上海市\n43.00\n'
            '开票时间\n发票号码\n2026/09/18 [编号省略]\n公司名称\n示例广告有限公司\n发票总金额(元)\n43.00')
    return {'id': 'detail', 'sha256': 'b' * 64, 'role': 'detail', 'complete': True, 'total_cents': 4300,
            'issues': [], 'goods_issues': [], 'goods_complete': True, 'source_text_truncated': False,
            'goods_text_truncated': False, 'source_text': text,
            'goods': [{'name': '顺丰快递费', 'quote': '顺丰电子发票 — 运单明细', 'method': 'native_text'}]}


def test_each_source_is_checked_when_service_invoice_has_a_dedicated_detail() -> None:
    goods, invoice = native_service('收派服务', '顺丰快递费', carrier='示例顺丰速运有限公司')
    detail = courier_detail()
    goods.append({**detail['goods'][0], 'material_id': 'detail', 'sha256': detail['sha256']})
    result = inspect_product_rules(goods, [invoice, detail], {'顺丰快递费': '顺丰', '收派服务费': '顺丰'})
    assert result['status'] == 'exact' and result['category'] == '顺丰'
    assert len(result['products']) == 2
    invoice['source_text'] += '\n商品名称：鼠标蛋糕'
    assert inspect_product_rules(goods, [invoice, detail], CONTENT_CATEGORIES)['status'] == 'needs_input'


@pytest.mark.parametrize('mutation', ['extra_text', 'extra_product', 'other_carrier', 'line_amount',
                                    'invoice_total', 'truncated', 'extra_record', 'hidden_service'])
def test_courier_detail_requires_all_rows_and_conserved_total(mutation: str) -> None:
    detail = courier_detail()
    if mutation == 'extra_text': detail['source_text'] += '\n未知设备'
    elif mutation == 'extra_product': detail['source_text'] += '\n商品名称：鼠标蛋糕'
    elif mutation == 'other_carrier': detail['source_text'] = detail['source_text'].replace('顺丰', '中通')
    elif mutation == 'line_amount': detail['source_text'] = detail['source_text'].replace('上海市\n43.00', '上海市\n42.00')
    elif mutation == 'invoice_total': detail['total_cents'] = 4500
    elif mutation == 'truncated': detail['source_text_truncated'] = True
    elif mutation == 'extra_record': detail['source_text'] = detail['source_text'].replace('\n开票时间', '\n2\n未知设备\n开票时间')
    else: detail['source_text'] = detail['source_text'].replace('发票总金额(元)', '安装服务\n发票总金额(元)')
    record = {**detail['goods'][0], 'material_id': 'detail', 'sha256': detail['sha256']}
    assert inspect_product_rules([record], [detail], CONTENT_CATEGORIES)['status'] == 'needs_input'


def rainwear_text() -> str:
    return ('闪购示例便利店\n【小巧便携】一次性成人雨衣便 实付￥23.7\n雨衣\n'
            '携式加厚雨披1个雨季出行透明长 ￥32.3\n7天无理由退换\n加入购物车\nx31\n打包费￥1\n实付款24.7')


def test_rainwear_split_word_and_thumbnail_are_one_product_with_explicit_count() -> None:
    goods = goods_from_text(rainwear_text(), {'shop_name': '示例便利店'})
    assert len(goods) == 1 and goods[0]['name'] == '雨衣'
    assert goods[0]['quantity'] == '31' and goods[0]['quantity_quote'] == 'x31' and 'unit' not in goods[0]
    assert '雨衣便' in goods[0]['quote'] and '携式加厚雨披' in goods[0]['quote']
    assert '￥23.7' in goods[0]['quote'] and '￥32.3' in goods[0]['quote']


def test_independently_priced_rainwear_and_other_goods_are_not_joined() -> None:
    goods = goods_from_text('商品明细\n雨衣 ￥20\n雨披 ￥30\nx2\n价格明细', {})
    assert [g['name'] for g in goods] == ['雨衣', '雨披']
    assert 'quantity' not in goods[0] and goods[1]['quantity'] == '2'
    goods = goods_from_text('商品明细\n一次性成人雨衣实付￥20\n鼠标蛋糕￥30\n价格明细', {})
    assert len(goods) == 2


def test_a_different_intervening_product_is_not_a_rainwear_thumbnail() -> None:
    text = ('商品明细\n一次性成人雨衣便 实付￥20\n鼠标蛋糕\n'
            '携式加厚雨披 ￥30\n价格明细')
    goods = goods_from_text(text, {})
    assert any('携式加厚雨披' in good['quote'] and '鼠标蛋糕' not in good['quote'] for good in goods)
    assert any('鼠标蛋糕' in good['quote'] for good in goods)


@pytest.mark.parametrize('make', [split_food, concrete_split_food])
def test_delivery_scene_is_only_exposed_after_the_full_source_bound_split_proof(make: Any) -> None:
    item, facts, goods = make()
    before = deepcopy((item, facts, goods))
    plan = supporting_product_sources(item, facts, goods, CONTENT_CATEGORIES)
    assert plan['context_category'] == '外卖'
    assert (item, facts, goods) == before
    facts[-1]['source_text_truncated'] = True
    assert 'context_category' not in supporting_product_sources(item, facts, goods, CONTENT_CATEGORIES)


def test_delivery_category_survives_the_drinks_template_keyword(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    job, ident = draft(environment)
    tools.execute('extract_goods', {'item_id': ident}, job)
    raw, item, _, binding = tools.content._load(job, ident)
    state = item['agent_content'];state['goods'][0]['name'] = '茶饮料'
    tools.content._save(job, raw, item, state, binding)
    checked = {'status': 'exact', 'category': '餐饮', 'reason': '完整食品商品', 'goods_text': '茶饮料',
               'coverage_complete': True, 'forbidden_categories': ['材料采购'], 'products': [],
               'context_category': '外卖', 'context_reason': '完整订单和独立配送票证明配送场景'}
    monkeypatch.setattr(tools.content, '_inspect_products', lambda *_: deepcopy(checked))
    result = tools.execute('classify_expense', {'item_id': ident}, job)
    assert result['decision']['category'] == '外卖'
    result = tools.execute('name_expense', {'item_id': ident}, job)
    after = tools.store.get(ident)
    assert result['category'] == after['category'] == '外卖'
    assert after['report_section'] == 'takeout' and after['project_name'] == '员工统一购餐费(附清单)'
    assert after['title'] == '茶饮料' and not after['verified'] and after['stage'] == 'draft'


def test_source_qualified_wrapped_carrier_alias_has_no_cross_source_leak(environment: Any, monkeypatch: Any) -> None:
    from invoice_print_layout import agent_evidence
    _, tools, _ = environment
    goods, source = native_service('收派服务', '顺丰快递费', carrier='示例顺丰速运有限公司')
    monkeypatch.setattr(agent_evidence, 'material_facts', lambda *_args, **_kwargs: deepcopy(source))
    checked = tools.content._inspect_products({'goods': goods}, [{'id': 'invoice'}])
    assert checked['status'] == 'exact' and checked['category'] == '顺丰'
    source['source_text'] = source['source_text'].replace('顺丰', '中通')
    checked = tools.content._inspect_products({'goods': goods}, [{'id': 'invoice'}])
    assert checked['status'] != 'exact' or checked['category'] != '顺丰'


def confirmed_entries(env: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from invoice_print_layout.agent_confirmed_facts import restore
    jobs, tools, project = env
    entries = [material(tools, project, 'receipt', '17', 'human-content-receipt', text=
                        '收据\n商家名称：合成工具商店\n品名\n打印+年\n实付：17.00'),
               material(tools, project, 'payment', '17', 'human-content-payment')]
    fact = restore(jobs, {'project_id': project, 'material_ids': [e['id'] for e in entries],
                         'claim': 'material_content', 'statement': '用户明确确认本组是打印和胶带，17元，类别材料采购',
                         'provenance': {'source': 'recorded_user_confirmation', 'reference': 'synthetic-user-receipt',
                                        'sha256': 'c' * 64},
                         'goods': ['打印', '胶带'], 'category': '材料采购', 'amount_cents': 1700,
                         'content_material_id': entries[0]['id']})
    import json
    from hashlib import sha256
    canonical = {key: value for key, value in fact.items() if key not in {'fact_id', 'recorded_at'}}
    assert fact['fact_id'] == 'source-fact:' + sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    entries[0]['confirmed_content'] = fact
    with tools.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                   (json.dumps(entries[0], ensure_ascii=False), entries[0]['id']))
    return entries, fact


@pytest.mark.parametrize('old_preference_conflict', [False, True])
def test_exact_human_content_is_separate_from_native_ocr_and_needs_no_model(
        environment: Any, monkeypatch: Any, old_preference_conflict: bool) -> None:
    import time
    from invoice_print_layout import agent_goods_ai, expense_classifier
    jobs, tools, _ = environment
    entries, fact = confirmed_entries(environment)
    originals = {e['path']: Path(e['path']).read_bytes() for e in entries}
    monkeypatch.setattr(agent_goods_ai, 'supplement_goods', lambda *_: pytest.fail('human fact does not ask GLM'))
    monkeypatch.setattr(expense_classifier, 'ask_jev', lambda *_: pytest.fail('human category does not ask Jev'))
    if old_preference_conflict:
        from invoice_print_layout.expense_preferences import Preferences
        monkeypatch.setattr(Preferences, 'apply', lambda *_args, **_kwargs:
                            {'changes': {}, 'conflicts': ['旧默认分类冲突']})
    job = start(environment, entries)
    job = jobs.update(job['id'], state='running', run_started=time.time())
    proposal = tools._scan(job)['proposals'][0]
    validation = tools.execute('validate_group', {'voucher_keys': proposal['voucher_keys'],
                                                'payment_ids': proposal['payment_ids']}, job)
    job = jobs.update(job['id'], last_results=[{'tool': 'validate_group', 'result': validation}])
    ident = tools.execute('create_draft', {'proposal_id': proposal['id']}, job)['item_id']
    assert tools.execute('extract_goods', {'item_id': ident}, job)['content_status'] == 'extracted'
    classified = tools.execute('classify_expense', {'item_id': ident}, job)
    assert classified['decision']['source'] == 'human'
    assert classified['decision']['category'] == '材料采购'
    tools.execute('name_expense', {'item_id': ident}, job)
    after = tools.store.get(ident)
    assert after['title'] == '打印、胶带采购' and after['amount_cents'] == 1700
    assert not after['verified'] and after['stage'] == 'draft'
    content = after['agent_content']
    assert [g['name'] for g in content['goods']] == ['打印', '胶带']
    assert all(g['method'] == 'user_confirmation' and g['quote'] == fact['statement'] and
               g['fact_id'] == fact['fact_id'] for g in content['goods'])
    assert all(g['method'] != 'user_confirmation' for g in content['native_goods'])
    assert content['product_check']['coverage_basis'] == 'exact_source_set_user_confirmation'
    assert jobs.get(job['id'])['models'] == 0
    assert all(Path(path).read_bytes() == payload for path, payload in originals.items())


@pytest.mark.parametrize('mutation', ['extra_source', 'wrong_amount', 'automatic_actor', 'same_expense_only',
                                    'wrong_group', 'changed_file', 'missing_statement'])
def test_old_defaults_or_a_changed_group_cannot_supply_confirmed_products(
        environment: Any, monkeypatch: Any, mutation: str) -> None:
    from invoice_print_layout import agent_evidence
    _, tools, project = environment
    entries, _ = confirmed_entries(environment)
    source = {'id': entries[0]['id'], 'sha256': entries[0]['digest'], 'role': 'receipt',
              'complete': False, 'source_text': '品名\n打印+年', 'goods': [], 'goods_complete': False}
    monkeypatch.setattr(agent_evidence, 'material_facts', lambda *_args, **_kwargs: deepcopy(source))
    item = {'amount_cents': 1700, 'project_id': project}
    if mutation == 'extra_source': entries.append(material(tools, project, 'receipt', '3', 'new-receipt'))
    elif mutation == 'wrong_amount': item['amount_cents'] = 1800
    elif mutation == 'automatic_actor': entries[0]['confirmed_content']['actor'] = 'model'
    elif mutation == 'same_expense_only': entries[0]['confirmed_content']['claim'] = 'same_expense'
    elif mutation == 'wrong_group': entries[0]['confirmed_content']['material_digests'] = ['d' * 64]
    elif mutation == 'changed_file': Path(entries[0]['path']).write_bytes(b'changed original')
    else: entries[0]['confirmed_content']['statement'] = ''
    checked = tools.content._inspect_products({'goods': [], 'confirmed_content': {'prior': True}}, entries, item)
    assert checked['status'] == 'needs_input' and checked['coverage_complete'] is False


@pytest.mark.parametrize('tail', ['商品名称：鼠标蛋糕', '商品名称：胶带安装服务', '商品名称：未知仪器'])
def test_confirmed_content_does_not_hide_an_explicit_additional_source_product(
        environment: Any, monkeypatch: Any, tail: str) -> None:
    from invoice_print_layout import agent_evidence
    _, tools, project = environment
    entries, _ = confirmed_entries(environment)
    def facts(entry: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {'id': entry['id'], 'sha256': entry['digest'], 'role': entry['role'],
                'source_text': '品名\n打印+年\n' + tail if entry['role'] != 'payment' else '支付成功',
                'goods': [], 'issues': []}
    monkeypatch.setattr(agent_evidence, 'material_facts', facts)
    checked = tools.content._inspect_products({'goods': []}, entries,
                                            {'amount_cents': 1700, 'project_id': project})
    assert checked['status'] == 'needs_input' and not checked['coverage_complete']


@pytest.mark.parametrize('problem', ['source_text_truncated', 'goods_text_truncated', 'unread_extra_page'])
def test_human_content_does_not_replace_an_unread_or_truncated_original(
        environment: Any, monkeypatch: Any, problem: str) -> None:
    from invoice_print_layout import agent_evidence
    _, tools, project = environment
    entries, _ = confirmed_entries(environment)
    def facts(entry: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        result = {'id': entry['id'], 'sha256': entry['digest'], 'role': entry['role'],
                  'source_text': '品名\n打印+年', 'goods': [], 'issues': []}
        if problem == 'unread_extra_page':
            result['issues'] = ['原件PDF未能完整读取：额外页面']
        else:
            result[problem] = True
        return result
    monkeypatch.setattr(agent_evidence, 'material_facts', facts)
    checked = tools.content._inspect_products({'goods': []}, entries,
                                            {'amount_cents': 1700, 'project_id': project})
    assert checked['status'] == 'needs_input' and not checked['coverage_complete']


@pytest.mark.parametrize('date,total', [
    ('开票日期：2030年01月02日\n', '价税合计\n（小写）¥56.20'),
    ('开票日期：2030年01月02日\n', '价税合计（小写）¥56.20'),
    ('', '价税合计\n（小写）¥56.20'),
])
def test_native_text_invoice_metadata_keeps_exact_original_total_quote(date: str, total: str) -> None:
    from invoice_print_layout.agent_product_support import _complete_simple_source
    quote = '*服务*茶饮料'
    source = {'role': 'invoice', 'complete': True, 'goods_complete': True, 'goods_issues': [],
              'issues': [], 'source_text_truncated': False, 'goods_text_truncated': False,
              'total_cents': 5620, 'source_text': '电子发票（普通发票）\n' + date + total + '\n' + quote}
    assert _complete_simple_source(source, quote) == total


@pytest.mark.parametrize('tail', [
    '\n未知仪器', '\n*服务*鼠标蛋糕', '\n商品名称：胶带安装服务',
    '\n开票日期：2030年01月02日附加商品',
    '\n价税合计\n（小写）¥58.30',
])
def test_native_text_invoice_metadata_never_consumes_unknown_tail(tail: str) -> None:
    from invoice_print_layout.agent_product_support import _complete_simple_source
    quote = '*服务*茶饮料'
    source = {'role': 'invoice', 'complete': True, 'goods_complete': True, 'goods_issues': [],
              'issues': [], 'source_text_truncated': False, 'goods_text_truncated': False,
              'total_cents': 5620,
              'source_text': '开票日期：2030年01月02日\n价税合计\n（小写）¥56.20\n' + quote + tail}
    assert _complete_simple_source(source, quote) == ''


@pytest.mark.parametrize('total', [
    '价税合计\n未知商品\n（小写）¥56.20', '价税合计\n（小写）¥56.20未知商品',
    '价税合计\n（小写）¥58.30', '价税合计\n（小写）¥0.00', '价税合计\n',
])
def test_native_text_invoice_split_total_requires_adjacent_matching_amount(total: str) -> None:
    from invoice_print_layout.agent_product_support import _complete_simple_source
    quote = '*服务*茶饮料'
    source = {'role': 'invoice', 'complete': True, 'goods_complete': True, 'goods_issues': [],
              'issues': [], 'source_text_truncated': False, 'goods_text_truncated': False,
              'total_cents': 5620, 'source_text': total + '\n' + quote}
    assert _complete_simple_source(source, quote) == ''
