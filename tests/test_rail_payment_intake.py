"""Redundant railway payments stay archived, without creating expenses."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest
from PIL import Image

from invoice_print_layout import intake_queue as module, mail_search
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.intake_usage import RAIL_PAYMENT_UNUSED_REASON, rail_payment_not_required
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.workbench import ExpenseStore


RAIL_TEXT = '微信支付\n中铁网络\n-357.00\n支付成功\n商品\n12306消费\n商户全称\n中铁网络有限公司'


class StubAI:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.calls = 0

    def extract(self, text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        self.calls += 1
        return {'text': RAIL_TEXT, 'role': 'payment', 'fields': {'amount': '357.00', 'goods': '12306消费',
            'merchant': '中铁网络有限公司', 'payment_status': 'succeeded', 'category': '高铁'},
            'issues': [], 'evidence': {key: RAIL_TEXT for key in
                ('amount', 'goods', 'merchant', 'payment_status', 'category')}, 'model': 'glm-5.3-flash'}


def picture(index: int = 0) -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (90 + index, 190), 'white').save(stream, format='PNG')
    return stream.getvalue()


def setup(tmp_path: Path, *, enabled: bool = False) -> tuple[IntakeQueue, str, StubAI]:
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    ai = StubAI(enabled)
    return IntakeQueue(ExpenseStore(tmp_path), ai=cast(GlmInvoiceAI, ai)), project, ai


def update(queue: IntakeQueue, entry: dict[str, Any], **changes: Any) -> dict[str, Any]:
    entry = {**entry, **changes}
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry, ensure_ascii=False), entry['id']))
    return entry


def record(queue: IntakeQueue, project: str, index: int = 0, **changes: Any) -> dict[str, Any]:
    source = queue.receive('rail.png', picture(index), project_id=project)
    return update(queue, source, role='payment', status='review', text=RAIL_TEXT,
        fields={'amount': '357.00', 'merchant': '中铁网络', 'goods': '12306消费', 'category': '其他'},
        ai_analysis={'status': 'failed', 'message': 'Existing analysis retained'},
        manual_fields=['amount'], issues=['商品不明'], **changes)


def local_ocr(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: '')
    monkeypatch.setattr(module, 'read_receipt', lambda *_: [
        OcrLine(value, 0, index * 30, 400, index * 30 + 20) for index, value in enumerate(text.splitlines())])


@pytest.mark.parametrize('goods', ['12306消费', '12306车票', '铁路客运', '中国铁路客运服务', '铁路电子客票', '高铁票', '火车票'])
def test_source_labelled_passenger_ticket_goods_identify_redundant_payment(goods: str) -> None:
    assert rail_payment_not_required({'role': 'payment', 'text': '商品：' + goods})
    assert rail_payment_not_required({'role': 'payment', 'text': '商品\n' + goods})


@pytest.mark.parametrize('entry', [
    {'role': 'invoice', 'text': '商品：铁路客运'},
    {'role': 'invoice', 'text': '铁路退票费发票\n商品：12306消费'},
    {'role': 'other', 'text': RAIL_TEXT},
    {'role': 'payment', 'status': 'linked', 'text': RAIL_TEXT},
    {'role': 'payment', 'status': 'duplicate', 'text': RAIL_TEXT},
    {'role': 'payment', 'item_id': 'existing', 'text': RAIL_TEXT},
    {'role': 'payment', 'name': '铁路付款12306消费357元.png', 'fields': {'amount': '357', 'category': '高铁'}},
    {'role': 'payment', 'text': '商户全称：中铁物流有限公司\n商品：铁路运输费'},
    {'role': 'payment', 'text': '商品：12306商城'},
    {'role': 'payment', 'text': '商品：铁路货运'},
    {'role': 'payment', 'text': '商家名称：铁路宾馆\n商品：住宿'},
    {'role': 'payment', 'text': '铁路客运\n某店商品', 'fields': {'category': '高铁'}},
    {'role': 'payment', 'text': '支付成功', 'fields': {'goods': '12306消费'}},
    {'role': 'payment', 'text': RAIL_TEXT + '\n当前状态：退款成功'},
    {'role': 'payment', 'text': RAIL_TEXT, 'fields': {'payment_status': 'refunded'}},
])
def test_invoice_generic_railway_and_filename_do_not_exclude_other_materials(entry: dict[str, Any]) -> None:
    assert not rail_payment_not_required(entry)


def test_verified_goods_quote_and_manual_goods_are_accepted() -> None:
    assert rail_payment_not_required({'role': 'payment', 'text': '12306消费',
        'fields': {'goods': '12306消费'}, 'local_evidence': {'goods': '12306消费'}})
    assert rail_payment_not_required({'role': 'payment', 'fields': {'goods': '12306消费'}, 'manual_fields': ['goods']})
    assert not rail_payment_not_required({'role': 'payment', 'text': '其他商品',
        'fields': {'goods': '12306消费'}, 'local_evidence': {'goods': '12306消费'}})


@pytest.mark.parametrize('enabled,force_ai', [(False, False), (True, False), (True, True)])
def test_prepare_local_rail_payment_is_archived_without_cloud_call(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, force_ai: bool) -> None:
    queue, project, ai = setup(tmp_path, enabled=enabled)
    source = queue.receive('payment.png', picture(), project_id=project)
    local_ocr(monkeypatch, RAIL_TEXT)
    result = queue.prepare_batch_entry(source['id'], force_ai=force_ai)
    assert result['status'] == 'unused' and result['unused_reason'] == RAIL_PAYMENT_UNUSED_REASON
    assert result['fields']['amount'] == '357.00' and result['fields']['goods'] == '12306消费'
    assert result['revision'] == 1 and ai.calls == 0
    assert queue.prepare_batch_entry(source['id'], force_ai=True) == result
    assert queue.analyze(source['id']) == result and ai.calls == 0
    assert queue.store.list_items() == [] and Path(result['path']).read_bytes() == picture()


def test_legacy_analyze_archives_rail_payment_before_organizing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, project, ai = setup(tmp_path)
    source = queue.receive('legacy.png', picture(), project_id=project)
    local_ocr(monkeypatch, RAIL_TEXT + '\n实付款：357.00')
    result = queue.analyze(source['id'])
    assert result['status'] == 'unused' and result['fields']['amount'] == '357.00'
    assert result['revision'] == 1 and ai.calls == 0 and queue.store.list_items() == []


@pytest.mark.parametrize('legacy', [False, True])
def test_local_exclusion_refuses_changed_original_before_saving(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool) -> None:
    queue, project, _ = setup(tmp_path)
    source = queue.receive('payment.png', picture(), project_id=project)
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: '')
    def read(*_: Any) -> list[OcrLine]:
        Path(source['path']).write_bytes(picture(1))
        return [OcrLine(value, 0, index * 30, 400, index * 30 + 20)
            for index, value in enumerate(RAIL_TEXT.splitlines())]
    monkeypatch.setattr(module, 'read_receipt', read)
    with pytest.raises(ValueError, match='原件已变化'):
        (queue.analyze if legacy else queue.prepare_batch_entry)(source['id'])
    assert queue.get(source['id']) == source and queue.store.list_items() == []


def test_visual_fallback_identifies_rail_payment_then_archives_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, project, ai = setup(tmp_path, enabled=True)
    source = queue.receive('scan.png', picture(), project_id=project)
    local_ocr(monkeypatch, '')
    result = queue.prepare_batch_entry(source['id'])
    assert result['status'] == 'unused' and result['fields']['amount'] == '357.00'
    assert result['ai_analysis']['status'] == 'ok' and ai.calls == 1
    assert queue.prepare_batch_entry(source['id'], force_ai=True) == result and ai.calls == 1
    assert queue.store.list_items() == []


def test_native_rail_invoice_remains_review_and_original_preserved(tmp_path: Path) -> None:
    queue, project, ai = setup(tmp_path)
    with pymupdf.open() as document:
        page = document.new_page()
        for index, text in enumerate(['电子发票（铁路电子客票）', '发票号码：12345678901234567890',
                'TOTAL WITH TAX:25.00', 'SELLER: Synthetic Railway', '商品：铁路客运']):
            page.insert_text((20, 40 + index * 30), text, fontname='china-s', fontsize=10)
        payload = document.tobytes()
    source = queue.receive('ticket.pdf', payload, project_id=project)
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'invoice' and result['status'] == 'review'
    assert result['fields']['category'] == '高铁' and result['fields']['amount'] == '25.00'
    assert not result.get('unused_reason') and ai.calls == 0 and queue.store.list_items() == []
    assert Path(result['path']).read_bytes() == payload


def test_batch_archives_exact_selected_rows_preserving_every_other_field(tmp_path: Path) -> None:
    queue, project, _ = setup(tmp_path)
    entries = [record(queue, project, index) for index in range(4)]
    unrelated = record(queue, project, 4)
    originals = {entry['id']: Path(entry['path']).read_bytes() for entry in entries}
    result = queue.mark_rail_payments_unused([entry['id'] for entry in entries], {entry['id']: entry['revision'] for entry in entries})
    assert result['marked'] == 4 and result['unchanged'] == 0
    for previous, current in zip(entries, result['entries']):
        expected = {**previous, 'status': 'unused', 'revision': previous['revision'] + 1,
            'unused_reason': RAIL_PAYMENT_UNUSED_REASON, 'result': RAIL_PAYMENT_UNUSED_REASON}
        visible_expected = {**expected, 'issues': []}
        assert current == visible_expected and queue.get(current['id']) == visible_expected
        with queue.store.connect() as db:
            assert json.loads(db.execute('SELECT data FROM intake_queue WHERE id=?',
                                         (current['id'],)).fetchone()['data']) == expected
        assert hashlib.sha256(Path(current['path']).read_bytes()).hexdigest() == previous['digest']
        assert Path(current['path']).read_bytes() == originals[current['id']]
    assert queue.get(unrelated['id']) == {**unrelated, 'issues': []} and queue.store.list_items() == []
    with queue.store.connect() as db:
        assert json.loads(db.execute('SELECT data FROM intake_queue WHERE id=?',
                                     (unrelated['id'],)).fetchone()['data']) == unrelated
    repeated = queue.mark_rail_payments_unused([entry['id'] for entry in result['entries']],
        {entry['id']: entry['revision'] for entry in result['entries']})
    assert repeated['marked'] == 0 and repeated['unchanged'] == 4 and repeated['entries'] == result['entries']
    with queue.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


@pytest.mark.parametrize('failure', ['stale', 'linked', 'duplicate', 'different_project', 'no_project', 'original', 'not_rail', 'attached', 'missing'])
def test_batch_validation_failure_never_changes_any_selected_row(tmp_path: Path, failure: str) -> None:
    queue, project, _ = setup(tmp_path)
    first, second = record(queue, project), record(queue, project, 1)
    expected = {first['id']: 0, second['id']: 0}
    if failure == 'stale':
        expected[second['id']] = 1
    elif failure in {'linked', 'duplicate'}:
        second = update(queue, second, status=failure, item_id='existing')
    elif failure == 'different_project':
        other = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Other')['id']
        second = update(queue, second, project_id=other)
    elif failure == 'no_project':
        second = update(queue, second, project_id='')
    elif failure == 'original':
        Path(second['path']).write_bytes(picture(5))
    elif failure == 'not_rail':
        second = update(queue, second, text='商品：铁路货运', fields={'amount': '357.00', 'goods': '铁路货运'})
    elif failure == 'attached':
        owner = queue.store.create({'title': 'Existing', 'amount': '357'})
        queue.store.add_attachment(owner['id'], 'same.png', Path(second['path']).read_bytes(), 'payment')
    elif failure == 'missing':
        with queue.store.connect() as db:
            db.execute('DELETE FROM intake_queue WHERE id=?', (second['id'],))
    before = copy.deepcopy(queue.list())
    with pytest.raises(ValueError):
        queue.mark_rail_payments_unused([first['id'], second['id']], expected)
    assert queue.list() == before


@pytest.mark.parametrize('ids,revisions', [([], {}), (['a', 'a'], {'a': 0}), (['a'], {}),
    (['a'], {'a': True}), (['a'], {'a': -1}), (['a'], {'a': 0, 'other': 0})])
def test_batch_rejects_invalid_versions_without_changes(tmp_path: Path, ids: list[str], revisions: dict[str, int]) -> None:
    queue, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        queue.mark_rail_payments_unused(ids, revisions)
    assert queue.list() == []


@pytest.mark.parametrize('archive', [False, True])
def test_direct_resolve_cannot_create_expense_from_redundant_rail_payment(tmp_path: Path, archive: bool) -> None:
    queue, project, _ = setup(tmp_path)
    entry = record(queue, project)
    if archive:
        entry = queue.mark_rail_payments_unused([entry['id']], {entry['id']: 0})['entries'][0]
    before = copy.deepcopy(queue.list())
    with pytest.raises(ValueError, match='无需使用'):
        queue.resolve(entry['id'], entry['revision'], {'title': 'Rail payment', 'amount': '357'}, 'payment')
    assert queue.list() == before and queue.store.list_items() == []
