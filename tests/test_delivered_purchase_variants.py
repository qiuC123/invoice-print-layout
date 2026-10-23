"""Delivery wording variants retain matching and unsettled-state boundaries."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_material_matching import merchant, queued, setup, snapshots
from tests.test_matching_suggestions import create_from


SHOP = '合成测试雨具便利店'


def delivered_pair(matcher: Any, project: str, heading: str, *, amount_line: str = '实付款￥28.70',
                   payment_status: str = '') -> tuple[dict[str, Any], dict[str, Any]]:
    voucher = queued(matcher, project, 'Synthetic delivered purchase', 'purchase', '28.70',
                     text='\n'.join([heading, '商家名称：' + SHOP, '一次性成人雨衣', amount_line]))
    voucher['fields'].update(merchant=SHOP, payment_status=payment_status)
    with matcher.store.connect() as db:
        matcher._write_queue(db, voucher)
    paid = merchant(matcher, queued(matcher, project, 'Synthetic paid receipt', 'payment', '28.70'), SHOP)
    return voucher, paid


@pytest.mark.parametrize('heading', [
    '已提前21分钟送达>', '已提前21分钟送达>\n已送至指定暂存点，请及时取货',
    '已提前1小时5分钟送达', '已延迟3分钟送达', '已送达',
    '已送至指定暂存点，请及时取货', '已送至您的收货地址。', '配送已完成', '包裹已签收',
])
@pytest.mark.parametrize('status', ['', 'unknown'])
def test_delivered_variants_produce_read_only_receipt_and_balanced_suggestion(
        setup: Any, heading: str, status: str) -> None:
    matcher, project, _ = setup
    voucher, paid = delivered_pair(matcher, project, heading, payment_status=status)
    originals = {row['path']: Path(row['path']).read_bytes() for row in (voucher, paid)}
    before = snapshots(matcher)
    board = matcher.board(project)
    assert len(board['vouchers']) == 1
    card = board['vouchers'][0]
    assert card['id'] == voucher['id'] and card['role'] == 'receipt' and card['source_role'] == 'purchase'
    assert card['amount_cents'] == 2870
    assert len(board['suggestions']) == 1
    suggestion = board['suggestions'][0]
    assert suggestion['voucher_keys'] == ['q:' + voucher['id']]
    assert suggestion['payment_ids'] == [paid['id']] and not suggestion['needs_name_review']
    assert snapshots(matcher) == before
    assert matcher.intake.get(voucher['id'])['role'] == 'purchase'
    assert all(Path(path).read_bytes() == payload for path, payload in originals.items())


def test_delivered_variant_confirmation_keeps_originals_and_creates_only_one_unverified_draft(setup: Any) -> None:
    matcher, project, _ = setup
    voucher, paid = delivered_pair(matcher, project, '已提前21分钟送达>\n已送至指定暂存点，请及时取货')
    originals = {row['path']: Path(row['path']).read_bytes() for row in (voucher, paid)}
    result = create_from(matcher, matcher.board(project)['suggestions'][0])
    item = result['item']
    assert item['amount_cents'] == 2870 and len(matcher.store.list_items()) == 1
    assert sorted(row['role'] for row in item['attachments']) == ['payment', 'receipt']
    assert not item['verified'] and not item['ready'] and item['stage'] == 'draft'
    assert matcher.intake.get(voucher['id'])['role'] == 'purchase'
    assert all(Path(path).read_bytes() == payload for path, payload in originals.items())


@pytest.mark.parametrize('heading', [
    '预计提前21分钟送达', '预计送达时间：12:00', '尚未送达', '配送中', '待配送', '待发货',
    '订单状态：待收货', '骑士正在配送', '待取货', '已送达\n已取消',
    '已提前21分钟送达\n退款成功', '已送至指定暂存点\n已申请退款',
    '已送达\n配送状态：待配送',
])
def test_delivery_promises_pending_and_refunded_orders_do_not_become_receipts(setup: Any, heading: str) -> None:
    matcher, project, _ = setup
    # Explicit stale success must not override a current negative state.
    status = '' if heading.startswith('预计') else 'succeeded'
    voucher, _ = delivered_pair(matcher, project, heading, payment_status=status)
    before = snapshots(matcher)
    board = matcher.board(project)
    assert not board['vouchers'] and not board['suggestions']
    assert next(row for row in board['other'] if row['id'] == voucher['id'])['role'] == 'purchase'
    assert snapshots(matcher) == before


@pytest.mark.parametrize('amount_line', ['商品价格￥28.70', '应付款￥28.70', ''])
def test_delivery_state_without_actual_paid_total_does_not_complete_purchase(setup: Any, amount_line: str) -> None:
    matcher, project, _ = setup
    delivered_pair(matcher, project, '已提前21分钟送达', amount_line=amount_line)
    before = snapshots(matcher)
    board = matcher.board(project)
    assert not board['vouchers'] and not board['suggestions']
    assert snapshots(matcher) == before
