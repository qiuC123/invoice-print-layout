"""Real payment-screen shapes with synthetic identities, amounts and image bytes."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
import pytest

from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.local_payment_fields import extract_local_payment
from invoice_print_layout.receipt_intake import labelled_counterparty
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.workbench import ExpenseStore


TITLE = '扫二维码付款-给示例广告店'


def screen(amount: str, card: str = '收款方名片') -> list[str]:
    return ['14:59 CS', TITLE, '-' + amount, '当前状态 支付成功', '收款方备注 二维码收款',
            '支付方式 银行储蓄卡(1234)', '转账时间 2030年1月1日15:48:55',
            '转账单号 10001073012030010100000000000001', '账单服务',
            '对订单有疑惑 发起群收款', '申请电子凭证', '收款方服务', card, '本服务由财付通提供']


@pytest.mark.parametrize('amount,card', [('11.00', '回收款方名片'), ('3.00', '收款方名片')])
def test_real_shape_metadata_does_not_compete_with_the_title_payee(amount: str, card: str) -> None:
    rows = screen(amount, card)
    assert labelled_counterparty(rows) == ('示例广告店', TITLE, False)
    local = extract_local_payment(rows)
    assert local['fields']['merchant'] == '示例广告店'
    assert local['evidence']['merchant'] == TITLE
    assert local['fields']['amount'] == amount
    assert local['issues'] == []


@pytest.mark.parametrize('label', ['收款方备注 二维码收款', '收款方备注：二维码收款',
                                  '收款方备注 现场打印', '收款方服务', '收款方服务 >',
                                  '收款方名片', '回收款方名片', '收款方名片：↗'])
def test_payment_metadata_is_never_an_independent_payee(label: str) -> None:
    assert labelled_counterparty([label]) == ('', '', False)
    assert labelled_counterparty(['收款人：示例广告店', label]) == (
        '示例广告店', '收款人：示例广告店', False)


@pytest.mark.parametrize('title', [TITLE, '二维码付款-给示例广告店', '付款-给示例广告店',
                                  '转账-转给示例广告店', '给示例广告店的转账',
                                  '给示例广告店的付款', '给示例广告店的二维码付款'])
def test_explicit_payment_titles_keep_the_exact_original_quote(title: str) -> None:
    assert labelled_counterparty([title]) == ('示例广告店', title, False)


@pytest.mark.parametrize('rows', [
    ['收款方：示例广告店', '收款人：另一收款方'],
    [TITLE, '收款方：另一收款方'],
    [TITLE, '给另一收款方的付款'],
    ['商家名称：示例广告店', '商家名称：另一收款方'],
])
def test_genuine_recipient_conflicts_remain_blocked(rows: list[str]) -> None:
    assert labelled_counterparty(rows + ['收款方备注 二维码收款', '收款方服务']) == ('', '', True)
    local = extract_local_payment(['微信支付', '-11.00', '支付成功', *rows])
    assert 'merchant' not in local['fields']
    assert '收款身份待核对' in local['issues']


def test_official_full_merchant_identity_still_has_priority_over_nickname() -> None:
    assert labelled_counterparty([TITLE, '商户全称：示例文化传播有限公司']) == (
        '示例文化传播有限公司', '商户全称：示例文化传播有限公司', False)


@pytest.mark.parametrize('name', ['名片印刷有限公司', '服务广告有限公司', '示例收款人服务有限公司'])
def test_metadata_words_inside_an_explicit_merchant_name_are_not_controls(name: str) -> None:
    assert labelled_counterparty(['商户全称：' + name, '收款方服务']) == (
        name, '商户全称：' + name, False)


@pytest.mark.parametrize('rows', [['收款人', '收款方服务'], ['商户名称', '收款方名片'],
                                ['收款方备注', '二维码收款'], ['二维码收款']])
def test_empty_identity_and_metadata_do_not_promote_the_following_control(rows: list[str]) -> None:
    assert labelled_counterparty(rows) == ('', '', False)


@pytest.mark.parametrize('row', ['备注：给示例广告店的付款', '商品：给示例广告店的付款',
                                '对订单有疑惑 给示例广告店的付款'])
def test_payment_title_must_be_a_whole_title_not_embedded_prose(row: str) -> None:
    assert labelled_counterparty([row]) == ('', '', False)


@pytest.mark.parametrize('amount,card', [('11.00', '回收款方名片'), ('3.00', '收款方名片')])
def test_normal_local_intake_has_no_false_payee_conflict_for_payment_screen(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amount: str, card: str) -> None:
    rows = screen(amount, card)
    monkeypatch.setattr('invoice_print_layout.intake_queue.read_receipt', lambda *_: [
        OcrLine(text, 0, index * 40, 500, index * 40 + 25) for index, text in enumerate(rows)])
    payload = io.BytesIO()
    Image.new('RGB', (120, 240), 'white').save(payload, format='PNG')
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    entry = queue.receive('synthetic-payment.png', payload.getvalue())
    result = queue.prepare_batch_entry(entry['id'])
    assert result['role'] == 'payment'
    assert result['fields']['merchant'] == '示例广告店'
    assert result['fields']['amount'] == amount
    assert result['fields']['payment_status'] == 'succeeded'
    assert result['local_evidence']['merchant'] == TITLE
    assert '收款身份待核对' not in result['issues']
    assert labelled_counterparty(result['text'].splitlines()) == ('示例广告店', TITLE, False)
    assert queue.store.list_items() == []
