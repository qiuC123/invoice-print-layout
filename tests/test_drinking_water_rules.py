from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from invoice_print_layout.report_types import infer_report_choice, material_report_suggestion, validate_report_choice


@pytest.mark.parametrize('description', ['娃哈哈饮用水596ml*12瓶', '矿泉水24瓶', '纯净水', '瓶装水', '桶装水'])
def test_explicit_water_line_suggests_existing_fixed_template_row(description: str) -> None:
    item = {'title': '电子发票（费用类别待核对）', 'category': '其他', 'amount_cents': 2960,
            'text': '*生产生活服务*派送服务\n7.55 0.45\n*软饮料*' + description +
                    '\n22.65 2.95\n*软饮料*' + description + '\n-3.54 -0.46\n价税合计29.60'}
    before = deepcopy(item)
    assert infer_report_choice(item) == validate_report_choice('living', '现场饮用水、夜宵')
    assert material_report_suggestion(item)['evidence'] == description
    assert item == before


@pytest.mark.parametrize('key', ['goods', 'purchase_content', 'payment_note', 'transaction_description'])
def test_optional_water_fields_are_usable_without_full_text(key: str) -> None:
    assert infer_report_choice({'fields': {key: '现场采购饮用水'}})['report_section'] == 'dining'


@pytest.mark.parametrize('item', [
    {}, {'merchant': 'Synthetic 饮用水有限公司'}, {'goods': '娃哈哈营养快线'},
    {'text': '销方：Synthetic 矿泉水公司'}, {'text': '未经标注的饮用水'},
    {'goods': '饮用水过滤器'}, {'goods': '矿泉水瓶标签'}, {'goods': '服务费，不含饮用水'},
    {'project_type': 'materials', 'goods': '矿泉水'},
    {'project_type': 'other', 'project_name': '人工确定项目', 'goods': '矿泉水'},
    {'title': '人工确定用途', 'goods': '矿泉水'}, {'category': '酒店', 'goods': '矿泉水'},
])
def test_missing_merchant_brand_equipment_or_manual_choices_are_not_reclassified(item: dict[str, Any]) -> None:
    assert material_report_suggestion(item) == {}


def test_generic_machine_title_and_saved_related_queue_text_support_water() -> None:
    item = {'title': 'Synthetic 商家 · 发票', 'amount_cents': 2960, 'category': '其他',
            'recognition_sources': [{'text': '*软饮料*娃哈哈饮用水59\n6ml*12瓶', 'fields': {}}]}
    before = deepcopy(item)
    assert material_report_suggestion(item)['project_type'] == 'living'
    assert infer_report_choice(item)['project_name'] == '现场饮用水、夜宵'
    assert item == before
