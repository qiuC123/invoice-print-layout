"""Synthetic regressions for correcting unrecognized registered originals."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
import pymupdf
import pytest

from invoice_print_layout import report_print_preview
from invoice_print_layout.expense_print_preview import create_expense_print_pdf
from invoice_print_layout.rail_print_layout import rail_pairable
from invoice_print_layout.report_excel import TEMPLATE_NAME
from tests.test_material_creation import confirmed_creation
from tests.test_material_matching import expense, proof, queued, revisions, setup, snapshots
from tests.test_rail_report_integration import native_ticket, rail
from tests.test_report_print_preview import fake_excel


def stored_group(matcher: Any, group_id: str) -> dict[str, Any]:
    with matcher.store.connect() as db:
        return json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()['data'])


def test_other_payment_correction_prints_invoice_and_image_on_one_page(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Synthetic invoice', 'invoice', '14')
    image = Image.new('RGB', (300, 500), 'white')
    ImageDraw.Draw(image).text((20, 30), 'SYNTHETIC PAYMENT 14.00', fill='black')
    payload = BytesIO()
    image.save(payload, format='JPEG')
    payment = matcher.intake.receive('synthetic-payment.jpg', payload.getvalue(), project_id=project)
    result = confirmed_creation(matcher, ['q:' + voucher['id']], [], project,
                                support_ids=[payment['id']], revisions=revisions(voucher, payment))
    item = result['item']
    attachment = next(a for a in item['attachments'] if a['role'] == 'other')
    original = Path(payment['path']).read_bytes()
    matcher.store.set_role(attachment['id'], 'payment')
    current = matcher.store.get(item['id'])
    assert current['complete'] and not current['verified']
    assert current['material_match']['support_attachment_ids'] == []
    assert current['material_match']['support_ids'] == []
    assert current['material_match']['support_count'] == 0
    assert current['material_match']['recognition']['supports'] == []
    group = stored_group(matcher, result['match']['id'])
    for field in ('support_attachment_ids', 'support_ids', 'support_count', 'recognition'):
        assert group[field] == current['material_match'][field]
    assert next(g for g in matcher.board(project)['matches'] if g['id'] == group['id'])['support_count'] == 0

    preview = create_expense_print_pdf(matcher.store, current['id'], current['version'])
    with pymupdf.open(matcher.store.root / '导出' / preview['pdf']) as document:
        assert len(document) == 1
        assert 'Synthetic invoice' in document[0].get_text()
        assert document[0].get_images()
    (matcher.store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(report_print_preview, 'create_excel', fake_excel([]))
    batch = report_print_preview.create_report_print_preview(matcher.store, [current['id']])
    with pymupdf.open(matcher.store.root / '导出' / batch['pdf']) as document:
        assert len(document) == 2  # One table, one invoice/payment page.
        assert 'Synthetic invoice' in document[1].get_text() and document[1].get_images()
    assert Path(payment['path']).read_bytes() == original
    assert matcher.intake.get(payment['id'])['item_id'] == current['id']
    assert any(event['message'].startswith('材料用途改为：') for event in current['events'])


def test_other_rail_invoice_correction_can_share_page_with_another_ticket(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    unknown = matcher.intake.receive('synthetic-rail.pdf', native_ticket('CORRECTED'), project_id=project)
    result = confirmed_creation(matcher, [], [], project, support_ids=[unknown['id']], revisions=revisions(unknown),
                                fields={'project_type': '车费', 'project_name': '火车、飞机'})
    matcher.store.set_role(result['item']['attachments'][0]['id'], 'invoice')
    current = matcher.store.get(result['item']['id'])
    assert current['complete'] and current['material_match']['support_attachment_ids'] == []
    assert current['material_match']['recognition']['supports'] == []
    assert rail_pairable(matcher.store, current)
    other = rail(matcher.store, {'id': project}, 'SECOND')
    (matcher.store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(report_print_preview, 'create_excel', fake_excel([]))
    batch = report_print_preview.create_report_print_preview(matcher.store, [current['id'], other['id']])
    with pymupdf.open(matcher.store.root / '导出' / batch['pdf']) as document:
        assert len(document) == 2
        assert 'TOP_CORRECTED' in document[1].get_text() and 'TOP_SECOND' in document[1].get_text()
        assert not document[1].get_images()


@pytest.mark.parametrize('role', ['invoice', 'receipt', 'payment'])
@pytest.mark.parametrize('mode', ['registration', 'single', 'multiple', 'supplement'])
def test_correction_removes_only_selected_support_and_preserves_undo_history(
        setup: Any, role: str, mode: str) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Retained invoice', 'invoice', '14')
    payment = queued(matcher, project, 'Retained payment', 'payment', '14')
    supports = [queued(matcher, project, title, 'other', None) for title in ('Corrected proof', 'Retained detail')]
    if mode == 'registration':
        # Existing member supports precede queue supports: their indices do not correspond.
        member = expense(matcher, project, 'Existing member')
        member = matcher.store.add_attachment(member['id'], 'member-detail.pdf', proof('MEMBER DETAIL'), 'detail')
        result = confirmed_creation(matcher, ['e:' + member['id']], [], project,
                                    expected_versions={member['id']: member['version']},
                                    support_ids=[e['id'] for e in supports], revisions=revisions(*supports))
    elif mode == 'multiple':
        another = queued(matcher, project, 'Second invoice', 'invoice', '1')
        payment['fields']['amount'] = '15'
        with matcher.store.connect() as db:
            matcher._write_queue(db, payment)
        result = matcher.match(['q:' + voucher['id'], 'q:' + another['id']], [payment['id']], project,
                               support_ids=[e['id'] for e in supports], revisions=revisions(voucher, another, payment, *supports))
    else:
        result = matcher.match('q:' + voucher['id'], [payment['id']], project,
                               support_ids=[] if mode == 'supplement' else [e['id'] for e in supports],
                               revisions=revisions(voucher, payment, *supports))
        if mode == 'supplement':
            result = matcher.supplement(result['match']['id'], [e['id'] for e in supports],
                                        expected_version=result['item']['version'], revisions=revisions(*supports))
    group_id = result['match']['id']
    before = stored_group(matcher, group_id)
    attachment = next(a for a in result['item']['attachments'] if a['name'] == supports[0]['name'])
    matcher.store.set_role(attachment['id'], role)
    current = matcher.store.get(result['item']['id'])
    after = stored_group(matcher, group_id)
    for record in (current['material_match'], after):
        assert attachment['id'] not in record['support_attachment_ids']
        assert record['support_ids'] == [supports[1]['id']]
        assert record['support_count'] == 1
        if 'recognition' in record:
            assert [r['queue_id'] for r in record['recognition']['supports']] == [supports[1]['id']]
    assert after['support_attachment_ids'] == [key for key in before['support_attachment_ids'] if key != attachment['id']]
    for field in ('attachment_ids', 'queue_before', 'queue_after', 'aggregate_after', 'member_before', 'moved_attachments'):
        if field in before:
            assert after[field] == before[field]
    # A role correction remains a manual edit; undo must not overwrite it.
    before_undo = snapshots(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(group_id, current['version'])
    assert snapshots(matcher) == before_undo
    for entry in supports:
        assert matcher.intake.get(entry['id'])['item_id'] == current['id']
        assert Path(entry['path']).is_file()


def test_receipt_correction_can_return_incomplete_expense_without_losing_original(setup: Any) -> None:
    matcher, project, _ = setup
    source = matcher.intake.receive('unrecognized-receipt.pdf', proof('SYNTHETIC RECEIPT'), project_id=project)
    result = confirmed_creation(matcher, [], [], project, support_ids=[source['id']], revisions=revisions(source))
    attachment = result['item']['attachments'][0]
    original = Path(source['path']).read_bytes()
    matcher.store.set_role(attachment['id'], 'receipt')
    current = matcher.store.get(result['item']['id'])
    assert current['missing'] == ['付款截图']
    returned = matcher.store.transition(current['id'], 'return_materials', expected_version=current['version'])
    assert not returned['pending_review'] and returned['attachments'] == current['attachments']
    assert returned['material_match']['support_ids'] == []
    group = stored_group(matcher, result['match']['id'])
    assert source['id'] in group['queue_before'] and attachment['id'] in group['attachment_ids']
    assert matcher.intake.get(source['id'])['item_id'] == returned['id']
    assert Path(source['path']).read_bytes() == original


def test_legacy_group_without_snapshot_keeps_role_edit_guard(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Legacy invoice', 'invoice', '14')
    payment = queued(matcher, project, 'Legacy payment', 'payment', '14')
    support = queued(matcher, project, 'Legacy support', 'other', None)
    result = matcher.match('q:' + voucher['id'], [payment['id']], project,
                           support_ids=[support['id']], revisions=revisions(voucher, payment, support))
    group = stored_group(matcher, result['match']['id'])
    group.pop('aggregate_after')
    group.pop('queue_after')
    with matcher.store.connect() as db:
        db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group), group['id']))
    attachment = next(a for a in result['item']['attachments'] if a['name'] == support['name'])
    matcher.store.set_role(attachment['id'], 'receipt')
    current = matcher.store.get(result['item']['id'])
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(group['id'], current['version'])
    assert snapshots(matcher) == before
    assert matcher.intake.get(support['id'])['item_id'] == current['id']
