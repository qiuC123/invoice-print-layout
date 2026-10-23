from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf
import pytest

from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.automatic_rides import organize_rides
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.reimbursement import create_report
from invoice_print_layout.report_snapshot import snapshot
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from invoice_print_layout.workbench_web import make_server


def proof(text: str) -> bytes:
    with pymupdf.open() as document:
        document.new_page().insert_text((20, 40), text)
        return document.tobytes()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[MaterialMatching, str, str]:
    store = ExpenseStore(tmp_path)
    projects = ExpenseProjects(store)
    first = projects.registry.create_project('Synthetic source')['id']
    second = projects.registry.create_project('Synthetic target')['id']
    return MaterialMatching(store, IntakeQueue(store)), first, second


def queued(matcher: MaterialMatching, project_id: str, title: str, role: str, amount: str) -> dict[str, Any]:
    entry = matcher.intake.receive(title + '.pdf', proof(title), project_id=project_id)
    entry.update(status='review', role=role, fields={'title': title, 'category': '餐饮', 'amount': amount})
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def expense(matcher: MaterialMatching, pid: str, title: str = 'Synthetic expense', amount: str = '14') -> dict[str, Any]:
    item = matcher.store.create({'title': title, 'amount': amount, 'project_id': pid, 'category': '餐饮',
                                 'material_basis': 'invoice_payment'})
    return matcher.store.add_attachment(item['id'], title + '.pdf', proof(title), 'invoice')


def matched(matcher: MaterialMatching, pid: str, mode: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payment = queued(matcher, pid, 'Payment', 'payment', '14')
    if mode == 'single':
        voucher = expense(matcher, pid)
        result = matcher.match('e:' + voucher['id'], [payment['id']], pid,
                               expected_version=voucher['version'], revisions={payment['id']: payment['revision']})
    elif mode == 'multiple':
        vouchers = [expense(matcher, pid, 'Voucher ten', '10'), expense(matcher, pid, 'Voucher four', '4')]
        result = matcher.match(['e:' + entry['id'] for entry in vouchers], [payment['id']], pid,
                               expected_versions={entry['id']: entry['version'] for entry in vouchers},
                               revisions={payment['id']: payment['revision']})
    else:
        voucher = queued(matcher, pid, 'Voucher', 'invoice', '14')
        result = matcher.create_expense(['q:' + voucher['id']], [payment['id']], pid,
                                        revisions={entry['id']: entry['revision'] for entry in [voucher, payment]},
                                        fields={'project_type': 'other', 'project_name': 'Synthetic expense', 'amount': '14'})
    return result['item'], result['match']


def stored_tables(store: ExpenseStore) -> dict[str, list[tuple[Any, ...]]]:
    with store.connect() as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('expenses', 'attachments', 'events', 'intake_queue', 'material_matches')}


@pytest.mark.parametrize('stage', ['draft', 'cancelled'])
def test_delete_restore_retains_identity_amount_originals_and_previous_stage(setup: Any, stage: str) -> None:
    matcher, pid, _ = setup
    item = expense(matcher, pid)
    if stage == 'cancelled':
        item = matcher.store.transition(item['id'], 'cancelled')
    original = matcher.store.attachment_path(item['attachments'][0]['id'])[0]
    content = original.read_bytes()
    removed = matcher.store.remove(item['id'], item['version'])
    assert removed['deleted'] and removed['deleted_at'] and removed['stage'] == stage
    assert not removed['ready'] and not removed['pending_review']
    assert not matcher.store.list_items()
    assert matcher.store.list_items(include_deleted=True)[0]['id'] == item['id']
    assert snapshot([removed])['total_cents'] == 0
    assert removed['attachments'] == item['attachments'] and original.read_bytes() == content
    with pytest.raises(ValueError, match='已变化'):
        matcher.store.remove(item['id'], item['version'], restore=True)
    restored = matcher.store.remove(item['id'], removed['version'], restore=True)
    assert restored['id'] == item['id'] and restored['amount_cents'] == item['amount_cents']
    assert restored['stage'] == stage and not restored['deleted'] and not restored['verified']
    assert restored['attachments'] == item['attachments'] and original.read_bytes() == content
    assert len(matcher.store.list_items()) == 1
    assert matcher.store.remove(item['id'], restored['version'], restore=True) == restored


@pytest.mark.parametrize('stage', ['submitted', 'reimbursed'])
def test_submitted_or_reimbursed_cannot_delete(setup: Any, stage: str) -> None:
    matcher, pid, _ = setup
    item = matcher.store.create({'title': 'Paid item', 'amount': '3', 'project_id': pid,
                                 'material_basis': 'payment_only'})
    item = matcher.store.add_attachment(item['id'], 'payment.pdf', proof('Paid'), 'payment')
    item = matcher.store.transition(item['id'], 'verify')
    item = matcher.store.transition(item['id'], 'submitted')
    if stage == 'reimbursed':
        item = matcher.store.transition(item['id'], 'reimbursed')
    before = stored_tables(matcher.store)
    with pytest.raises(ValueError, match='仅可删除'):
        matcher.store.remove(item['id'], item['version'])
    assert stored_tables(matcher.store) == before


def test_delete_verified_draft_cannot_export_and_restoration_requires_review(setup: Any) -> None:
    matcher, pid, _ = setup
    item = matcher.store.create({'title': 'Verified draft', 'amount': '3', 'project_id': pid,
                                 'material_basis': 'payment_only'})
    item = matcher.store.add_attachment(item['id'], 'verified.pdf', proof('Verified'), 'payment')
    item = matcher.store.transition(item['id'], 'verify')
    assert item['ready']
    removed = matcher.store.remove(item['id'], item['version'])
    assert removed['verified'] and not removed['ready']
    assert matcher.store.remove(item['id'], removed['version']) == removed
    for submit in [True, False]:
        with pytest.raises(ValueError, match='Verified draft'):
            create_report(matcher.store, [item['id']], submit=submit)
    restored = matcher.store.remove(item['id'], removed['version'], restore=True)
    assert not restored['verified'] and not restored['ready'] and restored['pending_review']


def test_deleted_blocks_edit_attachment_review_export_and_category(setup: Any) -> None:
    matcher, pid, target = setup
    item = expense(matcher, pid)
    removed = matcher.store.remove(item['id'], item['version'])
    before = stored_tables(matcher.store)
    for operation in [lambda: matcher.store.update(item['id'], {'title': 'Changed'}),
                      lambda: matcher.store.transition(item['id'], 'draft'),
                      lambda: matcher.store.add_attachment(item['id'], 'new.pdf', proof('New'), 'payment'),
                      lambda: matcher.store.set_role(item['attachments'][0]['id'], 'receipt'),
                      lambda: matcher.store.request_review([item['id']], {item['id']: removed['version']}),
                      lambda: ExpenseProjects(matcher.store).move([item['id']], target, {item['id']: removed['version']}),
                      lambda: export_items(matcher.store, [item['id']])]:
        with pytest.raises(ValueError):
            operation()
    category = AutomaticCategory(matcher.store, nullcontext())
    assert not category.eligible(removed, None)
    assert category.run(item['id'])['status'] == 'skipped'
    assert not category.save_category(removed, '酒店', category.input_key(removed))
    assert stored_tables(matcher.store) == before


@pytest.mark.parametrize('mode', ['single', 'multiple', 'registration'])
def test_deleted_match_cannot_release_or_reuse_and_restored_match_can_undo(setup: Any, mode: str) -> None:
    matcher, pid, _ = setup
    item, group = matched(matcher, pid, mode)
    removed = matcher.store.remove(item['id'], item['version'])
    board = matcher.board(pid)
    assert not board['matches'] and not board['payments'] and not board['vouchers']
    with pytest.raises(ValueError):
        matcher.undo(group['group_id'], removed['version'])
    support = queued(matcher, pid, 'Extra', 'detail', '14')
    with pytest.raises(ValueError):
        matcher.supplement(group['group_id'], [support['id']], expected_version=removed['version'],
                           revisions={support['id']: support['revision']})
    restored = matcher.store.remove(item['id'], removed['version'], restore=True)
    matcher.undo(group['group_id'], restored['version'])
    assert matcher.intake.get(group['payment_ids'][0])['status'] == 'review'


@pytest.mark.parametrize('mode', ['single', 'multiple', 'registration'])
def test_move_group_with_supplement_and_undo_never_returns_to_source(setup: Any, mode: str) -> None:
    matcher, source, target = setup
    item, group = matched(matcher, source, mode)
    support = queued(matcher, source, 'Waybill', 'detail', '14')
    result = matcher.supplement(group['group_id'], [support['id']], expected_version=item['version'],
                                revisions={support['id']: support['revision']})
    item = result['item']
    originals = {Path(entry['path']): Path(entry['path']).read_bytes() for entry in matcher.intake.list()}
    attachments = item['attachments']
    with matcher.store.connect() as db:
        before = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (group['id'],)).fetchone()['data'])
    moved = ExpenseProjects(matcher.store).move([item['id']], target, {item['id']: item['version']})
    assert moved['moved'] == 1
    item = moved['items'][0]
    assert item['project_id'] == target and item['project'] == 'Synthetic target'
    assert item['amount_cents'] == 1400 and item['attachments'] == attachments and not item['verified']
    assert item['pending_review'] and not item['ready']
    assert not matcher.board(source)['matches'] and matcher.board(target)['matches'][0]['id'] == group['id']
    for entry in matcher.intake.list():
        assert entry['project_id'] == target and entry['item_id'] == item['id']
        assert entry['revision'] == before['queue_after'][entry['id']]['revision'] + 1
    matcher.undo(group['id'], item['version'])
    for entry in matcher.intake.list():
        assert entry['project_id'] == target and entry['status'] == 'review'
    for member_id in before.get('member_before', {}):
        restored = matcher.store.get(member_id)
        assert restored['project_id'] == target and not restored['verified'] and restored['stage'] == 'draft'
    assert all(path.read_bytes() == payload for path, payload in originals.items())


def test_move_noop_invalid_target_stale_version_and_atomic_batch(setup: Any) -> None:
    matcher, source, target = setup
    item = expense(matcher, source)
    second = expense(matcher, source, 'Second', '4')
    projects = ExpenseProjects(matcher.store)
    before = stored_tables(matcher.store)
    assert projects.move([item['id']], source, {item['id']: item['version']})['moved'] == 0
    assert stored_tables(matcher.store) == before
    for ids, destination, versions in [([item['id']], 'missing', {item['id']: item['version']}),
                                       ([item['id']], target, {item['id']: 'stale'}),
                                       ([item['id'], 'missing'], target, {item['id']: item['version']}),
                                       ([item['id'], item['id']], target, {item['id']: item['version']}),
                                       ([item['id'], second['id']], target,
                                        {item['id']: item['version'], second['id']: 'stale'})]:
        with pytest.raises(ValueError):
            projects.move(ids, destination, versions)
        assert stored_tables(matcher.store) == before


def test_move_preserves_later_content_edits_in_undo_guard(setup: Any) -> None:
    matcher, source, target = setup
    item, group = matched(matcher, source, 'registration')
    item = matcher.store.update(item['id'], {'title': 'Human correction', 'amount': '14'})
    item = ExpenseProjects(matcher.store).move([item['id']], target, {item['id']: item['version']})['items'][0]
    assert item['title'] == 'Human correction' and item['project_id'] == target
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(group['id'], item['version'])
    assert matcher.store.get(item['id'])['title'] == 'Human correction'


def test_move_rejects_changed_queue_relation_atomically(setup: Any) -> None:
    matcher, source, target = setup
    item, group = matched(matcher, source, 'registration')
    entry = matcher.intake.get(group['payment_ids'][0])
    entry['material_match_id'] = 'changed'
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    before = stored_tables(matcher.store)
    with pytest.raises(ValueError, match='关联已变化'):
        ExpenseProjects(matcher.store).move([item['id']], target, {item['id']: item['version']})
    assert stored_tables(matcher.store) == before


def test_category_cannot_save_after_deleted_during_classification(setup: Any) -> None:
    matcher, source, _ = setup
    item = expense(matcher, source)
    category = AutomaticCategory(matcher.store, nullcontext())
    key = category.input_key(item)
    matcher.store.remove(item['id'], item['version'])
    assert not category.save_category(item, '酒店', key)
    assert matcher.store.get(item['id'])['category'] == item['category']


def test_row_delete_restore_move_http_contract(setup: Any) -> None:
    matcher, source, target = setup
    item = expense(matcher, source)
    server = make_server(matcher.store.workspace, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'

    def state() -> dict[str, Any]:
        with urlopen(root + '/api/state') as response:
            return json.load(response)

    token = state()['token']

    def post(route: str, body: dict[str, Any]) -> dict[str, Any]:
        with urlopen(Request(root + route, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
            return json.load(response)

    try:
        with pytest.raises(HTTPError) as stale:
            post('/api/expenses/delete', {'id': item['id'], 'expected_version': 'stale'})
        assert stale.value.code == 400
        removed = post('/api/expenses/delete', {'id': item['id'], 'expected_version': item['version']})
        assert removed['deleted'] and removed['id'] == item['id']
        current = state()
        assert current['items'][0]['deleted'] and item['id'] not in current['classification']
        with pytest.raises(HTTPError):
            with urlopen(root + '/api/report-snapshot?id=' + item['id']):
                pass
        restored = post('/api/expenses/restore', {'id': item['id'], 'expected_version': removed['version']})
        assert not restored['deleted'] and not restored['verified']
        moved = post('/api/projects/move', {'ids': [item['id']], 'project_id': target,
                                           'versions': {item['id']: restored['version']}})
        assert moved['moved'] == 1 and moved['items'][0]['project_id'] == target
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('identity', ['invoice_number', 'order_number'])
def test_same_identity_variant_stays_pending_while_deleted_then_supplements_restored(setup: Any, identity: str) -> None:
    matcher, source, _ = setup
    first = queued(matcher, source, 'Original identity', 'invoice', '14')
    first['fields'].update({identity: '26000000000000009991'})
    if identity == 'order_number':
        first['transaction_identifiers'] = [{'kind': 'shopping_order', 'namespace': '淘宝闪购',
                                            'value': first['fields'][identity]}]
    with matcher.store.connect() as db:
        matcher._write_queue(db, first)
    linked = matcher.intake.resolve(first['id'], first['revision'], first['fields'], 'invoice')
    item = matcher.store.get(linked['item_id'])
    removed = matcher.store.remove(item['id'], item['version'])
    variant = queued(matcher, source, 'New export same identity', 'invoice', '14')
    variant['fields'].update({identity: first['fields'][identity]})
    if identity == 'order_number':
        variant['transaction_identifiers'] = first['transaction_identifiers']
    with matcher.store.connect() as db:
        matcher._write_queue(db, variant)
    originals = {Path(entry['path']): Path(entry['path']).read_bytes() for entry in matcher.intake.list()}
    result = matcher.intake.organize(variant['id'])
    assert result['status'] == 'review' and not result['item_id'] and result['candidates'] == [item['id']]
    assert '对应事项已删除' in result['issues']
    assert len(matcher.store.list_items(include_deleted=True)) == 1
    assert matcher.store.get(item['id']) == removed
    for owner in ['', item['id']]:
        with pytest.raises(ValueError, match='已删除'):
            matcher.intake.resolve(variant['id'], variant['revision'], variant['fields'], 'invoice', owner,
                                   removed['version'] if owner else '')
    # Omitting the recognized identity from the old manual payload cannot bypass it.
    with pytest.raises(ValueError, match='已删除'):
        matcher.intake.resolve(variant['id'], variant['revision'], {'title': 'Manual', 'amount': '14'}, 'invoice')
    restored = matcher.store.remove(item['id'], removed['version'], restore=True)
    result = matcher.intake.organize(variant['id'])
    assert result['status'] == 'linked' and result['item_id'] == item['id']
    after = matcher.store.get(item['id'])
    assert after['amount_cents'] == restored['amount_cents'] == 1400
    assert len(after['attachments']) == 2 and len(matcher.store.list_items()) == 1
    assert all(path.read_bytes() == payload for path, payload in originals.items())


def test_deleted_original_pdf_invoice_number_without_queue_metadata_is_reserved(setup: Any, tmp_path: Path) -> None:
    from tests.helpers import make_invoice_pdf
    matcher, source, _ = setup
    number = '26000000000000008881'
    item = matcher.store.create({'title': 'Old invoice', 'amount': '14', 'project_id': source})
    original = make_invoice_pdf(tmp_path / 'old-invoice.pdf', amount='14', invoice_number=number)
    item = matcher.store.add_attachment(item['id'], original.name, original.read_bytes(), 'invoice')
    removed = matcher.store.remove(item['id'], item['version'])
    variant = queued(matcher, source, 'Different PDF same original number', 'invoice', '14')
    variant['fields']['invoice_number'] = number
    with matcher.store.connect() as db:
        matcher._write_queue(db, variant)
    assert '对应事项已删除' in matcher.intake.organize(variant['id'])['issues']
    with pytest.raises(ValueError, match='已删除'):
        matcher.intake.resolve(variant['id'], variant['revision'], variant['fields'], 'invoice')
    assert matcher.store.get(item['id']) == removed


def test_legacy_resolve_cannot_supplement_deleted_and_reconcile_cannot_rebind(setup: Any) -> None:
    matcher, source, _ = setup
    item = expense(matcher, source)
    removed = matcher.store.remove(item['id'], item['version'])
    supplemental = queued(matcher, source, 'Fresh payment', 'payment', '14')
    before = stored_tables(matcher.store)
    with pytest.raises(ValueError, match='已删除'):
        matcher.intake.resolve(supplemental['id'], supplemental['revision'], supplemental['fields'],
                               'payment', item['id'], removed['version'])
    assert stored_tables(matcher.store) == before
    original = matcher.store.attachment_path(item['attachments'][0]['id'])[0]
    duplicate = matcher.intake.receive('same-original.pdf', original.read_bytes(), project_id=source)
    duplicate.update(status='review', item_id='')
    with matcher.store.connect() as db:
        matcher._write_queue(db, duplicate)
    matcher.intake.reconcile()
    assert matcher.intake.get(duplicate['id'])['status'] == 'review'
    assert not matcher.intake.get(duplicate['id'])['item_id']
    assert matcher.store.get(item['id']) == removed


def test_review_old_confirm_merge_preview_merge_and_committed_retry_reject_deleted(setup: Any) -> None:
    matcher, source, _ = setup
    first = expense(matcher, source, 'Review first')
    second = expense(matcher, source, 'Review second')
    review = ExpenseReview(matcher.store)
    preview = review.merge_preview([first['id'], second['id']], first['id'])
    removed = matcher.store.remove(first['id'], first['version'])
    before = stored_tables(matcher.store)
    for operation in [lambda: review.confirm(first['id'], removed['version'], {'title': 'Changed'}),
                      lambda: review.merge_preview(preview['ids'], first['id']),
                      lambda: review.merge(preview['ids'], first['id'], preview['token'], 'Synthetic duplicate')]:
        with pytest.raises(ValueError, match='已删除'):
            operation()
        assert stored_tables(matcher.store) == before
    restored = matcher.store.remove(first['id'], removed['version'], restore=True)
    preview = review.merge_preview(preview['ids'], restored['id'])
    merged = review.merge(preview['ids'], restored['id'], preview['token'], 'Synthetic duplicate')
    removed = matcher.store.remove(merged['id'], merged['version'])
    before = stored_tables(matcher.store)
    with pytest.raises(ValueError, match='已删除'):
        review.merge(preview['ids'], restored['id'], preview['token'], 'Synthetic duplicate')
    assert stored_tables(matcher.store) == before


@pytest.mark.parametrize('complete', [True, False])
def test_automatic_ride_variants_do_not_supplement_deleted_or_register_as_used(setup: Any, complete: bool) -> None:
    from tests.test_automatic_rides import pair
    matcher, _, _ = setup
    trip, invoice = pair(matcher.store)
    if complete:
        assert organize_rides(matcher.store)['created'] == 1
        item = matcher.store.list_items()[0]
        for path in [trip, invoice]:
            with pymupdf.open(path) as document:
                document.set_metadata({'title': 'A fresh export'})
                document.saveIncr()
    else:
        item = matcher.store.create({'title': 'Original ride', 'category': '打车', 'amount': '123.45'})
        item = matcher.store.add_attachment(item['id'], trip.name, trip.read_bytes(), 'trip')
    removed = matcher.store.remove(item['id'], item['version'])
    with matcher.store.connect() as db:
        original_receipts = [tuple(row) for row in db.execute('SELECT * FROM automatic_ride_files')]
    payloads = [trip.read_bytes(), invoice.read_bytes()]
    result = organize_rides(matcher.store)
    assert result['created'] == result['supplemented'] == result['duplicates'] == 0
    assert result['issues'] and all('已删除' in reason for reason in result['issues'].values())
    assert matcher.store.get(item['id']) == removed
    assert len(matcher.store.list_items(include_deleted=True)) == 1
    assert payloads == [trip.read_bytes(), invoice.read_bytes()]
    with matcher.store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM automatic_ride_files')] == original_receipts
    matcher.store.remove(item['id'], removed['version'], restore=True)
    result = organize_rides(matcher.store)
    assert result['created'] == 0 and result['supplemented'] == (0 if complete else 1)
    assert len(matcher.store.list_items()) == 1


def test_normal_cancelled_identity_retains_existing_duplicate_behavior(setup: Any) -> None:
    matcher, source, _ = setup
    item = matcher.store.create({'title': 'Cancelled original', 'amount': '14', 'project_id': source,
        'order_number': 'SYNTHETIC-ORDER-20261016', 'transaction_identifiers': [
            {'kind': 'shopping_order', 'namespace': '淘宝闪购', 'value': 'SYNTHETIC-ORDER-20261016'}]})
    item = matcher.store.transition(item['id'], 'cancelled')
    entry = queued(matcher, source, 'Cancelled order variant', 'invoice', '14')
    entry['fields']['order_number'] = item['order_number']
    entry['transaction_identifiers'] = item['transaction_identifiers']
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    result = matcher.intake.organize(entry['id'])
    assert result['status'] == 'review' and result['candidates'] == [item['id']]
    assert '疑似重复' in result['issues'] and '对应事项已删除' not in result['issues']
    assert matcher.store.get(item['id']) == item


def test_old_review_and_intake_http_routes_return_clear_deleted_errors(setup: Any) -> None:
    matcher, source, _ = setup
    item = expense(matcher, source)
    second = expense(matcher, source, 'Second review')
    preview = ExpenseReview(matcher.store).merge_preview([item['id'], second['id']], item['id'])
    removed = matcher.store.remove(item['id'], item['version'])
    entry = queued(matcher, source, 'HTTP supplemental', 'payment', '14')
    server = make_server(matcher.store.workspace, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']
    try:
        before = stored_tables(matcher.store)
        for route, body in [('/api/intake/resolve', {'id': entry['id'], 'revision': entry['revision'],
                                                 'fields': entry['fields'], 'role': 'payment',
                                                 'item_id': item['id'], 'expected_version': removed['version']}),
                            ('/api/review/confirm', {'id': item['id'], 'version': removed['version']}),
                            ('/api/review/merge-preview', {'ids': preview['ids'], 'target': item['id']}),
                            ('/api/review/merge', {'ids': preview['ids'], 'target': item['id'],
                                                   'token': preview['token'], 'reason': 'Synthetic'})]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(root + route, data=json.dumps(body).encode(),
                                headers={'Content-Type': 'application/json', 'X-Workbench-Token': token}))
            assert error.value.code == 400 and '已删除' in json.loads(error.value.read())['error']
            assert stored_tables(matcher.store) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
