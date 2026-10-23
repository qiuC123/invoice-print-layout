from __future__ import annotations

import json
import threading
from pathlib import Path
from collections.abc import Callable
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.reimbursement import create_report
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_export import export_items
from invoice_print_layout.workbench_web import make_server
from tests.test_material_creation import confirmed_creation
from tests.test_material_matching import expense, proof, queued, revisions, setup, snapshots


def raw(matcher: MaterialMatching, key: str) -> dict[str, Any]:
    with matcher.store.connect() as db:
        return cast(dict[str, Any], json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data']))


def hotel_withdrawal(matcher: MaterialMatching, project: str) -> dict[str, Any]:
    item = matcher.store.create({'title': '发票', 'category': '酒店', 'amount': '14', 'project_id': project})
    item = matcher.store.add_attachment(item['id'], 'hotel.pdf', proof('HOTEL ORIGINAL'), 'invoice')
    payment = queued(matcher, project, 'hotel-payment', 'payment', '14')
    linked = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment))
    return cast(dict[str, Any], matcher.undo(linked['match']['id'], linked['item']['version'])['item'])


def test_withdrawn_complete_invoice_only_reenters_review_after_explicit_creation(setup: Any) -> None:
    matcher, project, _ = setup
    item = hotel_withdrawal(matcher, project)
    assert item['complete'] and item['returned_to_synthesis']
    assert not item['pending_review'] and not item['ready'] and not item['verified']
    card = next(card for card in matcher.board(project)['vouchers'] if card['item_id'] == item['id'])
    assert card['returned_to_synthesis'] and card['report_choice']['project_type'] == 'living'
    attachment_id = item['attachments'][0]['id']
    path, _ = matcher.store.attachment_path(attachment_id)
    original = path.read_bytes()
    result = confirmed_creation(matcher, [card['key']], [], project,
                                expected_versions={item['id']: item['version']},
                                fields={'project_type': 'living', 'project_name': '住宿费(附清单)'})
    reopened = result['item']
    assert reopened['id'] == item['id'] and reopened['pending_review'] and not reopened['returned_to_synthesis']
    assert not reopened['verified'] and reopened['stage'] == 'draft'
    assert reopened['attachments'] == item['attachments'] and path.read_bytes() == original
    assert 'returned_to_synthesis' not in raw(matcher, item['id'])


def test_legacy_withdrawal_is_read_only_and_later_edit_does_not_hold_ordinary_draft(setup: Any) -> None:
    matcher, project, _ = setup
    item = hotel_withdrawal(matcher, project)
    with matcher.store.connect() as db:
        stored = raw(matcher, item['id'])
        stored.pop('returned_to_synthesis')
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored), item['id']))
    before = snapshots(matcher)
    item = matcher.store.get(item['id'])
    assert item['returned_to_synthesis'] and not item['pending_review']
    assert matcher.store.get(item['id']) == item and snapshots(matcher) == before
    assert next(card for card in matcher.board(project)['vouchers'] if card['id'] == item['id'])
    with matcher.store.connect() as db:
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item['id'], '2099-01-01', '更新事项；历史人工修改'))
    current = matcher.store.get(item['id'])
    assert not current['returned_to_synthesis'] and current['pending_review']
    ordinary = expense(matcher, project, 'Unrelated missing material')
    assert not ordinary['returned_to_synthesis'] and not ordinary['complete']
    matcher.store.request_review([ordinary['id']], {ordinary['id']: ordinary['version']})
    assert matcher.store.get(ordinary['id'])['pending_review']


def test_withdrawal_guards_every_existing_mutation_and_report_path(setup: Any) -> None:
    matcher, project, _ = setup
    item = hotel_withdrawal(matcher, project)
    second = expense(matcher, project, 'Ordinary merge member')
    extra = queued(matcher, project, 'extra invoice', 'invoice', '14')
    review = ExpenseReview(matcher.store)
    operations: list[Callable[[], Any]] = [
        lambda: matcher.store.update(item['id'], {'title': 'Bypass'}),
        lambda: matcher.store.edit_review_fields(item['id'], item['version'], 'other', 'Edited', '14'),
        lambda: matcher.store.request_review([item['id']], {item['id']: item['version']}),
        lambda: matcher.store.add_attachment(item['id'], 'extra.pdf', proof('EXTRA'), 'other'),
        lambda: matcher.store.set_role(item['attachments'][0]['id'], 'receipt'),
        lambda: matcher.intake.resolve(extra['id'], extra['revision'], extra['fields'], 'invoice', item['id'], item['version']),
        lambda: review.confirm(item['id'], item['version'], {}),
        lambda: review.merge_preview([item['id'], second['id']], item['id']),
        lambda: review.merge([item['id'], second['id']], item['id'], 'old-token', 'merge'),
        lambda: export_items(matcher.store, [item['id']]),
        lambda: create_report(matcher.store, [item['id']], submit=False),
        *[lambda action=action: matcher.store.transition(item['id'], action) for action in ('verify', 'draft', 'submitted', 'reimbursed', 'return_materials')],
    ]
    before = snapshots(matcher)
    for operation in operations:
        with pytest.raises(ValueError):
            operation()
        assert snapshots(matcher) == before
    classifier = AutomaticCategory(matcher.store, threading.Lock())
    assert not classifier.eligible(item, None)
    assert not classifier.save_category(item, '餐饮', classifier.input_key(item))
    assert snapshots(matcher) == before


def test_legacy_held_state_survives_move_and_soft_delete_restore(setup: Any) -> None:
    matcher, project, other = setup
    item = hotel_withdrawal(matcher, project)
    with matcher.store.connect() as db:
        stored = raw(matcher, item['id'])
        stored.pop('returned_to_synthesis')
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored), item['id']))
        MaterialMatching.move_expense(db, item['id'], matcher._project(other))
    current = matcher.store.get(item['id'])
    assert current['returned_to_synthesis'] and current['project_id'] == other
    hidden = matcher.store.remove(current['id'], current['version'])
    restored = matcher.store.remove(hidden['id'], hidden['version'], restore=True)
    assert restored['returned_to_synthesis'] and not restored['pending_review']
    assert restored['attachments'] == item['attachments'] and restored['amount_cents'] == 1400


@pytest.mark.parametrize('basis', ['standard', 'invoice_payment', 'payment_only'])
def test_review_edit_changes_only_three_choices_and_keeps_original_material_policy(setup: Any, basis: str) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Existing named invoice')
    with matcher.store.connect() as db:
        stored = raw(matcher, item['id'])
        stored.update(material_basis=basis, invoice_number='12345678901234567890',
                      history_imported=True, confirmation={'reason': 'Original human confirmation'}, verified=True)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored), item['id']))
        before_state = matcher._member_state(db, item['id'])
    current = matcher.store.get(item['id'])
    path, _ = matcher.store.attachment_path(current['attachments'][0]['id'])
    original = path.read_bytes()
    edited = matcher.store.edit_review_fields(item['id'], current['version'], 'other', '打印', '15.40')
    assert edited['title'] == edited['display_name'] == edited['project_name'] == '打印'
    assert edited['project_type'] == 'other' and edited['report_section'] == 'other' and edited['category'] == '其他'
    assert edited['amount_cents'] == 1540 and not edited['amount_pending'] and not edited['verified']
    assert edited['pending_review'] and edited['review_requested'] and not edited['ready']
    changed = {'title', 'display_name', 'project_type', 'project_name', 'report_section', 'report_group',
               'category', 'amount_cents', 'amount_pending', 'verified', 'review_requested', 'updated_at', 'legacy'}
    after_raw = raw(matcher, item['id'])
    assert {key: value for key, value in before_state['data'].items() if key not in changed} == {
        key: value for key, value in after_raw.items() if key not in changed}
    assert after_raw['material_basis'] == basis and path.read_bytes() == original
    with matcher.store.connect() as db:
        assert matcher._member_state(db, item['id'])['attachments'] == before_state['attachments']


@pytest.mark.parametrize('invalid', ['stale', 'missing_version', 'blank_amount', 'negative', 'infinite',
                                   'blank_name', 'bad_fixed_name', 'cancelled', 'deleted', 'merged', 'held'])
def test_review_edit_rejects_invalid_or_noneditable_state_without_writes(setup: Any, invalid: str) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Review validation')
    if invalid == 'held':
        item = hotel_withdrawal(matcher, project)
    elif invalid in {'cancelled', 'merged'}:
        item = matcher.store.transition(item['id'], 'cancelled')
    elif invalid == 'deleted':
        item = matcher.store.remove(item['id'], item['version'])
    expected = item['version'] if invalid not in {'stale', 'missing_version'} else ('stale' if invalid == 'stale' else '')
    project_type, name = ('living', 'Invalid arbitrary hotel') if invalid == 'bad_fixed_name' else ('other', 'Edit')
    name = '' if invalid == 'blank_name' else name
    amount = {'blank_amount': '', 'negative': '-1', 'infinite': 'Infinity'}.get(invalid, '16')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.store.edit_review_fields(item['id'], expected, project_type, name, amount)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('legacy', [False, True])
def test_manual_edit_cannot_be_overwritten_by_later_group_undo(setup: Any, legacy: bool) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Pair edited later')
    payment = queued(matcher, project, 'pair-payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment))
    if legacy:
        with matcher.store.connect() as db:
            group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (result['match']['id'],)).fetchone()['data'])
            group.pop('aggregate_after')
            group.pop('queue_after')
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group), group['id']))
    edited = matcher.store.edit_review_fields(item['id'], result['item']['version'], 'other', '打印', '16')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], edited['version'])
    assert snapshots(matcher) == before and matcher.store.get(item['id'])['amount_cents'] == 1600


def test_legacy_update_honors_explicit_report_choice_and_requires_both_fields(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Old update compatibility')
    edited = matcher.store.update(item['id'], {'project_type': 'transport', 'project_name': '火车、飞机',
                                              'amount': '18', 'report_section': 'old obsolete value'})
    assert edited['title'] == '火车、飞机' and edited['category'] == '高铁' and edited['report_section'] == 'rail'
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.store.update(item['id'], {'project_type': 'other'})
    assert snapshots(matcher) == before


@pytest.mark.parametrize('description_field', ['goods', 'purchase_content', 'payment_note', 'transaction_description', 'text'])
def test_saved_queue_evidence_suggests_printing_without_changing_fee_or_rereading_original(
    setup: Any, monkeypatch: pytest.MonkeyPatch, description_field: str,
) -> None:
    matcher, project, _ = setup
    entry = queued(matcher, project, '26360000000000000001', 'invoice', '14')
    entry['fields'] = {'title': '26360000000000000001', 'category': '其他', 'amount': '14', 'merchant': '合成测试广告有限公司'}
    if description_field == 'text':
        entry['text'] = '*现代服务*打印制作\n购买方：合成购买公司'
    else:
        entry['fields'][description_field] = '图文快印服务'
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    before = snapshots(matcher)
    card = matcher._queue_card(entry)
    assert card['report_choice_suggestion']['project_name'] == card['report_choice']['project_name'] == '打印'
    assert 'text' not in card and snapshots(matcher) == before
    item = matcher.store.create({'title': '电子发票', 'project_id': project, 'amount': '14', 'category': '其他'},
                                source='intake:' + entry['id'])
    with matcher.store.connect() as db:
        entry.update(status='linked', item_id=item['id'])
        matcher._write_queue(db, entry)
    before = snapshots(matcher)
    def fail_read(path: Path) -> bytes:
        raise AssertionError('Saved recognition must not reread original bytes')
    monkeypatch.setattr(Path, 'read_bytes', fail_read)
    viewed = matcher.store.get(item['id'])
    assert viewed['report_choice']['project_name'] == viewed['report_choice_suggestion']['project_name'] == '打印'
    assert viewed['title'] == '电子发票' and viewed['amount_cents'] == 1400
    assert 'recognition_sources' not in viewed and 'text' not in viewed and snapshots(matcher) == before


def test_optional_print_description_absence_and_manual_name_do_not_change_legacy_choice(setup: Any) -> None:
    matcher, project, _ = setup
    entry = queued(matcher, project, 'Unrelated invoice filename', 'invoice', '14')
    entry['fields'] = {'category': '其他', 'amount': '14', 'note': '打印费', 'merchant': '图文快印有限公司'}
    card = matcher._queue_card(entry)
    assert not card['report_choice_suggestion'] and card['report_choice']['project_name'] == 'Unrelated invoice filename'
    entry['fields'].update(goods='打印制作', title='人工命名的现场材料')
    card = matcher._queue_card(entry)
    assert not card['report_choice_suggestion'] and card['report_choice']['project_name'] == '人工命名的现场材料'


def test_review_fields_http_state_auth_stale_and_classification_assets(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    project = ExpenseProjects(store).registry.create_project('Synthetic review project')['id']
    item = store.create({'project_id': project, 'title': 'Missing material is editable', 'amount': '12', 'category': '其他'})
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root + '/api/state') as response:
            state = json.load(response)
        assert len(state['report_types']) == 6 and 'returned_to_synthesis' in state['items'][0]
        body = {'id': item['id'], 'expected_version': item['version'], 'project_type': 'other', 'project_name': '打印', 'amount': '12.30'}
        def post(token: str) -> Any:
            with urlopen(Request(root + '/api/review/edit-fields', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
                return json.load(response)
        with pytest.raises(HTTPError) as unauthenticated:
            post('invalid')
        assert unauthenticated.value.code == 403
        edited = post(state['token'])
        assert edited['project_name'] == '打印' and edited['amount_cents'] == 1230 and edited['pending_review']
        with pytest.raises(HTTPError) as stale:
            post(state['token'])
        assert stale.value.code == 400 and '变化' in json.load(stale.value)['error']
        assert store.get(item['id']) == edited
        for asset in ('review-edit.js', 'review-edit.css'):
            with urlopen(root + '/' + asset) as response:
                assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
