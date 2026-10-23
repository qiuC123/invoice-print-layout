"""Source-labelled transaction roles and low-frequency, injected AI fallback."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast

from PIL import Image
import pytest

from invoice_print_layout import intake_queue as intake
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI, GlmAIError
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.transaction_evidence import record_identifier_keys
from invoice_print_layout.workbench import ExpenseStore


class OfflineAI:
    enabled = True

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response
        self.calls: list[tuple[str, list[bytes] | None]] = []

    def extract(self, text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        self.calls.append((text, images))
        if self.response is None:
            pytest.fail('Complete local evidence must not invoke the model')
        return copy.deepcopy(self.response)


def image() -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (120, 240), 'white').save(stream, format='PNG')
    return stream.getvalue()


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[str],
          response: dict[str, Any] | None = None) -> tuple[IntakeQueue, OfflineAI, dict[str, Any]]:
    from invoice_print_layout import mail_search
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: '')
    monkeypatch.setattr(intake, 'read_receipt', lambda *_: [
        OcrLine(row, 0, n * 30, 500, n * 30 + 20) for n, row in enumerate(rows)])
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic roles')['id']
    ai = OfflineAI(response)
    queue = IntakeQueue(store, ai=cast(GlmInvoiceAI, ai))
    entry = queue.receive('roles.png', image(), project_id=project)
    return queue, ai, entry


def payment_rows(*, channel: str = '支付宝', number: str = '20261016000012345678') -> list[str]:
    return ['账单详情', channel, '淘宝闪购', '-17.00', '交易成功',
            '商品说明：合成便利店（南门店）', '支付时间：2026-10-16 16:23:47',
            '订单号：' + number, '商家订单号：13160000000001234567']


def response(*, merchant: str = '合成便利店（南门店）', amount: str = '17.00',
             role: str = 'payment', text: str = '') -> dict[str, Any]:
    text = text or '\n'.join(payment_rows())
    fields = {'amount': amount, 'merchant': merchant, 'payment_status': 'succeeded'}
    return {'text': text, 'role': role, 'fields': fields, 'issues': [],
            'evidence': {key: text for key in fields}}


def test_local_payment_separates_roles_and_never_prepares_ai_image(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = payment_rows()
    queue, ai, entry = setup(tmp_path, monkeypatch, rows)
    monkeypatch.setattr(queue, '_bounded_vision_png', lambda *_: pytest.fail('Unneeded visual derivative'))
    result = queue.prepare_batch_entry(entry['id'])
    assert ai.calls == [] and result['ai_analysis']['status'] == 'not_needed'
    assert result['fields']['shopping_platform'] == '淘宝闪购'
    assert result['fields']['shop_name'] == '合成便利店(南门店)'
    assert result['fields']['payment_channel'] == '支付宝'
    assert result['fields']['merchant'] == '合成便利店(南门店)'
    assert result['fields']['amount'] == '17.00'
    for field in ('shopping_platform', 'shop_name', 'payment_channel'):
        assert result['field_sources'][field] == 'local_ocr'
        quote = result['transaction_evidence'][field]
        assert quote and quote in '\n'.join(rows)
    ids = result['transaction_identifiers']
    assert {item['kind'] for item in ids} == {'payment_order', 'merchant_order'}
    assert all(item['quote'] in result['text'] for item in ids)
    assert len(record_identifier_keys(result)) == 2
    assert not queue.store.list_items()
    assert hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() == entry['digest']


def test_completed_platform_order_with_thanks_text_is_locally_ready_without_optional_number(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = ['订单已送达', '感谢你对淘宝闪购的信任，期待再次光临',
            '闪购合成便利店（南门店）', '商品明细', '纸巾', '实付款￥17.00']
    queue, ai, entry = setup(tmp_path, monkeypatch, rows)
    result = queue.prepare_batch_entry(entry['id'])
    assert ai.calls == [] and result['ai_analysis']['status'] == 'not_needed'
    assert result['role'] in {'order', 'purchase'}
    assert result['fields']['shopping_platform'] == '淘宝闪购'
    assert result['fields']['shop_name'] == '合成便利店(南门店)'
    assert not result['fields'].get('payment_channel')
    assert not result['fields'].get('order_number')
    assert not result['transaction_identifiers'] and result['issues'] == []


def test_bank_funding_source_does_not_invent_channel_or_trigger_ai(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = payment_rows(channel='付款方式：农业银行储蓄卡(0000)')
    queue, ai, entry = setup(tmp_path, monkeypatch, rows)
    result = queue.prepare_batch_entry(entry['id'])
    assert ai.calls == [] and not result['fields'].get('payment_channel')
    assert result['fields']['shop_name'] == '合成便利店(南门店)'
    assert result['fields']['amount'] == '17.00'
    assert not any(item['namespace'] for item in result['transaction_identifiers']
                   if item['kind'] == 'payment_order')


@pytest.mark.parametrize('force', [False, True])
def test_only_actual_fallback_or_explicit_recheck_prepares_bounded_original_image(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool) -> None:
    rows = payment_rows() if force else ['支付宝', '交易成功', '商户名称：合成便利店（南门店）']
    queue, ai, entry = setup(tmp_path, monkeypatch, rows, response())
    result = queue.prepare_batch_entry(entry['id'], force_ai=force)
    assert len(ai.calls) == 1
    assert ai.calls[0][0] == '\n'.join(rows)
    pictures = ai.calls[0][1]
    assert pictures and len(pictures) == 1 and pictures[0].startswith(b'\x89PNG')
    assert result['ai_analysis']['status'] in {'ok', 'review'}
    assert Path(entry['path']).read_bytes() == image()
    assert not queue.store.list_items()


def test_fallback_failure_keeps_local_money_and_roles_without_creating_expense(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, ai, entry = setup(tmp_path, monkeypatch, payment_rows(), response())
    def failure(text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        ai.calls.append((text, images))
        raise GlmAIError('', failure_code='network_error')
    monkeypatch.setattr(ai, 'extract', failure)
    result = queue.prepare_batch_entry(entry['id'], force_ai=True)
    assert result['ai_analysis']['status'] == 'failed'
    assert result['fields']['amount'] == '17.00'
    assert result['fields']['shop_name'] == '合成便利店(南门店)'
    assert result['field_sources']['shop_name'] == 'local_ocr'
    assert result['issues'] == [] and not queue.store.list_items()


def test_manual_actor_fields_and_custom_notes_are_not_replaced_on_local_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, ai, entry = setup(tmp_path, monkeypatch, payment_rows())
    entry['fields'] = {'shopping_platform': '人工指定平台', 'shop_name': '人工指定分店',
                       'payment_channel': '人工指定渠道', 'custom_note': '人工保留'}
    entry['manual_fields'] = list(entry['fields'])
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                   (json.dumps(entry, ensure_ascii=False), entry['id']))
    result = queue.prepare_batch_entry(entry['id'])
    assert ai.calls == []
    for key, value in entry['fields'].items():
        assert result['fields'][key] == value and result['field_sources'][key] == 'manual'
    assert not any(key in result['transaction_evidence'] for key in entry['fields'])


@pytest.mark.parametrize(('local_merchant', 'ai_merchant'), [
    ('淘宝闪购', '合成便利店（南门店）'), ('合成便利店（南门店）', '淘宝闪购')])
def test_platform_and_actual_shop_are_not_same_role_merchant_conflict(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_merchant: str, ai_merchant: str) -> None:
    rows = payment_rows()
    queue, _, entry = setup(tmp_path, monkeypatch, rows, response(merchant=ai_merchant))
    local = queue.prepare_batch_entry(entry['id'])
    local['fields']['merchant'] = local_merchant
    local['local_evidence']['merchant'] = local_merchant
    queue._apply_ai_analysis(local, entry, set(), images=None, complete=True, native_source='local_ocr')
    assert local['fields']['merchant'] == local_merchant
    assert not local.get('field_conflicts')
    assert not any('商家名称不同' in issue for issue in local['issues'])


def test_actual_different_branch_stays_visible_and_keeps_local_value(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proposed = response(merchant='合成便利店（北门店）')
    proposed['fields']['shop_name'] = '合成便利店（北门店）'
    proposed['evidence']['shop_name'] = '商户名称：合成便利店（北门店）'
    queue, _, entry = setup(tmp_path, monkeypatch, payment_rows(), proposed)
    result = queue.prepare_batch_entry(entry['id'], force_ai=True)
    assert result['fields']['shop_name'] == '合成便利店(南门店)'
    conflicts = result['field_conflicts']
    conflict = next(item for item in conflicts if item['field'] == 'shop_name')
    assert conflict['label'] == '实际店铺'
    assert conflict['local_value'] == '合成便利店(南门店)'
    assert conflict['ai_value'] == '合成便利店（北门店）'
    assert 'AI与本地识别的实际店铺不同，请对照原件确认。' in result['issues']


@pytest.mark.parametrize('state', ['已退款', '退款成功', '付款失败', '待付款'])
def test_unsuccessful_order_or_payment_cannot_be_revived_by_ai(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    rows = [*payment_rows(), state]
    queue, ai, entry = setup(tmp_path, monkeypatch, rows, response())
    result = queue.prepare_batch_entry(entry['id'])
    assert ai.calls and result['role'] == 'other'
    assert result['fields']['amount'] == '' and '付款状态待确认' in result['issues']
    assert not queue.store.list_items()


@pytest.mark.parametrize('bad_order', [
    {'value': '12345678901234567890', 'quote': '发票号码：12345678901234567890'},
    {'value': '12345678901234567890', 'quote': '不存在的订单号：12345678901234567890'},
    {'value': ['not-a-string'], 'quote': '订单号：12345678901234567890'},
    {'value': '12345678901234567890', 'quote': None},
])
def test_bad_optional_order_never_discards_successful_payment_core(bad_order: dict[str, Any]) -> None:
    text = '\n'.join(payment_rows()) + '\n发票号码：12345678901234567890\n订单号：12345678901234567890'
    payload = response(text=text)
    payload['fields']['order_number'] = bad_order['value']
    payload['evidence']['order_number'] = bad_order['quote']
    checked = GlmInvoiceAI._validate_extraction(payload, text, False)
    assert 'order_number' not in checked['fields']
    assert checked['fields']['amount'] == '17.00' and checked['fields']['payment_status'] == 'succeeded'
    assert checked['fields']['merchant'] == '合成便利店（南门店）'
    assert any('订单号' in item for item in checked['field_warnings'])


def test_ai_actor_fields_need_visible_role_and_bank_is_not_channel() -> None:
    text = '\n'.join(payment_rows())
    payload = response(text=text)
    for key, value, quote in [
        ('shopping_platform', '淘宝闪购', '淘宝闪购'),
        ('shop_name', '合成便利店（南门店）', '商品说明：合成便利店（南门店）'),
        ('payment_channel', '支付宝', '支付宝')]:
        payload['fields'][key], payload['evidence'][key] = value, quote
    checked = GlmInvoiceAI._validate_extraction(payload, text, False)
    assert all(key in checked['fields'] for key in ('shopping_platform', 'shop_name', 'payment_channel'))
    payload['fields']['payment_channel'] = '支付宝'
    payload['evidence']['payment_channel'] = '付款方式：农业银行储蓄卡(0000)'
    text += '\n付款方式：农业银行储蓄卡(0000)'
    payload['text'] = text
    checked = GlmInvoiceAI._validate_extraction(payload, text, False)
    assert 'payment_channel' not in checked['fields'] and checked['fields']['amount'] == '17.00'


def test_invalid_core_amount_evidence_still_fails() -> None:
    text = '\n'.join(payment_rows())
    payload = response(amount='999.00', text=text)
    with pytest.raises(GlmAIError) as error:
        GlmInvoiceAI._validate_extraction(payload, text, False)
    assert error.value.failure_field == 'amount'


def test_legacy_bare_order_number_does_not_automatically_link_an_expense(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout import mail_search
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: '')
    store = ExpenseStore(tmp_path)
    item = store.create({'title': '原有费用', 'amount': '17', 'order_number': '20261016000012345678'})
    queue = IntakeQueue(store)
    entry = queue.receive('legacy.png', image())
    monkeypatch.setattr(intake, 'read_receipt', lambda *_: [OcrLine('普通材料', 0, 0, 200, 20)])
    monkeypatch.setattr(intake, 'parse_receipt', lambda *_: {'role': 'payment', 'fields': {
        'title': '新材料', 'amount': '17', 'order_number': item['order_number'], 'category': '其他'}})
    result = queue.analyze(entry['id'])
    assert result['status'] == 'review' and not result.get('item_id')
    assert len(store.list_items()) == 1 and not record_identifier_keys(result)


def test_deleted_expense_bare_number_does_not_occupy_different_identifier_namespace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _, entry = setup(tmp_path, monkeypatch, payment_rows())
    store = queue.store
    item = store.create({'title': '历史裸编号', 'amount': '17', 'order_number': '20261016000012345678'})
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        saved['deleted'] = True
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), item['id']))
    result = queue.prepare_batch_entry(entry['id'])
    with store.connect() as db:
        assert queue._deleted_owners(db, result['fields'], result['digest'], record=result) == []


def test_typed_payment_identifier_prevents_new_expense_but_permits_supplement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _, entry = setup(tmp_path, monkeypatch, payment_rows())
    result = queue.prepare_batch_entry(entry['id'])
    item = queue.store.create({'title': '已有同付款', 'amount': '17',
        'transaction_identifiers': result['transaction_identifiers']})
    with pytest.raises(ValueError, match='不能重复记账'):
        queue.resolve(result['id'], result['revision'], result['fields'], 'payment')
    unrelated = queue.store.create({'title': '另外一笔费用', 'amount': '17'})
    with pytest.raises(ValueError, match='不能重复记账'):
        queue.resolve(result['id'], result['revision'], result['fields'], 'payment',
                      unrelated['id'], unrelated['version'])
    latest = queue.store.get(item['id'])
    linked = queue.resolve(result['id'], result['revision'], result['fields'], 'payment',
                           item['id'], latest['version'])
    assert linked['item_id'] == item['id'] and len(queue.store.list_items()) == 2
    assert record_identifier_keys(queue.store.get(item['id'])) == record_identifier_keys(result)


@pytest.mark.parametrize('local_id', [False, True])
def test_partial_ocr_visual_fallback_preserves_scoped_identifiers_and_their_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_id: bool) -> None:
    rows = ['支付宝', '交易成功', '商户名称：合成便利店（南门店）']
    if local_id:
        rows.append('支付宝交易号：20261016000099998888')
    payload = response()
    for field, value, quote in [
        ('shopping_platform', '淘宝闪购', '淘宝闪购'),
        ('shop_name', '合成便利店（南门店）', '商品说明：合成便利店（南门店）'),
        ('payment_channel', '支付宝', '支付宝'),
        ('order_number', '20261016000012345678', '订单号：20261016000012345678')]:
        payload['fields'][field], payload['evidence'][field] = value, quote
    # Exercise the same validated extraction contract as a real vision reply,
    # with an injected client and a synthetic image rather than a network call.
    checked = GlmInvoiceAI._validate_extraction(payload, '\n'.join(rows), True)
    queue, ai, entry = setup(tmp_path, monkeypatch, rows, checked)
    result = queue.prepare_batch_entry(entry['id'])
    assert len(ai.calls) == 1 and ai.calls[0][1]
    assert result['text'] == '\n'.join(rows) and result['ai_text'] == payload['text']
    assert result['fields']['amount'] == '17.00' and result['fields']['order_number'] == '20261016000012345678'
    assert result['field_sources']['merchant'] == 'local_ocr'
    assert result['field_sources']['shop_name'] == 'local_ocr'
    assert result['field_sources']['payment_channel'] == 'local_ocr'
    assert result['field_sources']['shopping_platform'] == 'glm-5.3-flash'
    ids = result['transaction_identifiers']
    assert {item['kind'] for item in ids} == ({'payment_order', 'merchant_order', 'payment_transaction'}
                                            if local_id else {'payment_order', 'merchant_order'})
    assert len(record_identifier_keys(result)) == (3 if local_id else 2)
    evidence = result['transaction_identifier_evidence']
    assert all(item['source_sha256'] == entry['digest'] for item in evidence)
    visual = [item for item in evidence if item['source'] == 'glm-5.3-flash']
    assert len(visual) == 2 and all(item['quote'] in result['ai_text'] for item in visual)
    native = [item for item in evidence if item['source'] == 'local_ocr']
    assert len(native) == int(local_id) and all(item['quote'] in result['text'] for item in native)
    assert not queue.store.list_items()


def test_visual_fallback_never_guesses_scoped_identity_from_unlabelled_order_field(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = ['支付宝', '交易成功', '商户名称：合成便利店（南门店）']
    text = '\n'.join([*rows, '付款金额：17.00', '编号：20261016000012345678'])
    payload = response(text=text)
    payload['fields']['order_number'] = '20261016000012345678'
    payload['evidence']['order_number'] = '编号：20261016000012345678'
    checked = GlmInvoiceAI._validate_extraction(payload, '\n'.join(rows), True)
    queue, ai, entry = setup(tmp_path, monkeypatch, rows, checked)
    result = queue.prepare_batch_entry(entry['id'])
    assert len(ai.calls) == 1 and ai.calls[0][1]
    assert result['fields']['amount'] == '17.00'
    assert not result['transaction_identifiers'] and not record_identifier_keys(result)
    assert not result['transaction_identifier_evidence']


@pytest.mark.parametrize(('manual_fields', 'expected'), [
    (['merchant'], {'merchant'}), ([], set()),
    (None, {'merchant', 'shop_name', 'shopping_platform', 'payment_channel'})])
def test_explicit_manual_field_list_takes_priority_over_legacy_result_marker(
        manual_fields: list[str] | None, expected: set[str]) -> None:
    data: dict[str, Any] = {'fields': {'merchant': '人工店铺', 'shop_name': '旧自动店铺',
        'shopping_platform': '淘宝闪购', 'payment_channel': '支付宝'},
        'result': '人工补充材料信息，尚未关联费用'}
    if manual_fields is not None:
        data['manual_fields'] = manual_fields
    assert IntakeQueue._manual_keys(data) == expected


def test_local_retry_uses_original_identifier_role_after_manual_material_role_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, ai, entry = setup(tmp_path, monkeypatch, payment_rows())
    local = queue.prepare_batch_entry(entry['id'])
    local.update(role='receipt', manual_role=True, manual_fields=[],
                 transaction_source_role='payment', result='人工补充材料信息，尚未关联费用')
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                   (json.dumps(local, ensure_ascii=False), local['id']))
    retried = queue.prepare_batch_entry(local['id'])
    assert ai.calls == [] and retried['role'] == 'receipt'
    assert {item['kind'] for item in retried['transaction_identifiers']} == {'payment_order', 'merchant_order'}
    assert record_identifier_keys(retried) == record_identifier_keys(local)
