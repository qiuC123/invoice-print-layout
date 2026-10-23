"""Pair by platform, shop and amount; order/payment times are not comparable."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout import material_matching as matching_module
from invoice_print_layout import matching_evidence as evidence_module
from tests.test_glm_material_matching import activate
from tests.test_material_matching import proof, setup, snapshots
from tests.test_matching_suggestions import create_from
from tests.test_platform_transaction_matching import material, save


SHOP = '京东便利店（合成湖店）'
OTHER_SHOP = '京东便利店（合成西门店）'
TIME_WARNING = re.compile(r'时间|间隔|相差.*秒|精确到分钟')


@pytest.mark.parametrize('payment_shop', [
    '京东便利店(合成湖店)', '京东 便利店 （ 合成湖店 ）', '闪购京东便利店（合成湖店）',
])
@pytest.mark.parametrize('legacy_issue', [
    'AI与原件字段冲突：merchant', 'AI与本地识别的商家名称不同，请对照原件确认。',
])
def test_same_shop_with_matching_local_ai_source_quotes_does_not_inherit_old_name_conflict(
        setup: Any, payment_shop: str, legacy_issue: str) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order', shop=SHOP)
    paid = material(matcher, project, 'payment', shop=payment_shop)
    order.update(issues=[legacy_issue], local_evidence={'merchant': SHOP}, ai_evidence={'merchant': SHOP},
                 field_sources={'merchant': 'local_ocr'})
    save(matcher, order)
    before = snapshots(matcher)
    board = matcher.board(project)
    assert snapshots(matcher) == before and len(board['suggestions']) == 1
    choice = board['suggestions'][0]
    assert choice['matching_context']['shop'] == 'matched' and not choice['needs_name_review']
    assert choice['voucher_keys'] == ['q:' + order['id']] and choice['payment_ids'] == [paid['id']]
    assert not any(re.search(r'名称.*不同|识别不一致|名称.*冲突', value) for value in choice['warnings'])
    card = next(value for value in board['vouchers'] if value['id'] == order['id'])
    assert not card.get('field_conflicts')
    assert not any('merchant' in value or re.search(r'商家名称.*不同|商家.*冲突', value)
                   for value in card['issues'])


def test_unproven_historical_marker_without_ai_source_quote_still_requires_confirmation(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order', shop=SHOP)
    material(matcher, project, 'payment', shop=SHOP)
    order['issues'] = ['AI与原件字段冲突：merchant']
    save(matcher, order)
    choice = matcher.suggest(project)['suggestions'][0]
    assert choice['needs_name_review'] and any('名称' in value for value in choice['warnings'])


def test_equal_saved_local_ai_names_with_source_quotes_filter_only_false_name_conflict(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order', shop=SHOP)
    material(matcher, project, 'payment', shop=SHOP)
    order.update(issues=['AI与原件字段冲突：merchant'],
                 local_evidence={'merchant': SHOP}, ai_evidence={'merchant': SHOP},
                 field_conflicts=[{'field': 'merchant', 'label': '商家名称', 'local_value': SHOP,
                                  'ai_value': '京东 便利店 ( 合成湖店 )', 'local_quote': SHOP,
                                  'ai_quote': SHOP, 'local_source': 'local_ocr'}])
    save(matcher, order)
    board = matcher.board(project)
    choice = board['suggestions'][0]
    assert not choice['needs_name_review']
    card = next(value for value in board['vouchers'] if value['id'] == order['id'])
    assert not card.get('field_conflicts')


def test_distinct_shop_branches_still_require_name_review_instead_of_collapsing_to_brand(setup: Any) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'order', shop=SHOP)
    material(matcher, project, 'payment', shop=OTHER_SHOP)
    choice = matcher.suggest(project)['suggestions'][0]
    assert choice['matching_context']['shop'] == 'different'
    assert choice['needs_name_review'] and choice['warnings']
    assert any(re.search(r'名称|店铺|原件', value) for value in choice['warnings'])


def test_real_saved_local_and_ai_shop_disagreement_is_kept_even_when_payment_matches_local_name(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order', shop=SHOP)
    material(matcher, project, 'payment', shop=SHOP)
    order.update(issues=['AI与原件字段冲突：merchant'], field_conflicts=[{
        'field': 'merchant', 'label': '商家名称', 'local_value': SHOP, 'ai_value': OTHER_SHOP,
        'local_quote': '店铺名称：' + SHOP, 'ai_quote': '商家名称：' + OTHER_SHOP, 'local_source': 'local_ocr',
    }])
    save(matcher, order)
    board = matcher.board(project)
    choice = board['suggestions'][0]
    assert choice['needs_name_review'] and any('名称' in value for value in choice['warnings'])
    card = next(value for value in board['vouchers'] if value['id'] == order['id'])
    assert card['field_conflicts'][0]['local_value'] == SHOP
    assert card['field_conflicts'][0]['ai_value'] == OTHER_SHOP
    assert any('商家名称' in value for value in card['issues'])


def without_time_rows(text: str) -> str:
    return '\n'.join(row for row in text.splitlines()
                     if not re.match(r'^(?:下单|支付|付款|交易|订单创建)时间', row))


@pytest.mark.parametrize('replacement', [
    '', '支付时间：2026-02-20 23:59:59',
    '支付时间：2026-01-05 16:23:44\n支付时间：2026-02-20 23:59:59',
])
def test_times_do_not_change_pairing_rank_reasons_or_confirmation_token(setup: Any, replacement: str) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'order', shop=SHOP)
    first = material(matcher, project, 'payment', name='Synthetic first equal amount payment', shop=SHOP)
    second = material(matcher, project, 'payment', name='Synthetic second equal amount payment', shop=SHOP,
                      number='2026010523001100000000000002')
    first['text'] = without_time_rows(first['text']) + '\n支付时间：2026-02-20 23:59:59'
    save(matcher, first)
    initial = matcher.suggest(project)['suggestions']
    assert len(initial) == 2 and all(choice['ambiguous'] for choice in initial)
    original_bytes = Path(second['path']).read_bytes()
    second['text'] = without_time_rows(second['text']) + ('\n' + replacement if replacement else '')
    # OCR transcript-only time changes do not revise a material or its original;
    # changing the material revision remains a separate concurrency guard.
    save(matcher, second)
    updated = matcher.suggest(project)['suggestions']
    assert [choice['id'] for choice in updated] == [choice['id'] for choice in initial]
    assert {choice['id']: choice['snapshot_token'] for choice in updated} == {
        choice['id']: choice['snapshot_token'] for choice in initial}
    for before, after in zip(initial, updated, strict=True):
        assert before['reason'] == after['reason'] and before['warnings'] == after['warnings']
        assert 'time' not in after['matching_context'] and 'time_gap_seconds' not in after['matching_context']
        assert all('transaction_time' not in row and 'time_source' not in row for row in after['evidence'])
        assert not TIME_WARNING.search(after['reason'])
        assert all(not TIME_WARNING.search(value) for value in after['warnings'])
        assert all(not TIME_WARNING.search(value) for value in after['matching_context']['checks'])
    assert Path(second['path']).read_bytes() == original_bytes


def test_matching_does_not_invoke_transaction_time_parser_or_llm(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    ai = activate(matcher)
    material(matcher, project, 'order', shop=SHOP)
    material(matcher, project, 'payment', shop=SHOP)

    def irrelevant_time_parser(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError('Order and payment timestamps must not be read for pairing')

    monkeypatch.setattr(matching_module, 'labelled_transaction_time', irrelevant_time_parser, raising=False)
    monkeypatch.setattr(evidence_module, 'labelled_transaction_time', irrelevant_time_parser, raising=False)
    before = snapshots(matcher)
    result = matcher.suggest(project)
    assert result['suggestion_count'] == 1 and result['requests'] == 0 and not ai.calls
    assert snapshots(matcher) == before


def test_ignoring_time_does_not_weaken_changed_original_confirmation_guard(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order', shop=SHOP)
    material(matcher, project, 'payment', shop=SHOP)
    choice = matcher.suggest(project)['suggestions'][0]
    Path(order['path']).write_bytes(proof('Synthetic original actually changed'))
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        create_from(matcher, choice)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('invalid', ['amount-conflict', 'refunded', 'other-project'])
def test_platform_and_same_shop_cannot_bypass_amount_refund_or_project_guards(setup: Any, invalid: str) -> None:
    matcher, project, other = setup
    material(matcher, project, 'order', shop=SHOP)
    paid = material(matcher, other if invalid == 'other-project' else project, 'payment', shop=SHOP)
    if invalid == 'amount-conflict':
        paid['issues'] = ['金额冲突']
        save(matcher, paid)
    elif invalid == 'refunded':
        paid['text'] += '\n退款成功'
        paid['fields']['payment_status'] = 'refunded'
        save(matcher, paid)
    before = snapshots(matcher)
    assert not matcher.suggest(project)['suggestions']
    assert snapshots(matcher) == before
