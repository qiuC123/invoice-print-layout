"""Local-only payment facts: no OCR, models, or historical source fixtures."""
from __future__ import annotations

import pytest

from invoice_print_layout.local_payment_fields import extract_local_payment, is_label_only_payee


BANK = '''电子回单
测试付款人向示例建设有限公司转账
电子回单专用章 某银行股份有限公司
￥ 200.00
付款人 测试付款人
付款账户 6230****1234
收款人 示例建设有限
公司
收款账户 1001****5678
收款银行 测试银行
转账金额 200.00
转账附言 A区项目，违约罚款
交易时间 2026-09-2114:46:54
￥200.00'''


def payment(title: str = '扫二维码付款-给示例广告店', status: str = '支付成功',
            amount: str = '-17.00') -> str:
    return f'{title}\n{amount}\n当前状态 {status}\n收款方备注 二维码收款\n支付方式 零钱\n转账单号 1234567890123456789012345678'


def test_complete_issued_bank_receipt_keeps_amount_payee_purpose_and_exact_quotes() -> None:
    found = extract_local_payment(BANK)
    assert found['role'] == 'payment'
    assert found['fields'] == {'merchant': '示例建设有限公司', 'amount': '200.00',
                               'payment_status': 'succeeded', 'note': 'A区项目，违约罚款'}
    assert found['evidence']['merchant'] == '收款人 示例建设有限\n公司'
    assert found['evidence']['amount'] == '转账金额 200.00'
    assert found['evidence']['payment_status'] == BANK
    assert found['evidence']['note'] == '转账附言 A区项目，违约罚款'
    assert all(quote in BANK for quote in found['evidence'].values())
    assert not found['issues']
    assert not any(field in found['fields'] for field in ('category', 'goods', 'shop_name'))


@pytest.mark.parametrize('remove', [
    '电子回单专用章 某银行股份有限公司\n',
    '付款人 测试付款人\n',
    '收款人 示例建设有限\n公司\n',
    '转账金额 200.00\n',
    '交易时间 2026-09-2114:46:54\n',
])
def test_incomplete_bank_receipt_does_not_claim_execution(remove: str) -> None:
    found = extract_local_payment(BANK.replace(remove, ''))
    assert found['role'] == 'other'
    assert found['fields']['payment_status'] == 'uncertain'
    assert '付款状态待确认' in found['issues']


@pytest.mark.parametrize('status,expected', [
    ('转账处理中', 'uncertain'), ('处理中', 'uncertain'), ('受理成功', 'uncertain'),
    ('转账申请已提交', 'uncertain'), ('转账已发起', 'uncertain'),
    ('转账失败', 'failed'), ('已撤销', 'failed'), ('已退回', 'refunded'), ('未付款', 'unpaid'),
])
def test_bank_issued_structure_never_overrules_actual_negative_or_pending_status(status: str, expected: str) -> None:
    found = extract_local_payment(BANK + '\n交易状态 ' + status)
    assert found['role'] == 'other'
    assert found['fields']['payment_status'] == expected


@pytest.mark.parametrize('notice', ['转账业务已受理', '电子回单申请已发起', '回单样例', '仅供参考', '预览回单'])
def test_bank_application_or_sample_markers_cannot_be_promoted_by_receipt_shape(notice: str) -> None:
    found = extract_local_payment(BANK + '\n' + notice)
    assert found['role'] == 'other'
    assert found['fields']['payment_status'] == 'uncertain'


def test_electronic_receipt_heading_alone_is_not_success() -> None:
    found = extract_local_payment('电子回单\n付款人 测试甲\n收款人 测试乙\n转账金额 200.00')
    assert found['role'] == 'other'
    assert 'amount' not in found['fields']
    assert found['observed_fields']['amount'] == '200.00'
    assert found['fields']['payment_status'] == 'uncertain'


def test_issued_bank_receipt_supports_next_line_payer_identity() -> None:
    found = extract_local_payment(BANK.replace('付款人 测试付款人', '付款人\n测试付款人'))
    assert found['role'] == 'payment'
    assert found['fields']['amount'] == '200.00'


def test_empty_payee_label_does_not_make_bank_receipt_complete() -> None:
    found = extract_local_payment(BANK.replace('收款人 示例建设有限\n公司', '收款人'))
    assert found['role'] == 'other'
    assert found['fields']['payment_status'] == 'uncertain'


@pytest.mark.parametrize('title,merchant', [
    ('扫二维码付款-给示例广告店', '示例广告店'),
    ('二维码付款-给示例广告店', '示例广告店'),
    ('转账-转给示例订餐经办人', '示例订餐经办人'),
    ('转账-给示例收款人的转账', '示例收款人'),
    ('给示例小店的付款', '示例小店'),
    ('给示例收款人的转账', '示例收款人'),
    ('转账-转给示例广告传媒有限公司开票50元', '示例广告传媒有限公司'),
    ('扫二维码付款-给示例智能锁经办人', '示例智能锁经办人'),
])
def test_payment_title_is_source_bound_payee_identity(title: str, merchant: str) -> None:
    source = payment(title)
    found = extract_local_payment(source)
    assert found['role'] == 'payment'
    assert found['fields']['merchant'] == merchant
    assert found['evidence']['merchant'] == title
    assert found['fields']['amount'] == '17.00'
    assert not any(field in found['fields'] for field in ('category', 'goods', 'title'))


def test_payment_title_does_not_consume_next_row_phone_as_amount_or_goods() -> None:
    source = payment('扫二维码付款-给示例智能锁经办人\n13800000000', amount='-300.00')
    found = extract_local_payment(source)
    assert found['fields']['merchant'] == '示例智能锁经办人'
    assert found['fields']['amount'] == '300.00'
    assert found['evidence']['merchant'] == '扫二维码付款-给示例智能锁经办人'
    assert 'goods' not in found['fields']


@pytest.mark.parametrize('label', ['收款方备注 二维码收款', '收款方（盖章）: 开票人： 收款人',
                                  '收款人', '收款方服务', '二维码收款', '收款银行', '收款方名片'])
def test_labels_and_controls_are_not_payee_identity(label: str) -> None:
    found = extract_local_payment('微信支付\n-20.00\n当前状态 支付成功\n' + label)
    assert 'merchant' not in found['fields']
    assert 'merchant' not in found['evidence']


@pytest.mark.parametrize('value', ['二维码收款', '收款方', '（盖章）: 开票人： 收款人',
                                  '收款方服务', '商户全称', '收款银行', '收款账号', '未知商家', '200.00'])
def test_reject_label_only_payee(value: str) -> None:
    assert is_label_only_payee(value)


def test_payee_may_legitimately_include_a_field_word_inside_its_name() -> None:
    assert not is_label_only_payee('示例收款人服务有限公司')


@pytest.mark.parametrize('source', [
    '微信支付\n收款方全称 示例有限公司\n-20.00\n支付成功',
    '微信支付\n收款方全称\n示例有限公司\n-20.00\n支付成功',
])
def test_explicit_payee_label_supports_same_and_next_row(source: str) -> None:
    found = extract_local_payment(source)
    assert found['fields']['merchant'] == '示例有限公司'
    assert found['evidence']['merchant'] in source


def test_conflicting_explicit_payees_remain_unset() -> None:
    found = extract_local_payment(payment() + '\n收款人 另一示例公司')
    assert 'merchant' not in found['fields']
    assert '收款身份待核对' in found['issues']


@pytest.mark.parametrize('status', ['支付成功', '付款成功', '交易成功', '对方已收钱', '转账成功'])
def test_success_requires_visible_completed_state(status: str) -> None:
    source = payment(status=status)
    found = extract_local_payment(source)
    assert found['role'] == 'payment'
    assert found['fields']['payment_status'] == 'succeeded'
    assert found['evidence']['payment_status'] == '当前状态 ' + status


@pytest.mark.parametrize('status,expected', [
    ('支付失败', 'failed'), ('退款成功', 'refunded'), ('待支付', 'unpaid'),
    ('等待对方收款', 'uncertain'), ('转账中', 'uncertain'), ('已提交', 'uncertain'),
])
def test_negative_and_pending_payment_cannot_be_expense_execution(status: str, expected: str) -> None:
    found = extract_local_payment(payment(status=status))
    assert found['role'] == 'other'
    assert found['fields']['payment_status'] == expected
    assert 'amount' not in found['fields']
    assert found['observed_fields']['amount'] == '17.00'


def test_unlabelled_refund_action_is_not_current_refund() -> None:
    source = payment() + '\n申请退款'
    assert extract_local_payment(source)['fields']['payment_status'] == 'succeeded'


def test_refund_application_as_actual_status_is_a_hold() -> None:
    source = payment(status='退款申请')
    assert extract_local_payment(source)['fields']['payment_status'] == 'uncertain'


def test_conflicting_success_and_failure_never_claims_success() -> None:
    found = extract_local_payment(payment() + '\n交易状态 转账失败')
    assert found['role'] == 'other'
    assert found['fields']['payment_status'] == 'uncertain'


@pytest.mark.parametrize('amount', ['付款金额：￥200.00元', '转账金额 200.00',
                                   '金额（小写）：¥200.00', '人民币（小写）200.00',
                                   '转账金额（元）200.00', '交易金额人民币200.00',
                                   '￥ 200.00', '-200.00'])
def test_rmb_amount_labels_and_standalone_money(amount: str) -> None:
    found = extract_local_payment(payment(amount=amount))
    assert found['fields']['amount'] == '200.00'
    assert found['evidence']['amount'] == amount


def test_account_date_order_phone_and_purpose_numbers_are_not_amounts() -> None:
    source = payment(amount='') + '\n付款账户 6230123412341234\n收款账户 10015678\n手机号13800000000\n交易日期2026-09-21\n转账附言 开票50元，A区14号\n收款银行 测试银行'
    found = extract_local_payment(source)
    assert 'amount' not in found['fields']
    assert found['fields']['note'] == '开票50元，A区14号'


@pytest.mark.parametrize('amount', ['交易金额USD200.00', '交易金额 0.00', '付款金额1.234',
                                   '付款金额 1000000000.00', '付款账户 200.00'])
def test_invalid_or_non_rmb_amounts_are_not_selected(amount: str) -> None:
    assert 'amount' not in extract_local_payment(payment(amount=amount))['fields']


def test_thousands_separator_is_money_but_bank_account_is_not() -> None:
    found = extract_local_payment(payment(amount='转账金额 1,250.50元') + '\n收款账户 1234567890')
    assert found['fields']['amount'] == '1250.50'


@pytest.mark.parametrize('extra', ['付款金额 250.00', '¥250.00', '-250.00', '金额（大写）贰佰伍拾元整'])
def test_conflicting_payment_amounts_are_not_cherry_picked(extra: str) -> None:
    found = extract_local_payment(payment(amount='转账金额200.00') + '\n' + extra)
    assert 'amount' not in found['fields']
    assert '多个付款金额待核对' in found['issues']


@pytest.mark.parametrize('chinese,amount', [('贰佰元整', '200.00'), ('壹仟贰佰伍拾元伍角', '1250.50'),
                                         ('壹万零贰佰元零伍分', '10200.05'), ('零元伍分', '0.05')])
def test_labelled_financial_uppercase_amount(chinese: str, amount: str) -> None:
    source = payment(amount='金额（大写）' + chinese)
    found = extract_local_payment(source)
    assert found['fields']['amount'] == amount
    assert found['evidence']['amount'] == '金额（大写）' + chinese


@pytest.mark.parametrize('chinese', ['贰佰佰元整', '壹贰元整', '壹佰零拾元整', '贰佰元伍伍分'])
def test_malformed_uppercase_is_not_reinterpreted_as_money(chinese: str) -> None:
    assert 'amount' not in extract_local_payment(payment(amount='金额（大写）' + chinese))['fields']


def test_same_value_uppercase_and_repeated_arabic_totals_agree() -> None:
    found = extract_local_payment(payment(amount='转账金额200.00') + '\n¥200.00\n金额（大写）贰佰元整')
    assert found['fields']['amount'] == '200.00'
    assert not found['issues']


def test_manual_fields_are_untouched_but_observations_remain_available() -> None:
    existing = {'amount': '99.00', 'merchant': '人工收款方', 'payment_status': 'unpaid', 'note': '人工用途'}
    found = extract_local_payment(BANK, current_fields=existing, manual_fields=set(existing))
    assert found['fields'] == {}
    assert found['observed_fields']['amount'] == '200.00'
    assert found['observed_fields']['note'] == 'A区项目，违约罚款'
    assert existing == {'amount': '99.00', 'merchant': '人工收款方', 'payment_status': 'unpaid', 'note': '人工用途'}


def test_existing_amount_conflict_is_not_overwritten() -> None:
    found = extract_local_payment(payment(), current_fields={'amount': '21.00'})
    assert 'amount' not in found['fields']
    assert found['conflicts']['amount']['existing'] == '21.00'
    assert found['conflicts']['amount']['observed'] == '17.00'
    assert '金额冲突' in found['issues']


def test_equivalent_existing_amount_is_not_a_conflict() -> None:
    found = extract_local_payment(payment(), current_fields={'amount': '17'})
    assert found['fields']['amount'] == '17.00'
    assert not found['conflicts']


def test_genuine_existing_identity_is_not_overwritten() -> None:
    found = extract_local_payment(payment(), current_fields={'merchant': '另一公司'})
    assert 'merchant' not in found['fields']
    assert 'merchant' in found['conflicts']
    assert '收款身份待核对' in found['issues']


def test_nonmanual_label_placeholder_can_be_replaced_by_quoted_identity() -> None:
    found = extract_local_payment(payment(), current_fields={'merchant': '二维码收款'})
    assert found['fields']['merchant'] == '示例广告店'
    manual = extract_local_payment(payment(), current_fields={'merchant': '二维码收款'}, manual_fields={'merchant'})
    assert 'merchant' not in manual['fields']


def test_unsuccessful_existing_state_is_not_silently_overwritten() -> None:
    found = extract_local_payment(payment(), current_fields={'payment_status': 'failed'})
    assert 'payment_status' not in found['fields']
    assert 'amount' not in found['fields']
    assert '付款状态待确认' in found['issues']


@pytest.mark.parametrize('state', ['退款金额17.00', '退款申请已提交', '已发起退款申请',
                                 '已 申请退款', '当前状态\n\n退款申请', '退款状态\n退款申请'])
def test_actual_refund_rows_override_historical_success_without_writing_amount(state: str) -> None:
    source = payment() + '\n' + state
    found = extract_local_payment(source)
    assert found['role'] == 'other' and found['fields']['payment_status'] == 'uncertain'
    assert 'amount' not in found['fields']
    assert found['observed_fields']['amount'] == '17.00'
    assert found['evidence']['payment_status'] in source


@pytest.mark.parametrize('control', ['  可申请退款 >  ', '退款申请↗', '申请退款›'])
def test_standalone_refund_controls_keep_current_success(control: str) -> None:
    found = extract_local_payment(payment() + '\n' + control)
    assert found['role'] == 'payment' and found['fields']['amount'] == '17.00'


def test_unknown_existing_state_can_be_completed_from_actual_success() -> None:
    found = extract_local_payment(payment(), current_fields={'payment_status': 'uncertain'})
    assert found['fields']['payment_status'] == 'succeeded'


def test_existing_note_is_preserved_and_literal_purpose_is_not_lost() -> None:
    found = extract_local_payment(BANK, current_fields={'note': '原有备注'})
    assert 'note' not in found['fields']
    assert found['observed_fields']['note'] == 'A区项目，违约罚款'
    assert found['evidence']['note'] == '转账附言 A区项目，违约罚款'


def test_non_payment_receipt_does_not_promote_form_labels_or_handwriting_guess() -> None:
    source = '示例文化传播有限公司\n收据\n品名 单位 金额\n打印+年\n收款方（盖章）: 开票人： 收款人'
    found = extract_local_payment(source)
    assert found['role'] == ''
    assert found['fields'] == {}
    assert found['evidence'] == {}


def test_invoice_does_not_get_reclassified_as_payment() -> None:
    source = '电子发票\n发票号码12345678901234567890\n备注：转账付款\n金额200.00'
    assert extract_local_payment(source)['role'] == ''


def test_rows_input_preserves_exact_newline_quotes() -> None:
    rows = ['微信支付', '收款人', '示例收款方', '-17.00', '当前状态', '支付成功']
    found = extract_local_payment(rows)
    assert found['fields']['merchant'] == '示例收款方'
    assert found['fields']['payment_status'] == 'succeeded'
    assert all(quote in '\n'.join(rows) for quote in found['evidence'].values())
