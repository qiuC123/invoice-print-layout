"""Descriptive hints are optional; proven money and identities remain mandatory."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image
import pymupdf
import pytest

from invoice_print_layout import intake_queue as module, mail_search
from invoice_print_layout.glm_invoice_ai import (FIELD_LIMITS, GlmAIError, GlmInvoiceAI,
                                               optional_material_issue)
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.workbench import ExpenseStore


NUMBER = '12345678901234567890'
TEXT = '电子发票（普通发票）\n发票号码：' + NUMBER + '\nSELLER: Synthetic Seller\nTOTAL WITH TAX:20.86'


def answer() -> dict[str, Any]:
    return {'text': '', 'role': 'invoice', 'fields': {'amount': '20.86', 'merchant': 'Synthetic Seller'},
            'evidence': {'amount': 'TOTAL WITH TAX:20.86', 'merchant': 'SELLER: Synthetic Seller'}, 'issues': []}


class ValidatedAI(GlmInvoiceAI):
    def __init__(self, value: dict[str, Any] | None = None) -> None:
        self.enabled = True
        self.value = value
        self.calls = 0

    def extract(self, text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        self.calls += 1
        assert self.value is not None, 'Clear local core must not call assistance'
        return self._validate_extraction(copy.deepcopy(self.value), text, bool(images))


def native_pdf() -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        for index, value in enumerate(TEXT.splitlines()):
            page.insert_text((20, 40 + index * 30), value, fontname='china-s', fontsize=10)
        return document.tobytes()


def picture() -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (20, 20), 'white').save(output, format='PNG')
    return output.getvalue()


def setup(tmp_path: Path, ai: ValidatedAI) -> tuple[IntakeQueue, str]:
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    return IntakeQueue(ExpenseStore(tmp_path), ai=ai), project


def local_ocr(monkeypatch: pytest.MonkeyPatch, rows: list[str]) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: '')
    monkeypatch.setattr(module, 'read_receipt', lambda *_: [
        OcrLine(value, 0, index * 30, 400, index * 30 + 20) for index, value in enumerate(rows)])


@pytest.mark.parametrize('kind', ['invoice', 'payment'])
def test_local_core_without_goods_category_date_or_note_does_not_call_ai(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    ai = ValidatedAI()
    queue, project = setup(tmp_path, ai)
    if kind == 'invoice':
        received = queue.receive('clear.pdf', native_pdf(), project_id=project)
    else:
        received = queue.receive('clear.png', picture(), project_id=project)
        local_ocr(monkeypatch, ['微信支付', '商家名称：明确商家有限公司', '付款金额20.86', '支付成功'])
    result = queue.prepare_batch_entry(received['id'])
    assert ai.calls == 0 and result['ai_analysis']['status'] == 'not_needed'
    assert result['issues'] == [] and result['fields']['amount'] == '20.86'
    assert result['fields']['category'] == '其他' and not result['fields'].get('goods')
    assert result['fields']['expense_date'] == '' and result['role'] == kind
    assert not queue.store.list_items()
    assert Path(result['path']).read_bytes() == Path(received['path']).read_bytes()


@pytest.mark.parametrize('label', ['商品说明', '付款备注', '交易说明'])
def test_visible_optional_description_is_preserved_without_classification_or_ai(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    ai = ValidatedAI()
    queue, project = setup(tmp_path, ai)
    received = queue.receive('payment.png', picture(), project_id=project)
    local_ocr(monkeypatch, ['微信支付', '商家名称：明确商家有限公司', '付款金额20.86', '支付成功',
                            label + '：合成明珠宾馆住宿费'])
    result = queue.prepare_batch_entry(received['id'])
    assert result['fields']['goods'] == '合成明珠宾馆住宿费'
    assert result['local_evidence']['goods'] == label + '：合成明珠宾馆住宿费'
    assert label + '：合成明珠宾馆住宿费' in result['text']
    assert result['issues'] == [] and ai.calls == 0 and not queue.store.list_items()


@pytest.mark.parametrize('field', ['goods', 'category', 'title', 'note', 'expense_date'])
@pytest.mark.parametrize('defect', ['missing_quote', 'invented_quote', 'non_string', 'too_long'])
def test_invalid_optional_ai_field_is_discarded_without_losing_core(field: str, defect: str) -> None:
    result = answer()
    result['fields'][field] = '机器生成的内容'
    if defect == 'invented_quote':
        result['evidence'][field] = '不存在于原件的文字'
    elif defect == 'non_string':
        result['fields'][field] = {'unexpected': 'object'}
    elif defect == 'too_long':
        result['fields'][field] = 'x' * (FIELD_LIMITS[field] + 1)
    checked = GlmInvoiceAI._validate_extraction(result, TEXT, False)
    assert field not in checked['fields'] and field not in checked['evidence']
    assert checked['fields'] == {'amount': '20.86', 'merchant': 'Synthetic Seller', 'invoice_number': NUMBER}
    assert checked['role'] == 'invoice' and checked['issues'] == [] and checked['field_warnings']


@pytest.mark.parametrize('field,label', [('goods', '商品说明'), ('note', '付款备注')])
def test_optional_goods_or_note_keeps_only_literal_source_text(field: str, label: str) -> None:
    result = answer()
    result['fields'][field] = '合成明珠宾馆住宿费'
    result['evidence'][field] = label + '：合成明珠宾馆住宿费'
    checked = GlmInvoiceAI._validate_extraction(result, TEXT + '\n' + result['evidence'][field], False)
    assert checked['fields'][field] == '合成明珠宾馆住宿费' and checked['issues'] == []
    result['fields'][field] = '模型概括的另一家宾馆'
    checked = GlmInvoiceAI._validate_extraction(result, TEXT + '\n' + result['evidence'][field], False)
    assert field not in checked['fields'] and checked['fields']['amount'] == '20.86'


def test_optional_ai_problem_does_not_leave_material_in_failed_state(tmp_path: Path) -> None:
    value = answer()
    value['fields']['goods'] = '生成的商品名称'
    value['evidence']['goods'] = '原文没有的描述'
    value['issues'] = ['商品不明', '消费日期未能识别', '费用类别未能确定']
    ai = ValidatedAI(value)
    queue, project = setup(tmp_path, ai)
    entry = queue.receive('clear.pdf', native_pdf(), project_id=project)
    prepared = queue.prepare_batch_entry(entry['id'], force_ai=True)
    assert ai.calls == 1 and prepared['ai_analysis']['status'] == 'ok'
    assert prepared['issues'] == [] and prepared['ai_optional_notes']
    assert prepared['fields']['amount'] == '20.86' and prepared['fields']['invoice_number'] == NUMBER
    assert not prepared['fields'].get('goods') and not queue.store.list_items()


def test_descriptive_date_ambiguity_alone_does_not_block_proven_transaction(tmp_path: Path) -> None:
    value = answer()
    value['issues'] = ['消费日期存在多个不一致记录']
    queue, project = setup(tmp_path, ValidatedAI(value))
    entry = queue.receive('clear.pdf', native_pdf(), project_id=project)
    result = queue.prepare_batch_entry(entry['id'], force_ai=True)
    assert result['issues'] == [] and result['ai_analysis']['status'] == 'ok'
    assert result['fields']['amount'] == '20.86' and not result['fields'].get('expense_date')


@pytest.mark.parametrize('field', ['amount', 'merchant'])
def test_unproven_core_ai_fields_still_fail(field: str) -> None:
    value = answer()
    value['evidence'][field] = '机器编造的证据20.86'
    with pytest.raises(GlmAIError) as error:
        GlmInvoiceAI._validate_extraction(value, TEXT, False)
    assert error.value.failure_code == 'evidence_invalid' and error.value.failure_field == field


@pytest.mark.parametrize('issue', ['商品不明但金额冲突', '商品说明对应商家存在冲突',
                                  '商品备注出现退款', '消费日期缺少，付款状态未确认'])
def test_optional_issue_filter_retains_mixed_critical_blockers(issue: str) -> None:
    assert not optional_material_issue(issue)


def test_old_unknown_goods_hint_is_hidden_without_rewriting_stored_row(tmp_path: Path) -> None:
    queue, project = setup(tmp_path, ValidatedAI())
    received = queue.receive('old.pdf', native_pdf(), project_id=project)
    old = {**received, 'issues': ['商品不明', '金额冲突', '归属不明']}
    raw = json.dumps(old, ensure_ascii=False)
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (raw, old['id']))
    assert queue.get(old['id'])['issues'] == queue.list()[0]['issues'] == ['金额冲突', '归属不明']
    with queue.store.connect() as db:
        assert db.execute('SELECT data FROM intake_queue WHERE id=?', (old['id'],)).fetchone()['data'] == raw


@pytest.mark.parametrize('category', [None, '', '其他'])
def test_legacy_organizer_does_not_create_unclassified_expense_when_hint_removed(
        tmp_path: Path, category: str | None) -> None:
    queue, project = setup(tmp_path, ValidatedAI())
    received = queue.receive('local.pdf', native_pdf(), project_id=project)
    prepared = queue.prepare_batch_entry(received['id'])
    prepared['fields']['category'] = category
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                   (json.dumps(prepared, ensure_ascii=False), prepared['id']))
    result = queue.organize(prepared['id'])
    assert result['status'] == 'review' and result['issues'] == [] and not result['item_id']
    assert not queue.store.list_items()


def test_missing_descriptive_fields_can_still_produce_readonly_match_candidate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ai = ValidatedAI()
    queue, project = setup(tmp_path, ai)
    invoice = queue.receive('invoice.pdf', native_pdf(), project_id=project)
    invoice = queue.prepare_batch_entry(invoice['id'])
    paid = queue.receive('payment.png', picture(), project_id=project)
    local_ocr(monkeypatch, ['微信支付', '商家名称：Synthetic Seller', '付款金额20.86', '支付成功'])
    paid = queue.prepare_batch_entry(paid['id'])
    matcher = MaterialMatching(queue.store, queue)
    matcher.ai = None
    result = matcher.suggest(project)
    assert len(result['suggestions']) == 1 and result['engine'] == 'rules' and result['requests'] == 0
    assert result['suggestions'][0]['voucher_keys'] == ['q:' + invoice['id']]
    assert result['suggestions'][0]['payment_ids'] == [paid['id']]
    assert queue.get(invoice['id']) == invoice and queue.get(paid['id']) == paid
    assert not queue.store.list_items() and ai.calls == 0
