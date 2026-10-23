"""Only received proofs appear in partial previews; formal requirements stay strict."""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.payment_layout import append_invoice_payments
from invoice_print_layout.reimbursement import create_report
from invoice_print_layout.workbench import ExpenseStore
from tests.test_expense_print_preview import proof, unchanged_snapshot


@pytest.mark.parametrize('role', ['invoice', 'receipt'])
def test_received_voucher_uses_half_sheet_and_missing_payment_stays_missing(tmp_path: Path, role: str) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic partial voucher', 'category': '酒店',
                         'amount': '14', 'material_basis': 'invoice_payment'})
    item = store.add_attachment(item['id'], 'voucher.pdf', proof('VOUCHER'), role)
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert not result['complete'] and result['partial'] and result['missing'] == ['付款截图']
    assert '非正式报销包' in result['layout'] and '1×2' in result['layout']
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 1
        page = document[0]
        assert page.search_for('VOUCHER TOP')[0].y0 < page.search_for('VOUCHER BOTTOM')[0].y1 < A4_HEIGHT / 2
        assert 'PAYMENT' not in page.get_text()
        assert abs(page.rect.width - A4_WIDTH) < 1
    assert unchanged_snapshot(store) == before
    with pymupdf.open() as document:
        with pytest.raises(ValueError, match='缺失'):
            append_invoice_payments(document, store, item)
    with pytest.raises(ValueError, match='材料齐全'):
        create_report(store, [item['id']], submit=False)


def test_received_payments_use_quarter_cells_without_inventing_a_voucher(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic partial payment', 'category': '餐饮', 'amount': '14',
                         'material_basis': 'invoice_payment'})
    for number in range(2):
        item = store.add_attachment(item['id'], f'payment{number}.pdf', proof(f'PAYMENT {number}'), 'payment')
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert result['partial'] and not result['complete'] and result['missing'] == ['发票或收据']
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 1
        left = document[0].search_for('PAYMENT 0 TOP')[0]
        right = document[0].search_for('PAYMENT 1 TOP')[0]
        assert left.x1 < A4_WIDTH / 2 < right.x0
        assert left.y1 < A4_HEIGHT / 2 and right.y1 < A4_HEIGHT / 2
        assert 'VOUCHER' not in document[0].get_text()
    assert unchanged_snapshot(store) == before


def test_partial_standard_trip_preserves_full_content_instead_of_guessing_crop(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Missing corresponding invoice', 'category': '打车', 'amount': '14'})
    item = store.add_attachment(item['id'], 'uncertain-trip.pdf', proof('UNCONFIRMED TRIP DECORATION', pages=2), 'trip')
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert result['partial'] and result['missing'] == ['发票'] and result['page_count'] == 2
    assert '不推断' in result['layout']
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        for number, page in enumerate(document):
            assert f'UNCONFIRMED TRIP DECORATION TOP {number}' in page.get_text()
            assert f'UNCONFIRMED TRIP DECORATION BOTTOM {number}' in page.get_text()
            assert abs(page.rect.height - A4_HEIGHT) < 1
    assert unchanged_snapshot(store) == before


def test_unclassified_proofs_remain_visible_and_uncategorized(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Unclassified proof', 'category': '其他', 'amount': '14'})
    item = store.add_attachment(item['id'], 'unknown.pdf', proof('UNKNOWN PROOF'), 'other')
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert result['partial'] and result['missing'] and '非正式报销包' in result['layout']
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert 'UNKNOWN PROOF' in document[0].get_text()
    assert unchanged_snapshot(store) == before


def test_unclassified_proof_is_full_size_beside_known_partial_voucher(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Partial voucher and other proof', 'category': '餐饮',
                         'amount': '14', 'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], 'voucher.pdf', proof('VOUCHER'), 'invoice')
    item = store.add_attachment(item['id'], 'unknown.pdf', proof('UNCLASSIFIED'), 'other')
    before = unchanged_snapshot(store)
    result = create_expense_print_preview(store, item['id'], item['version'])
    assert result['page_count'] == 2 and '用途仍待核对' in result['layout']
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert 'VOUCHER' in document[0].get_text() and 'UNCLASSIFIED' in document[1].get_text()
        assert document[1].search_for('UNCLASSIFIED BOTTOM')[0].y1 > A4_HEIGHT / 2
    assert unchanged_snapshot(store) == before


def test_partial_preview_cache_reuses_pages_and_full_preview_changes_when_completed(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic incomplete', 'category': '餐饮', 'amount': '14',
                         'material_basis': 'invoice_payment'})
    item = store.add_attachment(item['id'], 'voucher.pdf', proof('VOUCHER'), 'invoice')
    first = create_expense_print_preview(store, item['id'], item['version'], build_id='test-build')
    assert create_expense_print_preview(store, item['id'], item['version'], build_id='test-build') == first
    item = store.add_attachment(item['id'], 'payment.pdf', proof('PAYMENT'), 'payment')
    complete = create_expense_print_preview(store, item['id'], item['version'], build_id='test-build')
    assert complete['complete'] and not complete['partial'] and complete['missing'] == []
    assert complete['pdf'] != first['pdf'] and '非正式' not in complete['layout']
    assert not store.get(item['id'])['verified'] and not store.get(item['id'])['ready']


def test_missing_original_file_is_not_silently_shown_from_full_preview_cache(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Changed availability', 'category': '酒店', 'amount': '14',
                         'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], 'voucher.pdf', proof('VOUCHER'), 'invoice')
    item = store.add_attachment(item['id'], 'payment.pdf', proof('PAYMENT'), 'payment')
    full = create_expense_print_preview(store, item['id'], item['version'], build_id='test-build')
    payment = next(attachment for attachment in item['attachments'] if attachment['role'] == 'payment')
    store.attachment_path(payment['id'])[0].unlink()
    current = store.get(item['id'])
    assert current['version'] != item['version']
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='事项或材料已变化'):
        create_expense_print_preview(store, item['id'], item['version'], build_id='test-build')
    assert unchanged_snapshot(store) == before
    partial = create_expense_print_preview(store, item['id'], current['version'], build_id='test-build')
    assert partial['partial'] and partial['missing'] == ['付款截图'] and partial['pdf'] != full['pdf']
    with pymupdf.open(store.root / '导出' / partial['pdf']) as document:
        text = '\n'.join(page.get_text() for page in document)
        assert 'VOUCHER' in text and 'PAYMENT' not in text
    assert unchanged_snapshot(store) == before
