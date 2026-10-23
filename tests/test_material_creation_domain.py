"""Unreviewed material drafts support custom categories and explicit unknown amounts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout.layout import A4_HEIGHT
from invoice_print_layout.reimbursement import create_report
from invoice_print_layout.workbench import CATEGORIES, ExpenseStore, evaluate
from invoice_print_layout.workbench_export import export_items


def proof(marker: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=700)
        page.insert_text((15, 30), marker + ' TOP')
        page.insert_text((15, 680), marker + ' BOTTOM')
        return document.tobytes()


def pending_matter(store: ExpenseStore, *, category: str = '自定义现场费用', both: bool = True) -> dict[str, Any]:
    item = store.create({'title': 'Synthetic amount-pending expense', 'category': category,
                         'amount': '', 'amount_pending': True, 'expense_date': '',
                         'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], 'invoice.pdf', proof('INVOICE'), 'invoice')
    if both:
        store.add_attachment(item['id'], 'payment.pdf', proof('PAYMENT'), 'payment')
    return store.get(item['id'])


def table_snapshot(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as database:
        return {name: [tuple(row) for row in database.execute('SELECT * FROM ' + name)]
                for name in ('expenses', 'attachments', 'events')}


@pytest.mark.parametrize('category,expected', [(' 自定义现场费用 ', '自定义现场费用'), (' 酒店 ', '酒店'),
                                            ('', '其他'), ('   ', '其他'), (None, '其他'), ('类' * 200, '类' * 200)])
def test_custom_category_is_trimmed_persisted_and_presets_remain_suggestions(
        tmp_path: Path, category: str | None, expected: str) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic custom expense', 'category': category, 'amount': '12.34',
                         'material_basis': 'invoice_payment'})
    reopened = ExpenseStore(tmp_path).get(item['id'])
    assert reopened['category'] == expected and reopened['amount'] == '12.34'
    assert reopened['amount_cents'] == 1234 and not reopened['amount_pending']
    assert CATEGORIES == ('材料采购', '酒店', '高铁', '外卖', '餐饮', '打车', '顺丰', '其他')
    assert not reopened['complete'] and not reopened['verified']


@pytest.mark.parametrize('category', ['类' * 201, 123, True, [], {}])
def test_invalid_category_is_rejected_without_creating_a_record(tmp_path: Path, category: Any) -> None:
    store = ExpenseStore(tmp_path)
    with pytest.raises(ValueError, match='类别'):
        store.create({'title': 'Synthetic expense', 'category': category, 'amount': '14'})
    assert store.list_items() == []


@pytest.mark.parametrize('amount', ['', '   ', None])
def test_empty_amount_is_zero_storage_with_an_explicit_pending_flag(tmp_path: Path, amount: Any) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic unknown amount', 'category': '自定义费用', 'amount': amount,
                         'material_basis': 'invoice_payment'})
    assert item['amount_pending'] is True and item['amount_cents'] == 0 and item['amount'] == ''
    assert item['missing'] == ['发票或收据', '付款截图', '金额待确认']
    assert not item['complete'] and not item['ready']
    reopened = ExpenseStore(tmp_path).get(item['id'])
    assert reopened['amount_pending'] is True and reopened['amount'] == ''
    with store.connect() as database:
        raw = json.loads(database.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
    assert raw['amount_cents'] == 0 and raw['amount_pending'] is True


@pytest.mark.parametrize('flag', [0, 1, 'true', 'false', None, [], {}])
def test_amount_pending_accepts_boolean_only(tmp_path: Path, flag: Any) -> None:
    store = ExpenseStore(tmp_path)
    with pytest.raises(ValueError, match='布尔值'):
        store.create({'title': 'Synthetic invalid pending flag', 'amount': '0', 'amount_pending': flag})
    assert store.list_items() == []


def test_explicit_pending_zero_is_not_displayed_as_a_confirmed_zero(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic unknown zero', 'amount': '0', 'amount_pending': True,
                         'material_basis': 'invoice_payment'})
    assert item['amount_cents'] == 0 and item['amount_pending'] is True and item['amount'] == ''
    assert '金额待确认' in item['missing'] and not item['complete']


def test_pending_flag_cannot_silently_discard_a_nonzero_amount(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    with pytest.raises(ValueError, match='0占位'):
        store.create({'title': 'Synthetic conflicting amount', 'amount': '14', 'amount_pending': True})
    assert store.list_items() == []


@pytest.mark.parametrize('basis,category,roles,legacy', [
    ('standard', '酒店', {'invoice'}, False),
    ('invoice_payment', '自定义费用', {'invoice', 'payment'}, False),
    ('payment_only', '自定义费用', {'payment'}, False),
    ('standard', '其他', {'package'}, True),
])
def test_unknown_amount_prevents_completion_for_every_material_basis_including_legacy(
        basis: str, category: str, roles: set[str], legacy: bool) -> None:
    assert evaluate(category, roles, material_basis=basis, legacy=legacy)['complete']
    pending = evaluate(category, roles, material_basis=basis, legacy=legacy, amount_pending=True)
    assert pending['missing'] == ['金额待确认'] and not pending['complete']


def test_pending_amount_keeps_full_materials_partial_and_blocks_verify_report_and_export(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = pending_matter(store)
    assert item['missing'] == ['金额待确认'] and not item['complete'] and not item['ready']
    before = table_snapshot(store)
    originals = {path: path.read_bytes() for path in store.files.iterdir()}
    preview = create_expense_print_preview(store, item['id'], item['version'])
    assert preview['partial'] and not preview['complete'] and preview['missing'] == ['金额待确认']
    assert preview['page_count'] == 1 and '非正式报销包' in preview['layout']
    with pymupdf.open(store.root / '导出' / preview['pdf']) as document:
        assert len(document) == 1
        page = document[0]
        text = page.get_text()
        assert text.count('INVOICE TOP') == text.count('PAYMENT TOP') == 1
        assert page.search_for('INVOICE BOTTOM')[0].y1 < A4_HEIGHT / 2
        assert page.search_for('PAYMENT TOP')[0].y0 > A4_HEIGHT / 2
    for action in ('verify', 'submitted', 'reimbursed'):
        with pytest.raises(ValueError, match='补齐材料'):
            store.transition(item['id'], action)
    with pytest.raises(ValueError, match='材料齐全'):
        create_report(store, [item['id']], submit=False)
    with pytest.raises(ValueError, match='材料齐全'):
        export_items(store, [item['id']])
    assert table_snapshot(store) == before
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_name_category_or_pending_flag_only_update_cannot_confirm_a_zero_placeholder(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = pending_matter(store)
    for values in ({'title': 'Renamed expense'}, {'category': ' 第二自定义类别 '}, {'amount_pending': False}):
        item = store.update(item['id'], values)
        assert item['amount_pending'] is True and item['amount_cents'] == 0 and item['amount'] == ''
        assert item['missing'] == ['金额待确认'] and not item['complete'] and not item['ready']
        assert item['expense_date'] == '' and item['material_basis'] == 'invoice_payment'
    assert item['title'] == 'Renamed expense' and item['category'] == '第二自定义类别'
    round_trip = store.update(item['id'], {**item, 'note': 'Notes only'})
    assert round_trip['amount_pending'] and round_trip['amount'] == ''


@pytest.mark.parametrize('amount,cents', [('25.86', 2586), ('0', 0)])
def test_explicit_valid_amount_clears_pending_and_can_then_be_verified(
        tmp_path: Path, amount: str, cents: int) -> None:
    store = ExpenseStore(tmp_path)
    item = pending_matter(store)
    changed = store.update(item['id'], {**item, 'amount': amount})
    assert changed['amount_cents'] == cents and changed['amount'] == f'{cents / 100:.2f}'
    assert changed['amount_pending'] is False and changed['complete'] and changed['missing'] == []
    assert not changed['verified'] and not changed['ready']
    verified = store.transition(item['id'], 'verify')
    assert verified['ready'] and verified['amount_pending'] is False


def test_partial_update_preserves_a_previously_confirmed_amount_and_all_other_fields(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic known expense', 'category': '餐饮', 'amount': '25.86',
                         'expense_date': '', 'followup_date': '2026-01-02', 'material_basis': 'invoice_payment',
                         'merchant': 'Synthetic seller', 'order_number': 'Synthetic order'})
    changed = store.update(item['id'], {'title': 'Renamed known expense'})
    assert changed['amount_cents'] == 2586 and changed['amount'] == '25.86' and not changed['amount_pending']
    for field in ('category', 'expense_date', 'followup_date', 'merchant', 'order_number', 'material_basis'):
        assert changed[field] == item[field]


@pytest.mark.parametrize('values', [{'amount': 'NaN'}, {'amount': '-1'}, {'amount': '1.001'},
                                  {'amount': '14', 'amount_pending': 'false'}, {'category': '类' * 201}])
def test_invalid_update_rolls_back_without_confirming_unknown_amount(tmp_path: Path, values: dict[str, Any]) -> None:
    store = ExpenseStore(tmp_path)
    item = pending_matter(store)
    before = table_snapshot(store)
    with pytest.raises(ValueError):
        store.update(item['id'], values)
    assert table_snapshot(store) == before and store.get(item['id'])['amount_pending'] is True


def test_old_record_without_pending_flag_retains_its_confirmed_amount_and_verification(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = pending_matter(store)
    item = store.update(item['id'], {'amount': '14'})
    item = store.transition(item['id'], 'verify')
    with store.connect() as database:
        raw = json.loads(database.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        raw.pop('amount_pending')
        database.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), item['id']))
    old = store.get(item['id'])
    assert old['amount_pending'] is False and old['amount'] == '14.00' and old['ready']
    updated = store.update(item['id'], {**old})
    assert updated['amount_pending'] is False and updated['amount'] == '14.00' and updated['ready']


def test_unknown_custom_standard_category_still_needs_explicit_material_requirements(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic custom standard', 'category': '自定义类别', 'amount': '14'})
    item = store.add_attachment(item['id'], 'invoice.pdf', proof('INVOICE'), 'invoice')
    assert item['missing'] == ['确认此类别的材料要求（请选择具体类别）']
    assert not item['complete'] and not item['ready']
