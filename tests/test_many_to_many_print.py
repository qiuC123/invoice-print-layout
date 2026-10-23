"""Synthetic many-voucher groups remain one expense with every source printed once."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from PIL import Image, ImageDraw

from invoice_print_layout import reimbursement as report
from invoice_print_layout.expense_print_preview import create_expense_print_preview
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.report_excel import TEMPLATE_NAME
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items


HALF_HEIGHT = (A4_HEIGHT - 36) / 2
QUARTER_WIDTH = (A4_WIDTH - 36) / 2


def vector_proof(role: str, number: int, cents: int) -> tuple[bytes, str, str]:
    """Source frames expose the complete half/quarter cells after composition."""
    marker = f'1234567890123456{number:04d}' if role == 'invoice' else f'PAYMENT-{number:02d}'
    footer = f'BOTTOM-{"V" if role == "invoice" else "P"}-{number:02d}'
    width = A4_WIDTH - 24 if role == 'invoice' else QUARTER_WIDTH
    with pymupdf.open() as source:
        page = source.new_page(width=width, height=HALF_HEIGHT)
        page.draw_rect(pymupdf.Rect(1, 1, width - 1, HALF_HEIGHT - 1))
        heading = 'INVOICE NUMBER: ' if role == 'invoice' else 'TRANSACTION: '
        page.insert_text((12, 30), heading + marker, fontsize=9)
        page.insert_text((12, 55), f'TOTAL: {cents / 100:.2f}', fontsize=9)
        page.insert_text((12, HALF_HEIGHT - 15), footer, fontsize=9)
        return source.tobytes(), marker, footer


def expense_group(store: ExpenseStore, invoices: list[int], payments: list[int]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    assert sum(invoices) == sum(payments)
    item = store.create({'title': 'Synthetic combined expense', 'project': 'Synthetic project',
                         'category': '餐饮', 'amount': f'{sum(invoices) / 100:.2f}',
                         'material_basis': 'invoice_payment', 'expense_date': ''})
    proofs = []
    for role, amounts in [('invoice', invoices), ('payment', payments)]:
        for number, cents in enumerate(amounts):
            content, marker, footer = vector_proof(role, number, cents)
            store.add_attachment(item['id'], f'{role}-{number:02d}.pdf', content, role)
            proofs.append({'role': role, 'marker': marker, 'footer': footer})
    return store.get(item['id']), proofs


def ledger_snapshot(store: ExpenseStore) -> dict[str, Any]:
    with store.connect() as database:
        existing = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = {name: [tuple(row) for row in database.execute('SELECT * FROM ' + name)]
                  for name in ('expenses', 'attachments', 'events', 'selections', 'automatic_ride_files',
                               'intake_queue', 'material_matches') if name in existing}
    source_paths = set(store.files.iterdir())
    source_paths.update(store.attachment_path(attachment['id'])[0]
                        for item in store.list_items() for attachment in item['attachments'] if attachment['available'])
    return {'items': store.list_items(), 'tables': tables,
            'sources': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}}


def assert_all_sources_once_in_expected_cells(path: Path, proofs: list[dict[str, Any]], *, support_pages: int = 0) -> None:
    with pymupdf.open(path) as output:
        all_text = '\n'.join(page.get_text() for page in output)
        invoice_count = sum(proof['role'] == 'invoice' for proof in proofs)
        payment_count = len(proofs) - invoice_count
        core_pages = (invoice_count * 2 + payment_count + 3) // 4
        assert len(output) == core_pages + support_pages
        assert sum(len(output[number].get_drawings()) for number in range(core_pages)) == len(proofs)
        for number, page in enumerate(output):
            assert tuple(page.rect) == pytest.approx((0, 0, A4_WIDTH, A4_HEIGHT), abs=.01)
            if number < core_pages:
                assert not page.get_images()  # Synthetic main proofs remain vector text/frames.
        unit = 0
        for proof in proofs:
            is_payment = proof['role'] == 'payment'
            page = output[unit // 4]
            top = 12 + ((unit % 4) // 2) * (HALF_HEIGHT + 12)
            left = 12 + (unit % 2) * (QUARTER_WIDTH + 12) if is_payment else 12
            width = QUARTER_WIDTH if is_payment else A4_WIDTH - 24
            cell = pymupdf.Rect(left, top, left + width, top + HALF_HEIGHT)
            expected_frame = pymupdf.Rect(cell.x0 + 1, cell.y0 + 1, cell.x1 - 1, cell.y1 - 1)
            assert any(tuple(drawing['rect']) == pytest.approx(tuple(expected_frame), abs=.02)
                       for drawing in page.get_drawings())
            for marker in (proof['marker'], proof['footer']):
                assert all_text.count(marker) == 1
                bounds = page.search_for(marker)
                assert len(bounds) == 1 and cell.contains(bounds[0])
            unit += 1 if is_payment else 2


def printed_page_signature(path: Path) -> list[dict[str, Any]]:
    with pymupdf.open(path) as document:
        return [{'rect': tuple(page.rect), 'words': page.get_text('words'),
                 'frames': [tuple(drawing['rect']) for drawing in page.get_drawings()],
                 'pixels': hashlib.sha256(page.get_pixmap(dpi=60, alpha=False).samples).hexdigest()}
                for page in document]


@pytest.mark.parametrize('invoices,payments', [
    ([1000, 2000], [3000]),                 # Two invoices, one payment.
    ([1000, 2000], [1200, 1800]),           # Two invoices, two payments.
    ([1000, 2000, 3000], [1500, 2000, 2500]),  # Both kinds continue onto later pages.
    ([500, 1000, 1500, 2000, 2500], [500, 1000, 1500, 2000, 2500]),
])
def test_many_to_many_final_composition_and_preview_preserve_every_vector_source_once(
        tmp_path: Path, invoices: list[int], payments: list[int]) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item, proofs = expense_group(store, invoices, payments)
    before = ledger_snapshot(store)
    expected_folder = tmp_path / 'final-composition'
    expected_folder.mkdir()
    final_pdf, layout = report.compose_matter(store, copy.deepcopy(item), expected_folder)
    assert '票据A4纸1×2' in layout and '付款截图2×2' in layout
    assert_all_sources_once_in_expected_cells(final_pdf, proofs)
    preview = create_expense_print_preview(store, item['id'], item['version'], build_id='synthetic-many-to-many')
    preview_pdf = store.root / '导出' / preview['pdf']
    assert preview['complete'] and not preview['partial'] and preview['missing'] == []
    assert len(preview['preview_pages']) == preview['page_count']
    assert printed_page_signature(preview_pdf) == printed_page_signature(final_pdf)
    assert_all_sources_once_in_expected_cells(preview_pdf, proofs)
    assert ledger_snapshot(store) == before
    assert not (store.root / TEMPLATE_NAME).exists()
    assert len(store.list_items()) == 1 and item['amount_cents'] == sum(invoices)


def test_many_to_many_material_export_counts_the_combined_expense_only_once(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item, proofs = expense_group(store, [1000, 2000], [1200, 1800])
    item = store.transition(item['id'], 'verify')
    before = ledger_snapshot(store)
    exported = export_items(store, [item['id']])
    folder = store.root / '导出'
    assert_all_sources_once_in_expected_cells(folder / exported['pdf'], proofs)
    markdown = (folder / exported['md']).read_text(encoding='utf-8')
    assert markdown.count('| ' + item['id'] + ' |') == 1
    assert '- 餐饮：30.00元' in markdown and '- 总计：30.00元' in markdown
    assert len(store.list_items()) == 1 and store.list_items()[0]['amount_cents'] == 3000
    assert ledger_snapshot(store) == before
    assert not (store.root / TEMPLATE_NAME).exists()


def test_many_to_many_report_passes_one_total_row_to_excel_without_real_template(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item, proofs = expense_group(store, [1000, 2000, 3000], [1500, 2000, 2500])
    before = ledger_snapshot(store)
    # Only the Excel boundary is stubbed. The real report compositor, selection,
    # markdown totals and report snapshot all execute against synthetic sources.
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template placeholder')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: None)
    captured_rows: list[dict[str, Any]] = []
    def capture_excel(template: Path, rows: list[dict[str, Any]], options: dict[str, str], destination: Path) -> None:
        captured_rows.extend(copy.deepcopy(rows))
        destination.write_bytes(b'synthetic workbook placeholder')
    monkeypatch.setattr(report, 'create_excel', capture_excel)
    exported = report.create_report(store, [item['id']], submit=False)
    folder = store.root / '导出'
    assert_all_sources_once_in_expected_cells(folder / exported['pdf'], proofs)
    assert len(captured_rows) == 1 and captured_rows[0]['amount_cents'] == 6000
    assert captured_rows[0]['id'] == item['id'] and len(captured_rows[0]['attachments']) == len(proofs)
    markdown = (folder / exported['md']).read_text(encoding='utf-8')
    assert markdown.count('| ' + item['id'] + ' |') == 1
    assert '- 餐饮：60.00元' in markdown and '- 总计：60.00元' in markdown
    assert ledger_snapshot(store) == before
    assert not list(folder.glob('.report-*'))


def support_pdf(marker: str, pages: int = 2) -> bytes:
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            page.draw_rect(pymupdf.Rect(1, 1, A4_WIDTH - 1, A4_HEIGHT - 1))
            page.insert_text((20, 30), f'{marker}-TOP-{number}')
            page.insert_text((20, 60), 'AMOUNT UNKNOWN / SUPPORTING ORIGINAL ONLY')
            page.insert_text((20, A4_HEIGHT - 25), f'{marker}-BOTTOM-{number}')
        return document.tobytes()


def support_image() -> bytes:
    image = Image.new('RGB', (400, 800), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 399, 25), fill='red')
    draw.rectangle((0, 774, 399, 799), fill='blue')
    content = io.BytesIO()
    image.save(content, format='PNG')
    return content.getvalue()


def prepared_queue_entry(matcher: MaterialMatching, project: str, name: str, content: bytes,
                         role: str, cents: int | None = None, marker: str = '') -> dict[str, Any]:
    entry = matcher.intake.receive(name, content, project_id=project)
    fields: dict[str, Any] = {'title': name, 'category': '餐饮', 'expense_date': ''}
    if cents is not None:
        fields['amount'] = f'{cents / 100:.2f}'
    if role == 'invoice':
        fields['invoice_number'] = marker
    elif role == 'payment':
        fields['payment_status'] = 'succeeded'
    entry.update(role=role, fields=fields, status='review', revision=1)
    with matcher.store.connect() as database:
        matcher._write_queue(database, entry)
    return entry


def matching_sources(tmp_path: Path, *, single_voucher: bool = False) -> tuple[MaterialMatching, str, list[dict[str, Any]],
                                                                            list[dict[str, Any]], list[dict[str, Any]]]:
    store = ExpenseStore(tmp_path / 'workspace')
    matcher = MaterialMatching(store, IntakeQueue(store))
    project = ProjectRegistry(store.workspace / '后勤' / 'tasks.sqlite3').create_project('Synthetic support project')['id']
    vouchers, payments, proofs = [], [], []
    for role, amounts in [('invoice', [3000] if single_voucher else [1000, 2000]), ('payment', [1200, 1800])]:
        for number, cents in enumerate(amounts):
            content, marker, footer = vector_proof(role, number, cents)
            entry = prepared_queue_entry(matcher, project, f'{role}-{number:02d}.pdf', content, role, cents, marker)
            (vouchers if role == 'invoice' else payments).append(entry)
            proofs.append({'role': role, 'marker': marker, 'footer': footer})
    return matcher, project, vouchers, payments, proofs


def assert_full_size_supports(path: Path, marker: str, *, pages: int = 2, image: bool = True) -> None:
    with pymupdf.open(path) as document:
        text = '\n'.join(page.get_text() for page in document)
        for number in range(pages):
            top, bottom = f'{marker}-TOP-{number}', f'{marker}-BOTTOM-{number}'
            assert text.count(top) == text.count(bottom) == 1
            matching_pages = [page for page in document if top in page.get_text()]
            assert len(matching_pages) == 1
            page = matching_pages[0]
            assert not page.get_image_info()
            assert len(page.get_drawings()) == 1
            assert page.get_drawings()[0]['rect'].height > A4_HEIGHT * .9
            assert page.search_for(top)[0].y1 < A4_HEIGHT / 2 < page.search_for(bottom)[0].y0
        if image:
            image_pages = [page for page in document if page.get_image_info()]
            assert len(image_pages) == 1
            page = image_pages[0]
            assert len(page.get_image_info()) == 1
            image_rect = pymupdf.Rect(page.get_image_info()[0]['bbox'])
            assert image_rect.height > A4_HEIGHT * .9
            # Both coloured source edges survive the complete-page scaling.
            pixels = page.get_pixmap(dpi=30, alpha=False)
            for y, colour in [(image_rect.y0 + 5, 'red'), (image_rect.y1 - 5, 'blue')]:
                point = pixels.pixel(round(image_rect.x0 * 30 / 72 + 5), round(y * 30 / 72))
                assert point[0 if colour == 'red' else 2] > 200


@pytest.mark.parametrize('support_role', ['other', 'detail', 'order', 'trip_sheet'])
def test_grouped_unknown_amount_pdf_and_image_supports_print_once_without_changing_total(
        tmp_path: Path, support_role: str) -> None:
    matcher, project, vouchers, payments, proofs = matching_sources(tmp_path)
    pdf_source = prepared_queue_entry(matcher, project, 'a-support.pdf', support_pdf('SUPPORT'), support_role)
    image_source = prepared_queue_entry(matcher, project, 'b-support.png', support_image(), 'other')
    entries = [*vouchers, *payments, pdf_source, image_source]
    source_hashes = {Path(entry['path']): hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() for entry in entries}
    result = matcher.match(['q:' + entry['id'] for entry in vouchers], [entry['id'] for entry in payments], project,
                           support_ids=[pdf_source['id'], image_source['id']],
                           revisions={entry['id']: entry['revision'] for entry in entries})
    item = result['item']
    assert item['amount_cents'] == 3000 and len(matcher.store.list_items()) == 1
    assert item['material_match']['support_count'] == 2
    assert len(item['material_match']['support_attachment_ids']) == 2
    assert matcher.intake.get(pdf_source['id'])['fields'].get('amount') is None
    before = ledger_snapshot(matcher.store)
    composition = tmp_path / 'composition'
    composition.mkdir()
    final_pdf, layout = report.compose_matter(matcher.store, copy.deepcopy(item), composition)
    assert '补充材料完整原件逐页A4' in layout and '不另计费用' in layout
    assert_all_sources_once_in_expected_cells(final_pdf, proofs, support_pages=3)
    assert_full_size_supports(final_pdf, 'SUPPORT')
    preview = create_expense_print_preview(matcher.store, item['id'], item['version'], build_id='support-test')
    preview_pdf = matcher.store.root / '导出' / preview['pdf']
    assert printed_page_signature(preview_pdf) == printed_page_signature(final_pdf)
    assert ledger_snapshot(matcher.store) == before
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in source_hashes.items())
    item = matcher.store.transition(item['id'], 'verify')
    verified_snapshot = ledger_snapshot(matcher.store)
    exported = export_items(matcher.store, [item['id']])
    exported_pdf = matcher.store.root / '导出' / exported['pdf']
    assert printed_page_signature(exported_pdf) == printed_page_signature(final_pdf)
    markdown = (matcher.store.root / '导出' / exported['md']).read_text(encoding='utf-8')
    assert markdown.count('| ' + item['id'] + ' |') == 1 and '- 总计：30.00元' in markdown
    assert ledger_snapshot(matcher.store) == verified_snapshot


@pytest.mark.parametrize('single_voucher', [True, False])
def test_existing_group_supplement_updates_print_and_cache_without_recounting_originals(
        tmp_path: Path, single_voucher: bool) -> None:
    matcher, project, vouchers, payments, proofs = matching_sources(tmp_path, single_voucher=single_voucher)
    entries = [*vouchers, *payments]
    result = matcher.match(['q:' + entry['id'] for entry in vouchers], [entry['id'] for entry in payments], project,
                           revisions={entry['id']: entry['revision'] for entry in entries})
    initial = result['item']
    first = create_expense_print_preview(matcher.store, initial['id'], initial['version'], build_id='support-test')
    source = prepared_queue_entry(matcher, project, 'a-added.pdf', support_pdf('ADDED'), 'other')
    image_source = prepared_queue_entry(matcher, project, 'b-added.png', support_image(), 'other')
    updated = matcher.supplement(result['match']['id'], [source['id'], image_source['id']],
                                 expected_version=initial['version'],
                                 revisions={entry['id']: entry['revision'] for entry in (source, image_source)})
    item = updated['item']
    assert item['id'] == initial['id'] and item['amount_cents'] == initial['amount_cents'] == 3000
    assert len(matcher.store.list_items()) == 1 and not item['verified']
    before = ledger_snapshot(matcher.store)
    current = create_expense_print_preview(matcher.store, item['id'], item['version'], build_id='support-test')
    assert current['pdf'] != first['pdf']
    assert create_expense_print_preview(matcher.store, item['id'], item['version'], build_id='support-test') == current
    final_pdf = matcher.store.root / '导出' / current['pdf']
    assert_all_sources_once_in_expected_cells(final_pdf, proofs, support_pages=3)
    assert_full_size_supports(final_pdf, 'ADDED')
    assert ledger_snapshot(matcher.store) == before
    # Actual content changes must invalidate a preview even when its attachment
    # metadata/version is unchanged. Only this temporary synthetic original changes.
    Path(source['path']).write_bytes(support_pdf('REPLACED'))
    changed_snapshot = ledger_snapshot(matcher.store)
    assert matcher.store.get(item['id'])['version'] == item['version']
    changed = create_expense_print_preview(matcher.store, item['id'], item['version'], build_id='support-test')
    assert changed['pdf'] != current['pdf']
    assert_full_size_supports(matcher.store.root / '导出' / changed['pdf'], 'REPLACED')
    assert ledger_snapshot(matcher.store) == changed_snapshot


def mark_supports(store: ExpenseStore, item_id: str, ids: Any) -> dict[str, Any]:
    """Set explicit attachment metadata in an isolated synthetic ledger."""
    with store.connect() as database:
        data = json.loads(database.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
        data['material_match'] = {'support_attachment_ids': ids}
        database.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
    return store.get(item_id)


@pytest.mark.parametrize('received_role', ['invoice', 'payment'])
def test_partial_preview_prints_explicit_supports_once_and_retains_missing_requirement(
        tmp_path: Path, received_role: str) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item = store.create({'title': 'Synthetic partial group', 'category': '餐饮', 'amount': '30',
                         'material_basis': 'invoice_payment'})
    content, marker, footer = vector_proof(received_role, 0, 3000)
    store.add_attachment(item['id'], 'main.pdf', content, received_role)
    item = store.add_attachment(item['id'], 'a-partial.pdf', support_pdf('PARTIAL'), 'other')
    item = store.add_attachment(item['id'], 'b-partial.png', support_image(), 'other')
    ids = [attachment['id'] for attachment in item['attachments'] if attachment['role'] == 'other']
    item = mark_supports(store, item['id'], ids)
    before = ledger_snapshot(store)
    preview = create_expense_print_preview(store, item['id'], item['version'], build_id='support-partial')
    assert preview['partial'] and not preview['complete']
    assert preview['missing'] == (['付款截图'] if received_role == 'invoice' else ['发票或收据'])
    assert preview['page_count'] == 4 and '非正式报销包' in preview['layout']
    pdf = store.root / '导出' / preview['pdf']
    with pymupdf.open(pdf) as document:
        text = '\n'.join(page.get_text() for page in document)
        assert text.count(marker) == text.count(footer) == 1
    assert_full_size_supports(pdf, 'PARTIAL')
    assert ledger_snapshot(store) == before


def test_unmarked_optional_support_remains_excluded_from_complete_composition(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item, proofs = expense_group(store, [1000, 2000], [3000])
    item = store.add_attachment(item['id'], 'unselected.pdf', support_pdf('UNSELECTED'), 'other')
    preview = create_expense_print_preview(store, item['id'], item['version'])
    pdf = store.root / '导出' / preview['pdf']
    assert_all_sources_once_in_expected_cells(pdf, proofs)
    with pymupdf.open(pdf) as document:
        assert 'UNSELECTED' not in '\n'.join(page.get_text() for page in document)


@pytest.mark.parametrize('failure', ['missing-reference', 'missing-file', 'repeated-id', 'wrong-role'])
def test_explicit_invalid_support_never_silently_disappears_from_final_print(tmp_path: Path, failure: str) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    item, _ = expense_group(store, [1000, 2000], [3000])
    item = store.add_attachment(item['id'], 'support.pdf', support_pdf('SUPPORT'), 'other')
    support = next(attachment for attachment in item['attachments'] if attachment['role'] == 'other')
    ids = [support['id']]
    if failure == 'missing-reference':
        ids = ['unavailable-attachment']
    elif failure == 'missing-file':
        store.attachment_path(support['id'])[0].unlink()
    elif failure == 'repeated-id':
        ids *= 2
    elif failure == 'wrong-role':
        ids = [next(attachment['id'] for attachment in item['attachments'] if attachment['role'] == 'payment')]
    item = mark_supports(store, item['id'], ids)
    before = ledger_snapshot(store)
    with pytest.raises(ValueError, match='补充材料'):
        create_expense_print_preview(store, item['id'], item['version'])
    assert ledger_snapshot(store) == before
