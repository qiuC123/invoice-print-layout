"""Print entry points share full-size invoice pairing and courier proof rules."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import reimbursement, report_print_preview
from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout.layout import A4_WIDTH
from invoice_print_layout.report_snapshot import material_batches, snapshot
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from tests.test_courier import make_courier_pair
from tests.test_expense_print_preview import unchanged_snapshot
from tests.test_rail_report_integration import install_excel, store_and_projects


def _native_invoice(label: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=397)
        page.insert_text((20, 30), '电子发票（普通发票）', fontname='china-s', fontsize=13)
        page.insert_text((20, 70), label, fontsize=12)
        page.insert_text((20, 300), '价税合计', fontname='china-s', fontsize=10)
        page.insert_text((20, 370), 'BOTTOM_' + label, fontsize=11)
        page.draw_rect(pymupdf.Rect(400, 200, 460, 260), color=(0, 0, 0))
        return document.tobytes()


def _hotel(store: ExpenseStore, project: dict[str, Any], label: str, *, verified: bool = False) -> dict[str, Any]:
    item = store.create({'title': label, 'category': '酒店', 'project_id': project['id'],
                         'amount': '43', 'material_basis': 'standard'})
    store.add_attachment(item['id'], label + '.pdf', _native_invoice(label), 'invoice')
    return store.transition(item['id'], 'verify') if verified else store.get(item['id'])


@pytest.mark.parametrize('mode', ['preview', 'formal', 'formal-table', 'export'])
def test_other_invoices_keep_full_content_and_old_size_across_print_entries(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    store, project, _ = store_and_projects(tmp_path)
    items = [_hotel(store, project, label, verified=mode == 'export') for label in ['HOTEL_A', 'HOTEL_B', 'HOTEL_C']]
    captured: list[dict[str, Any]] = []
    install_excel(store, monkeypatch, captured)
    before = unchanged_snapshot(store)
    ids = [item['id'] for item in items]
    if mode == 'preview':
        result = report_print_preview.create_report_print_preview(store, ids, build_id='other-invoice-test')
    elif mode == 'export':
        result = export_items(store, ids)
    else:
        result = reimbursement.create_report(store, ids, submit=False, include_table=mode == 'formal-table')
    offset = int(mode in {'preview', 'formal-table'})
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 2 + offset
        pages = list(document)[offset:]
        assert 'HOTEL_A' in pages[0].get_text() and 'HOTEL_B' in pages[0].get_text()
        assert 'HOTEL_C' in pages[1].get_text() and 'HOTEL_C' not in pages[0].get_text()
        for item in items:
            text = ''.join(page.get_text() for page in pages)
            assert text.count('BOTTOM_' + item['title']) == 1
        for page in pages:
            assert not page.get_images()
            for block in page.get_text('dict')['blocks']:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        if span['text'].startswith('BOTTOM_'):
                            assert span['size'] == pytest.approx(11 * (A4_WIDTH - 24) / 595, abs=.02)
        assert sum(len(page.get_drawings()) for page in pages) == 3
    if mode != 'export':
        assert [item['pages'] for item in captured] == [str(1 + offset), str(1 + offset), str(2 + offset)]
    assert unchanged_snapshot(store) == before


def test_category_and_report_subitems_break_invoice_pairing(tmp_path: Path) -> None:
    store, project, _ = store_and_projects(tmp_path)
    hotel = _hotel(store, project, 'HOTEL')
    other = store.create({'title': '住宿补票', 'project_type': 'other', 'project_name': '住宿补票', 'project_id': project['id'],
                          'amount': '43', 'material_basis': 'standard'})
    store.add_attachment(other['id'], 'other.pdf', _native_invoice('OTHER'), 'invoice')
    other = store.get(other['id'])
    other = store.edit_review_fields(other['id'], other['version'], 'other', '住宿补票', other['amount'])
    report = snapshot([other, hotel])
    assert all(len(batch) == 1 for batch in material_batches(report, store))
    assert [item['id'] for item in report['items']] == [hotel['id'], other['id']]


def _courier(store: ExpenseStore, project: dict[str, Any], tmp_path: Path, *, verified: bool = False) -> dict[str, Any]:
    invoice, detail = make_courier_pair(tmp_path / 'courier-source', '顺丰')
    item = store.create({'title': '顺丰', 'project_type': 'other', 'project_name': '顺丰', 'project_id': project['id'],
                         'amount': '43', 'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], invoice.name, invoice.read_bytes(), 'invoice')
    store.add_attachment(item['id'], detail.name, detail.read_bytes(), 'detail')
    store.add_attachment(item['id'], 'mistaken-payment.pdf', _native_invoice('PAYMENT_NOT_PRINTED'), 'payment')
    current = store.get(item['id'])
    store.edit_review_fields(item['id'], current['version'], 'other', '顺丰', current['amount'])
    return store.transition(item['id'], 'verify') if verified else store.get(item['id'])


@pytest.mark.parametrize('mode', ['single', 'preview', 'formal', 'formal-table', 'export'])
def test_courier_invoice_and_waybill_print_without_mistaken_payment_and_without_ledger_writes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    store, project, _ = store_and_projects(tmp_path)
    item = _courier(store, project, tmp_path, verified=mode == 'export')
    assert item['category'] == '其他' and item['material_basis'] == 'invoice_payment'
    assert item['complete'] and not item['missing']
    captured: list[dict[str, Any]] = []
    install_excel(store, monkeypatch, captured)
    before = unchanged_snapshot(store)
    if mode == 'single':
        result = create_expense_print_preview(store, item['id'], item['version'], build_id='courier-test')
    elif mode == 'preview':
        result = report_print_preview.create_report_print_preview(store, [item['id']], build_id='courier-test')
    elif mode == 'export':
        result = export_items(store, [item['id']])
    else:
        result = reimbursement.create_report(store, [item['id']], submit=False, include_table=mode == 'formal-table')
    offset = int(mode in {'preview', 'formal-table'})
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 1 + offset
        text = ''.join(page.get_text() for page in list(document)[offset:])
        assert '运单明细' in text and 'SF1234567890' in text and '价税合计' in text
        assert 'PAYMENT_NOT_PRINTED' not in text
    if mode != 'single':
        if mode != 'export':
            assert captured[0]['pages'] == str(1 + offset)
        if mode != 'preview':
            assert '发票＋运单明细' in (store.root / '导出' / result['md']).read_text(encoding='utf-8')
    assert unchanged_snapshot(store) == before
    assert len(store.get(item['id'])['attachments']) == 3


def test_courier_rejects_replaced_original_and_mismatched_invoice_number(tmp_path: Path) -> None:
    store, project, _ = store_and_projects(tmp_path)
    item = _courier(store, project, tmp_path)
    detail = next(a for a in item['attachments'] if a['role'] == 'detail')
    path, _ = store.attachment_path(detail['id'])
    _, wrong = make_courier_pair(tmp_path / 'different-number', '顺丰', '26000000000000000002')
    path.write_bytes(wrong.read_bytes())
    folder = tmp_path / 'output'
    folder.mkdir()
    with pytest.raises(ValueError, match='原件'):
        reimbursement.compose_matter(store, deepcopy(item), folder)
    assert not list(folder.iterdir())


def test_courier_equal_amount_cannot_pair_a_different_invoices_waybill(tmp_path: Path) -> None:
    store, project, _ = store_and_projects(tmp_path)
    invoice, _ = make_courier_pair(tmp_path / 'invoice-source', '顺丰')
    _, detail = make_courier_pair(tmp_path / 'waybill-source', '顺丰', '26000000000000000002')
    item = store.create({'title': '快递', 'project_id': project['id'], 'category': '顺丰',
                         'amount': '43', 'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    store.add_attachment(item['id'], 'detail.pdf', detail.read_bytes(), 'detail')
    current = store.get(item['id'])
    assert current['complete']
    before = unchanged_snapshot(store)
    folder = tmp_path / 'mismatched-output'
    folder.mkdir()
    with pytest.raises(ValueError, match='号码不对应'):
        reimbursement.compose_matter(store, current, folder)
    assert not list(folder.iterdir()) and unchanged_snapshot(store) == before


def test_existing_multipage_payment_only_keeps_page_offsets_with_table(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project, _ = store_and_projects(tmp_path)
    item = store.create({'title': 'Multi-page payment', 'project_id': project['id'], 'category': '其他',
                         'amount': '43', 'material_basis': 'payment_only'})
    with pymupdf.open() as document:
        for index in range(9):
            document.new_page(width=300, height=600).insert_text((20, 40), f'PAYMENT_{index}')
        store.add_attachment(item['id'], 'payment.pdf', document.tobytes(), 'payment')
    captured: list[dict[str, Any]] = []
    install_excel(store, monkeypatch, captured)
    before = unchanged_snapshot(store)
    result = reimbursement.create_report(store, [item['id']], submit=False, include_table=True)
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 4
        for index in range(9):
            assert f'PAYMENT_{index}' in ''.join(page.get_text() for page in document)
    assert captured[0]['pages'] == '2,3,4'
    assert unchanged_snapshot(store) == before
