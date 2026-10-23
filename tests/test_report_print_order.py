from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

import pymupdf
import pytest

import invoice_print_layout.reimbursement as reimbursement
from invoice_print_layout.report_excel import TEMPLATE_NAME, create_excel
from invoice_print_layout.report_snapshot import material_batches, snapshot
from invoice_print_layout.report_types import validate_report_choice
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items


def expense(key: str, category: str, *, project: str = 'A', **changes: Any) -> dict[str, Any]:
    return {'id': key, 'title': key, 'project': project, 'project_id': '',
            'category': category, 'merchant': '', 'expense_date': '',
            'amount_cents': 123, 'amount': '1.23', 'stage': 'draft',
            'material_basis': 'standard', 'alternative': False, 'order_number': '',
            'pages': '1', **changes}


def test_snapshot_orders_template_rows_and_keeps_project_materials_together() -> None:
    items = [expense('hotel-a1', '酒店'), expense('taxi-a1', '打车'),
             expense('rail-a1', '高铁'), expense('hotel-b1', '酒店', project='B'),
             expense('taxi-a2', '打车'), expense('rail-a2', '高铁'),
             expense('hotel-a2', '酒店')]
    before = deepcopy(items)
    result = snapshot(items)
    expected = ['rail-a1', 'rail-a2', 'taxi-a1', 'taxi-a2',
                'hotel-a1', 'hotel-a2', 'hotel-b1']
    assert result['ordered_item_ids'] == expected
    assert [item['id'] for item in result['items']] == expected
    assert [row['row'] for row in result['rows']] == [12, 13, 16]
    assert [item['id'] for row in result['rows'] for item in row['items']] == expected
    assert [item['detail_row'] for row in result['rows'] for item in row['items']] == list(range(2, 9))
    assert result['total_cents'] == 7 * 123
    assert result['person_allocation']['total_cents'] == result['total_cents']
    assert items == before


def test_snapshot_legacy_dynamic_overflow_is_complete_and_idempotent() -> None:
    material = lambda key, name: {**expense(key, '材料采购'), **validate_report_choice('materials', name)}
    other = lambda key, name: {**expense(key, '其他'), **validate_report_choice('other', name)}
    items = [material('new-material-x1', 'Material X'), other('new-other-x1', 'Other X'),
             *[expense(f'legacy-material-{i}', '材料采购', report_group=f'legacy-m-{i}') for i in range(7)],
             *[expense(f'legacy-other-{i}', '其他', report_group=f'legacy-o-{i}') for i in range(7)],
             material('new-material-y', 'Material Y'), other('new-other-y', 'Other Y'),
             expense('train-last', '高铁'), material('new-material-x2', 'Material X'),
             expense('legacy-material-5b', '材料采购', report_group='legacy-m-5'),
             other('new-other-x2', 'Other X')]
    before = deepcopy(items)
    report = snapshot(items)
    again = snapshot(report['items'])
    assert report['ordered_item_ids'] == again['ordered_item_ids']
    assert report['rows'] == again['rows']
    assert report['material_groups'] == again['material_groups']
    ids = report['ordered_item_ids']
    assert len(ids) == len(items) == len(set(ids))
    assert set(ids) == {item['id'] for item in items}
    assert ids[0] == 'train-last'
    rows = {item['id']: row['row'] for row in report['rows'] for item in row['items']}
    for i in range(7):
        assert rows[f'legacy-material-{i}'] == 24 + min(i, 4)
        assert rows[f'legacy-other-{i}'] == 30 + min(i, 4)
    assert rows['new-material-x1'] == rows['new-material-y'] == 28
    assert rows['new-other-x1'] == rows['new-other-x2'] == 29
    assert rows['new-other-y'] == 34
    assert ids.index('new-material-x2') == ids.index('new-material-x1') + 1
    assert ids.index('legacy-material-5b') == ids.index('legacy-material-5') + 1
    assert ids.index('new-other-x2') == ids.index('new-other-x1') + 1
    assert [item['detail_row'] for row in report['rows'] for item in row['items']] == list(range(2, len(items) + 2))
    assert sum(row['amount_cents'] for row in report['rows']) == report['total_cents'] == len(items) * 123
    assert items == before


@pytest.mark.parametrize(('kind', 'category', 'first_row'), [
    ('materials', '材料采购', 24), ('other', '其他', 30)])
@pytest.mark.parametrize('explicit_first', [True, False])
def test_same_explicit_name_and_legacy_group_keep_reserved_rows_when_resnapshotted(
        kind: str, category: str, first_row: int, explicit_first: bool) -> None:
    explicit = {**expense('new-a', category), **validate_report_choice(kind, 'A')}
    legacy_a = expense('old-a', category, report_group='A')
    legacy_b = expense('old-b', category, report_group='B')
    items = [explicit, legacy_a, legacy_b] if explicit_first else [legacy_a, legacy_b, explicit]
    before = deepcopy(items)
    report = snapshot(items)
    again = snapshot(report['items'])
    grouped_ids = ['new-a', 'old-a'] if explicit_first else ['old-a', 'new-a']
    assert report['ordered_item_ids'] == grouped_ids + ['old-b']
    assert [(row['row'], [item['id'] for item in row['items']]) for row in report['rows']] == [
        (first_row, grouped_ids), (first_row + 1, ['old-b'])]
    assert [item['detail_row'] for row in report['rows'] for item in row['items']] == [2, 3, 4]
    assert again['ordered_item_ids'] == report['ordered_item_ids']
    assert again['rows'] == report['rows']
    assert again['material_groups'] == report['material_groups']
    assert again['total_cents'] == report['total_cents'] == 369
    assert items == before


def marked_pdf(label: str) -> bytes:
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=700)
        page.insert_text((20, 30), f'BEGIN {label}')
        page.insert_text((20, 680), f'END {label}')
        return pdf.tobytes()


def test_payment_batches_separate_expenses_and_preserve_canonical_project_order() -> None:
    items = [expense('a1', '酒店', project='Same name', project_id='project-a', material_basis='payment_only'),
             expense('b1', '酒店', project='Same name', project_id='project-b', material_basis='payment_only'),
             expense('a2', '酒店', project='Same name', project_id='project-a', material_basis='payment_only')]
    assert [[item['id'] for item in batch] for batch in material_batches(snapshot(items))] == [
        ['a1'], ['a2'], ['b1']]


def stored_expense(store: ExpenseStore, label: str, category: str, *,
                   project: str = 'A', payment: bool = True, group: str = '',
                   verified: bool = True) -> dict[str, Any]:
    item = store.create({'title': label, 'category': category, 'project': project,
                         'amount': '1.23', 'expense_date': '',
                         'material_basis': 'payment_only' if payment else 'standard',
                         'report_group': group, 'display_name': group})
    store.add_attachment(item['id'], label + '.pdf', marked_pdf(label), 'payment' if payment else 'invoice')
    return store.transition(item['id'], 'verify') if verified else store.get(item['id'])


@pytest.mark.parametrize('mode', ['trial', 'formal', 'legacy'])
def test_pdf_manifest_and_excel_follow_report_order_without_cross_group_pages(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    verified = mode != 'trial'
    specs = [
        ('CABLE_ONE', '材料采购', 'A', True, 'Cable'),
        ('HOTEL_A_ONE', '酒店', 'A', True, ''),
        ('TAXI', '打车', 'A', True, ''),
        ('USB', '材料采购', 'A', True, 'USB'),
        ('RAIL', '高铁', 'A', False, ''),
        ('HOTEL_INVOICE', '酒店', 'A', False, ''),
        ('HOTEL_A_TWO', '酒店', 'A', True, ''),
        ('HOTEL_B', '酒店', 'B', True, ''),
        ('HOTEL_A_THREE', '酒店', 'A', True, ''),
        ('CABLE_TWO', '材料采购', 'A', True, 'Cable'),
    ]
    items = [stored_expense(store, label, category, project=project, payment=payment,
                            group=group, verified=verified)
             for label, category, project, payment, group in specs]
    by_label = {item['title']: item for item in items}
    expected_pages = [['RAIL'], ['TAXI'], ['HOTEL_A_ONE'], ['HOTEL_INVOICE'],
                      ['HOTEL_A_TWO'], ['HOTEL_A_THREE'], ['HOTEL_B'],
                      ['CABLE_ONE'], ['CABLE_TWO'], ['USB']]
    expected_labels = [label for page in expected_pages for label in page]
    expected_ids = [by_label[label]['id'] for label in expected_labels]
    original_files = {path: path.read_bytes() for path in store.files.iterdir()}
    before = store.list_items()
    excel_items: list[dict[str, Any]] = []
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(reimbursement, 'validate_template', lambda _: None)
    monkeypatch.setattr(reimbursement, 'spreadsheet_runtime', lambda: None)

    def capture_excel(template: Path, rows: list[dict[str, Any]],
                      options: dict[str, str], output: Path) -> None:
        excel_items.extend(deepcopy(rows))
        output.write_bytes(b'synthetic excel')

    monkeypatch.setattr(reimbursement, 'create_excel', capture_excel)
    ids = [item['id'] for item in items]
    result = (export_items(store, ids) if mode == 'legacy' else
              reimbursement.create_report(store, ids, submit=mode == 'formal'))
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == len(expected_pages)
        for page, labels in zip(pdf, expected_pages, strict=True):
            text = page.get_text()
            assert [line.removeprefix('BEGIN ') for line in text.splitlines() if line.startswith('BEGIN ')] == labels
            assert [line.removeprefix('END ') for line in text.splitlines() if line.startswith('END ')] == labels
            assert not page.get_images(), 'PDF originals must remain vector-based'
            assert abs(page.rect.width - 595.28) < 1 and abs(page.rect.height - 841.89) < 1
    markdown = (store.root / '导出' / result['md']).read_text(encoding='utf-8')
    detail_lines = [line for line in markdown.splitlines()
                    if line.startswith('| ') and line.split('|')[1].strip() in expected_ids]
    assert [line.split('|')[1].strip() for line in detail_lines] == expected_ids
    if mode != 'legacy':
        assert [item['id'] for item in excel_items] == expected_ids
        page_by_id = {by_label[label]['id']: str(number)
                      for number, labels in enumerate(expected_pages, 1) for label in labels}
        assert {item['id']: item['pages'] for item in excel_items} == page_by_id
        assert {line.split('|')[1].strip(): line.split('|')[7].strip()
                for line in detail_lines} == page_by_id
    assert all(path.read_bytes() == content for path, content in original_files.items())
    if mode == 'formal':
        assert all(item['stage'] == 'submitted' and item['report_files'] == result for item in store.list_items())
    else:
        assert store.list_items() == before


def test_excel_detail_rows_and_summary_formulas_follow_template_order(tmp_path: Path) -> None:
    template = os.environ.get('INVOICE_TEST_REPORT_TEMPLATE')
    if not template:
        pytest.skip('Set INVOICE_TEST_REPORT_TEMPLATE for private-template integration')
    source = Path(template)
    before = source.read_bytes()
    items = [expense('hotel-one', '酒店'), expense('taxi-one', '打车'),
             expense('rail-one', '高铁'), expense('hotel-two', '酒店')]
    output = tmp_path / 'ordered-report.xlsx'
    create_excel(source, items, {'person': 'Test', 'payment': 'personal', 'period': '2026-10'}, output)
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(output) as archive:
        strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        detail = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        main = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))

        def cell_text(ref: str) -> str:
            cell = detail.find(f'.//s:c[@r="{ref}"]', ns)
            assert cell is not None
            value = cell.find('s:v', ns)
            if cell.get('t') == 's':
                assert value is not None
                return ''.join(strings[int(value.text or '0')].itertext())
            return ''.join(cell.itertext())

        assert [cell_text(f'B{row}') for row in range(2, 6)] == [
            'rail-one', 'taxi-one', 'hotel-one', 'hotel-two']
        for row, expected in [(12, "SUM('报销明细'!H2)"), (13, "SUM('报销明细'!H3)"),
                              (16, "SUM('报销明细'!H4,'报销明细'!H5)")]:
            formula = main.find(f'.//s:c[@r="H{row}"]/s:f', ns)
            assert formula is not None and formula.text == expected
    assert source.read_bytes() == before


def test_payment_subitems_sharing_overflow_row_still_have_separate_pages(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    items = [stored_expense(store, f'GROUP_{i}_ONE', '材料采购', group=f'Group {i}') for i in range(6)]
    items += [stored_expense(store, 'GROUP_4_TWO', '材料采购', group='Group 4'),
              stored_expense(store, 'GROUP_5_TWO', '材料采购', group='Group 5')]
    report = snapshot(items)
    overflow = next(row for row in report['rows'] if row['row'] == 28)
    assert len(overflow['items']) == 4
    result = export_items(store, [item['id'] for item in items])
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == 8
        expected_labels = [f'GROUP_{index}_ONE' for index in range(4)] + [
            'GROUP_4_ONE', 'GROUP_4_TWO', 'GROUP_5_ONE', 'GROUP_5_TWO']
        for page, label in zip(pdf, expected_labels, strict=True):
            assert [line.removeprefix('BEGIN ') for line in page.get_text().splitlines()
                    if line.startswith('BEGIN ')] == [label]
