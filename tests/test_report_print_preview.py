"""The screen and print button share a real table-plus-materials PDF."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import report_print_preview as preview
from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout import reimbursement
from invoice_print_layout.report_excel import TEMPLATE_NAME, create_excel
from invoice_print_layout.workbench import ExpenseStore
from tests.test_expense_print_preview import matter, unchanged_snapshot


def fake_excel(captured: list[dict[str, Any]]) -> Any:
    def create(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path,
               *, summary_pdf: Path | None = None) -> None:
        captured.extend(copy.deepcopy(items))
        output.write_bytes(b'synthetic workbook')
        assert summary_pdf is not None
        with pymupdf.open() as document:
            page = document.new_page(width=595.28, height=841.89)
            page.insert_text((30, 40), 'COMPANY TABLE ONLY')
            page.insert_text((30, 60), str(sum(item['amount_cents'] for item in items)))
            document.save(summary_pdf)
    return create


def test_preview_orders_form_then_originals_without_reviewing_and_reuses_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    hotel = matter(store, pages=2)
    rail = matter(store, category='高铁')
    # An unverified and incomplete payment remains a read-only partial preview.
    partial = store.create({'title': 'Missing taxi proof', 'category': '打车', 'amount': '18'})
    store.add_attachment(partial['id'], 'payment.pdf', _pdf('PARTIAL PAYMENT'), 'payment')
    no_proof = store.create({'title': 'No proof received', 'category': '其他', 'amount': '3'})
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(preview, 'create_excel', fake_excel(captured))
    before = unchanged_snapshot(store)
    ids = [hotel['id'], partial['id'], rail['id'], no_proof['id']]
    result = preview.create_report_print_preview(store, ids, build_id='test-build')
    folder = store.root / '导出'
    assert result['combined_pdf'] == result['pdf']
    assert result['combined_preview_pages'] == result['preview_pages']
    assert result['page_count'] == 5 and result['table_page_count'] == 1
    assert result['ordered_item_ids'] == [rail['id'], partial['id'], hotel['id'], no_proof['id']]
    assert [(item['id'], item['pages']) for item in captured] == [
        (rail['id'], '2'), (partial['id'], '3'), (hotel['id'], '4-5'), (no_proof['id'], '无材料')]
    with pymupdf.open(folder / result['combined_pdf']) as document:
        assert 'COMPANY TABLE ONLY' in document[0].get_text()
        assert 'PARTIAL PAYMENT' in document[2].get_text()
        for index, page in enumerate(document):
            image = pymupdf.Pixmap(folder / result['combined_preview_pages'][index])
            # Every displayed image is a rendering of this exact PDF page.
            assert image.samples == page.get_pixmap(dpi=120, alpha=False).samples
        original = create_expense_print_preview(store, hotel['id'], hotel['version'], build_id='test-build')
        with pymupdf.open(folder / original['pdf']) as hotel_pdf:
            assert document[3].get_text('words') == hotel_pdf[0].get_text('words')
            assert document[4].get_pixmap().samples == hotel_pdf[1].get_pixmap().samples
    monkeypatch.setattr(preview, 'create_excel', lambda *a, **kw: pytest.fail('cache should skip Excel'))
    assert preview.create_report_print_preview(store, ids, build_id='test-build') == result
    assert unchanged_snapshot(store) == before


def test_combined_preview_renders_each_final_page_only_once_and_reuses_vector_material_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import expense_print_preview

    store = ExpenseStore(tmp_path)
    hotel = matter(store, pages=2)
    partial = store.create({'title': 'Unclassified receipt', 'category': '其他', 'amount': '3'})
    partial = store.add_attachment(partial['id'], 'proof.pdf', _pdf('UNCLASSIFIED ORIGINAL'), 'other')
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    monkeypatch.setattr(preview, 'create_excel', fake_excel([]))
    before = unchanged_snapshot(store)
    rendered: list[int] = []
    original_pixmap = pymupdf.Page.get_pixmap

    def counted_pixmap(page: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get('dpi') == 120:
            rendered.append(page.number)
        return original_pixmap(page, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, 'get_pixmap', counted_pixmap)
    ids = [partial['id'], hotel['id']]
    reads: list[list[str]] = []
    original_get_many = store.get_many

    def batch_read(keys: list[str]) -> list[dict[str, Any]]:
        reads.append(list(keys))
        return original_get_many(keys)

    monkeypatch.setattr(store, 'get_many', batch_read)
    combined = preview.create_report_print_preview(store, ids, build_id='test-build')
    assert reads == [ids, combined['ordered_item_ids']]
    assert combined['page_count'] == len(rendered) == 4
    folder = store.root / '导出'
    assert len(list(folder.glob('expense-pdf-*.pdf'))) == 2
    assert not list(folder.glob('expense-preview-*.png'))

    # Selecting another report scope reuses material PDFs; it must not compose
    # or render a separate set of hidden per-expense preview images first.
    monkeypatch.setattr(expense_print_preview, 'compose_matter',
                        lambda *_: pytest.fail('Reuse the cached vector expense PDF'))
    monkeypatch.setattr(expense_print_preview, '_compose_partial',
                        lambda *_: pytest.fail('Reuse the cached partial vector PDF'))
    rendered.clear()
    selected = preview.create_report_print_preview(store, [hotel['id']], build_id='test-build')
    assert selected['page_count'] == len(rendered) == 3
    rendered.clear()
    reads.clear()
    assert preview.create_report_print_preview(store, [hotel['id']], build_id='test-build') == selected
    assert rendered == [] and reads == [[hotel['id']], [hotel['id']]]

    # The unchanged public single-expense API can add images from that same PDF.
    single = create_expense_print_preview(store, hotel['id'], hotel['version'], build_id='test-build')
    assert single['page_count'] == len(single['preview_pages']) == len(rendered) == 2
    assert unchanged_snapshot(store) == before


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('change', ['version', 'source'])
def test_report_batch_recheck_uses_fresh_views_and_sources_before_returning(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool, change: str) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    monkeypatch.setattr(preview, 'create_excel', fake_excel([]))
    if cached:
        preview.create_report_print_preview(store, [item['id']], build_id='test')
    original_get_many = store.get_many
    calls: list[list[str]] = []

    def change_before_final_read(ids: list[str]) -> list[dict[str, Any]]:
        calls.append(list(ids))
        if len(calls) == 2:
            if change == 'version':
                current = store.get(item['id'])
                store.update(item['id'], {**current, 'title': 'Changed before final preview read'})
            else:
                path, _ = store.attachment_path(item['attachments'][0]['id'])
                path.write_bytes(_pdf('CHANGED BEFORE FINAL PREVIEW READ'))
        return original_get_many(ids)

    monkeypatch.setattr(store, 'get_many', change_before_final_read)
    with pytest.raises(ValueError, match='预览期间'):
        preview.create_report_print_preview(store, [item['id']], build_id='test')
    assert calls == [[item['id']], [item['id']]]
    if not cached:
        assert not list((store.root / '导出').glob('report-preview-*'))
    if change == 'source':
        assert store.get(item['id'])['version'] == item['version']
    assert not store.get(item['id'])['verified']


def _pdf(text: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=700)
        page.insert_text((20, 40), text)
        return document.tobytes()


@pytest.mark.parametrize('historical', ['submitted', 'reimbursed'])
def test_historical_preview_does_not_require_draft_stage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: str) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    with store.connect() as database:
        stored = json.loads(database.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()[0])
        stored['stage'] = historical
        database.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored), item['id']))
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    monkeypatch.setattr(preview, 'create_excel', fake_excel([]))
    before = unchanged_snapshot(store)
    assert preview.create_report_print_preview(store, [item['id']], build_id='test')['page_count'] == 2
    assert unchanged_snapshot(store) == before


def test_failed_table_export_never_publishes_a_combined_result_or_changes_fee(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    before = unchanged_snapshot(store)
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError('Excel unavailable')
    monkeypatch.setattr(preview, 'create_excel', fail)
    with pytest.raises(ValueError, match='Excel unavailable'):
        preview.create_report_print_preview(store, [item['id']])
    assert not list((store.root / '导出').glob('report-preview-*'))
    assert unchanged_snapshot(store) == before


def test_formal_table_is_first_and_md_excel_pages_are_offset_together(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store, pages=2)
    store.transition(item['id'], 'verify')
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(reimbursement, 'create_excel', fake_excel(captured))
    monkeypatch.setattr(reimbursement, 'validate_template', lambda path: None)
    monkeypatch.setattr(reimbursement, 'spreadsheet_runtime', lambda: None)
    result = reimbursement.create_report(store, [item['id']], include_table=True)
    assert captured[0]['pages'] == '2-3'
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert len(document) == 3
        assert 'COMPANY TABLE ONLY' in document[0].get_text()
        assert 'INVOICE' in document[1].get_text()
    assert '| 2-3 |' in (store.root / '导出' / result['md']).read_text(encoding='utf-8')
    assert store.get(item['id'])['stage'] == 'submitted'


def test_formal_table_failure_does_not_submit_or_publish_report(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    store.transition(item['id'], 'verify')
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    monkeypatch.setattr(reimbursement, 'validate_template', lambda path: None)
    monkeypatch.setattr(reimbursement, 'spreadsheet_runtime', lambda: None)
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError('No native Excel')
    monkeypatch.setattr(reimbursement, 'create_excel', fail)
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='No native Excel'):
        reimbursement.create_report(store, [item['id']], include_table=True)
    assert not list((store.root / '导出').glob('*报销包*'))
    assert unchanged_snapshot(store) == before


def test_native_company_form_is_one_vector_a4_with_chinese_and_matching_total(tmp_path: Path) -> None:
    template_name = os.environ.get('INVOICE_TEST_REPORT_TEMPLATE')
    if not template_name or os.name != 'nt':
        pytest.skip('requires configured local company template and native Excel')
    store = ExpenseStore(tmp_path)
    item = matter(store)
    item['pages'] = '2'
    template = Path(template_name)
    source = template.read_bytes()
    output, pdf = tmp_path / 'company.xlsx', tmp_path / 'company.pdf'
    create_excel(template, [item], {'person': '测试', 'period': '2026-10', 'payment': 'personal'},
                 output, summary_pdf=pdf)
    with pymupdf.open(pdf) as document:
        assert len(document) == 1
        text = ''.join(document[0].get_text().split())
        for expected in ('活动运营费用统计表', '费用合计', '测试', '14.00', '相关人员核实签字'):
            assert expected in text
        assert abs(document[0].rect.width - 595.28) < 1
        assert abs(document[0].rect.height - 841.89) < 1
        assert not document[0].get_images(), 'Company form must remain vector-based'
    assert template.read_bytes() == source
