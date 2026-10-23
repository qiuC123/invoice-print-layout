"""Synthetic final-layout side previews remain independent of report submission."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator
from urllib.parse import quote

import pymupdf
import pytest

from invoice_print_layout import expense_print_preview as preview
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.reimbursement import compose_matter
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.helpers import make_invoice_pdf, make_trip_pdf


def proof(text: str, *, pages: int = 1) -> bytes:
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page(width=300, height=700)
            page.insert_text((15, 30), f'{text} TOP {number}')
            page.insert_text((15, 680), f'{text} BOTTOM {number}')
        return document.tobytes()


def matter(store: ExpenseStore, *, category: str = '酒店', basis: str = 'standard',
           pages: int = 1) -> dict[str, Any]:
    item = store.create({'title': 'Synthetic preview expense', 'project': 'Synthetic project',
                         'category': category, 'amount': '14', 'material_basis': basis})
    if basis == 'payment_only':
        store.add_attachment(item['id'], 'payment.pdf', proof('PAYMENT', pages=pages), 'payment')
    elif basis == 'invoice_payment':
        store.add_attachment(item['id'], 'receipt.pdf', proof('RECEIPT'), 'receipt')
        for number in range(2):
            store.add_attachment(item['id'], f'payment-{number}.pdf', proof(f'PAYMENT {number}'), 'payment')
    elif category == '打车':
        store.add_attachment(item['id'], 'trip.pdf',
                             make_trip_pdf(store.workspace / 'trip.pdf', pages=pages, amount='14.00').read_bytes(), 'trip')
        store.add_attachment(item['id'], 'invoice.pdf',
                             make_invoice_pdf(store.workspace / 'invoice.pdf', amount='14.00').read_bytes(), 'invoice')
    else:
        store.add_attachment(item['id'], 'invoice.pdf', proof('INVOICE', pages=pages), 'invoice')
    return store.get(item['id'])


def unchanged_snapshot(store: ExpenseStore) -> dict[str, Any]:
    with store.connect() as database:
        rows = {name: [tuple(row) for row in database.execute('SELECT * FROM ' + name)]
                for name in ('expenses', 'attachments', 'events', 'selections', 'automatic_ride_files')}
    return {'items': store.list_items(), 'rows': rows,
            'originals': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in store.files.iterdir()}}


def rendered_pages(path: Path) -> list[dict[str, Any]]:
    with pymupdf.open(path) as document:
        return [{'rect': tuple(page.rect), 'words': page.get_text('words'),
                 'pixels': hashlib.sha256(page.get_pixmap(dpi=60, alpha=False).samples).hexdigest()}
                for page in document]


@pytest.mark.parametrize('category,basis,pages', [
    ('打车', 'standard', 2), ('高铁', 'standard', 2), ('餐饮', 'invoice_payment', 1),
    ('餐饮', 'payment_only', 5),
])
def test_preview_reuses_real_final_composition_without_template(
        tmp_path: Path, category: str, basis: str, pages: int) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store, category=category, basis=basis, pages=pages)
    before = unchanged_snapshot(store)
    expected_folder = tmp_path / 'expected'
    expected_folder.mkdir()
    expected, _ = compose_matter(store, copy.deepcopy(item), expected_folder)
    result = preview.create_expense_print_preview(store, item['id'], item['version'], build_id='synthetic-build')
    assert result['complete'] and result['missing'] == []
    pdf = store.root / '导出' / result['pdf']
    assert rendered_pages(pdf) == rendered_pages(expected)
    with pymupdf.open(pdf) as document:
        assert result['page_count'] == len(document) == len(result['preview_pages'])
        for number, page in enumerate(document):
            assert abs(page.rect.width - A4_WIDTH) < 1 and abs(page.rect.height - A4_HEIGHT) < 1
            assert page.get_text().strip()  # Vector source text is still extractable.
            with pymupdf.open(store.root / '导出' / result['preview_pages'][number]) as image:
                assert abs(image[0].rect.width * 120 / 72 - 993) < 2
                assert abs(image[0].rect.height * 120 / 72 - 1404) < 2
    if basis == 'invoice_payment':
        with pymupdf.open(pdf) as document:
            page = document[0]
            assert page.search_for('RECEIPT TOP')[0].y1 < A4_HEIGHT / 2
            assert page.search_for('PAYMENT 0 TOP')[0].y0 > A4_HEIGHT / 2
            assert page.search_for('PAYMENT 1 TOP')[0].x0 > A4_WIDTH / 2
    if basis == 'payment_only':
        assert '当前费用' in result['layout'] and '不同费用' in result['layout']
        assert '可与同项目支付凭证共页' not in result['layout']
    assert unchanged_snapshot(store) == before
    assert not (store.root / '公司报销模板.xlsx').exists()


@pytest.mark.parametrize('state', ['draft', 'verified', 'submitted', 'reimbursed', 'cancelled'])
def test_any_expense_state_is_previewed_without_mutating_state_or_events(tmp_path: Path, state: str) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    if state in {'verified', 'submitted', 'reimbursed'}:
        item = store.transition(item['id'], 'verify')
    if state in {'submitted', 'reimbursed'}:
        item = store.transition(item['id'], 'submitted')
    if state in {'reimbursed', 'cancelled'}:
        item = store.transition(item['id'], state)
    before = unchanged_snapshot(store)
    result = preview.create_expense_print_preview(store, item['id'], item['version'])
    assert result['pdf'] and result['page_count'] == 1
    assert unchanged_snapshot(store) == before


@pytest.mark.parametrize('attachment', [False, True])
def test_missing_material_returns_missing_list_and_never_a_fake_final_preview(tmp_path: Path, attachment: bool) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Missing payment', 'category': '酒店', 'amount': '14',
                         'material_basis': 'invoice_payment'})
    if attachment:
        item = store.add_attachment(item['id'], 'invoice.pdf', proof('INVOICE'), 'invoice')
    before = unchanged_snapshot(store)
    result = preview.create_expense_print_preview(store, item['id'], item['version'])
    assert result['id'] == item['id'] and result['version'] == item['version']
    assert result['complete'] is False and result['partial'] is True
    assert result['missing'] == (['付款截图'] if attachment else ['发票或收据', '付款截图'])
    if attachment:
        assert result['pdf'] and result['preview_pages'] and result['page_count'] == 1
        assert '非正式报销包' in result['layout']
    else:
        assert result['pdf'] is None and result['preview_pages'] == [] and result['page_count'] == 0
        assert not (store.root / '导出').exists()
    assert unchanged_snapshot(store) == before


def test_content_cache_reuses_artifacts_and_rechecks_actual_source_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    first = preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build-one')
    outputs = list((store.root / '导出').iterdir())
    timestamps = {path.name: path.stat().st_mtime_ns for path in outputs}
    compose = preview.compose_matter
    monkeypatch.setattr(preview, 'compose_matter', lambda *_: pytest.fail('Valid cache must not be recomposed'))
    assert preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build-one') == first
    assert {path.name: path.stat().st_mtime_ns for path in (store.root / '导出').iterdir()} == timestamps
    monkeypatch.setattr(preview, 'compose_matter', compose)
    path, _ = store.attachment_path(item['attachments'][0]['id'])
    path.write_bytes(proof('EXTERNALLY CHANGED SOURCE'))
    # File content is absent from the optimistic record version. It must be hashed separately.
    assert store.get(item['id'])['version'] == item['version']
    changed = preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build-one')
    assert changed['pdf'] != first['pdf']
    with pymupdf.open(store.root / '导出' / changed['pdf']) as document:
        assert 'EXTERNALLY CHANGED SOURCE' in document[0].get_text()
    rebuilt = preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build-two')
    assert rebuilt['pdf'] not in {changed['pdf'], first['pdf']}


def test_corrupt_cached_page_is_regenerated_and_stale_version_is_rejected(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    first = preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build')
    image = store.root / '导出' / first['preview_pages'][0]
    image.write_bytes(b'broken cached image')
    result = preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build')
    assert result == first and image.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    current = store.update(item['id'], {**item, 'title': 'Updated synthetic title'})
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='已变化'):
        preview.create_expense_print_preview(store, item['id'], item['version'], build_id='build')
    changed = preview.create_expense_print_preview(store, item['id'], current['version'], build_id='build')
    assert changed['pdf'] != first['pdf']
    assert unchanged_snapshot(store) == before


@pytest.mark.parametrize('pdf_first', [True, False])
def test_pdf_only_cache_and_public_image_preview_reuse_the_same_vector_composition(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pdf_first: bool) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store, basis='payment_only', pages=5)
    before = unchanged_snapshot(store)
    first_call = preview.create_expense_print_pdf if pdf_first else preview.create_expense_print_preview
    second_call = preview.create_expense_print_preview if pdf_first else preview.create_expense_print_pdf
    first = first_call(store, item['id'], item['version'], build_id='test-build')
    folder = store.root / '导出'
    if pdf_first:
        assert first['preview_pages'] == [] and first['page_count'] == 1
        assert not list(folder.glob('*.png'))
    monkeypatch.setattr(preview, 'compose_matter', lambda *_: pytest.fail('The vector PDF is already cached'))
    second = second_call(store, item['id'], item['version'], build_id='test-build')
    pdf_only, images = (first, second) if pdf_first else (second, first)
    assert pdf_only['preview_pages'] == []
    assert images['page_count'] == len(images['preview_pages']) == pdf_only['page_count'] == 1
    assert images['layout'] == pdf_only['layout']
    assert images['layout'].count('此预览仅显示当前费用') == 1
    assert rendered_pages(folder / pdf_only['pdf']) == rendered_pages(folder / images['pdf'])
    assert second_call(store, item['id'], item['version'], build_id='test-build') == second
    assert unchanged_snapshot(store) == before


def test_pdf_only_cache_rechecks_source_bytes_version_build_and_cached_pdf(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    path, _ = store.attachment_path(item['attachments'][0]['id'])
    original = path.read_bytes()
    replacement = proof('CHANGED')
    # PDF producers may encode equal-length text into different stream sizes.
    # Trailing whitespace keeps both PDFs valid while fixing the byte length.
    same_size = max(len(original), len(replacement))
    path.write_bytes(original.ljust(same_size, b' '))
    replacement = replacement.ljust(same_size, b' ')
    first = preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='one')
    folder = store.root / '导出'
    compose = preview.compose_matter
    monkeypatch.setattr(preview, 'compose_matter', lambda *_: pytest.fail('Valid vector cache must be reused'))
    assert preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='one') == first
    monkeypatch.setattr(preview, 'compose_matter', compose)
    # Corruption must regenerate even if the item and source are unchanged.
    (folder / first['pdf']).write_bytes(b'broken cached PDF')
    assert preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='one') == first
    assert (folder / first['pdf']).read_bytes().startswith(b'%PDF-')

    path, _ = store.attachment_path(item['attachments'][0]['id'])
    recorded_stat = path.stat()
    assert len(replacement) == recorded_stat.st_size
    path.write_bytes(replacement)
    os.utime(path, ns=(recorded_stat.st_atime_ns, recorded_stat.st_mtime_ns))
    assert store.get(item['id'])['version'] == item['version']
    changed = preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='one')
    assert changed['pdf'] != first['pdf']
    with pymupdf.open(folder / changed['pdf']) as document:
        assert 'CHANGED' in document[0].get_text()
    rebuilt = preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='two')
    assert rebuilt['pdf'] not in {first['pdf'], changed['pdf']}
    current = store.update(item['id'], {**item, 'title': 'Edited title'})
    with pytest.raises(ValueError, match='已变化'):
        preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='two')
    updated = preview.create_expense_print_pdf(store, item['id'], current['version'], build_id='two')
    assert updated['pdf'] != rebuilt['pdf']
    assert not list(folder.glob('*.png'))


def test_pdf_only_source_change_during_composition_is_rejected_without_publication(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    compose = preview.compose_matter
    source, _ = store.attachment_path(item['attachments'][0]['id'])

    def change_source(current_store: ExpenseStore, current: dict[str, Any], folder: Path) -> tuple[Path, str]:
        result = compose(current_store, current, folder)
        source.write_bytes(proof('SOURCE CHANGED DURING PDF COMPOSITION'))
        return result

    monkeypatch.setattr(preview, 'compose_matter', change_source)
    with pytest.raises(ValueError, match='预览期间'):
        preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='test')
    assert list((store.root / '导出').iterdir()) == []
    assert store.get(item['id']) == item


@pytest.mark.parametrize('broken', ['page_count', 'non_a4', 'encrypted', 'unreadable'])
def test_pdf_only_cache_validates_actual_document_beyond_manifest_artifact_hashes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store, pages=2)
    before = unchanged_snapshot(store)
    result = preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='test')
    folder = store.root / '导出'
    pdf = folder / result['pdf']
    manifest_path = pdf.with_suffix('.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if broken == 'page_count':
        manifest['result']['page_count'] = result['page_count'] + 1
    else:
        if broken == 'non_a4':
            payload = proof('NON A4 CACHE', pages=2)
        elif broken == 'encrypted':
            with pymupdf.open(pdf) as document:
                payload = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                           user_pw='synthetic-password')
        else:
            payload = b'%PDF-unreadable-synthetic-cache'
        pdf.write_bytes(payload)
        manifest['artifacts'][pdf.name] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    composed: list[str] = []
    compose = preview.compose_matter

    def counted_compose(current_store: ExpenseStore, current: dict[str, Any], staging: Path) -> tuple[Path, str]:
        composed.append(current['id'])
        return compose(current_store, current, staging)

    monkeypatch.setattr(preview, 'compose_matter', counted_compose)
    rebuilt = preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='test')
    assert rebuilt == result and composed == [item['id']]
    with pymupdf.open(pdf) as document:
        assert document.is_pdf and not document.needs_pass
        assert len(document) == rebuilt['page_count'] == 2
        for page in document:
            assert abs(page.rect.width - A4_WIDTH) < 1 and abs(page.rect.height - A4_HEIGHT) < 1
            assert 'INVOICE' in page.get_text()
    assert preview.create_expense_print_pdf(store, item['id'], item['version'], build_id='test') == rebuilt
    assert composed == [item['id']]
    assert unchanged_snapshot(store) == before


def test_uncertain_trip_crop_fails_without_source_fallback_or_leftover_files(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    path = make_trip_pdf(tmp_path / 'uncertain-trip.pdf', pages=2, amount='14.00')
    with pymupdf.open(path) as document:
        document.delete_page(1)
        page = document.new_page()
        page.insert_text((72, 200), 'UNKNOWN CONTINUATION CONTENT 14.00')
        content = document.tobytes()
    # Register the uncertain original itself. Replacing an already registered
    # original now correctly fails its digest check before layout is attempted.
    item = store.create({'title': 'Uncertain continuation', 'category': '打车', 'amount': '14.00'})
    store.add_attachment(item['id'], 'trip.pdf', content, 'trip')
    item = store.add_attachment(item['id'], 'invoice.pdf',
                                make_invoice_pdf(tmp_path / 'invoice.pdf', amount='14.00').read_bytes(), 'invoice')
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='安全的续页表格标志'):
        preview.create_expense_print_preview(store, item['id'], item['version'])
    assert list((store.root / '导出').iterdir()) == []
    assert unchanged_snapshot(store) == before


def test_detects_source_change_during_composition_and_discards_partial_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    original_compose = preview.compose_matter
    path, _ = store.attachment_path(item['attachments'][0]['id'])
    def changing_compose(current_store: ExpenseStore, current: dict[str, Any], folder: Path) -> tuple[Path, str]:
        result = original_compose(current_store, current, folder)
        path.write_bytes(proof('SOURCE REPLACED DURING PREVIEW'))
        return result
    monkeypatch.setattr(preview, 'compose_matter', changing_compose)
    with pytest.raises(ValueError, match='预览期间'):
        preview.create_expense_print_preview(store, item['id'], item['version'])
    assert list((store.root / '导出').iterdir()) == []
    assert store.get(item['id']) == item


def test_non_a4_legacy_package_is_rejected_without_rewriting_the_original(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    with store.connect() as database:
        data = json.loads(database.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()[0])
        data['legacy'] = True
        database.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
        database.execute("UPDATE attachments SET role='package' WHERE item_id=?", (item['id'],))
    item = store.get(item['id'])
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='非A4'):
        preview.create_expense_print_preview(store, item['id'], item['version'])
    assert list((store.root / '导出').iterdir()) == []
    assert unchanged_snapshot(store) == before


@pytest.mark.parametrize('broken', ['encrypted', 'malformed'])
def test_unreadable_originals_have_no_final_preview_or_fallback(tmp_path: Path, broken: str) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Unreadable original', 'category': '酒店', 'amount': '14'})
    if broken == 'encrypted':
        with pymupdf.open(stream=proof('PRIVATE ORIGINAL'), filetype='pdf') as document:
            payload = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw='synthetic-password')
    else:
        payload = b'%PDF-synthetic-invalid-file'
    item = store.add_attachment(item['id'], 'original.pdf', payload, 'invoice')
    before = unchanged_snapshot(store)
    with pytest.raises(ValueError, match='加密|无法生成最终打印预览'):
        preview.create_expense_print_preview(store, item['id'], item['version'])
    assert list((store.root / '导出').iterdir()) == []
    assert unchanged_snapshot(store) == before


def test_partial_publication_failure_removes_only_new_preview_outputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    item = matter(store)
    folder = store.root / '导出'
    folder.mkdir()
    unrelated = folder / 'existing-report.pdf'
    unrelated.write_bytes(proof('EXISTING REPORT'))
    before = unchanged_snapshot(store)
    original_replace = Path.replace
    def fail_png(source: Path, target: str | Path) -> Path:
        if source.suffix == '.png' and source.parent.name.startswith('.expense-preview-'):
            raise OSError('Synthetic publish failure')
        return original_replace(source, target)
    monkeypatch.setattr(Path, 'replace', fail_png)
    with pytest.raises(OSError, match='publish failure'):
        preview.create_expense_print_preview(store, item['id'], item['version'])
    assert list(folder.iterdir()) == [unrelated]
    assert unchanged_snapshot(store) == before


@contextmanager
def server_client(workspace: Path) -> Iterator[tuple[HTTPConnection, str]]:
    server = make_server(workspace, 0, mode='invoice')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    try:
        connection.request('GET', '/api/state')
        response = connection.getresponse()
        assert response.status == 200
        token = json.loads(response.read())['token']
        yield connection, token
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()


def post(connection: HTTPConnection, body: dict[str, Any], token: str) -> tuple[int, Any]:
    connection.request('POST', '/api/expense-print-preview', json.dumps(body),
                       {'Content-Type': 'application/json', 'X-Workbench-Token': token})
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def test_http_preview_downloads_real_pdf_and_120dpi_pages_without_template_or_writes(tmp_path: Path) -> None:
    with server_client(tmp_path) as (connection, token):
        store = ExpenseStore(tmp_path)
        item = matter(store, category='餐饮', basis='invoice_payment')
        before = unchanged_snapshot(store)
        body = {'id': item['id'], 'expected_version': item['version']}
        status, result = post(connection, body, token)
        assert status == 200 and result['page_count'] == 1
        assert post(connection, body, token) == (200, result)
        for filename, magic in [(result['pdf'], b'%PDF-'),
                                (result['preview_pages'][0], b'\x89PNG\r\n\x1a\n')]:
            connection.request('GET', '/export/' + quote(filename))
            response = connection.getresponse()
            assert response.status == 200 and response.read().startswith(magic)
        assert unchanged_snapshot(store) == before


def test_http_missing_version_id_and_authentication_boundary(tmp_path: Path) -> None:
    with server_client(tmp_path) as (connection, token):
        store = ExpenseStore(tmp_path)
        item = store.create({'title': 'Missing invoice', 'category': '酒店', 'amount': '14'})
        before = unchanged_snapshot(store)
        valid = {'id': item['id'], 'expected_version': item['version']}
        status, result = post(connection, valid, token)
        assert status == 200 and not result['complete'] and result['missing'] == ['发票']
        for body in ({'id': item['id']}, {**valid, 'expected_version': 'stale'},
                     {**valid, 'expected_version': []}, {**valid, 'id': '../outside'}, {**valid, 'id': [item['id']]}):
            assert post(connection, body, token)[0] == 400
        assert post(connection, valid, '')[0] == 403
        connection.request('POST', '/api/expense-print-preview', json.dumps(valid),
                           {'Content-Type': 'application/json', 'X-Workbench-Token': token,
                            'Origin': 'https://untrusted.example'})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request('GET', '/export/..%2Foutside.pdf')
        response = connection.getresponse()
        assert response.status == 404
        response.read()
        assert unchanged_snapshot(store) == before
        assert not (store.root / '导出').exists()
