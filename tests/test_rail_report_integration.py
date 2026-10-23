"""Synthetic native rail tickets share A4 pages across every report entry point."""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import math
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import reimbursement
from invoice_print_layout import report_print_preview
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.report_excel import TEMPLATE_NAME
from invoice_print_layout.report_snapshot import material_batches, snapshot
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from tests.test_expense_print_preview import unchanged_snapshot


def native_ticket(label: str, *, identified: bool = True) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=595.27, height=396.65)
        if identified:
            page.insert_text((25, 35), '电子发票（铁路电子客票）', fontname='china-s', fontsize=13)
        else:
            page.insert_text((25, 35), 'UNCLASSIFIED ORIGINAL', fontsize=13)
        page.insert_text((25, 75), 'TOP_' + label, fontsize=11)
        page.insert_text((25, 105), 'Paid CNY 14.00', fontsize=11)
        page.insert_text((25, 375), 'BOTTOM_' + label, fontsize=11)
        page.draw_rect(pymupdf.Rect(450, 280, 510, 340), color=(0, 0, 0), width=1)
        page.draw_circle(pymupdf.Point(400, 315), 20, color=(1, 0, 0), width=1)
        return document.tobytes()


def store_and_projects(tmp_path: Path) -> tuple[ExpenseStore, dict[str, Any], dict[str, Any]]:
    store = ExpenseStore(tmp_path / 'workspace')
    registry = ProjectRegistry(store.workspace / '后勤' / 'tasks.sqlite3')
    return store, registry.create_project('Synthetic A'), registry.create_project('Synthetic B')


def rail(store: ExpenseStore, project: dict[str, Any], label: str, *, identified: bool = True,
         extra: bool = False, category: str = '高铁', verified: bool = False) -> dict[str, Any]:
    item = store.create({'title': label, 'category': category, 'project_id': project['id'],
                         'amount': '14', 'material_basis': 'standard', 'expense_date': '2026-10-15'})
    store.add_attachment(item['id'], label + '.pdf', native_ticket(label, identified=identified), 'invoice')
    if extra:
        store.add_attachment(item['id'], label + '-detail.pdf', native_ticket(label + '_DETAIL', identified=False), 'invoice')
    return store.transition(item['id'], 'verify') if verified else store.get(item['id'])


def excel_stub(captured: list[dict[str, Any]]) -> Any:
    def create(_template: Path, items: list[dict[str, Any]], _options: dict[str, str], output: Path,
               *, summary_pdf: Path | None = None) -> None:
        captured.extend(deepcopy(items))
        output.write_bytes(b'synthetic workbook')
        if summary_pdf is not None:
            with pymupdf.open() as document:
                page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                page.insert_text((30, 40), 'SYNTHETIC COMPANY TABLE ONLY')
                page.insert_text((30, 60), 'Total ' + str(sum(item['amount_cents'] for item in items)))
                document.save(summary_pdf)
    return create


def install_excel(store: ExpenseStore, monkeypatch: pytest.MonkeyPatch,
                  captured: list[dict[str, Any]]) -> None:
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(reimbursement, 'validate_template', lambda _: None)
    monkeypatch.setattr(reimbursement, 'spreadsheet_runtime', lambda: None)
    monkeypatch.setattr(reimbursement, 'create_excel', excel_stub(captured))
    monkeypatch.setattr(report_print_preview, 'create_excel', excel_stub(captured))


def page_labels(page: pymupdf.Page, labels: list[str]) -> list[str]:
    text = page.get_text()
    return [label for label in labels if 'TOP_' + label + '\n' in text]


def assert_vectors_and_scale(document: pymupdf.Document, labels: list[str], offset: int = 0) -> None:
    expected_size = 11 * (A4_WIDTH - 24) / 595.27
    text = ''.join(page.get_text() for page in document)
    for label in labels:
        assert text.count('TOP_' + label + '\n') == 1
        assert text.count('BOTTOM_' + label + '\n') == 1
    for page in list(document)[offset:]:
        assert not page.get_images(), 'Native rail originals must not become raster images'
        assert page.rect.width == pytest.approx(A4_WIDTH, abs=0.02)
        assert page.rect.height == pytest.approx(A4_HEIGHT, abs=0.02)
        for block in page.get_text('dict')['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    if span['text'].startswith(('TOP_', 'BOTTOM_')):
                        assert span['size'] == pytest.approx(expected_size, abs=0.02), 'Pairing must keep the old full-A4 printed scale'
    assert sum(len(page.get_drawings()) for page in list(document)[offset:]) == len(labels) * 2


def assert_all_native_content(store: ExpenseStore, items: list[dict[str, Any]],
                              document: pymupdf.Document, offset: int = 0) -> None:
    original = Counter()
    for item in items:
        for attachment in item['attachments']:
            path, _ = store.attachment_path(attachment['id'])
            with pymupdf.open(path) as source:
                original.update(''.join(''.join(page.get_text() for page in source).split()))
    printed = Counter(''.join(''.join(page.get_text() for page in list(document)[offset:]).split()))
    assert printed == original, 'Every source character, including Chinese ticket headings, must survive exactly once'


@pytest.mark.parametrize('count', [3, 4, 5])
@pytest.mark.parametrize('mode', ['formal', 'formal-table', 'preview', 'export'])
def test_odd_even_tickets_share_pages_and_all_entry_points_keep_sources_read_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, mode: str) -> None:
    store, project, _ = store_and_projects(tmp_path)
    labels = [f'RAIL_{number:02}' for number in range(count)]
    items = [rail(store, project, label, verified=mode == 'export') for label in labels]
    captured: list[dict[str, Any]] = []
    install_excel(store, monkeypatch, captured)
    before = unchanged_snapshot(store)
    ids = [item['id'] for item in items]
    if mode == 'preview':
        result = report_print_preview.create_report_print_preview(store, ids, build_id='rail-integration')
    elif mode == 'export':
        result = export_items(store, ids)
    else:
        result = reimbursement.create_report(store, ids, submit=False, include_table=mode == 'formal-table')
    offset = int(mode in {'preview', 'formal-table'})
    folder = store.root / '导出'
    with pymupdf.open(folder / result['pdf']) as document:
        assert len(document) == math.ceil(count / 2) + offset
        if offset:
            assert 'SYNTHETIC COMPANY TABLE ONLY' in document[0].get_text()
        assert [page_labels(page, labels) for page in list(document)[offset:]] == [labels[index:index + 2] for index in range(0, count, 2)]
        assert_vectors_and_scale(document, labels, offset)
        assert_all_native_content(store, items, document, offset)
        if mode == 'preview':
            assert result['table_page_count'] == 1
            assert result['combined_pdf'] == result['pdf']
            assert result['combined_preview_pages'] == result['preview_pages']
            assert result['page_count'] == len(document)
            for page, name in zip(document, result['combined_preview_pages'], strict=True):
                image = pymupdf.Pixmap(folder / name)
                assert image.samples == page.get_pixmap(dpi=120, alpha=False).samples
    if mode != 'export':
        expected_pages = {item['id']: str(number // 2 + 1 + offset) for number, item in enumerate(items)}
        assert {item['id']: item['pages'] for item in captured} == expected_pages
        assert [item['id'] for item in captured] == ids
        if mode != 'preview':
            markdown = (folder / result['md']).read_text(encoding='utf-8')
            lines = [line for line in markdown.splitlines() if line.startswith('| ') and line.split('|')[1].strip() in ids]
            assert [line.split('|')[1].strip() for line in lines] == ids
            assert {line.split('|')[1].strip(): line.split('|')[7].strip() for line in lines} == expected_pages
    else:
        markdown = (folder / result['md']).read_text(encoding='utf-8')
        lines = [line for line in markdown.splitlines() if line.startswith('| ') and line.split('|')[1].strip() in ids]
        assert [line.split('|')[1].strip() for line in lines] == ids
        assert f'总计：{count * 14:.2f}元' in markdown
    assert unchanged_snapshot(store) == before
    assert all(item['stage'] == 'draft' for item in store.list_items())


def test_batching_uses_store_eligibility_and_never_crosses_project_identity(tmp_path: Path) -> None:
    store, project_a, project_b = store_and_projects(tmp_path)
    a = [rail(store, project_a, f'A_{number}') for number in range(3)]
    b = [rail(store, project_b, f'B_{number}') for number in range(3)]
    report = snapshot([b[0], a[0], b[1], a[1], b[2], a[2]])
    frozen = deepcopy(report)
    assert all(len(batch) == 1 for batch in material_batches(report)), 'Without source access, batching remains conservative'
    grouped = material_batches(report, store)
    assert [[item['id'] for item in batch] for batch in grouped] == [
        [b[0]['id'], b[1]['id']], [b[2]['id']], [a[0]['id'], a[1]['id']], [a[2]['id']]]
    assert all(len({item['project_id'] for item in batch}) == 1 for batch in grouped)
    assert report == frozen


def test_unknown_or_extra_originals_break_runs_and_other_categories_never_share_rail_pages(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project_a, project_b = store_and_projects(tmp_path)
    specs = [('FIRST', True, False), ('UNKNOWN', False, False), ('AFTER_UNKNOWN', True, False),
             ('EXTRA', True, True), ('AFTER_EXTRA', True, False)]
    items = [rail(store, project_a, label, identified=identified, extra=extra)
             for label, identified, extra in specs]
    items += [rail(store, project_b, 'OTHER_PROJECT'), rail(store, project_a, 'HOTEL', category='酒店')]
    labels = [label for label, _, _ in specs] + ['EXTRA_DETAIL', 'OTHER_PROJECT', 'HOTEL']
    captured: list[dict[str, Any]] = []
    install_excel(store, monkeypatch, captured)
    before = unchanged_snapshot(store)
    result = reimbursement.create_report(store, [item['id'] for item in items], submit=False)
    # Ineligible expenses retain the existing source-filename order and full pages.
    expected = [['FIRST'], ['UNKNOWN'], ['AFTER_UNKNOWN'], ['EXTRA_DETAIL'], ['EXTRA'], ['AFTER_EXTRA'], ['OTHER_PROJECT'], ['HOTEL']]
    with pymupdf.open(store.root / '导出' / result['pdf']) as document:
        assert [page_labels(page, labels) for page in document] == expected
        assert_vectors_and_scale(document, labels)
        assert_all_native_content(store, items, document)
    assert unchanged_snapshot(store) == before


def test_single_eligible_rail_preview_uses_upper_half_without_cropping_or_reducing_size(
        tmp_path: Path) -> None:
    store, project, _ = store_and_projects(tmp_path)
    item = rail(store, project, 'SINGLE')
    folder = tmp_path / 'compose'
    folder.mkdir()
    before = unchanged_snapshot(store)
    output, _ = reimbursement.compose_matter(store, deepcopy(item), folder)
    with pymupdf.open(output) as document:
        assert len(document) == 1
        assert document[0].search_for('BOTTOM_SINGLE')[0].y1 < A4_HEIGHT / 2
        assert_vectors_and_scale(document, ['SINGLE'])
        assert_all_native_content(store, [item], document)
    assert unchanged_snapshot(store) == before
