from pathlib import Path
import io

from PIL import Image
import pytest

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.workbench import ExpenseStore


def image_bytes() -> bytes:
    data = io.BytesIO()
    Image.new('RGB', (60, 60), 'white').save(data, format='PNG')
    return data.getvalue()


def test_intake_pdf_browser_preview_preserves_original(tmp_path: Path) -> None:
    import hashlib
    import threading
    from urllib.error import HTTPError
    from urllib.request import urlopen
    import pymupdf
    from invoice_print_layout.workbench_web import make_server
    store = ExpenseStore(tmp_path)
    with pymupdf.open() as document:
        document.new_page().insert_text((40, 40), 'Receipt page one')
        document.new_page().insert_text((40, 40), 'Receipt page two')
        original = document.tobytes()
    entry = IntakeQueue(store).receive('receipt.pdf', original)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(url + '/intake-preview/' + entry['id']) as response:
            html = response.read().decode()
            assert html.count('<img ') == 2 and '?page=1' in html
        with urlopen(url + '/intake-page/' + entry['id'] + '?page=1') as response:
            assert response.read().startswith(b'\x89PNG\r\n\x1a\n')
        with pytest.raises(HTTPError) as error:
            urlopen(url + '/intake-page/' + entry['id'] + '?page=-1')
        assert error.value.code == 404
        with urlopen(url + '/intake-file/' + entry['id']) as response:
            assert hashlib.sha256(response.read()).digest() == hashlib.sha256(original).digest()
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_durable_receipt_and_duplicate_retry(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store)
    data = image_bytes()
    row = queue.receive('receipt.png', data)
    assert IntakeQueue(ExpenseStore(tmp_path)).get(row['id'])['status'] == 'received'
    assert queue.receive('again.png', data)['duplicate']
    saved = queue.resolve(row['id'], 0, {'title': '打印与胶带', 'amount': '14'}, 'purchase')
    assert queue.resolve(row['id'], 0, {'title': '重复', 'amount': '14'}, 'purchase') == saved
    assert len(store.list_items()) == 1
    assert Path(saved['path']).read_bytes() == data


def test_correction_and_payment_sum_are_per_item(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    review = ExpenseReview(store)
    item = store.create({'title': '收据', 'amount': '240'})
    result = review.confirm(item['id'], item['version'], {'title': '香烟', 'amount': '239', 'face_amount': '240', 'reason': '票面误写，实付239'})
    assert result['amount_cents'] == 23900
    assert result['confirmation']['face_amount_cents'] == 24000
    assert result['stage'] == 'draft' and not result['verified']
    with pytest.raises(ValueError, match='已变化'):
        review.confirm(item['id'], item['version'], {'amount': '240'})
    other = store.create({'title': '打印与胶带', 'amount': '14'})
    confirmed = review.confirm(other['id'], other['version'], {'payments': ['11', '3']})
    assert confirmed['confirmation']['payments_cents'] == [1100, 300]
    with pytest.raises(ValueError, match='合计'):
        review.confirm(other['id'], confirmed['version'], {'payments': ['11', '4']})


def test_duplicate_merge_retains_audit_and_rejects_stale(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    review = ExpenseReview(store)
    a = store.create({'title': '重复A', 'amount': '14'})
    b = store.create({'title': '重复B', 'amount': '14'})
    ids = [a['id'], b['id']]
    preview = review.merge_preview(ids, a['id'])
    store.add_attachment(b['id'], 'receipt.png', image_bytes(), 'purchase')
    with pytest.raises(ValueError, match='过期'):
        review.merge(ids, a['id'], preview['token'], '相同交易')
    preview = review.merge_preview(ids, a['id'])
    merged = review.merge(ids, a['id'], preview['token'], '相同交易')
    assert merged['amount_cents'] == 1400 and len(merged['attachments']) == 1
    assert store.get(b['id'])['merged_into'] == a['id']
    assert store.get(b['id'])['attachments']
    assert review.merge(ids, a['id'], preview['token'], '相同交易')['amount_cents'] == 1400
    with pytest.raises(ValueError, match='已合并'):
        store.transition(b['id'], 'draft')


def test_upload_ride_pair_one_expense(tmp_path: Path) -> None:
    from tests.test_automatic_rides import pair
    from invoice_print_layout.expense_projects import ExpenseProjects
    store = ExpenseStore(tmp_path)
    project = ExpenseProjects(store).registry.create_project('项目甲')
    trip, bill = pair(store)
    queue = IntakeQueue(store)
    a = queue.receive(trip.name, trip.read_bytes(), batch='pair', project_id=project['id'])
    b = queue.receive(bill.name, bill.read_bytes(), batch='pair', project_id=project['id'])
    assert queue.analyze(a['id'])['status'] == 'linked'
    assert queue.get(b['id'])['status'] == 'linked'
    assert len(store.list_items()) == 1
    item = store.list_items()[0]
    assert item['amount_cents'] == 12345 and item['project_id'] == project['id']
    assert len(item['attachments']) == 2 and not item['verified']


def test_cross_batch_exact_order_supplements_and_conflict_stops(tmp_path: Path, monkeypatch) -> None:
    from invoice_print_layout import intake_queue
    from invoice_print_layout.takeout import OcrLine
    store = ExpenseStore(tmp_path)
    item = store.create({'title': '打印', 'amount': '14', 'order_number': '1000000000000000001'})
    queue = IntakeQueue(store)
    row = queue.receive('pay.png', image_bytes(), batch='later')
    monkeypatch.setattr(intake_queue, 'read_receipt', lambda *a: [OcrLine('支付', 0, 0, 50, 20)])
    monkeypatch.setattr(intake_queue, 'parse_receipt', lambda *a: {'fields': {'title': '打印', 'amount': '14', 'order_number': item['order_number'], 'category': '材料采购'}, 'role': 'payment'})
    assert queue.analyze(row['id'])['item_id'] == item['id']
    assert len(store.list_items()) == 1
    second = io.BytesIO()
    Image.new('RGB', (70, 70), 'white').save(second, format='PNG')
    row = queue.receive('other.png', second.getvalue(), batch='different')
    monkeypatch.setattr(intake_queue, 'parse_receipt', lambda *a: {'fields': {'title': '打印', 'amount': '15', 'order_number': item['order_number'], 'category': '材料采购'}, 'role': 'payment'})
    result = queue.analyze(row['id'])
    assert result['status'] == 'review' and '金额冲突' in result['issues']
    assert store.get(item['id'])['amount_cents'] == 1400


def test_unreadable_material_persists_for_review(tmp_path: Path, monkeypatch) -> None:
    from invoice_print_layout import intake_queue
    queue = IntakeQueue(ExpenseStore(tmp_path))
    row = queue.receive('handwriting.png', image_bytes())
    monkeypatch.setattr(intake_queue, 'read_receipt', lambda *a: [])
    assert '手写难辨' in queue.analyze(row['id'])['issues']
    assert not queue.store.list_items()
