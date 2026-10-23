from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from invoice_print_layout.report_choice_rules import PRINTING_ALIASES, printing_report_suggestion
from invoice_print_layout.report_types import infer_report_choice, validate_report_choice


@pytest.mark.parametrize('alias', PRINTING_ALIASES)
def test_explicit_invoice_transaction_aliases_suggest_one_name(alias: str) -> None:
    item = {'title': '电子发票（费用类别待核对）', 'category': '其他', 'amount_cents': 5000,
            'text': '项目名称\n*生产生活服务*' + alias + '\n49.50 0.50\n价税合计50.00'}
    before = deepcopy(item)
    assert infer_report_choice(item) == validate_report_choice('other', '打印')
    assert printing_report_suggestion(item)['evidence'] == alias
    assert item == before


@pytest.mark.parametrize('key', ['goods', 'purchase_content', 'payment_note', 'transaction_description'])
def test_optional_description_fields_support_suggestion(key: str) -> None:
    item = {'fields': {key: '打印费'}, 'amount_cents': 5000}
    assert printing_report_suggestion(item)['project_name'] == '打印'


@pytest.mark.parametrize('text', ['商品说明：复印服务', '付款备注：打印费', '商品名称\n图文快印'])
def test_labelled_description_in_saved_text_is_usable(text: str) -> None:
    assert infer_report_choice({'text': text})['project_name'] == '打印'


@pytest.mark.parametrize('item', [
    {'merchant': 'Synthetic 广告传媒有限公司'}, {'merchant': 'Synthetic 图文快印店'},
    {'text': '销方名称：Synthetic 图文快印有限公司\n打印此发票'},
    {'text': '*办公设备*打印机'}, {'text': '*设备*复印机'},
    {'text': '打印发票\n打印费'}, {'note': '历史登记：打印费'},
    {'goods': '打印机安装（不包含打印服务）'}, {'goods': '设计费，不包括打印制作'},
    {}, {'goods': '', 'payment_note': None},
])
def test_merchant_equipment_footer_unlabelled_and_missing_details_do_not_infer(item: dict[str, Any]) -> None:
    assert printing_report_suggestion(item) == {}
    assert infer_report_choice(item)['project_name'] == ''


@pytest.mark.parametrize('fields', [
    {'project_type': 'other', 'project_name': '喷绘'}, {'title': '用户确认的用途'},
    {'display_name': '电源线'}, {'project_type': 'materials'}, {'category': '材料采购'},
    {'category': '酒店'}, {'report_section': 'hotel'},
])
def test_named_or_other_classifications_keep_user_choices(fields: dict[str, Any]) -> None:
    item = {'goods': '打印制作', **fields}
    assert printing_report_suggestion(item) == {}
    assert infer_report_choice(item)['project_name'] != '打印'


def test_related_saved_queue_evidence_does_not_change_amount_or_source() -> None:
    item = {'title': '发票文件1', 'category': '其他', 'source': 'intake:synthetic-q',
            'amount_cents': 5000, 'recognition_sources': [{'fields': {'goods': ''},
            'text': '*生产生活服务*打印制作\n合计49.50\n税额0.50\n价税合计50.00'}]}
    before = deepcopy(item)
    assert infer_report_choice(item)['project_name'] == '打印'
    assert printing_report_suggestion(item)['source'] == 'local_rule'
    assert item == before


def test_machine_generated_merchant_invoice_title_does_not_block_description() -> None:
    item = {'title': 'Synthetic 广告公司 · 发票', 'category': '其他',
            'text': '*生产生活服务*打印制作'}
    assert infer_report_choice(item)['project_name'] == '打印'
    item.update(project_type='other', project_name='人工保留名称')
    assert printing_report_suggestion(item) == {}
    assert infer_report_choice(item)['project_name'] == '人工保留名称'
