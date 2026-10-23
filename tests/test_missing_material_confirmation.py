"""Missing proof facts survive explicit, versioned human review and exports."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator
import zipfile
from xml.etree import ElementTree as ET

import pymupdf
import pytest

import invoice_print_layout.reimbursement as report
import invoice_print_layout.workbench as workbench
import invoice_print_layout.workbench_export as legacy_export
from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout.report_excel import TEMPLATE_NAME, create_excel
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from invoice_print_layout.workbench_web import make_server
from tests.helpers import make_trip_pdf
from tests.test_person_allocation_export import excel_cell


def _proof(label: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=595.28, height=410)
        page.insert_text((40, 80), label, fontsize=15)
        page.draw_rect(pymupdf.Rect(25, 30, 570, 370))
        return document.tobytes()


def _expense(store: ExpenseStore, title: str = 'Synthetic expense', *,
             proof: bool = True, amount: str = '15.00') -> dict[str, Any]:
    item = store.create({'title': title, 'category': '餐饮', 'project': 'Synthetic project',
                         'amount': amount, 'material_basis': 'invoice_payment'})
    if proof:
        item = store.add_attachment(item['id'], 'invoice.pdf', _proof(title), 'invoice')
    return item


def _acknowledge(store: ExpenseStore, item: dict[str, Any]) -> dict[str, Any]:
    return store.transition(item['id'], 'verify', expected_version=item['version'],
                            acknowledge_missing=True, expected_missing=item['missing'])


def _ledger(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                for table in ('expenses', 'attachments', 'events')}


@contextmanager
def _server(store: ExpenseStore) -> Iterator[tuple[int, str]]:
    server = make_server(store.workspace, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection('127.0.0.1', server.server_port)
    try:
        connection.request('GET', '/api/state')
        response = connection.getresponse()
        assert response.status == 200
        token = json.loads(response.read())['token']
        yield server.server_port, token
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(port: int, token: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    connection = HTTPConnection('127.0.0.1', port)
    try:
        connection.request('POST', '/api/transition', body=json.dumps(body),
                           headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('proof', [False, True])
def test_acknowledged_missing_proofs_become_ready_without_becoming_complete(
        tmp_path: Path, proof: bool) -> None:
    store = ExpenseStore(tmp_path)
    item = _expense(store, proof=proof)
    originals = {path.name: path.read_bytes() for path in store.files.iterdir()}
    assert not item['complete'] and item['missing_acknowledgement_allowed']
    with pytest.raises(ValueError, match='补齐材料'):
        store.transition(item['id'], 'verify', expected_version=item['version'])
    reviewed = _acknowledge(store, item)
    assert reviewed['verified'] and reviewed['ready'] and reviewed['missing_acknowledged']
    assert not reviewed['complete'] and not reviewed['pending_review']
    assert reviewed['missing'] == item['missing']
    assert reviewed['amount_cents'] == item['amount_cents']
    assert reviewed['attachments'] == item['attachments']
    assert any('已确认仍缺' in event['message'] for event in reviewed['events'])
    assert ExpenseStore(tmp_path).get(item['id']) == reviewed
    submitted = store.transition(item['id'], 'submitted', expected_version=reviewed['version'])
    assert submitted['ready'] and submitted['missing_acknowledged'] and not submitted['complete']
    assert submitted['missing'] == item['missing']
    assert {path.name: path.read_bytes() for path in store.files.iterdir()} == originals


@pytest.mark.parametrize('changes', [
    {'acknowledge_missing': False}, {'acknowledge_missing': 'true'},
    {'acknowledge_missing': 1}, {'acknowledge_missing': None},
    {'expected_version': None}, {'expected_version': ''}, {'expected_version': 'stale'},
    {'expected_missing': None}, {'expected_missing': []}, {'expected_missing': ['Wrong missing proof']},
    {'expected_missing': [1]},
])
def test_invalid_or_stale_missing_acknowledgements_do_not_write(
        tmp_path: Path, changes: dict[str, Any]) -> None:
    store = ExpenseStore(tmp_path)
    item = _expense(store)
    arguments = {'expected_version': item['version'], 'acknowledge_missing': True,
                 'expected_missing': item['missing'], **changes}
    before = _ledger(store)
    with pytest.raises(ValueError):
        store.transition(item['id'], 'verify', **arguments)
    assert _ledger(store) == before
    assert store.get(item['id']) == item


@pytest.mark.parametrize('category,amount,basis', [
    ('餐饮', '', 'invoice_payment'), ('Unknown material configuration', '15', 'standard'),
])
def test_missing_confirmation_cannot_invent_amount_or_material_requirements(
        tmp_path: Path, category: str, amount: str, basis: str) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Unresolved expense', 'category': category,
                         'amount': amount, 'material_basis': basis})
    assert not item['missing_acknowledgement_allowed']
    before = _ledger(store)
    with pytest.raises(ValueError):
        _acknowledge(store, item)
    assert _ledger(store) == before


@pytest.mark.parametrize('change', ['missing-file', 'changed-file'])
def test_first_confirmation_cannot_silently_adopt_a_source_changed_after_opening(
        tmp_path: Path, change: str) -> None:
    store = ExpenseStore(tmp_path)
    opened = _expense(store)
    path, _ = store.attachment_path(opened['attachments'][0]['id'])
    if change == 'missing-file':
        path.unlink()
    else:
        path.write_bytes(_proof('REPLACED BEFORE FIRST CONFIRMATION'))
    before = _ledger(store)
    with pytest.raises(ValueError, match='已变化'):
        _acknowledge(store, opened)
    assert _ledger(store) == before
    refreshed = store.get(opened['id'])
    assert refreshed['version'] != opened['version']
    assert _acknowledge(store, refreshed)['ready']


def test_inferred_native_trip_role_does_not_conflict_with_the_stored_source_snapshot(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Native trip without invoice', 'category': '打车', 'amount': '123.45',
                         'material_basis': 'invoice_payment'})
    trip = make_trip_pdf(tmp_path / 'native-trip.pdf')
    item = store.add_attachment(item['id'], 'unclassified-original.pdf', trip.read_bytes(), 'other')
    assert item['attachments'][0]['role'] == 'trip' and item['missing'] == ['发票']
    reviewed = _acknowledge(store, item)
    assert reviewed['ready'] and reviewed['missing_acknowledged']
    assert reviewed['attachments'][0]['role'] == 'trip' and not reviewed['complete']
    assert ExpenseStore(tmp_path).get(item['id']) == reviewed


def test_confirmation_never_adopts_a_source_replaced_inside_its_own_transaction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    opened = _expense(store)
    path, _ = store.attachment_path(opened['attachments'][0]['id'])
    snapshot = workbench.source_snapshot
    calls = 0
    def change_on_second_snapshot(attachments: list[dict[str, Any]], workspace: Path) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(_proof('REPLACED INSIDE CONFIRMATION'))
        return snapshot(attachments, workspace)
    monkeypatch.setattr(workbench, 'source_snapshot', change_on_second_snapshot)
    before = _ledger(store)
    with pytest.raises(ValueError, match='确认期间.*已变化'):
        _acknowledge(store, opened)
    assert _ledger(store) == before
    current = store.get(opened['id'])
    assert not current['verified'] and not current['missing_acknowledged'] and not current['ready']
    assert current['version'] != opened['version']


@pytest.mark.parametrize('change', ['missing-file', 'changed-file', 'edited-title', 'new-proof', 'changed-role'])
def test_material_or_expense_change_invalidates_acknowledgement_until_reconfirmed(
        tmp_path: Path, change: str) -> None:
    store = ExpenseStore(tmp_path)
    reviewed = _acknowledge(store, _expense(store))
    path, _ = store.attachment_path(reviewed['attachments'][0]['id'])
    if change == 'missing-file':
        path.unlink()
    elif change == 'changed-file':
        path.write_bytes(_proof('CHANGED SOURCE CONTENT'))
    elif change == 'edited-title':
        store.update(reviewed['id'], {'title': 'Changed expense title'})
    elif change == 'new-proof':
        store.add_attachment(reviewed['id'], 'unclassified.pdf', _proof('ADDED UNKNOWN ORIGINAL'), 'other')
    else:
        store.set_role(reviewed['attachments'][0]['id'], 'other')
    before_read = _ledger(store)
    current = store.get(reviewed['id'])
    assert _ledger(store) == before_read
    assert not current['verified'] and not current['ready'] and not current['missing_acknowledged']
    assert current['pending_review'] and not current['complete']
    if change == 'missing-file':
        assert current['version'] != reviewed['version']
        with pytest.raises(ValueError, match='缺失材料已变化'):
            store.transition(current['id'], 'verify', expected_version=current['version'],
                             acknowledge_missing=True, expected_missing=reviewed['missing'])
        assert _ledger(store) == before_read
    if change in {'missing-file', 'changed-file'}:
        # Source snapshots strengthen only acknowledged rows' versions. An
        # already-open confirmation cannot silently adopt a changed original.
        assert current['version'] != reviewed['version']
        with pytest.raises(ValueError, match='已变化'):
            _acknowledge(store, reviewed)
        assert _ledger(store) == before_read
    confirmed_again = _acknowledge(store, current)
    assert confirmed_again['ready'] and confirmed_again['missing_acknowledged']
    assert confirmed_again['missing'] == current['missing']
    assert confirmed_again['amount_cents'] == reviewed['amount_cents']


@pytest.mark.parametrize('legacy', [False, True])
def test_existing_complete_and_legacy_expenses_do_not_need_a_missing_acknowledgement(
        tmp_path: Path, legacy: bool) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Previously supported complete expense', 'category': '酒店', 'amount': '15'})
    item = store.add_attachment(item['id'], 'invoice.pdf', _proof('COMPLETE SOURCE'), 'invoice')
    if legacy:
        with store.connect() as db:
            stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
            stored['legacy'] = True
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored), item['id']))
            db.execute("UPDATE attachments SET role='package' WHERE item_id=?", (item['id'],))
    reviewed = store.transition(item['id'], 'verify')
    assert reviewed['complete'] and reviewed['verified'] and reviewed['ready']
    assert not reviewed['missing_acknowledged'] and not reviewed['missing']
    assert 'missing_material_acknowledgement' not in reviewed


def test_http_requires_explicit_current_acknowledgement_and_preserves_other_expenses(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    first, second = _expense(store, 'HTTP FIRST'), _expense(store, 'HTTP SECOND', proof=False)
    with _server(store) as (port, token):
        before = _ledger(store)
        body = {'id': first['id'], 'action': 'verify', 'expected_version': first['version']}
        status, _ = _post(port, token, body)
        assert status == 400 and _ledger(store) == before
        body.update(acknowledge_missing=True, expected_missing=first['missing'])
        status, _ = _post(port, '', body)
        assert status == 403 and _ledger(store) == before
        status, result = _post(port, token, body)
        assert status == 200 and result['ready'] and result['missing_acknowledged']
        assert not result['complete'] and result['missing'] == first['missing']
        assert store.get(second['id']) == second
        # The same endpoint is also used by the page's per-item batch loop.
        status, result = _post(port, token, {'id': second['id'], 'action': 'verify',
            'expected_version': second['version'], 'acknowledge_missing': True,
            'expected_missing': second['missing']})
        assert status == 200 and result['ready'] and result['attachments'] == []


@pytest.fixture
def mocked_excel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ExpenseStore, list[dict[str, Any]]]:
    store = ExpenseStore(tmp_path)
    (store.root / TEMPLATE_NAME).write_bytes(b'Synthetic template')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))
    captured = []
    def excel(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path) -> None:
        captured.extend(copy.deepcopy(items))
        output.write_bytes(b'Synthetic spreadsheet')
    monkeypatch.setattr(report, 'create_excel', excel)
    return store, captured


def test_reviewed_missing_materials_share_preview_and_report_composition_without_guessing_or_duplication(
        mocked_excel: tuple[ExpenseStore, list[dict[str, Any]]]) -> None:
    store, captured = mocked_excel
    received = _expense(store, 'RECEIVED INVOICE')
    received = store.add_attachment(received['id'], 'unclassified.pdf', _proof('UNCLASSIFIED ORIGINAL'), 'other')
    received = _acknowledge(store, received)
    empty = _acknowledge(store, _expense(store, 'NO MATERIALS', proof=False))
    before = _ledger(store)
    preview = create_expense_print_preview(store, received['id'], received['version'])
    assert preview['partial'] and not preview['complete'] and preview['missing'] == ['付款截图']
    assert '已确认仍缺' in preview['layout'] and '非正式' not in preview['layout']
    result = report.create_report(store, [empty['id'], received['id']], submit=False)
    folder = store.root / '导出'
    with pymupdf.open(folder / result['pdf']) as document:
        text = '\n'.join(page.get_text() for page in document)
        assert text.count('RECEIVED INVOICE') == text.count('UNCLASSIFIED ORIGINAL') == 1
        assert 'NO MATERIALS' not in text and len(document) == preview['page_count']
        assert not any(page.get_images(full=True) for page in document)
    empty_row = next(item for item in captured if item['id'] == empty['id'])
    received_row = next(item for item in captured if item['id'] == received['id'])
    assert empty_row['pages'] == '无材料' and '无材料' in empty_row['layout']
    assert empty_row['amount_cents'] == empty['amount_cents']
    assert received_row['missing'] == received['missing'] and '已确认仍缺' in received_row['layout']
    manifest = (folder / result['md']).read_text(encoding='utf-8')
    assert '无材料' in manifest and '已确认仍缺' in manifest and '付款截图' in manifest
    exported = export_items(store, [empty['id'], received['id']])
    with pymupdf.open(folder / exported['pdf']) as document:
        text = '\n'.join(page.get_text() for page in document)
        assert text.count('RECEIVED INVOICE') == text.count('UNCLASSIFIED ORIGINAL') == 1
    assert '无材料' in (folder / exported['md']).read_text(encoding='utf-8')
    assert _ledger(store) == before


def test_no_originals_can_be_reviewed_but_cannot_create_a_fictitious_pdf(
        mocked_excel: tuple[ExpenseStore, list[dict[str, Any]]]) -> None:
    store, captured = mocked_excel
    empty = _acknowledge(store, _expense(store, proof=False))
    before = _ledger(store)
    preview = create_expense_print_preview(store, empty['id'], empty['version'])
    assert preview['pdf'] is None and not preview['preview_pages'] and preview['partial']
    for generate in (lambda: report.create_report(store, [empty['id']], submit=False),
                     lambda: export_items(store, [empty['id']])):
        with pytest.raises(ValueError, match='均无材料'):
            generate()
    assert _ledger(store) == before and store.get(empty['id'])['ready']
    assert not captured
    folder = store.root / '导出'
    assert not folder.exists() or not list(folder.iterdir())


@pytest.mark.parametrize('mode', ['trial-report', 'legacy-export'])
@pytest.mark.parametrize('change', ['source', 'expense'])
def test_read_only_exports_recheck_missing_acknowledgements_before_publishing(
        mocked_excel: tuple[ExpenseStore, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch,
        mode: str, change: str) -> None:
    store, _ = mocked_excel
    item = _acknowledge(store, _expense(store))
    source, _ = store.attachment_path(item['attachments'][0]['id'])
    target = report if mode == 'trial-report' else legacy_export
    compose = target.compose_matter
    expected_after_change: list[dict[str, list[tuple[Any, ...]]]] = []
    def mutate_before_composition(current_store: ExpenseStore, snapshot: dict[str, Any],
                                  folder: Path) -> tuple[Path, str]:
        if change == 'source':
            source.write_bytes(_proof('REPLACED WITHOUT REVIEW'))
        else:
            store.update(item['id'], {'title': 'Changed during rendering'})
        expected_after_change.append(_ledger(store))
        return compose(current_store, snapshot, folder)
    monkeypatch.setattr(target, 'compose_matter', mutate_before_composition)
    generate = (lambda: report.create_report(store, [item['id']], submit=False)) if mode == 'trial-report' else (
        lambda: export_items(store, [item['id']]))
    with pytest.raises(ValueError, match='制作期间.*已变化'):
        generate()
    assert _ledger(store) == expected_after_change[0]
    assert not store.get(item['id'])['ready']
    folder = store.root / '导出'
    assert not folder.exists() or not list(folder.iterdir())


def test_real_excel_retains_missing_proof_text_and_readable_row_height(tmp_path: Path) -> None:
    raw = os.environ.get('INVOICE_TEST_REPORT_TEMPLATE')
    if not raw:
        pytest.skip('Set INVOICE_TEST_REPORT_TEMPLATE for private-template integration')
    template = Path(raw)
    original_template = template.read_bytes()
    store = ExpenseStore(tmp_path / 'workspace')
    empty = _acknowledge(store, _expense(store, 'No received materials', proof=False))
    received = _acknowledge(store, _expense(store, 'Invoice received'))
    empty['pages'], received['pages'] = '无材料', '1'
    output = tmp_path / 'missing-materials.xlsx'
    create_excel(template, [empty, received], {'person': 'Synthetic leader', 'payment': 'personal',
                                             'period': '2026-10'}, output)
    namespace = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(output) as archive:
        detail = ET.fromstring(archive.read('xl/worksheets/sheet2.xml'))
        for number, item in enumerate((empty, received), 2):
            label = excel_cell(archive, 2, f'I{number}')[0]
            assert '已确认仍缺' in label
            assert all(gap in label for gap in item['missing'])
            if not item['attachments']:
                assert '无材料' in label and excel_cell(archive, 2, f'K{number}')[0] == '无材料'
            row = detail.find(f'.//s:row[@r="{number}"]', namespace)
            assert row is not None and float(row.get('ht', '0')) >= 88
            assert round(float(excel_cell(archive, 2, f'H{number}')[0]) * 100) == item['amount_cents']
    assert template.read_bytes() == original_template
