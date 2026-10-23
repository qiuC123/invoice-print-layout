"""Paper saving may move a proof, but may not shrink or mix expenses."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.payment_layout import append_invoice_payments, append_payments
from invoice_print_layout.print_packing import pack_fixed_sizes
from invoice_print_layout.workbench import ExpenseStore


MARGIN = 12.0
GAP = 12.0
FULL_WIDTH = A4_WIDTH - 2 * MARGIN
FULL_HEIGHT = A4_HEIGHT - 2 * MARGIN
HALF_HEIGHT = (A4_HEIGHT - 3 * MARGIN) / 2
QUARTER_WIDTH = (A4_WIDTH - 3 * MARGIN) / 2


def _proof(marker: str, role: str, *, width: float | None = None,
           height: float = 400) -> dict[str, Any]:
    width = width if width is not None else (FULL_WIDTH if role == 'invoice' else 180.0)
    with pymupdf.open() as source:
        page = source.new_page(width=width, height=height)
        page.draw_rect(pymupdf.Rect(.5, .5, width - .5, height - .5), width=.5)
        # These native marks include the original's extreme edges and a small
        # QR-like vector pattern: checking words alone would miss clipped paint.
        for x, y in [(8, 60), (16, 60), (8, 68), (24, 76)]:
            page.draw_rect(pymupdf.Rect(x, y, x + 4, y + 4), color=None, fill=(0, 0, 0))
        for y, label in [(28, 'TOP'), (height - 15, 'BOTTOM')]:
            page.insert_text((8, y), f'{marker}-{label}', fontsize=9)
        page.insert_text((8, 48), f'AMOUNT-{marker}: 1.23', fontsize=9)
        return {'marker': marker, 'role': role, 'width': width, 'height': height,
                'content': source.tobytes()}


def _item(store: ExpenseStore, proofs: list[dict[str, Any]], *, title: str = 'Synthetic expense',
          payment_only: bool = False) -> dict[str, Any]:
    item = store.create({'title': title, 'project': 'Synthetic project', 'category': '餐饮',
                         'amount': '1.23',
                         'material_basis': 'payment_only' if payment_only else 'invoice_payment'})
    for index, proof in enumerate(proofs):
        item = store.add_attachment(item['id'], f'{index:02d}-{proof["marker"]}.pdf',
                                    proof['content'], proof['role'])
    return item


def _hashes(store: ExpenseStore) -> dict[str, str]:
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in store.files.iterdir()}


def _baseline_scale(proof: dict[str, Any]) -> float:
    width = QUARTER_WIDTH if proof['role'] == 'payment' else FULL_WIDTH
    height = FULL_HEIGHT if proof['role'] == 'other' else HALF_HEIGHT
    return min(width / proof['width'], height / proof['height'])


def _spans(document: pymupdf.Document) -> list[dict[str, Any]]:
    return [span for page in document for block in page.get_text('dict')['blocks']
            if block['type'] == 0 for line in block['lines'] for span in line['spans']]


def _assert_complete_at_baseline_scale(document: pymupdf.Document,
                                       proofs: list[dict[str, Any]], *, has_bitmaps: bool = False) -> None:
    text = '\n'.join(page.get_text() for page in document)
    spans = _spans(document)
    assert sum(len(page.get_drawings()) for page in document) == 5 * len(proofs)
    for page in document:
        assert tuple(page.rect) == pytest.approx((0, 0, A4_WIDTH, A4_HEIGHT), abs=.01)
        if not has_bitmaps:
            assert not page.get_images(), 'Native proof text and QR paths must remain vector-based'
    for proof in proofs:
        marker = proof['marker']
        scale = _baseline_scale(proof)
        for label in ('TOP', 'BOTTOM'):
            token = marker + '-' + label
            assert text.count(token) == 1, f'Missing or repeated source fragment: {token}'
            found = [span for span in spans if token in span['text']]
            assert len(found) == 1
            assert found[0]['size'] == pytest.approx(9 * scale, abs=.005)
        assert text.count(f'AMOUNT-{marker}: 1.23') == 1
        owner = next(page for page in document if marker + '-TOP' in page.get_text())
        assert marker + '-BOTTOM' in owner.get_text()
        frames = [drawing['rect'] for drawing in owner.get_drawings()
                  if drawing['type'] == 's'
                  and abs(drawing['rect'].width - (proof['width'] - 1) * scale) < .03
                  and abs(drawing['rect'].height - (proof['height'] - 1) * scale) < .03]
        assert frames, f'The complete original frame was lost or scaled: {marker}'
        assert owner.rect.contains(frames[0])


def test_three_portrait_payments_fit_below_voucher_without_smaller_type(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    proofs = [_proof('VOUCHER', 'invoice')] + [_proof(f'PAY-{i}', 'payment') for i in range(3)]
    item = _item(store, proofs)
    original_hashes, original_rows = _hashes(store), store.list_items()
    payment_width = proofs[1]['width'] * _baseline_scale(proofs[1])
    payment_height = proofs[1]['height'] * _baseline_scale(proofs[1])
    assert 3 * payment_width + 2 * GAP <= FULL_WIDTH
    assert proofs[0]['height'] * _baseline_scale(proofs[0]) + GAP + payment_height <= FULL_HEIGHT
    with pymupdf.open() as output:
        append_invoice_payments(output, store, item)
        assert len(output) == 1, 'Three unchanged portrait proofs can form a third column'
        _assert_complete_at_baseline_scale(output, proofs)
        tops = [output[0].search_for(f'PAY-{i}-TOP')[0] for i in range(3)]
        assert max(rect.y0 for rect in tops) - min(rect.y0 for rect in tops) < .1
        assert tops[0].x1 < tops[1].x0 and tops[1].x1 < tops[2].x0
        assert output[0].search_for('VOUCHER-BOTTOM')[0].y1 < min(rect.y0 for rect in tops)
    assert _hashes(store) == original_hashes and store.list_items() == original_rows


def test_same_expense_five_payments_use_three_columns_but_distinct_expenses_stay_separate(
        tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    proofs = [_proof(f'SINGLE-{i}', 'payment') for i in range(5)]
    same = _item(store, proofs, payment_only=True)
    separate_proofs = [_proof(f'SEPARATE-{i}', 'payment') for i in range(5)]
    separate = [_item(store, [proof], title=proof['marker'], payment_only=True)
                for proof in separate_proofs]
    original_hashes, original_rows = _hashes(store), store.list_items()
    with pymupdf.open() as output:
        append_payments(output, store, [same])
        assert len(output) == 1, 'Five proofs belonging to one expense fit at their existing size'
        _assert_complete_at_baseline_scale(output, proofs)
        assert same['pages'] == '1'
    with pymupdf.open() as output:
        append_payments(output, store, separate)
        assert len(output) == 5, 'Different expense IDs must never share an A4 page'
        _assert_complete_at_baseline_scale(output, separate_proofs)
        for number, (page, item, proof) in enumerate(zip(output, separate, separate_proofs, strict=True), 1):
            assert item['pages'] == str(number)
            assert proof['marker'] + '-TOP' in page.get_text()
            assert all(other['marker'] not in page.get_text()
                       for other in separate_proofs if other is not proof)
    assert _hashes(store) == original_hashes and store.list_items() == original_rows


def test_no_page_saving_keeps_original_half_quarter_positions_and_order(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    # Two full-width halves already fill one A4; shrinking either half is the
    # only way to force its additional payment onto that page.
    proofs = [_proof('VOUCHER-0', 'invoice', height=HALF_HEIGHT),
              _proof('VOUCHER-1', 'invoice', height=HALF_HEIGHT),
              _proof('PAYMENT', 'payment', width=QUARTER_WIDTH, height=HALF_HEIGHT)]
    item = _item(store, proofs)
    original_hashes = _hashes(store)
    with pymupdf.open() as output:
        append_invoice_payments(output, store, item)
        assert len(output) == 2
        _assert_complete_at_baseline_scale(output, proofs)
        assert [line for page in output for line in page.get_text().splitlines() if line.endswith('-TOP')] == [
            'VOUCHER-0-TOP', 'VOUCHER-1-TOP', 'PAYMENT-TOP']
        assert item['pages'] == '1,2'
        first = output[0].search_for('VOUCHER-0-TOP')[0]
        second = output[0].search_for('VOUCHER-1-TOP')[0]
        payment = output[1].search_for('PAYMENT-TOP')[0]
        assert first.x0 == pytest.approx(MARGIN + 8, abs=.01)
        assert second.x0 == pytest.approx(first.x0, abs=.01)
        assert second.y0 - first.y0 == pytest.approx(HALF_HEIGHT + GAP, abs=.01)
        # The two strings have different widths. Their original placement and
        # line height are the invariants, rather than their right text edges.
        assert (payment.x0, payment.y0, payment.y1) == pytest.approx((first.x0, first.y0, first.y1), abs=.01)
    assert _hashes(store) == original_hashes


def test_unknown_supplement_retains_full_a4_scale_and_every_edge(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    main = [_proof('INVOICE', 'invoice'), _proof('PAY', 'payment')]
    support = _proof('UNKNOWN-SUPPORT', 'other', width=A4_WIDTH, height=A4_HEIGHT)
    item = _item(store, main + [support])
    item = copy.deepcopy(item)
    support_id = next(attachment['id'] for attachment in item['attachments'] if attachment['role'] == 'other')
    item['material_match'] = {'support_attachment_ids': [support_id]}
    original_hashes, original_rows = _hashes(store), store.list_items()
    with pymupdf.open() as output:
        append_invoice_payments(output, store, item)
        assert len(output) == 2
        _assert_complete_at_baseline_scale(output, main + [support])
        support_page = next(page for page in output if 'UNKNOWN-SUPPORT-TOP' in page.get_text())
        assert 'UNKNOWN-SUPPORT-BOTTOM' in support_page.get_text()
        assert 'INVOICE-TOP' not in support_page.get_text() and 'PAY-TOP' not in support_page.get_text()
        assert support_page.search_for('UNKNOWN-SUPPORT-TOP')[0].y1 < A4_HEIGHT / 2
        assert support_page.search_for('UNKNOWN-SUPPORT-BOTTOM')[0].y0 > A4_HEIGHT / 2
    assert _hashes(store) == original_hashes and store.list_items() == original_rows


def test_multipage_bitmap_source_preserves_image_size_digest_and_existing_page_offset(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    proofs = [_proof(f'MULTI-{i}', 'payment') for i in range(5)]
    images = []
    with pymupdf.open() as source:
        for number, proof in enumerate(proofs):
            with pymupdf.open(stream=proof['content'], filetype='pdf') as page_source:
                source.insert_pdf(page_source)
            bitmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 30), False)
            bitmap.clear_with(30 + number * 37)
            source[-1].insert_image(pymupdf.Rect(50, 90, 90, 150), pixmap=bitmap)
            image = source[-1].get_image_info(hashes=True)[0]
            images.append({'digest': image['digest'], 'width': image['width'], 'height': image['height'],
                           'rect': pymupdf.Rect(image['bbox'])})
        multipage_content = source.tobytes()
    item = _item(store, [{**proofs[0], 'content': multipage_content}], payment_only=True)
    original_hashes, original_rows = _hashes(store), store.list_items()
    with pymupdf.open() as output:
        for number in range(2):
            page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            page.insert_text((20, 35), f'EXISTING-PREFIX-{number}', fontsize=13)
        prefix_pixels = [hashlib.sha256(page.get_pixmap(dpi=36).samples).hexdigest() for page in output]
        append_payments(output, store, [item])
        assert len(output) == 3 and item['pages'] == '3'
        assert [hashlib.sha256(output[number].get_pixmap(dpi=36).samples).hexdigest()
                for number in range(2)] == prefix_pixels
        with pymupdf.open() as packed_only:
            packed_only.insert_pdf(output, from_page=2, to_page=2)
            _assert_complete_at_baseline_scale(packed_only, proofs, has_bitmaps=True)
        actual_images = output[2].get_image_info(hashes=True)
        assert len(actual_images) == 5
        scale = _baseline_scale(proofs[0])
        for image in images:
            matches = [actual for actual in actual_images if actual['digest'] == image['digest']]
            assert len(matches) == 1
            actual = matches[0]
            assert (actual['width'], actual['height']) == (image['width'], image['height'])
            rect = pymupdf.Rect(actual['bbox'])
            assert (rect.width, rect.height) == pytest.approx(
                (image['rect'].width * scale, image['rect'].height * scale), abs=.01)
            assert output[2].rect.contains(rect)
        combined = '\n'.join(page.get_text() for page in output)
        assert all(combined.count(f'EXISTING-PREFIX-{number}') == 1 for number in range(2))
    assert _hashes(store) == original_hashes and store.list_items() == original_rows


@pytest.mark.parametrize('pattern,count', [
    ([(180.0, 400.0)], 60),
    ([(571.0, 200.0), (180.0, 400.0), (250.0, 110.0)], 20),
    ([(80.0, 90.0), (200.0, 100.0), (120.0, 240.0), (400.0, 80.0)], 15),
])
def test_fixed_size_packing_is_deterministic_bounded_and_nonoverlapping(
        pattern: list[tuple[float, float]], count: int) -> None:
    sizes = pattern * count
    result = pack_fixed_sizes(sizes, FULL_WIDTH, FULL_HEIGHT)
    assert result is not None and result == pack_fixed_sizes(sizes, FULL_WIDTH, FULL_HEIGHT)
    assert len(result) == len(sizes) and sorted(piece.index for piece in result) == list(range(len(sizes)))
    for piece in result:
        assert (piece.width, piece.height) == sizes[piece.index], 'Neither resize nor rotate a proof'
        assert piece.x >= 0 and piece.y >= 0
        assert piece.x + piece.width <= FULL_WIDTH + .001
        assert piece.y + piece.height <= FULL_HEIGHT + .001
        for other in result:
            if piece.index >= other.index or piece.page != other.page:
                continue
            assert (piece.x + piece.width + GAP <= other.x + .001 or
                    other.x + other.width + GAP <= piece.x + .001 or
                    piece.y + piece.height + GAP <= other.y + .001 or
                    other.y + other.height + GAP <= piece.y + .001), 'Proofs need a complete gutter'


@pytest.mark.parametrize('sizes', [[], [(FULL_WIDTH + 1, 100)], [(100, FULL_HEIGHT + 1)],
                                  [(0, 100)], [(100, -1)]])
def test_fixed_size_packing_rejects_impossible_or_empty_sources(sizes: list[tuple[float, float]]) -> None:
    assert pack_fixed_sizes(sizes, FULL_WIDTH, FULL_HEIGHT) is None
