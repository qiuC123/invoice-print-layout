"""Questions retain context of proofs already linked by the current task."""
from __future__ import annotations

from typing import Any

from invoice_print_layout.agent_combinations import missing_questions


def row(key: str, role: str, *, item_id: str = '', merchant: str = '', goods: str = '',
        text: str = '') -> dict[str, Any]:
    return {'id': key, 'role': role, 'item_id': item_id,
            'fields': {'merchant': merchant, 'goods': goods}, 'text': text}


def test_payment_question_keeps_already_linked_invoice_context() -> None:
    invoice = row('invoice', 'invoice', item_id='draft', merchant='合成甲区测试旅馆')
    payment = row('payment', 'payment', merchant='合成旅行代理有限公司', goods='合成测试站旅馆')
    question = missing_questions([payment], context=[invoice, payment])[0]
    assert question['material_id'] == payment['id']
    assert '已收到本任务的发票' in question['reason'] and '对应关系' in question['reason']
    assert '合成甲区测试旅馆' in question['reason'] and '合成测试站旅馆' in question['reason']
    assert '无需重复上传已有发票' in question['reason']
    assert '只有付款记录' not in question['reason'] and '还缺对应正式发票' not in question['reason']


def test_multiple_existing_invoices_question_still_targets_only_unresolved_payment() -> None:
    invoices = [row(str(i), 'invoice', item_id='draft-' + str(i), merchant='合成商家' + str(i)) for i in range(2)]
    payment = row('payment', 'payment', merchant='合成收款方')
    questions = missing_questions([payment], context=[*invoices, payment])
    assert len(questions) == 1 and questions[0]['material_id'] == 'payment'
    assert '合成商家0' in questions[0]['reason'] and '合成商家1' in questions[0]['reason']


def test_partial_order_price_only_question_names_missing_goods_and_shop() -> None:
    fragment = row('fragment', 'other', text='价格明细\n总优惠￥3 实付款￥17\n总价￥20\n收货地址：合成测试位置')
    payment = row('payment', 'payment', merchant='合成测试商店', goods='合成测试商店')
    questions = missing_questions([fragment, payment])
    assert {q['material_id'] for q in questions} == {'fragment', 'payment'}
    for question in questions:
        assert '商品名称及店铺' in question['reason'] and '完整购买截图' in question['reason']
        assert '已有付款记录无需重复上传' in question['reason']


def test_payment_without_any_invoice_still_requests_real_purchase_evidence() -> None:
    payment = row('payment', 'payment', merchant='合成收款方')
    question = missing_questions([payment])[0]
    assert '尚缺购买内容' in question['reason'] and '请补充材料和具体用途' in question['reason']
    assert '已收到本任务的发票' not in question['reason']
