"""Transport preference ranks real business identity before mail before provider."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from invoice_print_layout.agent_combinations import extended_candidates
from invoice_print_layout.agent_transport import MailOrigin, pair_priority_evidence, pair_status, transport_profile
from tests.test_agent_transport import bill, city, entry, projected, trip


def pair(tmp_path: Path, index: int, *, invoice_id: str = '', trip_id: str = '',
         partial: bool = False) -> list[dict[str, Any]]:
    invoice = bill('享道出行测试分公司', '30.00').replace('12345678901234567890', f'{index:020d}')
    support = trip('享道出行').replace('起点甲', f'起点{index}')
    if invoice_id:
        invoice += '\n商户单号：' + invoice_id
    if trip_id:
        support += '\n商户单号：' + trip_id
    if partial:
        support = support.replace('1 / 1', '1 / 2')
    invoice_row = projected(entry(tmp_path, f'invoice-{index}', 'invoice', invoice, '30.00'))
    support_row = entry(tmp_path, f'trip-{index}', 'trip', support, '30.00')
    return [invoice_row, support_row if partial else projected(support_row)]


def origins(rows: list[dict[str, Any]], *, one_mail: bool = False) -> dict[str, MailOrigin]:
    return {r['id']: MailOrigin('synthetic-account', 'INBOX', '20260001',
            1 if one_mail else int(r['id'].split('-')[-1]),
            '<same@example.invalid>' if one_mail else f"<{r['id'].split('-')[-1]}@example.invalid>",
            r['digest'], authority='mail_imports.jsonl') for r in rows}


def find(rows: list[dict[str, Any]], mail: dict[str, MailOrigin] | None = None,
         scope: set[str] | None = None) -> list[dict[str, Any]]:
    states = {row['id']: {'name_conflict': False} for row in rows}
    return extended_candidates(rows, states, trusted_mail_origins=mail, scope=scope)


def test_two_complete_mails_with_equal_amount_known_provider_form_two_pairs(tmp_path: Path) -> None:
    rows = pair(tmp_path, 1) + pair(tmp_path, 2)
    found = find(rows, origins(rows))
    assert {(r['voucher_keys'][0], r['support_ids'][0]) for r in found} == {
        ('q:invoice-1', 'trip-1'), ('q:invoice-2', 'trip-2')}
    assert all(r['source_context']['priority_rank'] == 2 and
               r['source_context']['authority'] == 'mail_imports.jsonl' for r in found)
    assert all(r['source_context']['source_quotes'] for r in found)


def test_same_mail_two_equal_amount_orders_still_have_competing_pairs(tmp_path: Path) -> None:
    rows = pair(tmp_path, 1) + pair(tmp_path, 2)
    assert not find(rows, origins(rows, one_mail=True))


def test_shared_native_typed_business_id_outweighs_a_different_same_mail_pair(tmp_path: Path) -> None:
    rows = pair(tmp_path, 1, invoice_id='A123456789012') + pair(tmp_path, 2, trip_id='A123456789012')
    found = find(rows, origins(rows))
    assert len(found) == 1 and found[0]['voucher_keys'] == ['q:invoice-1'] and found[0]['support_ids'] == ['trip-2']
    proof = found[0]['source_context']
    assert proof['priority_rank'] == 3 and proof['priority_basis'] == 'shared_business_identity'
    assert proof['shared_business_ids'] == [['merchant_order', '享道', 'A123456789012']]
    assert all(any('A123456789012' in quote for quote in source['quotes']) for source in proof['source_quotes'])


def test_same_mail_never_resolves_a_native_typed_business_id_conflict(tmp_path: Path) -> None:
    rows = pair(tmp_path, 1, invoice_id='A123456789012', trip_id='B123456789012')
    assert not pair_status(*rows, trusted_mail_origins=origins(rows))
    assert not find(rows, origins(rows))


def test_unique_provider_pair_without_mail_keeps_existing_rule_and_ignores_injected_ids(tmp_path: Path) -> None:
    rows = pair(tmp_path, 1)
    for row in rows:
        row['transaction_identifiers'] = [{'kind': 'merchant_order', 'namespace': '享道',
            'value': 'INJECT123456', 'quote': '商户单号：INJECT123456'}]
    found = find(rows)
    assert len(found) == 1 and found[0]['source_context']['priority_rank'] == 1
    assert found[0]['source_context']['shared_business_ids'] == []


def test_unscoped_orphan_provider_trip_is_not_hidden_by_one_trusted_mail(tmp_path: Path) -> None:
    rows = pair(tmp_path, 1) + [pair(tmp_path, 2)[1]]
    mail = origins(rows[:2])
    assert not find(rows, mail)


def test_priority_cannot_hide_scope_outside_unknown_amount_or_incomplete_competitor(tmp_path: Path) -> None:
    for defect in ('scope_outside', 'unknown_amount', 'incomplete'):
        folder = tmp_path / defect
        folder.mkdir()
        rows = pair(folder, 1) + [pair(folder, 2, partial=defect == 'incomplete')[1]]
        if defect == 'unknown_amount':
            rows[-1]['fields']['amount'] = ''
        scope = {'invoice-1', 'trip-1'} if defect == 'scope_outside' else None
        assert not find(rows, origins(rows), scope), defect


def test_exact_native_waybill_invoice_reference_is_highest_rank_and_keeps_mail_audit(tmp_path: Path) -> None:
    invoice = projected(entry(tmp_path, 'invoice-1', 'invoice',
        bill('顺丰同城测试有限公司', '30.00', courier=True), '30.00'))
    support = projected(entry(tmp_path, 'detail-1', 'detail',
        city() + '\n发票号码：12345678901234567890', '30.00'))
    rows = [invoice, support]
    profiles = {r['id']: transport_profile(r) for r in rows}
    evidence = pair_priority_evidence(invoice, support, profiles, trusted_mail_origins=origins(rows))
    assert evidence['priority_rank'] == 3 and evidence['detail_invoice_number'] == '12345678901234567890'
    assert evidence['authority'] == 'mail_imports.jsonl'
    mail = origins(rows)
    mail[support['id']] = replace(mail[support['id']], uid=2)
    assert len(find(rows, mail)) == 1
