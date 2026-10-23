"""Native two-up railway tickets retain source pages, fonts, QR and ordering."""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
import pymupdf
import pytest

import invoice_print_layout.rail_print_layout as rail
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.reimbursement import _full_pages
from invoice_print_layout.workbench import ExpenseStore


def _pdf(label: str, *, width: float = 595.27, height: float = 396.65,
         pages: int = 1, native_rail: bool = True, rotation: int = 0) -> bytes:
    image = Image.new('RGB', (33, 33), 'white')
    draw = ImageDraw.Draw(image)
    for row in range(33):
        for column in range(33):
            if (row * 17 + column * 11) % 7 < 3:
                draw.point((column, row), fill='black')
    image_bytes = BytesIO()
    image.save(image_bytes, format='PNG')
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page(width=width, height=height)
            heading = '电子发票（铁路电子客票）' if native_rail else '电子发票（普通发票）'
            page.insert_text((22, 30), heading, fontname='china-s', fontsize=13)
            page.insert_text((22, 55), f'RAIL SOURCE {label} PAGE {index + 1} TOP', fontsize=11)
            page.insert_text((22, height - 15), f'RAIL SOURCE {label} BOTTOM 18.50', fontsize=11)
            page.draw_rect(pymupdf.Rect(4, 4, width - 4, height - 4), color=(1, 0, 0), width=.6)
            page.insert_image(pymupdf.Rect(width - 82, height - 82, width - 22, height - 22),
                               stream=image_bytes.getvalue())
            page.set_rotation(rotation)
        return document.tobytes()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ExpenseStore, str, str]:
    store = ExpenseStore(tmp_path)
    projects = ExpenseProjects(store)
    first = projects.create('Synthetic railway project')['id']
    second = projects.create('Synthetic other project')['id']
    return store, first, second


def _expense(store: ExpenseStore, project: str, label: str, **source_options: Any) -> dict[str, Any]:
    item = store.create({'title': f'Railway synthetic {label}', 'category': '高铁', 'amount': '18.50',
                         'project_id': project})
    return store.add_attachment(item['id'], label + '.pdf', _pdf(label, **source_options), 'invoice')


def _raw_edit(store: ExpenseStore, item: dict[str, Any], **fields: Any) -> dict[str, Any]:
    with store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        data.update(fields)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    return store.get(item['id'])


def _ledger(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        tables = [row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [tuple(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                for name in tables if not name.startswith('sqlite_')}


def _originals(store: ExpenseStore) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in store.files.iterdir()}


def _text_sizes(document: pymupdf.Document) -> Counter[tuple[str, str, float]]:
    return Counter((char['c'], span['font'], round(span['size'], 3))
                   for page in document for block in page.get_text('rawdict')['blocks'] if block['type'] == 0
                   for line in block['lines'] for span in line['spans'] for char in span['chars']
                   if not char['c'].isspace())


def _image_sizes(document: pymupdf.Document) -> Counter[tuple[bytes, int, int, float, float]]:
    return Counter((info['digest'], info['width'], info['height'],
                    round(pymupdf.Rect(info['bbox']).width, 3), round(pymupdf.Rect(info['bbox']).height, 3))
                   for page in document for info in page.get_image_info(hashes=True))


@pytest.mark.parametrize('count', [1, 2, 3, 5])
def test_two_per_a4_preserves_original_order_full_native_size_and_ledger(setup: Any, count: int) -> None:
    store, project, _ = setup
    items = [_expense(store, project, f'TICKET{index}') for index in range(count)]
    assert all(rail.rail_pairable(store, item) for item in items)
    paths = [store.attachment_path(item['attachments'][0]['id'])[0] for item in items]
    old_file = store.workspace / 'old-single-pages.pdf'
    _full_pages(paths, old_file)
    before = _ledger(store), _originals(store)
    with pymupdf.open(old_file) as baseline, pymupdf.open() as output:
        rail.append_rail_tickets(output, store, items)
        assert len(output) == (count + 1) // 2
        assert _text_sizes(output) == _text_sizes(baseline)
        assert _image_sizes(output) == _image_sizes(baseline)
        assert sum(len(page.get_drawings()) for page in output) == sum(len(page.get_drawings()) for page in baseline)
        for index, item in enumerate(items):
            page = output[index // 2]
            assert abs(page.rect.width - A4_WIDTH) < .01 and abs(page.rect.height - A4_HEIGHT) < .01
            top = page.search_for(f'RAIL SOURCE TICKET{index} PAGE 1 TOP')
            bottom = page.search_for(f'RAIL SOURCE TICKET{index} BOTTOM 18.50')
            assert len(top) == len(bottom) == 1
            limit = A4_HEIGHT / 2
            assert (top[0].y0 < bottom[0].y1 < limit) if index % 2 == 0 else (limit < top[0].y0 < bottom[0].y1)
            assert item['pages'] == str(index // 2 + 1)
            assert '打印宽度和字号保持' in item['layout'] and '不裁切不旋转' in item['layout']
        if count % 2:
            assert '末页单张' in items[-1]['layout']
            assert len(output[-1].get_image_info()) == 1
    assert (_ledger(store), _originals(store)) == before


def test_existing_pages_use_global_numbers_and_halves_never_reuse_other_expenses(setup: Any) -> None:
    store, project, _ = setup
    items = [_expense(store, project, f'OFFSET{index}') for index in range(3)]
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'EARLIER OTHER EXPENSE')
        rail.append_rail_tickets(output, store, items)
        assert len(output) == 3 and 'OFFSET' not in output[0].get_text()
        assert [item['pages'] for item in items] == ['2', '2', '3']
        assert output[1].get_text().index('OFFSET0') < output[1].get_text().index('OFFSET1')


@pytest.mark.parametrize('kind', ['tall', 'multiple_pages', 'unidentified', 'rotation', 'extra_invoice',
                                 'extra_payment', 'extra_unknown', 'unavailable_extra', 'legacy', 'payment_only',
                                 'other_category', 'other_subitem', 'deleted', 'merged', 'returned', 'pending_amount',
                                 'unknown_project', 'corrupt_source', 'replaced_source', 'encrypted', 'widget', 'cropbox'])
def test_unproven_or_incompatible_ticket_is_not_pairable_and_never_appended(setup: Any, kind: str) -> None:
    store, project, _ = setup
    options: dict[str, Any] = {'height': 842} if kind == 'tall' else {'pages': 2} if kind == 'multiple_pages' else (
        {'native_rail': False} if kind == 'unidentified' else {'rotation': 90} if kind == 'rotation' else {})
    item = _expense(store, project, kind, **options)
    if kind in {'extra_invoice', 'extra_payment', 'extra_unknown', 'unavailable_extra'}:
        role = {'extra_invoice': 'invoice', 'extra_payment': 'payment', 'extra_unknown': 'other', 'unavailable_extra': 'other'}[kind]
        item = store.add_attachment(item['id'], 'extra.pdf', _pdf('EXTRA'), role)
        if kind == 'unavailable_extra':
            store.attachment_path(item['attachments'][-1]['id'])[0].unlink()
            item = store.get(item['id'])
    elif kind in {'legacy', 'payment_only', 'other_category', 'other_subitem', 'deleted', 'merged', 'returned',
                  'pending_amount', 'unknown_project'}:
        fields = {'legacy': {'legacy': True}, 'payment_only': {'material_basis': 'payment_only'},
                  'other_category': {'category': '酒店'}, 'other_subitem': {'report_section': 'other'},
                  'deleted': {'deleted': True}, 'merged': {'merged_into': 'another-fee'},
                  'returned': {'returned_to_synthesis': True},
                  'pending_amount': {'amount_pending': True, 'amount_cents': 0}, 'unknown_project': {'project_id': ''}}[kind]
        item = _raw_edit(store, item, **fields)
    elif kind in {'corrupt_source', 'replaced_source', 'encrypted', 'widget', 'cropbox'}:
        path = store.attachment_path(item['attachments'][0]['id'])[0]
        if kind == 'corrupt_source':
            path.write_bytes(b'Unreadable synthetic PDF')
        elif kind == 'replaced_source':
            path.write_bytes(_pdf('REPLACEMENT'))
        else:
            with pymupdf.open(path) as original:
                if kind == 'encrypted':
                    payload = original.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='user')
                elif kind == 'widget':
                    widget = pymupdf.Widget()
                    widget.field_name = 'unknown-form'
                    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                    widget.rect = pymupdf.Rect(100, 100, 200, 130)
                    widget.field_value = 'Unproven form field'
                    original[0].add_widget(widget)
                    payload = original.tobytes()
                else:
                    original[0].set_cropbox(pymupdf.Rect(15, 15, 580, 381))
                    payload = original.tobytes()
            path.write_bytes(payload)
            with store.connect() as db:
                db.execute('UPDATE attachments SET digest=? WHERE id=?',
                           (hashlib.sha256(payload).hexdigest(), item['attachments'][0]['id']))
            item = store.get(item['id'])
    assert not rail.rail_pairable(store, item)
    before = copy.deepcopy(item), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'EXISTING PAGE')
        with pytest.raises(ValueError):
            rail.append_rail_tickets(output, store, [item])
        assert len(output) == 1 and 'EXISTING PAGE' in output[0].get_text()
    assert (item, _ledger(store), _originals(store)) == before


def test_cross_project_batches_are_rejected_without_output_or_item_changes(setup: Any) -> None:
    store, project, foreign = setup
    items = [_expense(store, project, 'MAIN'), _expense(store, foreign, 'FOREIGN')]
    assert all(rail.rail_pairable(store, item) for item in items)
    before = copy.deepcopy(items), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        with pytest.raises(ValueError, match='同一项目'):
            rail.append_rail_tickets(output, store, items)
        assert len(output) == 0
    assert (items, _ledger(store), _originals(store)) == before


def test_stale_batch_rejects_whole_batch_without_partial_new_pages(setup: Any) -> None:
    store, project, _ = setup
    items = [_expense(store, project, f'STALE{index}') for index in range(2)]
    _raw_edit(store, items[-1], note='Changed since selecting the invoice')
    before = copy.deepcopy(items), _ledger(store), _originals(store)
    with pymupdf.open() as output:
        with pytest.raises(ValueError, match='已变化'):
            rail.append_rail_tickets(output, store, items)
        assert len(output) == 0
    assert (items, _ledger(store), _originals(store)) == before


def test_changed_source_during_composition_is_not_published(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project, _ = setup
    items = [_expense(store, project, f'RACE{index}') for index in range(2)]
    path = store.attachment_path(items[-1]['attachments'][0]['id'])[0]
    before = copy.deepcopy(items), _ledger(store)
    original_check = rail._same_printed_content

    def changed_after_check(baseline: Any, staged: Any) -> bool:
        result = original_check(baseline, staged)
        path.write_bytes(_pdf('CHANGED DURING COMPOSITION'))
        return result

    monkeypatch.setattr(rail, '_same_printed_content', changed_after_check)
    with pymupdf.open() as output:
        with pytest.raises(ValueError, match='生成期间'):
            rail.append_rail_tickets(output, store, items)
        assert len(output) == 0
    assert (items, _ledger(store)) == before


def test_size_validation_failure_keeps_old_output_and_metadata(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    store, project, _ = setup
    items = [_expense(store, project, f'VALIDATE{index}') for index in range(2)]
    before = copy.deepcopy(items), _ledger(store), _originals(store)
    monkeypatch.setattr(rail, '_same_printed_content', lambda _before, _after: False)
    with pymupdf.open() as output:
        output.new_page().insert_text((30, 30), 'ORIGINAL OUTPUT')
        with pytest.raises(ValueError, match='打印尺寸'):
            rail.append_rail_tickets(output, store, items)
        assert len(output) == 1 and 'ORIGINAL OUTPUT' in output[0].get_text()
    assert (items, _ledger(store), _originals(store)) == before
