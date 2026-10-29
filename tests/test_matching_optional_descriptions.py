from __future__ import annotations

from typing import Any

import pytest

from tests.material_matching_helpers import confirmed, payment
from tests.test_material_matching import queued, revisions, setup, snapshots


HOTEL = '合成明珠公寓'
REMARK_LABELS = ('付款备注', '交易备注', '交易说明', '备注')


@pytest.mark.parametrize('label', REMARK_LABELS)
@pytest.mark.parametrize('separator', ['：', '\n'])
def test_source_payment_remark_supports_readonly_candidate_and_provisional_match(
        setup: Any, label: str, separator: str) -> None:
    matcher, project, _ = setup
    voucher = confirmed(matcher, queued(matcher, project, 'Synthetic hotel invoice', 'invoice', '50'), HOTEL)
    entry = payment(matcher, project, 'Synthetic platform payment', '50', name='携程旅行网',
                    text='支付成功\n' + label + separator + HOTEL + '住宿费')
    before = snapshots(matcher)
    result = matcher.suggest(project)
    assert snapshots(matcher) == before and result['suggested'] == 1
    candidate = result['suggestions'][0]
    assert candidate['voucher_keys'] == ['q:' + voucher['id']] and candidate['payment_ids'] == [entry['id']]
    assert not candidate['needs_name_review'] and not candidate['ambiguous']
    evidence = next(record for record in candidate['evidence'] if record['key'] == 'q:' + entry['id'])
    assert evidence['descriptions'] == [HOTEL + '住宿费']
    matched = matcher.auto_match(project)
    assert matched['matched'] == 1 and not matched['items'][0]['item']['verified']
    assert matched['items'][0]['item']['pending_review']


@pytest.mark.parametrize('optional_fields', ['absent', 'empty'])
@pytest.mark.parametrize('remark', ['', '\n付款备注：'])
def test_optional_goods_category_and_remark_do_not_block_established_seller(
        setup: Any, optional_fields: str, remark: str) -> None:
    matcher, project, _ = setup
    voucher = confirmed(matcher, queued(matcher, project, 'Synthetic plain invoice', 'invoice', '50'))
    entry = payment(matcher, project, 'Synthetic plain payment', '50')
    for record in (voucher, entry):
        for field in ('goods', 'category'):
            if optional_fields == 'empty':
                record['fields'][field] = ''
            else:
                record['fields'].pop(field, None)
        record['issues'] = ['商品不明']
        with matcher.store.connect() as db:
            matcher._write_queue(db, record)
    entry['text'] = '商家名称：合成印务有限公司\n支付成功' + remark
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    candidate = matcher.suggest(project)['suggestions'][0]
    assert not candidate['needs_name_review'] and not candidate['warnings']
    assert matcher.auto_match(project)['matched'] == 1


def test_generated_note_and_goods_do_not_establish_source_relationship(setup: Any) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Synthetic hotel invoice', 'invoice', '50'), HOTEL)
    entry = payment(matcher, project, 'Synthetic platform payment', '50', name='携程旅行网')
    entry['fields'].update(goods=HOTEL + '住宿费', note='付款备注：' + HOTEL + '住宿费')
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    before = snapshots(matcher)
    candidate = matcher.suggest(project)['suggestions'][0]
    assert candidate['needs_name_review']
    assert all(record['descriptions'] == [] for record in candidate['evidence'])
    assert matcher.auto_match(project)['matched'] == 0 and snapshots(matcher) == before


@pytest.mark.parametrize('label', REMARK_LABELS)
@pytest.mark.parametrize('prefix', ['不是', '另一家'])
def test_payment_remark_keeps_negative_and_longer_name_boundaries(
        setup: Any, label: str, prefix: str) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Synthetic hotel invoice', 'invoice', '50'), HOTEL)
    payment(matcher, project, 'Synthetic platform payment', '50', name='携程旅行网',
            text='支付成功\n' + label + '：' + prefix + HOTEL + '住宿费')
    candidate = matcher.suggest(project)['suggestions'][0]
    assert candidate['needs_name_review'] and matcher.auto_match(project)['matched'] == 0


def test_generic_invoice_remark_is_not_payment_relationship_evidence(setup: Any) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Synthetic invoice with note', 'invoice', '50',
                             text='备注：' + HOTEL + '住宿费'), '另一合成公寓')
    payment(matcher, project, 'Synthetic hotel payment', '50', name=HOTEL)
    candidate = matcher.suggest(project)['suggestions'][0]
    assert candidate['needs_name_review']
    assert all(record['descriptions'] == [] for record in candidate['evidence'])


def test_source_remark_does_not_hide_same_amount_competition(setup: Any) -> None:
    matcher, project, _ = setup
    confirmed(matcher, queued(matcher, project, 'Synthetic hotel invoice', 'invoice', '50'), HOTEL)
    for index in range(2):
        payment(matcher, project, f'Synthetic platform payment {index}', '50', name='携程旅行网',
                text='支付成功\n付款备注：' + HOTEL + '住宿费')
    result = matcher.suggest(project)
    assert result['suggested'] == 2
    assert all(candidate['ambiguous'] and not candidate['needs_name_review'] for candidate in result['suggestions'])
    assert matcher.auto_match(project)['matched'] == 0


@pytest.mark.parametrize('payment_project', ['other', 'unassigned'])
def test_source_remark_cannot_pair_across_projects(setup: Any, payment_project: str) -> None:
    matcher, project, other = setup
    confirmed(matcher, queued(matcher, project, 'Synthetic hotel invoice', 'invoice', '50'), HOTEL)
    payment(matcher, other if payment_project == 'other' else '', 'Synthetic foreign payment', '50',
            name='携程旅行网', text='支付成功\n付款备注：' + HOTEL + '住宿费')
    assert matcher.suggest(project)['suggested'] == 0 and not matcher.board('')['suggestions']
    assert matcher.auto_match(project)['matched'] == 0


@pytest.mark.parametrize('role', ['invoice', 'payment'])
def test_board_hides_only_legacy_optional_goods_issue_without_persisting(setup: Any, role: str) -> None:
    matcher, project, _ = setup
    entry = queued(matcher, project, 'Synthetic legacy issue', role, '50',
                   issues=['商品不明', '金额待确认'])
    before = snapshots(matcher)
    cards = matcher.board(project)['vouchers' if role == 'invoice' else 'payments']
    assert cards[0]['issues'] == ['金额待确认']
    with matcher.store.connect() as db:
        assert matcher._queue(db, entry['id'])['issues'] == ['商品不明', '金额待确认']
    assert snapshots(matcher) == before


def test_source_rail_invoice_needs_no_payment_when_optional_description_fields_absent(setup: Any) -> None:
    matcher, project, _ = setup
    invoice = queued(matcher, project, 'Synthetic source rail invoice', 'invoice', '25',
                     text='电子发票（铁路电子客票）')
    invoice['fields'].pop('category', None)
    with matcher.store.connect() as db:
        matcher._write_queue(db, invoice)
    payment(matcher, project, 'Synthetic unrelated payment', '25')
    board = matcher.board(project)
    assert board['vouchers'][0]['payment_not_required'] and not board['suggestions']
    result = matcher.create_expense(['q:' + invoice['id']], [], project, revisions=revisions(invoice),
                                    fields={'project_type': 'transport', 'project_name': '火车、飞机', 'amount': '25'})
    item = result['item']
    assert item['category'] == '高铁' and item['material_basis'] == 'standard'
    assert item['complete'] and item['pending_review'] and not item['verified']
    assert not item['missing'] and len(item['attachments']) == 1
