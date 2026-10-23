"""Manual card corrections override display identity without rewriting source IDs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.matching_evidence import merchant_identity
from invoice_print_layout.transaction_evidence import identifiers_for_record, record_identifier_keys
from invoice_print_layout.workbench_web import classify_intake
from tests.test_material_matching import setup
from tests.test_platform_transaction_matching import material


OLD_SHOP = '合成便利店（展馆店）'
NEW_SHOP = '合成更正便利店（北门店）'


def correct(matcher: Any, entry: dict[str, Any], *, role: str,
            fields: dict[str, str]) -> dict[str, Any]:
    """Exercise the same transaction and validation used by intake/classify."""
    return classify_intake(matcher.intake, {'id': entry['id'], 'revision': entry['revision'],
                                           'role': role, 'fields': fields})


@pytest.mark.parametrize('changed_role', ['order', 'payment'])
def test_manual_merchant_correction_does_not_display_or_match_stale_automatic_shop(
        setup: Any, changed_role: str) -> None:
    matcher, project, _ = setup
    original = material(matcher, project, changed_role, name='Synthetic manually corrected identity')
    counterpart_role = 'payment' if changed_role == 'order' else 'order'
    material(matcher, project, counterpart_role, name='Synthetic old-shop counterpart')
    original_keys = record_identifier_keys(original)
    original_bytes = Path(original['path']).read_bytes()
    updated = correct(matcher, original, role='receipt' if changed_role == 'order' else 'payment',
                      fields={'merchant': NEW_SHOP})
    board = matcher.board(project)
    card = next(row for row in [*board['vouchers'], *board['payments']] if row['id'] == original['id'])
    assert card['fields']['merchant'] == NEW_SHOP
    assert merchant_identity(str(card['fields'].get('shop_name') or '')) != merchant_identity(OLD_SHOP)
    choice = board['suggestions'][0]
    assert choice['matching_context']['shop'] != 'matched', 'The stale original shop cannot override a human merchant correction'
    assert choice['needs_name_review']
    assert 'merchant' in updated['manual_fields']
    assert record_identifier_keys(matcher.intake.get(original['id'])) == original_keys
    assert Path(original['path']).read_bytes() == original_bytes
    assert not matcher.store.list_items(), 'Correcting a card and reading candidates are not bookkeeping'


@pytest.mark.parametrize('original_role', ['order', 'payment'])
@pytest.mark.parametrize('saved_metadata', [False, True])
def test_repeated_role_corrections_preserve_original_order_namespace_including_legacy_records(
        setup: Any, original_role: str, saved_metadata: bool) -> None:
    matcher, project, _ = setup
    original = material(matcher, project, original_role, metadata=saved_metadata)
    keys_before = record_identifier_keys(original)
    expected_kind = 'shopping_order' if original_role == 'order' else 'payment_order'
    assert {item['kind'] for item in identifiers_for_record(original)} == {expected_kind}
    first = correct(matcher, original, role='payment' if original_role == 'order' else 'receipt', fields={})
    assert first['transaction_source_role'] == original_role
    assert record_identifier_keys(first) == keys_before
    assert {item['kind'] for item in identifiers_for_record(first)} == {expected_kind}
    second = correct(matcher, first, role='receipt' if original_role == 'order' else 'payment', fields={'amount': '18'})
    assert second['transaction_source_role'] == original_role
    assert record_identifier_keys(second) == keys_before
    assert {item['kind'] for item in identifiers_for_record(second)} == {expected_kind}
    assert matcher.intake.get(original['id'])['transaction_source_role'] == original_role
    assert not matcher.store.list_items()


def test_amount_only_manual_correction_keeps_evidenced_shop_and_source_identifiers(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    keys_before = record_identifier_keys(order)
    updated = correct(matcher, order, role='receipt', fields={'amount': '18'})
    card = next(row for row in matcher.board(project)['vouchers'] if row['id'] == order['id'])
    assert card['amount_cents'] == 1800
    assert merchant_identity(card['fields']['shop_name']) == merchant_identity(OLD_SHOP)
    assert card['fields']['shopping_platform'] == '淘宝闪购'
    assert record_identifier_keys(updated) == keys_before
    assert updated['manual_fields'] == ['amount']


def test_explicit_manual_shop_is_protected_when_merchant_is_corrected_again(setup: Any) -> None:
    matcher, project, _ = setup
    order = material(matcher, project, 'order')
    keys_before = record_identifier_keys(order)
    manual_shop = '合成人工确认便利店（东门店）'
    first = correct(matcher, order, role='receipt', fields={'shop_name': manual_shop})
    second = correct(matcher, first, role='receipt', fields={'merchant': NEW_SHOP})
    card = next(row for row in matcher.board(project)['vouchers'] if row['id'] == order['id'])
    assert card['fields']['merchant'] == NEW_SHOP
    assert merchant_identity(card['fields']['shop_name']) == merchant_identity(manual_shop)
    assert {'merchant', 'shop_name'} <= set(second['manual_fields'])
    assert record_identifier_keys(second) == keys_before
