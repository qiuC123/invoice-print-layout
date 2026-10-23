"""Completed platform purchase records are receipts without rewriting originals."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.expense_print_preview import create_expense_print_preview
from tests.test_material_matching import active_items, merchant, queued, revisions, setup, snapshots


def purchase(matcher: Any, project: str, role: str = 'order', amount: str = '17', *,
             text: str = '订单已完成\n实付款：￥17.00\n申请退款', **extra: Any) -> dict[str, Any]:
    entry = queued(matcher, project, 'PLATFORM RECEIPT ' + role + ' ' + amount, role, amount,
                   text=text, **extra)
    entry['fields'].update(payment_status='succeeded', merchant='合成购买记录便利店')
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def rewrite(matcher: Any, entry: dict[str, Any], **changes: Any) -> dict[str, Any]:
    entry.update(changes)
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def card_for(matcher: Any, project: str, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    board = matcher.board(project)
    return next((section, card) for section in ('vouchers', 'payments', 'other')
                for card in board[section] if card['id'] == entry['id'])


def create(matcher: Any, project: str, entries: list[dict[str, Any]],
           payments: list[dict[str, Any]] | None = None, **kwargs: Any) -> dict[str, Any]:
    payments = payments or []
    return matcher.create_expense(['q:' + entry['id'] for entry in entries],
                                  [entry['id'] for entry in payments], project,
                                  revisions=revisions(*entries, *payments),
                                  fields={'project_type': 'other', 'project_name': 'Synthetic purchase',
                                          'amount': '17'}, **kwargs)


@pytest.mark.parametrize('role', ['order', 'purchase'])
@pytest.mark.parametrize('evidence', ['field', 'delivered', 'completed', 'transaction'])
def test_paid_purchase_is_read_only_receipt_even_without_order_number(setup: Any, role: str, evidence: str) -> None:
    matcher, project, _ = setup
    text = {'field': '平台购买明细\n实付款 ￥17.00', 'delivered': '订单已送达\n实付款：￥17.00',
            'completed': '订单已完成\n实付款：￥17.00', 'transaction': '交易完成\n实付款：￥17.00'}[evidence]
    entry = purchase(matcher, project, role, text=text)
    if evidence != 'field':
        entry['fields'].pop('payment_status')
        rewrite(matcher, entry)
    before = snapshots(matcher)
    section, card = card_for(matcher, project, entry)
    assert section == 'vouchers' and card['role'] == 'receipt' and card['source_role'] == role
    assert card['amount_cents'] == 1700 and not card['fields'].get('order_number')
    assert not card['fields'].get('invoice_number')
    assert card['url'].endswith(entry['id']) and card['revision'] == entry['revision']
    assert snapshots(matcher) == before
    assert matcher.intake.get(entry['id']) == entry


@pytest.mark.parametrize('changes,text,status', [
    ({'manual_role': True}, '订单已完成\n实付款 ￥17.00', 'succeeded'),
    ({'result': '人工补充材料信息，尚未关联费用'}, '订单已完成\n实付款 ￥17.00', 'succeeded'),
    ({}, '待付款\n实付款 ￥17.00', 'succeeded'),
    ({}, '未付款\n实付款 ￥17.00', 'succeeded'),
    ({}, '支付失败\n实付款 ￥17.00', 'succeeded'),
    ({}, '付款失败\n实付款 ￥17.00', 'succeeded'),
    ({}, '订单已完成\n退款成功\n实付款 ￥17.00', 'succeeded'),
    ({}, '订单已完成\n已退款 ￥17.00', 'refunded'),
    ({}, '订单已完成\n实付款 ￥17.00', 'unpaid'),
    ({}, '订单已完成\n实付款 ￥17.00', 'failed'),
    ({}, '订单已完成\n实付款 ￥17.00', 'cancelled'),
    ({}, '商品售价 ￥17.00', ''),
    ({}, '广告：订单完成享优惠\n商品金额 ￥17.00', ''),
    ({}, '订单已完成\n应付款 ￥17.00', ''),
    ({}, '实付款 ￥17.00', ''),
])
def test_unsettled_ambiguous_or_manually_assigned_records_keep_original_role(
        setup: Any, changes: dict[str, Any], text: str, status: str) -> None:
    matcher, project, _ = setup
    entry = purchase(matcher, project, 'purchase', text=text, **changes)
    entry['fields']['payment_status'] = status
    rewrite(matcher, entry)
    before = snapshots(matcher)
    section, card = card_for(matcher, project, entry)
    assert section == 'other' and card['role'] == 'purchase'
    assert snapshots(matcher) == before


@pytest.mark.parametrize('qr', ['manual', 'converted'])
def test_tax_qr_carriers_never_become_purchase_receipts(setup: Any, qr: str) -> None:
    matcher, project, _ = setup
    entry = purchase(matcher, project, qr_status=qr)
    before = snapshots(matcher)
    board = matcher.board(project)
    assert entry['id'] not in {row['id'] for row in board['vouchers']}
    if qr == 'manual':
        card = next(row for row in board['other'] if row['id'] == entry['id'])
        assert card['kind'] == 'qr_claim' and card['role'] == 'other'
    assert snapshots(matcher) == before


def test_refund_action_is_not_a_refund_and_merchant_conflict_requires_name_review(setup: Any) -> None:
    matcher, project, _ = setup
    entry = purchase(matcher, project, text='订单已完成\n实付款 ￥17.00\n申请退款',
                     issues=['AI与原件字段冲突：merchant'])
    payment = merchant(matcher, queued(matcher, project, 'CONFLICT PAYMENT', 'payment', '17'), '合成购买记录便利店')
    before = snapshots(matcher)
    section, card = card_for(matcher, project, entry)
    assert section == 'vouchers' and card['role'] == 'receipt'
    assert 'AI与本地识别的商家名称不同，请对照原件确认。' in card['issues']
    suggestions = matcher.suggest(project)['suggestions']
    assert len(suggestions) == 1 and suggestions[0]['needs_name_review']
    assert any('识别不一致' in warning for warning in suggestions[0]['warnings'])
    assert snapshots(matcher) == before
    result = create(matcher, project, [entry], [payment])
    assert result['item']['amount_cents'] == 1700 and not result['item']['verified']


@pytest.mark.parametrize('role', ['order', 'purchase'])
@pytest.mark.parametrize('with_payment', [False, True])
def test_registration_counts_purchase_once_uses_receipt_and_undo_restores_original_role(
        setup: Any, role: str, with_payment: bool) -> None:
    matcher, project, _ = setup
    voucher = purchase(matcher, '', role)
    payments = [queued(matcher, project, 'PAYMENT MARKER', 'payment', '17')] if with_payment else []
    originals = {entry['path']: Path(entry['path']).read_bytes() for entry in [voucher, *payments]}
    result = create(matcher, project, [voucher], payments)
    item = result['item']
    assert len(active_items(matcher)) == 1 and item['amount_cents'] == 1700
    assert result['match']['voucher_count'] == 1 and result['match']['support_count'] == 0
    assert result['match']['voucher_total_cents'] == 1700
    assert sorted(row['role'] for row in item['attachments']) == (['payment', 'receipt'] if with_payment else ['receipt'])
    assert not item.get('invoice_number') and not item.get('invoice_numbers')
    assert not item['verified'] and not item['ready'] and item['stage'] == 'draft'
    assert matcher.intake.get(voucher['id'])['role'] == role
    assert ('付款截图' in item['missing']) is not with_payment
    assert '发票或收据' not in item['missing']
    before_preview = snapshots(matcher)
    preview = create_expense_print_preview(matcher.store, item['id'], item['version'])
    assert preview['pdf'] and preview['preview_pages'] and preview['page_count'] >= 1
    with pymupdf.open(matcher.store.root / '导出' / preview['pdf']) as document:
        text = '\n'.join(page.get_text() for page in document)
        assert 'PLATFORM RECEIPT' in text
        if with_payment:
            assert 'PAYMENT MARKER' in text
    assert snapshots(matcher) == before_preview
    matcher.undo(result['match']['id'], item['version'])
    assert not active_items(matcher)
    restored = matcher.intake.get(voucher['id'])
    assert restored['role'] == role and restored['project_id'] == '' and not restored['item_id']
    assert restored['fields'] == voucher['fields'] and restored['text'] == voucher['text']
    assert card_for(matcher, project, restored)[0] == 'vouchers'
    assert all(Path(name).read_bytes() == payload for name, payload in originals.items())


@pytest.mark.parametrize('method', ['registration', 'matching'])
def test_two_purchase_receipts_and_one_payment_form_one_seventeen_cost_and_undo(setup: Any, method: str) -> None:
    matcher, project, _ = setup
    vouchers = [purchase(matcher, project, 'order', '10'), purchase(matcher, project, 'purchase', '7')]
    payment = queued(matcher, project, 'AGGREGATE PAYMENT', 'payment', '17')
    if method == 'registration':
        result = create(matcher, project, vouchers, [payment])
    else:
        result = matcher.match(['q:' + entry['id'] for entry in vouchers], [payment['id']], project,
                               revisions=revisions(*vouchers, payment))
    item = result['item']
    assert len(active_items(matcher)) == 1 and item['amount_cents'] == 1700
    assert [row['role'] for row in item['attachments']].count('receipt') == 2
    assert [row['role'] for row in item['attachments']].count('payment') == 1
    assert not item.get('invoice_number') and not item.get('invoice_numbers')
    matcher.undo(result['match']['id'], item['version'])
    assert not active_items(matcher)
    for entry in vouchers:
        restored = matcher.intake.get(entry['id'])
        assert restored['role'] == entry['role'] and not restored['item_id']
        assert card_for(matcher, project, restored)[0] == 'vouchers'


@pytest.mark.parametrize('role', ['order', 'purchase'])
def test_legacy_single_match_stores_receipt_and_undo_keeps_existing_receipt_cost(setup: Any, role: str) -> None:
    matcher, project, _ = setup
    voucher = purchase(matcher, project, role)
    payment = queued(matcher, project, 'LEGACY PAYMENT', 'payment', '17')
    result = matcher.match('q:' + voucher['id'], [payment['id']], project, revisions=revisions(voucher, payment))
    item = result['item']
    assert item['amount_cents'] == 1700 and len(active_items(matcher)) == 1
    assert sorted(row['role'] for row in item['attachments']) == ['payment', 'receipt']
    assert not item.get('invoice_number')
    matcher.undo(result['match']['id'], item['version'])
    kept = matcher.store.get(item['id'])
    assert kept['amount_cents'] == 1700 and [row['role'] for row in kept['attachments']] == ['receipt']
    assert len(active_items(matcher)) == 1 and not matcher.intake.get(payment['id'])['item_id']
    assert matcher.intake.get(voucher['id'])['role'] == role


@pytest.mark.parametrize('role', ['order', 'purchase'])
def test_suggestion_confirmation_uses_same_projected_pool_and_receipt_role(setup: Any, role: str) -> None:
    matcher, project, _ = setup
    voucher = merchant(matcher, purchase(matcher, project, role), '合成购买记录便利店')
    payment = merchant(matcher, queued(matcher, project, 'SUGGESTION PAYMENT', 'payment', '17'), '合成购买记录便利店')
    before = snapshots(matcher)
    board = matcher.board(project)
    candidate = next(row for row in board['suggestions'] if row['voucher_keys'] == ['q:' + voucher['id']])
    assert candidate['payment_ids'] == [payment['id']] and snapshots(matcher) == before
    result = create(matcher, project, [voucher], [payment], suggestion_id=candidate['id'],
                    suggestion_token=candidate['snapshot_token'])
    assert result['item']['amount_cents'] == 1700
    assert sorted(row['role'] for row in result['item']['attachments']) == ['payment', 'receipt']
    assert len(active_items(matcher)) == 1


@pytest.mark.parametrize('role', ['order', 'purchase'])
def test_legacy_support_selection_still_accepts_the_original_purchase_role(setup: Any, role: str) -> None:
    matcher, project, _ = setup
    entry = purchase(matcher, project, role)
    result = matcher.create_expense([], [], project, support_ids=[entry['id']], revisions=revisions(entry),
                                    fields={'project_type': 'other', 'project_name': 'Legacy selection', 'amount': '17'})
    assert result['item']['amount_cents'] == 1700 and len(active_items(matcher)) == 1
    assert [row['role'] for row in result['item']['attachments']] == ['receipt']
    assert matcher.intake.get(entry['id'])['role'] == role
    matcher.undo(result['match']['id'], result['item']['version'])
    assert not active_items(matcher) and card_for(matcher, project, matcher.intake.get(entry['id']))[0] == 'vouchers'


def test_projected_candidate_can_be_confirmed_in_a_retryable_batch(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = merchant(matcher, purchase(matcher, project), '合成购买记录便利店')
    merchant(matcher, queued(matcher, project, 'BATCH PAYMENT', 'payment', '17'), '合成购买记录便利店')
    candidate = next(row for row in matcher.board(project)['suggestions']
                     if row['voucher_keys'] == ['q:' + voucher['id']])
    rows = [{'suggestion_id': candidate['id'], 'snapshot_token': candidate['snapshot_token'],
             'fields': {'project_type': 'other', 'project_name': 'Batch purchase', 'amount': '17'}}]
    result = matcher.create_batch(project, rows, 'synthetic-platform-batch-001')
    assert result['created'] == 1 and result['new_expenses'] == 1 and not result['replayed']
    assert len(active_items(matcher)) == 1 and result['items'][0]['amount_cents'] == 1700
    assert sorted(row['role'] for row in result['items'][0]['attachments']) == ['payment', 'receipt']
    before = snapshots(matcher)
    replay = matcher.create_batch(project, rows, 'synthetic-platform-batch-001')
    assert replay['replayed'] and replay['items'][0]['id'] == result['items'][0]['id']
    assert snapshots(matcher) == before


@pytest.mark.parametrize('role', ['order', 'purchase'])
def test_supplemented_purchase_is_receipt_without_changing_cost_or_undo_source(setup: Any, role: str) -> None:
    matcher, project, _ = setup
    invoice = queued(matcher, project, 'SUPPLEMENT INVOICE', 'invoice', '17')
    payment = queued(matcher, project, 'SUPPLEMENT PAYMENT', 'payment', '17')
    result = create(matcher, project, [invoice], [payment])
    supplemental = purchase(matcher, '', role)
    original = Path(supplemental['path']).read_bytes()
    before_item = result['item']
    if role == 'purchase':
        # This legacy endpoint has always accepted order supports, not purchase.
        # Receipt projection must not enlarge the endpoint's permitted roles.
        before = snapshots(matcher)
        with pytest.raises(ValueError, match='材料用途不符'):
            matcher.supplement(result['match']['id'], [supplemental['id']],
                               expected_version=before_item['version'], revisions=revisions(supplemental))
        assert snapshots(matcher) == before
        assert matcher.intake.get(supplemental['id']) == supplemental
        assert card_for(matcher, project, supplemental)[0] == 'vouchers'
        assert Path(supplemental['path']).read_bytes() == original
        return
    amended = matcher.supplement(result['match']['id'], [supplemental['id']],
                                  expected_version=before_item['version'], revisions=revisions(supplemental))
    item = amended['item']
    assert len(active_items(matcher)) == 1 and item['amount_cents'] == before_item['amount_cents'] == 1700
    added = next(row for row in item['attachments'] if row['name'] == supplemental['name'])
    assert added['role'] == 'receipt' and added['id'] in amended['match']['support_attachment_ids']
    linked = matcher.intake.get(supplemental['id'])
    assert linked['role'] == role and linked['item_id'] == item['id'] and linked['status'] == 'linked'
    assert linked['fields'] == supplemental['fields'] and linked['text'] == supplemental['text']
    matcher.undo(result['match']['id'], item['version'])
    assert not active_items(matcher)
    restored = matcher.intake.get(supplemental['id'])
    assert restored['role'] == role and restored['project_id'] == '' and not restored['item_id']
    assert restored['fields'] == supplemental['fields'] and restored['text'] == supplemental['text']
    section, card = card_for(matcher, project, restored)
    assert section == 'vouchers' and card['role'] == 'receipt' and card['source_role'] == role
    assert Path(supplemental['path']).read_bytes() == original


@pytest.mark.parametrize('violation', ['stale', 'foreign', 'content', 'payment-role', 'duplicate'])
def test_projected_receipts_preserve_versions_projects_originals_and_role_guards(setup: Any, violation: str) -> None:
    matcher, project, other = setup
    voucher = purchase(matcher, other if violation == 'foreign' else project)
    payment = queued(matcher, project, 'GUARDED PAYMENT', 'payment', '17')
    revision = revisions(voucher, payment)
    if violation == 'stale':
        revision[voucher['id']] += 1
    elif violation == 'content':
        Path(voucher['path']).write_bytes(b'synthetic replaced original')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        if violation == 'payment-role':
            matcher.create_expense([], [voucher['id']], project, revisions=revision,
                                   fields={'project_type': 'other', 'project_name': 'Guard', 'amount': '17'})
        else:
            keys = ['q:' + voucher['id']] * (2 if violation == 'duplicate' else 1)
            matcher.create_expense(keys, [payment['id']], project, revisions=revision,
                                   fields={'project_type': 'other', 'project_name': 'Guard', 'amount': '17'})
    assert snapshots(matcher) == before


@pytest.mark.parametrize('violation', ['token', 'revision', 'membership', 'new-competitor'])
def test_stale_or_tampered_purchase_candidate_cannot_write(setup: Any, violation: str) -> None:
    matcher, project, _ = setup
    voucher = merchant(matcher, purchase(matcher, project), '合成购买记录便利店')
    payment = merchant(matcher, queued(matcher, project, 'CANDIDATE PAYMENT', 'payment', '17'), '合成购买记录便利店')
    candidate = copy.deepcopy(matcher.board(project)['suggestions'][0])
    if violation == 'revision':
        rewrite(matcher, voucher, revision=voucher['revision'] + 1)
    elif violation == 'new-competitor':
        merchant(matcher, queued(matcher, project, 'COMPETING PAYMENT', 'payment', '17'), '合成购买记录便利店')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.create_expense(candidate['voucher_keys'], [] if violation == 'membership' else candidate['payment_ids'],
                               project, revisions=candidate['revisions'],
                               fields={'project_type': 'other', 'project_name': 'Guard', 'amount': '17'},
                               suggestion_id=candidate['id'],
                               suggestion_token='tampered' if violation == 'token' else candidate['snapshot_token'])
    assert snapshots(matcher) == before
