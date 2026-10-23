from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.workbench import ExpenseStore, evaluate
from invoice_print_layout.workbench_export import export_items
from invoice_print_layout.payment_layout import append_payments, payment_groups
import invoice_print_layout.reimbursement as report
from invoice_print_layout.report_excel import TEMPLATE_NAME


def payment(store: ExpenseStore, i: int, project: str = 'A') -> dict[str, Any]:
    item = store.create({'title': f'Payment {i}', 'project': project, 'category': '餐饮',
                         'amount': '10', 'material_basis': 'payment_only'})
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=700)
        page.insert_text((15, 25), f'TOP {i}')
        page.insert_text((15, 690), f'BOTTOM {i}')
        store.add_attachment(item['id'], f'{i}.pdf', pdf.tobytes(), 'payment')
    return store.transition(item['id'], 'verify')


def test_payment_approval_is_explicit_and_needs_existing_file(tmp_path: Path) -> None:
    assert not evaluate('餐饮', {'payment'})['complete']
    assert not evaluate('其他', {'payment'})['complete']
    assert not evaluate('餐饮', {'invoice'}, material_basis='payment_only')['complete']
    store = ExpenseStore(tmp_path)
    item = payment(store, 1)
    assert item['ready'] and item['stage'] == 'draft'
    values = {k: v for k, v in item.items() if k != 'material_basis'}
    values['note'] = 'Updated note'
    changed = store.update(item['id'], values)
    assert changed['material_basis'] == 'payment_only' and not changed['verified']
    store.transition(item['id'], 'verify')
    store.attachment_path(item['attachments'][0]['id'])[0].unlink()
    assert not store.get(item['id'])['ready']
    with pytest.raises(ValueError):
        store.transition(item['id'], 'verify')


@pytest.mark.parametrize('count', [4, 5])
def test_separate_expenses_keep_full_content_on_independent_pages(tmp_path: Path, count: int) -> None:
    store = ExpenseStore(tmp_path)
    items = [payment(store, i) for i in range(count)]
    originals = {p: p.read_bytes() for p in store.files.iterdir()}
    result = export_items(store, [x['id'] for x in items])
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == count
        for i, page in enumerate(pdf):
            text = page.get_text()
            assert text.splitlines() == [f'TOP {i}', f'BOTTOM {i}']
            assert len(page.get_xobjects()) == 2  # one placement and its source form
            assert not page.get_images(), 'PDF originals must remain vector-based'
            blocks = page.get_text('blocks')
            assert all(b[0] < 297 and b[1] < 421 for b in blocks)
    assert all(x['stage'] == 'draft' and x['ready'] for x in store.list_items())
    assert all(p.read_bytes() == content for p, content in originals.items())


def test_projects_separate_pages_and_report_page_numbers(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    items = [payment(store, i, 'B' if i == 2 else 'A') for i in range(6)]
    with pymupdf.open() as output:
        for group in payment_groups(items):
            append_payments(output, store, group)
        assert len(output) == 6
    assert items[0]['pages'] == '1' and items[5]['pages'] == '5' and items[2]['pages'] == '6'


def test_formal_report_keeps_payment_expenses_on_separate_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    items = [payment(store, i) for i in range(4)]
    (store.root / TEMPLATE_NAME).write_bytes(b'template')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: None)
    def excel(template: Path, rows: list[dict[str, Any]], options: dict[str, str], path: Path) -> None:
        assert [x['pages'] for x in rows] == ['1', '2', '3', '4']
        path.write_bytes(b'excel')
    monkeypatch.setattr(report, 'create_excel', excel)
    result = report.create_report(store, [x['id'] for x in items])
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == 4
    assert all(x['stage'] == 'submitted' for x in store.list_items())
