from pathlib import Path
from typing import Any

import pymupdf
import pytest
from PIL import Image

import invoice_print_layout.reimbursement as report
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.report_excel import TEMPLATE_NAME
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items


def proof(text: str, *, pages: int = 1) -> bytes:
    with pymupdf.open() as pdf:
        for i in range(pages):
            page = pdf.new_page(width=300, height=700)
            page.insert_text((15, 25), f'{text} TOP {i}')
            page.insert_text((15, 690), f'{text} BOTTOM {i}')
        return pdf.tobytes()


def voucher_matter(store: ExpenseStore, role: str = 'invoice', payments: int = 2) -> dict[str, Any]:
    item = store.create({'title': 'Synthetic voucher expense', 'project': 'Synthetic project',
                         'category': '餐饮', 'amount': '14', 'material_basis': 'invoice_payment'})
    store.add_attachment(item['id'], 'voucher.pdf', proof('VOUCHER'), role)
    for i in range(payments):
        store.add_attachment(item['id'], f'payment{i}.pdf', proof(f'PAYMENT {i}'), 'payment')
    # These optional records must not replace or duplicate the actual voucher.
    store.add_attachment(item['id'], 'order.pdf', proof('EXCLUDED ORDER'), 'order')
    store.add_attachment(item['id'], 'purchase.pdf', proof('EXCLUDED PURCHASE'), 'purchase')
    return store.transition(item['id'], 'verify')


def test_attachment_selection_keeps_all_payments_and_preserves_old_basis() -> None:
    attachments = [{'role': role, 'name': f'{role}{i}', 'available': available}
                   for i, (role, available) in enumerate([
                       ('invoice', True), ('receipt', True), ('payment', True), ('payment', True),
                       ('order', True), ('purchase', True), ('payment', False)])]
    item = {'category': '餐饮', 'attachments': attachments, 'alternative': False}
    assert [a['role'] for a in report.selected_attachments({**item, 'material_basis': 'invoice_payment'})] == [
        'invoice', 'receipt', 'payment', 'payment']
    assert {a['role'] for a in report.selected_attachments(item)} == {'invoice', 'order', 'purchase'}
    assert [a['role'] for a in report.selected_attachments({**item, 'material_basis': 'payment_only'})] == [
        'payment', 'payment']


@pytest.mark.parametrize('role', ['invoice', 'receipt'])
def test_voucher_and_two_payments_share_one_sheet_without_losing_content(tmp_path: Path, role: str) -> None:
    store = ExpenseStore(tmp_path)
    item = voucher_matter(store, role)
    before = store.get(item['id'])
    originals = {p: p.read_bytes() for p in store.files.iterdir()}
    result = export_items(store, [item['id']])
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        assert abs(page.rect.width - A4_WIDTH) < 1 and abs(page.rect.height - A4_HEIGHT) < 1
        text = page.get_text()
        for marker in ('VOUCHER', 'PAYMENT 0', 'PAYMENT 1'):
            assert marker + ' TOP 0' in text and marker + ' BOTTOM 0' in text
        assert 'EXCLUDED' not in text
        assert page.search_for('VOUCHER BOTTOM 0')[0].y1 < A4_HEIGHT / 2
        left, right = page.search_for('PAYMENT 0 TOP 0')[0], page.search_for('PAYMENT 1 TOP 0')[0]
        assert left.y0 > A4_HEIGHT / 2 and right.y0 > A4_HEIGHT / 2
        assert left.x1 < A4_WIDTH / 2 < right.x0
    assert store.get(item['id']) == before
    assert all(p.read_bytes() == content for p, content in originals.items())
    assert '发票／收据＋付款截图' in (store.root / '导出' / result['md']).read_text(encoding='utf-8')


def test_multiple_payment_pages_continue_without_omission(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = voucher_matter(store, payments=1)
    store.add_attachment(item['id'], 'multipage-payment.pdf', proof('MULTIPAGE PAYMENT', pages=4), 'payment')
    store.transition(item['id'], 'verify')
    originals = {path: path.read_bytes() for path in store.files.iterdir()}
    result = export_items(store, [item['id']])
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        # Each 300x700 original prints at 172.69x402.94pt in the old
        # half/quarter cells. Six proofs fit three columns and two rows
        # with the same 12pt gutters, without reducing that printed scale.
        assert len(pdf) == 1
        text = ''.join(p.get_text() for p in pdf)
        for i in range(4):
            assert f'MULTIPAGE PAYMENT TOP {i}' in text and f'MULTIPAGE PAYMENT BOTTOM {i}' in text
        assert all(marker in text for marker in ('VOUCHER TOP 0', 'VOUCHER BOTTOM 0',
                                                 'PAYMENT 0 TOP 0', 'PAYMENT 0 BOTTOM 0'))
        assert len(text.splitlines()) == 12 and 'EXCLUDED' not in text
        old_scale = ((A4_HEIGHT - 36) / 2) / 700
        spans = [span for page in pdf for block in page.get_text('dict')['blocks']
                 if block['type'] == 0 for line in block['lines'] for span in line['spans']]
        assert spans and all(span['size'] == pytest.approx(11 * old_scale, abs=.001) for span in spans)
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_receipt_and_payment_images_use_the_same_half_quarter_layout(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'store')
    item = store.create({'title': 'Synthetic image receipt', 'category': '材料采购',
                         'amount': '14', 'material_basis': 'invoice_payment'})
    for name, role, color in [('receipt', 'receipt', 'red'), ('payment0', 'payment', 'green'),
                              ('payment1', 'payment', 'blue')]:
        image = tmp_path / (name + '.png')
        Image.new('RGB', (300, 700), color).save(image)
        store.add_attachment(item['id'], image.name, image.read_bytes(), role)
    store.transition(item['id'], 'verify')
    result = export_items(store, [item['id']])
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        bounds = [page.get_image_rects(image[0])[0] for image in page.get_images()]
        assert len(bounds) == 3
        assert sum(rect.y1 < A4_HEIGHT / 2 for rect in bounds) == 1
        assert sum(rect.y0 > A4_HEIGHT / 2 for rect in bounds) == 2
        assert all(0 <= rect.x0 < rect.x1 <= A4_WIDTH and 0 <= rect.y0 < rect.y1 <= A4_HEIGHT for rect in bounds)


def test_report_preview_uses_same_layout_and_does_not_change_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExpenseStore(tmp_path)
    item = voucher_matter(store, 'receipt')
    before = store.get(item['id'])
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: None)
    def excel(template: Path, rows: list[dict[str, Any]], options: dict[str, str], output: Path) -> None:
        assert rows[0]['pages'] == '1'
        assert '票据A4纸1×2' in rows[0]['layout']
        output.write_bytes(b'synthetic excel')
    monkeypatch.setattr(report, 'create_excel', excel)
    result = report.create_report(store, [item['id']], submit=False)
    with pymupdf.open(store.root / '导出' / result['pdf']) as pdf:
        assert len(pdf) == 1
        text = pdf[0].get_text()
        assert all(marker in text for marker in ('VOUCHER', 'PAYMENT 0', 'PAYMENT 1'))
        assert 'EXCLUDED' not in text
    assert store.get(item['id']) == before


def test_duplicate_payment_cannot_be_counted_under_two_report_matters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ExpenseStore(tmp_path)
    first = voucher_matter(store)
    second = store.create({'title': 'Another synthetic expense', 'category': '餐饮', 'amount': '14',
                           'material_basis': 'invoice_payment'})
    store.add_attachment(second['id'], 'different-voucher.pdf', proof('DIFFERENT VOUCHER'), 'invoice')
    payment = next(a for a in first['attachments'] if a['name'] == 'payment0.pdf')
    store.add_attachment(second['id'], 'same-payment.pdf', store.attachment_path(payment['id'])[0].read_bytes(), 'payment')
    store.transition(second['id'], 'verify')
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: None)
    with pytest.raises(ValueError, match='相同凭证'):
        report.create_report(store, [first['id'], second['id']], submit=False)
    assert all(x['stage'] == 'draft' for x in store.list_items())
