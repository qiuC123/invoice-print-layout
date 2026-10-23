"""Already associated hotel payments stay in actual preview and print PDFs."""
from __future__ import annotations

import copy
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
import pymupdf
import pytest

from invoice_print_layout import report_print_preview
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.invoice_two_up import invoice_pairable
from invoice_print_layout.reimbursement import compose_matter, selected_attachments
from invoice_print_layout.report_excel import TEMPLATE_NAME
from invoice_print_layout.workbench import ExpenseStore
from tests.test_expense_print_preview import unchanged_snapshot
from tests.test_invoice_two_up import pdf as invoice_pdf


def hotel(store: ExpenseStore) -> dict[str, Any]:
    project = ExpenseProjects(store).create('Synthetic hotel project')['id']
    item = store.create({'title': 'Synthetic lodging fee', 'category': '酒店', 'amount': '1353.00',
                         'project_id': project, 'material_basis': 'standard'})
    return store.add_attachment(item['id'], 'synthetic-invoice.pdf', invoice_pdf('HOTEL'), 'invoice')


def payment(index: int) -> bytes:
    image = Image.new('RGB', (90 + index, 210 + index), (20 + index * 30, 80, 150))
    for x in range(image.width):
        image.putpixel((x, x % image.height), (255, index * 40, 0))
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def pixel_digest(payload: bytes) -> tuple[tuple[int, int], str]:
    with Image.open(BytesIO(payload)) as image:
        rgb = image.convert('RGB')
        return rgb.size, hashlib.sha256(rgb.tobytes()).hexdigest()


def assert_payment_pixels(pdf: Path, originals: list[bytes]) -> None:
    expected = {pixel_digest(payload) for payload in originals}
    found = set()
    with pymupdf.open(pdf) as document:
        assert 'SOURCE HOTEL PAGE 1 TOP' in ''.join(page.get_text() for page in document)
        assert 'SOURCE HOTEL BOTTOM 18.50' in ''.join(page.get_text() for page in document)
        for page in document:
            for info in page.get_image_info(xrefs=True):
                if info['xref']:
                    fingerprint = pixel_digest(document.extract_image(info['xref'])['image'])
                    if fingerprint in expected:
                        assert pymupdf.Rect(info['bbox']) in page.rect
                        found.add(fingerprint)
    assert found == expected


@pytest.mark.parametrize('payment_count', [0, 1, 4])
def test_standard_hotel_prints_each_bound_payment_without_changing_single_invoice_path(
        tmp_path: Path, payment_count: int) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item = hotel(store)
    originals = [payment(index) for index in range(payment_count)]
    for index, payload in enumerate(originals):
        item = store.add_attachment(item['id'], f'payment-{index}.png', payload, 'payment')
    assert item['complete'] and item['material_basis'] == 'standard'
    assert len(selected_attachments(item)) == payment_count + 1
    assert invoice_pairable(store, item) is (payment_count == 0)
    before = unchanged_snapshot(store)
    output = tmp_path / 'composition'
    output.mkdir()
    pdf, _ = compose_matter(store, copy.deepcopy(item), output)
    with pymupdf.open(pdf) as document:
        assert len(document) == payment_count + 1
    assert_payment_pixels(pdf, originals)
    assert unchanged_snapshot(store) == before
    assert len(store.list_items()) == 1
    current = store.get(item['id'])
    assert current['amount_cents'] == 135300 and not current['verified'] and current['stage'] == 'draft'


def test_report_preview_cache_changes_with_added_hotel_payments_and_keeps_one_amount(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item = hotel(store)
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    captured: list[list[dict[str, Any]]] = []
    def excel(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path, *,
              summary_pdf: Path | None = None) -> None:
        captured.append(copy.deepcopy(items))
        output.write_bytes(b'synthetic workbook')
        assert summary_pdf is not None
        with pymupdf.open() as document:
            page = document.new_page(width=595.28, height=841.89)
            page.insert_text((30, 40), 'SYNTHETIC COMPANY TABLE')
            page.insert_text((30, 65), f'TOTAL {sum(item["amount_cents"] for item in items) / 100:.2f}')
            document.save(summary_pdf)
    monkeypatch.setattr(report_print_preview, 'create_excel', excel)
    first = report_print_preview.create_report_print_preview(store, [item['id']], build_id='synthetic-hotel')
    assert first['page_count'] == 2
    original_payloads = [payment(index) for index in range(4)]
    initial_version = item['version']
    for index, payload in enumerate(original_payloads):
        item = store.add_attachment(item['id'], f'payment-{index}.png', payload, 'payment')
    assert item['version'] != initial_version and not invoice_pairable(store, item)
    before = unchanged_snapshot(store)
    result = report_print_preview.create_report_print_preview(store, [item['id']], build_id='synthetic-hotel')
    assert result['pdf'] != first['pdf'] and result['page_count'] == 6
    assert result['ordered_item_ids'] == [item['id']] and result['total_cents'] == 135300
    assert len(captured[-1]) == 1 and captured[-1][0]['amount_cents'] == 135300
    assert captured[-1][0]['pages'] == '2-6'
    pdf = store.root / '导出' / result['pdf']
    assert_payment_pixels(pdf, original_payloads)
    with pymupdf.open(pdf) as document:
        assert 'TOTAL 1353.00' in document[0].get_text()
    monkeypatch.setattr(report_print_preview, 'create_excel', lambda *args, **kwargs: pytest.fail('unchanged source should reuse preview'))
    assert report_print_preview.create_report_print_preview(store, [item['id']], build_id='synthetic-hotel') == result
    assert unchanged_snapshot(store) == before


def test_standard_rail_payment_policy_is_not_changed_by_hotel_support(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = hotel(store)
    item = store.add_attachment(item['id'], 'synthetic-payment.png', payment(0), 'payment')
    as_rail = {**item, 'category': '高铁'}
    assert [attachment['role'] for attachment in selected_attachments(as_rail)] == ['invoice']
