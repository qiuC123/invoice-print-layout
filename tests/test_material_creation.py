from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from tests.helpers import make_invoice_pdf
from tests.test_material_matching import active_items, expense, proof, queued, revisions, setup, snapshots


def confirmed_creation(matcher: Any, *args: Any, fields: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Supply explicit user confirmation for transaction-focused regressions."""
    confirmed: dict[str, Any] = {'project_type': 'other', 'project_name': 'Confirmed expense', 'amount': '14'}
    values = fields or {}
    confirmed.update(values)
    if values.get('title'):
        confirmed['project_name'] = values['title']
    if values.get('category') and len(str(values['category'])) > 200:
        confirmed['project_name'] = values['category']
    return matcher.create_expense(*args, fields=confirmed, **kwargs)


def selection(entry: dict[str, Any]) -> dict[str, Any]:
    role = entry['role']
    return {'voucher_keys': ['q:' + entry['id']] if role in {'invoice', 'receipt'} else [],
            'payment_ids': [entry['id']] if role == 'payment' else [],
            'support_ids': [entry['id']] if role not in {'invoice', 'receipt', 'payment'} else []}


@pytest.mark.parametrize('role', ['invoice', 'receipt', 'payment', 'other', 'order', 'detail', 'purchase'])
@pytest.mark.parametrize('identified', [True, False])
def test_one_original_can_register_after_three_fields_are_confirmed(setup: Any, role: str, identified: bool) -> None:
    matcher, project, _ = setup
    entry = queued(matcher, '', f'Single original {role}', role, '14' if identified else None)
    original = Path(entry['path']).read_bytes()
    result = confirmed_creation(matcher, **selection(entry), project_id=project, revisions=revisions(entry))
    item, group = result['item'], result['match']
    assert result['created'] and len(active_items(matcher)) == 1
    assert item['title'] == 'Confirmed expense' and item['category'] == '其他'
    assert group['recognition']['title'] == entry['fields']['title']
    assert not item['amount_pending']
    assert item['amount'] == '14.00'
    assert item['amount_cents'] == 1400
    assert group['recognition']['amount_cents'] == (1400 if identified else None)
    assert item['material_basis'] == 'invoice_payment' and item['stage'] == 'draft'
    assert not item['verified'] and not item['ready'] and not item['complete']
    assert group['mode'] == 'registration' and group['schema_version'] == 3
    assert group['voucher_count'] == int(role in {'invoice', 'receipt'})
    assert group['payment_count'] == int(role == 'payment')
    assert group['support_count'] == int(role not in {'invoice', 'receipt', 'payment'})
    assert group['voucher_keys'] == selection(entry)['voucher_keys']
    assert not group['amount_pending']
    assert '金额待确认' not in item['missing']
    if role not in {'invoice', 'receipt', 'payment'}:
        assert group['support_attachment_ids'] == [item['attachments'][0]['id']]
        assert item['material_match']['support_attachment_ids'] == group['support_attachment_ids']
    assert matcher.intake.get(entry['id'])['project_id'] == project
    assert str(matcher.store.workspace) not in json.dumps(result)
    restored = matcher.undo(group['id'], item['version'])
    assert restored['item']['stage'] == 'cancelled' and not restored['item']['attachments']
    assert not active_items(matcher)
    released = matcher.intake.get(entry['id'])
    assert released['status'] == 'review' and not released['item_id'] and released['project_id'] == ''
    assert Path(entry['path']).read_bytes() == original
    repeated = confirmed_creation(matcher, **selection(released), project_id=project, revisions=revisions(released))
    assert repeated['item']['id'] != item['id'] and len(active_items(matcher)) == 1


def test_confirmed_report_choice_preserves_machine_title_and_category_evidence(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Filename fallback', 'invoice', '14')
    voucher['fields'].update(title='', category='机器识别自定义类别', expense_date='2026-01-02')
    with matcher.store.connect() as db:
        matcher._write_queue(db, voucher)
    result = confirmed_creation(matcher, ['q:' + voucher['id']], [], project, revisions=revisions(voucher),
                                    fields={'project_type': 'other', 'project_name': 'Filename fallback', 'amount': '14'})
    assert result['item']['title'] == 'Filename fallback'
    assert result['item']['category'] == '其他' and result['item']['amount_cents'] == 1400
    assert result['match']['recognition']['category'] == '机器识别自定义类别'
    released = matcher.undo(result['match']['id'], result['item']['version'])
    voucher = matcher.intake.get(voucher['id'])
    manual = confirmed_creation(matcher, ['q:' + voucher['id']], [], project, revisions=revisions(voucher),
                                    fields={'project_type': 'materials', 'project_name': '  手工事项名称  ', 'amount': '17.25', 'category': '  忽略自由类别  ',
                                            'verified': True, 'material_basis': 'payment_only', 'project_id': 'ignored'})
    assert manual['item']['title'] == '手工事项名称' and manual['item']['category'] == '材料采购'
    assert manual['item']['amount_cents'] == 1725 and not manual['item']['amount_pending']
    assert manual['match']['alignment_status'] == 'amount_override'
    assert manual['match']['recognition']['amount_cents'] == 1400
    assert manual['match']['recognition']['amount_difference_cents'] == 325
    assert manual['item']['material_basis'] == 'invoice_payment' and not manual['item']['verified']
    assert released['item']['stage'] == 'cancelled' and len(active_items(matcher)) == 1


@pytest.mark.parametrize('manual', [None, '20'])
def test_mismatched_voucher_and_payment_create_unverified_draft_without_double_amount(setup: Any, manual: str | None) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Thirty invoice', 'invoice', '30')
    payment = queued(matcher, project, 'Fourteen payment', 'payment', '14')
    result = confirmed_creation(matcher, ['q:' + voucher['id']], [payment['id']], project,
                                    revisions=revisions(voucher, payment), fields={'amount': '30' if manual is None else manual})
    assert result['item']['amount_cents'] == (3000 if manual is None else 2000)
    assert result['match']['alignment_status'] == 'mismatch'
    assert result['match']['recognition']['difference_cents'] == -1600
    assert result['match']['voucher_total_cents'] == 3000 and result['match']['payment_total_cents'] == 1400
    assert not result['item']['verified'] and not result['item']['ready']
    assert result['item']['material_match']['recognition'] == result['match']['recognition']
    assert '已对应' not in result['match']['reason']


@pytest.mark.parametrize('voucher_amount,payment_amount,pending,status', [(None, '14', False, 'unconfirmed'),
                                                                      ('14', None, False, 'unconfirmed'),
                                                                      (None, None, True, 'unconfirmed'),
                                                                      ('14', '14', False, 'balanced')])
def test_machine_amount_suggestion_is_distinct_from_explicitly_confirmed_amount(setup: Any, voucher_amount: str | None,
                                                                           payment_amount: str | None, pending: bool, status: str) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Precedence invoice', 'invoice', voucher_amount)
    payment = queued(matcher, project, 'Precedence payment', 'payment', payment_amount)
    result = confirmed_creation(matcher, ['q:' + voucher['id']], [payment['id']], project, revisions=revisions(voucher, payment))
    assert not result['item']['amount_pending'] and result['match']['alignment_status'] == status
    assert result['item']['amount_cents'] == 1400
    assert result['match']['recognition']['amount_cents'] == (None if pending else 1400)
    assert not result['item']['verified'] and not result['item']['ready']
    assert '金额待确认' not in result['item']['missing']


def test_multiple_support_amounts_are_not_summed_and_manual_zero_is_explicit(setup: Any) -> None:
    matcher, project, _ = setup
    supports = [queued(matcher, project, 'Support ten', 'purchase', '10'), queued(matcher, project, 'Support four', 'detail', '4')]
    result = confirmed_creation(matcher, [], [], project, support_ids=[entry['id'] for entry in supports], revisions=revisions(*supports))
    assert not result['item']['amount_pending'] and result['item']['amount'] == '14.00'
    assert result['match']['voucher_count'] == result['match']['payment_count'] == 0
    assert result['match']['support_count'] == 2 and result['match']['recognition']['amount_cents'] is None
    matcher.undo(result['match']['id'], result['item']['version'])
    supports = [matcher.intake.get(entry['id']) for entry in supports]
    explicit = confirmed_creation(matcher, [], [], project, support_ids=[entry['id'] for entry in supports],
                                      revisions=revisions(*supports), fields={'amount': '0'})
    assert explicit['item']['amount_cents'] == 0 and not explicit['item']['amount_pending']
    assert explicit['item']['amount'] == '0.00' and not explicit['item']['verified']


@pytest.mark.parametrize('role', ['invoice', 'payment'])
def test_support_amount_never_substitutes_an_unknown_financial_proof(setup: Any, role: str) -> None:
    matcher, project, _ = setup
    financial = queued(matcher, project, 'Unknown financial original', role, None)
    support = queued(matcher, project, 'Recognized supporting amount', 'detail', '14')
    selected = selection(financial)
    selected['support_ids'] = [support['id']]
    result = confirmed_creation(matcher, **selected, project_id=project, revisions=revisions(financial, support))
    assert not result['item']['amount_pending'] and result['item']['amount'] == '14.00'
    assert result['match']['recognition']['amount_cents'] is None
    assert result['match']['recognition']['amount_source'] == 'unknown'
    assert result['match']['recognition']['supports'][0]['amount_cents'] == 1400
    assert not result['item']['complete'] and not result['item']['verified']


@pytest.mark.parametrize('payments', [False, True])
def test_single_existing_expense_reuses_id_and_undo_restores_all_original_fields(setup: Any, payments: bool) -> None:
    matcher, project, _ = setup
    item = expense(matcher, project, 'Original existing cost', '14')
    support = queued(matcher, project, 'Existing supplemental purchase', 'purchase', None)
    entries = [support]
    payment_ids = []
    if payments:
        payment = queued(matcher, project, 'Existing replacement payment', 'payment', '14')
        entries.append(payment)
        payment_ids.append(payment['id'])
    with matcher.store.connect() as db:
        original = matcher._member_state(db, item['id'])
    result = confirmed_creation(matcher, ['e:' + item['id']], payment_ids, project, support_ids=[support['id']],
                                    expected_versions={item['id']: item['version']}, revisions=revisions(*entries),
                                    fields={'title': 'Renamed existing cost', 'category': '用户运输类别', 'amount': '20'})
    assert not result['created'] and result['item']['id'] == item['id']
    assert len(matcher.store.list_items()) == len(active_items(matcher)) == 1
    assert result['item']['amount_cents'] == 2000 and result['item']['title'] == 'Renamed existing cost'
    assert result['match']['recognition']['amount_cents'] == 1400
    extra = queued(matcher, project, 'Additional ordinary detail', 'detail', None)
    result = matcher.supplement(result['match']['id'], [extra['id']], expected_version=result['item']['version'], revisions=revisions(extra))
    assert result['match']['support_count'] == 2
    matcher.undo(result['match']['id'], result['item']['version'])
    with matcher.store.connect() as db:
        restored = matcher._member_state(db, item['id'])
    assert restored['data'] == {**original['data'], 'returned_to_synthesis': True}
    assert restored['attachments'] == original['attachments']
    assert len(active_items(matcher)) == 1 and active_items(matcher)[0]['amount_cents'] == 1400
    assert all(not matcher.intake.get(entry['id'])['item_id'] for entry in [*entries, extra])


@pytest.mark.parametrize('add_queue', [False, True])
def test_multiple_existing_expenses_create_one_subject_and_undo_all_members(setup: Any, add_queue: bool) -> None:
    matcher, project, _ = setup
    items = [expense(matcher, project, 'Existing aggregate ten', '10'), expense(matcher, project, 'Existing aggregate four', '4')]
    queue_vouchers = [queued(matcher, project, 'Additional invoice two', 'invoice', '2')] if add_queue else []
    support = queued(matcher, project, 'Aggregate purchase proof', 'purchase', None)
    entries = [*queue_vouchers, support]
    keys = [*['e:' + item['id'] for item in items], *['q:' + entry['id'] for entry in queue_vouchers]]
    with matcher.store.connect() as db:
        before_members = {item['id']: matcher._member_state(db, item['id']) for item in items}
    result = confirmed_creation(matcher, keys, [], project, support_ids=[support['id']], revisions=revisions(*entries),
                                    expected_versions={item['id']: item['version'] for item in items},
                                    fields={'amount': '16' if add_queue else '14'})
    assert result['created'] and len(active_items(matcher)) == 1
    assert result['item']['amount_cents'] == (1600 if add_queue else 1400)
    assert not result['item']['complete'] and '付款截图' in result['item']['missing']
    assert len(result['item']['material_members']) == (3 if add_queue else 2)
    for item in items:
        current = matcher.store.get(item['id'])
        assert current['stage'] == 'cancelled' and current['amount_cents'] == item['amount_cents']
        assert current['merged_into'] == result['item']['id'] and not current['attachments']
    matcher.undo(result['match']['id'], result['item']['version'])
    assert len(active_items(matcher)) == 2 and sum(item['amount_cents'] for item in active_items(matcher)) == 1400
    with matcher.store.connect() as db:
        for item in items:
            restored = matcher._member_state(db, item['id'])
            assert restored['data'] == {**before_members[item['id']]['data'], 'returned_to_synthesis': True}
            assert restored['attachments'] == before_members[item['id']]['attachments']
    assert all(not matcher.intake.get(entry['id'])['item_id'] for entry in entries)


def test_partial_registration_is_not_offered_as_an_unmatched_expense_again(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Registered partial invoice', 'invoice', '14')
    result = confirmed_creation(matcher, ['q:' + voucher['id']], [], project, revisions=revisions(voucher))
    queued(matcher, project, 'Unselected fourteen payment', 'payment', '14')
    board = matcher.board(project)
    assert not board['vouchers'] and len(board['matches']) == 1
    assert matcher.auto_match(project)['matched'] == 0
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        confirmed_creation(matcher, ['e:' + result['item']['id']], [], project,
                               expected_versions={result['item']['id']: result['item']['version']})
    assert snapshots(matcher) == before


@pytest.mark.parametrize('failure', ['empty', 'duplicate', 'cross-project', 'revision', 'qr-manual', 'qr-converted',
                                     'role', 'tamper', 'missing', 'bad-amount', 'category-long'])
def test_creation_rejection_rolls_back_every_table(setup: Any, failure: str) -> None:
    matcher, project, other = setup
    support = queued(matcher, other if failure == 'cross-project' else project, 'Protected registration original', 'other', None)
    if failure.startswith('qr-'):
        support['qr_status'] = failure.removeprefix('qr-')
    if failure == 'role':
        support['role'] = 'payment'
    with matcher.store.connect() as db:
        matcher._write_queue(db, support)
    if failure == 'tamper':
        Path(support['path']).write_bytes(proof('Tampered registration original'))
    if failure == 'missing':
        Path(support['path']).unlink()
    ids = [] if failure == 'empty' else [support['id']]
    if failure == 'duplicate':
        ids.append('q:' + support['id'])
    fields = {'amount': 'NaN'} if failure == 'bad-amount' else {'category': 'x' * 201} if failure == 'category-long' else {}
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        confirmed_creation(matcher, [], [], project, support_ids=ids,
                               revisions={support['id']: 0 if failure == 'revision' else support['revision']}, fields=fields)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('failure', ['stale', 'cross-project', 'verified', 'submitted', 'tamper'])
def test_existing_member_creation_checks_version_state_and_original_integrity(setup: Any, failure: str) -> None:
    matcher, project, other = setup
    item = expense(matcher, other if failure == 'cross-project' else project, 'Protected existing registration', '14')
    if failure in {'verified', 'submitted'}:
        with matcher.store.connect() as db:
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
            data.update(verified=True, stage='submitted' if failure == 'submitted' else 'draft')
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
        item = matcher.store.get(item['id'])
    if failure == 'tamper':
        path, _ = matcher.store.attachment_path(item['attachments'][0]['id'])
        path.write_bytes(proof('Externally tampered existing invoice'))
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        confirmed_creation(matcher, ['e:' + item['id']], [], project,
                               expected_versions={item['id']: 'stale' if failure == 'stale' else item['version']})
    assert snapshots(matcher) == before


def test_creation_parses_missing_invoice_metadata_and_blocks_binary_variant_duplicate(setup: Any) -> None:
    matcher, project, _ = setup
    number = '12345678901234567890'
    source = make_invoice_pdf(matcher.store.workspace / 'creation-original.pdf', amount='14', invoice_number=number)
    entries = []
    for index in range(2):
        with pymupdf.open(source) as document:
            document.set_metadata({'title': f'Binary variant {index}'})
            entry = matcher.intake.receive(f'variant-{index}.pdf', document.tobytes(), project_id=project)
        entry.update(role='invoice', status='review', revision=1, fields={'amount': '14', 'category': '餐饮'})
        with matcher.store.connect() as db:
            matcher._write_queue(db, entry)
        entries.append(entry)
    first = confirmed_creation(matcher, ['q:' + entries[0]['id']], [], project, revisions=revisions(entries[0]))
    assert first['item']['invoice_numbers'] == [number]
    before = snapshots(matcher)
    with pytest.raises(ValueError, match='发票号码'):
        confirmed_creation(matcher, ['q:' + entries[1]['id']], [], project, revisions=revisions(entries[1]))
    assert snapshots(matcher) == before
    with pytest.raises(ValueError):
        confirmed_creation(matcher, ['q:' + entries[0]['id']], [], project, revisions=revisions(entries[0]))
    assert len(active_items(matcher)) == 1


@pytest.mark.parametrize('reused', [False, True])
def test_registration_undo_and_supplement_refuse_later_edits(setup: Any, reused: bool) -> None:
    matcher, project, _ = setup
    if reused:
        item = expense(matcher, project, 'Reused edit protection', '14')
        result = confirmed_creation(matcher, ['e:' + item['id']], [], project, expected_versions={item['id']: item['version']})
    else:
        support = queued(matcher, project, 'New edit protection', 'other', None)
        result = confirmed_creation(matcher, [], [], project, support_ids=[support['id']], revisions=revisions(support))
    with matcher.store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (result['item']['id'],)).fetchone()['data'])
        data['note'] = 'Later human annotation'
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), data['id']))
    support = queued(matcher, project, 'Later new ordinary proof', 'detail', None)
    before = snapshots(matcher)
    version = matcher.store.get(result['item']['id'])['version']
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.undo(result['match']['id'], version)
    assert snapshots(matcher) == before
    with pytest.raises(ValueError, match='后续内容已变化'):
        matcher.supplement(result['match']['id'], [support['id']], expected_version=version, revisions=revisions(support))
    assert snapshots(matcher) == before


def test_creation_transaction_fault_rolls_back_member_moves_and_all_relationships(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    members = [expense(matcher, project, 'Atomic creation ten', '10'), expense(matcher, project, 'Atomic creation four', '4')]
    support = queued(matcher, project, 'Atomic creation support', 'purchase', None)
    before = snapshots(matcher)
    write = matcher._write_queue

    def fail_after_write(db: Any, entry: dict[str, Any]) -> None:
        write(db, entry)
        raise RuntimeError('Synthetic creation interruption')

    monkeypatch.setattr(matcher, '_write_queue', fail_after_write)
    with pytest.raises(RuntimeError, match='interruption'):
        confirmed_creation(matcher, ['e:' + item['id'] for item in members], [], project,
                               expected_versions={item['id']: item['version'] for item in members},
                               support_ids=[support['id']], revisions=revisions(support))
    assert snapshots(matcher) == before and Path(support['path']).is_file()


def test_purchase_creation_does_not_expand_existing_match_or_supplement_allowlist(setup: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Strict old voucher', 'invoice', '14')
    payment = queued(matcher, project, 'Strict old payment', 'payment', '14')
    support = queued(matcher, project, 'Purchase creation only', 'purchase', None)
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.match('q:' + voucher['id'], [payment['id']], project,
                      revisions=revisions(voucher, payment, support), support_ids=[support['id']])
    assert snapshots(matcher) == before
    grouped = matcher.match('q:' + voucher['id'], [payment['id']], project, revisions=revisions(voucher, payment))
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.supplement(grouped['match']['id'], [support['id']], expected_version=grouped['item']['version'], revisions=revisions(support))
    assert snapshots(matcher) == before


@pytest.mark.parametrize('field,value', [('project_type', None), ('project_type', ''), ('project_type', 'unknown'),
                                       ('project_name', None), ('project_name', ''), ('project_name', 'x' * 201),
                                       ('amount', None), ('amount', ''), ('amount', '  '), ('amount', 'NaN'), ('amount', True)])
def test_creation_requires_three_explicit_valid_confirmations(setup: Any, field: str, value: Any) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Machine-recognized but unconfirmed invoice', 'invoice', '14')
    fields: dict[str, Any] = {'project_type': 'living', 'project_name': '住宿费(附清单)', 'amount': '14'}
    fields[field] = value
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.create_expense(['q:' + voucher['id']], [], project, revisions=revisions(voucher), fields=fields)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('fields', [None, {}, {'title': 'Old free title', 'category': '酒店', 'amount': '14'},
                                  {'project_type': 'living', 'project_name': '自填住宿名称', 'amount': '14'},
                                  {'project_type': 'personnel', 'project_name': '住宿费(附清单)', 'amount': '14'}])
def test_legacy_free_fields_cannot_bypass_template_selection(setup: Any, fields: dict[str, Any] | None) -> None:
    matcher, project, _ = setup
    voucher = queued(matcher, project, 'Cannot infer confirmation from machine defaults', 'invoice', '14')
    before = snapshots(matcher)
    with pytest.raises(ValueError):
        matcher.create_expense(['q:' + voucher['id']], [], project, revisions=revisions(voucher), fields=fields)
    assert snapshots(matcher) == before


@pytest.mark.parametrize('kind,name,category,section', [('living', '住宿费(附清单)', '酒店', 'hotel'),
                                                      ('transport', '火车、飞机', '高铁', 'rail'),
                                                      ('personnel', '工厂搭建人工数', '其他', 'build_workers'),
                                                      ('expansion', '施工押金(可退)', '其他', 'deposit'),
                                                      ('materials', '自填现场材料', '材料采购', 'materials'),
                                                      ('other', '自填其他费用', '其他', 'other')])
def test_template_choice_persists_final_report_fields_and_ignores_free_overrides(setup: Any, kind: str, name: str,
                                                                               category: str, section: str) -> None:
    matcher, project, _ = setup
    original = queued(matcher, project, 'Machine dining evidence', 'invoice', '14')
    result = matcher.create_expense(['q:' + original['id']], [], project, revisions=revisions(original),
                                    fields={'project_type': kind, 'project_name': '  ' + name + '  ', 'amount': '14',
                                            'category': '恶意自由类别', 'title': 'Ignored independent title', 'report_section': 'sf'})
    item = result['item']
    assert item['project_type'] == kind and item['project_name'] == name
    assert item['title'] == item['display_name'] == name
    assert item['category'] == category and item['report_section'] == section and item['report_group'] == ''
    assert item['amount_cents'] == 1400 and not item['verified']
    assert item['complete'] is (category == '高铁')
    assert item['material_basis'] == ('standard' if category == '高铁' else 'invoice_payment')
    assert result['match']['recognition']['title'] == 'Machine dining evidence'


def test_board_exposes_shared_report_hierarchy_and_readonly_machine_suggestions(setup: Any) -> None:
    matcher, project, _ = setup
    original = queued(matcher, project, 'Dining machine suggestion', 'invoice', '14')
    before = snapshots(matcher)
    board = matcher.board(project)
    assert [kind['code'] for kind in board['report_types']] == ['expansion', 'transport', 'living', 'personnel', 'materials', 'other']
    card = next(card for card in board['vouchers'] if card['id'] == original['id'])
    assert card['report_choice']['project_type'] == 'living'
    assert card['report_choice']['project_name'] == '现场饮用水、夜宵'
    assert snapshots(matcher) == before
