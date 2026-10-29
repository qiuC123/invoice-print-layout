"""The complete original controls classification, naming and bounded continuation."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from tests.legacy_agent_fixture import LegacyQuestionFixture, review_question
from invoice_print_layout import agent_content, expense_classifier as classifier
from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.agent_content import ContentTools
from tests.test_invoice_agent import environment, start
from tests.test_invoice_agent_content import pair


def test_exact_mouse_and_signboard_go_to_preview_without_a_model(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: pytest.fail('Explicit goods require no model'))
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标、亚克力导视牌'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert item['title'] == '鼠标、亚克力导视牌采购' and item['category'] == '材料采购'
    assert {g['name'] for g in item['agent_content']['goods']} == {'鼠标', '亚克力导视牌'}
    assert item['agent_content']['product_check']['coverage_complete']
    assert not item['verified'] and item['stage'] == 'draft' and len(item['attachments']) == 2
    assert done['preview']['total_cents'] == 1700


@pytest.mark.parametrize('goods', ['鼠标蛋糕', '亚克力导视牌安装服务', '亚克力导视牌租赁费'])
def test_complete_food_or_service_cannot_be_released_as_materials_even_by_model(
        environment: Any, monkeypatch: Any, goods: str) -> None:
    jobs, tools, _ = environment
    sent: list[dict[str, str]] = []
    def wrong_model(evidence: dict[str, str]) -> dict[str, Any]:
        sent.append(deepcopy(evidence))
        return {'choice': 'materials', 'confidence': .99, 'model': classifier.MODEL}
    monkeypatch.setattr(classifier, 'api_key', lambda: 'synthetic-test-key')
    monkeypatch.setattr(classifier, 'ask_jev', wrong_model)
    done = AgentLoop(jobs).run(start(environment, pair(environment, goods), use_model=True)['id'], tools)
    assert done['state'] == 'ready' and not done.get('preview')
    assert len(sent) == done['models'] == 1 and goods in sent[0]['goods']
    item = tools.store.get(done['item_ids'][0])
    assert item['agent_content']['decision']['category'] is None
    assert any(goods in g['name'] for g in item['agent_content']['goods'])
    assert not item['verified']


def test_mouse_cake_is_sent_and_named_as_the_full_food(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    monkeypatch.setattr(classifier, 'api_key', lambda: 'synthetic-test-key')
    def food(evidence: dict[str, str]) -> dict[str, Any]:
        assert evidence['goods'] == '鼠标蛋糕'
        return {'choice': 'food', 'confidence': .99, 'model': classifier.MODEL}
    monkeypatch.setattr(classifier, 'ask_jev', food)
    done = AgentLoop(jobs).run(start(environment, pair(environment, '鼠标蛋糕'), use_model=True)['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 1
    item = tools.store.get(done['item_ids'][0])
    assert item['category'] in {'外卖', '餐饮'} and item['title'] == '鼠标蛋糕'
    good = item['agent_content']['goods'][0]
    assert good['name'] == '鼠标蛋糕' and good['recognized_name'] == '鼠标'
    assert '鼠标蛋糕' in good['quote'] and good['sha256']


def test_wrapped_exact_product_reaches_details_with_full_source_quote(environment: Any) -> None:
    jobs, tools, _ = environment
    done = AgentLoop(jobs).run(start(environment, pair(environment, '透明亚克力\n导视牌'))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    good = item['agent_content']['goods'][0]
    assert good['name'] == '透明亚克力导视牌' and item['title'] == '透明亚克力导视牌采购'
    assert '导视牌' in good['quote'] and good['recognized_quote'] == '商品名称：透明亚克力'
    assert good['raw_name'] == good['recognized_name'] == '透明亚克力'


def test_product_label_disambiguates_a_food_qualifier_from_a_second_description(
        environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    monkeypatch.setattr(classifier, 'api_key', lambda: 'synthetic-test-key')
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: {'choice': 'food', 'confidence': .99, 'model': classifier.MODEL})
    done = AgentLoop(jobs).run(start(environment, pair(environment,
        '鼠标蛋糕、瓶装饮用水\n商品说明：食用奶油蛋糕及饮用水'), use_model=True)['id'], tools)
    assert done['state'] == 'ready'
    goods = tools.store.get(done['item_ids'][0])['agent_content']['goods']
    assert any(g['name'] == '瓶装饮用水' and '瓶装饮用水' in g['quote'] for g in goods)
    assert any(g['name'] == '鼠标蛋糕' for g in goods)


@pytest.mark.parametrize('goods', ['鼠标、亚克力导视牌、午餐', '鼠标蛋糕、亚克力导视牌',
                                  '鼠标、亚克力导视牌安装服务', '鼠标、未知曦岚项'])
def test_mixed_or_unknown_baskets_are_not_overruled_by_one_familiar_good(
        environment: Any, monkeypatch: Any, goods: str) -> None:
    jobs, tools, _ = environment
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: pytest.fail('No model authorized'))
    done = AgentLoop(jobs).run(start(environment, pair(environment, goods))['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0 and not done.get('preview')
    item = tools.store.get(done['item_ids'][0])
    assert item['agent_content']['decision']['category'] is None
    assert review_question(done)['type'] == 'expense_content' and not item['verified']


@pytest.mark.parametrize('goods', ['茶饮料\n量杯', '奶茶机租赁服务', '住宿服务会议室租赁'])
def test_nonmaterial_canonical_name_never_exempts_the_full_source(
        environment: Any, monkeypatch: Any, goods: str) -> None:
    jobs, tools, _ = environment
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: pytest.fail('No model authorized'))
    done = AgentLoop(jobs).run(start(environment, pair(environment, goods))['id'], tools)
    assert done['state'] == 'ready' and not done.get('preview') and done['models'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert item['agent_content']['decision']['category'] is None
    assert not item['verified'] and item['stage'] == 'draft'


@pytest.mark.parametrize('extra', ['', '\n商品清单\n量杯', '\n购买明细：量杯',
                                  '\n商品信息\n量杯', '\n*日用杂品*量杯'])
def test_special_ticket_identity_cannot_hide_an_additional_goods_section(extra: str) -> None:
    goods = [{'name': '高铁车票', 'material_id': 'rail', 'sha256': 'a' * 64}]
    facts = [{'id': 'rail', 'sha256': 'a' * 64, 'role': 'trip', 'goods_complete': True,
              'source_text': '电子发票（铁路电子客票）\n合计48.00' + extra}]
    checked = ContentTools._special_ticket_check({'goods': goods}, facts)
    assert (checked is not None) == (not extra)


def test_one_carrier_source_cannot_qualify_another_attachments_service(
        environment: Any, monkeypatch: Any) -> None:
    from invoice_print_layout import agent_evidence
    _, tools, _ = environment
    goods, facts = [], []
    for ident, carrier, name in [('sf', '顺丰速运', '顺丰快递费'), ('other', '中通快递', '收派服务')]:
        text = '商品名称：收派服务\n承运方：' + carrier + '\n实付48.00'
        goods.append({'name': name, 'raw_name': '收派服务', 'material_id': ident,
                      'sha256': ident * 16, 'quote': '商品名称：收派服务'})
        facts.append({'id': ident, 'sha256': ident * 16, 'role': 'invoice', 'goods_complete': True,
                      'goods': [{'name': name, 'quote': '商品名称：收派服务'}], 'source_text': text})
    monkeypatch.setattr(agent_evidence, 'material_facts', lambda e, **_: next(f for f in facts if f['id'] == e['id']))
    checked = tools.content._inspect_products({'goods': goods}, [{'id': 'sf'}, {'id': 'other'}])
    assert checked['status'] != 'exact' and checked['category'] is None


def test_old_low_confidence_job_rechecks_new_rules_once_without_new_budget(environment: Any, monkeypatch: Any) -> None:
    jobs, tools, _ = environment
    entries = pair(environment, '鼠标、亚克力导视牌')
    with monkeypatch.context() as old:
        old.setattr(agent_content, 'PRODUCT_RULES_VERSION', 'synthetic-old-product-rules')
        old.setattr(tools.content, '_inspect_products', lambda *_: {
            'status': 'model', 'category': None, 'reason': 'Synthetic historical path',
            'goods_text': '鼠标\n亚克力导视牌', 'products': [], 'rules_version': 'synthetic-old-product-rules'})
        old.setattr(classifier, 'api_key', lambda: 'synthetic-test-key')
        old.setattr(classifier, 'ask_jev', lambda _: {'choice': 'materials', 'confidence': .78, 'model': classifier.MODEL})
        waiting = LegacyQuestionFixture(jobs).run(start(environment, entries, use_model=True)['id'], tools)
    assert waiting['state'] == 'needs_input' and waiting['models'] == 1
    before_item = tools.store.get(waiting['item_ids'][0])
    with tools.store.connect() as db:
        old_actions = {r['key'] for r in db.execute('SELECT key FROM invoice_agent_actions WHERE job_id=?', (waiting['id'],))}
    queued = jobs.retry(waiting['id'])
    assert queued['state'] == 'queued'
    assert queued['models'] == waiting['models'] and queued['tools'] == waiting['tools']
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: pytest.fail('New exact rules require no model'))
    done = LegacyQuestionFixture(jobs).run(queued['id'], tools)
    assert done['state'] == 'ready' and done['id'] == waiting['id']
    assert done['models'] == 1 and done['tools'] > waiting['tools'] and done['elapsed'] >= waiting['elapsed']
    assert done['item_ids'] == waiting['item_ids'] and len(tools.store.list_items()) == 1
    item = tools.store.get(done['item_ids'][0])
    assert item['amount_cents'] == before_item['amount_cents'] == 1700
    assert item['attachments'] == before_item['attachments'] and not item['verified']
    with tools.store.connect() as db:
        actions = {r['key'] for r in db.execute('SELECT key FROM invoice_agent_actions WHERE job_id=?', (waiting['id'],))}
        assert old_actions <= actions
    with pytest.raises(ValueError):
        jobs.retry(done['id'])
