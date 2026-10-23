"""Historical taxi proofs use the same validated layout across preview and export."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout.reimbursement import compose_matter, selected_attachments
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from tests.helpers import make_caocao_trip_pdf, make_invoice_pdf, make_trip_pdf, make_xiangdao_trip_pdf
from tests.test_expense_print_preview import proof, rendered_pages, unchanged_snapshot


def taxi(store: ExpenseStore, platform: str = 'didi', *, trip_amount: str = '123.45') -> dict[str, Any]:
    item = store.create({'title': 'Synthetic taxi', 'project': 'Synthetic project', 'category': '打车',
                         'amount': '123.45', 'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], 'invoice.pdf',
                         make_invoice_pdf(store.workspace / 'invoice.pdf').read_bytes(), 'invoice')
    if platform == 'tencent':
        with pymupdf.open() as document:
            page = document.new_page(width=595.28, height=841.89)
            for number, text in enumerate((
                '腾讯出行服务—打车——行程单', f'共1次行程；合计{trip_amount}元',
                '序号 上车时间 城市 起点 终点 金额', f'1 10-05 09:18 南昌 车站 会场 {trip_amount}',
                '行程信息仅作凭证参考，金额以实际订单为准。', '请保留对应电子发票并核对行程。',
            )):
                page.insert_text((60, 125 + number * 25), text, fontname='china-s', fontsize=10)
            page.draw_rect(pymupdf.Rect(55, 170, 545, 215), width=0.5)
            payload = document.tobytes()
    else:
        makers = {'didi': make_trip_pdf, 'caocao': make_caocao_trip_pdf, 'xiangdao': make_xiangdao_trip_pdf}
        payload = makers[platform](store.workspace / 'trip.pdf', amount=trip_amount).read_bytes()
    # Old ordinary material: source content, rather than its file name, proves its effective role.
    store.add_attachment(item['id'], 'supplement.pdf', payload, 'other')
    # An optional payment remains an original, without becoming required or printing twice.
    return store.add_attachment(item['id'], 'payment.pdf', proof('OPTIONAL PAYMENT'), 'payment')


@pytest.mark.parametrize('platform', ['didi', 'caocao', 'xiangdao', 'tencent'])
def test_historical_taxi_preview_company_composer_and_material_export_share_one_a4(
        tmp_path: Path, platform: str) -> None:
    store = ExpenseStore(tmp_path)
    item = taxi(store, platform)
    assert item['complete'] and item['missing'] == [] and not item['ready']
    assert {a['role'] for a in selected_attachments(item)} == {'trip', 'invoice'}
    before = unchanged_snapshot(store)
    folder = tmp_path / 'expected'
    folder.mkdir()
    expected, layout = compose_matter(store, item, folder)
    result = create_expense_print_preview(store, item['id'], item['version'])
    preview = store.root / '导出' / result['pdf']
    assert result['page_count'] == 1 and '上下拼版' in layout
    assert rendered_pages(preview) == rendered_pages(expected)
    with pymupdf.open(preview) as document:
        assert len(document) == 1
        text = document[0].get_text()
        assert 'ELECTRONIC INVOICE' in text and '123.45' in text
        assert 'OPTIONAL PAYMENT' not in text
        assert document[0].get_drawings()  # The PDF still contains vector table edges.
        if platform == 'tencent':
            assert '请保留对应电子发票并核对行程。' in text.replace(' ', '')
    assert unchanged_snapshot(store) == before
    with pytest.raises(ValueError, match='已核对'):
        export_items(store, [item['id']])
    verified = store.transition(item['id'], 'verify')  # Synthetic fixture only.
    before_export = unchanged_snapshot(store)
    exported = export_items(store, [verified['id']])
    assert rendered_pages(store.root / '导出' / exported['pdf']) == rendered_pages(expected)
    assert '发票＋行程单' in (store.root / '导出' / exported['md']).read_text(encoding='utf-8')
    assert unchanged_snapshot(store) == before_export
    assert not list((store.root / '导出').glob('.ride-export-*'))


def test_taxi_without_trip_remains_partial_with_full_original_and_cannot_be_verified(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Missing trip', 'category': '打车', 'amount': '123.45',
                         'material_basis': 'invoice_payment'})
    item = store.add_attachment(item['id'], 'invoice.pdf',
                                make_invoice_pdf(tmp_path / 'invoice.pdf').read_bytes(), 'invoice')
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert result['partial'] and result['missing'] == ['行程单']
    assert '完整原件逐页A4' in result['layout'] and '付款截图' not in result['layout']
    with pytest.raises(ValueError, match='材料'):
        store.transition(item['id'], 'verify')
    assert unchanged_snapshot(store) == before


def test_mismatched_taxi_amounts_cannot_publish_preview_or_export_and_keep_originals(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = taxi(store, trip_amount='99.00')
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='不一致'):
        create_expense_print_preview(store, item['id'], item['version'])
    assert unchanged_snapshot(store) == before
    assert list((store.root / '导出').iterdir()) == []
    store.transition(item['id'], 'verify')
    verified_before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='不一致'):
        export_items(store, [item['id']])
    assert unchanged_snapshot(store) == verified_before
    assert list((store.root / '导出').iterdir()) == []


def test_confirmed_taxi_payment_only_retains_its_quarter_sheet_rule(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Confirmed payment', 'category': '打车', 'amount': '18.00',
                         'material_basis': 'payment_only'})
    item = store.add_attachment(item['id'], 'payment.pdf', proof('CONFIRMED PAYMENT'), 'payment')
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert result['complete'] and result['missing'] == [] and '2×2' in result['layout']
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 1 and 'CONFIRMED PAYMENT' in document[0].get_text()
    assert unchanged_snapshot(store) == before


@pytest.mark.parametrize('role', ['trip', 'invoice'])
def test_even_same_amount_replaced_ride_original_blocks_cached_preview_and_final_export(
        tmp_path: Path, role: str) -> None:
    store = ExpenseStore(tmp_path)
    item = taxi(store)
    # Make the trip canonical rather than an other-role projection, to exercise both stored roles.
    trip = next(a for a in item['attachments'] if a['role'] == 'trip')
    store.set_role(trip['id'], 'trip')
    item = store.transition(item['id'], 'verify')
    create_expense_print_preview(store, item['id'], item['version'], build_id='stable')
    attachment = next(a for a in item['attachments'] if a['role'] == role)
    path, _ = store.attachment_path(attachment['id'])
    replacement = (make_caocao_trip_pdf(tmp_path / 'replacement.pdf', amount='123.45') if role == 'trip'
                   else make_invoice_pdf(tmp_path / 'replacement.pdf', invoice_number='12345678901234567891'))
    path.write_bytes(replacement.read_bytes())
    before = unchanged_snapshot(store)
    existing_exports = {p.name: p.read_bytes() for p in (store.root / '导出').iterdir()}
    with pytest.raises(ValueError, match='原件与保存时不一致'):
        create_expense_print_preview(store, item['id'], item['version'], build_id='stable')
    with pytest.raises(ValueError, match='原件与保存时不一致'):
        export_items(store, [item['id']])
    assert unchanged_snapshot(store) == before
    assert {p.name: p.read_bytes() for p in (store.root / '导出').iterdir()} == existing_exports
