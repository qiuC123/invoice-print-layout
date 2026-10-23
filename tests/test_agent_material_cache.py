"""Source-bound reuse, invalidation and isolation of local fact descriptions."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import agent_evidence as evidence
from invoice_print_layout import agent_material_cache as cache
from invoice_print_layout.takeout import OcrLine
from tests.test_invoice_agent_evidence import invoice


@pytest.fixture(autouse=True)
def isolated_memory() -> Any:
    cache.clear_memory_cache()
    yield
    cache.clear_memory_cache()


def _count_pdf_parses(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = evidence._words
    def counted(page: Any) -> Any:
        calls.append(1)
        return original(page)
    monkeypatch.setattr(evidence, '_words', counted)
    return calls


def test_warm_lookup_reuses_parser_but_still_hashes_bytes_and_returns_independent_copy(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path, discount=True)
    before = copy.deepcopy(entry)
    parses = _count_pdf_parses(monkeypatch)
    reads: list[str] = []
    real_read = Path.read_bytes
    def counted_read(path: Path) -> bytes:
        reads.append(str(path))
        return real_read(path)
    monkeypatch.setattr(Path, 'read_bytes', counted_read)
    cold: dict[str, Any] = {}
    warm: dict[str, Any] = {}
    first = evidence.material_facts(entry, cache_root=tmp_path / 'private', cache_info=cold)
    first['goods'][0]['name'] = 'consumer mutation'
    first['actors']['buyer']['value'] = 'consumer mutation'
    second = evidence.material_facts(entry, cache_root=tmp_path / 'private', cache_info=warm)
    assert len(parses) == 1 and reads.count(entry['path']) == 2
    assert second['goods'][0]['name'] == '饮用水' and second['actors']['buyer']['value'] == '合成买方公司'
    assert cold['hit'] is False and warm['hit'] is True and warm['storage'] == 'memory'
    assert warm['source_bytes'] > 0 and warm['source_hash_seconds'] >= 0
    assert entry == before


def test_disk_cache_survives_process_memory_clear_and_does_not_reparse(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path)
    first = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    cache.clear_memory_cache()
    monkeypatch.setattr(evidence, '_words', lambda page: pytest.fail('persisted hit must not reparse'))
    info: dict[str, Any] = {}
    assert evidence.material_facts(entry, cache_root=tmp_path / 'private', cache_info=info) == first
    assert info['hit'] and info['storage'] == 'disk'


def test_restart_process_reuses_persisted_facts_without_opening_pdf(tmp_path: Path) -> None:
    entry = invoice(tmp_path)
    root = tmp_path / 'private'
    first = evidence.material_facts(entry, cache_root=root)
    code = '''
import json, sys
from pathlib import Path
from invoice_print_layout import agent_evidence as evidence
entry, root = json.load(sys.stdin)
def must_not_parse(page):
    raise AssertionError('restarted process must reuse persisted facts')
evidence._words = must_not_parse
info = {}
result = evidence.material_facts(entry, cache_root=Path(root), cache_info=info)
print(json.dumps([result, info]))
'''
    run = subprocess.run([sys.executable, '-c', code], input=json.dumps([entry, str(root)]),
                         text=True, encoding='utf-8', capture_output=True, check=True)
    result, info = json.loads(run.stdout)
    assert result == first and info['hit'] and info['storage'] == 'disk'


def test_same_size_same_mtime_replacement_cannot_reuse_cached_original(tmp_path: Path) -> None:
    entry = invoice(tmp_path)
    original = Path(entry['path'])
    evidence.material_facts(entry, cache_root=tmp_path / 'private')
    before = original.stat()
    payload = original.read_bytes()
    original.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
    info: dict[str, Any] = {}
    failed = evidence.material_facts(entry, cache_root=tmp_path / 'private', cache_info=info)
    assert not failed['complete'] and failed['issues'] and not failed['goods']
    assert info['storage'] == 'invalid_source' and not info['hit']
    # Recover the exact original, without invalidating a valid prior parse.
    original.write_bytes(payload)
    assert evidence.material_facts(entry, cache_root=tmp_path / 'private')['complete']


def test_deleted_original_is_not_resurrected_by_persisted_cache(tmp_path: Path) -> None:
    entry = invoice(tmp_path)
    evidence.material_facts(entry, cache_root=tmp_path / 'private')
    cache.clear_memory_cache()
    Path(entry['path']).unlink()
    result = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    assert not result['complete'] and not result['goods'] and result['issues']


@pytest.mark.parametrize('change', ['revision', 'role', 'fields', 'manual_fields', 'text'])
def test_entry_changes_invalidate_even_with_unchanged_file_and_revision(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    entry = invoice(tmp_path)
    parses = _count_pdf_parses(monkeypatch)
    evidence.material_facts(entry, cache_root=tmp_path / 'private')
    changed = copy.deepcopy(entry)
    if change == 'revision':
        changed['revision'] += 1
    elif change == 'fields':
        changed['fields']['amount'] = '9.00'
    elif change == 'manual_fields':
        changed['manual_fields'] = ['amount', 'merchant']
    elif change == 'text':
        changed['text'] = '人工本地修订，仅原文有据时有效'
    else:
        changed['role'] = 'purchase'
    info: dict[str, Any] = {}
    result = evidence.material_facts(changed, cache_root=tmp_path / 'private', cache_info=info)
    assert not info['hit'] and len(parses) == 2 and entry != changed
    assert result['revision'] == changed['revision']
    if change == 'fields':
        assert result['total_cents'] == 900 and not result['complete']
        assert changed['fields']['amount'] == '9.00'


def test_parser_version_invalidates_both_memory_and_disk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path)
    parses = _count_pdf_parses(monkeypatch)
    first = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    monkeypatch.setattr(evidence, 'PARSER_VERSION', 'material-facts-test-new-rules')
    info: dict[str, Any] = {}
    second = evidence.material_facts(entry, cache_root=tmp_path / 'private', cache_info=info)
    assert not info['hit'] and len(parses) == 2
    assert first['parser_version'] != second['parser_version']


def test_goods_rules_version_invalidates_both_memory_and_disk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import agent_content
    entry = invoice(tmp_path)
    parses = _count_pdf_parses(monkeypatch)
    evidence.material_facts(entry, cache_root=tmp_path / 'private')
    monkeypatch.setattr(agent_content, 'GOODS_RULES_VERSION', 'synthetic-new-goods-rule')
    info: dict[str, Any] = {}
    evidence.material_facts(entry, cache_root=tmp_path / 'private', cache_info=info)
    assert not info['hit'] and len(parses) == 2


def test_workspace_roots_never_share_facts_or_memory_hits(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path)
    parses = _count_pdf_parses(monkeypatch)
    evidence.material_facts(entry, cache_root=tmp_path / 'one')
    info: dict[str, Any] = {}
    evidence.material_facts(entry, cache_root=tmp_path / 'two', cache_info=info)
    assert not info['hit'] and len(parses) == 2
    assert (tmp_path / 'one/agent-material-cache/facts.sqlite3').is_file()
    assert (tmp_path / 'two/agent-material-cache/facts.sqlite3').is_file()


def test_parse_failures_and_invalid_sources_never_poison_disk_cache(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path)
    original = evidence._words
    calls: list[int] = []
    def temporary_failure(page: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('temporary parser failure')
        return original(page)
    monkeypatch.setattr(evidence, '_words', temporary_failure)
    assert evidence.material_facts(entry, cache_root=tmp_path / 'private')['issues']
    assert evidence.material_facts(entry, cache_root=tmp_path / 'private')['complete']
    assert len(calls) == 2
    with sqlite3.connect(tmp_path / 'private/agent-material-cache/facts.sqlite3') as db:
        assert db.execute('SELECT COUNT(*) FROM material_facts_cache').fetchone()[0] == 1


def test_multiline_multi_goods_amount_quantity_discount_survive_cold_and_warm_cache(tmp_path: Path) -> None:
    path = tmp_path / 'multi.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=650, height=450)
        for x, y, text in ((20, 205, '项目名称'), (155, 205, '规格型号'), (225, 205, '单位'),
                           (285, 205, '数量'), (355, 205, '单价'), (425, 205, '金额'), (565, 205, '税额'),
                           (20, 225, '*日用品*长柄防水'), (20, 239, '合成雨衣'),
                           (225, 225, '件'), (303, 225, '2'), (425, 225, '20.00'), (565, 225, '2.60'),
                           (20, 260, '*文具*合成笔记本'), (225, 260, '本'), (303, 260, '3'),
                           (425, 260, '9.00'), (565, 260, '1.17'),
                           (20, 280, '*日用品*长柄防水合成雨衣'), (425, 280, '-1.00'),
                           (565, 280, '-0.13'), (20, 310, '合计')):
            page.insert_text((x, y), text, fontname='china-s', fontsize=8)
        document.save(path)
    entry = {'id': 'multi', 'path': str(path), 'role': 'purchase', 'fields': {'amount': '28'},
             'digest': hashlib.sha256(path.read_bytes()).hexdigest(), 'revision': 1}
    first = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    second = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    assert first == second
    assert [g['name'] for g in first['goods']] == ['长柄防水合成雨衣', '合成笔记本', '长柄防水合成雨衣']
    assert [g['amount_cents'] for g in first['goods']] == [2000, 900, -100]
    assert first['goods'][0]['quantity'] == '2' and first['goods'][1]['unit'] == '本'
    assert first['goods'][2]['is_discount'] and first['goods'][2]['tax_cents'] == -13
    assert all(g['page'] == 1 and g['bbox'] and g['quote'] for g in first['goods'])


def test_goods_limit_is_explicit_and_does_not_leak_uncapped_descriptions(tmp_path: Path) -> None:
    path = tmp_path / 'many.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=650, height=900)
        for x, text in ((20, '项目名称'), (155, '规格型号'), (225, '单位'), (285, '数量'), (425, '金额')):
            page.insert_text((x, 30), text, fontname='china-s', fontsize=8)
        for index in range(25):
            page.insert_text((20, 60 + index * 24), f'*文具*合成笔记本{index}', fontname='china-s', fontsize=8)
            page.insert_text((425, 60 + index * 24), '1.00', fontsize=8)
        document.save(path)
    result = evidence.material_facts({'id': 'many', 'path': str(path), 'role': 'purchase',
                                      'digest': hashlib.sha256(path.read_bytes()).hexdigest(),
                                      'fields': {'amount': '25'}}, cache_root=tmp_path / 'private')
    assert len(result['goods']) == 20 and len(result['descriptions']) == 20
    assert result['goods_issues'] and result['goods_complete'] is False


def test_same_key_threads_collapse_parser_work_and_isolate_mutable_outputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path)
    original = evidence._words
    calls: list[int] = []
    def slow_parse(page: Any) -> Any:
        calls.append(1)
        time.sleep(.02)
        return original(page)
    monkeypatch.setattr(evidence, '_words', slow_parse)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: evidence.material_facts(entry, cache_root=tmp_path / 'private'), range(12)))
    assert len(calls) == 1 and len(results) == 12
    results[0]['goods'][0]['name'] = 'one consumer'
    assert all(r['goods'][0]['name'] == '饮用水' for r in results[1:])


def test_concurrent_processes_share_one_consistent_disk_record(tmp_path: Path) -> None:
    entry = invoice(tmp_path)
    root = tmp_path / 'private'
    code = '''
import json, sys
from pathlib import Path
from invoice_print_layout.agent_evidence import material_facts
entry, root = json.load(sys.stdin)
print(json.dumps(material_facts(entry, cache_root=Path(root))))
'''
    def read_facts(_: int) -> Any:
        run = subprocess.run([sys.executable, '-c', code], input=json.dumps([entry, str(root)]),
                             text=True, encoding='utf-8', capture_output=True, check=True)
        return json.loads(run.stdout)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(read_facts, range(3)))
    assert all(r == results[0] and r['complete'] for r in results)
    with sqlite3.connect(root / 'agent-material-cache/facts.sqlite3') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT COUNT(*) FROM material_facts_cache').fetchone()[0] == 1


def test_image_role_ocr_is_persisted_but_replacement_is_still_rejected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'test.jpg'
    path.write_bytes(b'fake image valid for mocked local OCR')
    calls: list[int] = []
    def ocr(payload: bytes, suffix: str) -> list[OcrLine]:
        calls.append(1)
        return [OcrLine('购买方信息', 20, 90, 35, 160),
                OcrLine('名称：合成买方公司', 50, 90, 250, 110),
                OcrLine('销售方信息', 330, 90, 345, 160),
                OcrLine('名称：合成卖方公司', 360, 90, 560, 110)]
    monkeypatch.setattr(evidence, 'read_receipt', ocr)
    entry = {'id': 'img', 'path': str(path), 'role': 'invoice', 'text': '购买方信息 销售方信息',
             'digest': hashlib.sha256(path.read_bytes()).hexdigest(), 'fields': {'amount': '22'}}
    first = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    cache.clear_memory_cache()
    second = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    assert first == second and len(calls) == 1
    path.write_bytes(b'changed source')
    assert evidence.material_facts(entry, cache_root=tmp_path / 'private')['issues'] and len(calls) == 1


def test_cache_storage_failure_does_not_block_grounded_extraction(tmp_path: Path) -> None:
    entry = invoice(tmp_path)
    blocked = tmp_path / 'blocked'
    blocked.write_text('not a directory')
    result = evidence.material_facts(entry, cache_root=blocked)
    assert result['complete'] and result['goods'][0]['name'] == '饮用水'


def test_cache_payload_corruption_is_discarded_and_reparsed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = invoice(tmp_path)
    first = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    cache.clear_memory_cache()
    with sqlite3.connect(tmp_path / 'private/agent-material-cache/facts.sqlite3') as db:
        db.execute("UPDATE material_facts_cache SET payload='{}'")
    parses = _count_pdf_parses(monkeypatch)
    assert evidence.material_facts(entry, cache_root=tmp_path / 'private') == first
    assert len(parses) == 1


def test_disk_entry_and_payload_limits_evict_only_disposable_facts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, 'MAX_ENTRIES', 3)
    monkeypatch.setattr(cache, 'MAX_PAYLOAD_BYTES', 300)
    root = tmp_path / 'private'
    for index in range(8):
        cache.source_facts(str(index), lambda: {'text': 'a' * 90, 'issues': []}, root=root)
    with sqlite3.connect(root / 'agent-material-cache/facts.sqlite3') as db:
        count, total = db.execute('SELECT COUNT(*),SUM(payload_bytes) FROM material_facts_cache').fetchone()
        assert count <= 3 and total <= 300
    monkeypatch.setattr(cache, 'MAX_VALUE_BYTES', 30)
    cache.source_facts('oversized', lambda: {'text': 'z' * 100, 'issues': []}, root=root)
    with sqlite3.connect(root / 'agent-material-cache/facts.sqlite3') as db:
        assert not db.execute('SELECT 1 FROM material_facts_cache WHERE cache_key=?', ('oversized',)).fetchone()


def test_full_source_context_remains_contact_filtered_and_does_not_override_manual_fields(tmp_path: Path) -> None:
    entry = invoice(tmp_path)
    entry['manual_fields'] = ['goods']
    entry['fields']['goods'] = '人工补充但原件没有的内容'
    result = evidence.material_facts(entry, cache_root=tmp_path / 'private')
    assert '91320000MA1234567X' not in json.dumps(result, ensure_ascii=False)
    assert '合成测试街道' not in result['source_text']
    assert result['goods'][0]['name'] == '饮用水'
    assert entry['fields']['goods'] == '人工补充但原件没有的内容'
