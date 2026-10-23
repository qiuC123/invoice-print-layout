"""Goods completeness, grounded text completion and durable attempt boundaries."""
from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any

import pytest
import pymupdf

from invoice_print_layout import expense_classifier as classifier
from invoice_print_layout.agent_content import goods_from_text
from invoice_print_layout.agent_goods_ai import _covered, _sources, _validate, supplement_goods
from invoice_print_layout.agent_jobs import AgentLoop, ModelBudgetExceeded
from invoice_print_layout.glm_invoice_ai import GlmAIError, GlmInvoiceAI
from tests.test_invoice_agent import environment, material, proof, start
from tests.test_invoice_agent_content import draft, pair


@pytest.mark.parametrize('content,expected', [
    ('鼠标、亚克力导视牌', ['鼠标', '亚克力导视牌']),
    ('亚克力导视牌、鼠标', ['亚克力导视牌', '鼠标']),
    ('胶带；定制磁吸支架；纸杯', ['胶带', '定制磁吸支架', '纸杯']),
    ('鼠标，午餐', ['鼠标', '午餐']),
    ('鼠标+亚克力导视牌', ['鼠标', '亚克力导视牌']),
    ('螺丝,亚克力导视牌,盒饭', ['螺丝', '亚克力导视牌', '盒饭']),
    ('鼠标及亚克力导视牌', ['鼠标', '亚克力导视牌']),
    ('鼠标和亚克力导视牌', ['鼠标', '亚克力导视牌']),
    ('鼠标与亚克力导视牌', ['鼠标', '亚克力导视牌']),
    ('鼠标以及亚克力导视牌', ['鼠标', '亚克力导视牌']),
    ('鼠标以及键盘和亚克力导视牌', ['鼠标', '键盘', '亚克力导视牌']),
    ('有线鼠标及笔记本', ['鼠标', '笔记本']),
])
def test_familiar_noun_never_hides_an_explicit_other_product(content: str, expected: list[str]) -> None:
    goods = goods_from_text('商品名称：' + content, {})
    assert [item['name'] for item in goods] == expected
    assert all(item['name'] in item['quote'] for item in goods)


@pytest.mark.parametrize('separator', ['及', '和', '与', '以及'])
def test_coordinated_goods_cannot_be_covered_by_only_the_familiar_product(separator: str) -> None:
    text = '鼠标' + separator + '亚克力导视牌'
    partial = [{'name': '鼠标', 'quote': text}]
    assert not _covered(text, partial)
    complete = goods_from_text('商品清单\n' + text + '\n合计：17', {})
    assert [row['name'] for row in complete] == ['鼠标', '亚克力导视牌']
    assert _covered(text, complete)
    with pytest.raises(ValueError, match='遗漏多商品'):
        _validate({'goods': [{'name': '鼠标', 'source_ids': [0], 'quote': text}]},
                  [{'id': 0, 'text': text}])
    assert _validate({'goods': [{'name': text, 'source_ids': [0], 'quote': text}]},
                     [{'id': 0, 'text': text}])[0]['name'] == text


def test_conjunction_inside_a_name_or_device_compatibility_does_not_invent_products() -> None:
    assert [row['name'] for row in goods_from_text('商品名称：和田玉摆件', {})] == ['和田玉摆件']
    compatible = '适用于笔记本电脑和台式电脑的有线鼠标'
    assert [row['name'] for row in goods_from_text('商品名称：' + compatible, {})] == ['鼠标']


@pytest.mark.parametrize('price', ['金额：17.00', '商品金额17.00', '价格：￥17.00元',
                                  '单价:17.00', '实付金额17.00', '总价17.00', '金额',
                                  '17.00元', 'CNY17.00', 'RMB17.00', '¥17.00'])
def test_price_metadata_cannot_be_a_labelled_or_incoming_product(price: str) -> None:
    assert goods_from_text('商品名称：' + price, {'goods': price}) == []
    assert goods_from_text('收据\n' + price, {'goods': price}) == []
    assert _sources({'source_text': '项目明细\n定制收纳盒\n' + price + '\n合计17'}) == [
        {'id': 0, 'text': '定制收纳盒'}]


@pytest.mark.parametrize('name', ['3M反光膜', '17号展示牌', 'A4文件夹', '价格标签打印纸'])
def test_numbered_product_names_are_not_price_metadata(name: str) -> None:
    assert [item['name'] for item in goods_from_text('商品名称：' + name, {'goods': name})] == [name]


@pytest.mark.parametrize('fragment', ['透明亚克力', '304不锈钢', '加厚', '黑色磨砂塑料'])
def test_bare_material_or_finish_fragments_are_not_independent_model_products(fragment: str) -> None:
    sources = [{'id': 0, 'text': fragment}, {'id': 1, 'text': '保温杯'}, {'id': 2, 'text': '加厚帆布袋'}]
    fragmented = {'goods': [{'name': row['text'], 'source_ids': [row['id']], 'quote': row['text']}
                            for row in sources]}
    with pytest.raises(ValueError, match='跨行修饰片段'):
        _validate(fragmented, sources)
    grouped = {'goods': [{'name': fragment + '保温杯', 'source_ids': [0, 1], 'quote': fragment + '\n保温杯'},
                         {'name': '加厚帆布袋', 'source_ids': [2], 'quote': '加厚帆布袋'}]}
    assert [row['name'] for row in _validate(grouped, sources)] == [fragment + '保温杯', '加厚帆布袋']


@pytest.mark.parametrize('quote,other', [('透明亚克力', False), ('透明亚克力 20元', True),
                                       ('透明亚克力 2件', True), ('透明亚克力 2盒', True),
                                       ('透明亚克力 2平方米', True)])
def test_independently_supported_raw_material_is_not_generally_forbidden(quote: str, other: bool) -> None:
    sources = [{'id': 0, 'text': quote}]
    goods = [{'name': '透明亚克力', 'source_ids': [0], 'quote': quote}]
    if other:
        sources.append({'id': 1, 'text': '保温杯'})
        goods.append({'name': '保温杯', 'source_ids': [1], 'quote': '保温杯'})
    assert _validate({'goods': goods}, sources)[0]['name'] == '透明亚克力'


def test_natural_local_amount_field_is_filtered_before_wrapped_goods_completion(
        environment: Any, monkeypatch: Any) -> None:
    from invoice_print_layout.agent_evidence import material_facts
    jobs, tools, project = environment
    text = ('收据\n商家名称：合成工具商店\n发票信息：本单未开票\n项目明细\n'
            '不锈钢\n保温杯\n加厚帆布袋\n合计：17.00\n商品金额：17.00\n实付：17.00\n'
            '商家订单号：SYNTHETICWRAPPEDGOODS20261021')
    purchase = tools.intake.receive('synthetic-natural-receipt.pdf', proof(text), project_id=project)
    purchase = tools.intake.analyze_local(purchase['id'])
    assert purchase['fields']['goods'] == '金额：17.00'
    facts = material_facts(purchase)
    assert facts['goods'] == []
    assert _sources(facts) == [{'id': 0, 'text': '不锈钢'}, {'id': 1, 'text': '保温杯'},
                               {'id': 2, 'text': '加厚帆布袋'}]
    payment = material(tools, project, 'payment', '17', 'natural-wrapped-payment')
    enable(monkeypatch)
    requests = []
    def request(_: Any, rules: str, value: str, **kwargs: Any) -> dict[str, Any]:
        requests.append(json.loads(value))
        return {'goods': [{'name': '不锈钢保温杯', 'source_ids': [0, 1], 'quote': '不锈钢\n保温杯'},
                          {'name': '加厚帆布袋', 'source_ids': [2], 'quote': '加厚帆布袋'}]}
    monkeypatch.setattr(GlmInvoiceAI, '_request', request)
    monkeypatch.setattr(classifier, 'api_key', lambda: 'synthetic-key')
    monkeypatch.setattr(classifier, 'ask_jev', lambda evidence: {
        'choice': 'materials', 'confidence': .99, 'model': classifier.MODEL})
    done = AgentLoop(jobs).run(start(environment, [purchase, payment], use_model=True)['id'], tools)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 1700
    item = tools.store.get(done['item_ids'][0])
    assert [row['name'] for row in item['agent_content']['goods']] == ['不锈钢保温杯', '加厚帆布袋']
    assert item['title'] == '不锈钢保温杯、加厚帆布袋采购'
    assert len(requests) == 1 and not item['verified'] and len(item['attachments']) == 2


def test_service_normalization_cannot_hide_a_coordinated_physical_product(environment: Any) -> None:
    jobs, tools, _ = environment
    done = AgentLoop(jobs).run(start(environment, pair(environment, '打印制作服务及鼠标'),
                                     use_model=False)['id'], tools)
    assert done['state'] == 'needs_input' and done['preview'] is None and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert [row['name'] for row in item['agent_content']['goods']] == ['打印制作费', '鼠标']
    assert not item['verified']


def test_explicit_list_is_local_and_outranks_an_earlier_shop_or_invoice_block() -> None:
    text = ('收据\n商家名称：合成展示材料商店a\n发票信息\n商品清单\n'
            '磁吸双面导向立牌\n2件\n合计：48.00\n商品金额：48.00\n实付：48.00')
    goods = goods_from_text(text, {'merchant': '合成展示材料商店a'})
    assert [row['name'] for row in goods] == ['磁吸双面导向立牌']
    assert goods[0]['quote'] == '磁吸双面导向立牌\n2件'
    assert goods[0]['quantity'] == '2' and goods[0]['unit'] == '件'


def test_discounted_familiar_product_cannot_absorb_a_separately_priced_unknown_product() -> None:
    goods = goods_from_text('商品清单\n有线鼠标 实付￥17\n亚克力导视牌 ￥20\n合计：37', {})
    assert [row['name'] for row in goods] == ['鼠标', '亚克力导视牌']
    assert '亚克力' not in goods[0]['quote']


def facts() -> dict[str, Any]:
    return {'source_text': '收据\n商品清单\n透明亚克力导视牌\n定制磁吸支架\n实付：17.00',
            'parser_version': 'synthetic-goods-parser', 'goods': [], 'goods_issues': []}


def answer() -> dict[str, Any]:
    return {'goods': [{'name': '透明亚克力导视牌', 'source_ids': [0], 'quote': '透明亚克力导视牌'},
                      {'name': '定制磁吸支架', 'source_ids': [1], 'quote': '定制磁吸支架'}]}


@pytest.mark.parametrize('raw', [
    {'goods': [{'name': '胶带', 'source_ids': [0], 'quote': '透明亚克力导视牌'},
               {'name': '定制磁吸支架', 'source_ids': [1], 'quote': '定制磁吸支架'}]},
    {'goods': [{'name': '透明亚克力导视牌', 'source_ids': [0], 'quote': '错误引用'},
               {'name': '定制磁吸支架', 'source_ids': [1], 'quote': '定制磁吸支架'}]},
    {'goods': [{'name': '透明亚克力导视牌', 'source_ids': [0], 'quote': '透明亚克力导视牌'}]},
    {'goods': [{'name': '透明亚克力导视牌', 'source_ids': [0, 0], 'quote': '透明亚克力导视牌'}]},
    {'goods': [{'name': '透明亚克力导视牌', 'source_ids': [True], 'quote': '透明亚克力导视牌'}]},
    {'goods': [{'name': '透明亚克力导视牌', 'source_ids': [0], 'quote': '透明亚克力导视牌', 'category': '材料采购'}]},
    {'goods': [], 'amount': '17'}, {'goods': []},
])
def test_missing_hallucinated_or_unbound_model_content_is_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _validate(raw, _sources(facts()))


@pytest.mark.parametrize('indices', [[0, 1_000_000_000], [-1, 0], [1, 0], [0, 2], [0] * 21])
def test_model_source_indices_are_bounded_before_contiguous_range_validation(indices: list[int]) -> None:
    started = time.perf_counter()
    with pytest.raises(ValueError, match='引用无效'):
        _validate({'goods': [{'name': '鼠标', 'source_ids': indices, 'quote': '鼠标'}]},
                  [{'id': 0, 'text': '鼠标'}, {'id': 1, 'text': '亚克力导视牌'}])
    assert time.perf_counter() - started < .5


@pytest.mark.parametrize('rows', [
    ['鼠标'] * 20 + ['亚克力导视牌'],
    ['鼠标'] + ['亚克力导视牌' * 41],
    ['鼠标'] + ['亚克力导视牌' * 38] * 16,
])
def test_bounded_product_region_rejection_is_not_mistaken_for_no_product_region(rows: list[str]) -> None:
    text = '商品清单\n' + '\n'.join(rows) + '\n合计：17.00'
    assert _sources({'source_text': text}) is None
    goods = [{'name': '鼠标', 'quote': text}]
    result = supplement_goods(None, {'use_model': False}, {}, {'source_text': text}, goods)  # type: ignore[arg-type]
    assert result['required'] and not result['complete'] and result['goods'] == goods
    assert '上限' in result['reason']


@pytest.mark.parametrize('product', ['日用百货', '饮用水', '鼠标及亚克力导视牌'])
def test_native_invoice_goods_region_uses_positioned_facts_instead_of_pdf_stream_order(
        tmp_path: Path, product: str) -> None:
    from invoice_print_layout.agent_evidence import material_facts
    path = tmp_path / 'synthetic-header-first.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=650, height=450)
        def draw(x: float, y: float, text: str) -> None:
            page.insert_text((x, y), text, fontname='china-s', fontsize=8)
        draw(20, 30, '电子发票（普通发票）')
        draw(20, 50, '发票号码：12345678901234567890')
        for x, label in ((20, '项目名称'), (200, '单位'), (285, '数量'), (425, '金额'), (565, '税额')):
            draw(x, 205, label)
        # Real invoice producers may emit the footer text before item values.
        draw(20, 310, '合')
        draw(70, 310, '计')
        draw(20, 330, '价税合计（大写）')
        draw(20, 350, '备注')
        draw(20, 380, '下载次数：1')
        draw(20, 225, '*日用杂品*' + product)
        draw(425, 225, '17.00')
        draw(565, 225, '0.00')
        document.save(path)
    entry = {'id': 'synthetic', 'path': str(path), 'role': 'invoice', 'revision': 1,
             'digest': hashlib.sha256(path.read_bytes()).hexdigest(), 'fields': {'amount': '17'}}
    facts = material_facts(entry)
    assert facts['source_text'].index('合\n计') < facts['source_text'].index('*日用杂品*')
    assert facts['goods'][0]['method'] == 'native_geometry'
    assert facts['goods'][0]['name'] == product
    expected = [] if product == '日用百货' else [{'id': 0, 'text': product}]
    assert _sources(facts) == expected
    concrete = [] if product == '日用百货' else facts['goods']
    result = supplement_goods(None, {'use_model': False}, entry, facts, concrete)  # type: ignore[arg-type]
    assert not result['required']
    assert result['complete'] == bool(concrete)


def test_grounded_wrapped_products_require_all_rows_and_exact_quotes() -> None:
    sources = [{'id': 0, 'text': '一次性'}, {'id': 1, 'text': '防护面罩'}, {'id': 2, 'text': '亚克力导视牌'}]
    raw = {'goods': [{'name': '一次性防护面罩', 'source_ids': [0, 1], 'quote': '一次性\n防护面罩'},
                     {'name': '亚克力导视牌', 'source_ids': [2], 'quote': '亚克力导视牌'}]}
    accepted = _validate(raw, sources)
    assert [row['name'] for row in accepted] == ['一次性防护面罩', '亚克力导视牌']
    with pytest.raises(ValueError, match='遗漏多商品'):
        _validate({'goods': [{'name': '鼠标', 'source_ids': [0], 'quote': '鼠标、亚克力导视牌'}]},
                  [{'id': 0, 'text': '鼠标、亚克力导视牌'}])
    with pytest.raises(ValueError, match='遗漏跨行'):
        _validate({'goods': [{'name': '鼠标', 'source_ids': [0, 1], 'quote': '鼠标\n亚克力导视牌'}]},
                  [{'id': 0, 'text': '鼠标'}, {'id': 1, 'text': '亚克力导视牌'}])


def test_goods_region_never_turns_merchant_or_redacted_identifiers_into_products() -> None:
    sources = _sources({'source_text': '项目明细\n透明亚克力导视牌\n[编号省略]\n商家名称：合成文具店\n胶带'})
    assert sources == [{'id': 0, 'text': '透明亚克力导视牌'}]


def enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GlmInvoiceAI, 'public_status', lambda _: {'available': True})


def test_local_complete_or_truly_generic_material_never_requests_ai(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    job, ident = draft(environment)
    job = jobs.update(job['id'], use_model=True)
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: pytest.fail('script-first clear goods'))
    assert tools.execute('extract_goods', {'item_id': ident}, job)['goods'] == ['胶带']
    source = pair(environment, '超市服务')[0]
    result = supplement_goods(jobs, job, source, {'source_text': '*生产生活服务*超市服务'}, [])
    assert not result['required'] and not result['complete']
    assert jobs.get(job['id'])['models'] == 0


def test_text_overflow_remains_visible_to_shared_facts_and_needs_manual_confirmation(
        environment: Any, monkeypatch: Any) -> None:
    from invoice_print_layout.agent_evidence import material_facts
    jobs, tools, _ = environment
    job, _ = draft(environment)
    entry = tools.intake.get(job['material_ids'][0])
    text = '\n'.join('商品名称：导视牌型号' + str(index) for index in range(24))
    assert len(goods_from_text(text, {})) == 21
    # A separate synthetic original preserves the real draft's source bytes.
    path = Path(entry['path']).parent / 'synthetic-overflow-goods.pdf'
    payload = proof(text)
    path.write_bytes(payload)
    extracted = material_facts({**entry, 'path': str(path), 'digest': hashlib.sha256(payload).hexdigest(), 'text': text})
    assert len(extracted['goods']) == 20 and extracted['goods_issues']
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: pytest.fail('overflow is not permission to guess'))
    result = supplement_goods(jobs, {**job, 'use_model': True}, entry, extracted, extracted['goods'])
    assert result['required'] and not result['complete'] and jobs.get(job['id'])['models'] == 0


def test_text_completion_is_once_cached_and_bound_to_revision(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    job, _ = draft(environment)
    job = jobs.update(job['id'], use_model=True)
    entry = tools.intake.get(job['material_ids'][0])
    received = []
    enable(monkeypatch)
    def request(_: Any, rules: str, text: str, **kwargs: Any) -> dict[str, Any]:
        received.append(json.loads(text))
        assert jobs.get(job['id'])['models'] == len(received)
        assert isinstance(text, str) and set(received[-1]) == {'sources'}
        assert '商家' not in text and '图片' not in text and '17.00' not in text
        return answer()
    monkeypatch.setattr(GlmInvoiceAI, '_request', request)
    first = supplement_goods(jobs, job, entry, facts(), [])
    second = supplement_goods(jobs, job, entry, facts(), [])
    assert first['complete'] and first['source'] == 'glm'
    assert second['goods'] == first['goods'] and second['source'] == 'cache'
    assert len(received) == jobs.get(job['id'])['models'] == 1
    timings = jobs.get(job['id'])['timings']['stages']
    assert timings['goods_model']['model_calls'] == 1
    assert timings['goods_completion_cache']['cache_hits'] == 1
    assert timings['goods_completion_cache']['cache_misses'] == 1
    supplement_goods(jobs, job, {**entry, 'revision': entry['revision'] + 1}, facts(), [])
    assert len(received) == jobs.get(job['id'])['models'] == 2
    with tools.store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='invoice_agent_goods_cache'").fetchone()


def test_broad_local_quote_cannot_hide_an_unpriced_second_product(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    job, _ = draft(environment)
    job = jobs.update(job['id'], use_model=True)
    entry = tools.intake.get(job['material_ids'][0])
    text = '商品清单\n鼠标\n亚克力导视牌\n合计：17.00'
    partial = goods_from_text(text, {})
    assert [row['name'] for row in partial] == ['鼠标']
    assert '亚克力' in partial[0]['quote']
    enable(monkeypatch)
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: {'goods': [
        {'name': '鼠标', 'source_ids': [0], 'quote': '鼠标'},
        {'name': '亚克力导视牌', 'source_ids': [1], 'quote': '亚克力导视牌'}]})
    result = supplement_goods(jobs, job, entry, {'source_text': text, 'parser_version': 'synthetic'}, partial)
    assert result['complete'] and jobs.get(job['id'])['models'] == 1
    assert [row['name'] for row in result['goods']] == ['鼠标', '亚克力导视牌']


@pytest.mark.parametrize('failure', ['network', 'empty', 'unbound'])
def test_negative_attempt_survives_adapter_recreation_without_provider_retry(
        environment: Any, monkeypatch: Any, failure: str) -> None:
    jobs, tools, _ = environment
    job, _ = draft(environment)
    job = jobs.update(job['id'], use_model=True)
    entry = tools.intake.get(job['material_ids'][0])
    enable(monkeypatch)
    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if failure == 'network':
            raise GlmAIError('', failure_code='network_error')
        return {'goods': []} if failure == 'empty' else {'goods': [{'name': '胶带', 'source_ids': [0], 'quote': '胶带'}]}
    monkeypatch.setattr(GlmInvoiceAI, '_request', request)
    first = supplement_goods(jobs, job, entry, facts(), [])
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: pytest.fail('durable negative cache'))
    second = supplement_goods(jobs, jobs.get(job['id']), entry, facts(), [])
    assert not first['complete'] and not second['complete']
    assert second['source'] == 'cache' and jobs.get(job['id'])['models'] == 1


def test_budget_exhaustion_does_not_call_or_poison_an_unattempted_product(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    job, _ = draft(environment)
    job = jobs.update(job['id'], use_model=True, models=6)
    entry = tools.intake.get(job['material_ids'][0])
    enable(monkeypatch)
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: pytest.fail('budget reservation first'))
    with pytest.raises(ModelBudgetExceeded):
        supplement_goods(jobs, job, entry, facts(), [])
    job = jobs.update(job['id'], models=0)  # Unit-only boundary reset, never an application retry policy.
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: answer())
    assert supplement_goods(jobs, job, entry, facts(), [])['complete']
    assert jobs.get(job['id'])['models'] == 1


@pytest.mark.parametrize('goods', ['鼠标、午餐', '胶带、盒饭', '鼠标、饮用水', '鼠标+午餐'])
def test_script_rejects_known_mixed_categories_before_any_model(
        environment: Any, monkeypatch: Any, goods: str) -> None:
    jobs, tools, _ = environment
    monkeypatch.setattr(classifier, 'ask_jev', lambda *a: pytest.fail('known mixed basket needs a split decision'))
    monkeypatch.setattr(GlmInvoiceAI, '_request', lambda *a, **k: pytest.fail('complete original text'))
    done = AgentLoop(jobs).run(start(environment, pair(environment, goods), use_model=True)['id'], tools)
    assert done['state'] == 'needs_input' and done['models'] == 0 and done['preview'] is None
    item = tools.store.get(done['item_ids'][0])
    assert len(item['agent_content']['goods']) == 2 and not item['verified']


def test_native_positioned_goods_metadata_is_reused_not_reparsed_from_intake(environment: Any, monkeypatch: Any) -> None:
    from invoice_print_layout import agent_evidence
    jobs, tools, _ = environment
    job, ident = draft(environment)
    seen = []
    def unified(entry: Any, *, cache_root: Any) -> dict[str, Any]:
        seen.append(cache_root)
        return {'goods': [{'name': '胶带', 'quote': '胶带 2卷', 'method': 'native_geometry',
                            'page': 1, 'bbox': [1, 2, 3, 4], 'quantity': '2', 'unit': '卷'},
                           {'name': '亚克力导视牌', 'quote': '亚克力导视牌 1块', 'method': 'native_geometry',
                            'page': 1, 'bbox': [5, 6, 7, 8], 'quantity': '1', 'unit': '块'}]}
    monkeypatch.setattr(agent_evidence, 'material_facts', unified)
    tools.execute('extract_goods', {'item_id': ident}, job)
    result = tools.store.get(ident)['agent_content']['goods']
    assert [good['name'] for good in result] == ['胶带', '亚克力导视牌']
    assert [good['quantity'] for good in result] == ['2', '1']
    assert result[1]['bbox'] == [5, 6, 7, 8] and result[1]['raw_name'] == '亚克力导视牌'
    assert seen == [tools.store.root]


@pytest.mark.parametrize('mutation', ['cancel', 'manual'])
def test_late_goods_completion_never_overwrites_cancel_or_human_edit(
        environment: Any, monkeypatch: Any, mutation: str) -> None:
    from invoice_print_layout import agent_evidence
    jobs, tools, _ = environment
    job, ident = draft(environment)
    job = jobs.update(job['id'], use_model=True)
    enable(monkeypatch)
    monkeypatch.setattr(agent_evidence, 'material_facts', lambda *a, **k: facts())
    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if mutation == 'cancel':
            jobs.stop(job['id'], 'cancelled', '已取消')
        else:
            with tools.store.connect() as db:
                item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
                item['title'] = '人工指定名称'
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(item), ident))
        return answer()
    monkeypatch.setattr(GlmInvoiceAI, '_request', request)
    with pytest.raises(ValueError, match='停止|人工修改|变化'):
        tools.execute('extract_goods', {'item_id': ident}, job)
    item = tools.store.get(ident)
    assert not item.get('agent_content') and not item['verified']
    assert jobs.get(job['id'])['models'] == 1
    if mutation == 'manual':
        assert item['title'] == '人工指定名称'


def test_real_text_completion_then_classification_and_preview_share_full_goods(
        environment: Any, monkeypatch: Any) -> None:
    jobs, tools, project = environment
    # A legacy receipt parser stops at the invoice-info block preceding this
    # explicit product list. The bounded fallback may read only that list.
    purchase = material(tools, project, 'receipt', '17', 'fallback-receipt', text=
                        '收据\n商家名称：合成工具商店\n发票信息\n项目明细\n'
                        '透明亚克力导视牌\n定制磁吸支架\n实付：17.00\n商品名称详见商品清单')
    payment = material(tools, project, 'payment', '17', 'fallback-payment')
    job = start(environment, [purchase, payment], use_model=True)
    enable(monkeypatch)
    text_calls: list[Any] = []
    category_calls: list[Any] = []
    def request(_: Any, rules: str, text: str, **kwargs: Any) -> dict[str, Any]:
        text_calls.append(json.loads(text))
        assert jobs.get(job['id'])['models'] == 1
        return answer()
    def classify(evidence: Any) -> dict[str, Any]:
        category_calls.append(evidence)
        assert '透明亚克力导视牌' in evidence['goods'] and '定制磁吸支架' in evidence['goods']
        return {'choice': 'materials', 'confidence': .99, 'model': classifier.MODEL}
    monkeypatch.setattr(GlmInvoiceAI, '_request', request)
    monkeypatch.setattr(classifier, 'api_key', lambda: 'synthetic-key')
    monkeypatch.setattr(classifier, 'ask_jev', classify)
    original_execute = tools.execute
    def execute(name: str, args: Any, current: Any) -> Any:
        try:
            return original_execute(name, args, current)
        except Exception as exc:
            raise AssertionError((name, repr(exc))) from exc
    monkeypatch.setattr(tools, 'execute', execute)
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 2, (done['message'], done['models'], done['last_results'])
    assert len(text_calls) == len(category_calls) == 1
    assert done['preview']['total_cents'] == 1700
    item = tools.store.get(done['item_ids'][0])
    assert item['title'] == '透明亚克力导视牌、定制磁吸支架采购'
    assert [row['name'] for row in item['agent_content']['goods']] == ['透明亚克力导视牌', '定制磁吸支架']
    assert all(row['method'] == 'glm_text_grounded' and row['quote'] for row in item['agent_content']['goods'])
    assert not item['verified'] and len(item['attachments']) == 2
