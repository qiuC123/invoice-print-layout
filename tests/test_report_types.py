from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

import pytest

from invoice_print_layout.report_excel import create_excel, validate_template
from invoice_print_layout.report_snapshot import REPORT_SECTIONS, snapshot
from invoice_print_layout.report_types import (FIXED_ITEMS, REPORT_LABELS, infer_report_choice,
                                               report_types, validate_report_choice)


def matter(key: str, cents: int = 100, **fields: Any) -> dict[str, Any]:
    return {'id': key, 'title': key, 'project': 'Synthetic project', 'project_id': 'synthetic-project',
            'category': '其他', 'merchant': '', 'amount_cents': cents, 'expense_date': '',
            'order_number': '', 'pages': '1', 'material_basis': 'invoice_payment', **fields}


def chosen(kind: str, name: str, key: str, cents: int = 100) -> dict[str, Any]:
    return matter(key, cents, **validate_report_choice(kind, name))


def test_hierarchy_contains_exact_template_sections_and_all_fixed_rows() -> None:
    hierarchy = report_types()
    assert [section['label'] for section in hierarchy] == ['拓展', '车费', '生活费', '人员', '现场购材料', '其他']
    assert [section['editable_name'] for section in hierarchy] == [False] * 4 + [True] * 2
    assert [len(section['names']) for section in hierarchy] == [2, 6, 4, 4, 0, 0]
    assert [row for section in hierarchy for row in section['rows']] == list(range(8, 35))
    assert set(REPORT_LABELS) == set(range(8, 24))
    assert len(json.loads(json.dumps(hierarchy, ensure_ascii=False))) == 6
    hierarchy[0]['names'].append('Never persist to shared contract')
    assert 'Never persist to shared contract' not in report_types()[0]['names']


@pytest.mark.parametrize('kind,name,row,category,section', FIXED_ITEMS)
def test_fixed_choices_roundtrip_to_the_actual_summary_row(kind: str, name: str, row: int,
                                                          category: str, section: str) -> None:
    choice = validate_report_choice(kind, name)
    assert choice['project_type'] == kind and choice['project_name'] == name
    assert choice['category'] == category and choice['report_section'] == section
    assert choice['title'] == choice['display_name'] == name and section in REPORT_SECTIONS
    item = matter('row-' + str(row), **choice)
    before = deepcopy(item)
    report = snapshot([item])
    assert report['rows'][0]['row'] == row and report['rows'][0]['name'] == name
    assert report['rows'][0]['amount_cents'] == report['total_cents'] == 100
    assert report['row_labels'][row] == name
    assert infer_report_choice(item) == infer_report_choice({'category': category, 'report_section': section}) == choice
    assert item == before


@pytest.mark.parametrize('kind', ['materials', 'other'])
def test_only_open_sections_allow_a_trimmed_custom_project_name(kind: str) -> None:
    name = '现场材料及服务明细'
    choice = validate_report_choice(kind, '  ' + name + '  ')
    assert choice['project_name'] == name and choice['report_section'] == kind
    assert validate_report_choice(kind, 'X' * 200)['project_name'] == 'X' * 200
    with pytest.raises(ValueError, match='200'):
        validate_report_choice(kind, 'X' * 201)


@pytest.mark.parametrize('kind,name', [('', '名称'), ('unknown', '名称'), (None, '名称'),
                                      ('transport', ''), ('other', ' '), ('materials', 14),
                                      ('personnel', '任意人员费'), ('living', '住宿费'),
                                      ('expansion', '火车、飞机'), ('transport', '施工押金(可退)')])
def test_invalid_or_cross_section_name_rejected(kind: Any, name: Any) -> None:
    with pytest.raises(ValueError):
        validate_report_choice(kind, name)


def test_label_input_normalizes_to_code_and_legacy_machine_suggestions_are_read_only() -> None:
    assert validate_report_choice('生活费', '住宿费(附清单)')['project_type'] == 'living'
    legacy = [matter('铁路', category='高铁'), matter('出租', category='打车'),
              matter('住宿', category='酒店'), matter('餐饮', category='餐饮'),
              matter('口罩5个', category='材料采购'), matter('普通电子发票', merchant='顺丰速运公司'),
              matter('奶茶', category='外卖'), matter('未知内容')]
    before = deepcopy(legacy)
    suggestions = [infer_report_choice(item) for item in legacy]
    assert [item['project_type'] for item in suggestions] == ['transport', 'transport', 'living', 'living',
                                                            'materials', 'other', 'living', 'other']
    assert [item['project_name'] for item in suggestions] == ['火车、飞机', '打的(出租车、滴滴等)', '住宿费(附清单)',
                                                            '现场饮用水、夜宵', '口罩采购', '顺丰同城',
                                                            '现场饮用水、夜宵', '未知内容']
    assert legacy == before
    assert infer_report_choice({'category': '其他', 'title': ''})['project_name'] == ''


def test_all_fixed_and_open_rows_are_filled_once_without_losing_overflow_or_grouped_amounts() -> None:
    items = [chosen(kind, name, f'fixed-{row}', row) for kind, name, row, _, _ in FIXED_ITEMS]
    items += [chosen('materials', 'Synthetic material ' + str(i), 'material-' + str(i), 100 + i) for i in range(7)]
    items += [chosen('other', 'Synthetic service ' + str(i), 'other-' + str(i), 200 + i) for i in range(8)]
    items += [chosen('materials', 'Synthetic material 0', 'same-name-extra', 50)]
    before = deepcopy(items)
    report = snapshot(items)
    rows = {row['row']: row for row in report['rows']}
    assert set(rows) == set(range(8, 35))
    assert rows[24]['amount_cents'] == 150 and len(rows[24]['items']) == 2
    assert rows[28]['amount_cents'] == 104 + 105 + 106
    assert rows[34]['amount_cents'] == 205 + 206 + 207
    assert report['total_cents'] == sum(item['amount_cents'] for item in items) == sum(row['amount_cents'] for row in rows.values())
    ids = [item['id'] for row in rows.values() for item in row['items']]
    assert len(ids) == len(set(ids)) == len(items)
    assert items == before


def test_new_open_rows_keep_historical_row_placement_and_sf_reservation() -> None:
    old = [matter('rain-1', 8870, category='材料采购', title='雨衣33件'),
           matter('rain-2', 2870, category='材料采购', title='雨衣另规格'),
           matter('sf', 4050, category='顺丰'), matter('old-other', 50),
           matter('unknown', 75, category='用户旧自定义类'), matter('old-meal', 100, category='外卖', title='咖啡')]
    baseline = snapshot(old)
    rows = {row['row']: row for row in baseline['rows']}
    assert rows[24]['amount_cents'] == 11740 and rows[24]['name'] == '雨衣采购'
    assert rows[29]['amount_cents'] == 4050 and rows[30]['amount_cents'] == 125 and rows[19]['amount_cents'] == 100
    result = snapshot([chosen('other', 'Synthetic new service', 'new-service'),
                       chosen('materials', 'Synthetic new material', 'new-material'), *old])
    by_id = {item['id']: row['row'] for row in result['rows'] for item in row['items']}
    assert by_id['new-service'] == 31 and by_id['new-material'] == 25
    assert all(by_id[item['id']] == row['row'] for row in baseline['rows'] for item in row['items'])


def synthetic_template(path: Path, replacement: dict[str, str] | None = None) -> Path:
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    cells = {'A1': '活动运营费用统计表', 'B35': '费用合计：', 'D6': '实际消费', 'H6': '需报销'}
    cells.update({f'B{row}': label for row, label in REPORT_LABELS.items()})
    cells.update({f"A{section['rows'][0]}": section['label'] for section in report_types()})
    cells.update(replacement or {})
    root = ET.Element('worksheet', xmlns=ns)
    data = ET.SubElement(root, 'sheetData')
    for address, value in cells.items():
        row = ET.SubElement(data, 'row', r=re.sub(r'\D', '', address))
        cell = ET.SubElement(row, 'c', r=address, t='inlineStr')
        ET.SubElement(ET.SubElement(cell, 'is'), 't').text = value
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', ET.tostring(root, encoding='utf-8'))
    return path


def test_template_validator_checks_the_actual_hierarchy_without_mutating_source(tmp_path: Path) -> None:
    template = synthetic_template(tmp_path / 'synthetic.xlsx', {'B13': '打的(出租车、滴滴\n等)'})
    original = template.read_bytes()
    validate_template(template)
    assert template.read_bytes() == original
    for replacement in ({'B13': '运输费用'}, {'B18': '不存在的生活子项'}, {'A20': '其他人员分类'}):
        bad = synthetic_template(tmp_path / 'wrong.xlsx', replacement)
        with pytest.raises(ValueError, match='分类不同'):
            validate_template(bad)


def test_company_template_all_fixed_and_dynamic_excel_rows(tmp_path: Path) -> None:
    raw = os.environ.get('INVOICE_TEST_REPORT_TEMPLATE')
    if not raw:
        pytest.skip('Set INVOICE_TEST_REPORT_TEMPLATE for blank company-template integration')
    source = Path(raw)
    original = source.read_bytes()
    items = [chosen(kind, name, f'synthetic-{row}', row * 100 + 1) for kind, name, row, _, _ in FIXED_ITEMS]
    items += [chosen('materials', f'Synthetic material {i}', f'm-{i}', (24 + i) * 100 + 1) for i in range(5)]
    items += [chosen('other', f'Synthetic service {i}', f'o-{i}', (29 + i) * 100 + 1) for i in range(6)]
    output = tmp_path / 'synthetic-types.xlsx'
    create_excel(source, items, {'person': 'Synthetic person', 'payment': 'personal', 'period': '2026-01'}, output)
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(output) as archive:
        root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        for row in range(8, 35):
            cell = root.find(f'.//s:c[@r="H{row}"]', ns)
            assert cell is not None and cell.find('s:f', ns) is not None
            value = cell.find('s:v', ns)
            assert value is not None and round(float(value.text or '0') * 100) == row * 100 + 1
            for column in 'CDEFGIJKLM':
                unused = root.find(f'.//s:c[@r="{column}{row}"]', ns)
                assert unused is None or all(unused.find('s:' + child, ns) is None for child in ('v', 'f', 'is'))
        total = root.find('.//s:c[@r="H35"]/s:v', ns)
        assert total is not None and round(float(total.text or '0') * 100) == sum(item['amount_cents'] for item in items)
        setup = root.find('s:pageSetup', ns)
        assert setup is not None and setup.get('fitToHeight') == '1' and setup.get('paperSize') == '9'
    assert source.read_bytes() == original
