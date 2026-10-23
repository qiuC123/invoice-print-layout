"""Conservative same-scale two-up native invoices; synthetic originals only."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
import pymupdf
import pytest

from invoice_print_layout import invoice_two_up as pairs
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.reimbursement import _full_pages
from invoice_print_layout.workbench import ExpenseStore
from tests.test_rail_print_layout import _image_sizes, _ledger, _originals, _raw_edit, _text_sizes


def pdf(label: str, *, heading: str = '电子发票（普通发票）', height: float = 360,
        pages: int = 1, footer_heading: bool = False) -> bytes:
    image = Image.new('RGB', (33, 33), 'white')
    for x in range(33):
        for y in range(33):
            if (x * 17 + y * 11) % 7 < 3:
                image.putpixel((x, y), (0, 0, 0))
    pixels = BytesIO()
    image.save(pixels, format='PNG')
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page(width=600, height=height)
            if heading:
                page.insert_text((20, height - 35 if footer_heading else 30), heading, fontname='china-s', fontsize=13)
            page.insert_text((20, 60), f'SOURCE {label} PAGE {number + 1} TOP', fontsize=11)
            page.insert_text((20, height - 15), f'SOURCE {label} BOTTOM 18.50', fontsize=11)
            page.draw_rect(pymupdf.Rect(3, 3, 597, height - 3), color=(1, 0, 0), width=.6)
            page.insert_image(pymupdf.Rect(500, height - 90, 560, height - 30), stream=pixels.getvalue())
        return document.tobytes()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ExpenseStore, str, str]:
    store = ExpenseStore(tmp_path)
    projects = ExpenseProjects(store)
    return store, projects.create('Synthetic invoice A')['id'], projects.create('Synthetic invoice B')['id']


def expense(store: ExpenseStore, project: str, label: str, **options: Any) -> dict[str, Any]:
    item = store.create({'title': 'Synthetic ' + label, 'category': '酒店', 'amount': '18.50', 'project_id': project})
    return store.add_attachment(item['id'], label + '.pdf', pdf(label, **options), 'invoice')


@pytest.mark.parametrize('count', [1, 2, 3, 4, 5])
def test_native_invoice_pairing_preserves_text_fonts_qr_vectors_and_source_order(setup: Any, count: int) -> None:
    store, project, _ = setup
    items = [expense(store, project, f'INV{index}') for index in range(count)]
    assert all(pairs.invoice_pairable(store, item) for item in items)
    paths = [store.attachment_path(item['attachments'][0]['id'])[0] for item in items]
    old = store.workspace / 'old-full-a4.pdf'
    _full_pages(paths, old)
    before = _ledger(store), _originals(store)
    with pymupdf.open(old) as baseline, pymupdf.open() as output:
        pairs.append_invoice_pairs(output, store, items)
        assert len(output) == (count + 1) // 2
        assert _text_sizes(output) == _text_sizes(baseline)
        assert _image_sizes(output) == _image_sizes(baseline)
        assert sum(len(page.get_drawings()) for page in output) == count
        for index, item in enumerate(items):
            page = output[index // 2]
            assert page.rect.width == pytest.approx(A4_WIDTH, abs=.01)
            assert page.rect.height == pytest.approx(A4_HEIGHT, abs=.01)
            top = page.search_for(f'SOURCE INV{index} PAGE 1 TOP')
            bottom = page.search_for(f'SOURCE INV{index} BOTTOM 18.50')
            assert len(top) == len(bottom) == 1
            if index % 2:
                assert A4_HEIGHT / 2 < top[0].y0 < bottom[0].y1
            else:
                assert top[0].y0 < bottom[0].y1 < A4_HEIGHT / 2
            assert item['pages'] == str(index // 2 + 1)
            assert '打印宽度和字号保持' in item['layout']
        if count % 2:
            assert '末页单张' in items[-1]['layout']
    assert (_ledger(store), _originals(store)) == before


@pytest.mark.parametrize('heading', ['电子发票（普通发票）', '电子发票（增值税专用发票）',
                                     '上海增值税电子普通发票', '增值税普通发票', '增值税专用发票'])
def test_recognized_native_invoice_headings_are_eligible(setup: Any, heading: str) -> None:
    store, project, _ = setup
    assert pairs.invoice_pairable(store, expense(store, project, 'IDENTITY', heading=heading))


@pytest.mark.parametrize('kind', ['unknown', 'footer_identity', 'tall', 'multiple', 'extra_invoice', 'extra_payment',
                                 'extra_unknown', 'missing_extra', 'wrong_role', 'invoice_payment', 'payment_only',
                                 'partial', 'pending', 'legacy', 'deleted', 'merged', 'returned', 'unknown_project',
                                 'rotation', 'cropbox', 'widget', 'encrypted', 'owner_encrypted', 'corrupt', 'replaced'])
def test_uncertain_or_extra_materials_reject_without_appending_or_changing_items(setup: Any, kind: str) -> None:
    store, project, _ = setup
    options: dict[str, Any] = ({'heading': ''} if kind == 'unknown' else {'footer_heading': True} if kind == 'footer_identity'
                              else {'height': 842} if kind == 'tall' else {'pages': 2} if kind == 'multiple' else {})
    item = expense(store, project, kind, **options)
    if kind.startswith('extra_') or kind == 'missing_extra':
        role = {'extra_invoice': 'invoice', 'extra_payment': 'payment', 'extra_unknown': 'other', 'missing_extra': 'other'}[kind]
        item = store.add_attachment(item['id'], 'extra.pdf', pdf('EXTRA'), role)
        if kind == 'missing_extra':
            store.attachment_path(item['attachments'][-1]['id'])[0].unlink()
            item = store.get(item['id'])
    elif kind in {'wrong_role', 'invoice_payment', 'payment_only', 'partial', 'pending', 'legacy', 'deleted', 'merged', 'returned', 'unknown_project'}:
        if kind == 'wrong_role':
            with store.connect() as db:
                db.execute('UPDATE attachments SET role=? WHERE id=?', ('receipt', item['attachments'][0]['id']))
            item = store.get(item['id'])
        else:
            fields = {'invoice_payment': {'material_basis': 'invoice_payment'}, 'payment_only': {'material_basis': 'payment_only'},
                      'partial': {'category': '餐饮'}, 'pending': {'amount_pending': True, 'amount_cents': 0},
                      'legacy': {'legacy': True}, 'deleted': {'deleted': True}, 'merged': {'merged_into': 'another'},
                      'returned': {'returned_to_synthesis': True}, 'unknown_project': {'project_id': ''}}[kind]
            item = _raw_edit(store, item, **fields)
    elif kind in {'rotation', 'cropbox', 'widget', 'encrypted', 'owner_encrypted', 'corrupt', 'replaced'}:
        path = store.attachment_path(item['attachments'][0]['id'])[0]
        if kind == 'corrupt':
            path.write_bytes(b'Unreadable synthetic PDF')
        elif kind == 'replaced':
            path.write_bytes(pdf('REPLACED'))
        else:
            with pymupdf.open(path) as document:
                if kind in {'encrypted', 'owner_encrypted'}:
                    payload = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner',
                                              user_pw='user' if kind == 'encrypted' else '')
                else:
                    if kind == 'rotation':
                        document[0].set_rotation(90)
                    elif kind == 'cropbox':
                        document[0].set_cropbox(pymupdf.Rect(10, 10, 590, 350))
                    else:
                        widget = pymupdf.Widget()
                        widget.field_name = 'unknown-field'
                        widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                        widget.rect = pymupdf.Rect(100, 100, 200, 130)
                        document[0].add_widget(widget)
                    payload = document.tobytes()
            path.write_bytes(payload)
            with store.connect() as db:
                db.execute('UPDATE attachments SET digest=? WHERE id=?',
                           (hashlib.sha256(payload).hexdigest(), item['attachments'][0]['id']))
            item = store.get(item['id'])
    assert not pairs.invoice_pairable(store, item)
    before = deepcopy(item), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'EXISTING PAGE')
        with pytest.raises((ValueError, RuntimeError)):
            pairs.append_invoice_pairs(output, store, [item])
        assert len(output) == 1 and 'EXISTING PAGE' in output[0].get_text()
    assert (item, _ledger(store), _originals(store)) == before


@pytest.mark.parametrize('boundary', ['project', 'category', 'section', 'subitem'])
def test_batch_boundaries_are_rejected_atomically_even_if_individual_invoices_fit(setup: Any, boundary: str) -> None:
    store, project, foreign = setup
    first = expense(store, project, 'FIRST')
    second = expense(store, foreign if boundary == 'project' else project, 'SECOND')
    if boundary == 'category':
        second = _raw_edit(store, second, category='高铁')
        # A rail identity deliberately cannot qualify as a hotel invoice.
    elif boundary == 'section':
        second = _raw_edit(store, second, report_section='rail')
    elif boundary == 'subitem':
        first = _raw_edit(store, first, report_section='other', report_group='Group A')
        second = _raw_edit(store, second, report_section='other', report_group='Group B')
    items = [first, second]
    before = deepcopy(items), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        with pytest.raises(ValueError, match='同一项目|类别|子项'):
            pairs.append_invoice_pairs(output, store, items)
        assert len(output) == 0
    assert (items, _ledger(store), _originals(store)) == before


def test_existing_output_gets_fresh_pages_and_global_page_numbers(setup: Any) -> None:
    store, project, _ = setup
    items = [expense(store, project, f'OFFSET{index}') for index in range(3)]
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'EARLIER CATEGORY')
        pairs.append_invoice_pairs(output, store, items)
        assert len(output) == 3 and 'SOURCE' not in output[0].get_text()
        assert [item['pages'] for item in items] == ['2', '2', '3']


@pytest.mark.parametrize('when', ['before', 'during', 'content_validation'])
def test_changed_metadata_or_validation_failure_never_publishes_partial_pages(
        setup: Any, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    store, project, _ = setup
    items = [expense(store, project, f'RACE{index}') for index in range(2)]
    if when == 'before':
        _raw_edit(store, items[-1], note='Changed before rendering')
    else:
        original = pairs._same_printed_content
        def check(baseline: Any, candidate: Any) -> bool:
            result = original(baseline, candidate)
            if when == 'during':
                _raw_edit(store, items[-1], note='Changed during rendering')
            return False if when == 'content_validation' else result
        monkeypatch.setattr(pairs, '_same_printed_content', check)
    before = deepcopy(items), _originals(store)
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'UNCHANGED OLD OUTPUT')
        with pytest.raises(ValueError):
            pairs.append_invoice_pairs(output, store, items)
        assert len(output) == 1 and 'UNCHANGED OLD OUTPUT' in output[0].get_text()
    assert (items, _originals(store)) == before


def test_source_change_after_render_is_rejected_without_item_or_output_changes(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project, _ = setup
    items = [expense(store, project, f'FILE_RACE{index}') for index in range(2)]
    path = store.attachment_path(items[-1]['attachments'][0]['id'])[0]
    original = pairs._same_printed_content
    def changed(baseline: Any, candidate: Any) -> bool:
        result = original(baseline, candidate)
        path.write_bytes(pdf('CHANGED'))
        return result
    monkeypatch.setattr(pairs, '_same_printed_content', changed)
    before = deepcopy(items), _ledger(store)
    with pymupdf.open() as output:
        with pytest.raises(ValueError, match='已变化'):
            pairs.append_invoice_pairs(output, store, items)
        assert len(output) == 0
    assert (items, _ledger(store)) == before


def test_rail_tickets_keep_the_existing_specialized_contract(setup: Any) -> None:
    store, project, _ = setup
    items = [expense(store, project, f'RAIL{index}', heading='电子发票（铁路电子客票）') for index in range(2)]
    items = [_raw_edit(store, item, category='高铁') for item in items]
    assert all(pairs.invoice_pairable(store, item) for item in items)
    with pymupdf.open() as output:
        pairs.append_invoice_pairs(output, store, items)
        assert len(output) == 1 and [item['pages'] for item in items] == ['1', '1']
        assert all('高铁12306' in item['layout'] for item in items)


@pytest.mark.parametrize(('delta', 'eligible'), [(-.01, True), (.01, False)])
def test_half_page_boundary_never_adopts_a_smaller_scale(setup: Any, delta: float, eligible: bool) -> None:
    store, project, _ = setup
    limit = pairs.CELL_HEIGHT * 600 / pairs.PRINT_WIDTH
    assert pairs.invoice_pairable(store, expense(store, project, 'LIMIT', height=limit + delta)) is eligible


def test_forged_attachment_ownership_rejects_without_metadata_or_output_changes(setup: Any) -> None:
    store, project, _ = setup
    first, second = expense(store, project, 'OWNER_A'), expense(store, project, 'OWNER_B')
    first['attachments'] = deepcopy(second['attachments'])
    assert not pairs.invoice_pairable(store, first)
    before = deepcopy(first), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        with pytest.raises(ValueError):
            pairs.append_invoice_pairs(output, store, [first])
        assert len(output) == 0
    assert (first, _ledger(store), _originals(store)) == before


@pytest.mark.parametrize('kind', ['empty', 'duplicate', 'over_limit', 'invalid_id'])
def test_invalid_batch_shape_never_touches_existing_output(setup: Any, kind: str) -> None:
    store, project, _ = setup
    item = expense(store, project, 'VALID')
    batch = ([] if kind == 'empty' else [item, item] if kind == 'duplicate' else
             [item] * 201 if kind == 'over_limit' else [{'id': None}])
    before = deepcopy(batch), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'OLD CONTENT')
        with pytest.raises(ValueError):
            pairs.append_invoice_pairs(output, store, batch)
        assert len(output) == 1 and 'OLD CONTENT' in output[0].get_text()
    assert (batch, _ledger(store), _originals(store)) == before
