from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3

import pytest

from invoice_print_layout import receipt_intake as receipt, takeout
from invoice_print_layout.ocr_cache import ReceiptOcrCache
from invoice_print_layout.takeout import OcrLine, TakeoutError


@pytest.fixture
def engine(monkeypatch):
    calls = []
    monkeypatch.setattr(receipt, '_ocr_cache', ReceiptOcrCache())
    monkeypatch.setattr(receipt, '_engine', object())
    monkeypatch.setattr(receipt, 'ocr_engine_signature', lambda: 'synthetic-engine-v1:threads=2/1')
    def recognize(path, _engine):
        calls.append(path.read_bytes())
        return [OcrLine('订单号 12345678901234567890 实付17.00', 0, 0, 100, 20)]
    monkeypatch.setattr(receipt, '_ocr_lines', recognize)
    return calls


def test_callers_share_byte_cache_and_return_independent_lists(engine):
    from invoice_print_layout import expense_classifier, agent_evidence
    first = receipt.read_receipt(b'synthetic-image', '.png')
    assert expense_classifier.read_receipt(b'synthetic-image', '.jpg') == first
    assert agent_evidence.read_receipt(b'synthetic-image', '.png') == first
    first.clear()
    assert len(receipt.read_receipt(b'synthetic-image', '.png')) == 1
    assert len(engine) == 1


def test_signature_change_and_different_bytes_do_not_hit(engine, monkeypatch):
    receipt.read_receipt(b'first', '.png')
    monkeypatch.setattr(receipt, 'ocr_engine_signature', lambda: 'synthetic-engine-v2:threads=2/1')
    receipt.read_receipt(b'first', '.png')
    receipt.read_receipt(b'second', '.png')
    assert len(engine) == 3


def test_disk_survives_restart_and_bad_row_is_replaced(engine, tmp_path, monkeypatch):
    path = tmp_path / 'ocr-cache.sqlite3'
    monkeypatch.setattr(receipt, '_ocr_cache', ReceiptOcrCache(path))
    first = receipt.read_receipt(b'first', '.png')
    monkeypatch.setattr(receipt, '_ocr_cache', ReceiptOcrCache(path))
    assert receipt.read_receipt(b'first', '.png') == first and len(engine) == 1
    with sqlite3.connect(path) as db:
        db.execute('UPDATE ocr_results SET lines=?', ('{"broken": true}',))
    monkeypatch.setattr(receipt, '_ocr_cache', ReceiptOcrCache(path))
    assert receipt.read_receipt(b'first', '.png') == first and len(engine) == 2


@pytest.mark.parametrize('empty', [False, True])
def test_failed_or_empty_recognition_is_not_cached(engine, monkeypatch, empty):
    calls = []
    def fail(*args):
        calls.append(1)
        if not empty:
            raise TakeoutError('synthetic failure')
        return []
    monkeypatch.setattr(receipt, '_ocr_lines', fail)
    for _ in range(2):
        if empty:
            assert receipt.read_receipt(b'failure', '.png') == []
        else:
            with pytest.raises(TakeoutError):
                receipt.read_receipt(b'failure', '.png')
    assert len(calls) == 2


def test_cached_result_does_not_enter_ocr_lock(engine):
    expected = receipt.read_receipt(b'first', '.png')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with receipt._ocr_lock:
            future = pool.submit(receipt.read_receipt, b'first', '.png')
            assert future.result(timeout=1) == expected
    assert len(engine) == 1


def test_concurrent_misses_recognize_once(engine):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: receipt.read_receipt(b'same', '.png'), range(8)))
    assert all(result == results[0] for result in results) and len(engine) == 1


def test_memory_is_bounded_and_bad_database_is_optional(tmp_path):
    path = tmp_path / 'unavailable.sqlite3'
    path.write_bytes(b'not a database')
    cache = ReceiptOcrCache(path, capacity=2)
    lines = [OcrLine('合成文字', 0, 0, 20, 20)]
    for number in range(3):
        cache.put(str(number), 'signature', lines)
    assert len(cache.memory) == 2 and cache.get('0', 'signature') is None
    assert cache.get('2', 'signature') == lines


def test_takeout_inspection_uses_shared_engine_across_batches(engine, tmp_path, monkeypatch):
    source = tmp_path / 'order.png'
    source.write_bytes(b'synthetic-image')
    monkeypatch.setattr(takeout, '_parse_order', lambda path, lines: takeout.OrderReceipt(
        path, '12345678901234567890', '合成餐厅', Decimal('17.00'),
        hashlib.sha256(path.read_bytes()).hexdigest(), (0, 0, 100, 100)))
    receipt.read_receipt(source.read_bytes(), source.suffix)
    for number in range(2):
        staging = tmp_path / str(number)
        staging.mkdir()
        orders, invoices = takeout.inspect_takeout_files([source], staging)
        assert len(orders) == 1 and not invoices
    assert len(engine) == 1
