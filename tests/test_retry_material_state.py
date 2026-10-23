"""Successful local retries clear stale cloud failures without network access."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pymupdf
import pytest

from invoice_print_layout.glm_invoice_ai import GlmAIError, GlmInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore


NUMBER = '12345678901234567890'


class StubAI:
    def __init__(self, enabled: bool, *, fail: bool = False):
        self.enabled = enabled
        self.fail = fail
        self.calls = 0

    def extract(self, text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        assert self.enabled, 'Disabled assistance must not be invoked'
        self.calls += 1
        if self.fail:
            raise GlmAIError('Synthetic failure', failure_code='evidence_invalid', failure_field='goods')
        return {'text': text, 'role': 'invoice', 'fields': {'category': '餐饮'},
                'issues': [], 'evidence': {'category': text}, 'model': 'glm-5.3-flash'}


def pdf() -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        for index, text in enumerate(['电子发票（普通发票）', '发票号码：' + NUMBER,
                'TOTAL WITH TAX:20.86', 'SELLER: Synthetic Seller', '商品：晚餐']):
            page.insert_text((20, 40 + index * 30), text, fontname='china-s', fontsize=10)
        return document.tobytes()


def setup(tmp_path: Path, *, enabled: bool = False, fail: bool = False) -> tuple[IntakeQueue, str, StubAI]:
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    ai = StubAI(enabled, fail=fail)
    return IntakeQueue(ExpenseStore(tmp_path), ai=cast(GlmInvoiceAI, ai)), project, ai


def update(queue: IntakeQueue, entry: dict[str, Any], **changes: Any) -> dict[str, Any]:
    saved = {**entry, **changes}
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), saved['id']))
    return saved


def stale(queue: IntakeQueue, prepared: dict[str, Any], status: str) -> dict[str, Any]:
    return update(queue, prepared,
        ai_analysis={'status': status, 'failure_code': 'evidence_invalid', 'failure_field': 'invoice_number',
                     'message': 'Old invoice-number failure', 'revision': prepared['revision']},
        ai_text='Old transcript', ai_evidence={'invoice_number': 'Old incorrect quote'},
        ai_optional_notes=['Old warning'], issues=[*prepared['issues'], 'AI识别未完成'])


def assert_ledger_empty(queue: IntakeQueue) -> None:
    with queue.store.connect() as db:
        assert all(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
                   for table in ('expenses', 'attachments', 'events'))


@pytest.mark.parametrize('enabled,force_ai', [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize('old_status', ['failed', 'review'])
def test_local_retry_clears_stale_ai_metadata_when_assistance_is_disabled_or_unneeded(
        tmp_path: Path, enabled: bool, force_ai: bool, old_status: str) -> None:
    queue, project, ai = setup(tmp_path, enabled=enabled)
    source = queue.receive('clear.pdf', pdf(), project_id=project)
    first = queue.prepare_batch_entry(source['id'])
    previous = stale(queue, first, old_status)
    result = queue.prepare_batch_entry(source['id'], force_ai=force_ai)
    assert ai.calls == 0 and result['ai_analysis']['status'] == 'not_needed'
    assert result['ai_analysis']['revision'] == result['revision'] == previous['revision'] + 1
    assert result['ai_analysis']['source_sha256'] == source['digest']
    assert not ({'failure_code', 'failure_field'} & result['ai_analysis'].keys())
    assert not ({'ai_text', 'ai_evidence', 'ai_optional_notes'} & result.keys())
    assert result['fields'] == first['fields'] and result['role'] == first['role'] == 'invoice'
    assert result['project_id'] == project and result['item_id'] == '' and result['status'] == 'review'
    assert result['issues'] == [] and '未调用AI' in result['result']
    assert result['fields']['amount'] == '20.86' and result['fields']['invoice_number'] == NUMBER
    assert Path(result['path']).read_bytes() == Path(source['path']).read_bytes()
    assert hashlib.sha256(Path(result['path']).read_bytes()).hexdigest() == source['digest']
    assert queue.get(source['id']) == result
    repeated = queue.prepare_batch_entry(source['id'], force_ai=force_ai)
    assert repeated['fields'] == result['fields'] and repeated['ai_analysis']['status'] == 'not_needed'
    assert repeated['revision'] == result['revision'] + 1 and ai.calls == 0
    assert_ledger_empty(queue)


@pytest.mark.parametrize('force_ai', [False, True])
def test_fresh_offline_invoice_does_not_gain_ai_metadata(tmp_path: Path, force_ai: bool) -> None:
    queue, project, ai = setup(tmp_path)
    source = queue.receive('fresh.pdf', pdf(), project_id=project)
    result = queue.prepare_batch_entry(source['id'], force_ai=force_ai)
    assert 'ai_analysis' not in result and ai.calls == 0
    assert result['fields']['amount'] == '20.86' and result['project_id'] == project
    assert_ledger_empty(queue)


@pytest.mark.parametrize('old_status', ['failed', 'review'])
@pytest.mark.parametrize('fail', [False, True])
def test_explicit_enabled_ai_retry_runs_once_and_keeps_proven_core(
        tmp_path: Path, old_status: str, fail: bool) -> None:
    queue, project, ai = setup(tmp_path, enabled=True, fail=fail)
    source = queue.receive('clear.pdf', pdf(), project_id=project)
    first = queue.prepare_batch_entry(source['id'])
    previous = stale(queue, first, old_status)
    result = queue.prepare_batch_entry(source['id'], force_ai=True)
    assert ai.calls == 1 and result['revision'] == previous['revision'] + 1
    assert result['ai_analysis']['status'] == ('failed' if fail else 'ok')
    assert result['fields']['amount'] == first['fields']['amount'] == '20.86'
    assert result['fields']['merchant'] == first['fields']['merchant'] == 'Synthetic Seller'
    assert result['fields']['invoice_number'] == NUMBER and result['role'] == 'invoice'
    assert result['project_id'] == project and result['status'] == 'review'
    assert 'AI识别未完成' not in result['issues']
    if fail:
        assert result['ai_analysis']['failure_code'] == 'evidence_invalid'
        assert result['ai_analysis']['failure_field'] == 'goods'
    assert_ledger_empty(queue)


@pytest.mark.parametrize('blocker', ['金额冲突', '商家冲突', '发票号码冲突，请拆分材料或对照原件确认。',
                                    '多页材料未完整识别', 'missing_merchant', 'missing_number', 'incomplete'])
def test_offline_stale_state_is_not_cleared_without_current_complete_core(
        tmp_path: Path, blocker: str) -> None:
    queue, project, ai = setup(tmp_path)
    source = queue.receive('uncertain.pdf', pdf(), project_id=project)
    previous = stale(queue, queue.prepare_batch_entry(source['id']), 'failed')
    candidate = copy.deepcopy(previous)
    candidate['issues'] = ['商品不明']
    if blocker == 'missing_merchant':
        candidate['fields']['merchant'] = ''
    elif blocker == 'missing_number':
        candidate['fields']['invoice_number'] = ''
    elif blocker != 'incomplete':
        candidate['issues'].append(blocker)
    result = queue._finish_batch_analysis(candidate, previous, native_source='native_pdf',
                                          complete=blocker != 'incomplete')
    assert result['ai_analysis'] == previous['ai_analysis'] and ai.calls == 0
    assert result['status'] == 'review' and result['project_id'] == project
    assert '未调用AI' not in result['result']
    assert_ledger_empty(queue)


def test_successful_local_retry_preserves_confirmed_manual_fields_and_role(tmp_path: Path) -> None:
    queue, project, ai = setup(tmp_path)
    source = queue.receive('manual.pdf', pdf(), project_id=project)
    first = queue.prepare_batch_entry(source['id'])
    previous = stale(queue, first, 'failed')
    fields = {**first['fields'], 'amount': '88.00', 'merchant': 'Manual Seller', 'title': 'Manual title'}
    update(queue, previous, fields=fields, role='receipt', manual_role=True,
           manual_fields=['amount', 'merchant', 'title'])
    result = queue.prepare_batch_entry(source['id'])
    assert result['ai_analysis']['status'] == 'not_needed' and ai.calls == 0
    assert result['role'] == 'receipt' and all(result['fields'][key] == fields[key]
                                             for key in ('amount', 'merchant', 'title'))
    assert all(result['field_sources'][key] == 'manual' for key in ('amount', 'merchant', 'title'))
    assert result['manual_role'] and result['project_id'] == project
    assert_ledger_empty(queue)


@pytest.mark.parametrize('status', ['linked', 'duplicate', 'unused'])
def test_stale_ai_metadata_of_bound_or_unused_material_is_untouched(tmp_path: Path, status: str) -> None:
    queue, project, ai = setup(tmp_path, enabled=True)
    source = queue.receive('protected.pdf', pdf(), project_id=project)
    previous = stale(queue, queue.prepare_batch_entry(source['id']), 'failed')
    previous = update(queue, previous, status=status, item_id='synthetic-item' if status == 'linked' else '')
    assert queue.prepare_batch_entry(source['id'], force_ai=True) == previous
    assert ai.calls == 0
    assert_ledger_empty(queue)


def test_offline_stale_recovery_checks_original_digest_before_saving(tmp_path: Path) -> None:
    queue, project, ai = setup(tmp_path)
    source = queue.receive('changed.pdf', pdf(), project_id=project)
    previous = stale(queue, queue.prepare_batch_entry(source['id']), 'failed')
    candidate = copy.deepcopy(previous)
    candidate['issues'] = ['商品不明']
    Path(source['path']).write_bytes(b'Changed synthetic original')
    with pytest.raises(ValueError, match='材料原件已变化'):
        queue._finish_batch_analysis(candidate, previous, native_source='native_pdf')
    assert queue.get(source['id']) == previous and ai.calls == 0
    assert_ledger_empty(queue)
