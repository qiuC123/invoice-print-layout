"""Courier invoice/waybill requirements never need a payment screenshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.courier import inspect_courier_pdf
from invoice_print_layout.courier_proofs import invoice_waybill_required, native_waybill_pdf
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore, evaluate
from tests.test_courier import make_courier_pair


@pytest.mark.parametrize('basis', ['standard', 'invoice_payment'])
@pytest.mark.parametrize('roles,missing', [
    ({'invoice', 'detail'}, []), ({'invoice', 'detail', 'payment'}, []),
    ({'invoice'}, ['运单明细']), ({'detail'}, ['发票']),
    ({'receipt', 'detail'}, ['发票']), ({'invoice', 'payment'}, ['运单明细']),
    ({'payment'}, ['发票', '运单明细']),
])
def test_courier_requires_formal_invoice_and_waybill_not_payment(
        basis: str, roles: set[str], missing: list[str]) -> None:
    result = evaluate('顺丰', roles, material_basis=basis)
    assert result['missing'] == missing and result['complete'] is (not missing)
    assert not result['alternative']
    assert '不要求付款截图' in result['basis']


def test_explicit_payment_only_legacy_and_unrelated_expenses_keep_their_rules() -> None:
    assert evaluate('顺丰', {'payment'}, material_basis='payment_only')['complete']
    assert evaluate('顺丰', {'invoice', 'detail'}, material_basis='payment_only')['missing'] == ['支付凭证']
    assert evaluate('顺丰', {'package'}, legacy=True)['complete']
    assert evaluate('顺丰', {'invoice', 'payment'}, legacy=True, material_basis='invoice_payment')['complete']
    assert evaluate('餐饮', {'invoice', 'detail'}, material_basis='invoice_payment')['missing'] == ['付款截图']
    assert evaluate('顺丰', {'invoice', 'detail'}, amount_pending=True)['missing'] == ['金额待确认']


@pytest.mark.parametrize('fields,expected', [
    ({'category': '顺丰'}, True),
    ({'category': '顺丰', 'material_basis': 'invoice_payment'}, True),
    ({'category': '顺丰', 'legacy': True}, False),
    ({'category': '顺丰', 'material_basis': 'payment_only'}, False),
    ({'category': '其他', 'project_type': 'other', 'project_name': '顺丰'}, True),
    ({'category': '其他', 'project_type': 'other', 'project_name': ' 顺丰 同城 '}, True),
    ({'category': '其他', 'project_type': '其他', 'project_name': '快递费'}, True),
    ({'category': '其他', 'report_section': 'sf'}, True),
    ({'category': '其他', 'courier_waybill_required': True}, True),
    ({'category': '其他', 'courier_waybill_required': 'true'}, False),
    ({'category': '其他', 'title': '顺丰'}, False),
    ({'category': '其他', 'merchant': '顺丰速运'}, False),
    ({'category': '其他', 'project_name': '顺丰'}, False),
    ({'category': '材料采购', 'project_type': 'materials', 'project_name': '顺丰'}, False),
    ({'category': '其他', 'project_type': 'other', 'project_name': '顺丰附近五金店'}, False),
    ({'category': '其他', 'project_type': 'other', 'project_name': '打印'}, False),
    ({'category': '顺丰', 'courier_waybill_required': True, 'legacy': True}, False),
])
def test_rule_uses_explicit_classification_without_guessing_titles(fields: dict[str, Any], expected: bool) -> None:
    assert invoice_waybill_required(fields) is expected


def _rows(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        tables = [row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')]
                for name in tables if not name.startswith('sqlite_')}


def _originals(store: ExpenseStore) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in store.files.iterdir()}


def _expense(store: ExpenseStore, *, category: str = '顺丰', basis: str = 'invoice_payment') -> dict[str, Any]:
    return store.create({'title': 'Synthetic courier', 'category': category, 'amount': '43.00',
                         'material_basis': basis})


def _classified_other(store: ExpenseStore, item: dict[str, Any], name: str = '顺丰') -> dict[str, Any]:
    return store.edit_review_fields(item['id'], item['version'], 'other', name, item['amount'])


@pytest.mark.parametrize('kind', ['顺丰', '同城'])
def test_historical_other_waybill_is_readonly_and_does_not_rewrite_basis_or_version(
        tmp_path: Path, kind: str) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    invoice, detail = make_courier_pair(tmp_path / 'sources', kind)
    item = _expense(store)
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    current = store.add_attachment(item['id'], 'unknown.pdf', detail.read_bytes(), 'other')
    before = _rows(store), _originals(store)
    with store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        raw['attachments'] = [dict(row) for row in db.execute(
            'SELECT id,name,role,path FROM attachments WHERE item_id=?', (item['id'],))]
        raw['events'] = [dict(row) for row in db.execute(
            'SELECT at,message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 30', (item['id'],))]
    expected_version = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert current['courier_waybill_required'] and current['complete'] and current['missing'] == []
    assert {attachment['role'] for attachment in current['attachments']} == {'invoice', 'detail'}
    assert current['category'] == '顺丰' and current['material_basis'] == 'invoice_payment'
    assert current['version'] == expected_version and current['amount_cents'] == 4300
    assert not current['verified'] and not current['ready']
    assert store.get(item['id']) == current
    assert (_rows(store), _originals(store)) == before
    verified = store.transition(item['id'], 'verify', expected_version=expected_version)
    assert verified['ready']
    rows_before_noop = _rows(store)
    saved = store.update(item['id'], dict(verified))
    assert saved['ready'] and saved['verified']
    assert _rows(store)['attachments'] == rows_before_noop['attachments']


def test_explicit_other_courier_ignores_misattached_payment_and_preserves_all_saved_fields(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    invoice, detail = make_courier_pair(tmp_path / 'sources', '顺丰')
    item = _classified_other(store, _expense(store, category='其他'))
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    store.add_attachment(item['id'], 'detail.pdf', detail.read_bytes(), 'detail')
    with pymupdf.open() as document:
        document.new_page().insert_text((30, 50), 'UNRELATED PAYMENT RECORD 43.00')
        payload = document.tobytes()
    item = store.add_attachment(item['id'], 'wrong-payment.pdf', payload, 'payment')
    before = _rows(store), _originals(store)
    current = store.get(item['id'])
    assert current['category'] == '其他' and current['project_type'] == 'other' and current['project_name'] == '顺丰'
    assert current['material_basis'] == 'invoice_payment' and current['courier_waybill_required']
    assert current['complete'] and current['missing'] == [] and current['amount_cents'] == 4300
    assert {attachment['role'] for attachment in current['attachments']} == {'invoice', 'detail', 'payment'}
    assert (_rows(store), _originals(store)) == before
    with store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
    assert 'courier_waybill_required' not in data


def test_named_other_courier_with_invoice_and_payment_still_requires_waybill(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    invoice, _ = make_courier_pair(tmp_path / 'sources', '顺丰')
    item = _classified_other(store, _expense(store, category='其他'), '快递费')
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    item = store.add_attachment(item['id'], 'payment.pdf', invoice.read_bytes(), 'payment')
    before = _rows(store), _originals(store)
    assert item['courier_waybill_required'] and item['missing'] == ['运单明细'] and not item['complete']
    assert store.get(item['id'])['missing'] == ['运单明细']
    with pytest.raises(ValueError, match='补齐材料'):
        store.transition(item['id'], 'verify')
    assert (_rows(store), _originals(store)) == before


def test_unclassified_other_and_filename_are_not_promoted_to_courier_rules(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    invoice, detail = make_courier_pair(tmp_path / 'sources', '顺丰')
    item = _expense(store, category='其他')
    store.add_attachment(item['id'], '顺丰发票43.00.pdf', invoice.read_bytes(), 'invoice')
    item = store.add_attachment(item['id'], '顺丰运单明细43.00.pdf', detail.read_bytes(), 'other')
    before = _rows(store), _originals(store)
    assert not item['courier_waybill_required'] and item['missing'] == ['付款截图']
    assert {attachment['role'] for attachment in item['attachments']} == {'invoice', 'other'}
    assert store.get(item['id']) == item and (_rows(store), _originals(store)) == before


@pytest.mark.parametrize('initial_role', ['detail', 'other'])
def test_manual_other_role_wins_even_after_display_history_window(tmp_path: Path, initial_role: str) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    invoice, detail = make_courier_pair(tmp_path / 'sources', '顺丰')
    item = _expense(store)
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    item = store.add_attachment(item['id'], 'unknown.pdf', detail.read_bytes(), initial_role)
    key = next(attachment['id'] for attachment in item['attachments'] if attachment['role'] == 'detail')
    store.set_role(key, 'other')
    with store.connect() as db:
        for index in range(31):
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item['id'], '2026-10-15', f'Synthetic unrelated event {index}'))
    before = _rows(store), _originals(store)
    current = store.get(item['id'])
    assert current['missing'] == ['运单明细'] and not current['complete']
    assert next(attachment for attachment in current['attachments'] if attachment['id'] == key)['role'] == 'other'
    assert not any(event['message'].startswith('材料用途改为：') for event in current['events'])
    assert (_rows(store), _originals(store)) == before
    store.set_role(key, 'detail')
    assert store.get(item['id'])['complete']


@pytest.mark.parametrize('manual', ['flag', 'result'])
def test_manually_classified_intake_other_is_not_overridden(tmp_path: Path, manual: str) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    intake = IntakeQueue(store)
    invoice, detail = make_courier_pair(tmp_path / 'sources', '同城')
    item = _expense(store)
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    entry = intake.receive('Native unknown detail.pdf', detail.read_bytes())
    entry.update(item_id=item['id'], role='other', status='linked')
    entry.update({'manual_role': True} if manual == 'flag' else {'result': '人工补充材料信息，尚未关联费用'})
    with store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
    item = store.add_attachment(item['id'], 'unknown.pdf', detail.read_bytes(), 'other')
    before = _rows(store), _originals(store)
    assert item['missing'] == ['运单明细'] and not item['complete']
    assert {attachment['role'] for attachment in item['attachments']} == {'invoice', 'other'}
    assert store.get(item['id']) == item and (_rows(store), _originals(store)) == before


@pytest.mark.parametrize('kind', ['顺丰', '同城'])
def test_native_waybill_recognition_requires_identity_hash_and_no_mixed_invoice(tmp_path: Path, kind: str) -> None:
    invoice, detail = make_courier_pair(tmp_path / 'sources', kind, pages=2)
    payload = detail.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    assert native_waybill_pdf(detail, digest)
    assert native_waybill_pdf(detail)
    assert not native_waybill_pdf(detail, '0' * 64)
    assert not native_waybill_pdf(invoice)
    with pymupdf.open(detail) as source, pymupdf.open(invoice) as bill:
        source.insert_pdf(bill)
        mixed = tmp_path / 'mixed.pdf'
        source.save(mixed)
    assert not inspect_courier_pdf(mixed).is_invoice  # Old detector alone accepts the mixed package.
    assert not native_waybill_pdf(mixed)
    detail.write_bytes(invoice.read_bytes())
    assert not native_waybill_pdf(detail, digest)


def test_payment_text_blank_encrypted_and_unreadable_sources_are_not_waybills(tmp_path: Path) -> None:
    for name, text in [('payment', '顺丰支付成功43.00'), ('blank', '')]:
        with pymupdf.open() as document:
            page = document.new_page()
            if text:
                page.insert_text((30, 40), text, fontname='china-s')
            path = tmp_path / (name + '.pdf')
            document.save(path)
        assert not native_waybill_pdf(path)
    _, detail = make_courier_pair(tmp_path / 'encrypted-source', '顺丰')
    encrypted = tmp_path / 'encrypted.pdf'
    with pymupdf.open(detail) as document:
        document.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='user')
    assert not native_waybill_pdf(encrypted)
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(b'%PDF-not-readable')
    assert not native_waybill_pdf(broken)
    assert not native_waybill_pdf(tmp_path / 'missing.pdf')


def test_replaced_historical_waybill_stays_missing_without_writes_or_amount_changes(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path / 'workspace')
    invoice, detail = make_courier_pair(tmp_path / 'sources', '顺丰')
    item = _expense(store)
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    current = store.add_attachment(item['id'], 'unknown.pdf', detail.read_bytes(), 'other')
    path = store.attachment_path(next(attachment['id'] for attachment in current['attachments']
                                     if attachment['role'] == 'detail'))[0]
    _, replacement = make_courier_pair(tmp_path / 'replacement', '顺丰', number='26000000000000000002')
    path.write_bytes(replacement.read_bytes())
    before = _rows(store), _originals(store)
    reloaded = store.get(item['id'])
    assert reloaded['missing'] == ['运单明细'] and not reloaded['complete'] and not reloaded['ready']
    assert reloaded['amount_cents'] == 4300 and reloaded['category'] == '顺丰'
    assert (_rows(store), _originals(store)) == before


def test_file_changing_during_native_inspection_never_inherits_detail_role(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import invoice_print_layout.courier as courier
    invoice, detail = make_courier_pair(tmp_path / 'sources', '顺丰')
    original_inspect = courier.inspect_courier_pdf

    def replace_after_inspection(path: Path) -> Any:
        inspected = original_inspect(path)
        path.write_bytes(invoice.read_bytes())
        return inspected

    monkeypatch.setattr(courier, 'inspect_courier_pdf', replace_after_inspection)
    assert not native_waybill_pdf(detail)
