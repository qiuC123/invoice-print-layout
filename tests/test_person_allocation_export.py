from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

import pytest

import invoice_print_layout.reimbursement as reimbursement
from invoice_print_layout.report_excel import TEMPLATE_NAME, create_excel
from invoice_print_layout.report_snapshot import snapshot
from invoice_print_layout.report_types import validate_report_choice
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


def expense(key: str, cents: int, person_id: str = '', label: str = '', **changes: Any) -> dict[str, Any]:
    return {'id': key, 'project': 'Synthetic project', 'project_id': 'synthetic-project',
            'title': 'Synthetic hotel', 'category': '酒店', 'merchant': 'Synthetic hotel company',
            'expense_date': '2026-01-01', 'amount_cents': cents, 'amount': f'{cents/100:.2f}',
            'pages': '1', 'order_number': '', 'alternative': False, 'stage': 'draft',
            'reimbursement_person_id': person_id, 'reimbursement_person_label': label, **changes}


def test_snapshot_allocation_preserves_company_grouping_and_original_input() -> None:
    items = [expense('a', 1001, 'person-a', 'Synthetic A'),
             expense('b', 2002, 'person-b', 'Synthetic B'),
             expense('c', 305, 'person-a', 'Synthetic A',
                     **validate_report_choice('living', '现场饮用水、夜宵')),
             expense('d', 407)]
    before = deepcopy(items)
    result = snapshot(items)
    plain = [{key: value for key, value in item.items() if not key.startswith('reimbursement_person_')}
             for item in items]
    baseline = snapshot(plain)
    assert result['rows'] == baseline['rows']
    assert result['row_labels'] == baseline['row_labels']
    assert result['total_cents'] == baseline['total_cents'] == 3715
    rows = {row['person_id']: row for row in result['person_allocation']['rows']}
    assert rows['person-a']['total_cents'] == 1306 and rows['person-a']['expense_count'] == 2
    assert rows['person-b']['total_cents'] == 2002 and rows['person-b']['expense_count'] == 1
    assert rows['']['total_cents'] == 407 and rows['']['label'] == '未指定'
    assert sum(row['total_cents'] for row in rows.values()) == result['total_cents']
    assert result['person_allocation']['expense_count'] == 4
    assert [item['id'] for item in rows['person-a']['items']] == ['a', 'c']
    assert rows['person-a']['items'][1]['display_name'] == '现场饮用水、夜宵'
    assert [item['id'] for item in rows['']['items']] == ['d']
    assert all(sum(item['amount_cents'] for item in row['items']) == row['total_cents'] for row in rows.values())
    assert 'reference_settlement' not in result['person_allocation']
    assert items == before


def test_snapshot_uses_one_active_expense_basis_for_main_and_person_amounts() -> None:
    active = expense('active', 1001, 'person-a', 'Synthetic A', attachments=[{}, {}, {}])
    inactive = [expense('deleted', 5000, deleted=True),
                expense('merged', 5000, merged_into='active'),
                expense('cancelled', 5000, stage='cancelled'),
                expense('returned', 5000, returned_to_synthesis=True),
                expense('archived', 5000, synthesis_material_state='archived')]
    result = snapshot([active, active, *inactive])
    assert result['items'] == [active]
    assert result['total_cents'] == result['person_allocation']['total_cents'] == 1001
    assert result['person_allocation']['expense_count'] == 1
    assert [item['id'] for item in result['person_allocation']['rows'][0]['items']] == ['active']
    assert result['rows'][0]['items'][0]['detail_row'] == 2
    assert sum(row['amount_cents'] for row in result['rows']) == 1001


def test_person_manifest_keeps_unassigned_amount_separate_from_leader() -> None:
    result = snapshot([expense('a', 1200, 'person-a', 'Synthetic | A\nnext'), expense('b', 803)])
    lines = '\n'.join(reimbursement.person_allocation_lines(result))
    assert '| Synthetic / A next | 1 | 12.00 |' in lines
    assert '| 未指定 | 1 | 8.03 |' in lines
    assert '| 合计 | 2 | 20.03 |' in lines
    assert '每人费用清单与金额汇总' in lines
    assert '| a |' not in lines and '| b |' not in lines
    assert '结算方式' not in lines and '垫付' not in lines
    assert '| 费用归属人 |' in lines and '| 垫付人 |' not in lines
    result['person_allocation']['total_cents'] += 1
    with pytest.raises(ValueError, match='不一致'):
        reimbursement.person_allocation_lines(result)


@pytest.mark.parametrize('state', [{'merged_into': 'synthetic-target'},
                                  {'synthesis_material_state': 'archived'},
                                  {'synthesis_material_state': 'deleted'}])
def test_report_rejects_inactive_source_before_creating_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: dict[str, str]) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item = expense('inactive', 1000, ready=True, complete=True, **state)
    monkeypatch.setattr(store, 'get', lambda _: item)
    with pytest.raises(ValueError, match='未提交'):
        reimbursement.create_report(store, ['inactive'])
    assert not (store.root / '导出').exists()


def test_report_manifest_exports_attribution_without_replacing_leader_or_extra_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(reimbursement, 'validate_template', lambda _: None)
    monkeypatch.setattr(reimbursement, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))
    received: list[dict[str, Any]] = []
    def excel(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path) -> None:
        received.append({'options': dict(options), 'items': deepcopy(items)})
        output.write_bytes(b'synthetic excel')
    monkeypatch.setattr(reimbursement, 'create_excel', excel)
    items = []
    for index, amount in enumerate(('12.00', '8.03')):
        item = store.create({'title': 'Synthetic hotel ' + str(index), 'category': '酒店', 'amount': amount})
        source = make_invoice_pdf(tmp_path / f'invoice-{index}.pdf', amount=amount,
                                  invoice_number=str(10000000000000000000 + index))
        store.add_attachment(item['id'], source.name, source.read_bytes(), 'invoice')
        items.append(store.get(item['id']))
    original_get = store.get
    def projected_get(key: str) -> dict[str, Any]:
        item = original_get(key)
        item.update(project_id='synthetic-project', project='Synthetic project')
        if key == items[0]['id']:
            item.update(reimbursement_person_id='synthetic-person', reimbursement_person_label='Synthetic payer')
        return item
    monkeypatch.setattr(store, 'get', projected_get)
    before = [store.get(item['id']) for item in items]
    result = reimbursement.create_report(store, [item['id'] for item in items], {'person': 'Synthetic leader'}, submit=False)
    assert set(result) == {'pdf', 'md', 'xlsx'}
    assert len(list((store.root / '导出').iterdir())) == 3
    assert received[0]['options']['person'] == 'Synthetic leader'
    text = (store.root / '导出' / result['md']).read_text(encoding='utf-8')
    assert '报销人：Synthetic leader' in text and '| 费用归属人 |' in text
    assert '| Synthetic payer | 1 | 12.00 |' in text and '| 未指定 | 1 | 8.03 |' in text
    assert '| 合计 | 2 | 20.03 |' in text
    assert all(text.count('| ' + item['id'] + ' |') == 1 for item in items)
    assert '结算方式' not in text and '垫付' not in text
    assert [store.get(item['id']) for item in items] == before


def excel_cell(archive: zipfile.ZipFile, sheet: int, address: str) -> tuple[str, str | None]:
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    root = ET.fromstring(archive.read(f'xl/worksheets/sheet{sheet}.xml'))
    cell = root.find(f'.//s:c[@r="{address}"]', ns)
    assert cell is not None
    formula = cell.find('s:f', ns)
    if cell.get('t') == 's':
        value = cell.find('s:v', ns)
        assert value is not None
        strings = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        return ''.join(strings[int(value.text or '0')].itertext()), formula.text if formula is not None else None
    value = cell.find('s:is', ns) if cell.get('t') == 'inlineStr' else cell.find('s:v', ns)
    return ''.join(value.itertext()) if value is not None else '', formula.text if formula is not None else None


def test_company_main_sheet_identical_with_person_detail_column(tmp_path: Path) -> None:
    raw = os.environ.get('INVOICE_TEST_REPORT_TEMPLATE')
    if not raw:
        pytest.skip('Set INVOICE_TEST_REPORT_TEMPLATE for private-template integration')
    template = Path(raw)
    original = template.read_bytes()
    options = {'person': 'Synthetic leader', 'payment': 'personal', 'period': '2026-01'}
    items = [expense('a', 1234, 'person-a', 'Synthetic A'),
             expense('b', 567, 'person-b', '=Synthetic B'), expense('c', 890)]
    plain = [{key: value for key, value in item.items() if not key.startswith('reimbursement_person_')}
             for item in items]
    baseline, attributed = tmp_path / 'baseline.xlsx', tmp_path / 'attributed.xlsx'
    create_excel(template, plain, options, baseline)
    create_excel(template, items, options, attributed)
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(baseline) as old, zipfile.ZipFile(attributed) as current:
        assert old.read('xl/worksheets/sheet1.xml') == current.read('xl/worksheets/sheet1.xml')
        workbook = ET.fromstring(current.read('xl/workbook.xml'))
        assert len(workbook.findall('s:sheets/s:sheet', ns)) == 2
        assert excel_cell(current, 2, 'M1') == ('费用归属人', None)
        assert excel_cell(current, 2, 'M2') == ('Synthetic A', None)
        assert excel_cell(current, 2, 'M3')[0].lstrip("'") == '=Synthetic B'
        assert excel_cell(current, 2, 'M3')[1] is None
        assert excel_cell(current, 2, 'M4') == ('未指定', None)
        assert round(float(excel_cell(current, 1, 'H35')[0]) * 100) == 2691
        assert round(float(excel_cell(current, 2, 'H5')[0]) * 100) == 2691
        areas = [entry.text for entry in workbook.findall('s:definedNames/s:definedName', ns)
                 if entry.get('name') == '_xlnm.Print_Area']
        assert "'报销明细'!$A$1:$M$5" in areas
        assert 'Synthetic leader' in excel_cell(current, 1, 'A4')[0]
    assert template.read_bytes() == original
    assert not list(tmp_path.glob('*.input.json'))
    assert not list(tmp_path.glob('*.printing.xlsx'))
