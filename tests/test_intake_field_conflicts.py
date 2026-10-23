"""Extraction disagreements retain source values and readable evidence offline."""
from __future__ import annotations

import copy
import io
from pathlib import Path
from typing import Any, cast

from PIL import Image
import pytest

from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue, ORDER_NUMBER_NOTE
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore


class ExtractedAI:
    enabled = True

    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.calls = 0

    def extract(self, _text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        self.calls += 1
        return copy.deepcopy(self.result)


def prepared(tmp_path: Path, *, local: dict[str, str] | None = None,
             proposed: dict[str, str] | None = None, issues: list[str] | None = None,
             manual: set[str] | None = None) -> tuple[IntakeQueue, dict[str, Any], dict[str, Any]]:
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic fixture')['id']
    fields = local if local is not None else {'amount': '17.00', 'merchant': '合成便利店（南门店）'}
    ai_fields = proposed if proposed is not None else {'merchant': '合成便利店（北门店）'}
    ai_text = '\n'.join(f'{key}: {value}' for key, value in ai_fields.items())
    ai = ExtractedAI({'fields': ai_fields, 'role': 'receipt', 'text': ai_text,
                      'evidence': {key: f'AI原文 {key}: {value}' for key, value in ai_fields.items()},
                      'issues': issues or []})
    queue = IntakeQueue(store, ai=cast(GlmInvoiceAI, ai))
    stream = io.BytesIO()
    Image.new('RGB', (20, 40), 'white').save(stream, format='PNG')
    original = queue.receive('synthetic.png', stream.getvalue(), project_id=project)
    text = '\n'.join(f'本地原文 {key}: {value}' for key, value in fields.items())
    data = {**original, 'fields': fields.copy(), 'role': 'receipt', 'text': text,
            'local_evidence': {key: f'本地原文 {key}: {value}' for key, value in fields.items()}}
    queue._apply_ai_analysis(data, original, manual or set(), images=None,
                             complete=True, native_source='local_ocr')
    assert ai.calls == 1 and Path(original['path']).read_bytes() == stream.getvalue()
    assert queue.store.list_items() == []
    return queue, data, original


def test_real_merchant_conflict_retains_both_values_and_both_source_quotes(tmp_path: Path) -> None:
    _, data, _ = prepared(tmp_path)
    assert data['fields']['merchant'] == '合成便利店（南门店）'
    assert data['field_sources']['merchant'] == 'local_ocr'
    assert data['field_conflicts'] == [{
        'field': 'merchant', 'label': '商家名称',
        'local_value': '合成便利店（南门店）', 'ai_value': '合成便利店（北门店）',
        'local_quote': '本地原文 merchant: 合成便利店（南门店）',
        'ai_quote': 'AI原文 merchant: 合成便利店（北门店）', 'local_source': 'local_ocr'}]
    assert data['issues'] == ['AI与本地识别的商家名称不同，请对照原件确认。']
    assert 'merchant' not in data['result'] and data['ai_analysis']['status'] == 'review'


@pytest.mark.parametrize('ai_name', ['合成便利店(南门店)', ' 合成便利店 （ 南门店 ） ', '闪购 合成便利店（南门店）'])
def test_formatting_and_explicit_platform_prefix_are_not_merchant_conflicts(tmp_path: Path, ai_name: str) -> None:
    _, data, _ = prepared(tmp_path, proposed={'merchant': ai_name})
    assert data['fields']['merchant'] == '合成便利店（南门店）'
    assert data['issues'] == [] and not data.get('field_conflicts')
    assert data['ai_analysis']['status'] == 'ok'


@pytest.mark.parametrize(('field', 'local_value', 'ai_value', 'label'), [
    ('amount', '17.00', '20.00', '金额'),
    ('invoice_number', '12345678901234567890', '99999999999999999999', '发票号码'),
    ('payment_status', 'succeeded', 'uncertain', '付款状态')])
def test_core_disagreements_are_retained_in_chinese_without_overwriting_source(
        tmp_path: Path, field: str, local_value: str, ai_value: str, label: str) -> None:
    _, data, _ = prepared(tmp_path, local={'amount': '17.00', 'merchant': '合成商店', field: local_value},
                           proposed={field: ai_value})
    assert data['fields'][field] == local_value
    conflict = data['field_conflicts'][0]
    assert conflict['field'] == field and conflict['label'] == label
    assert conflict['local_value'] == local_value and conflict['ai_value'] == ai_value
    assert data['issues'] == ['AI与本地识别的' + label + '不同，请对照原件确认。']


def test_order_namespace_disagreement_keeps_local_number_as_optional_context(tmp_path: Path) -> None:
    _, data, _ = prepared(tmp_path, local={'amount': '17.00', 'merchant': '合成商店', 'order_number': 'BUSINESS001'},
                           proposed={'order_number': 'PAYMENT002'})
    assert data['fields']['order_number'] == 'BUSINESS001'
    assert data['issues'] == [] and not data.get('field_conflicts')
    assert data['ai_optional_notes'] == [ORDER_NUMBER_NOTE]
    assert data['ai_evidence']['order_number'] == 'AI原文 order_number: PAYMENT002'


@pytest.mark.parametrize('issue', [
    '页面未显示订单号或交易单号，无法建立本笔交易与平台的唯一对应关系',
    '平台订单号与支付宝交易单号不一致',
    '缺少订单号，无法对应支付流水号',
    'AI与原件字段冲突：order_number'])
def test_missing_or_different_order_ids_do_not_standalone_block_a_material(tmp_path: Path, issue: str) -> None:
    _, data, _ = prepared(tmp_path, proposed={}, issues=[issue])
    assert data['issues'] == [] and data['ai_optional_notes'] == [ORDER_NUMBER_NOTE]
    assert data['ai_analysis']['status'] == 'ok'


@pytest.mark.parametrize('issue', [
    '同一文件含多个订单号，无法区分对应交易',
    '缺少订单号且存在多笔可能的支付记录',
    '订单号不一致，金额冲突',
    '未显示订单号，付款状态待确认',
    '订单号不同，商家名称不一致',
    '未显示订单号，可能是退款'])
def test_order_note_filter_does_not_hide_mixed_identity_or_money_ambiguity(tmp_path: Path, issue: str) -> None:
    _, data, _ = prepared(tmp_path, proposed={}, issues=[issue])
    assert data['issues'] == [issue] and data['ai_analysis']['status'] == 'review'
    assert not data.get('ai_optional_notes')


def test_manually_confirmed_fields_are_not_replaced_or_reported_as_ai_conflicts(tmp_path: Path) -> None:
    _, data, _ = prepared(tmp_path, proposed={'merchant': '另一家店', 'amount': '20.00'},
                           manual={'merchant', 'amount'})
    assert data['fields'] == {'merchant': '合成便利店（南门店）', 'amount': '17.00'}
    assert data['field_sources'] == {'merchant': 'manual', 'amount': 'manual'}
    assert not data.get('field_conflicts') and data['issues'] == []


def test_successful_local_retry_clears_obsolete_ai_conflicts_and_uses_no_ai(tmp_path: Path) -> None:
    queue, data, original = prepared(tmp_path)
    assert data['field_conflicts']
    data['issues'] = []
    result = queue._finish_batch_analysis(data, original, native_source='local_ocr')
    assert not result.get('field_conflicts') and result['issues'] == []
    assert result['ai_analysis']['status'] == 'not_needed'
    assert queue.get(original['id']) == result and queue.store.list_items() == []


def test_returned_legacy_field_diagnostic_is_translated_without_fake_candidate(tmp_path: Path) -> None:
    _, data, _ = prepared(tmp_path, proposed={}, issues=['AI与原件字段冲突：merchant'])
    assert data['issues'] == ['AI与本地识别的商家名称不同，请对照原件确认。']
    assert not data.get('field_conflicts')


def test_locally_ambiguous_invoice_money_and_number_cannot_be_filled_by_ai(tmp_path: Path) -> None:
    queue, data, original = prepared(tmp_path, proposed={})
    data['fields'].update(amount='', invoice_number='')
    data['issues'] = ['同文件含多张发票，请拆分核对']
    queue.ai = cast(GlmInvoiceAI, ExtractedAI({'fields': {'amount': '17.00', 'invoice_number': '12345678901234567890'},
        'role': 'receipt', 'text': '合成原件', 'evidence': {'amount': '17.00', 'invoice_number': '12345678901234567890'},
        'issues': []}))
    queue._apply_ai_analysis(data, original, set(), images=None, complete=True, native_source='local_ocr')
    assert data['fields']['amount'] == data['fields']['invoice_number'] == ''
    assert '同文件含多张发票，请拆分核对' in data['issues'] and '金额冲突' in data['issues']
    assert not data.get('field_conflicts')
