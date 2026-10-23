from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.intake_usage import RAIL_PAYMENT_UNUSED_REASON
from tests.test_glm_material_matching import confirmed, payment
from tests.test_material_matching import queued, revisions, setup, snapshots


def rail_payment(matcher: Any, project: str, title: str = 'Synthetic rail payment', amount: str = '25',
                 **extra: Any) -> dict[str, Any]:
    queue_status = extra.pop('status', 'review')
    entry = payment(matcher, project, title, amount, name='中国铁路',
                    text='当前状态：支付成功\n商品：铁路客运服务', **extra)
    entry['status'] = queue_status
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def rail_invoice(matcher: Any, project: str, *, title: str = 'Synthetic rail invoice',
                 category: str = '高铁', amount: str = '25') -> dict[str, Any]:
    entry = queued(matcher, project, title, 'invoice', amount, text='电子发票（铁路电子客票）')
    entry['fields'].update(category=category, merchant='中国铁路')
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def create_one(matcher: Any, project: str, voucher: dict[str, Any], project_type: str = '车费',
               project_name: str = '火车、飞机') -> dict[str, Any]:
    return matcher.create_expense(['q:' + voucher['id']], [], project, revisions=revisions(voucher),
                                  fields={'project_type': project_type, 'project_name': project_name,
                                          'amount': voucher['fields']['amount']})


@pytest.mark.parametrize('status', ['review', 'received', 'unused'])
def test_source_backed_rail_payment_is_readonly_unused_and_keeps_original(setup: Any, status: str) -> None:
    matcher, project, _ = setup
    entry = rail_payment(matcher, project, status=status)
    original = Path(entry['path']).read_bytes()
    before = snapshots(matcher)
    board = matcher.board(project)
    assert {card['id'] for card in board['unused']} == {entry['id']}
    assert board['unused'][0]['unused_reason'] == RAIL_PAYMENT_UNUSED_REASON
    assert not board['payments'] and not board['other'] and not board['suggestions']
    assert snapshots(matcher) == before and Path(entry['path']).read_bytes() == original
    assert matcher.intake.get(entry['id'])['status'] == status
    assert matcher.board('')['unused'][0]['id'] == entry['id']


@pytest.mark.parametrize('operation', ['create', 'match'])
def test_legacy_review_rail_payment_cannot_bypass_with_direct_creation_or_matching(setup: Any, operation: str) -> None:
    matcher, project, _ = setup
    entry = rail_payment(matcher, project)
    voucher = queued(matcher, project, 'Synthetic non-rail invoice', 'invoice', '25')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='高铁报销只需12306发票'):
        if operation == 'create':
            matcher.create_expense(['q:' + voucher['id']], [entry['id']], project, revisions=revisions(voucher, entry),
                                   fields={'project_type': '其他', 'project_name': 'Synthetic expense', 'amount': '25'})
        else:
            matcher.match('q:' + voucher['id'], [entry['id']], project, revisions=revisions(voucher, entry))
    assert snapshots(matcher) == before and not matcher.store.list_items()


def test_no_source_or_rail_filenames_and_logistics_do_not_exclude_payments(setup: Any) -> None:
    matcher, project, _ = setup
    filename = payment(matcher, project, '12306 rail ticket filename only', '25', name='中国铁路', text='支付成功')
    logistics = payment(matcher, project, 'Synthetic freight payment', '40', name='中铁物流有限公司',
                        text='支付成功\n商品：铁路货运物流运费')
    before = snapshots(matcher)
    board = matcher.board(project)
    assert not board['unused'] and {card['id'] for card in board['payments']} == {filename['id'], logistics['id']}
    assert snapshots(matcher) == before


@pytest.mark.parametrize('category', ['高铁', '其他'])
def test_rail_invoice_stays_usable_alone_and_never_seeks_unrelated_payment(setup: Any, category: str) -> None:
    matcher, project, _ = setup
    invoice = rail_invoice(matcher, project, category=category)
    foreign = payment(matcher, project, 'Unrelated same amount payment', '25')
    unused = rail_payment(matcher, project)
    board = matcher.board(project)
    assert {card['id'] for card in board['vouchers']} == {invoice['id']}
    assert board['vouchers'][0]['payment_not_required']
    assert {card['id'] for card in board['payments']} == {foreign['id']}
    assert {card['id'] for card in board['unused']} == {unused['id']}
    assert not board['suggestions'] and matcher.suggest(project)['suggested'] == 0
    assert matcher.auto_match(project)['matched'] == 0
    result = create_one(matcher, project, invoice)
    item = result['item']
    assert item['category'] == '高铁' and item['material_basis'] == 'standard'
    assert item['complete'] and item['pending_review'] and not item['verified'] and not item['ready']
    assert not item['missing'] and '付款截图' not in item['missing']
    assert not result['match']['can_supplement']
    assert len(item['attachments']) == 1 and item['attachments'][0]['role'] == 'invoice'
    assert item['amount_cents'] == 2500


def test_rail_refund_fee_invoice_is_not_discarded(setup: Any) -> None:
    matcher, project, _ = setup
    invoice = rail_invoice(matcher, project, title='Synthetic rail refund fee invoice', amount='9.50')
    invoice['text'] += '\n退票费'
    with matcher.store.connect() as db:
        matcher._write_queue(db, invoice)
    board = matcher.board(project)
    assert not board['unused'] and board['vouchers'][0]['id'] == invoice['id']
    item = create_one(matcher, project, invoice)['item']
    assert item['complete'] and item['amount_cents'] == 950 and item['material_basis'] == 'standard'


@pytest.mark.parametrize('project_type,project_name', [('生活费', '住宿费(附清单)'),
                                                     ('生活费', '现场饮用水、夜宵'),
                                                     ('现场购材料', 'Synthetic supplies')])
def test_other_categories_keep_invoice_payment_requirements(setup: Any, project_type: str,
                                                            project_name: str) -> None:
    matcher, project, _ = setup
    invoice = queued(matcher, project, 'Synthetic hotel or meal invoice', 'invoice', '25')
    item = create_one(matcher, project, invoice, project_type, project_name)['item']
    assert item['material_basis'] == 'invoice_payment' and not item['complete']
    assert item['missing'] == ['付款截图'] and not item['verified']


def test_rail_exclusions_do_not_contend_or_stale_other_candidate_pool_tokens(setup: Any) -> None:
    matcher, project, _ = setup
    invoice = confirmed(matcher, queued(matcher, project, 'Synthetic ordinary invoice', 'invoice', '25'))
    regular = payment(matcher, project, 'Synthetic ordinary payment', '25')
    before = matcher.board(project)
    suggestion = before['suggestions'][0]
    excluded_payment = rail_payment(matcher, project)
    excluded_invoice = rail_invoice(matcher, project)
    after = matcher.board(project)
    assert after['suggestions'][0]['id'] == suggestion['id']
    assert after['suggestions'][0]['snapshot_token'] == suggestion['snapshot_token']
    assert not after['suggestions'][0]['ambiguous']
    with matcher.store.connect() as db:
        current = matcher._rules_current_pool_snapshot(db, project)
    assert current == matcher._rules_pool_snapshot(after, project)
    assert current == {'q:' + invoice['id']: invoice['revision'], 'q:' + regular['id']: regular['revision']}
    assert not {'q:' + excluded_payment['id'], 'q:' + excluded_invoice['id']}.intersection(current)
    result = matcher.create_expense(suggestion['voucher_keys'], suggestion['payment_ids'], project,
                                    revisions=suggestion['revisions'], expected_versions=suggestion['expected_versions'],
                                    suggestion_id=suggestion['id'], suggestion_token=suggestion['snapshot_token'],
                                    fields={'project_type': '生活费', 'project_name': '现场饮用水、夜宵', 'amount': '25'})
    assert result['item']['pending_review'] and result['item']['amount_cents'] == 2500


def test_linked_rail_proof_and_existing_attachments_are_untouched(setup: Any) -> None:
    matcher, project, _ = setup
    regular = payment(matcher, project, 'Payment before later legacy source correction', '25')
    invoice = queued(matcher, project, 'Invoice before legacy association', 'invoice', '25')
    result = matcher.match('q:' + invoice['id'], [regular['id']], project, revisions=revisions(invoice, regular))
    entry = matcher.intake.get(regular['id'])
    entry['text'] = '支付成功\n商品：铁路客运服务'
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    before = snapshots(matcher)
    board = matcher.board(project)
    assert not board['unused'] and not board['payments']
    assert len(board['matches']) == 1 and board['matches'][0]['item_id'] == result['item']['id']
    assert snapshots(matcher) == before
    assert len(matcher.store.get(result['item']['id'])['attachments']) == 2


def test_explicit_unused_nonrail_material_is_not_available_for_synthesis(setup: Any) -> None:
    matcher, project, _ = setup
    unused = queued(matcher, project, 'Synthetic deliberately unused material', 'other', '25',
                    status='unused', unused_reason='Synthetic user exclusion')
    board = matcher.board(project)
    assert board['unused'][0]['id'] == unused['id'] and board['unused'][0]['unused_reason'] == 'Synthetic user exclusion'
    assert not board['other']
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='材料已关联、需领票或不属于当前项目'):
        matcher.create_expense([], [], project, support_ids=[unused['id']], revisions=revisions(unused),
                               fields={'project_type': '其他', 'project_name': 'Synthetic unused expense', 'amount': '25'})
    assert snapshots(matcher) == before
