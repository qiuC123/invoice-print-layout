"""Archived materials retain evidence but cannot re-enter any expense path."""
from __future__ import annotations

import copy
import hashlib
import json
import io
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from invoice_print_layout import intake_queue as module, mail_search, invoice_claim
from invoice_print_layout.automatic_rides import organize_rides
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.intake_archive import archive_capabilities
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.workbench_web import classify_intake
from tests.helpers import make_invoice_pdf, make_trip_pdf
from tests.test_material_matching import queued, setup, snapshots, auto_queued


def action(matcher: MaterialMatching, entry: dict[str, Any], project: str, verb: str) -> dict[str, Any]:
    matcher.intake.archive_action(entry['id'], entry['revision'], project, verb)
    return matcher.intake.get(entry['id'])


def write(matcher: MaterialMatching, entry: dict[str, Any], **updates: Any) -> dict[str, Any]:
    entry = {**entry, **updates}
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


@pytest.mark.parametrize('role', ['invoice', 'receipt', 'payment', 'other', 'detail'])
@pytest.mark.parametrize('status', ['received', 'review'])
def test_archive_restore_preserves_recognition_source_and_expenses(
        setup: tuple[MaterialMatching, str, str], role: str, status: str) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, role, role, '31.80', status=status,
                    text='Source words', local_evidence={'amount': '31.80'},
                    ai_analysis={'status': 'ok'}, manual_fields=['amount'])
    baseline = snapshots(matcher)
    raw = Path(source['path']).read_bytes()
    archived = action(matcher, source, project, 'archive')
    assert archived['status'] == 'archived' and archived['revision'] == source['revision'] + 1
    assert archived['archived_from'] == {'status': status, 'result': source['result']}
    board = matcher.board(project)
    assert [card['id'] for card in board['archived']] == [source['id']]
    assert not [card for lane in ('vouchers', 'payments', 'other') for card in board[lane]]
    assert board['archived'][0]['can_restore'] and board['archived'][0]['can_delete']
    restored = action(matcher, archived, project, 'restore')
    assert restored == {**source, 'revision': source['revision'] + 2}
    assert Path(source['path']).read_bytes() == raw
    assert hashlib.sha256(raw).hexdigest() == restored['digest']
    after = snapshots(matcher)
    assert all(after[table] == baseline[table] for table in baseline if table != 'intake_queue')


def test_deleted_restore_goes_to_archive_before_explicit_active_restore(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    source = queued(matcher, '', 'unassigned', 'invoice', '25.80')
    archived = action(matcher, source, project, 'archive')
    deleted = action(matcher, archived, project, 'delete')
    board = matcher.board(project)
    assert not board['archived'] and board['deleted'][0]['id'] == source['id']
    assert board['deleted'][0]['can_restore_deleted'] and not board['deleted'][0]['can_restore']
    duplicate = matcher.intake.receive('renamed.pdf', Path(source['path']).read_bytes(), project_id=project)
    assert duplicate['duplicate'] and duplicate['status'] == 'deleted' and duplicate['revision'] == deleted['revision']
    assert duplicate['project_id'] == ''
    again = action(matcher, deleted, project, 'restore-deleted')
    assert again['status'] == 'archived' and not matcher.board(project)['vouchers']
    restored = action(matcher, again, project, 'restore')
    assert restored['status'] == source['status'] and restored['project_id'] == ''
    assert restored['fields'] == source['fields'] and Path(source['path']).exists()
    assert matcher.store.list_items() == []


@pytest.mark.parametrize('status', ['unused', 'review'])
def test_old_rail_payments_show_in_archive_delete_restore_never_rejoins_pool(
        setup: tuple[MaterialMatching, str, str], status: str) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'rail', 'payment', '357', status=status,
                    text='支付成功\n商品：12306消费')
    board = matcher.board(project)
    assert board['archived'] == board['unused'] and len(board['archived']) == 1
    assert not board['payments'] and not board['archived'][0]['can_restore']
    with pytest.raises(ValueError, match='铁路'):
        action(matcher, source, project, 'restore')
    deleted = action(matcher, source, project, 'delete')
    restored = action(matcher, deleted, project, 'restore-deleted')
    assert restored['status'] == 'unused'
    assert not matcher.board(project)['payments']
    assert matcher.board(project)['archived'][0]['can_delete']
    with pytest.raises(ValueError, match='铁路'):
        action(matcher, restored, project, 'restore')
    assert not matcher.store.list_items()


@pytest.mark.parametrize('revision', [None, True, '1', -1, 999])
def test_revision_validation_is_atomic(setup: tuple[MaterialMatching, str, str], revision: Any) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'versioned', 'invoice', '15')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.intake.archive_action(source['id'], revision, project, 'archive')
    assert snapshots(matcher) == before


def test_wrong_project_missing_project_and_expense_keys_rejected(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, other = setup
    source = queued(matcher, project, 'scoped', 'invoice', '15')
    before = snapshots(matcher)
    for pid in (other, '', 'missing', None):
        with pytest.raises(ValueError):
            matcher.intake.archive_action(source['id'], source['revision'], pid, 'archive')
    for key in ('q:' + source['id'], 'e:existing', '', 'missing'):
        with pytest.raises(ValueError):
            matcher.intake.archive_action(key, source['revision'], project, 'archive')
    assert snapshots(matcher) == before


@pytest.mark.parametrize('updates', [
    {'status': 'linked', 'item_id': 'expense'}, {'status': 'duplicate'},
    {'material_match_id': 'group'}, {'status': 'converted', 'qr_status': 'converted'},
    {'qr_status': 'manual', 'replacement_id': 'other-invoice'}, {'role': 'package'}, {'role': 'invalid'}])
def test_unsupported_linked_and_converted_entries_rejected(
        setup: tuple[MaterialMatching, str, str], updates: dict[str, Any]) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'invalid', 'invoice', '15')
    source = write(matcher, source, **updates)
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        action(matcher, source, project, 'archive')
    assert snapshots(matcher) == before and not archive_capabilities(source)['can_archive']


@pytest.mark.parametrize('attached', ['attachments', 'automatic_ride_files'])
def test_digest_linkage_overrides_stale_queue_status(setup: tuple[MaterialMatching, str, str], attached: str) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'stale-link', 'invoice', '15')
    owner = matcher.store.create({'title': 'Already owning source', 'project_id': project,
                                 'category': '餐饮', 'amount': '15'})['id']
    with matcher.store.connect() as db:
        if attached == 'attachments':
            db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                       ('test-attachment', owner, source['name'], 'invoice', source['path'], source['digest']))
        else:
            db.execute('INSERT INTO automatic_ride_files VALUES (?,?,?)', (source['digest'], owner, 'invoice:test'))
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='关联'):
        action(matcher, source, project, 'archive')
    assert snapshots(matcher) == before
    assert all(card['key'] != 'q:' + source['id'] for card in matcher.board(project)['vouchers'])


def test_qr_manual_carrier_can_archive_without_claim_or_reanalysis(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'qr-carrier', 'other', None, qr_status='manual',
                    claim_url='https://example.invalid/claim', issues=['领票待处理'])
    archived = action(matcher, source, project, 'archive')
    restored = action(matcher, archived, project, 'restore')
    assert restored['qr_status'] == 'manual' and restored['claim_url'] == source['claim_url']
    assert restored['issues'] == source['issues'] and not matcher.store.list_items()


class NeverAI:
    enabled = True
    calls = 0
    def extract(self, *_: Any, **__: Any) -> Any:
        self.calls += 1
        raise AssertionError('Archived material must not call AI')


@pytest.mark.parametrize('deleted', [False, True])
def test_inactive_entries_are_noop_for_read_organize_and_reconcile(
        setup: tuple[MaterialMatching, str, str], monkeypatch: pytest.MonkeyPatch, deleted: bool) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'inactive', 'invoice', '15')
    entry = action(matcher, source, project, 'archive')
    if deleted:
        entry = action(matcher, entry, project, 'delete')
    ai = NeverAI()
    matcher.intake.ai = cast(GlmInvoiceAI, ai)
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: pytest.fail('Must not decode an archived source'))
    before = snapshots(matcher)
    assert matcher.intake.prepare_batch_entry(entry['id'], force_ai=True) == entry
    assert matcher.intake.analyze(entry['id']) == entry
    assert matcher.intake.organize(entry['id']) == entry
    matcher.intake.reconcile()
    assert snapshots(matcher) == before and ai.calls == 0
    with pytest.raises(ValueError, match='归档|删除'):
        matcher.intake.resolve(entry['id'], entry['revision'], entry['fields'], 'invoice')
    with pytest.raises(ValueError):
        classify_intake(matcher.intake, {'id': entry['id'], 'revision': entry['revision'], 'role': 'invoice', 'fields': {}})
    assert snapshots(matcher) == before


@pytest.mark.parametrize('deleted', [False, True])
@pytest.mark.parametrize('role', ['invoice', 'payment', 'other'])
def test_every_explicit_material_creation_and_matching_route_refuses_inactive(
        setup: tuple[MaterialMatching, str, str], deleted: bool, role: str) -> None:
    matcher, project, _ = setup
    voucher = auto_queued(matcher, project, 'voucher', 'invoice', '15')
    payment = auto_queued(matcher, project, 'payment', 'payment', '15')
    support = queued(matcher, project, 'support', 'other', None)
    selected = {'invoice': voucher, 'payment': payment, 'other': support}[role]
    changed = action(matcher, selected, project, 'archive')
    if deleted:
        changed = action(matcher, changed, project, 'delete')
    versions = {entry['id']: entry['revision'] for entry in (voucher, payment, support)}
    versions[changed['id']] = changed['revision']
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.create_expense(['q:' + voucher['id']], [payment['id']], project,
            support_ids=[support['id']], revisions=versions,
            fields={'project_type': 'other', 'project_name': 'Synthetic', 'amount': '15'})
    with pytest.raises(ValueError):
        matcher.match('q:' + voucher['id'], [payment['id']], project,
                      support_ids=[support['id']], revisions=versions)
    assert snapshots(matcher) == before
    # Read-only suggestions and the legacy mutation engine both exclude these
    # rows; an unrelated archived support need not block the valid core pair.
    if role != 'other':
        assert not matcher.suggest(project)['suggestions']
        assert matcher.auto_match(project)['matched'] == 0
        assert snapshots(matcher) == before


def test_stale_suggestion_cannot_create_after_archive(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    voucher = auto_queued(matcher, project, 'voucher', 'invoice', '15')
    payment = auto_queued(matcher, project, 'payment', 'payment', '15')
    suggestion = matcher.suggest(project)['suggestions'][0]
    action(matcher, voucher, project, 'archive')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='候选|材料池'):
        matcher.create_expense(suggestion['voucher_keys'], suggestion['payment_ids'], project,
            revisions=suggestion['revisions'], fields={'project_type': 'other', 'project_name': 'Synthetic', 'amount': '15'},
            suggestion_id=suggestion['id'], suggestion_token=suggestion['snapshot_token'])
    assert snapshots(matcher) == before
    assert matcher.intake.get(payment['id']) == payment


def test_restored_material_can_create_only_after_explicit_confirmation(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'invoice', 'invoice', '15')
    archived = action(matcher, source, project, 'archive')
    restored = action(matcher, archived, project, 'restore')
    assert not matcher.store.list_items()
    result = matcher.create_expense(['q:' + source['id']], [], project,
        revisions={source['id']: restored['revision']},
        fields={'project_type': 'other', 'project_name': 'Synthetic', 'amount': '15'})
    assert len(matcher.store.list_items()) == 1 and not result['item']['verified']


@pytest.mark.parametrize('stage', ['ocr', 'qr', 'manual-qr', 'organize'])
def test_archive_during_async_read_or_legacy_annotation_refuses_commit(
        setup: tuple[MaterialMatching, str, str], monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'racing', 'invoice', '15')
    stale = copy.deepcopy(source)
    if stage == 'ocr':
        complete_evidence = matcher.intake._complete_local_evidence
        def read(data: dict[str, Any], rows: list[str]) -> None:
            action(matcher, source, project, 'archive')
            complete_evidence(data, rows)
        monkeypatch.setattr(matcher.intake, '_complete_local_evidence', read)
        with pytest.raises(ValueError, match='变化'):
            matcher.intake._analyze_batch_pdf(stale)
    elif stage == 'qr':
        replacement = queued(matcher, project, 'replacement', 'invoice', '15')
        monkeypatch.setattr(mail_search, 'qr_value', lambda *_: 'https://example.invalid/qr')
        def claim(*_: Any) -> dict[str, Any]:
            action(matcher, source, project, 'archive')
            return {'status': 'received', 'entry': replacement}
        monkeypatch.setattr(invoice_claim, 'claim', claim)
        # Synthetic PDF skips QR decode; exercise the same image-only carrier
        # branch with an existing valid source path having an image suffix.
        image_path = Path(source['path']).with_suffix('.png')
        image_path.write_bytes(Path(source['path']).read_bytes())
        source = write(matcher, source, path=str(image_path))
        with pytest.raises(ValueError, match='变化'):
            matcher.intake.prepare_batch_entry(source['id'])
    else:
        action(matcher, source, project, 'archive')
        with pytest.raises(ValueError, match='变化'):
            (matcher.intake._save_batch_qr_manual(stale, '', 'manual') if stage == 'manual-qr'
             else matcher.intake._save_organize_result(stale))
    assert matcher.intake.get(source['id'])['status'] == 'archived'
    assert matcher.store.list_items() == []


def test_legacy_ride_directory_scan_does_not_use_archived_originals(
        setup: tuple[MaterialMatching, str, str], tmp_path: Path) -> None:
    matcher, project, _ = setup
    invoice = matcher.intake.receive('invoice.pdf', make_invoice_pdf(tmp_path / 'invoice.pdf', amount='15').read_bytes(),
                                     project_id=project, batch='same-mail')
    trip = matcher.intake.receive('trip.pdf', make_trip_pdf(tmp_path / 'trip.pdf', amount='15').read_bytes(),
                                  project_id=project, batch='same-mail')
    invoice = write(matcher, invoice, role='invoice')
    archived = action(matcher, invoice, project, 'archive')
    before = snapshots(matcher)
    result = organize_rides(matcher.store, folder=matcher.intake.folder)
    assert result['created'] == 0 and result['supplemented'] == 0
    assert snapshots(matcher) == before
    assert Path(trip['path']).exists() and Path(archived['path']).exists()


def test_multiple_actions_with_same_revision_reject_replay(setup: tuple[MaterialMatching, str, str]) -> None:
    matcher, project, _ = setup
    source = queued(matcher, project, 'replay', 'invoice', '15')
    archived = action(matcher, source, project, 'archive')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='变化'):
        action(matcher, source, project, 'archive')
    with pytest.raises(ValueError, match='变化'):
        action(matcher, source, project, 'delete')
    assert snapshots(matcher) == before
    deleted = action(matcher, archived, project, 'delete')
    with pytest.raises(ValueError, match='状态'):
        action(matcher, deleted, project, 'restore')
    assert matcher.intake.get(source['id']) == deleted


def test_archive_during_qr_download_does_not_create_replacement_entry(
        setup: tuple[MaterialMatching, str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    matcher, project, _ = setup
    png = io.BytesIO()
    Image.new('RGB', (90, 90), 'white').save(png, format='PNG')
    source = matcher.intake.receive('qr.png', png.getvalue(), project_id=project)
    replacement = make_invoice_pdf(tmp_path / 'downloaded.pdf', amount='15').read_bytes()
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: 'https://example.invalid/qr')
    def claim(receiver: Any, *_: Any) -> dict[str, Any]:
        action(matcher, source, project, 'archive')
        entry = receiver.receive('downloaded.pdf', replacement, source='hotel_qr', project_id=project)
        return {'status': 'received', 'entry': entry}
    monkeypatch.setattr(invoice_claim, 'claim', claim)
    with pytest.raises(ValueError, match='变化'):
        matcher.intake.prepare_batch_entry(source['id'])
    assert len(matcher.intake.list()) == 1
    assert matcher.intake.get(source['id'])['status'] == 'archived'
    assert not matcher.store.list_items()
