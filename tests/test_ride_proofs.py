"""Taxi completeness and historical native-trip compatibility without ledger edits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.ride_proofs import invoice_trip_required, native_trip_pdf
from invoice_print_layout.workbench import ExpenseStore, evaluate
from tests.helpers import make_generic_trip_pdf, make_invoice_pdf, make_trip_pdf


@pytest.mark.parametrize('basis', ['standard', 'invoice_payment'])
@pytest.mark.parametrize('roles,missing', [
    ({'invoice', 'trip'}, []), ({'invoice', 'trip', 'payment'}, []),
    ({'invoice'}, ['行程单']), ({'trip'}, ['发票']),
    ({'receipt', 'trip'}, ['发票']), ({'invoice', 'payment'}, ['行程单']),
    ({'payment'}, ['发票', '行程单']),
])
def test_taxi_requires_formal_invoice_and_trip_but_no_payment(
        basis: str, roles: set[str], missing: list[str]) -> None:
    result = evaluate('打车', roles, material_basis=basis)
    assert result['missing'] == missing and result['complete'] is (not missing)
    assert not result['alternative']


def test_confirmed_payment_only_legacy_and_other_categories_keep_existing_rules() -> None:
    assert evaluate('打车', {'payment'}, material_basis='payment_only')['complete']
    assert evaluate('打车', {'invoice', 'trip'}, material_basis='payment_only')['missing'] == ['支付凭证']
    assert evaluate('打车', {'package'}, legacy=True)['complete']
    assert evaluate('打车', {'invoice', 'payment'}, legacy=True, material_basis='invoice_payment')['complete']
    assert evaluate('餐饮', {'invoice', 'trip'}, material_basis='invoice_payment')['missing'] == ['付款截图']
    assert evaluate('酒店', {'invoice', 'trip'}, material_basis='invoice_payment')['missing'] == ['付款截图']
    assert evaluate('外卖', {'invoice', 'trip'})['missing'] == ['订单记录']
    assert evaluate('打车', {'invoice', 'trip'}, amount_pending=True)['missing'] == ['金额待确认']
    assert invoice_trip_required({'category': '打车'})
    assert invoice_trip_required({'category': '打车', 'material_basis': 'invoice_payment'})
    assert not invoice_trip_required({'category': '打车', 'legacy': True})
    assert not invoice_trip_required({'category': '打车', 'material_basis': 'payment_only'})
    assert not invoice_trip_required({'category': '餐饮', 'material_basis': 'invoice_payment'})


def rows(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)]
                for name in ('expenses', 'attachments', 'events')}


def test_historical_other_trip_is_read_only_role_projection_and_keeps_version(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Historical taxi', 'category': '打车', 'amount': '123.45',
                         'material_basis': 'invoice_payment'})
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf')
    trip = make_trip_pdf(tmp_path / 'source.pdf')
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    current = store.add_attachment(item['id'], 'not-a-trip-filename.pdf', trip.read_bytes(), 'other')
    before = rows(store)
    with store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        raw['attachments'] = [dict(row) for row in db.execute(
            'SELECT id,name,role,path FROM attachments WHERE item_id=?', (item['id'],))]
        raw['events'] = [dict(row) for row in db.execute(
            'SELECT at,message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 30', (item['id'],))]
    expected_version = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert current['version'] == expected_version
    assert current['material_basis'] == 'invoice_payment'
    assert {a['role'] for a in current['attachments']} == {'invoice', 'trip'}
    assert current['complete'] and not current['verified'] and not current['ready']
    assert current['amount_cents'] == 12345 and current['missing'] == []
    assert rows(store) == before
    assert store.get(item['id'])['version'] == expected_version
    assert rows(store) == before
    verified = store.transition(item['id'], 'verify', expected_version=expected_version)
    assert verified['ready'] and verified['verified']
    verified_rows = rows(store)
    unchanged = store.update(item['id'], dict(verified))
    assert unchanged['ready'] and unchanged['verified'] and unchanged['amount_cents'] == 12345
    after_update = rows(store)
    assert after_update['attachments'] == verified_rows['attachments']
    old_data, new_data = (json.loads(snapshot['expenses'][0][2])
                          for snapshot in (verified_rows, after_update))
    old_data.pop('updated_at', None)
    new_data.pop('updated_at', None)
    assert new_data == old_data


def test_trip_filename_and_amount_do_not_suffice_and_non_taxi_is_not_projected(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Invoice only', 'category': '打车', 'amount': '123.45',
                         'material_basis': 'invoice_payment'})
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf')
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    current = store.add_attachment(item['id'], '打车行程单123.45.pdf', invoice.read_bytes(), 'other')
    assert current['missing'] == ['行程单']
    assert {a['role'] for a in current['attachments']} == {'invoice', 'other'}
    with pytest.raises(ValueError, match='补齐材料'):
        store.transition(item['id'], 'verify')
    other = store.create({'title': 'Other category', 'category': '餐饮', 'amount': '123.45',
                          'material_basis': 'invoice_payment'})
    trip = make_trip_pdf(tmp_path / 'source.pdf')
    other = store.add_attachment(other['id'], 'trip.pdf', trip.read_bytes(), 'other')
    assert other['attachments'][0]['role'] == 'other'
    assert other['missing'] == ['发票或收据', '付款截图']


def test_native_trip_requires_unchanged_original_and_rejects_mixed_package(tmp_path: Path) -> None:
    trip = make_trip_pdf(tmp_path / 'source.pdf', pages=2)
    original = trip.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    assert native_trip_pdf(trip, digest)
    assert not native_trip_pdf(trip, '0' * 64)
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf')
    with pymupdf.open(trip) as source, pymupdf.open(invoice) as other:
        source.insert_pdf(other)
        mixed = tmp_path / 'mixed.pdf'
        source.save(mixed)
    assert not native_trip_pdf(mixed)
    trip.write_bytes(invoice.read_bytes())
    assert not native_trip_pdf(trip, digest)


def test_replaced_historical_trip_stays_missing_without_ledger_writes(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Replaced trip', 'category': '打车', 'amount': '123.45',
                         'material_basis': 'invoice_payment'})
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf')
    trip = make_trip_pdf(tmp_path / 'trip.pdf')
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    store.add_attachment(item['id'], 'unknown.pdf', trip.read_bytes(), 'other')
    with store.connect() as db:
        path = Path(db.execute("SELECT path FROM attachments WHERE item_id=? AND role='other'",
                               (item['id'],)).fetchone()['path'])
    # A different but still valid trip must not inherit the original proof role.
    path.write_bytes(make_trip_pdf(tmp_path / 'replacement.pdf', amount='99.00').read_bytes())
    before = rows(store)
    current = store.get(item['id'])
    assert current['missing'] == ['行程单'] and not current['complete']
    assert rows(store) == before and current['amount_cents'] == 12345


def context(tmp_path: Path) -> tuple[MaterialMatching, str]:
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Taxi fixture')['id']
    return MaterialMatching(store, IntakeQueue(store)), project


def received(matcher: MaterialMatching, project: str, path: Path, role: str) -> dict[str, Any]:
    entry = matcher.intake.receive(path.name, path.read_bytes(), project_id=project)
    entry.update(role=role, fields={'title': 'Taxi proof', 'category': '打车', 'amount': '44.60'},
                 status='review', revision=1)
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def choice() -> dict[str, str]:
    return {'project_type': 'transport', 'project_name': '打的(出租车、滴滴等)', 'amount': '44.60'}


def test_new_registration_saves_native_trip_role_and_undo_restores_original_role(tmp_path: Path) -> None:
    matcher, project = context(tmp_path)
    invoice = received(matcher, project, make_invoice_pdf(tmp_path / 'invoice.pdf', amount='44.60'), 'invoice')
    trip = received(matcher, project, make_generic_trip_pdf(tmp_path / 'native.pdf', amount='44.60'), 'other')
    result = matcher.create_expense(['q:' + invoice['id']], [], project,
        support_ids=[trip['id']], revisions={invoice['id']: 1, trip['id']: 1}, fields=choice())
    item = result['item']
    assert item['material_basis'] == 'standard' and item['complete'] and item['missing'] == []
    assert item['amount_cents'] == 4460 and not item['verified'] and not item['ready']
    assert matcher.intake.get(trip['id'])['role'] == 'trip'
    with matcher.store.connect() as db:
        assert {row['role'] for row in db.execute('SELECT role FROM attachments WHERE item_id=?',
                                                (item['id'],))} == {'invoice', 'trip'}
    assert result['match']['can_supplement']
    matcher.undo(result['match']['id'], item['version'])
    restored = matcher.intake.get(trip['id'])
    assert restored['role'] == 'other' and not restored['item_id']
    assert restored['fields'] == trip['fields']


def test_manual_other_queue_role_is_preserved_in_registration_and_read_projection(tmp_path: Path) -> None:
    matcher, project = context(tmp_path)
    invoice = received(matcher, project, make_invoice_pdf(tmp_path / 'invoice.pdf', amount='44.60'), 'invoice')
    trip = received(matcher, project, make_generic_trip_pdf(tmp_path / 'native.pdf', amount='44.60'), 'other')
    trip['manual_role'] = True
    with matcher.store.connect() as db:
        matcher._write_queue(db, trip)
    result = matcher.create_expense(['q:' + invoice['id']], [], project,
        support_ids=[trip['id']], revisions={invoice['id']: 1, trip['id']: 1}, fields=choice())
    item = result['item']
    assert item['missing'] == ['行程单'] and not item['complete']
    assert {a['role'] for a in item['attachments']} == {'invoice', 'other'}
    assert matcher.intake.get(trip['id'])['manual_role']
    assert matcher.intake.get(trip['id'])['role'] == 'other'
    before = rows(matcher.store)
    reloaded = matcher.store.get(item['id'])
    assert reloaded['version'] == item['version'] and reloaded['missing'] == ['行程单']
    assert rows(matcher.store) == before
    with pytest.raises(ValueError, match='补齐材料'):
        matcher.store.transition(item['id'], 'verify')


@pytest.mark.parametrize('initial_role', ['trip', 'other'])
def test_existing_attachment_set_to_other_keeps_manual_choice_beyond_recent_events(
        tmp_path: Path, initial_role: str) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Manual other', 'category': '打车', 'amount': '123.45',
                         'material_basis': 'invoice_payment'})
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf')
    trip = make_trip_pdf(tmp_path / 'trip.pdf')
    store.add_attachment(item['id'], 'invoice.pdf', invoice.read_bytes(), 'invoice')
    item = store.add_attachment(item['id'], 'trip.pdf', trip.read_bytes(), initial_role)
    attachment_id = next(a['id'] for a in item['attachments'] if a['role'] == 'trip')
    store.set_role(attachment_id, 'other')
    # The protection uses durable history, not only the last 30 visible events.
    with store.connect() as db:
        for index in range(31):
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item['id'], '2026-01-01', 'Unrelated event ' + str(index)))
    before = rows(store)
    current = store.get(item['id'])
    assert current['missing'] == ['行程单'] and not current['complete'] and not current['verified']
    assert next(a for a in current['attachments'] if a['id'] == attachment_id)['role'] == 'other'
    assert not any(e['message'].startswith('材料用途改为：') for e in current['events'])
    assert store.get(item['id'])['version'] == current['version'] and rows(store) == before
    with pytest.raises(ValueError, match='补齐材料'):
        store.transition(item['id'], 'verify')
    # A later explicit trip selection remains valid and can be reviewed.
    store.set_role(attachment_id, 'trip')
    corrected = store.get(item['id'])
    assert corrected['complete'] and not corrected['verified']
    assert store.transition(item['id'], 'verify', expected_version=corrected['version'])['ready']


def test_standard_taxi_registration_can_be_supplemented_by_trip_without_changing_amount(tmp_path: Path) -> None:
    matcher, project = context(tmp_path)
    invoice = received(matcher, project, make_invoice_pdf(tmp_path / 'invoice.pdf', amount='44.60'), 'invoice')
    created = matcher.create_expense(['q:' + invoice['id']], [], project,
        revisions={invoice['id']: 1}, fields=choice())
    assert created['item']['missing'] == ['行程单'] and created['match']['can_supplement']
    trip = received(matcher, project, make_generic_trip_pdf(tmp_path / 'native.pdf', amount='44.60'), 'other')
    supplemented = matcher.supplement(created['match']['id'], [trip['id']],
        expected_version=created['item']['version'], revisions={trip['id']: 1})
    assert supplemented['item']['complete'] and supplemented['item']['amount_cents'] == 4460
    assert not supplemented['item']['verified'] and not supplemented['item']['ready']
    assert matcher.intake.get(trip['id'])['role'] == 'trip'
    matcher.undo(supplemented['match']['id'], supplemented['item']['version'])
    assert matcher.intake.get(trip['id'])['role'] == 'other'


def test_intake_structural_trip_with_unknown_platform_keeps_taxi_role_and_category(tmp_path: Path) -> None:
    matcher, project = context(tmp_path)
    source = make_generic_trip_pdf(tmp_path / 'unknown-provider.pdf', amount='44.60')
    entry = matcher.intake.receive(source.name, source.read_bytes(), project_id=project)
    recognized = matcher.intake._analyze_batch_pdf(entry)
    assert recognized['role'] == 'trip' and recognized['fields']['category'] == '打车'
    assert recognized['fields']['amount'] == '44.60' and not recognized['item_id']
    assert matcher.store.list_items() == []


def test_trip_role_does_not_depend_on_successful_amount_extraction(tmp_path: Path) -> None:
    matcher, project = context(tmp_path)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((30, 80), 'DIDI TRAVEL - TRIP TABLE')
        page.insert_text((30, 120), 'INDEX PICKUP DESTINATION AMOUNT')
        page.insert_text((30, 150), '1 START END UNKNOWN')
        entry = matcher.intake.receive('unconfirmed-total.pdf', document.tobytes(), project_id=project)
    recognized = matcher.intake._analyze_batch_pdf(entry)
    assert recognized['role'] == 'trip' and recognized['fields']['category'] == '打车'
    assert recognized['fields']['amount'] == '' and '行程合计待核对' in recognized['issues']
    assert not recognized['item_id'] and matcher.store.list_items() == []
