from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from PIL import Image
import pytest

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from tests.test_material_matching import proof


@pytest.fixture
def queue(tmp_path: Path) -> IntakeQueue:
    return IntakeQueue(ExpenseStore(tmp_path))


def save(queue: IntakeQueue, entry: dict[str, Any], **changes: Any) -> dict[str, Any]:
    entry.update(changes)
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
    return entry


def material(queue: IntakeQueue, name: str, *, number: str = '', batch: str = 'batch',
             source: str = 'mail_agent', role: str = 'other', **changes: Any) -> dict[str, Any]:
    if name.endswith('.pdf'):
        payload = proof(name)
        role = 'invoice'
    else:
        output = io.BytesIO()
        Image.new('RGB', (32, 32), (len(name), len(batch), len(number))).save(output, format={'.png': 'PNG', '.jpg': 'JPEG', '.webp': 'WEBP'}[Path(name).suffix])
        payload = output.getvalue()
    entry = queue.receive(name, payload, batch=batch, source=source)
    return save(queue, entry, **{'role': role, 'fields': {'invoice_number': number} if number else {},
                                 'status': 'review', **changes})


@pytest.mark.parametrize('image_name', ['x.pdf.png', 'x.png', 'x.jpg', 'x.pdf.webp'])
def test_filename_render_is_hidden_idempotently_and_originals_survive(queue: IntakeQueue, image_name: str) -> None:
    pdf = material(queue, 'x.pdf', number='12345678')
    image = material(queue, image_name)
    originals = {entry['path']: Path(entry['path']).read_bytes() for entry in (pdf, image)}
    assert queue.mark_invoice_render_duplicates() == 1
    marked = queue.get(image['id'])
    assert marked['duplicate_of'] == pdf['id']
    assert marked['status'] == 'duplicate' and marked['revision'] == image['revision'] + 1
    with queue.store.connect() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM intake_queue ORDER BY id')]
    assert queue.mark_invoice_render_duplicates() == 0
    queue.list()
    with queue.store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM intake_queue ORDER BY id')] == before
    assert all(Path(path).read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize('evidence', ['fields', 'claim_url', 'text'])
def test_same_number_with_different_name(queue: IntakeQueue, evidence: str) -> None:
    pdf = material(queue, 'invoice.pdf', number='12345678', status='linked', item_id='existing-fee')
    image = material(queue, 'render.png')
    changes: dict[str, Any] = {'fields': {'invoice_number': '12345678'}}
    if evidence == 'claim_url':
        changes = {'claim_url': '01,31,,12345678,249.50,20261030,'}
    elif evidence == 'text':
        changes = {'text': '电子发票\n发票号码：12345678'}
    save(queue, image, **changes)
    queue.list()
    assert queue.get(image['id'])['duplicate_of'] == pdf['id']


@pytest.mark.parametrize('changes', [
    {'fields': {'invoice_number': '87654321'}},
    {'batch': 'another-batch'}, {'batch': ''}, {'source': 'upload'}, {'project_id': 'different'},
    {'item_id': 'already-linked'}, {'role': 'payment'}, {'role': 'receipt'}, {'role': 'order'},
    {'duplicate_of': 'original-target'},
    {'fields': {'invoice_number': 'invalid'}},
    {'fields': {'invoice_number': '12345678'}, 'text': '发票号码：87654321'},
    {'fields': {'invoice_number': '12345678'}, 'claim_url': '01,31,,87654321,249.50,20261030,'},
    {'qr_check': {'mismatches': ['invoice_number']}},
    {'field_conflicts': [{'field': 'invoice_number', 'local_value': '12345678', 'ai_value': '87654321'}]},
    {'issues': ['发票号码冲突，请拆分材料或对照原件确认。']},
])
def test_uncertain_or_independent_image_is_unchanged(queue: IntakeQueue, changes: dict[str, Any]) -> None:
    material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    save(queue, image, **changes)
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image


def test_no_batch_and_ambiguous_pdf_targets_are_not_guessed(queue: IntakeQueue) -> None:
    material(queue, 'first.pdf', number='12345678', batch='')
    image = material(queue, 'first.png', batch='')
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image
    material(queue, 'x.pdf', number='12345678')
    material(queue, 'second.pdf', number='12345678')
    image = material(queue, 'x.png', number='12345678')
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image


def test_pdf_number_conflict_prevents_filename_match(queue: IntakeQueue) -> None:
    material(queue, 'x.pdf', number='12345678', text='发票号码：87654321')
    image = material(queue, 'x.png')
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image


def test_prepare_skips_already_confirmed_render_without_recognition(queue: IntakeQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('duplicate must not be decoded, OCRed, or sent to AI')
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', forbidden)
    monkeypatch.setattr(queue, '_analyze_batch_image', forbidden)
    assert queue.prepare_batch_entry(image['id'])['status'] == 'duplicate'
    assert queue.prepare_batch_entry(image['id'], force_ai=True)['status'] == 'duplicate'
    assert queue.analyze_local(image['id'])['status'] == 'duplicate'


def test_fresh_tax_qr_matches_before_claim_or_recognition(queue: IntakeQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'different.png')
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', lambda _: '01,31,,12345678,249.50,20261030,')
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('confirmed QR duplicate must not be claimed or recognized')
    monkeypatch.setattr('invoice_print_layout.invoice_claim.claim', forbidden)
    monkeypatch.setattr(queue, '_analyze_batch_image', forbidden)
    assert queue.prepare_batch_entry(image['id'])['duplicate_of'] == pdf['id']


@pytest.mark.parametrize('edited', ['image', 'pdf'])
def test_concurrent_changes_are_not_overwritten(queue: IntakeQueue, monkeypatch: pytest.MonkeyPatch, edited: str) -> None:
    from invoice_print_layout.invoice_render_duplicates import invoice_render_pairs
    pdf = material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    changed = image if edited == 'image' else pdf
    def intervening_edit(entries: list[dict[str, Any]], **kwargs: Any) -> list[tuple[str, str]]:
        pairs = invoice_render_pairs(entries, **kwargs)
        save(queue, changed, revision=changed['revision'] + 1, fields={'invoice_number': '87654321'})
        return pairs
    monkeypatch.setattr('invoice_print_layout.invoice_render_duplicates.invoice_render_pairs', intervening_edit)
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(changed['id']) == changed
    assert 'duplicate_of' not in queue.get(image['id'])


def test_attached_digest_without_link_is_not_changed(queue: IntakeQueue) -> None:
    material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    fee = queue.store.create({'title': 'Synthetic expense', 'category': '其他', 'amount': '249.50'})
    queue.store.add_attachment(fee['id'], image['name'], Path(image['path']).read_bytes(), 'invoice')
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image


def test_new_recognition_marks_render_before_return(queue: IntakeQueue) -> None:
    from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
    queue.ai = LocalOnlyInvoiceAI()
    pdf = material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'different.png')
    analyzed = {**image, 'role': 'invoice', 'fields': {'invoice_number': '12345678', 'amount': '249.50',
                'merchant': 'Synthetic shop', 'category': '其他', 'title': 'Synthetic invoice'},
                'text': '电子发票 发票号码：12345678'}
    result = queue._finish_batch_analysis(analyzed, image, native_source='local_ocr')
    assert result['status'] == 'duplicate' and result['duplicate_of'] == pdf['id']



@pytest.mark.parametrize('status', ['archived', 'unused', 'deleted'])
def test_inactive_render_is_marked_once_without_changing_status(queue: IntakeQueue, status: str) -> None:
    pdf = material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png', status=status)
    original = Path(image['path']).read_bytes()
    assert queue.mark_invoice_render_duplicates() == 1
    marked = queue.get(image['id'])
    assert marked['duplicate_of'] == pdf['id'] and marked['status'] == status
    assert marked['revision'] == image['revision'] + 1
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == marked
    assert Path(image['path']).read_bytes() == original


@pytest.mark.parametrize('status', ['archived', 'unused', 'deleted'])
def test_inactive_pdf_does_not_hide_only_available_image(queue: IntakeQueue, status: str) -> None:
    material(queue, 'x.pdf', number='12345678', status=status)
    image = material(queue, 'x.png')
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image


def test_explicit_auto_candidate_rejects_render_even_if_status_was_restored(queue: IntakeQueue) -> None:
    from invoice_print_layout.material_matching import MaterialMatching
    matcher = MaterialMatching(queue.store, queue)
    material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png', status='archived')
    assert queue.mark_invoice_render_duplicates() == 1
    marked = queue.get(image['id'])
    save(queue, marked, status='review')
    card = {**marked, 'key': 'q:' + marked['id']}
    with pytest.raises(ValueError, match='材料已关联'):
        matcher._rules_material_state(card, '', amount_only=True)
    assert queue.get(image['id']) == marked


@pytest.mark.parametrize('member', ['pdf', 'image'])
@pytest.mark.parametrize('damage', ['missing', 'replaced', 'outside'])
def test_unavailable_original_never_hides_image(queue: IntakeQueue, member: str, damage: str) -> None:
    pdf = material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    entry = pdf if member == 'pdf' else image
    path = Path(entry['path'])
    if damage == 'missing':
        path.unlink()
    elif damage == 'replaced':
        path.write_bytes(b'replaced synthetic original')
    else:
        outside = queue.store.workspace.parent / (queue.store.workspace.name + '-outside' + path.suffix)
        outside.write_bytes(path.read_bytes())
        save(queue, entry, path=str(outside))
    assert queue.mark_invoice_render_duplicates() == 0
    assert queue.get(image['id']) == image
    assert queue.get(pdf['id']) == pdf


def test_no_candidate_does_not_read_original_files(queue: IntakeQueue, monkeypatch: pytest.MonkeyPatch) -> None:
    material(queue, 'x.pdf', number='12345678')
    material(queue, 'unrelated.png')
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('unmatched material scan must not read file bytes')
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    assert queue.mark_invoice_render_duplicates() == 0


@pytest.mark.parametrize('deleted', [False, True])
def test_real_archive_restore_cannot_revive_render(queue: IntakeQueue, deleted: bool) -> None:
    from invoice_print_layout.intake_archive import archive_capabilities
    image = material(queue, 'x.png')
    archived = queue.archive_action(image['id'], image['revision'], '', 'archive')['entry']
    if deleted:
        queue.archive_action(image['id'], archived['revision'], '', 'delete')
    material(queue, 'x.pdf', number='12345678')
    assert queue.mark_invoice_render_duplicates() == 1
    marked = queue.get(image['id'])
    assert not any(archive_capabilities(marked).values())
    with pytest.raises(ValueError, match='材料状态已变化'):
        queue.archive_action(image['id'], marked['revision'], '', 'restore-deleted' if deleted else 'restore')
    assert queue.get(image['id']) == marked
    assert Path(image['path']).is_file()


@pytest.mark.parametrize('action', ['prepare_batch_entry', 'analyze_local', 'analyze', 'organize', 'resolve', 'reconcile'])
def test_review_status_cannot_bypass_duplicate_identity(queue: IntakeQueue, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    from tests.test_archive_intake_paths import ledger_state
    material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    queue.mark_invoice_render_duplicates()
    marked = queue.get(image['id'])
    save(queue, marked, status='review')
    if action == 'reconcile':
        fee = queue.store.create({'title': 'Synthetic expense', 'category': '其他', 'amount': '249.50'})
        with queue.store.connect() as db:
            db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                       ('legacy-proof', fee['id'], image['name'], 'invoice', image['path'], image['digest']))
    before = ledger_state(queue.store)
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('duplicate identity must prevent all recognition')
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', forbidden)
    monkeypatch.setattr('invoice_print_layout.intake_queue.read_receipt', forbidden)
    monkeypatch.setattr(IntakeQueue, '_analyze_batch_image', forbidden)
    if action == 'resolve':
        result = queue.resolve(image['id'], marked['revision'],
                               {'title': 'Duplicate', 'amount': '249.50', 'category': '其他'}, 'invoice')
        assert result == marked
    elif action == 'reconcile':
        queue.reconcile()
    else:
        assert getattr(queue, action)(image['id']) == marked
    assert ledger_state(queue.store) == before
    assert Path(image['path']).is_file()


@pytest.mark.parametrize('action', ['attachment', 'receipt'])
def test_old_attachment_routes_reject_duplicate_identity(queue: IntakeQueue, action: str) -> None:
    from invoice_print_layout.receipt_intake import save_receipt
    from tests.test_archive_intake_paths import ledger_state
    material(queue, 'x.pdf', number='12345678')
    image = material(queue, 'x.png')
    queue.mark_invoice_render_duplicates()
    marked = queue.get(image['id'])
    save(queue, marked, status='review')
    fields = {'title': 'Synthetic expense', 'category': '其他', 'amount': '249.50'}
    fee = queue.store.create(fields)
    before = ledger_state(queue.store)
    payload = Path(image['path']).read_bytes()
    with pytest.raises(ValueError, match='发票图片重复件'):
        if action == 'attachment':
            queue.store.add_attachment(fee['id'], 'renamed.png', payload, 'invoice')
        else:
            save_receipt(queue.store, 'renamed.png', payload, fields, 'purchase')
    assert ledger_state(queue.store) == before
    assert Path(image['path']).read_bytes() == payload
