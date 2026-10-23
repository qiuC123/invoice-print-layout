from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_glm_material_matching import activate, confirmed, payment
from tests.test_material_matching import expense, queued, setup, snapshots


def hotel_group(matcher: Any, project: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    voucher = confirmed(matcher, queued(matcher, project, 'Synthetic hotel invoice', 'invoice', '1353'),
                        '合成测试注册宾馆')
    payments = [payment(matcher, project, f'Synthetic payment {i}', value,
                        name='合成平台旅行有限公司', text='支付成功\n商品：合成测试车站宾馆')
                for i, value in enumerate(['921', '124', '154', '154'])]
    return voucher, payments


def create_from(matcher: Any, suggestion: dict[str, Any], **extra: Any) -> Any:
    return matcher.create_expense(suggestion['voucher_keys'], suggestion['payment_ids'], suggestion['project_id'],
                                  revisions=suggestion['revisions'], expected_versions=suggestion['expected_versions'],
                                  fields={'project_type': '其他', 'project_name': '合成测试费用',
                                          'amount': f'{suggestion["amount_cents"] / 100:.2f}'},
                                  suggestion_id=suggestion['id'], suggestion_token=suggestion['snapshot_token'], **extra)


def test_hotel_one_to_four_scan_is_readonly_stable_and_never_calls_ai(setup: Any) -> None:
    matcher, project, _ = setup
    ai = activate(matcher)
    voucher, payments = hotel_group(matcher, project)
    originals = {entry['path']: Path(entry['path']).read_bytes() for entry in [voucher, *payments]}
    before = snapshots(matcher)
    first = matcher.suggest(project)
    second = matcher.suggest(project)
    assert first == second and snapshots(matcher) == before and not ai.calls
    assert first['matched'] == 0 and first['items'] == [] and first['requests'] == 0
    assert first['suggested'] == first['suggestion_count'] == 1
    suggestion = first['suggestions'][0]
    assert suggestion['voucher_keys'] == ['q:' + voucher['id']]
    assert set(suggestion['payment_ids']) == {entry['id'] for entry in payments}
    assert suggestion['amount_cents'] == 135300 and suggestion['needs_name_review'] and not suggestion['ambiguous']
    assert len(suggestion['evidence']) == 5 and '原件确认' in suggestion['reason']
    assert suggestion['matching_context']['shop'] == 'different'
    assert suggestion['revisions'] == {entry['id']: entry['revision'] for entry in [voucher, *payments]}
    assert all(Path(path).read_bytes() == content for path, content in originals.items())
    assert matcher.board(project)['suggestions'] == first['suggestions']


def test_candidate_confirmation_creates_only_unverified_pending_review(setup: Any) -> None:
    matcher, project, _ = setup
    hotel_group(matcher, project)
    suggestion = matcher.suggest(project)['suggestions'][0]
    result = create_from(matcher, suggestion)
    assert result['item']['pending_review'] and result['item']['review_requested']
    assert result['item']['amount_cents'] == 135300 and not result['item']['verified'] and not result['item']['ready']
    assert len(result['item']['attachments']) == 5 and len(matcher.store.list_items()) == 1
    assert not matcher.suggest(project)['suggestions']
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='候选或材料池已变化'):
        create_from(matcher, suggestion)
    assert snapshots(matcher) == before


def test_unknown_identity_remains_a_name_review_suggestion(setup: Any) -> None:
    matcher, project, _ = setup
    queued(matcher, project, 'Invoice unknown name', 'invoice', '50')
    payment(matcher, project, 'Payment unknown name', '50', name='')
    suggestion = matcher.suggest(project)['suggestions'][0]
    assert suggestion['needs_name_review'] and not suggestion['ambiguous'] and suggestion['warnings']


def test_complete_same_amount_competition_is_visible_even_for_different_merchants(setup: Any) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Invoice 50', 'invoice', '50'))
    direct = payment(matcher, project, 'Payment 50', '50')
    split = [payment(matcher, project, 'Payment 20', '20', name='合成另一家有限公司'),
             payment(matcher, project, 'Payment 30', '30', name='合成另一家有限公司')]
    result = matcher.suggest(project)
    assert result['suggested'] == 2
    assert all(candidate['ambiguous'] and candidate['competing_count'] == 1 for candidate in result['suggestions'])
    assert {frozenset(candidate['payment_ids']) for candidate in result['suggestions']} == {
        frozenset([direct['id']]), frozenset(entry['id'] for entry in split)}
    assert any(not candidate['needs_name_review'] for candidate in result['suggestions'])
    assert any(candidate['needs_name_review'] for candidate in result['suggestions'])


@pytest.mark.parametrize('status,text,issues', [
    ('refunded', '退款成功', []), ('succeeded', '支付成功\n退款金额50', []),
    ('', '', []), ('succeeded', '支付成功', ['金额冲突']),
])
def test_refund_unknown_status_or_core_conflicts_are_not_suggested(setup: Any, status: str, text: str,
                                                                 issues: list[str]) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Invoice invalid competitor', 'invoice', '50'))
    payment(matcher, project, 'Payment invalid competitor', '50', status=status, text=text, issues=issues)
    result = matcher.suggest(project)
    assert result['suggested'] == 0 and result['conflicts']


def test_blocked_amount_competitor_never_makes_remaining_candidate_appear_unique(setup: Any) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Invoice valid', 'invoice', '50'))
    valid = payment(matcher, project, 'Payment valid', '50')
    blocked = payment(matcher, project, 'Payment blocked', '50', issues=['金额冲突'])
    result = matcher.suggest(project)
    assert result['suggested'] == 1
    suggestion = result['suggestions'][0]
    assert suggestion['payment_ids'] == [valid['id']] and blocked['id'] not in suggestion['payment_ids']
    assert suggestion['ambiguous'] and suggestion['competing_count'] == 1
    assert any('核心信息' in warning for warning in suggestion['warnings'])


def test_projects_and_unassigned_materials_are_not_mixed(setup: Any) -> None:
    matcher, project, other = setup
    confirmed(matcher, queued(matcher, project, 'Invoice first project', 'invoice', '50'))
    payment(matcher, other, 'Payment other project', '50')
    payment(matcher, '', 'Payment unassigned', '50')
    assert matcher.suggest(project)['suggested'] == 0
    assert matcher.board('')['suggestions'] == []


def test_all_project_board_aggregates_each_project_independently_without_unassigned_materials(setup: Any) -> None:
    matcher, project, other = setup
    first = confirmed(matcher, queued(matcher, project, 'First project invoice', 'invoice', '50'))
    first_payment = payment(matcher, project, 'First project payment', '50')
    second = confirmed(matcher, queued(matcher, other, 'Second project invoice', 'invoice', '50'))
    second_payment = payment(matcher, other, 'Second project payment', '50')
    unassigned = payment(matcher, '', 'Unassigned payment', '50')
    before = snapshots(matcher)
    board = matcher.board('')
    assert board['suggestion_count'] == 2 and not board['suggestions_truncated']
    by_project = {candidate['project_id']: candidate for candidate in board['suggestions']}
    assert by_project[project]['voucher_keys'] == ['q:' + first['id']]
    assert by_project[project]['payment_ids'] == [first_payment['id']]
    assert by_project[other]['voucher_keys'] == ['q:' + second['id']]
    assert by_project[other]['payment_ids'] == [second_payment['id']]
    assert all(not candidate['ambiguous'] for candidate in board['suggestions'])
    assert all(unassigned['id'] not in candidate['payment_ids'] for candidate in board['suggestions'])
    assert snapshots(matcher) == before


def test_all_project_display_limit_retains_combined_total_and_project_conflicts(setup: Any) -> None:
    matcher, project, other = setup
    for pid, label in [(project, 'First'), (other, 'Second')]:
        for i in range(6):
            confirmed(matcher, queued(matcher, pid, f'{label} invoice {i}', 'invoice', '50'))
        for i in range(5):
            payment(matcher, pid, f'{label} payment {i}', '25')
    blocked = payment(matcher, other, 'Second blocked payment', '50', issues=['金额冲突'])
    board = matcher.board('')
    assert board['suggestion_count'] == 120 and len(board['suggestions']) == 100
    assert board['suggestions_truncated']
    assert all(blocked['id'] not in candidate['payment_ids'] for candidate in board['suggestions'])
    assert any(conflict['project_id'] == other and '金额冲突' in conflict['reason']
               for conflict in board['suggestion_conflicts'])
    assert any(conflict['project_id'] == '' and '120个候选' in conflict['reason']
               for conflict in board['suggestion_conflicts'])


def test_changed_candidate_revision_or_new_pool_material_rejects_stale_confirmation(setup: Any) -> None:
    matcher, project, _ = setup
    voucher, _ = hotel_group(matcher, project)
    original = matcher.suggest(project)['suggestions'][0]
    voucher['revision'] += 1
    with matcher.store.connect() as db:
        matcher._write_queue(db, voucher)
    revised = matcher.suggest(project)['suggestions'][0]
    assert revised['id'] == original['id'] and revised['snapshot_token'] != original['snapshot_token']
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='候选或材料池已变化'):
        create_from(matcher, original)
    assert snapshots(matcher) == before
    payment(matcher, project, 'New independent material', '99')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='候选或材料池已变化'):
        create_from(matcher, revised)
    assert snapshots(matcher) == before


def test_new_competitor_after_confirmation_read_is_rechecked_in_transaction(setup: Any, monkeypatch: Any) -> None:
    matcher, project, _ = setup
    hotel_group(matcher, project)
    board = matcher.board(project)
    suggestion = board['suggestions'][0]
    payment(matcher, project, 'Late competing whole payment', '1353')
    monkeypatch.setattr(matcher, 'board', lambda project_id: board)
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='材料池已变化'):
        create_from(matcher, suggestion)
    assert snapshots(matcher) == before


def test_existing_expense_versions_are_included_and_verified(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Existing invoice only', '50', merchant_name='合成印务有限公司')
    payment(matcher, project, 'Existing bill payment', '50')
    suggestion = matcher.suggest(project)['suggestions'][0]
    assert suggestion['expected_versions'] == {item['id']: item['version']}
    result = create_from(matcher, suggestion)
    assert not result['created'] and result['item']['id'] == item['id'] and result['item']['pending_review']


def test_missing_original_cannot_be_suggested(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = confirmed(matcher, queued(matcher, project, 'Missing invoice original', 'invoice', '50'))
    payment(matcher, project, 'Payment for missing invoice', '50')
    Path(voucher['path']).unlink()
    result = matcher.suggest(project)
    assert not result['suggestions'] and any('原始材料不可用' in issue['reason'] for issue in result['conflicts'])


def test_display_limit_keeps_total_competition_and_never_claims_uniqueness(setup: Any) -> None:
    matcher, project, _ = setup
    for i in range(11):
        confirmed(matcher, queued(matcher, project, f'Invoice competing {i}', 'invoice', '50'))
    for i in range(5):
        payment(matcher, project, f'Payment competing {i}', '25')
    result = matcher.suggest(project)
    assert result['suggestion_count'] == 110 and len(result['suggestions']) == 100
    assert result['suggestions_truncated'] and all(candidate['ambiguous'] for candidate in result['suggestions'])
    assert any('仅展示前100个' in conflict['reason'] for conflict in result['conflicts'])
