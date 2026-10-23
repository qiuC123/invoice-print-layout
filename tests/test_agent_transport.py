"""Native transport proofs keep identity, amount and source completeness separate."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.agent_combinations import extended_candidates, missing_questions, source_category
from invoice_print_layout.agent_transport import (MailOrigin, pair_status, project_transport, ride_service,
    transport_profile, transport_relation_candidates)


def entry(tmp_path: Path, key: str, role: str, text: str, amount: str,
          merchant: str = '') -> dict[str, Any]:
    path = tmp_path / (key + '.pdf')
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for i, row in enumerate(text.splitlines()):
            if row.startswith('价税合计（小写）￥'):
                page.insert_text((25, 25 + i * 14), '价税合计', fontname='china-s', fontsize=9)
                page.insert_text((300, 25 + i * 14), '（小写）', fontname='china-s', fontsize=10)
                page.insert_text((336, 25 + i * 14), '¥' + row.split('￥')[1], fontsize=10)
            else:
                page.insert_text((25, 25 + i * 14), row, fontname='china-s', fontsize=9)
        pdf.save(path)
    return {'id': key, 'path': str(path), 'digest': hashlib.sha256(path.read_bytes()).hexdigest(),
            'role': role, 'text': text, 'revision': 1, 'fields': {'amount': amount,
            'category': '其他', 'merchant': merchant, 'title': key}}


def bill(seller: str, amount: str, *, courier: bool = False) -> str:
    return ('电子发票（普通发票）\n发票号码：12345678901234567890\n'
            '开票日期：2030年01月02日\n销售方名称：' + seller + '\n'
            '*生产生活服务*收派服务费\n' if courier else
            '电子发票（普通发票）\n发票号码：12345678901234567890\n'
            '开票日期：2030年01月02日\n销售方名称：' + seller + '\n'
            '旅客运输服务\n*交通运输服务*客运服务费\n') + '价税合计（小写）￥' + amount


def trip(provider: str = '享道出行', amount: str = '30.00', count: int = 1) -> str:
    return ('腾讯出行服务—打车——行程单\n上车时间\n起点\n终点\n金额\n' + provider +
            '\n2030-01-01 12:00\n起点甲\n终点乙\n' + amount +
            f'\n共{count}个行程，合计{amount}元\n页码：1 / 1')


def city(amount: str = '30.00', identity: str = '') -> str:
    return (identity + '\n运单起止日期：2030-01-01至2030-01-01\n共1笔运单，合计' + amount +
            '元\n订单号\n发件信息\n收件信息\n金额\n8547695660999\n帮我送\n' + amount + '元\n在线支付\n页码：1 / 1')


def projected(row: dict[str, Any]) -> dict[str, Any]:
    p = project_transport(row)
    assert p
    return {**row, 'role': p['role'], 'fields': {**row['fields'], **p['fields']}}


def context(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    states = {r['id']: {'name_conflict': False} for r in rows}
    facts = {r['id']: {'complete': True, 'issues': [], 'goods_issues': [],
             'total_cents': round(float(r['fields']['amount']) * 100)} for r in rows}
    return states, facts


def test_passenger_fee_without_taxi_word_is_source_classified(tmp_path: Path) -> None:
    row = entry(tmp_path, 'invoice', 'invoice', bill('享道出行测试分公司', '30.00'), '30.00')
    assert source_category(row) == '打车'
    assert ride_service(row['text'])
    assert not ride_service(row['text'] + '\n货物运输')
    assert not ride_service('服务商：享道出行\n*租赁服务*车辆租赁费')


@pytest.mark.parametrize('provider', ['享道出行', '曹操出行', '滴滴出行'])
def test_known_service_provider_native_pair_is_automatic(tmp_path: Path, provider: str) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill(provider, '30.00'), '30.00'))
    support = projected(entry(tmp_path, 'trip', 'trip', trip(provider), '30.00'))
    states, facts = context([invoice, support])
    assert pair_status(invoice, support) == 'supported'
    found = extended_candidates([invoice, support], states)
    assert len(found) == 1 and found[0]['support_ids'] == ['trip'] and found[0]['amount_cents'] == 3000
    assert not transport_relation_candidates([invoice, support], states, facts)


def test_t3_printed_brand_does_not_create_unproved_legal_entity_alias(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('合成客运服务有限公司', '30.00'), '30.00', '合成客运服务有限公司'))
    support = projected(entry(tmp_path, 'trip', 'trip', trip('T3出行'), '30.00'))
    states, facts = context([invoice, support])
    assert pair_status(invoice, support) == 'needs_fact'
    assert not extended_candidates([invoice, support], states)
    rows = transport_relation_candidates([invoice, support], states, facts)
    assert len(rows) == 1 and rows[0]['strength'] == 'needs_fact' and rows[0]['category'] == '打车'
    question = missing_questions([invoice], context=[invoice, support])[0]
    assert '已收到' in question['reason'] and 'T3' in question['reason']
    assert '合成客运服务有限公司' in question['reason'] and '无需重复上传' in question['reason']
    assert not question['requires_source']


def test_anonymous_city_waybill_is_present_but_identity_needs_fact(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('顺丰同城测试有限公司', '30.00', courier=True), '30.00', '顺丰同城测试有限公司'))
    original = entry(tmp_path, 'detail', 'other', city(), '30.00')
    before = copy.deepcopy(original)
    detail = projected(original)
    assert original == before and detail['role'] == 'detail' and source_category(detail) == '顺丰'
    states, facts = context([invoice, detail])
    assert pair_status(invoice, detail) == 'needs_fact'
    assert not extended_candidates([invoice, detail], states)
    rows = transport_relation_candidates([invoice, detail], states, facts)
    assert len(rows) == 1 and rows[0]['category'] == '顺丰'
    q = missing_questions([invoice], context=[invoice, detail])[0]
    assert '已收到' in q['reason'] and '未标承运方' in q['reason'] and not q['requires_source']
    # Even an old role=other should not produce a false "not uploaded" message.
    q = missing_questions([invoice], context=[invoice, original])[0]
    assert '已收到' in q['reason'] and '无需重复上传' in q['reason']


def test_city_identity_in_original_can_support_unique_automatic_pair(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('顺丰同城测试有限公司', '30.00', courier=True), '30.00'))
    detail = projected(entry(tmp_path, 'detail', 'other', city(identity='顺丰同城运单明细'), '30.00'))
    states, _ = context([invoice, detail])
    assert pair_status(invoice, detail) == 'supported'
    found = extended_candidates([invoice, detail], states)
    assert len(found) == 1 and found[0]['support_ids'] == ['detail']


@pytest.mark.parametrize('kind', ['ride', 'city'])
def test_amount_conflict_never_becomes_a_confirmable_relation(tmp_path: Path, kind: str) -> None:
    courier = kind == 'city'
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('顺丰同城测试有限公司' if courier else '合成客运有限公司', '30.00', courier=courier), '30.00'))
    support = projected(entry(tmp_path, 'support', 'other', city('31.00') if courier else trip('T3出行', '31.00'), '31.00'))
    states, facts = context([invoice, support])
    assert not pair_status(invoice, support)
    assert not extended_candidates([invoice, support], states)
    assert not transport_relation_candidates([invoice, support], states, facts)
    question = missing_questions([invoice], context=[invoice, support])[0]
    assert '30.00' in question['reason'] and '31.00' in question['reason'] and question['requires_source']


def test_same_typed_domain_id_conflict_blocks_even_known_platform(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('享道出行', '30.00'), '30.00'))
    support = projected(entry(tmp_path, 'support', 'trip', trip(), '30.00'))
    for row, value in [(invoice, '123456789012'), (support, '987654321012')]:
        row['transaction_identifiers'] = [{'kind': 'merchant_order', 'namespace': '测试出行平台', 'value': value, 'label': '商户单号', 'quote': value}]
    states, facts = context([invoice, support])
    assert not pair_status(invoice, support)
    assert not extended_candidates([invoice, support], states)
    assert not transport_relation_candidates([invoice, support], states, facts)


@pytest.mark.parametrize('defect', ['different_provider', 'partial_pages', 'incomplete_total', 'replaced_bytes', 'conflicting_totals'])
def test_invalid_native_trip_cannot_be_auto_matched(tmp_path: Path, defect: str) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('享道出行', '30.00'), '30.00'))
    text = trip('曹操出行' if defect == 'different_provider' else '享道出行')
    if defect == 'partial_pages':
        text = text.replace('1 / 1', '1 / 2')
    if defect == 'incomplete_total':
        text = text.replace('共1个行程，合计30.00元', '')
    if defect == 'conflicting_totals':
        text += '\n共1个行程，合计31.00元'
    support = entry(tmp_path, 'trip', 'trip', text, '30.00')
    if defect == 'replaced_bytes':
        support['digest'] = '0' * 64
    states, facts = context([invoice, support])
    assert not extended_candidates([invoice, support], states)
    assert not transport_relation_candidates([invoice, support], states, facts)


def test_competing_native_trips_stay_ambiguous_and_external_unknown_blocks(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('合成客运有限公司', '30.00'), '30.00'))
    a = projected(entry(tmp_path, 'trip-a', 'trip', trip('T3出行'), '30.00'))
    b = projected(entry(tmp_path, 'trip-b', 'trip', trip('T3出行'), '30.00'))
    states, facts = context([invoice, a, b])
    rows = transport_relation_candidates([invoice, a, b], states, facts)
    assert len(rows) == 2 and all(row['competing'] for row in rows)
    assert not transport_relation_candidates([invoice, a, b], states, facts, scope={'invoice', 'trip-a'})
    b['fields']['amount'] = ''
    assert not transport_relation_candidates([invoice, a, b], states, facts, scope={'invoice', 'trip-a'})


def test_mixed_provider_table_cannot_be_cured_by_relation_answer(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('享道出行', '30.00'), '30.00'))
    support = projected(entry(tmp_path, 'mixed-trip', 'trip', trip('享道出行') + '\n曹操出行', '30.00'))
    states, facts = context([invoice, support])
    assert transport_profile(support)['identity_ambiguous']
    assert not pair_status(invoice, support)
    assert not extended_candidates([invoice, support], states)
    assert not transport_relation_candidates([invoice, support], states, facts)


def test_wrong_explicit_invoice_reference_and_manual_amount_cannot_be_overridden(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('顺丰同城测试有限公司', '30.00', courier=True), '30.00'))
    detail = projected(entry(tmp_path, 'detail', 'detail', city() + '\n发票号码：99999999999999999999', '30.00'))
    assert not pair_status(invoice, detail)
    detail['fields']['invoice_number'] = '12345678901234567890'
    assert not pair_status(invoice, detail)
    support = projected(entry(tmp_path, 'trip', 'trip', trip('T3出行'), '30.00'))
    support['fields']['amount'] = '29.00'
    assert not pair_status(invoice, support)


def test_projection_rejects_changed_pdf_and_image_without_ocr(tmp_path: Path) -> None:
    row = entry(tmp_path, 'source', 'other', city(), '')
    assert project_transport(row)['fields']['amount'] == '30.00'
    assert row['fields']['amount'] == ''
    assert not project_transport({**row, 'digest': '1' * 64})
    assert not project_transport({**row, 'path': str(tmp_path / 'source.jpg')})
    assert transport_profile(row)['identity'] == ''


def test_carrier_seller_name_does_not_turn_packaging_goods_into_delivery_service(tmp_path: Path) -> None:
    row = entry(tmp_path, 'packaging', 'invoice',
                bill('顺丰同城测试有限公司', '30.00', courier=True).replace('收派服务费', '包装纸箱'), '30.00')
    assert not project_transport(row)
    assert source_category(row) != '顺丰'


def mail_origins(rows: list[dict[str, Any]]) -> dict[str, MailOrigin]:
    # This stands for the verified server adapter, not entry/HTTP/OCR metadata.
    return {r['id']: MailOrigin('test-account', 'INBOX', '20260001', 42, '<unit@test.invalid>', r['digest']) for r in rows}


@pytest.mark.parametrize('kind', ['ride', 'city'])
def test_server_verified_mail_origin_supports_only_a_complete_unique_pair(tmp_path: Path, kind: str) -> None:
    is_city = kind == 'city'
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('顺丰同城测试有限公司' if is_city else '合成客运有限公司', '30.00', courier=is_city), '30.00'))
    support = projected(entry(tmp_path, 'support', 'other', city() if is_city else trip('T3出行'), '30.00'))
    states, facts = context([invoice, support])
    origins = mail_origins([invoice, support])
    assert pair_status(invoice, support) == 'needs_fact'
    assert pair_status(invoice, support, trusted_mail_origins=origins) == 'supported'
    found = extended_candidates([invoice, support], states, trusted_mail_origins=origins)
    assert len(found) == 1
    assert found[0]['source_context']['kind'] == 'trusted_same_mail'
    assert {e['digest'] for e in found[0]['source_context']['attachments']} == {invoice['digest'], support['digest']}
    assert not transport_relation_candidates([invoice, support], states, facts, trusted_mail_origins=origins)


@pytest.mark.parametrize('changed,value', [('account_id', 'other-account'), ('mailbox', 'Archive'),
    ('uidvalidity', '20260002'), ('uid', 43), ('message_id', '<different@test.invalid>'),
    ('attachment_digest', '0' * 64), ('uidvalidity', ''), ('uid', True)])
def test_mail_scope_epoch_message_and_attachment_binding_are_required(tmp_path: Path, changed: str, value: Any) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('合成客运有限公司', '30.00'), '30.00'))
    support = projected(entry(tmp_path, 'support', 'trip', trip('T3出行'), '30.00'))
    states, _ = context([invoice, support])
    origins = mail_origins([invoice, support])
    origins['support'] = replace(origins['support'], **{changed: value})
    assert pair_status(invoice, support, trusted_mail_origins=origins) == 'needs_fact'
    assert not extended_candidates([invoice, support], states, trusted_mail_origins=origins)


def test_entry_mail_dict_or_upload_batch_is_not_authoritative(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('合成客运有限公司', '30.00'), '30.00'))
    support = projected(entry(tmp_path, 'support', 'trip', trip('T3出行'), '30.00'))
    for row in [invoice, support]:
        row.update(source='mail_agent', batch='same', mail_origin={'uid': 42, 'verified': True},
                   mail_context={'message_uid': '42', 'subject': '同一封发票'})
    states, _ = context([invoice, support])
    assert not extended_candidates([invoice, support], states)
    assert not extended_candidates([invoice, support], states,
                                   trusted_mail_origins={row['id']: row['mail_origin'] for row in [invoice, support]})


@pytest.mark.parametrize('defect', ['amount', 'provider', 'typed_id', 'partial_page'])
def test_same_trusted_message_cannot_cure_hard_source_conflicts(tmp_path: Path, defect: str) -> None:
    invoice = projected(entry(tmp_path, 'invoice', 'invoice', bill('享道出行' if defect == 'provider' else '合成客运有限公司', '30.00'), '30.00'))
    text = trip('曹操出行' if defect == 'provider' else 'T3出行', '31.00' if defect == 'amount' else '30.00')
    if defect == 'partial_page':
        text = text.replace('1 / 1', '1 / 2')
    support = entry(tmp_path, 'support', 'trip', text, '31.00' if defect == 'amount' else '30.00')
    if defect == 'typed_id':
        for row, value in [(invoice, '123456789012'), (support, '987654321012')]:
            row['transaction_identifiers'] = [{'kind': 'merchant_order', 'namespace': '测试平台', 'value': value}]
    states, facts = context([invoice, support])
    origins = mail_origins([invoice, support])
    assert not extended_candidates([invoice, support], states, trusted_mail_origins=origins)
    assert not transport_relation_candidates([invoice, support], states, facts, trusted_mail_origins=origins)


def test_multi_invoice_mail_stays_pairwise_and_equal_amount_competition_stops(tmp_path: Path) -> None:
    rows = [projected(entry(tmp_path, 'invoice-' + amount, 'invoice',
            bill('合成客运有限公司', amount).replace('12345678901234567890', f'{i + 1:020d}'), amount))
            for i, amount in enumerate(['30.00', '40.00'])]
    rows += [projected(entry(tmp_path, 'trip-' + amount, 'trip', trip('T3出行', amount), amount))
             for amount in ['30.00', '40.00']]
    states, _ = context(rows)
    found = extended_candidates(rows, states, trusted_mail_origins=mail_origins(rows))
    assert len(found) == 2 and {r['amount_cents'] for r in found} == {3000, 4000}
    assert all(len(r['voucher_keys']) == len(r['support_ids']) == 1 for r in found)
    rows = [rows[0], rows[2], projected(entry(tmp_path, 'competing-trip', 'trip', trip('T3出行'), '30.00'))]
    states, _ = context(rows)
    assert not extended_candidates(rows, states, trusted_mail_origins=mail_origins(rows))
