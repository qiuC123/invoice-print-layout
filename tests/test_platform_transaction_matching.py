"""Integrated matching uses actor evidence while typed IDs protect accounting."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.transaction_evidence import extract_transaction_evidence, record_identifier_keys
from tests.test_glm_material_matching import activate
from tests.test_material_matching import active_items, proof, queued, revisions, setup, snapshots
from tests.test_matching_suggestions import create_from


SHOP = '合成便利店（展馆店）'
SHOP_NORMAL = '合成便利店(展馆店)'
ORDER = '4006123456789012345'
PAYMENT = '2026010523001100000000000001'


def save(matcher: Any, entry: dict[str, Any]) -> dict[str, Any]:
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def material(matcher: Any, project: str, role: str, *, name: str = '',
             platform: str = '淘宝闪购', channel: str = '支付宝', shop: str = SHOP,
             number: str = '', amount: str = '17', metadata: bool = True) -> dict[str, Any]:
    payment = role == 'payment'
    text = '\n'.join([
        *([platform] if platform else []), '交易成功' if payment else '订单已送达',
        '实付金额：￥' + amount, ('商品说明：' if payment else '店铺名称：') + shop,
        ('支付时间：2026-01-05 16:23:47' if payment else '下单时间：2026-01-05 16:23:43.650'),
        *(['支付方式：' + channel] if channel else []),
        *(['订单号：' + (number or (PAYMENT if payment else ORDER))] if number != '-' else []),
    ])
    entry = queued(matcher, project, name or ('Synthetic platform payment' if payment else 'Synthetic platform order'),
                   role, amount, text=text)
    entry['fields'].update(merchant=platform if payment else shop, payment_status='succeeded',
                            expense_date='2026-01-05')
    if metadata:
        observed = extract_transaction_evidence(text, role, entry['fields'])
        entry['fields'].update(observed['fields'])
        entry['transaction_identifiers'] = observed['identifiers']
        entry['transaction_evidence'] = observed['evidence']
    return save(matcher, entry)


def create(matcher: Any, project: str, vouchers: list[dict[str, Any]],
           payments: list[dict[str, Any]], *, amount: str = '17') -> dict[str, Any]:
    return matcher.create_expense(['q:' + row['id'] for row in vouchers], [row['id'] for row in payments], project,
                                  revisions=revisions(*vouchers, *payments),
                                  fields={'project_type': 'other', 'project_name': '合成平台购物', 'amount': amount})


@pytest.mark.parametrize('metadata', [False, True])
def test_board_separates_platform_shop_channel_and_typed_identifiers_from_sources(setup: Any, metadata: bool) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order', metadata=metadata)
    paid = material(matcher, project, 'payment', metadata=metadata)
    before = snapshots(matcher)
    board = matcher.board(project)
    assert snapshots(matcher) == before
    for card in [*board['vouchers'], *board['payments']]:
        assert card['fields']['shopping_platform'] == '淘宝闪购'
        assert card['fields']['shop_name'] == SHOP_NORMAL
        assert card['fields']['payment_channel'] == '支付宝'
        assert record_identifier_keys(card)
    order_card = next(row for row in board['vouchers'] if row['id'] == order['id'])
    pay_card = next(row for row in board['payments'] if row['id'] == paid['id'])
    assert order_card['transaction_identifiers'][0]['kind'] == 'shopping_order'
    assert pay_card['transaction_identifiers'][0]['kind'] == 'payment_order'
    assert order_card['transaction_identifiers'][0]['namespace'] == '淘宝闪购'
    assert pay_card['transaction_identifiers'][0]['namespace'] == '支付宝'


def test_platform_support_sorts_but_does_not_hide_other_equal_amount_competition(setup: Any) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'order')
    good = material(matcher, project, 'payment', name='Synthetic matching platform')
    other = material(matcher, project, 'payment', name='Synthetic competing platform', platform='美团外卖',
                     number='2026010523001100000000000002')
    before = snapshots(matcher)
    choices = matcher.suggest(project)['suggestions']
    assert snapshots(matcher) == before and len(choices) == 2
    assert choices[0]['payment_ids'] == [good['id']]
    assert choices[0]['matching_context']['platform'] == 'matched'
    alternative = next(row for row in choices if row['payment_ids'] == [other['id']])
    assert alternative['matching_context']['platform'] == 'different'
    assert all(row['ambiguous'] and row['competing_count'] == 1 for row in choices)
    assert all(row['matching_context']['shop'] == 'matched' for row in choices)
    assert all('time' not in row['matching_context'] and 'time_gap_seconds' not in row['matching_context']
               for row in choices)
    assert not any('时间' in row['reason'] for row in choices)


@pytest.mark.parametrize(('order_platform', 'paid_platform', 'channel'), [
    ('淘宝闪购', '淘宝闪购', ''), ('', '', '支付宝'), ('淘宝闪购', '', ''),
])
def test_missing_optional_platform_or_channel_keeps_rules_candidate_without_llm(
        setup: Any, order_platform: str, paid_platform: str, channel: str) -> None:
    matcher, project, _ = setup
    ai = activate(matcher)
    material(matcher, project, 'order', platform=order_platform, channel=channel, number='-')
    material(matcher, project, 'payment', platform=paid_platform, channel=channel, number='-')
    before = snapshots(matcher)
    result = matcher.suggest(project)
    assert result['suggestion_count'] == 1 and result['requests'] == 0 and not ai.calls
    assert snapshots(matcher) == before
    context = result['suggestions'][0]['matching_context']
    assert context['platform'] == ('matched' if order_platform and paid_platform else 'unknown')
    assert context['shop'] == 'matched'
    assert 'time' not in context and 'time_gap_seconds' not in context
    assert not any('时间' in check for check in context['checks'])


def test_order_number_differences_are_normal_context_and_all_actor_metadata_reaches_expense(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    originals = {entry['path']: Path(entry['path']).read_bytes() for entry in (order, paid)}
    suggestion = matcher.suggest(project)['suggestions'][0]
    assert not any('订单号' in warning for warning in suggestion['warnings'])
    result = create_from(matcher, suggestion)
    item = result['item']
    assert item['shopping_platform'] == '淘宝闪购' and item['shop_name'] == SHOP_NORMAL
    assert item['payment_channel'] == '支付宝'
    assert record_identifier_keys(item) == record_identifier_keys(order) | record_identifier_keys(paid)
    assert item['amount_cents'] == 1700 and item['pending_review'] and not item['verified'] and not item['ready']
    assert all(Path(name).read_bytes() == data for name, data in originals.items())


@pytest.mark.parametrize('method', ['create', 'legacy-match'])
def test_same_typed_identifier_cannot_create_a_second_expense_and_is_atomic(setup: Any, method: str) -> None:
    matcher, project, _ = setup
    original = material(matcher, project, 'order', name='Synthetic original order')
    first_paid = material(matcher, project, 'payment', name='Synthetic original payment')
    create(matcher, project, [original], [first_paid])
    duplicate = material(matcher, project, 'order', name='Synthetic second screenshot of same order')
    second_paid = material(matcher, project, 'payment', name='Synthetic second payment',
                           number='2026010523001100000000000002')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='交易编号|重复'):
        if method == 'create':
            create(matcher, project, [duplicate], [second_paid])
        else:
            matcher.match('q:' + duplicate['id'], [second_paid['id']], project,
                          revisions=revisions(duplicate, second_paid))
    assert snapshots(matcher) == before and len(active_items(matcher)) == 1


@pytest.mark.parametrize(('existing_kind', 'existing_namespace'), [
    ('payment_order', '支付宝'), ('shopping_order', '美团外卖'),
])
@pytest.mark.parametrize('method', ['create', 'legacy-match'])
def test_same_digits_across_identifier_types_or_namespaces_do_not_block_real_new_purchase(
        setup: Any, existing_kind: str, existing_namespace: str, method: str) -> None:
    matcher, project, _ = setup
    matcher.store.create({'title': 'Synthetic separate transaction', 'project_id': project, 'amount': '17',
                          'category': '其他', 'transaction_identifiers': [
                              {'kind': existing_kind, 'namespace': existing_namespace, 'value': ORDER,
                               'label': '订单号', 'quote': '合成来源订单号：' + ORDER}]})
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    if method == 'create':
        item = create(matcher, project, [order], [paid])['item']
    else:
        item = matcher.match('q:' + order['id'], [paid['id']], project,
                             revisions=revisions(order, paid))['item']
    assert item['amount_cents'] == 1700 and not item['verified'] and not item['ready']
    assert len(active_items(matcher)) == 2


@pytest.mark.parametrize('method', ['create', 'legacy-match'])
def test_one_group_can_share_one_business_id_across_order_and_payment_materials(setup: Any, method: str) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    paid['transaction_identifiers'].append(dict(order['transaction_identifiers'][0]))
    save(matcher, paid)
    result = (create(matcher, project, [order], [paid]) if method == 'create' else
              matcher.match('q:' + order['id'], [paid['id']], project, revisions=revisions(order, paid)))
    assert result['item']['amount_cents'] == 1700 and len(active_items(matcher)) == 1
    assert len(record_identifier_keys(result['item'])) == 2


@pytest.mark.parametrize('method', ['create', 'legacy-match'])
def test_two_payment_screenshots_with_same_payment_id_cannot_be_summed_twice(setup: Any, method: str) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    payments = [material(matcher, project, 'payment', amount='8.5', name='Synthetic repeated payment ' + str(index))
                for index in range(2)]
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='交易编号|重复'):
        if method == 'create':
            create(matcher, project, [order], payments)
        else:
            matcher.match('q:' + order['id'], [row['id'] for row in payments], project,
                          revisions=revisions(order, *payments))
    assert snapshots(matcher) == before and not matcher.store.list_items()


def test_undo_releases_cancelled_aggregate_identity_and_allows_originals_to_be_rebuilt(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    result = create(matcher, project, [order], [paid])
    matcher.undo(result['match']['id'], result['item']['version'])
    restored_order, restored_paid = matcher.intake.get(order['id']), matcher.intake.get(paid['id'])
    assert restored_order['status'] == restored_paid['status'] == 'review'
    rebuilt = create(matcher, project, [restored_order], [restored_paid])['item']
    assert rebuilt['id'] != result['item']['id'] and rebuilt['amount_cents'] == 1700
    assert len(active_items(matcher)) == 1
    assert record_identifier_keys(rebuilt) == record_identifier_keys(order) | record_identifier_keys(paid)


@pytest.mark.parametrize('guard', ['changed-original', 'same-original', 'same-invoice-number'])
def test_actor_context_and_optional_orders_do_not_weaken_original_or_tax_invoice_guards(setup: Any, guard: str) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    vouchers = [order]
    if guard == 'changed-original':
        Path(order['path']).write_bytes(proof('Synthetic modified original'))
    elif guard == 'same-original':
        paid.update(path=order['path'], digest=order['digest'])
        save(matcher, paid)
    else:
        duplicate = material(matcher, project, 'invoice', name='Synthetic same invoice number',
                             number='4006123456789012346')
        order['role'] = 'invoice'
        for entry in (order, duplicate):
            entry['fields']['invoice_number'] = '11112222333344445555'
            save(matcher, entry)
        vouchers.append(duplicate)
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        create(matcher, project, vouchers, [paid])
    assert snapshots(matcher) == before


def test_suggest_never_recommends_summing_two_screenshots_of_one_payment_identity(setup: Any) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'order')
    for index in range(2):
        material(matcher, project, 'payment', amount='8.5', name='Synthetic repeated half-payment ' + str(index))
    before = snapshots(matcher)
    result = matcher.suggest(project)
    assert result['suggestion_count'] == 0 and not result['suggestions']
    assert snapshots(matcher) == before and not matcher.store.list_items()


def test_suggest_keeps_individual_same_payment_screenshots_as_explicit_competing_choices(setup: Any) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'order', amount='8.5')
    payments = [material(matcher, project, 'payment', amount='8.5', name='Synthetic alternative screenshot ' + str(index))
                for index in range(2)]
    before = snapshots(matcher)
    choices = matcher.suggest(project)['suggestions']
    assert len(choices) == 2
    assert {tuple(choice['payment_ids']) for choice in choices} == {(row['id'],) for row in payments}
    assert all(choice['ambiguous'] and choice['competing_count'] == 1 for choice in choices)
    assert snapshots(matcher) == before


def supplementary_order(matcher: Any, project: str, number: str) -> dict[str, Any]:
    entry = queued(matcher, project, 'Synthetic supplementary order detail', 'detail', None,
                   text='淘宝闪购\n订单详情\n店铺名称：' + SHOP + '\n订单号：' + number)
    observed = extract_transaction_evidence(entry['text'], entry['role'], entry['fields'])
    entry['fields'].update(observed['fields'])
    entry['transaction_identifiers'] = observed['identifiers']
    entry['transaction_evidence'] = observed['evidence']
    return save(matcher, entry)


@pytest.mark.parametrize('number', [ORDER, '4006123456789012346'])
def test_supplement_preserves_typed_id_without_adding_amount_and_can_share_current_business_id(
        setup: Any, number: str) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    result = create(matcher, project, [order], [paid])
    support = supplementary_order(matcher, project, number)
    amended = matcher.supplement(result['match']['id'], [support['id']],
                                 expected_version=result['item']['version'], revisions=revisions(support))
    item = amended['item']
    assert item['amount_cents'] == 1700 and len(active_items(matcher)) == 1
    assert not item['verified'] and not item['ready']
    assert record_identifier_keys(item) == (record_identifier_keys(order) | record_identifier_keys(paid) |
                                            record_identifier_keys(support))
    assert len(item['attachments']) == 3
    assert matcher.intake.get(support['id'])['status'] == 'linked'


def test_supplement_cannot_occupy_another_expenses_typed_identity_and_failure_is_atomic(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    paid = material(matcher, project, 'payment')
    result = create(matcher, project, [order], [paid])
    support = supplementary_order(matcher, project, '4006123456789012346')
    matcher.store.create({'title': 'Synthetic other registered order', 'project_id': project, 'amount': '9',
                          'category': '其他', 'transaction_identifiers': support['transaction_identifiers']})
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='交易编号|重复'):
        matcher.supplement(result['match']['id'], [support['id']],
                           expected_version=result['item']['version'], revisions=revisions(support))
    assert snapshots(matcher) == before
    assert matcher.intake.get(support['id'])['status'] == 'review'
