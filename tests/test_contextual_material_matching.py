"""Context supports choices; it never proves a transaction or changes the ledger."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_matching_suggestions import create_from
from tests.test_material_matching import proof, queued, setup, snapshots


SHOP = '京东便利店（合成湖店）'
OTHER_SHOP = '合成阳光便利店'


def save(matcher: Any, entry: dict[str, Any]) -> dict[str, Any]:
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def purchase(matcher: Any, project: str, *, name: str = 'Synthetic purchase', shop: str = SHOP,
             time: str = '2026-09-30 16:23:43.650', order: str = '4006506215532850730',
             amount: str = '17') -> dict[str, Any]:
    text = '\n'.join(['订单已送达', '实付款：￥' + amount,
                     *(['商家名称：' + shop] if shop else []),
                     *(['下单时间：' + time] if time else []),
                     *(['订单号：' + order] if order else [])])
    entry = queued(matcher, project, name, 'order', amount, text=text)
    entry['fields'].update(merchant=shop, payment_status='succeeded', expense_date='2026-09-30')
    if order:
        entry['fields']['order_number'] = order
    return save(matcher, entry)


def payment(matcher: Any, project: str, *, name: str = 'Synthetic payment', shop: str = SHOP,
            time: str = '2026-09-30 16:23:47', order: str = '2026093023001106371443018062',
            amount: str = '17') -> dict[str, Any]:
    text = '\n'.join(['淘宝闪购', '交易成功', '实付金额：￥' + amount,
                     *(['商品说明：' + shop] if shop else []),
                     *(['支付时间：' + time] if time else []),
                     *(['订单号：' + order] if order else [])])
    entry = queued(matcher, project, name, 'payment', amount, text=text)
    entry['fields'].update(merchant='淘宝闪购', payment_status='succeeded', expense_date='2026-09-30')
    if order:
        entry['fields']['order_number'] = order
    return save(matcher, entry)


def candidate(matcher: Any, project: str) -> dict[str, Any]:
    result = matcher.suggest(project)
    assert result['matched'] == 0 and result['items'] == [] and result['requests'] == 0
    assert result['suggestion_count'] == 1
    return result['suggestions'][0]


def assert_no_matching_time(choice: dict[str, Any]) -> None:
    context = choice['matching_context']
    assert 'time' not in context and 'time_gap_seconds' not in context
    assert all('transaction_time' not in record and 'time_source' not in record for record in choice['evidence'])
    descriptions = [choice['reason'], *choice['warnings'], *context['checks']]
    assert not any(marker in value for value in descriptions for marker in ('时间', '分钟', '秒'))


@pytest.mark.parametrize('order,payment_order,expected', [
    ('4006506215532850730', '2026093023001106371443018062', 'different'),
    ('', '2026093023001106371443018062', 'missing'),
    ('4006506215532850730', '', 'missing'),
    ('', '', 'missing'),
])
def test_platform_and_payment_numbers_are_optional_context_not_a_matching_error(
        setup: Any, order: str, payment_order: str, expected: str) -> None:
    matcher, project, _ = setup
    voucher = purchase(matcher, project, order=order)
    paid = payment(matcher, project, order=payment_order)
    original_bytes = {entry['path']: Path(entry['path']).read_bytes() for entry in (voucher, paid)}
    before = snapshots(matcher)
    first = candidate(matcher, project)
    assert candidate(matcher, project) == first and snapshots(matcher) == before
    context = first['matching_context']
    assert context['shop'] == 'matched'
    assert_no_matching_time(first)
    assert context['order_numbers'] == expected and context['checks']
    assert not first['needs_name_review'] and not first['ambiguous']
    assert first['voucher_keys'] == ['q:' + voucher['id']] and first['payment_ids'] == [paid['id']]
    assert not any('订单号' in str(value) for value in first['warnings'])
    assert '店铺' in first['reason'] and '金额' in first['reason']
    assert len(first['evidence']) == 2
    assert all(Path(path).read_bytes() == content for path, content in original_bytes.items())


def test_shop_spacing_parentheses_and_platform_branded_branch_are_same_shop(setup: Any) -> None:
    matcher, project, _ = setup
    purchase(matcher, project, shop='京东便利店（合成湖店）')
    payment(matcher, project, shop='京东 便利店 ( 合成湖店 )')
    choice = candidate(matcher, project)
    assert choice['matching_context']['shop'] == 'matched' and not choice['needs_name_review']


def test_ocr_millisecond_timestamps_do_not_enter_matching_evidence(setup: Any) -> None:
    matcher, project, _ = setup
    purchase(matcher, project, time='2026-09-3016:23:43.650')
    payment(matcher, project, time='2026-09-3016:23:47')
    choice = candidate(matcher, project)
    assert choice['matching_context']['shop'] == 'matched'
    assert_no_matching_time(choice)


@pytest.mark.parametrize('left_time,right_time', [
    ('', '2026-09-30 16:23:47'), ('2026-09-30 16:23:43.650', ''), ('', ''),
    ('2026-09-30', '2026-09-30'),
])
def test_missing_or_date_only_timestamps_do_not_affect_matching(
        setup: Any, left_time: str, right_time: str) -> None:
    matcher, project, _ = setup
    purchase(matcher, project, time=left_time)
    payment(matcher, project, time=right_time)
    choice = candidate(matcher, project)
    assert choice['matching_context']['shop'] == 'matched'
    assert not choice['needs_name_review']
    assert_no_matching_time(choice)


def test_status_bar_clock_and_generated_fields_do_not_establish_transaction_time(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = purchase(matcher, project, time='')
    paid = payment(matcher, project, time='')
    for record in (voucher, paid):
        record['text'] = '16:23:47\n2026-09-30\n' + record['text']
        record['fields'].update(note='支付时间：2026-09-30 16:23:47', goods='2026-09-30 16:23:47')
        save(matcher, record)
    choice = candidate(matcher, project)
    assert choice['matching_context']['shop'] == 'matched'
    assert_no_matching_time(choice)


def test_same_amount_and_near_time_without_a_shop_stays_a_manual_identity_choice(setup: Any) -> None:
    matcher, project, _ = setup
    purchase(matcher, project, shop='')
    payment(matcher, project, shop='')
    choice = candidate(matcher, project)
    assert_no_matching_time(choice)
    assert choice['matching_context']['shop'] == 'unknown' and choice['needs_name_review']
    assert choice['warnings']


def test_same_amount_different_shop_and_distant_time_are_retained_with_review_context(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = purchase(matcher, project)
    good = payment(matcher, project, name='Synthetic close payment')
    other = payment(matcher, project, name='Synthetic distant payment', shop=OTHER_SHOP,
                    time='2026-10-02 09:00:00')
    before = snapshots(matcher)
    choices = matcher.suggest(project)['suggestions']
    assert len(choices) == 2 and snapshots(matcher) == before
    assert choices[0]['payment_ids'] == [good['id']], 'Supported shop sorts first independently of timestamps'
    distant = next(row for row in choices if row['payment_ids'] == [other['id']])
    assert distant['matching_context']['shop'] in {'different', 'unknown'}
    assert distant['needs_name_review']
    for choice in choices:
        assert_no_matching_time(choice)
    assert all(row['ambiguous'] and row['competing_count'] == 1 for row in choices)
    assert all(row['voucher_keys'] == ['q:' + voucher['id']] for row in choices)


def test_two_plausible_payments_require_explicit_choice_and_count_amount_once(setup: Any) -> None:
    matcher, project, _ = setup
    purchase(matcher, project)
    first = payment(matcher, project, name='Synthetic first payment')
    second = payment(matcher, project, name='Synthetic second payment', time='2026-09-30 16:24:00')
    before = snapshots(matcher)
    choices = matcher.suggest(project)['suggestions']
    assert len(choices) == 2 and snapshots(matcher) == before and not matcher.store.list_items()
    assert all(row['ambiguous'] and row['competing_count'] == 1 and row['warnings'] for row in choices)
    chosen = next(row for row in choices if row['payment_ids'] == [second['id']])
    result = create_from(matcher, chosen)
    assert result['item']['amount_cents'] == 1700 and len(matcher.store.list_items()) == 1
    assert result['item']['pending_review'] and not result['item']['verified'] and not result['item']['ready']
    assert matcher.intake.get(first['id'])['status'] == 'review'
    assert matcher.intake.get(second['id'])['status'] == 'linked'
    assert len(result['item']['attachments']) == 2


@pytest.mark.parametrize('change', ['time', 'description', 'new-competitor'])
def test_source_and_pool_changes_invalidate_only_when_relevant_to_matching(setup: Any, change: str) -> None:
    matcher, project, _ = setup
    purchase(matcher, project)
    paid = payment(matcher, project)
    original = candidate(matcher, project)
    if change == 'new-competitor':
        payment(matcher, project, name='Synthetic newly arrived payment', time='2026-09-30 16:23:50')
    else:
        # Shop corrections affect the explanation; timestamp text is retained
        # as source information but no longer participates in matching.
        paid['text'] = paid['text'].replace('16:23:47', '17:23:47') if change == 'time' else paid['text'].replace(SHOP, OTHER_SHOP)
        save(matcher, paid)
    before = snapshots(matcher)
    if change == 'time':
        unchanged = candidate(matcher, project)
        assert unchanged['matching_context'] == original['matching_context']
        assert unchanged['snapshot_token'] == original['snapshot_token']
        assert_no_matching_time(unchanged)
        assert snapshots(matcher) == before
        return
    with pytest.raises(ValueError, match='候选或材料池已变化'):
        create_from(matcher, original)
    assert snapshots(matcher) == before


def test_conflicting_options_cannot_be_confirmed_together(setup: Any) -> None:
    matcher, project, _ = setup
    purchase(matcher, project)
    payment(matcher, project, name='Synthetic one payment')
    payment(matcher, project, name='Synthetic two payment', time='2026-09-30 16:23:49')
    choices = matcher.suggest(project)['suggestions']
    groups = [{'suggestion_id': row['id'], 'snapshot_token': row['snapshot_token'],
               'voucher_keys': row['voucher_keys'], 'payment_ids': row['payment_ids'],
               'revisions': row['revisions'], 'expected_versions': row['expected_versions'],
               'fields': {'project_type': 'other', 'project_name': 'Synthetic chosen purchase', 'amount': '17'}}
              for row in choices]
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='共用材料'):
        matcher.create_batch(project, groups, 'synthetic-overlapping-context-options')
    assert snapshots(matcher) == before


@pytest.mark.parametrize('invalid', ['refund', 'amount-conflict', 'cross-project', 'tampered'])
def test_contextual_similarity_does_not_bypass_source_guards(setup: Any, invalid: str) -> None:
    matcher, project, other = setup
    purchase(matcher, project)
    paid = payment(matcher, other if invalid == 'cross-project' else project)
    if invalid == 'refund':
        paid['text'] += '\n退款成功'
        paid['fields']['payment_status'] = 'refunded'
        save(matcher, paid)
    elif invalid == 'amount-conflict':
        paid['issues'] = ['金额冲突']
        save(matcher, paid)
    elif invalid == 'tampered':
        Path(paid['path']).write_bytes(proof('Synthetic replacement original'))
    before = snapshots(matcher)
    assert not matcher.suggest(project)['suggestions']
    assert snapshots(matcher) == before


@pytest.mark.parametrize('with_values', [False, True])
def test_merchant_only_conflict_is_a_visible_manual_choice_without_overwriting_values(
        setup: Any, with_values: bool) -> None:
    matcher, project, _ = setup
    voucher = purchase(matcher, project)
    payment(matcher, project)
    voucher['issues'] = ['AI与原件字段冲突：merchant']
    if with_values:
        voucher['field_conflicts'] = [{'field': 'merchant', 'label': '商家名称', 'local_value': SHOP,
                                     'ai_value': OTHER_SHOP, 'local_quote': '商家名称：' + SHOP,
                                     'ai_quote': OTHER_SHOP, 'local_source': 'local_ocr'}]
    save(matcher, voucher)
    before = snapshots(matcher)
    choice = candidate(matcher, project)
    assert choice['needs_name_review'] and choice['warnings'] and snapshots(matcher) == before
    card = next(row for row in matcher.board(project)['vouchers'] if row['id'] == voucher['id'])
    assert card['fields']['merchant'] == SHOP
    if with_values:
        assert card['field_conflicts'][0]['local_value'] == SHOP
        assert card['field_conflicts'][0]['ai_value'] == OTHER_SHOP
    else:
        assert not card.get('field_conflicts'), 'Historical records must not invent an AI value'
    result = create_from(matcher, choice)
    assert result['item']['pending_review'] and not result['item']['verified']
