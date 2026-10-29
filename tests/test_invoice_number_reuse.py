"""Ticket identity reuses visible transcription, with no live cloud requests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pymupdf

from invoice_print_layout import glm_invoice_ai as glm
from invoice_print_layout.material_matching import MaterialMatching
from tests.glm_intake_helpers import FakeAI, NUMBER, local_ocr, pdf, picture, response, setup, update
from tests.test_material_matching import auto_queued, snapshots
from tests.test_glm_invoice_ai import TEXT, extraction, network, png

VISIBLE_NUMBER = '26360000000000000123'
OTHER_NUMBER = '26360000000000000456'


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> glm.GlmInvoiceAI:
    (tmp_path / 'invoice-ai.json').write_text('{"enabled":true}', encoding='utf-8')
    monkeypatch.setenv('GLM_API_KEY', 'synthetic-only-secret-token')
    return glm.GlmInvoiceAI(tmp_path)


@pytest.mark.parametrize('claim,quote', [
    (VISIBLE_NUMBER, None), (VISIBLE_NUMBER, '原件没有的号码'),
    (OTHER_NUMBER, '发票号码：' + OTHER_NUMBER), ('short', '发票号码：short'),
    ('913200123456789012', '税号：913200123456789012'),
    (123, '发票号码：123'), ('1' * 100, '发票号码：' + '1' * 100),
    (VISIBLE_NUMBER, 123), (VISIBLE_NUMBER, 'x' * 1300),
])
def test_unreliable_repeated_ai_number_reuses_unique_native_identity(
        client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch, claim: Any, quote: Any) -> None:
    answer = extraction()
    answer['fields']['invoice_number'] = claim
    if quote is None:
        answer['evidence'].pop('invoice_number')
    else:
        answer['evidence']['invoice_number'] = quote
    network(monkeypatch, answer)
    result = client.extract(TEXT)
    assert result['role'] == 'invoice' and result['issues'] == []
    assert result['fields']['invoice_number'] == VISIBLE_NUMBER
    assert result['evidence']['invoice_number'] == '发票号码：' + VISIBLE_NUMBER
    assert result['fields']['amount'] == '50.00' and result['fields']['merchant'] == '虚构印刷有限公司'
    assert '沿用' in result['field_warnings'][0]
    assert str(claim) not in result['field_warnings'][0]


def test_native_identity_is_read_once_even_if_model_omits_it(
        client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = extraction()
    answer['fields'].pop('invoice_number')
    answer['evidence'].pop('invoice_number')
    opener = network(monkeypatch, answer)
    result = client.extract(TEXT)
    payload = json.loads(opener.calls[0][0].data)
    assert '不再提取invoice_number' in payload['messages'][0]['content']
    assert len(opener.calls) == 1
    assert result['fields']['invoice_number'] == VISIBLE_NUMBER and result['field_warnings'] == []


@pytest.mark.parametrize('claim,quote', [
    (OTHER_NUMBER, '发票号码：' + OTHER_NUMBER), ('short', '发票号码：short'),
    ('913200123456789012', '税号：913200123456789012'), (123, None),
])
def test_unconfirmed_identity_is_discarded_without_losing_core_fields(
        client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch, claim: Any, quote: Any) -> None:
    text = TEXT.replace('发票号码：' + VISIBLE_NUMBER, '发票号码：看不清')
    answer = extraction()
    answer['fields']['invoice_number'] = claim
    answer['evidence']['invoice_number'] = quote
    network(monkeypatch, answer)
    result = client.extract(text)
    assert result['fields'] == {'merchant': '虚构印刷有限公司', 'amount': '50.00'}
    assert 'invoice_number' not in result['evidence']
    assert '发票号码尚未确认' in result['issues'] and result['role'] == 'other'
    assert '金额和商家识别结果保留' in result['field_warnings'][0]


@pytest.mark.parametrize('field', ['amount', 'merchant', 'payment_status'])
def test_bad_core_evidence_still_blocks_extraction(
        client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    answer = extraction()
    if field == 'payment_status':
        answer['role'] = 'payment'
        answer['fields'][field] = 'succeeded'
    answer['evidence'][field] = '没有出现的文字'
    network(monkeypatch, answer)
    with pytest.raises(glm.GlmAIError) as error:
        client.extract(TEXT)
    assert error.value.failure_code == 'evidence_invalid' and error.value.failure_field == field


def test_multiple_visible_numbers_never_choose_one_as_duplicate_key(
        client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    network(monkeypatch, extraction())
    result = client.extract(TEXT + '\n发票号码：' + OTHER_NUMBER)
    assert 'invoice_number' not in result['fields'] and result['role'] == 'other'
    assert any('发票号码冲突' in issue for issue in result['issues'])


def test_visual_transcription_conflicting_with_native_identity_remains_manual(
        client: glm.GlmInvoiceAI, monkeypatch: pytest.MonkeyPatch) -> None:
    answer = extraction(text=TEXT.replace(VISIBLE_NUMBER, OTHER_NUMBER))
    network(monkeypatch, answer)
    result = client.extract(TEXT, images=[png()])
    assert 'invoice_number' not in result['fields']
    assert any('发票号码冲突' in issue for issue in result['issues'])


def test_queue_local_number_unchanged_when_redundant_ai_number_is_rejected(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, project = setup(tmp_path, FakeAI())
    ai = glm.GlmInvoiceAI(tmp_path)
    ai.enabled = True
    queue.ai = ai
    answer = extraction(fields={'amount': '14.00', 'merchant': 'Synthetic Seller', 'invoice_number': 'wrong'},
        evidence={'amount': 'TOTAL WITH TAX:14.00', 'merchant': 'SELLER: Synthetic Seller',
                  'invoice_number': '发票号码：wrong'})
    monkeypatch.setattr(ai, '_request', lambda *_args, **_kwargs: answer)
    content = pdf()
    entry = queue.receive('native.pdf', content, project_id=project)
    result = queue.prepare_batch_entry(entry['id'], force_ai=True)
    assert result['role'] == 'invoice' and result['fields']['invoice_number'] == NUMBER
    assert result['fields']['amount'] == '14.00' and result['fields']['merchant'] == 'Synthetic Seller'
    assert result['field_sources']['invoice_number'] == 'native_pdf'
    assert result['ai_analysis']['status'] == 'ok' and 'AI识别未完成' not in result['issues']
    assert '沿用' in result['ai_analysis']['message']
    assert Path(result['path']).read_bytes() == content and queue.store.list_items() == []
    again = queue.receive('renamed.pdf', content, project_id=project)
    assert again['duplicate'] is True and again['id'] == entry['id']


def test_unclear_ticket_identity_keeps_readable_core_in_queue_for_manual_review(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, project = setup(tmp_path, FakeAI())
    ai = glm.GlmInvoiceAI(tmp_path)
    ai.enabled = True
    queue.ai = ai
    text = TEXT.replace('发票号码：' + VISIBLE_NUMBER, '发票号码：看不清')
    answer = extraction(text=text)
    monkeypatch.setattr(ai, '_request', lambda *_args, **_kwargs: answer)
    local_ocr(monkeypatch, fail=True)
    entry = queue.receive('unclear.png', picture(), project_id=project)
    result = queue.prepare_batch_entry(entry['id'])
    assert result['fields']['amount'] == '50.00' and result['fields']['merchant'] == '虚构印刷有限公司'
    assert not result['fields'].get('invoice_number')
    assert result['ai_analysis']['status'] == 'review' and 'AI识别未完成' not in result['issues']
    assert '发票号码尚未确认' in result['ai_analysis']['message'] and queue.store.list_items() == []


def test_confirmed_number_preserves_existing_invoice_duplicate_detection(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, project = setup(tmp_path, FakeAI())
    first = queue.receive('first.pdf', pdf(), project_id=project)
    first = queue.prepare_batch_entry(first['id'])
    matcher = MaterialMatching(queue.store, queue)
    fields = {'project_type': '其他', 'project_name': 'Synthetic invoice', 'amount': '14.00'}
    created = matcher.create_expense(['q:' + first['id']], [], project,
                                     revisions={first['id']: first['revision']}, fields=fields)
    second = queue.receive('second.pdf', pdf(seller='Another Synthetic Seller'), project_id=project)
    second = queue.prepare_batch_entry(second['id'])
    assert second['fields']['invoice_number'] == NUMBER
    with pytest.raises(ValueError, match='发票号码|关联'):
        matcher.create_expense(['q:' + second['id']], [], project,
                               revisions={second['id']: second['revision']}, fields=fields)
    assert len(queue.store.list_items()) == 1 and created['item']['verified'] is False


@pytest.mark.parametrize('issue', ['发票号码冲突，请拆分材料或对照原件确认。',
                                  'AI与原件字段冲突：invoice_number',
                                  'AI与本地识别的发票号码不同，请对照原件确认。'])
def test_invoice_identity_conflict_blocks_rule_matching_and_suggestions(
        tmp_path: Path, issue: str) -> None:
    queue, project = setup(tmp_path, FakeAI())
    matcher = MaterialMatching(queue.store, queue)
    voucher = auto_queued(matcher, project, 'Synthetic invoice', 'invoice', '14', issues=[issue])
    auto_queued(matcher, project, 'Synthetic payment', 'payment', '14')
    before = snapshots(matcher)
    matched = matcher.auto_match(project)
    suggested = matcher.suggest(project)
    assert matched['matched'] == 0 and suggested['suggestion_count'] == 0
    assert any('发票号码' in conflict['reason'] for conflict in matched['conflicts'])
    assert all('invoice_number' not in conflict['reason'] for conflict in matched['conflicts'])
    assert matcher.intake.get(voucher['id'])['issues'] == [issue] and snapshots(matcher) == before


def test_local_image_multiple_ticket_numbers_remain_conflicted_before_model_suggestions(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = ['电子发票（普通发票）', '发票号码：' + NUMBER, '发票号码：' + OTHER_NUMBER,
            '销售方：合成印刷有限公司', '价税合计：14.00']
    ai = FakeAI(response(text='\n'.join(rows), role='invoice',
                        fields={'amount': '14.00', 'merchant': '合成印刷有限公司', 'invoice_number': NUMBER}))
    queue, project = setup(tmp_path, ai)
    entry = queue.receive('multiple-numbers.png', picture(), project_id=project)
    local_ocr(monkeypatch, rows)
    result = queue.prepare_batch_entry(entry['id'])
    assert len(ai.calls) == 1 and result['fields']['amount'] == '14.00'
    assert not result['fields'].get('invoice_number')
    assert '发票号码冲突，请拆分材料或对照原件确认。' in result['issues']
    assert result['ai_analysis']['status'] == 'review' and queue.store.list_items() == []


def test_native_pdf_multiple_numbers_cannot_skip_ai_and_keep_a_guessed_identity(
        tmp_path: Path) -> None:
    ai = FakeAI(response(role='invoice', fields={'amount': '14.00', 'merchant': 'Synthetic Seller',
                                               'invoice_number': NUMBER}))
    queue, project = setup(tmp_path, ai)
    with pymupdf.open(stream=pdf(), filetype='pdf') as document:
        document[0].insert_text((20, 250), '发票号码：' + OTHER_NUMBER, fontname='china-s', fontsize=10)
        content = document.tobytes()
    entry = queue.receive('two-numbers.pdf', content, project_id=project)
    result = queue.prepare_batch_entry(entry['id'])
    assert len(ai.calls) == 1 and result['fields']['amount'] == '14.00'
    assert not result['fields'].get('invoice_number')
    assert '发票号码冲突，请拆分材料或对照原件确认。' in result['issues']
    assert result['ai_analysis']['status'] == 'review' and queue.store.list_items() == []
    assert Path(result['path']).read_bytes() == content


def test_local_confirmed_identity_retained_when_pdf_label_and_value_are_separated(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, project = setup(tmp_path, FakeAI(response(role='other', fields={'merchant': 'Synthetic Seller', 'amount': '14.00'},
        issues=['发票号码尚未确认', '未能确认正式发票，请领取原票或手动确认材料用途。'])))
    entry = queue.receive('native.pdf', pdf(), project_id=project)
    prepared = queue.prepare_batch_entry(entry['id'])
    separated = '电子发票（普通发票）\n发票号码\n销售方\n' + NUMBER + '\nSynthetic Seller\n价税合计：14.00'
    prepared = update(queue, prepared, text=separated,
                      local_evidence={'invoice_number': NUMBER, 'merchant': 'Synthetic Seller', 'amount': '价税合计：14.00'})
    data = dict(prepared)
    answer = response(text=separated, role='other', fields={'merchant': 'Synthetic Seller', 'amount': '14.00'},
                      issues=['发票号码尚未确认', '未能确认正式发票，请领取原票或手动确认材料用途。'])
    answer['field_warnings'] = ['发票号码尚未确认：未能在提取出的文字中核对，已忽略该号码；金额和商家识别结果保留。']
    monkeypatch.setattr(queue.ai, 'extract', lambda *_args, **_kwargs: answer)
    queue._apply_ai_analysis(data, prepared, set(), images=None, complete=True, native_source='native_pdf', local_ready=True)
    assert data['fields']['invoice_number'] == NUMBER and data['role'] == 'invoice'
    assert '发票号码尚未确认' not in data['issues']
    assert '本地已读取' in data['ai_analysis']['message']
