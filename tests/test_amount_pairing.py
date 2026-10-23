"""Explicit amount-only confirmation retains atomicity, ambiguity and originals."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.synthesis_expenses import SynthesisExpenses
from invoice_print_layout.workbench_web import make_server
from tests.test_material_matching import expense, proof, queued, revisions, setup, snapshots


def material(matcher: MaterialMatching, project: str, name: str, role: str, amount: str | None,
             **extra: Any) -> dict[str, Any]:
    entry = queued(matcher, project, name, role, amount)
    entry['fields'].update(category='其他', title='电子发票' if role == 'invoice' else '付款截图' if role == 'payment' else '收据')
    if role == 'payment':
        entry['fields']['payment_status'] = 'succeeded'
        entry['text'] = '当前状态：支付成功'
    entry.update(extra)
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def state(matcher: MaterialMatching) -> dict[str, Any]:
    result = snapshots(matcher)
    with matcher.store.connect() as db:
        result['batches'] = [tuple(row) for row in db.execute('SELECT * FROM material_creation_batches ORDER BY rowid')]
    return result


def confirm(matcher: MaterialMatching, preview: dict[str, Any], key: str = 'amount-confirm-test-001') -> dict[str, Any]:
    return matcher.amount_confirm(preview['project_id'], preview['snapshot_token'], key, True)


def totals(preview: dict[str, Any]) -> list[tuple[int, int, int]]:
    return [(row['voucher_count'], row['payment_count'], row['amount_cents']) for row in preview['groups']]


def test_preview_is_read_only_and_confirm_commits_all_groups_unverified(setup: Any) -> None:
    matcher, project, _ = setup
    entries = [material(matcher, project, f'{role}-{amount}', role, str(amount)) for amount in (11, 17)
               for role in ('invoice', 'payment')]
    originals = {entry['path']: Path(entry['path']).read_bytes() for entry in entries}
    before = state(matcher)
    preview = matcher.amount_preview(project)
    assert totals(preview) == [(1, 1, 1100), (1, 1, 1700)]
    assert preview['summary']['groups'] == 2 and preview['summary']['materials'] == 4 and not preview['skipped']
    assert all(row['report_choice']['project_name'] == '待核对费用' for row in preview['groups'])
    assert preview['requests'] == 0 and matcher.amount_preview(project) == preview and state(matcher) == before
    result = confirm(matcher, preview)
    assert result['created'] == result['new_expenses'] == 2 and not result['replayed']
    assert all(item['stage'] == 'draft' and item['pending_review'] and not item['verified'] and not item['ready']
               and item['material_match']['amount_only'] for item in result['items'])
    assert all(row['amount_only'] for row in result['matches'])
    assert all(matcher.intake.get(entry['id'])['status'] == 'linked' for entry in entries)
    assert all(Path(path).read_bytes() == content for path, content in originals.items())
    after = state(matcher)
    replay = confirm(matcher, preview)
    assert replay['replayed'] and [item['id'] for item in replay['items']] == [item['id'] for item in result['items']]
    assert state(matcher) == after


@pytest.mark.parametrize('voucher_amounts,payment_amounts,expected', [
    (['10'], ['10', '4', '6'], [(1, 1, 1000)]),
    (['11', '17'], ['11', '17', '28'], [(1, 1, 1100), (1, 1, 1700)]),
    (['1353'], ['921', '124', '154', '154'], [(1, 4, 135300)]),
    (['10', '4'], ['14'], [(2, 1, 1400)]),
    (['8', '7'], ['9', '6'], [(2, 2, 1500)]),
    (['0.30'], ['0.10', '0.20'], [(1, 2, 30)]),
])
def test_unique_prioritized_and_non_decomposable_amount_combinations(
    setup: Any, voucher_amounts: list[str], payment_amounts: list[str], expected: list[tuple[int, int, int]],
) -> None:
    matcher, project, _ = setup
    for index, amount in enumerate(voucher_amounts):
        material(matcher, project, f'voucher-{index}', 'invoice', amount)
    for index, amount in enumerate(payment_amounts):
        material(matcher, project, f'payment-{index}', 'payment', amount)
    preview = matcher.amount_preview(project)
    assert totals(preview) == expected
    result = confirm(matcher, preview)
    assert len(result['items']) == len(expected)
    assert sorted(item['amount_cents'] for item in result['items']) == sorted(row[2] for row in expected)


@pytest.mark.parametrize('vouchers,payments', [(['14'], ['14', '14']), (['14', '14'], ['14', '14']),
                                               (['10'], ['4', '6', '3', '7']), (['5'], ['1'] * 5)])
def test_ambiguous_or_oversize_combinations_remain_manual(setup: Any, vouchers: list[str], payments: list[str]) -> None:
    matcher, project, _ = setup
    for index, amount in enumerate(vouchers):
        material(matcher, project, f'voucher-{index}', 'invoice', amount)
    for index, amount in enumerate(payments):
        material(matcher, project, f'payment-{index}', 'payment', amount)
    before = state(matcher)
    preview = matcher.amount_preview(project)
    assert not preview['groups'] and preview['skipped']
    with pytest.raises(ValueError, match='没有可确认'):
        confirm(matcher, preview)
    assert state(matcher) == before


@pytest.mark.parametrize('blocked', ['unknown_payment', 'amount_conflict', 'original_missing', 'original_changed'])
def test_bad_known_amount_competitor_blocks_only_its_component(setup: Any, blocked: str) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'voucher-14', 'invoice', '14')
    material(matcher, project, 'valid-payment-14', 'payment', '14')
    bad = material(matcher, project, 'bad-payment-14', 'payment', '14')
    material(matcher, project, 'voucher-17', 'invoice', '17')
    material(matcher, project, 'payment-17', 'payment', '17')
    if blocked == 'unknown_payment':
        bad['text'] = ''
        bad['fields'].pop('payment_status')
    elif blocked == 'amount_conflict':
        bad['issues'] = ['AI与原件字段冲突：amount']
    elif blocked == 'original_missing':
        Path(bad['path']).unlink()
    else:
        Path(bad['path']).write_bytes(proof('CHANGED ORIGINAL'))
    with matcher.store.connect() as db:
        matcher._write_queue(db, bad)
    preview = matcher.amount_preview(project)
    assert totals(preview) == [(1, 1, 1700)]
    assert any('歧义' in row['reason'] for row in preview['skipped'])
    assert confirm(matcher, preview)['created'] == 1


@pytest.mark.parametrize('ignored', ['refund', 'failed', 'unknown_amount', 'merchant_conflict'])
def test_explicitly_invalid_payments_skip_but_merchant_conflicts_are_ignored(setup: Any, ignored: str) -> None:
    matcher, project, _ = setup
    voucher = material(matcher, project, 'voucher-14', 'invoice', '14')
    payment = material(matcher, project, 'payment-14', 'payment', '14')
    if ignored == 'merchant_conflict':
        voucher['fields']['merchant'] = '第一家合成广告有限公司'
        payment['fields']['merchant'] = '第二家合成酒店有限公司'
        voucher['issues'] = payment['issues'] = ['AI与原件字段冲突：merchant', '商家冲突']
        with matcher.store.connect() as db:
            matcher._write_queue(db, voucher)
            matcher._write_queue(db, payment)
    else:
        bad = material(matcher, project, 'excluded-payment', 'payment', None if ignored == 'unknown_amount' else '14')
        if ignored == 'refund':
            bad['text'] = '当前状态：已退款'
            bad['fields']['payment_status'] = 'refunded'
        elif ignored == 'failed':
            bad['text'] = '当前状态：支付失败'
            bad['fields']['payment_status'] = 'failed'
        with matcher.store.connect() as db:
            matcher._write_queue(db, bad)
    before = state(matcher)
    preview = matcher.amount_preview(project)
    assert totals(preview) == [(1, 1, 1400)]
    assert not matcher.auto_match(project)['matched'] and state(matcher) == before
    assert confirm(matcher, preview)['created'] == 1


def test_limit_project_ownership_rail_and_optional_roles_are_reported(setup: Any) -> None:
    matcher, project, foreign = setup
    material(matcher, foreign, 'foreign-invoice', 'invoice', '14')
    material(matcher, foreign, 'foreign-payment', 'payment', '14')
    material(matcher, '', 'unassigned-invoice', 'invoice', '14')
    material(matcher, project, 'ordinary-material', 'other', '14')
    rail = material(matcher, project, 'rail-invoice', 'invoice', '14')
    rail['fields']['category'] = '高铁'
    with matcher.store.connect() as db:
        matcher._write_queue(db, rail)
    preview = matcher.amount_preview(project)
    assert not preview['groups'] and len(preview['skipped']) == 3
    assert 'foreign' not in json.dumps(preview)
    for index in range(21):
        material(matcher, project, f'limited-invoice-{index}', 'invoice', str(index + 1))
    material(matcher, project, 'limit-payment', 'payment', '1')
    preview = matcher.amount_preview(project)
    assert not preview['groups'] and any('超过20份' in row['reason'] for row in preview['skipped'])


@pytest.mark.parametrize('change', ['revision', 'extra', 'archive', 'delete', 'original', 'project', 'unknown-role'])
def test_changed_population_or_original_refuses_whole_confirmation(setup: Any, change: str) -> None:
    matcher, project, foreign = setup
    for amount in (11, 17):
        material(matcher, project, f'voucher-{amount}', 'invoice', str(amount))
        payment = material(matcher, project, f'payment-{amount}', 'payment', str(amount))
    preview = matcher.amount_preview(project)
    if change in {'archive', 'delete'}:
        payment = matcher.intake.archive_action(payment['id'], payment['revision'], project, 'archive')['entry']
        if change == 'delete':
            matcher.intake.archive_action(payment['id'], payment['revision'], project, 'delete')
    elif change == 'extra':
        material(matcher, project, 'new-competitor', 'payment', '11')
    elif change == 'original':
        Path(payment['path']).write_bytes(proof('CHANGED PAY'))
    else:
        if change == 'project':
            payment['project_id'] = foreign
        elif change == 'unknown-role':
            payment['role'] = 'other'
        payment['revision'] += 1
        with matcher.store.connect() as db:
            matcher._write_queue(db, payment)
    before = state(matcher)
    with pytest.raises(ValueError, match='变化'):
        confirm(matcher, preview)
    assert state(matcher) == before


def test_duplicate_ticket_numbers_and_physical_digest_remain_reserved(setup: Any) -> None:
    matcher, project, _ = setup
    for amount in (11, 17):
        voucher = material(matcher, project, f'voucher-{amount}', 'invoice', str(amount))
        voucher['fields']['invoice_number'] = '12345678901234567890'
        with matcher.store.connect() as db:
            matcher._write_queue(db, voucher)
        material(matcher, project, f'payment-{amount}', 'payment', str(amount))
    preview = matcher.amount_preview(project)
    assert not preview['groups']
    assert any('待确认' in row['reason'] or '重复' in row['reason'] for row in preview['skipped'])


def test_actual_later_sql_failure_rolls_back_every_expense_and_receipt(setup: Any) -> None:
    matcher, project, _ = setup
    for amount in (11, 17):
        material(matcher, project, f'voucher-{amount}', 'invoice', str(amount))
        material(matcher, project, f'payment-{amount}', 'payment', str(amount))
    preview = matcher.amount_preview(project)
    with matcher.store.connect() as db:
        db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON expenses WHEN json_extract(NEW.data,'$.amount_cents')=1700 "
                   "BEGIN SELECT RAISE(ABORT,'Synthetic second group failure'); END")
    before = state(matcher)
    with pytest.raises(sqlite3.IntegrityError, match='second group failure'):
        confirm(matcher, preview)
    assert state(matcher) == before
    with matcher.store.connect() as db:
        db.execute('DROP TRIGGER reject_second')
    assert confirm(matcher, preview)['created'] == 2


@pytest.mark.parametrize('same_key', [False, True])
def test_concurrent_confirm_never_duplicates_expenses(setup: Any, same_key: bool) -> None:
    matcher, project, _ = setup
    material(matcher, project, 'voucher-14', 'invoice', '14')
    material(matcher, project, 'payment-14', 'payment', '14')
    preview = matcher.amount_preview(project)
    start = threading.Barrier(2)
    def run(index: int) -> Any:
        start.wait(timeout=5)
        try:
            return confirm(matcher, preview, 'same-confirm-key' if same_key else f'amount-confirm-key-{index}')
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert len(matcher.store.list_items()) == len(state(matcher)['batches']) == 1
    assert sum(isinstance(result, dict) for result in results) == (2 if same_key else 1)
    if same_key:
        assert sorted(result['replayed'] for result in results) == [False, True]


def test_confirmation_flag_project_token_and_reused_key_cannot_bypass_contract(setup: Any) -> None:
    matcher, project, foreign = setup
    material(matcher, project, 'voucher-14', 'invoice', '14')
    material(matcher, project, 'payment-14', 'payment', '14')
    preview = matcher.amount_preview(project)
    before = state(matcher)
    for value in (False, None, 1, 'true'):
        with pytest.raises(ValueError):
            matcher.amount_confirm(project, preview['snapshot_token'], 'amount-test-invalid', value)
    with pytest.raises(ValueError, match='变化'):
        matcher.amount_confirm(foreign, preview['snapshot_token'], 'amount-wrong-project', True)
    assert state(matcher) == before
    confirm(matcher, preview)
    before = state(matcher)
    with pytest.raises(ValueError, match='编号'):
        matcher.amount_confirm(project, '0' * 64, 'amount-confirm-test-001', True)
    with pytest.raises(ValueError, match='编号'):
        matcher.create_batch(project, [{'suggestion_id': 's:old', 'snapshot_token': 'old', 'fields': {}}], 'amount-confirm-test-001')
    assert state(matcher) == before


def test_stable_expense_reuse_archive_restore_and_stale_pool(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Reusable existing invoice', '14')
    original_attachment = item['attachments'][0]['id']
    path, _ = matcher.store.attachment_path(original_attachment)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    material(matcher, project, 'payment-14', 'payment', '14')
    helper = SynthesisExpenses(matcher.store)
    active = matcher.board(project)['vouchers'][0]
    assert active['can_edit'] and active['can_archive'] and active['issues'] == ['待配对：付款截图']
    preview = matcher.amount_preview(project)
    archived = helper.transition(item['id'], item['version'], project, 'archive')['item']
    board = matcher.board(project)
    assert not board['vouchers'] and board['archived'][0]['key'] == 'e:' + item['id']
    assert board['archived'][0]['can_restore'] and board['archived'][0]['can_delete']
    before = state(matcher)
    with pytest.raises(ValueError, match='变化'):
        confirm(matcher, preview)
    with pytest.raises(ValueError):
        matcher.create_expense(['e:' + item['id']], [], project, expected_versions={item['id']: archived['version']},
                               fields={'project_type': 'other', 'project_name': 'Bypass', 'amount': '14'})
    assert state(matcher) == before
    deleted = helper.transition(item['id'], archived['version'], project, 'delete')['item']
    assert matcher.board(project)['deleted'][0]['can_restore_deleted']
    restored_archive = helper.transition(item['id'], deleted['version'], project, 'restore-deleted')['item']
    restored = helper.transition(item['id'], restored_archive['version'], project, 'restore')['item']
    assert restored['returned_to_synthesis'] and matcher.board(project)['vouchers'][0]['can_edit']
    preview = matcher.amount_preview(project)
    result = confirm(matcher, preview)
    assert result['new_expenses'] == 0 and result['items'][0]['id'] == item['id']
    assert not result['items'][0]['returned_to_synthesis'] and result['items'][0]['pending_review']
    assert any(row['id'] == original_attachment for row in result['items'][0]['attachments'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_saved_water_evidence_selects_fixed_report_row_without_merchant_guessing(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = material(matcher, project, 'water-invoice', 'invoice', '29.60')
    voucher['fields']['merchant'] = '合成网络科技有限公司'
    voucher['text'] = '*软饮料*娃哈哈饮用水\n金额59.20\n金额-29.60\n价税合计29.60'
    with matcher.store.connect() as db:
        matcher._write_queue(db, voucher)
    material(matcher, project, 'water-payment', 'payment', '29.60')
    preview = matcher.amount_preview(project)
    choice = preview['groups'][0]['report_choice']
    assert choice['project_type'] == 'living' and choice['project_name'] == '现场饮用水、夜宵'
    result = confirm(matcher, preview)
    assert result['items'][0]['amount_cents'] == 2960 and result['items'][0]['report_section'] == 'dining'


def test_unavailable_expense_invoice_still_competes_with_same_amount_queue_voucher(setup: Any) -> None:
    matcher, project, _ = setup
    old = expense(matcher, project, 'Unavailable existing fourteen', '14')
    original, _ = matcher.store.attachment_path(old['attachments'][0]['id'])
    original.unlink()
    material(matcher, project, 'new-fourteen-voucher', 'invoice', '14')
    material(matcher, project, 'new-fourteen-payment', 'payment', '14')
    material(matcher, project, 'independent-17-voucher', 'invoice', '17')
    material(matcher, project, 'independent-17-payment', 'payment', '17')
    preview = matcher.amount_preview(project)
    assert totals(preview) == [(1, 1, 1700)]
    assert any('e:' + old['id'] in row['keys'] for row in preview['skipped'])


def test_archived_expense_version_is_part_of_full_population_token(setup: Any) -> None:
    matcher, project, _ = setup
    existing = expense(matcher, project, 'Archived independent invoice', '19')
    helper = SynthesisExpenses(matcher.store)
    archived = helper.transition(existing['id'], existing['version'], project, 'archive')['item']
    material(matcher, project, 'voucher-14', 'invoice', '14')
    material(matcher, project, 'payment-14', 'payment', '14')
    preview = matcher.amount_preview(project)
    assert totals(preview) == [(1, 1, 1400)]
    assert any('归档' in row['reason'] for row in preview['skipped'])
    deleted = helper.transition(existing['id'], archived['version'], project, 'delete')['item']
    helper.transition(existing['id'], deleted['version'], project, 'restore-deleted')
    before = state(matcher)
    with pytest.raises(ValueError, match='变化'):
        confirm(matcher, preview)
    assert state(matcher) == before


def test_amount_and_synthesis_actions_http_are_authenticated_project_bound_and_replay_safe(setup: Any, tmp_path: Path) -> None:
    matcher, project, foreign = setup
    item = expense(matcher, project, 'Editable source invoice', '14')
    material(matcher, project, 'payment-14', 'payment', '14')
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root + '/api/state') as response:
            token = json.load(response)['token']
        def post(route: str, body: dict[str, Any], credential: str = token) -> Any:
            with urlopen(Request(root + '/api/' + route, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'X-Workbench-Token': credential})) as response:
                return json.load(response)
        with pytest.raises(HTTPError) as unauthorized:
            post('material-matching/amount-preview', {'project_id': project}, '')
        assert unauthorized.value.code == 403
        with pytest.raises(HTTPError) as no_project:
            post('material-matching/amount-preview', {})
        assert no_project.value.code == 400
        preview = post('material-matching/amount-preview', {'project_id': project})
        body = {'id': item['id'], 'expected_version': item['version'], 'project_id': foreign, 'amount': '14', 'title': 'Edited source'}
        with pytest.raises(HTTPError):
            post('synthesis-expense/edit-fields', body)
        edited = post('synthesis-expense/edit-fields', {**body, 'project_id': project})['item']
        request = {'project_id': project, 'snapshot_token': preview['snapshot_token'],
                   'idempotency_key': 'amount-http-confirm', 'amount_only_confirmed': True}
        with pytest.raises(HTTPError) as stale:
            post('material-matching/amount-confirm', request)
        assert stale.value.code == 400 and '变化' in json.load(stale.value)['error']
        preview = post('material-matching/amount-preview', {'project_id': project})
        request['snapshot_token'] = preview['snapshot_token']
        result = post('material-matching/amount-confirm', request)
        assert result['items'][0]['id'] == edited['id'] and not result['items'][0]['verified']
        assert post('material-matching/amount-confirm', request)['replayed']
        with pytest.raises(HTTPError):
            post('synthesis-expense/archive', {'id': item['id'], 'expected_version': result['items'][0]['version'], 'project_id': project})
        for asset in ('amount-pairing.js', 'amount-pairing.css'):
            with urlopen(root + '/' + asset) as response:
                assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('unassigned_role', ['invoice', 'payment', 'both'])
def test_amount_confirmation_binds_visible_unassigned_queue_materials_only_after_confirmation(
    setup: Any, unassigned_role: str,
) -> None:
    matcher, project, foreign = setup
    voucher = material(matcher, '' if unassigned_role in {'invoice', 'both'} else project, 'unassigned-invoice', 'invoice', '14')
    payment = material(matcher, '' if unassigned_role in {'payment', 'both'} else project, 'unassigned-payment', 'payment', '14')
    foreign_voucher = material(matcher, foreign, 'foreign-must-not-bind', 'invoice', '14')
    before = state(matcher)
    preview = matcher.amount_preview(project)
    assert totals(preview) == [(1, 1, 1400)] and state(matcher) == before
    assert foreign_voucher['id'] not in json.dumps(preview)
    assert matcher.suggest(project)['suggested'] == matcher.auto_match(project)['matched'] == 0
    assert state(matcher) == before
    result = confirm(matcher, preview)
    assert result['items'][0]['project_id'] == project and result['items'][0]['pending_review']
    assert all(matcher.intake.get(entry['id'])['project_id'] == project for entry in (voucher, payment))
    assert matcher.intake.get(foreign_voucher['id'])['project_id'] == foreign
    assert matcher.intake.get(foreign_voucher['id'])['status'] == 'review'


@pytest.mark.parametrize('change', ['revision', 'foreign_assignment'])
def test_unassigned_material_changes_refuse_stale_amount_confirmation(setup: Any, change: str) -> None:
    matcher, project, foreign = setup
    voucher = material(matcher, '', 'unassigned-then-changed', 'invoice', '14')
    material(matcher, project, 'assigned-payment', 'payment', '14')
    preview = matcher.amount_preview(project)
    voucher['revision'] += 1
    if change == 'foreign_assignment':
        voucher['project_id'] = foreign
    with matcher.store.connect() as db:
        matcher._write_queue(db, voucher)
    before = state(matcher)
    with pytest.raises(ValueError, match='变化'):
        confirm(matcher, preview)
    assert state(matcher) == before


def test_http_amount_confirm_binds_unassigned_invoice_to_current_project(setup: Any, tmp_path: Path) -> None:
    matcher, project, foreign = setup
    voucher = material(matcher, '', 'http-unassigned-invoice', 'invoice', '14')
    material(matcher, project, 'http-assigned-payment', 'payment', '14')
    foreign_voucher = material(matcher, foreign, 'http-foreign-invoice', 'invoice', '14')
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(root + '/api/state') as response:
            token = json.load(response)['token']
        def post(route: str, body: dict[str, Any]) -> Any:
            with urlopen(Request(root + '/api/material-matching/' + route, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
                return json.load(response)
        before = state(matcher)
        preview = post('amount-preview', {'project_id': project})
        assert totals(preview) == [(1, 1, 1400)] and state(matcher) == before
        request = {'project_id': foreign, 'snapshot_token': preview['snapshot_token'],
                   'idempotency_key': 'amount-unassigned-http', 'amount_only_confirmed': True}
        with pytest.raises(HTTPError) as wrong_project:
            post('amount-confirm', request)
        assert wrong_project.value.code == 400 and state(matcher) == before
        result = post('amount-confirm', {**request, 'project_id': project})
        assert result['items'][0]['project_id'] == project and not result['items'][0]['verified']
        assert matcher.intake.get(voucher['id'])['project_id'] == project
        assert matcher.intake.get(foreign_voucher['id'])['project_id'] == foreign
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_two_projects_cannot_concurrently_claim_the_same_unassigned_materials(setup: Any) -> None:
    matcher, project, foreign = setup
    entries = [material(matcher, '', 'shared-unassigned-' + role, role, '14') for role in ('invoice', 'payment')]
    previews = [matcher.amount_preview(pid) for pid in (project, foreign)]
    assert all(totals(preview) == [(1, 1, 1400)] for preview in previews)
    start = threading.Barrier(2)
    def run(index: int) -> Any:
        start.wait(timeout=5)
        try:
            return confirm(matcher, previews[index], f'amount-two-projects-{index}')
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    items = matcher.store.list_items()
    assert len(items) == len(state(matcher)['batches']) == 1
    assert all(matcher.intake.get(entry['id'])['project_id'] == items[0]['project_id'] for entry in entries)
