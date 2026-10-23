"""Explicit multi-group confirmation is atomic and safe to retry."""
from __future__ import annotations

import copy
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
from invoice_print_layout.workbench_web import make_server
from tests.test_material_matching import auto_queued, auto_expense, queued, setup, snapshots


def full_state(matcher: MaterialMatching) -> dict[str, Any]:
    state = snapshots(matcher)
    with matcher.store.connect() as db:
        state['material_creation_batches'] = [tuple(row) for row in db.execute('SELECT * FROM material_creation_batches ORDER BY rowid')]
    return state


def choices(matcher: MaterialMatching, project: str, candidates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    candidates = matcher.suggest(project)['suggestions'] if candidates is None else candidates
    return [{'suggestion_id': candidate['id'], 'snapshot_token': candidate['snapshot_token'],
             'voucher_keys': candidate['voucher_keys'], 'payment_ids': candidate['payment_ids'],
             'revisions': candidate['revisions'], 'expected_versions': candidate['expected_versions'],
             'fields': {'project_type': 'other', 'project_name': f'Batch {candidate["amount_cents"] // 100}',
                        'amount': f'{candidate["amount_cents"] / 100:.2f}'}}
            for candidate in candidates if candidate['voucher_count'] == candidate['payment_count'] == 1]


@pytest.fixture
def batch(setup: tuple[MaterialMatching, str, str]) -> Any:
    matcher, project, other = setup
    entries = []
    for amount in (11, 17):
        entries.extend([auto_queued(matcher, project, f'voucher-{amount}', 'invoice', str(amount)),
                        auto_queued(matcher, project, f'payment-{amount}', 'payment', str(amount))])
    rows = choices(matcher, project)
    assert len(rows) == 2
    return matcher, project, other, entries, rows


def test_two_independent_groups_commit_once_and_enter_review(batch: Any) -> None:
    matcher, project, _, entries, rows = batch
    originals = {entry['path']: Path(entry['path']).read_bytes() for entry in entries}
    result = matcher.create_batch(project, rows, 'batch-success-001')
    assert result['created'] == result['new_expenses'] == 2 and not result['replayed']
    assert len(matcher.store.list_items()) == 2
    assert sum(item['amount_cents'] for item in result['items']) == 2800
    assert all(item['stage'] == 'draft' and item['pending_review'] and not item['verified'] and not item['ready']
               for item in result['items'])
    assert all(matcher.intake.get(entry['id'])['status'] == 'linked' for entry in entries)
    assert all(Path(path).read_bytes() == payload for path, payload in originals.items())
    before_retry = full_state(matcher)
    replay = matcher.create_batch(project, rows, 'batch-success-001')
    assert replay['replayed'] and [item['id'] for item in replay['items']] == [item['id'] for item in result['items']]
    assert full_state(matcher) == before_retry


def test_minimal_rows_derive_members_and_versions_from_verified_candidates(batch: Any) -> None:
    matcher, project, _, _, rows = batch
    minimal = [{key: row[key] for key in ('suggestion_id', 'snapshot_token', 'fields')} for row in rows]
    assert matcher.create_batch(project, minimal, 'batch-minimal-001')['created'] == 2


@pytest.mark.parametrize('field,bad', [('project_type', ''), ('project_name', ''), ('amount', ''),
                                    ('amount', '-1'), ('amount', '0.001'), ('project_type', 'wrong')])
def test_bad_later_group_has_no_partial_first_expense(batch: Any, field: str, bad: str) -> None:
    matcher, project, _, _, rows = batch
    rows[-1]['fields'][field] = bad
    before = full_state(matcher)
    with pytest.raises(ValueError):
        matcher.create_batch(project, rows, 'batch-invalid-001')
    assert full_state(matcher) == before


def test_shared_material_candidates_refuse_whole_batch(batch: Any) -> None:
    matcher, project, _, _, rows = batch
    before = full_state(matcher)
    with pytest.raises(ValueError, match='共用材料'):
        matcher.create_batch(project, [rows[0], copy.deepcopy(rows[0])], 'batch-shared-001')
    assert full_state(matcher) == before


def test_combined_and_single_candidate_overlap_is_rejected(batch: Any) -> None:
    matcher, project, _, _, rows = batch
    auto_queued(matcher, project, 'combined-payment', 'payment', '28')
    rows = choices(matcher, project)
    combined = next(candidate for candidate in matcher.suggest(project)['suggestions'] if candidate['voucher_count'] == 2)
    extra = {'suggestion_id': combined['id'], 'snapshot_token': combined['snapshot_token'],
             'fields': {'project_type': 'other', 'project_name': 'Combined', 'amount': '28'}}
    before = full_state(matcher)
    with pytest.raises(ValueError, match='共用材料'):
        matcher.create_batch(project, [rows[0], extra], 'batch-overlap-001')
    assert full_state(matcher) == before


@pytest.mark.parametrize('change', ['archive', 'delete', 'revision', 'extra-competitor', 'original', 'token', 'membership'])
def test_stale_changed_or_tampered_pool_refuses_batch(batch: Any, change: str) -> None:
    matcher, project, _, entries, rows = batch
    target = entries[-1]
    if change in {'archive', 'delete'}:
        target = matcher.intake.archive_action(target['id'], target['revision'], project, 'archive')['entry']
        if change == 'delete':
            matcher.intake.archive_action(target['id'], target['revision'], project, 'delete')
    elif change == 'revision':
        with matcher.store.connect() as db:
            matcher._write_queue(db, {**target, 'revision': target['revision'] + 1})
    elif change == 'extra-competitor':
        auto_queued(matcher, project, 'another-payment', 'payment', '11')
    elif change == 'original':
        Path(target['path']).write_bytes(Path(entries[0]['path']).read_bytes())
    elif change == 'token':
        rows[-1]['snapshot_token'] = 'invalid'
    else:
        rows[-1]['payment_ids'] = list(rows[0]['payment_ids'])
    before = full_state(matcher)
    with pytest.raises(ValueError):
        matcher.create_batch(project, rows, 'batch-stale-001')
    assert full_state(matcher) == before


def test_foreign_project_candidate_cannot_be_confirmed_in_selected_project(batch: Any) -> None:
    matcher, project, other, _, rows = batch
    auto_queued(matcher, other, 'foreign-voucher', 'invoice', '19')
    auto_queued(matcher, other, 'foreign-payment', 'payment', '19')
    foreign = choices(matcher, other)[0]
    before = full_state(matcher)
    with pytest.raises(ValueError):
        matcher.create_batch(project, [rows[0], foreign], 'batch-foreign-001')
    assert full_state(matcher) == before


def test_actual_sql_failure_rolls_back_expenses_attachments_events_queue_and_receipt(batch: Any) -> None:
    matcher, project, _, _, rows = batch
    rows.sort(key=lambda row: row['fields']['amount'])
    with matcher.store.connect() as db:
        db.execute("CREATE TRIGGER fail_second_group BEFORE INSERT ON expenses "
                   "WHEN json_extract(NEW.data,'$.project_name')='Batch 17' "
                   "BEGIN SELECT RAISE(ABORT,'Synthetic batch failure'); END")
    before = full_state(matcher)
    with pytest.raises(sqlite3.IntegrityError, match='Synthetic batch failure'):
        matcher.create_batch(project, rows, 'batch-sql-failure-001')
    assert full_state(matcher) == before
    with matcher.store.connect() as db:
        db.execute('DROP TRIGGER fail_second_group')
    assert matcher.create_batch(project, rows, 'batch-sql-failure-001')['created'] == 2


def test_duplicate_invoice_identity_in_later_group_rolls_back_first(batch: Any) -> None:
    matcher, project, _, entries, _ = batch
    for entry in entries:
        if entry['role'] == 'invoice':
            entry['fields']['invoice_number'] = '12345678901234567890'
            with matcher.store.connect() as db:
                matcher._write_queue(db, entry)
    rows = choices(matcher, project)
    before = full_state(matcher)
    with pytest.raises(ValueError, match='重复|发票'):
        matcher.create_batch(project, rows, 'batch-duplicate-number-001')
    assert full_state(matcher) == before


def test_same_idempotency_key_with_changed_fields_or_project_is_rejected(batch: Any) -> None:
    matcher, project, other, _, rows = batch
    matcher.create_batch(project, rows, 'batch-key-reuse-001')
    before = full_state(matcher)
    revised = copy.deepcopy(rows)
    revised[0]['fields']['project_name'] = 'Changed after success'
    for pid, supplied in ((project, revised), (other, rows)):
        with pytest.raises(ValueError, match='编号'):
            matcher.create_batch(pid, supplied, 'batch-key-reuse-001')
    assert full_state(matcher) == before


def test_simultaneous_same_key_returns_one_commit_and_one_replay(batch: Any) -> None:
    matcher, project, _, _, rows = batch
    start = threading.Barrier(2)
    def run() -> dict[str, Any]:
        start.wait(timeout=5)
        return matcher.create_batch(project, copy.deepcopy(rows), 'batch-double-click-001')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(result['replayed'] for result in results) == [False, True]
    assert len(matcher.store.list_items()) == 2
    assert len(full_state(matcher)['material_creation_batches']) == 1


def test_simultaneous_different_keys_never_duplicate_expenses(batch: Any) -> None:
    matcher, project, _, _, rows = batch
    start = threading.Barrier(2)
    def run(key: str) -> Any:
        start.wait(timeout=5)
        try:
            return matcher.create_batch(project, copy.deepcopy(rows), key)
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ['batch-click-one-001', 'batch-click-two-001']))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert len(matcher.store.list_items()) == 2
    assert len(full_state(matcher)['material_creation_batches']) == 1


def test_name_ambiguity_is_permitted_only_by_explicit_batch_confirmation(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    voucher = auto_queued(matcher, project, 'hotel-invoice', 'invoice', '1353')
    payment = auto_queued(matcher, project, 'hotel-payment', 'payment', '1353')
    payment['fields']['merchant'] = '另一家合成酒店有限公司'
    payment['text'] = '商家名称：另一家合成酒店有限公司\n支付成功'
    with matcher.store.connect() as db:
        matcher._write_queue(db, payment)
    suggestions = matcher.suggest(project)['suggestions']
    assert suggestions[0]['needs_name_review'] and not matcher.store.list_items()
    result = matcher.create_batch(project, choices(matcher, project, suggestions), 'batch-reviewed-name-001')
    assert result['items'][0]['pending_review'] and not result['items'][0]['verified']
    assert matcher.intake.get(voucher['id'])['status'] == 'linked'


def test_existing_expense_member_reuse_is_in_same_transaction(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    existing = auto_expense(matcher, project, 'existing', '11')
    auto_queued(matcher, project, 'payment-11', 'payment', '11')
    auto_queued(matcher, project, 'voucher-17', 'invoice', '17')
    auto_queued(matcher, project, 'payment-17', 'payment', '17')
    result = matcher.create_batch(project, choices(matcher, project), 'batch-existing-expense-001')
    assert result['created'] == 2 and result['new_expenses'] == 1
    assert existing['id'] in [item['id'] for item in result['items']]
    assert all(item['pending_review'] for item in result['items'])


@pytest.mark.parametrize('body', [None, [], {}, 'wrong', [{}]])
def test_invalid_rows_never_write(batch: Any, body: Any) -> None:
    matcher, project, _, _, _ = batch
    before = full_state(matcher)
    with pytest.raises(ValueError):
        matcher.create_batch(project, body, 'batch-malformed-001')
    assert full_state(matcher) == before


def test_batch_http_auth_and_retry_preserve_originals(batch: Any, tmp_path: Path) -> None:
    matcher, project, other, entries, rows = batch
    sources = {entry['path']: hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest() for entry in entries}
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']
    def post(body: dict[str, Any], credential: str = token) -> Any:
        with urlopen(Request(root + '/api/material-matching/create-batch', data=json.dumps(body).encode(),
                     headers={'Content-Type': 'application/json', 'X-Workbench-Token': credential})) as response:
            return json.load(response)
    body = {'project_id': project, 'idempotency_key': 'batch-http-success-001', 'groups': rows}
    try:
        before = full_state(matcher)
        for credential, request_body, status in [('', body, 403), (token, {**body, 'project_id': other}, 400)]:
            with pytest.raises(HTTPError) as error:
                post(request_body, credential)
            assert error.value.code == status
        assert full_state(matcher) == before
        result = post(body)
        assert result['created'] == 2 and not result['replayed']
        after = full_state(matcher)
        assert post(body)['replayed'] and full_state(matcher) == after
        assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in sources.items())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
