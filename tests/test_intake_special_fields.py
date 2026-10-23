"""Normal intake's special-field bridge preserves manual edits and evidence conflicts."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI, LocalOnlyInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from tests.test_local_payment_fields import BANK


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IntakeQueue:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('special-field bridge must not call OCR or a cloud model')
    monkeypatch.setattr(GlmInvoiceAI, '_request', forbidden)
    monkeypatch.setattr(IntakeQueue, '_apply_ai_analysis', forbidden)
    monkeypatch.setattr('invoice_print_layout.receipt_intake.read_receipt', forbidden)
    return IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())


def prepare(queue: IntakeQueue, text: str, *, role: str = 'other',
            fields: dict[str, str] | None = None, issues: list[str] | None = None,
            manual: tuple[str, ...] = (), manual_role: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for index, row in enumerate(text.splitlines()):
            page.insert_text((25, 25 + index * 14), row, fontname='china-s', fontsize=9)
        original = queue.receive('synthetic.pdf', pdf.tobytes())
    original.update(role=role, fields=copy.deepcopy(fields or {}), manual_fields=list(manual),
                    manual_role=manual_role)
    data = copy.deepcopy(original)
    data.update(text=text, issues=list(issues or []))
    return data, original


def complete(queue: IntakeQueue, data: dict[str, Any], original: dict[str, Any], source: str = 'local_ocr') -> None:
    path = Path(original['path'])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    queue._complete_local_special_fields(data, original, source)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before == original['digest']
    assert queue.store.list_items() == []


def test_issued_bank_receipt_completes_payee_amount_success_and_penalty_purpose(queue: IntakeQueue) -> None:
    data, original = prepare(queue, BANK, issues=['金额冲突', '收款身份待核对', '付款状态待确认'])
    complete(queue, data, original)
    assert data['role'] == 'payment'
    assert {k: data['fields'][k] for k in ('amount', 'merchant', 'payment_status', 'note')} == {
        'amount': '200.00', 'merchant': '示例建设有限公司', 'payment_status': 'succeeded',
        'note': 'A区项目，违约罚款'}
    assert not data['issues'] and not data.get('field_conflicts')
    assert data['local_evidence']['note'] == '转账附言 A区项目，违约罚款'
    assert data['field_sources']['amount'] == 'local_ocr'
    assert data['local_payment_observations']['fields']['amount'] == '200.00'
    assert 'category' not in data['fields'] and 'goods' not in data['fields']


def test_manual_amount_and_merchant_remain_authoritative_but_source_observations_survive(queue: IntakeQueue) -> None:
    data, original = prepare(queue, BANK, fields={'amount': '300.00', 'merchant': '人工指定收款单位'},
                             manual=('amount', 'merchant'), issues=['金额冲突'])
    complete(queue, data, original)
    assert data['fields']['amount'] == '300.00' and data['fields']['merchant'] == '人工指定收款单位'
    assert data['field_sources']['amount'] == data['field_sources']['merchant'] == 'manual'
    assert data['local_payment_observations']['fields']['amount'] == '200.00'
    assert data['local_payment_observations']['fields']['merchant'] == '示例建设有限公司'
    assert '金额冲突' in data['issues']


def test_manual_receipt_role_is_not_changed_by_bank_payment_projection(queue: IntakeQueue) -> None:
    data, original = prepare(queue, BANK, role='receipt', manual_role=True)
    complete(queue, data, original)
    assert data['role'] == 'receipt' and data['manual_role'] is True


@pytest.mark.parametrize('field,old,observed,issue', [
    ('amount', '300.00', '200.00', '金额冲突'),
    ('merchant', '另一家测试公司', '示例建设有限公司', '收款身份待核对'),
    ('payment_status', 'failed', 'succeeded', '付款状态待确认'),
])
def test_existing_critical_conflict_keeps_both_values_and_issue(queue: IntakeQueue, field: str,
        old: str, observed: str, issue: str) -> None:
    data, original = prepare(queue, BANK, fields={field: old})
    data['local_evidence'] = {field: '原有识别文字'}
    data['field_sources'] = {field: 'native_pdf'}
    complete(queue, data, original)
    assert data['fields'][field] == old
    assert isinstance(data['field_conflicts'], list)
    conflict = next(value for value in data['field_conflicts'] if value['field'] == field)
    assert conflict['local_value'] == old
    assert conflict['ai_value'] == observed
    assert conflict['ai_quote'] in BANK
    assert conflict['local_quote'] == '原有识别文字'
    assert conflict['local_source'] == 'native_pdf'
    assert conflict['observed_source'] == 'labelled_payment'
    assert issue in data['issues']


@pytest.mark.parametrize('footer', ['付款方式 微信支付', '付款方式 微信支付\n当前状态 支付成功'])
def test_shopping_receipt_is_not_relabelled_as_payment_for_a_paid_footer(queue: IntakeQueue, footer: str) -> None:
    text = '购物收据\n商家名称 示例文具店\n商品名称 打印纸\n实付款17.00\n' + footer
    data, original = prepare(queue, text, role='receipt', fields={'amount': '17.00', 'merchant': '示例文具店'})
    complete(queue, data, original)
    assert data['role'] == 'receipt'
    assert data['fields']['amount'] == '17.00' and data['fields']['merchant'] == '示例文具店'


TRIP = '''高德地图—打车——行程单
上车时间
起点
终点
金额
T3出行
2030-01-01 12:00
地点甲
地点乙
17.78元
共计1单行程，合计17.78元
页码：1 / 1'''
WAYBILL = '''运单起止日期：2030-01-01至2030-01-01
共1笔运单，合计13.75元
订单号
发件信息
收件信息
8547695660999
帮我送
13.75元
在线支付
页码：1 / 1'''


@pytest.mark.parametrize('text,role,category,amount', [(TRIP, 'trip', '打车', '17.78'),
                                                      (WAYBILL, 'detail', '顺丰', '13.75')])
def test_complete_native_trip_or_city_waybill_projects_actual_role_locally(queue: IntakeQueue,
        text: str, role: str, category: str, amount: str) -> None:
    data, original = prepare(queue, text, fields={'category': '其他'}, issues=['金额冲突'])
    complete(queue, data, original, 'native_pdf')
    assert data['role'] == role and data['fields']['amount'] == amount
    assert data['fields']['category'] == category and '金额冲突' not in data['issues']
    assert data['native_transport_evidence']['method'] == 'native_transport'
    assert data['field_sources']['amount'] == 'native_pdf'


def test_manual_transport_role_and_amount_are_preserved(queue: IntakeQueue) -> None:
    data, original = prepare(queue, TRIP, role='other', fields={'amount': '99.00', 'merchant': '人工商家'},
                             manual=('amount', 'merchant'), manual_role=True, issues=['金额冲突'])
    complete(queue, data, original, 'native_pdf')
    assert data['role'] == 'other' and data['fields']['amount'] == '99.00'
    assert data['fields']['merchant'] == '人工商家' and '金额冲突' in data['issues']
    assert data['field_sources']['amount'] == data['field_sources']['merchant'] == 'manual'


def test_native_transport_does_not_erase_existing_amount_conflict(queue: IntakeQueue) -> None:
    data, original = prepare(queue, TRIP, fields={'amount': '99.00'}, issues=['金额冲突'])
    complete(queue, data, original, 'native_pdf')
    assert data['role'] == 'other' and data['fields']['amount'] == '99.00'
    assert '金额冲突' in data['issues'] and not data.get('native_transport_evidence')


def test_partial_native_trip_is_not_promoted_even_with_cached_amount(queue: IntakeQueue) -> None:
    data, original = prepare(queue, TRIP.replace('1 / 1', '1 / 2'), fields={'amount': '17.78'})
    complete(queue, data, original, 'native_pdf')
    assert data['role'] == 'other' and not data.get('native_transport_evidence')


def test_pending_bank_receipt_stays_uncertain_in_the_actual_bridge(queue: IntakeQueue) -> None:
    data, original = prepare(queue, BANK + '\n交易状态 转账处理中')
    complete(queue, data, original)
    assert data['role'] == 'other' and data['fields']['payment_status'] == 'uncertain'
    assert data['fields']['amount'] == ''
    assert data['local_payment_observations']['fields']['amount'] == '200.00'
    assert '付款状态待确认' in data['issues']


@pytest.mark.parametrize('state', ['退款成功', '支付失败', '交易关闭', '等待对方收款', '已退回',
                                 '退款金额40.00', '退款申请已提交', '已申请退款', '已发起退款申请',
                                 '当前状态：退款申请', '当前状态\n退款申请', '支付状态\n退款申请',
                                 '付款状态\n退款申请'])
def test_refund_guard_cannot_be_undone_by_local_payment_completion(queue: IntakeQueue, state: str) -> None:
    text = '微信支付\n-40.00\n支付成功\n商家名称：示例小店\n' + state
    data, original = prepare(queue, text, fields={'amount': '', 'payment_status': 'uncertain'},
                             issues=['付款状态待确认'])
    complete(queue, data, original)
    assert data['role'] == 'other' and data['fields']['amount'] == ''
    assert data['fields']['payment_status'] != 'succeeded'
    assert data['local_payment_observations']['fields']['amount'] == '40.00'
    assert '付款状态待确认' in data['issues']


@pytest.mark.parametrize('control', ['申请退款', '可申请退款', '退款申请', '  可申请退款 >  '])
def test_refund_action_control_keeps_successful_payment_in_bridge(queue: IntakeQueue, control: str) -> None:
    text = '微信支付\n-40.00\n支付成功\n商家名称：示例小店\n' + control
    data, original = prepare(queue, text, fields={'amount': '', 'payment_status': 'uncertain'})
    complete(queue, data, original)
    assert data['role'] == 'payment' and data['fields']['amount'] == '40.00'
    assert data['fields']['payment_status'] == 'succeeded'


def test_failed_payment_preserves_manual_amount_without_promoting_execution(queue: IntakeQueue) -> None:
    data, original = prepare(queue, BANK + '\n交易状态 转账失败', fields={'amount': '300.00'},
                             manual=('amount',))
    complete(queue, data, original)
    assert data['role'] == 'other' and data['fields']['amount'] == '300.00'
    assert data['fields']['payment_status'] == 'failed'
    assert data['field_sources']['amount'] == 'manual'


def test_local_payment_conflict_keeps_existing_ai_conflict_list_contract(queue: IntakeQueue) -> None:
    data, original = prepare(queue, BANK, fields={'amount': '300.00'})
    previous = {'field': 'merchant', 'local_value': '旧名称', 'ai_value': '模型候选',
                'local_quote': '旧原文', 'ai_quote': '模型原文', 'local_source': 'native_pdf'}
    data['field_conflicts'] = [copy.deepcopy(previous)]
    complete(queue, data, original)
    assert data['field_conflicts'][0] == previous
    assert data['field_conflicts'][1]['field'] == 'amount'
    assert data['field_conflicts'][1]['observed_source'] == 'labelled_payment'
