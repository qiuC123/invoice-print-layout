from copy import deepcopy
import json
from threading import Lock, Thread
from urllib.request import urlopen

import pymupdf
import pytest

from invoice_print_layout import expense_classifier as classifier
from invoice_print_layout.agent_content import CONTENT_CATEGORIES
from invoice_print_layout.agent_product_rules import inspect_product_rules
from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.reimbursement import compose_matter, selected_attachments
from invoice_print_layout.report_snapshot import snapshot
from invoice_print_layout.report_types import infer_report_choice, validate_report_choice
from invoice_print_layout.workbench import CATEGORIES, ExpenseStore, evaluate
from invoice_print_layout.workbench_web import make_server
from tests.helpers import make_trip_pdf
from tests.test_agent_product_rules import evidence
from tests.test_report_types import matter, synthetic_template
from invoice_print_layout.report_excel import validate_template
from invoice_print_layout.missing_materials import can_acknowledge_missing


@pytest.mark.parametrize('goods', ['打印费', '复印', '*生产生活服务*打印制作',
                                   '打印、胶带', '复印费\n文件夹', '打印制作费\n签字笔2支'])
def test_printing_and_small_items_need_no_model(goods):
    assert classifier.decide({'goods': goods, 'purpose': ''},
                             ask_model=lambda _: pytest.fail('unexpected model call'))['category'] == '打印'


@pytest.mark.parametrize('goods', ['顺丰快递费', '快递费', '同城配送', '闪送费', '快递寄付', '快递到付'])
def test_all_express_services_share_category(goods):
    assert classifier.decide({'goods': goods},
                             ask_model=lambda _: pytest.fail('unexpected model call'))['category'] == '快递'


@pytest.mark.parametrize('choice,goods,category', [
    ('express', '合成平台的同城寄件服务', '快递'),
    ('printing', '装订并输出图纸', '打印'),
])
def test_jev_new_choices_map_to_categories(choice, goods, category):
    result = classifier.decide({'goods': goods}, ask_model=lambda _: {
        'choice': choice, 'confidence': .95, 'model': classifier.MODEL})
    assert result['category'] == category and result['source'] == 'jev'
    assert '同一张' in classifier.CRITERIA['printing'] and '任何平台' in classifier.CRITERIA['express']


@pytest.mark.parametrize('goods', ['打印费\n\n胶带', '打印费、台式电脑', '打印费、盒饭'])
def test_printing_does_not_absorb_other_receipts_or_unrelated_goods(goods):
    result = classifier.decide({'goods': goods}, ask_model=lambda _: {
        'choice': 'unknown', 'confidence': 1, 'model': classifier.MODEL})
    assert result['category'] is None


def test_printing_small_items_are_grounded_in_the_same_complete_source():
    goods, sources = evidence(['打印费', '胶带'])
    before = deepcopy((goods, sources))
    result = inspect_product_rules(goods, sources, CONTENT_CATEGORIES)
    assert result['status'] == 'exact' and result['category'] == '打印' and result['coverage_complete']
    assert (goods, sources) == before
    other_goods, other_sources = evidence(['胶带'])
    goods, sources = evidence(['打印费'])
    other_goods[0]['material_id'] = other_sources[0]['id'] = 'separate-receipt'
    result = inspect_product_rules(goods + other_goods, sources + other_sources, CONTENT_CATEGORIES)
    assert result['category'] is None and result['status'] == 'needs_input'


@pytest.mark.parametrize('voucher', ['invoice', 'receipt', 'purchase'])
def test_printing_requires_voucher_and_payment(voucher):
    assert not evaluate('打印', {voucher})['complete']
    assert not evaluate('打印', {'payment'})['complete']
    assert evaluate('打印', {voucher, 'payment'})['complete']
    assert can_acknowledge_missing(evaluate('打印', {voucher}))
    assert evaluate('打印', {'payment'}, material_basis='payment_only')['complete']


def test_automatic_printing_saved_and_manual_decision_retained(tmp_path, monkeypatch):
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic print receipt', 'amount': '12', 'category': '其他'})
    monkeypatch.setattr(classifier, 'collect', lambda *_: ({'goods': '打印、胶带', 'purpose': ''}, []))
    monkeypatch.setattr(classifier, 'ask_jev', lambda _: pytest.fail('unexpected model call'))
    automatic = AutomaticCategory(store, Lock())
    result = automatic.run(item['id'])
    assert result['saved'] and store.get(item['id'])['category'] == '打印'
    manual = store.create({'title': 'Synthetic confirmed category', 'amount': '12', 'category': '材料采购'})
    assert automatic.run(manual['id'])['status'] == 'skipped'
    assert store.get(manual['id'])['category'] == '材料采购'


def test_reports_group_printing_and_express_without_changing_names_or_template(tmp_path):
    items = [matter('synthetic-print', **validate_report_choice('printing', 'Synthetic print')),
             matter('synthetic-express', **validate_report_choice('express', '顺丰同城')),
             matter('synthetic-other', **validate_report_choice('other', 'Synthetic service'))]
    report = snapshot(items)
    assert [item['category'] for item in report['items']] == ['快递', '打印', '其他']
    assert [row['row'] for row in report['rows']] == [29, 30, 31]
    assert report['rows'][0]['name'] == '顺丰同城'
    assert report['total_cents'] == 300
    assert infer_report_choice({'category': '打印', 'title': 'Synthetic print'})['project_type'] == 'printing'
    assert infer_report_choice({'category': '快递', 'title': 'Synthetic courier'})['project_type'] == 'express'
    validate_template(synthetic_template(tmp_path / 'template.xlsx'))


def test_printing_export_preserves_voucher_payment_and_vector_text(tmp_path):
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic print', 'category': '打印', 'amount': '12'})
    for role in ('receipt', 'payment'):
        source = make_trip_pdf(tmp_path / (role + '.pdf'))
        store.add_attachment(item['id'], source.name, source.read_bytes(), role)
    item = store.get(item['id'])
    assert item['complete'] and {a['role'] for a in selected_attachments(item)} == {'receipt', 'payment'}
    output, _ = compose_matter(store, item, tmp_path)
    with pymupdf.open(output) as pdf:
        assert len(pdf) == 2 and all(page.get_text().strip() for page in pdf)


def test_state_serves_new_categories_and_report_types_on_isolated_port(tmp_path):
    server = make_server(tmp_path, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_port != 8765
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/state') as response:
            state = json.load(response)
        assert tuple(state['categories']) == CATEGORIES
        assert '打印' in state['categories'] and '快递' in state['categories'] and '顺丰' not in state['categories']
        assert {'printing', 'express'} <= {choice['code'] for choice in state['report_types']}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
