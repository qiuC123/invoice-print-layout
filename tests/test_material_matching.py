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
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


def proof(marker: str) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((20, 30), marker)
        return document.tobytes()


@pytest.fixture
def setup(tmp_path: Path) -> tuple[MaterialMatching, str, str]:
    store = ExpenseStore(tmp_path)
    registry = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3')
    first = registry.create_project('Synthetic project')['id']
    second = registry.create_project('Another synthetic project')['id']
    return MaterialMatching(store, IntakeQueue(store)), first, second


def queued(matcher: MaterialMatching, project: str, name: str, role: str, amount: str | None,
           *, status: str = 'review', **extra: Any) -> dict[str, Any]:
    entry = matcher.intake.receive(name + '.pdf', proof(name), project_id=project)
    entry.update(role=role, fields={'title': name, 'category': '餐饮',
                 **({'amount': amount} if amount is not None else {})}, status=status, revision=1, **extra)
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def expense(matcher: MaterialMatching, project: str, name: str, amount: str = '14',
            *, merchant_name: str = '') -> dict[str, Any]:
    item = matcher.store.create({'title': name, 'project_id': project, 'category': '餐饮',
                                 'amount': amount, 'expense_date': '2026-01-02',
                                 'merchant': merchant_name,
                                 'material_basis': 'invoice_payment'})
    return matcher.store.add_attachment(item['id'], name + '.pdf', proof(name), 'invoice')


def snapshots(matcher: MaterialMatching) -> dict[str, list[tuple[Any, ...]]]:
    with matcher.store.connect() as db:
        return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('expenses', 'attachments', 'events', 'intake_queue', 'material_matches')}


def revisions(*entries: dict[str, Any]) -> dict[str, int]:
    return {entry['id']: entry['revision'] for entry in entries}


def merchant(matcher: MaterialMatching, entry: dict[str, Any], name: str) -> dict[str, Any]:
    entry['fields']['merchant'] = name
    entry['text'] = '商家名称：' + name
    if entry['role'] == 'payment':
        entry['fields']['payment_status'] = 'succeeded'
        entry['text'] += '\n当前状态：支付成功'
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def auto_queued(matcher: MaterialMatching, project: str, name: str, role: str, amount: str | None,
                **extra: Any) -> dict[str, Any]:
    """Automatic-match fixtures have an evidenced relationship; manual fixtures do not."""
    return merchant(matcher, queued(matcher, project, name, role, amount, **extra), '合成配对测试有限公司')


def auto_expense(matcher: MaterialMatching, project: str, name: str, amount: str = '14') -> dict[str, Any]:
    return expense(matcher, project, name, amount, merchant_name='合成配对测试有限公司')


def native_seller_invoice(matcher: MaterialMatching, project: str, seller: str,
                          amount: str = '100') -> dict[str, Any]:
    with pymupdf.open() as document:
        page = document.new_page(width=800, height=400)
        for x, text in [(20, '购买方'), (60, '名称'), (90, '合成购买广告有限公司'),
                        (300, '销售方'), (340, '名称'), (375, seller)]:
            page.insert_text((x, 100), text, fontname='china-s', fontsize=10)
        entry = matcher.intake.receive('synthetic-native-seller.pdf', document.tobytes(), project_id=project)
    entry.update(role='invoice', status='review', revision=1, fields={'amount': amount, 'category': '其他'})
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def test_new_voucher_and_two_payments_record_one_expense_and_preserve_originals(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Invoice', 'invoice', '14')
    payments = [queued(matcher, project, 'Payment ten', 'payment', '10'),
                queued(matcher, project, 'Payment four', 'payment', '4')]
    originals = {Path(entry['path']): Path(entry['path']).read_bytes() for entry in [voucher, *payments]}
    result = matcher.match('q:' + voucher['id'], [entry['id'] for entry in payments], project,
                           revisions=revisions(voucher, *payments))
    item = result['item']
    assert len(matcher.store.list_items()) == 1 and item['amount_cents'] == 1400
    assert item['stage'] == 'draft' and item['complete'] and not item['verified'] and not item['ready']
    assert item['review_requested'] and item['material_basis'] == 'invoice_payment'
    assert item['material_match']['payment_total_cents'] == 1400
    assert item['material_match']['payment_count'] == 2
    assert len(item['attachments']) == 3 and {a['role'] for a in item['attachments']} == {'invoice', 'payment'}
    assert all(matcher.intake.get(entry['id'])['status'] == 'linked' for entry in [voucher, *payments])
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert not list(matcher.store.files.iterdir()), 'Original intake files need no copies or destructive moves'
    assert str(matcher.store.workspace) not in json.dumps(result)
    assert result['match']['expense_version'] == item['version']
    assert result['match']['status'] == 'provisional'


def test_existing_expense_only_adds_payments_and_keeps_ledger_fields(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Existing invoice')
    payment = queued(matcher, project, 'Pay existing', 'payment', '14')
    result = matcher.match('e:' + item['id'], ['q:' + payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment),
                           fields={'title': 'Do not overwrite', 'expense_date': '2026-10-10'})
    after = result['item']
    assert len(matcher.store.list_items()) == 1
    assert after['title'] == item['title'] and after['expense_date'] == item['expense_date']
    assert after['amount_cents'] == item['amount_cents'] and len(after['attachments']) == 2
    assert not after['verified'] and after['stage'] == 'draft'


@pytest.mark.parametrize('category', ['酒店', '餐饮'])
def test_manual_payment_pairing_converts_standard_basis_and_undo_restores_it(setup: Any, category: str) -> None:
    matcher, project, _ = setup
    item = matcher.store.create({'title': 'Standard synthetic bill', 'category': category,
                                 'project_id': project, 'amount': '14', 'material_basis': 'standard'})
    item = matcher.store.add_attachment(item['id'], 'standard.pdf', proof('Standard bill'), 'invoice')
    payment = queued(matcher, project, 'Standard bill payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment))
    assert result['item']['complete'] and result['item']['material_basis'] == 'invoice_payment'
    assert result['item']['pending_review'] and not result['item']['verified']
    restored = matcher.undo(result['match']['id'], result['item']['version'])['item']
    assert restored['material_basis'] == 'standard' and restored['complete'] == item['complete']
    assert restored['amount_cents'] == item['amount_cents'] and not restored['verified']


@pytest.mark.parametrize('failure', ['bill-version', 'payment-version', 'cross-project', 'wrong-role',
                                    'mismatch', 'repeated-payment', 'missing-payment-amount', 'tampered'])
def test_invalid_match_is_atomic(setup: Any, failure: str) -> None:
    matcher, project, other = setup
    item = expense(matcher, project, 'Guarded invoice')
    payment = queued(matcher, other if failure == 'cross-project' else project, 'Guarded payment',
                     'receipt' if failure == 'wrong-role' else 'payment',
                     None if failure == 'missing-payment-amount' else '15' if failure == 'mismatch' else '14')
    version = 'stale' if failure == 'bill-version' else item['version']
    rev = {payment['id']: 0} if failure == 'payment-version' else revisions(payment)
    ids = [payment['id'], 'q:' + payment['id']] if failure == 'repeated-payment' else [payment['id']]
    if failure == 'tampered':
        Path(payment['path']).write_bytes(proof('Replacement synthetic material'))
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.match('e:' + item['id'], ids, project, expected_version=version, revisions=rev)
    assert snapshots(matcher) == before


def test_failed_queue_write_rolls_back_new_expense_and_every_association(setup: Any, monkeypatch: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Rollback invoice', 'invoice', '14')
    one = queued(matcher, project, 'Rollback pay ten', 'payment', '10')
    two = queued(matcher, project, 'Rollback pay four', 'payment', '4')
    before = snapshots(matcher)
    original = matcher._write_queue
    def fail_after_first(db: Any, entry: dict[str, Any]) -> None:
        original(db, entry)
        if entry['id'] == two['id']:
            raise RuntimeError('Synthetic post-write failure')
    monkeypatch.setattr(matcher, '_write_queue', fail_after_first)
    with pytest.raises(RuntimeError, match='post-write'):
        matcher.match('q:' + voucher['id'], [one['id'], two['id']], project,
                      revisions=revisions(voucher, one, two))
    assert snapshots(matcher) == before
    assert all(Path(entry['path']).is_file() for entry in (voucher, one, two))


def test_payment_cannot_be_reused_by_another_expense(setup: Any) -> None:
    matcher, project, _ = setup
    one = expense(matcher, project, 'Invoice one')
    two = expense(matcher, project, 'Invoice two')
    payment = queued(matcher, project, 'One shared payment', 'payment', '14')
    matcher.match('e:' + one['id'], [payment['id']], project,
                  expected_version=one['version'], revisions=revisions(payment))
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='已关联'):
        matcher.match('e:' + two['id'], [payment['id']], project,
                      expected_version=two['version'], revisions=revisions(payment))
    assert snapshots(matcher) == before


def test_digest_used_elsewhere_is_rejected_even_if_queue_metadata_is_stale(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Target invoice')
    other = expense(matcher, project, 'Other invoice')
    payment = queued(matcher, project, 'Claimed outside matching', 'payment', '14')
    matcher.store.add_attachment(other['id'], 'already.pdf', Path(payment['path']).read_bytes(), 'payment')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='其他费用'):
        matcher.match('e:' + item['id'], [payment['id']], project,
                      expected_version=item['version'], revisions=revisions(payment))
    assert snapshots(matcher) == before
    assert payment['id'] not in [card['id'] for card in matcher.board(project)['payments']]


def test_unreadable_amounts_require_explicit_per_payment_confirmation(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Unreadable invoice', 'receipt', None)
    payment = queued(matcher, project, 'Unreadable payment', 'payment', None)
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='人工明确'):
        matcher.match('q:' + voucher['id'], [payment['id']], project, revisions=revisions(voucher, payment))
    assert snapshots(matcher) == before
    result = matcher.match('q:' + voucher['id'], [payment['id']], project,
                           revisions=revisions(voucher, payment),
                           fields={'amount': '14', 'payment_amounts': {payment['id']: '14'}})
    assert result['item']['amount_cents'] == 1400


@pytest.mark.parametrize('amount', ['0', '-1', 'NaN'])
def test_zero_negative_and_invalid_payment_amounts_cannot_match(setup: Any, amount: str) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Positive invoice')
    payment = queued(matcher, project, 'Invalid pay', 'payment', amount)
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.match('e:' + item['id'], [payment['id']], project,
                      expected_version=item['version'], revisions=revisions(payment))
    assert snapshots(matcher) == before


def test_undo_preserves_voucher_expense_and_all_files_then_rematches_once(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Undo invoice', 'invoice', '14')
    payment = queued(matcher, project, 'Undo payment', 'payment', '14')
    result = matcher.match('q:' + voucher['id'], [payment['id']], project,
                           revisions=revisions(voucher, payment))
    item = result['item']
    after = matcher.undo(result['match']['id'], item['version'])
    restored = matcher.intake.get(payment['id'])
    assert after['match']['status'] == 'undone'
    assert not after['item']['complete'] and after['item']['missing'] == ['付款截图']
    assert not after['item']['verified'] and not after['item']['review_requested']
    assert 'material_match' not in after['item'] and len(after['item']['attachments']) == 1
    assert restored['status'] == 'review' and not restored['item_id'] and restored['revision'] == 3
    assert matcher.intake.get(voucher['id'])['status'] == 'linked'
    assert all(Path(entry['path']).is_file() for entry in (voucher, payment))
    repeated = matcher.match('e:' + item['id'], [payment['id']], project,
                             expected_version=after['item']['version'], revisions=revisions(restored))
    assert repeated['item']['amount_cents'] == 1400 and len(matcher.store.list_items()) == 1
    assert len(repeated['item']['attachments']) == 2
    assert len(matcher.board(project)['matches']) == 1


@pytest.mark.parametrize('state', ['stale', 'verified', 'confirmed', 'submitted'])
def test_undo_rejects_changed_or_confirmed_expenses(setup: Any, state: str) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Immutable match')
    payment = queued(matcher, project, 'Immutable payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment), provisional=state != 'confirmed')
    if state in {'verified', 'submitted'}:
        matcher.store.transition(item['id'], 'verify')
    if state == 'submitted':
        matcher.store.transition(item['id'], 'submitted')
    version = 'stale' if state == 'stale' else matcher.store.get(item['id'])['version']
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.undo(result['match']['id'], version)
    assert snapshots(matcher) == before


def test_board_filters_project_linked_states_and_manual_qr_without_paths(setup: Any) -> None:
    matcher, project, other = setup
    invoice = queued(matcher, project, 'Visible invoice', 'invoice', '14')
    queued(matcher, project, 'Visible pay', 'payment', '14')
    unknown = queued(matcher, '', 'Unassigned pay', 'payment', '10')
    queued(matcher, other, 'Other project pay', 'payment', '14')
    for state in ('linked', 'duplicate', 'converted'):
        queued(matcher, project, state, 'invoice', '14', status=state)
    qr = queued(matcher, project, 'QR carrier', 'invoice', '14', qr_status='manual')
    existing = expense(matcher, project, 'Visible expense')
    verified = expense(matcher, project, 'Verified expense')
    matcher.store.add_attachment(verified['id'], 'Verified pay.pdf', proof('Verified pay'), 'payment')
    matcher.store.transition(verified['id'], 'verify')
    board = matcher.board(project)
    assert {card['key'] for card in board['vouchers']} == {'q:' + invoice['id'], 'e:' + existing['id']}
    assert qr['id'] in [card['id'] for card in board['other']]
    assert next(card for card in board['other'] if card['id'] == qr['id'])['role'] == 'other'
    assert next(card for card in board['other'] if card['id'] == qr['id'])['kind'] == 'qr_claim'
    assert next(card for card in board['other'] if card['id'] == qr['id'])['qr_status'] == 'manual'
    assert next(card for card in board['vouchers'] if card['id'] == invoice['id'])['fields']['amount'] == '14'
    unassigned = next(card for card in board['payments'] if card['id'] == unknown['id'])
    assert unassigned['project_id'] == '' and '归属未定' not in unassigned['issues']
    assert str(matcher.store.workspace) not in json.dumps(board)
    assert any(card['thumbnail'].startswith('/attachment-page/') for card in board['vouchers'])
    assert any(card['thumbnail'].startswith('/intake-page/') for card in board['vouchers'])


def test_manual_match_assigns_unowned_materials_but_auto_never_does(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = auto_queued(matcher, '', 'Unassigned invoice', 'invoice', '14')
    payment = auto_queued(matcher, '', 'Unassigned payment', 'payment', '14')
    assert matcher.auto_match(project)['matched'] == 0
    result = matcher.match('q:' + voucher['id'], [payment['id']], project,
                           revisions=revisions(voucher, payment))
    assert result['item']['project_id'] == project
    assert all(matcher.intake.get(entry['id'])['project_id'] == project for entry in (voucher, payment))


def test_all_project_board_is_readonly_and_complete_standard_bills_are_not_automatic_targets(setup: Any) -> None:
    matcher, project, other = setup
    queued(matcher, project, 'First project invoice', 'invoice', '14')
    queued(matcher, other, 'Second project invoice', 'invoice', '14')
    queued(matcher, '', 'No project payment', 'payment', '14')
    standard = matcher.store.create({'title': 'Complete standard rail', 'category': '高铁',
                                     'project_id': project, 'amount': '30',
                                     'merchant': '合成配对测试有限公司'})
    standard = matcher.store.add_attachment(standard['id'], 'rail.pdf', proof('Complete rail'), 'invoice')
    auto_queued(matcher, project, 'Rail-like payment', 'payment', '30')
    before = snapshots(matcher)
    board = matcher.board('')
    assert len(board['vouchers']) == 2 and len(board['payments']) == 2
    assert 'e:' + standard['id'] not in [card['key'] for card in board['vouchers']]
    assert snapshots(matcher) == before
    assert matcher.auto_match(project)['matched'] == 0
    with pytest.raises(ValueError, match='有效项目'):
        matcher.auto_match('')


def test_auto_matches_unique_four_payment_sum_and_is_idempotent(setup: Any) -> None:
    matcher, project, _ = setup
    item = auto_expense(matcher, project, 'Unique invoice', '30')
    for index, amount in enumerate(('1', '2', '4', '23')):
        auto_queued(matcher, project, f'Unique pay {index}', 'payment', amount)
    result = matcher.auto_match(project)
    assert result['matched'] == 1 and result['unmatched'] == 0 and not result['conflicts']
    assert result['items'][0]['item']['id'] == item['id']
    assert result['items'][0]['match']['payment_count'] == 4
    before = snapshots(matcher)
    assert matcher.auto_match(project)['matched'] == 0
    assert snapshots(matcher) == before


@pytest.mark.parametrize('ambiguity', ['same-invoice-amount', 'multiple-combinations', 'overlap', 'ambiguous-overlap'])
def test_auto_does_not_guess_ambiguous_or_competing_combinations(setup: Any, ambiguity: str) -> None:
    matcher, project, _ = setup
    auto_expense(matcher, project, 'Ambiguous one', '10')
    if ambiguity == 'same-invoice-amount':
        auto_expense(matcher, project, 'Ambiguous two', '10')
        amounts = ['10']
    elif ambiguity == 'multiple-combinations':
        amounts = ['10', '4', '6']
    elif ambiguity == 'overlap':
        auto_expense(matcher, project, 'Competing invoice', '4')
        amounts = ['4', '6']
    else:
        auto_expense(matcher, project, 'Competing with ambiguous invoice', '4')
        amounts = ['10', '4', '6']
    for index, amount in enumerate(amounts):
        auto_queued(matcher, project, f'Ambiguous pay {index}', 'payment', amount)
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and result['conflicts']
    assert snapshots(matcher) == before


def test_auto_skips_received_unknown_amount_manual_qr_and_large_pool(setup: Any) -> None:
    matcher, project, _ = setup
    auto_expense(matcher, project, 'No guessing', '14')
    queued(matcher, project, 'Unrecognized received', 'payment', None, status='received')
    auto_queued(matcher, project, 'Unknown pay amount', 'payment', None)
    auto_queued(matcher, project, 'Manual QR pay', 'payment', '14', qr_status='manual')
    assert matcher.auto_match(project)['matched'] == 0
    for index in range(33):
        auto_queued(matcher, project, f'Bounded pool {index}', 'payment', '14')
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and '32' in result['conflicts'][0]['reason']
    assert snapshots(matcher) == before


def test_amount_cents_metadata_is_accepted_without_guessing(setup: Any) -> None:
    matcher, project, _ = setup
    item = auto_expense(matcher, project, 'Cents invoice', '14')
    payment = auto_queued(matcher, project, 'Cents payment', 'payment', None)
    payment['fields']['amount_cents'] = 1400
    with matcher.store.connect() as db:
        matcher._write_queue(db, payment)
    assert matcher.auto_match(project)['matched'] == 1
    assert not matcher.store.get(item['id'])['verified']


def test_material_content_digest_is_preserved(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Digest invoice')
    payment = queued(matcher, project, 'Digest payment', 'payment', '14')
    matcher.match('e:' + item['id'], [payment['id']], project,
                  expected_version=item['version'], revisions=revisions(payment))
    attachment = next(a for a in matcher.store.get(item['id'])['attachments'] if a['role'] == 'payment')
    path, _ = matcher.store.attachment_path(attachment['id'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == payment['digest']


@pytest.mark.parametrize('evidence', ['stored-number', 'linked-intake', 'historical-match'])
def test_same_invoice_number_in_different_binary_cannot_create_second_expense(setup: Any, evidence: str) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    first = queued(matcher, project, 'Invoice binary one', 'invoice', '14')
    first['fields']['invoice_number'] = number
    with matcher.store.connect() as db:
        matcher._write_queue(db, first)
    payment_one = queued(matcher, project, 'First invoice payment', 'payment', '14')
    matched = matcher.match('q:' + first['id'], [payment_one['id']], project,
                            revisions=revisions(first, payment_one))
    assert matched['item']['invoice_number'] == number
    # Exercise both historical stores used before expenses retained invoice_number.
    if evidence != 'stored-number':
        with matcher.store.connect() as db:
            raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (matched['item']['id'],)).fetchone()['data'])
            raw.pop('invoice_number')
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), raw['id']))
            if evidence == 'historical-match':
                linked = matcher.intake.get(first['id'])
                linked['fields'].pop('invoice_number')
                matcher._write_queue(db, linked)
    second = queued(matcher, project, 'Invoice binary two', 'invoice', '14')
    second['fields']['invoice_number'] = number
    with matcher.store.connect() as db:
        matcher._write_queue(db, second)
    payment_two = queued(matcher, project, 'Second invoice payment', 'payment', '14')
    assert first['digest'] != second['digest']
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='发票号码'):
        matcher.match('q:' + second['id'], [payment_two['id']], project,
                      revisions=revisions(second, payment_two))
    assert snapshots(matcher) == before
    assert len(matcher.store.list_items()) == 1 and matcher.intake.get(payment_two['id'])['status'] == 'review'


def test_old_expense_without_number_metadata_is_deduplicated_from_original_pdf(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    original = make_invoice_pdf(matcher.store.workspace / 'old-invoice.pdf', amount='14', invoice_number=number)
    item = matcher.store.create({'title': 'Old expense without number metadata', 'category': '餐饮',
                                 'project_id': project, 'amount': '14', 'material_basis': 'invoice_payment'})
    item = matcher.store.add_attachment(item['id'], original.name, original.read_bytes(), 'invoice')
    assert 'invoice_number' not in item
    with pymupdf.open(original) as document:
        document.set_metadata({'title': 'Synthetic re-download binary variant'})
        variant = document.tobytes()
    voucher = matcher.intake.receive('download-again.pdf', variant, project_id=project)
    voucher.update(role='invoice', status='review', revision=1,
                   fields={'title': 'Downloaded again', 'category': '餐饮', 'amount': '14', 'invoice_number': number})
    with matcher.store.connect() as db:
        matcher._write_queue(db, voucher)
    payment = queued(matcher, project, 'Unused second payment', 'payment', '14')
    before = snapshots(matcher)
    content_before = original.read_bytes()
    with pytest.raises(ValueError, match='原有费用票据'):
        matcher.match('q:' + voucher['id'], [payment['id']], project, revisions=revisions(voucher, payment))
    assert snapshots(matcher) == before and original.read_bytes() == content_before
    assert len(matcher.store.list_items()) == 1


def active_items(matcher: MaterialMatching) -> list[dict[str, Any]]:
    return [item for item in matcher.store.list_items() if item['stage'] != 'cancelled']


@pytest.mark.parametrize('payment_amounts', [['14'], ['7', '7']])
def test_multiple_queued_vouchers_create_one_total_with_original_evidence(setup: Any, payment_amounts: list[str]) -> None:
    matcher, project, _ = setup
    first = queued(matcher, project, 'First distinct invoice', 'invoice', '10')
    second = queued(matcher, project, 'Second distinct receipt', 'receipt', '4')
    first['fields'].update(expense_date='2026-01-02', category='餐饮', invoice_number='11111111111111111111')
    second['fields'].update(expense_date='2026-01-03', category='酒店', invoice_number='22222222222222222222')
    with matcher.store.connect() as db:
        matcher._write_queue(db, first)
        matcher._write_queue(db, second)
    payments = [queued(matcher, project, f'Multi payment {index}', 'payment', amount)
                for index, amount in enumerate(payment_amounts)]
    originals = {Path(entry['path']): Path(entry['path']).read_bytes() for entry in [first, second, *payments]}
    result = matcher.match(['q:' + first['id'], 'q:' + second['id']], [p['id'] for p in payments], project,
                           revisions=revisions(first, second, *payments))
    item, group = result['item'], result['match']
    assert len(active_items(matcher)) == 1 and item['amount_cents'] == 1400
    assert item['category'] == '其他' and item['expense_date'] == ''
    assert item['complete'] and item['pending_review'] and not item['verified'] and not item['ready']
    assert len(item['attachments']) == 2 + len(payments)
    assert group['schema_version'] == 2 and group['voucher_count'] == 2 and group['voucher_total_cents'] == 1400
    assert group['version'] == group['expected_version'] == item['version']
    assert [(v['title'], v['amount_cents'], v['category'], v['expense_date']) for v in item['material_members']] == [
        ('First distinct invoice', 1000, '餐饮', '2026-01-02'), ('Second distinct receipt', 400, '酒店', '2026-01-03')]
    assert item['invoice_numbers'] == ['11111111111111111111', '22222222222222222222']
    assert all(matcher.intake.get(entry['id'])['item_id'] == item['id'] for entry in [first, second, *payments])
    assert all(path.read_bytes() == data for path, data in originals.items())
    assert str(matcher.store.workspace) not in json.dumps(result)


@pytest.mark.parametrize('include_queue', [False, True])
def test_multiple_existing_expenses_merge_once_and_undo_fully_restores_members(setup: Any, include_queue: bool) -> None:
    matcher, project, _ = setup
    one = expense(matcher, project, 'Existing ten', '10')
    two = expense(matcher, project, 'Existing four', '4')
    legacy_queue = queued(matcher, project, 'Legacy invoice association', 'invoice', '10')
    legacy_queue.update(item_id=one['id'], status='linked')
    with matcher.store.connect() as db:
        matcher._write_queue(db, legacy_queue)
        original_states = {item['id']: matcher._member_state(db, item['id']) for item in (one, two)}
    extra = [queued(matcher, project, 'Additional two', 'receipt', '2')] if include_queue else []
    payment = queued(matcher, project, 'Combined original payment', 'payment', '16' if include_queue else '14')
    keys = ['e:' + one['id'], 'e:' + two['id'], *['q:' + entry['id'] for entry in extra]]
    result = matcher.match(keys, [payment['id']], project, expected_versions={one['id']: one['version'], two['id']: two['version']},
                           revisions=revisions(payment, *extra))
    aggregate = result['item']
    assert len(active_items(matcher)) == 1 and aggregate['amount_cents'] == (1600 if include_queue else 1400)
    assert aggregate['category'] == '餐饮'
    assert aggregate['expense_date'] == ('' if include_queue else '2026-01-02')
    assert {a['id'] for a in one['attachments'] + two['attachments']} <= {a['id'] for a in aggregate['attachments']}
    for old in (one, two):
        merged = matcher.store.get(old['id'])
        assert merged['stage'] == 'cancelled' and merged['merged_into'] == aggregate['id']
        assert merged['amount_cents'] == old['amount_cents'] and merged['title'] == old['title']
        assert not merged['attachments']
    assert matcher.intake.get(legacy_queue['id'])['item_id'] == aggregate['id']
    restored = matcher.undo(result['match']['id'], aggregate['version'])
    assert restored['item']['stage'] == 'cancelled' and not restored['item']['attachments']
    assert restored['match']['status'] == 'undone'
    assert len(active_items(matcher)) == 2
    with matcher.store.connect() as db:
        for old in (one, two):
            state = matcher._member_state(db, old['id'])
            assert state['data'] == {**original_states[old['id']]['data'], 'returned_to_synthesis': True}
            assert state['attachments'] == original_states[old['id']]['attachments']
            assert len(state['events']) == len(original_states[old['id']]['events']) + 2
    assert matcher.intake.get(legacy_queue['id'])['item_id'] == one['id']
    assert matcher.intake.get(legacy_queue['id'])['status'] == 'linked'
    assert matcher.intake.get(legacy_queue['id'])['revision'] == legacy_queue['revision'] + 2
    for entry in [payment, *extra]:
        recovered = matcher.intake.get(entry['id'])
        assert recovered['status'] == 'review' and not recovered['item_id']
        assert recovered['revision'] == entry['revision'] + 2
        assert Path(entry['path']).is_file()
    rematch = matcher.match(keys, [payment['id']], project,
                           expected_versions={old['id']: matcher.store.get(old['id'])['version'] for old in (one, two)},
                           revisions={entry['id']: matcher.intake.get(entry['id'])['revision'] for entry in [payment, *extra]})
    assert rematch['item']['id'] != aggregate['id'] and len(active_items(matcher)) == 1


def test_multiple_queue_undo_releases_every_original_and_allows_rematch_once(setup: Any) -> None:
    matcher, project, _ = setup
    vouchers = [queued(matcher, project, 'Reversible voucher one', 'invoice', '10'),
                queued(matcher, project, 'Reversible voucher two', 'receipt', '4')]
    payment = queued(matcher, project, 'Reversible merged payment', 'payment', '14')
    entries = [*vouchers, payment]
    originals = {Path(entry['path']): Path(entry['path']).read_bytes() for entry in entries}
    keys = ['q:' + entry['id'] for entry in vouchers]
    first = matcher.match(keys, [payment['id']], project, revisions=revisions(*entries))
    matcher.undo(first['match']['id'], first['item']['version'])
    assert not active_items(matcher)
    assert all(not matcher.intake.get(entry['id'])['item_id'] for entry in entries)
    second = matcher.match(keys, [payment['id']], project,
                           revisions={entry['id']: matcher.intake.get(entry['id'])['revision'] for entry in entries})
    assert len(active_items(matcher)) == 1 and second['item']['amount_cents'] == 1400
    assert second['item']['id'] != first['item']['id']
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize('failure', ['stale-version', 'stale-queue', 'cross-project', 'verified', 'submitted', 'wrong-total'])
def test_multiple_match_failure_rolls_back_entire_ledger(setup: Any, failure: str) -> None:
    matcher, project, other = setup
    first = expense(matcher, project, 'Protected member one', '10')
    second = expense(matcher, other if failure == 'cross-project' else project, 'Protected member two', '4')
    if failure in {'verified', 'submitted'}:
        with matcher.store.connect() as db:
            raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (second['id'],)).fetchone()['data'])
            raw.update(verified=failure == 'verified', stage='submitted' if failure == 'submitted' else 'draft')
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), second['id']))
        second = matcher.store.get(second['id'])
    payment = queued(matcher, project, 'Protected combined payment', 'payment', '15' if failure == 'wrong-total' else '14')
    versions = {first['id']: first['version'], second['id']: second['version']}
    if failure == 'stale-version':
        versions[second['id']] = 'outdated'
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.match(['e:' + first['id'], 'e:' + second['id']], [payment['id']], project,
                      expected_versions=versions, revisions={payment['id']: 0 if failure == 'stale-queue' else payment['revision']})
    assert snapshots(matcher) == before


@pytest.mark.parametrize('failure', ['member-data', 'member-event', 'member-attachment', 'queue-data', 'aggregate-data'])
def test_multiple_undo_refuses_external_edits_and_never_overwrites_them(setup: Any, failure: str) -> None:
    matcher, project, _ = setup
    first = expense(matcher, project, 'Undo-protected first', '10')
    second = expense(matcher, project, 'Undo-protected second', '4')
    payment = queued(matcher, project, 'Undo-protected payment', 'payment', '14')
    result = matcher.match(['e:' + first['id'], 'e:' + second['id']], [payment['id']], project,
                           expected_versions={item['id']: item['version'] for item in (first, second)}, revisions=revisions(payment))
    with matcher.store.connect() as db:
        if failure in {'member-data', 'aggregate-data'}:
            key = first['id'] if failure == 'member-data' else result['item']['id']
            raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
            raw['note'] = 'Later human edit'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), key))
        elif failure == 'member-event':
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (first['id'], 'later', 'Later audit event'))
        elif failure == 'member-attachment':
            attachment = result['item']['attachments'][0]['id']
            db.execute('UPDATE attachments SET item_id=? WHERE id=?', (first['id'], attachment))
        else:
            queue = matcher._queue(db, payment['id'])
            queue['revision'] += 1
            queue['result'] = 'Later human material edit'
            matcher._write_queue(db, queue)
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], matcher.store.get(result['item']['id'])['version'])
    assert snapshots(matcher) == before


@pytest.mark.parametrize('duplicate', ['key', 'number', 'digest'])
def test_multiple_match_rejects_repeated_voucher_evidence_atomically(setup: Any, duplicate: str) -> None:
    matcher, project, _ = setup
    first = queued(matcher, project, 'Distinct binary first', 'invoice', '10')
    second = queued(matcher, project, 'Distinct binary second', 'invoice', '10')
    if duplicate == 'number':
        for entry in (first, second):
            entry['fields']['invoice_number'] = '12345678901234567890'
    elif duplicate == 'digest':
        second.update(path=first['path'], digest=first['digest'])
    with matcher.store.connect() as db:
        matcher._write_queue(db, first)
        matcher._write_queue(db, second)
    payment = queued(matcher, project, 'Repeated voucher payment', 'payment', '20')
    before = snapshots(matcher)
    keys = ['q:' + first['id'], 'q:' + (first if duplicate == 'key' else second)['id']]
    with pytest.raises(ValueError, match='重复'):
        matcher.match(keys, [payment['id']], project, revisions=revisions(first, second, payment))
    assert snapshots(matcher) == before


def test_multiple_unknown_amounts_require_individual_manual_confirmation(setup: Any) -> None:
    matcher, project, _ = setup
    first = queued(matcher, project, 'Unknown first amount', 'invoice', None)
    second = queued(matcher, project, 'Known second amount', 'receipt', '4')
    payment = queued(matcher, project, 'Unknown combined pay', 'payment', None)
    keys = ['q:' + first['id'], 'q:' + second['id']]
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='逐份'):
        matcher.match(keys, [payment['id']], project, revisions=revisions(first, second, payment), fields={'amount': '14'})
    assert snapshots(matcher) == before
    result = matcher.match(keys, [payment['id']], project, revisions=revisions(first, second, payment),
                           fields={'voucher_amounts': {first['id']: '10'}, 'payment_amounts': {payment['id']: '14'}})
    assert result['item']['amount_cents'] == 1400 and result['match']['voucher_count'] == 2


def test_single_array_and_version_map_keep_legacy_match_and_undo_contract(setup: Any) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Legacy array voucher', '14')
    payment = queued(matcher, project, 'Legacy array payment', 'payment', '14')
    result = matcher.match(['e:' + item['id']], [payment['id']], project,
                           expected_versions={item['id']: item['version']}, revisions=revisions(payment))
    assert result['item']['id'] == item['id'] and result['match']['schema_version'] == 1
    assert result['match']['voucher_keys'] == ['e:' + item['id']] and result['match']['voucher_count'] == 1
    restored = matcher.undo(result['match']['id'], result['item']['version'])
    assert restored['item']['id'] == item['id'] and restored['item']['stage'] == 'draft'
    assert len(active_items(matcher)) == 1 and not restored['item']['complete']


@pytest.mark.parametrize('vouchers,payments,groups', [(['10', '4'], ['14'], 1),
                                                    (['14', '16'], ['10', '20'], 1),
                                                    (['10', '4'], ['10', '4'], 2)])
def test_auto_supports_unique_multiple_groups_and_independent_single_pairs(setup: Any, vouchers: list[str],
                                                                          payments: list[str], groups: int) -> None:
    matcher, project, _ = setup
    for index, amount in enumerate(vouchers):
        auto_queued(matcher, project, f'Auto voucher {index}', 'invoice', amount)
    for index, amount in enumerate(payments):
        auto_queued(matcher, project, f'Auto grouped pay {index}', 'payment', amount)
    result = matcher.auto_match(project)
    assert result['matched'] == groups and result['unmatched'] == 0 and not result['conflicts']
    assert len(active_items(matcher)) == groups
    assert sum(item['amount_cents'] for item in active_items(matcher)) == sum(int(value) * 100 for value in vouchers)
    assert all(not item['verified'] and item['stage'] == 'draft' for item in active_items(matcher))
    assert matcher.auto_match(project)['matched'] == 0


@pytest.mark.parametrize('vouchers,payments', [(['10', '4', '6'], ['10']),
                                             (['10', '4'], ['14', '4']),
                                             (['10', '4'], ['14', '10', '4'])])
def test_auto_multiple_combination_ambiguity_or_shared_originals_remain_manual(setup: Any, vouchers: list[str], payments: list[str]) -> None:
    matcher, project, _ = setup
    for index, amount in enumerate(vouchers):
        auto_queued(matcher, project, f'Ambiguous multi voucher {index}', 'invoice', amount)
    for index, amount in enumerate(payments):
        auto_queued(matcher, project, f'Ambiguous multi pay {index}', 'payment', amount)
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and result['conflicts']
    assert snapshots(matcher) == before


def test_auto_refuses_to_truncate_oversized_voucher_pool(setup: Any) -> None:
    matcher, project, _ = setup
    for index in range(21):
        auto_queued(matcher, project, f'Oversized voucher {index}', 'invoice', str(index + 1))
    auto_queued(matcher, project, 'Unique-looking pool payment', 'payment', '1')
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and '20' in result['conflicts'][0]['reason']
    assert snapshots(matcher) == before


def test_multiple_existing_invoice_numbers_are_checked_from_original_pdfs(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    original = make_invoice_pdf(matcher.store.workspace / 'numbered-original.pdf', amount='10', invoice_number=number)
    items = []
    for index in range(2):
        item = matcher.store.create({'title': f'Numbered member {index}', 'category': '餐饮',
                                     'project_id': project, 'amount': '10', 'material_basis': 'invoice_payment'})
        with pymupdf.open(original) as document:
            document.set_metadata({'title': f'Synthetic binary {index}'})
            data = document.tobytes()
        items.append(matcher.store.add_attachment(item['id'], f'numbered-{index}.pdf', data, 'invoice'))
    payment = queued(matcher, project, 'Duplicate numbered member payment', 'payment', '20')
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='号码重复'):
        matcher.match(['e:' + item['id'] for item in items], [payment['id']], project,
                      expected_versions={item['id']: item['version'] for item in items}, revisions=revisions(payment))
    assert snapshots(matcher) == before


def test_multiple_transaction_failure_restores_moved_members_and_queue(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    first = expense(matcher, project, 'Atomic original ten', '10')
    second = expense(matcher, project, 'Atomic original four', '4')
    payment = queued(matcher, project, 'Atomic combined payment', 'payment', '14')
    before = snapshots(matcher)
    write = matcher._write_queue

    def fail_after_queue_write(db: Any, entry: dict[str, Any]) -> None:
        write(db, entry)
        raise RuntimeError('Synthetic transaction interruption')

    monkeypatch.setattr(matcher, '_write_queue', fail_after_queue_write)
    with pytest.raises(RuntimeError, match='interruption'):
        matcher.match(['e:' + first['id'], 'e:' + second['id']], [payment['id']], project,
                      expected_versions={item['id']: item['version'] for item in (first, second)}, revisions=revisions(payment))
    assert snapshots(matcher) == before
    assert {a['id'] for a in matcher.store.get(first['id'])['attachments']} == {a['id'] for a in first['attachments']}


def test_auto_refuses_an_excessive_candidate_space_without_partial_mutation(setup: Any) -> None:
    matcher, project, _ = setup
    for index in range(16):
        auto_queued(matcher, project, f'Excessive candidates voucher {index}', 'invoice', '1')
        auto_queued(matcher, project, f'Excessive candidates payment {index}', 'payment', '1')
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and '50000' in result['conflicts'][0]['reason']
    assert snapshots(matcher) == before


@pytest.mark.parametrize('multiple', [False, True])
@pytest.mark.parametrize('role', ['other', 'order', 'trip', 'detail', 'trip_sheet'])
def test_match_supports_unknown_amount_originals_without_counting_them(setup: Any, multiple: bool, role: str) -> None:
    matcher, project, _ = setup
    amounts = ['10', '4'] if multiple else ['14']
    vouchers = [queued(matcher, project, f'Supported voucher {index}', 'invoice', amount)
                for index, amount in enumerate(amounts)]
    payment = queued(matcher, project, 'Supported payment', 'payment', '14')
    support = queued(matcher, '', 'Original supplementary proof', role, None)
    original = Path(support['path']).read_bytes()
    assert support['id'] in [card['id'] for card in matcher.board(project)['other']]
    result = matcher.match(['q:' + entry['id'] for entry in vouchers], [payment['id']], project,
                           revisions=revisions(*vouchers, payment, support), support_ids=['q:' + support['id']])
    group, item = result['match'], result['item']
    support_attachment = next(attachment for attachment in item['attachments'] if attachment['name'] == support['name'])
    assert item['amount_cents'] == group['payment_total_cents'] == group['voucher_total_cents'] == 1400
    assert group['payment_count'] == 1 and group['support_count'] == 1
    assert group['support_ids'] == [support['id']] and group['support_attachment_ids'] == [support_attachment['id']]
    assert item['material_match']['support_ids'] == [support['id']]
    assert item['material_match']['support_attachment_ids'] == [support_attachment['id']]
    assert item['material_match']['support_count'] == 1
    assert item['complete'] and not item['verified'] and not item['ready'] and group['can_supplement']
    linked = matcher.intake.get(support['id'])
    assert linked['project_id'] == project and linked['status'] == 'linked' and linked['item_id'] == item['id']
    restored = matcher.undo(group['id'], item['version'])
    assert restored['match']['status'] == 'undone' and not restored['match']['can_supplement']
    assert 'material_match' not in restored['item']
    assert all(a['id'] != support_attachment['id'] for a in restored['item']['attachments'])
    released = matcher.intake.get(support['id'])
    assert released['project_id'] == '' and released['status'] == 'review' and not released['item_id']
    assert released['revision'] == support['revision'] + 2
    assert Path(support['path']).read_bytes() == original


@pytest.mark.parametrize('schema', ['single', 'legacy-single', 'multiple'])
def test_existing_group_supplement_preserves_bill_fields_and_remains_reversible(setup: Any, schema: str) -> None:
    matcher, project, _ = setup
    first = expense(matcher, project, 'Existing support target', '10' if schema == 'multiple' else '14')
    members = [first, expense(matcher, project, 'Second support target', '4')] if schema == 'multiple' else [first]
    payment = queued(matcher, project, 'Existing group payment', 'payment', '14')
    result = matcher.match(['e:' + member['id'] for member in members], [payment['id']], project,
                           expected_versions={member['id']: member['version'] for member in members}, revisions=revisions(payment))
    if schema == 'legacy-single':
        with matcher.store.connect() as db:
            group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (result['match']['id'],)).fetchone()['data'])
            for key in ('aggregate_after', 'queue_after', 'support_ids', 'support_count', 'support_attachment_ids'):
                group.pop(key, None)
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group), group['id']))
    support = queued(matcher, project, 'Waybill original', 'detail', None)
    before_item = result['item']
    amended = matcher.supplement(result['match']['id'], [support['id']], expected_version=before_item['version'], revisions=revisions(support))
    after = amended['item']
    for field in ('id', 'amount_cents', 'category', 'expense_date', 'merchant', 'verified', 'stage', 'payment_count'):
        if field in before_item:
            assert after[field] == before_item[field]
    assert len(after['attachments']) == len(before_item['attachments']) + 1 and not after['verified']
    assert amended['match']['support_ids'] == [support['id']] and amended['match']['support_count'] == 1
    assert amended['match']['version'] == after['version'] != before_item['version']
    second_support = queued(matcher, '', 'Supplementary order without monetary amount', 'order', None)
    amended = matcher.supplement(result['match']['id'], ['q:' + second_support['id']],
                                  expected_version=after['version'], revisions=revisions(second_support))
    assert amended['match']['support_count'] == 2
    assert len(amended['item']['material_match']['support_attachment_ids']) == 2
    restored = matcher.undo(result['match']['id'], amended['item']['version'])
    assert restored['match']['status'] == 'undone'
    assert len(active_items(matcher)) == len(members)
    assert sum(item['amount_cents'] for item in active_items(matcher)) == 1400
    for entry in (support, second_support, payment):
        current = matcher.intake.get(entry['id'])
        assert current['status'] == 'review' and not current['item_id'] and Path(entry['path']).is_file()
    if schema == 'multiple':
        assert restored['item']['stage'] == 'cancelled' and not restored['item']['attachments']
    else:
        assert restored['item']['stage'] == 'draft' and len(restored['item']['attachments']) == 1


@pytest.mark.parametrize('operation', ['match', 'supplement'])
@pytest.mark.parametrize('failure', ['cross-project', 'qr-manual', 'qr-converted', 'stale-revision', 'stale-version',
                                     'duplicate', 'invoice-role', 'receipt-role', 'payment-role',
                                     'missing-original', 'tampered-original', 'attached-digest'])
def test_invalid_supports_never_partially_write_a_group(setup: Any, operation: str, failure: str) -> None:
    matcher, project, other = setup
    item = expense(matcher, project, 'Protected support target', '14')
    payment = queued(matcher, project, 'Protected support payment', 'payment', '14')
    group = None
    if operation == 'supplement':
        grouped = matcher.match('e:' + item['id'], [payment['id']], project,
                                 expected_version=item['version'], revisions=revisions(payment))
        group, item = grouped['match'], grouped['item']
    support = queued(matcher, other if failure == 'cross-project' else project, 'Invalid support original', 'other', None)
    if failure.startswith('qr-'):
        support['qr_status'] = failure.removeprefix('qr-')
    if failure.endswith('-role'):
        support['role'] = failure.removesuffix('-role')
    with matcher.store.connect() as db:
        matcher._write_queue(db, support)
    if failure == 'missing-original':
        Path(support['path']).unlink()
    if failure == 'tampered-original':
        Path(support['path']).write_bytes(proof('Externally modified supplementary original'))
    if failure == 'attached-digest':
        other_item = expense(matcher, other, 'Owner of supplementary duplicate', '1')
        matcher.store.add_attachment(other_item['id'], support['name'], Path(support['path']).read_bytes(), 'other')
    support_ids = [support['id'], 'q:' + support['id']] if failure == 'duplicate' else [support['id']]
    revision_map = revisions(payment, support)
    if failure == 'stale-revision':
        revision_map[support['id']] -= 1
    version = 'outdated' if failure == 'stale-version' else item['version']
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        if operation == 'match':
            matcher.match('e:' + item['id'], [payment['id']], project, expected_version=version,
                          revisions=revision_map, support_ids=support_ids)
        else:
            assert group is not None
            matcher.supplement(group['id'], support_ids, expected_version=version, revisions=revision_map)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('state', ['verified', 'submitted', 'confirmed', 'undone'])
def test_existing_group_supplement_rejects_noneditable_states_and_updates_public_eligibility(setup: Any, state: str) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Closed supplementary group', '14')
    payment = queued(matcher, project, 'Closed supplementary payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project, expected_version=item['version'],
                           revisions=revisions(payment), provisional=state != 'confirmed')
    if state in {'verified', 'submitted'}:
        matcher.store.transition(item['id'], 'verify')
    if state == 'submitted':
        matcher.store.transition(item['id'], 'submitted')
    if state == 'undone':
        matcher.undo(result['match']['id'], result['item']['version'])
    support = queued(matcher, project, 'Closed group waybill', 'detail', None)
    current = matcher.store.get(item['id'])
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.supplement(result['match']['id'], [support['id']], expected_version=current['version'], revisions=revisions(support))
    assert snapshots(matcher) == before
    with matcher.store.connect() as db:
        group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (result['match']['id'],)).fetchone()['data'])
    assert not matcher._public_match(group)['can_supplement']


@pytest.mark.parametrize('multiple', [False, True])
@pytest.mark.parametrize('changed', ['expense', 'queue'])
def test_supplement_and_undo_do_not_adopt_later_manual_edits(setup: Any, multiple: bool, changed: str) -> None:
    matcher, project, _ = setup
    vouchers = [queued(matcher, project, 'Edit-protected voucher one', 'invoice', '10' if multiple else '14')]
    if multiple:
        vouchers.append(queued(matcher, project, 'Edit-protected voucher two', 'invoice', '4'))
    payment = queued(matcher, project, 'Edit-protected payment', 'payment', '14')
    support = queued(matcher, project, 'Edit-protected support', 'detail', None)
    result = matcher.match(['q:' + voucher['id'] for voucher in vouchers], [payment['id']], project,
                           revisions=revisions(*vouchers, payment, support), support_ids=[support['id']])
    with matcher.store.connect() as db:
        if changed == 'expense':
            raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (result['item']['id'],)).fetchone()['data'])
            raw['note'] = 'Later human annotation'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), raw['id']))
        else:
            linked = matcher._queue(db, support['id'])
            linked['revision'] += 1
            linked['result'] = 'Later manual material correction'
            matcher._write_queue(db, linked)
    extra = queued(matcher, project, 'Another waybill', 'detail', None)
    before = snapshots(matcher)
    version = matcher.store.get(result['item']['id'])['version']
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.supplement(result['match']['id'], [extra['id']], expected_version=version, revisions=revisions(extra))
    assert snapshots(matcher) == before
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], version)
    assert snapshots(matcher) == before


def test_supplement_transaction_failure_rolls_back_attachments_and_group_snapshots(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Atomic supplementary target', '14')
    payment = queued(matcher, project, 'Atomic supplementary payment', 'payment', '14')
    result = matcher.match('e:' + item['id'], [payment['id']], project,
                           expected_version=item['version'], revisions=revisions(payment))
    support = queued(matcher, project, 'Atomic supplementary original', 'detail', None)
    before = snapshots(matcher)
    write = matcher._write_queue

    def fail_after_write(db: Any, entry: dict[str, Any]) -> None:
        write(db, entry)
        raise RuntimeError('Synthetic supplement interruption')

    monkeypatch.setattr(matcher, '_write_queue', fail_after_write)
    with pytest.raises(RuntimeError, match='interruption'):
        matcher.supplement(result['match']['id'], [support['id']], expected_version=result['item']['version'], revisions=revisions(support))
    assert snapshots(matcher) == before and Path(support['path']).is_file()


def test_auto_never_selects_supplementary_originals_or_clears_manual_qr(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = auto_queued(matcher, project, 'Automatic invoice with manual support choice', 'invoice', '14')
    auto_queued(matcher, project, 'Automatic invoice payment', 'payment', '14')
    support = queued(matcher, project, 'Unselected waybill', 'detail', None)
    qr = queued(matcher, project, 'Unclaimed QR original', 'other', None, qr_status='manual', issues=['请人工领票'])
    result = matcher.auto_match(project)
    assert result['matched'] == 1 and result['items'][0]['match']['support_count'] == 0
    assert result['items'][0]['match']['voucher_keys'] == ['q:' + voucher['id']]
    assert matcher.intake.get(support['id'])['status'] == 'review' and not matcher.intake.get(support['id'])['item_id']
    assert matcher.intake.get(qr['id'])['issues'] == ['请人工领票']
    assert {support['id'], qr['id']} <= {card['id'] for card in matcher.board(project)['other']}


def numbered_group(matcher: MaterialMatching, project: str, name: str, number: str) -> dict[str, Any]:
    item = expense(matcher, project, name)
    with matcher.store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        raw['invoice_number'] = number
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), item['id']))
    item = matcher.store.get(item['id'])
    payment = queued(matcher, project, name + ' payment', 'payment', '14')
    return matcher.match('e:' + item['id'], [payment['id']], project,
                         expected_version=item['version'], revisions=revisions(payment))


def numbered_support(matcher: MaterialMatching, project: str, name: str, number: str) -> dict[str, Any]:
    support = queued(matcher, project, name, 'detail', None, issues=['Amount requires manual review'])
    support['fields']['invoice_number'] = number
    with matcher.store.connect() as db:
        matcher._write_queue(db, support)
    return support


def test_exact_number_candidate_is_readonly_and_reuses_existing_supplement_without_new_amount(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    result = numbered_group(matcher, project, 'Already paired fourteen', number)
    support = numbered_support(matcher, project, 'Waybill with no recognized amount', number)
    before = snapshots(matcher)
    candidates = matcher.board(project)['supplement_candidates']
    assert len(candidates) == 1 and snapshots(matcher) == before
    candidate = candidates[0]
    assert candidate['key'] == 'q:' + support['id'] and candidate['support_id'] == support['id']
    assert candidate['support_ids'] == [support['id']] and candidate['revisions'] == revisions(support)
    assert candidate['group_id'] == result['match']['id'] and candidate['item_id'] == result['item']['id']
    assert candidate['expected_version'] == candidate['expense_version'] == result['item']['version']
    assert candidate['can_supplement'] and candidate['number_source'] == 'fields'
    assert str(matcher.store.workspace) not in json.dumps(candidate)
    assert matcher.intake.get(support['id'])['issues'] == support['issues']
    amended = matcher.supplement(candidate['group_id'], candidate['support_ids'],
                                  expected_version=candidate['expected_version'], revisions=candidate['revisions'])
    assert amended['item']['amount_cents'] == 1400 and not amended['item']['verified']
    assert len(matcher.store.list_items()) == 1 and not matcher.board(project)['supplement_candidates']
    assert len(amended['match']['support_attachment_ids']) == 1


def test_exact_number_candidate_retains_all_multi_voucher_numbers(setup: Any) -> None:
    matcher, project, _ = setup
    numbers = ['12345678901234567890', '12345678901234567891']
    vouchers = [queued(matcher, project, 'Candidate ten invoice', 'invoice', '10'),
                queued(matcher, project, 'Candidate four invoice', 'invoice', '4')]
    with matcher.store.connect() as db:
        for entry, number in zip(vouchers, numbers):
            entry['fields']['invoice_number'] = number
            matcher._write_queue(db, entry)
    payment = queued(matcher, project, 'Candidate aggregate payment', 'payment', '14')
    result = matcher.match(['q:' + entry['id'] for entry in vouchers], [payment['id']], project,
                           revisions=revisions(*vouchers, payment))
    supports = [numbered_support(matcher, project, 'Details for voucher ' + str(index), number)
                for index, number in enumerate(numbers)]
    before = snapshots(matcher)
    candidates = matcher.board(project)['supplement_candidates']
    assert {candidate['invoice_number'] for candidate in candidates} == set(numbers)
    assert {candidate['support_id'] for candidate in candidates} == {entry['id'] for entry in supports}
    assert {candidate['group_id'] for candidate in candidates} == {result['match']['id']}
    assert snapshots(matcher) == before


def test_ambiguous_same_invoice_number_groups_have_no_supplement_suggestion(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    numbered_group(matcher, project, 'Ambiguous first legacy group', number)
    numbered_group(matcher, project, 'Ambiguous second legacy group', number)
    numbered_support(matcher, project, 'Ambiguous exact-number detail', number)
    before = snapshots(matcher)
    assert not matcher.board(project)['supplement_candidates']
    assert snapshots(matcher) == before


@pytest.mark.parametrize('condition', ['cross-project', 'unassigned', 'linked', 'qr-manual', 'qr-converted',
                                     'payment', 'invoice', 'purchase', 'same-amount-only', 'tamper', 'missing', 'outside'])
def test_ineligible_supplement_sources_do_not_produce_candidates(setup: Any, condition: str, tmp_path: Path) -> None:
    matcher, project, other = setup
    number = '12345678901234567890'
    result = numbered_group(matcher, project, 'Eligible paired group', number)
    support = numbered_support(matcher, other if condition == 'cross-project' else '' if condition == 'unassigned' else project,
                               'Ineligible supplemental source', number)
    if condition == 'linked':
        support.update(status='linked', item_id=result['item']['id'])
    if condition.startswith('qr-'):
        support['qr_status'] = condition.removeprefix('qr-')
    if condition in {'payment', 'invoice', 'purchase'}:
        support['role'] = condition
    if condition == 'same-amount-only':
        support['fields'] = {'amount': '14'}
    if condition == 'tamper':
        Path(support['path']).write_bytes(proof('Changed detail original'))
    if condition == 'missing':
        Path(support['path']).unlink()
    if condition == 'outside':
        external = tmp_path.parent / (tmp_path.name + '-outside.pdf')
        external.write_bytes(proof('External detail original'))
        support.update(path=str(external), digest=hashlib.sha256(external.read_bytes()).hexdigest())
    with matcher.store.connect() as db:
        matcher._write_queue(db, support)
    before = snapshots(matcher)
    assert not matcher.board('')['supplement_candidates']
    assert snapshots(matcher) == before
    if condition == 'outside':
        external.unlink()


@pytest.mark.parametrize('condition', ['verified', 'submitted', 'confirmed', 'edited', 'tampered-original'])
def test_supplement_candidates_require_current_editable_group_snapshot(setup: Any, condition: str) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    result = numbered_group(matcher, project, 'Protected paired group', number)
    numbered_support(matcher, project, 'Protected group supplementary detail', number)
    with matcher.store.connect() as db:
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (result['item']['id'],)).fetchone()['data'])
        if condition == 'verified':
            raw['verified'] = True
        elif condition == 'submitted':
            raw['stage'] = 'submitted'
        elif condition == 'edited':
            raw['note'] = 'Later manual edit'
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw), result['item']['id']))
        if condition == 'confirmed':
            group = json.loads(db.execute('SELECT data FROM material_matches WHERE id=?', (result['match']['id'],)).fetchone()['data'])
            group.update(status='confirmed', provisional=False)
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group), result['match']['id']))
    if condition == 'tampered-original':
        path, _ = matcher.store.attachment_path(result['item']['attachments'][0]['id'])
        path.write_bytes(proof('Tampered paired original'))
    before = snapshots(matcher)
    assert not matcher.board(project)['supplement_candidates']
    assert snapshots(matcher) == before


def test_pdf_text_number_suggests_legacy_unrecognized_detail_without_mutating_issues(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    source = make_invoice_pdf(matcher.store.workspace / 'synthetic-numbered-source.pdf', amount='14', invoice_number=number)
    entries = []
    for role in ['invoice', 'detail']:
        with pymupdf.open(source) as document:
            document.set_metadata({'title': role + ' source binary'})
            entry = matcher.intake.receive(role + '-legacy.pdf', document.tobytes(), project_id=project)
        entry.update(role=role, status='review', revision=1,
                     fields={'amount': '14'} if role == 'invoice' else {},
                     issues=[] if role == 'invoice' else ['OCR amount conflict'])
        with matcher.store.connect() as db:
            matcher._write_queue(db, entry)
        entries.append(entry)
    payment = queued(matcher, project, 'Legacy vector invoice payment', 'payment', '14')
    result = matcher.match('q:' + entries[0]['id'], [payment['id']], project,
                           revisions=revisions(entries[0], payment))
    before = snapshots(matcher)
    candidates = matcher.board(project)['supplement_candidates']
    assert len(candidates) == 1 and candidates[0]['invoice_number'] == number
    assert candidates[0]['group_id'] == result['match']['id'] and candidates[0]['number_source'] == 'pdf_text'
    assert snapshots(matcher) == before
    assert matcher.intake.get(entries[1]['id'])['fields'] == {} and matcher.intake.get(entries[1]['id'])['issues'] == ['OCR amount conflict']


def test_supplement_candidates_do_not_guess_number_from_filename_or_after_page_limit(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    numbered_group(matcher, project, 'Paired exact-number target', number)
    filename_only = matcher.intake.receive(number + '.pdf', proof('No invoice number in PDF text'), project_id=project)
    with pymupdf.open() as document:
        for index in range(11):
            document.new_page().insert_text((20, 30), 'Invoice No: ' + number if index == 10 else 'Unnumbered detail page')
        late_page = matcher.intake.receive('late-page-number.pdf', document.tobytes(), project_id=project)
    with matcher.store.connect() as db:
        for entry in [filename_only, late_page]:
            entry.update(role='detail', status='review', revision=1, fields={})
            matcher._write_queue(db, entry)
    before = snapshots(matcher)
    assert not matcher.board(project)['supplement_candidates']
    assert snapshots(matcher) == before


def test_board_reads_old_native_seller_without_changing_queue_amount_or_original(setup: Any) -> None:
    matcher, project, _ = setup
    entry = native_seller_invoice(matcher, project, '南昌青禾测试物料有限公司')
    content = Path(entry['path']).read_bytes()
    before = snapshots(matcher)
    card = matcher.board(project)['vouchers'][0]
    assert card['fields']['merchant'] == '南昌青禾测试物料有限公司'
    assert card['amount_cents'] == 10000 and card['revision'] == 1
    assert matcher.intake.get(entry['id'])['fields'] == {'amount': '100', 'category': '其他'}
    assert snapshots(matcher) == before and Path(entry['path']).read_bytes() == content


def test_auto_native_seller_pair_overrides_unrelated_rail_sums_and_retains_merchant(setup: Any) -> None:
    matcher, project, _ = setup
    invoice = native_seller_invoice(matcher, project, '南昌青禾测试物料有限公司')
    other = merchant(matcher, queued(matcher, project, 'Unrelated invoice', 'invoice', '2706'),
                     '合成铁路票务有限公司')
    payment = merchant(matcher, queued(matcher, project, 'Printing payment', 'payment', '100'),
                       '青禾测试物料有限公司 开票100元')
    rail = [merchant(matcher, queued(matcher, project, f'Rail {index}', 'payment', amount),
                     '合成铁路票务有限公司') for index, amount in enumerate(('1842', '714', '50', '50'))]
    other_before = matcher.intake.get(other['id'])
    rail_before = [matcher.intake.get(entry['id']) for entry in rail]
    result = matcher.auto_match(project)
    assert result['matched'] == 1 and result['unmatched'] == 1
    item = result['items'][0]['item']
    assert item['amount_cents'] == 10000 and item['merchant'] == '南昌青禾测试物料有限公司'
    assert item['stage'] == 'draft' and not item['verified'] and item['pending_review']
    assert result['items'][0]['match']['payment_ids'] == [payment['id']]
    assert result['items'][0]['match']['voucher_keys'] == ['q:' + invoice['id']]
    assert matcher.intake.get(other['id']) == other_before
    assert [matcher.intake.get(entry['id']) for entry in rail] == rail_before
    before = snapshots(matcher)
    assert matcher.auto_match(project)['matched'] == 0 and snapshots(matcher) == before
    restored = matcher.undo(result['items'][0]['match']['id'], item['version'])
    assert restored['item']['stage'] == 'draft' and len(active_items(matcher)) == 1
    assert restored['item']['merchant'] == item['merchant'] and restored['match']['status'] == 'undone'
    assert matcher.intake.get(payment['id'])['status'] == 'review'
    assert matcher.intake.get(invoice['id'])['fields'] == invoice['fields']


@pytest.mark.parametrize('payment_name', [
    '南昌青禾测试物料有限公司', '南昌市青禾测试物料有限公司',
    '青禾测试物料有限公司 开票100元', '青禾测试物料有限公司 开票￥100.00元',
])
def test_auto_company_identity_accepts_only_explicit_supported_display_variations(setup: Any,
                                                                               payment_name: str) -> None:
    matcher, project, _ = setup
    merchant(matcher, queued(matcher, project, 'Invoice 100', 'invoice', '100'), '青禾测试物料有限公司')
    payment = merchant(matcher, queued(matcher, project, 'Payment 100', 'payment', '100'), payment_name)
    merchant(matcher, queued(matcher, project, 'Noise 25a', 'payment', '50'), '合成铁路票务有限公司')
    merchant(matcher, queued(matcher, project, 'Noise 25b', 'payment', '50'), '合成铁路票务有限公司')
    result = matcher.auto_match(project)
    assert result['matched'] == 1 and result['items'][0]['match']['payment_ids'] == [payment['id']]
    assert result['items'][0]['item']['amount_cents'] == 10000 and not result['items'][0]['item']['verified']


@pytest.mark.parametrize('payment_name', [
    '青禾测试', '测试物料有限公司', '杭州青禾测试物料有限公司',
    '青禾测试物料有限公司 开票98元', '青禾测试物料有限责任公司',
    '微信支付青禾测试物料有限公司', '银行青禾测试物料有限公司',
    '青禾测试物料有限公司 其他用途', '青禾测试物料有限公司 开票100.001元',
])
def test_auto_identity_does_not_guess_short_names_other_regions_platforms_or_annotations(setup: Any,
                                                                                      payment_name: str) -> None:
    matcher, project, _ = setup
    merchant(matcher, queued(matcher, project, 'Invoice 100', 'invoice', '100'), '青禾测试物料有限公司')
    merchant(matcher, queued(matcher, project, 'Payment 100', 'payment', '100'), payment_name)
    merchant(matcher, queued(matcher, project, 'Noise 25a', 'payment', '50'), '合成铁路票务有限公司')
    merchant(matcher, queued(matcher, project, 'Noise 25b', 'payment', '50'), '合成铁路票务有限公司')
    before = snapshots(matcher)
    assert matcher.auto_match(project)['matched'] == 0 and snapshots(matcher) == before


@pytest.mark.parametrize('competition', ['payment-duplicate', 'voucher-duplicate', 'split-payments', 'cross-total'])
def test_auto_identity_competition_is_reserved_before_amount_fallback(setup: Any, competition: str) -> None:
    matcher, project, _ = setup
    name = '青禾测试物料有限公司'
    merchant(matcher, queued(matcher, project, 'Invoice 100', 'invoice', '100'), name)
    merchant(matcher, queued(matcher, project, 'Payment 100', 'payment', '100'), name)
    if competition == 'payment-duplicate':
        merchant(matcher, queued(matcher, project, 'Payment duplicate', 'payment', '100'), name)
    elif competition == 'voucher-duplicate':
        merchant(matcher, queued(matcher, project, 'Invoice duplicate', 'invoice', '100'), name)
    elif competition == 'split-payments':
        merchant(matcher, queued(matcher, project, 'Split 25a', 'payment', '50'), name)
        merchant(matcher, queued(matcher, project, 'Split 25b', 'payment', '50'), name)
        queued(matcher, project, 'Unrelated invoice 50', 'invoice', '50')
    else:
        merchant(matcher, queued(matcher, project, 'Additional invoice 10', 'invoice', '10'), name)
        merchant(matcher, queued(matcher, project, 'Combined payment 110', 'payment', '110'), name)
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and any('同商家' in conflict['reason'] for conflict in result['conflicts'])
    assert snapshots(matcher) == before


def test_auto_identity_independent_pairs_remain_independent(setup: Any) -> None:
    matcher, project, _ = setup
    for index, amount in enumerate(('100', '20')):
        merchant(matcher, queued(matcher, project, f'Invoice {index}', 'invoice', amount), '青禾测试物料有限公司')
        merchant(matcher, queued(matcher, project, f'Payment {index}', 'payment', amount), '青禾测试物料有限公司')
    merchant(matcher, queued(matcher, project, 'Other pay 25a', 'payment', '50'), '合成铁路票务有限公司')
    merchant(matcher, queued(matcher, project, 'Other pay 25b', 'payment', '50'), '合成铁路票务有限公司')
    result = matcher.auto_match(project)
    assert result['matched'] == 2 and result['unmatched'] == 0 and not result['conflicts']
    assert sorted(item['amount_cents'] for item in active_items(matcher)) == [2000, 10000]


def test_auto_failed_identity_write_is_not_retried_as_amount_match(setup: Any,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    for role in ('invoice', 'payment'):
        merchant(matcher, queued(matcher, project, role, role, '100'), '青禾测试物料有限公司')
    calls = []

    def fail(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise ValueError('Synthetic stale material')

    monkeypatch.setattr(matcher, 'match', fail)
    before = snapshots(matcher)
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and len(calls) == 1
    assert result['conflicts'][0]['reason'] == 'Synthetic stale material' and snapshots(matcher) == before


def test_native_seller_conflict_preserves_manual_value_and_blocks_all_automatic_matching(setup: Any) -> None:
    matcher, project, _ = setup
    invoice = native_seller_invoice(matcher, project, '南昌青禾测试物料有限公司')
    merchant(matcher, invoice, '人工修正广告有限公司')
    merchant(matcher, queued(matcher, project, 'Payment 100', 'payment', '100'), '人工修正广告有限公司')
    before = snapshots(matcher)
    card = matcher.board(project)['vouchers'][0]
    assert card['fields']['merchant'] == '人工修正广告有限公司'
    assert any('商家与原生销售方不一致' in issue for issue in card['issues'])
    result = matcher.auto_match(project)
    assert result['matched'] == 0 and any('商家与原生销售方不一致' in c['reason'] for c in result['conflicts'])
    assert snapshots(matcher) == before


def test_identical_noncompany_native_seller_retains_existing_unique_amount_matching(setup: Any) -> None:
    matcher, project, _ = setup
    invoice = native_seller_invoice(matcher, project, '合成小吃店')
    merchant(matcher, invoice, '合成 小吃店')
    merchant(matcher, queued(matcher, project, 'Store payment', 'payment', '100'), '合成小吃店')
    card = matcher.board(project)['vouchers'][0]
    assert card['fields']['merchant'] == '合成 小吃店'
    assert not any('商家与原生销售方不一致' in issue for issue in card['issues'])
    result = matcher.auto_match(project)
    assert result['matched'] == 1 and result['items'][0]['item']['merchant'] == '合成 小吃店'
    assert result['items'][0]['item']['amount_cents'] == 10000 and not result['items'][0]['item']['verified']


@pytest.mark.parametrize('invalid', ['changed-digest', 'outside-workspace', 'receipt-role'])
def test_native_seller_enrichment_does_not_read_untrusted_originals(setup: Any, invalid: str,
                                                                  tmp_path: Path) -> None:
    matcher, project, _ = setup
    entry = native_seller_invoice(matcher, project, '南昌青禾测试物料有限公司')
    if invalid == 'changed-digest':
        entry['digest'] = '0' * 64
    elif invalid == 'outside-workspace':
        entry['path'] = str(tmp_path.parent / 'not-a-workspace-invoice.pdf')
    else:
        entry['role'] = 'receipt'
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    before = snapshots(matcher)
    card = matcher.board(project)['vouchers'][0]
    assert not card['fields'].get('merchant') and snapshots(matcher) == before


def test_auto_never_uses_company_names_from_filenames_or_dates(setup: Any) -> None:
    matcher, project, _ = setup
    queued(matcher, project, '青禾测试物料有限公司-2026-09-30-invoice', 'invoice', '100')
    merchant(matcher, queued(matcher, project, 'Payment', 'payment', '100'), '青禾测试物料有限公司')
    merchant(matcher, queued(matcher, project, 'Other 25a', 'payment', '50'), '合成铁路票务有限公司')
    merchant(matcher, queued(matcher, project, 'Other 25b', 'payment', '50'), '合成铁路票务有限公司')
    before = snapshots(matcher)
    assert matcher.auto_match(project)['matched'] == 0 and snapshots(matcher) == before
