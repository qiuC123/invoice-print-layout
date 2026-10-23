from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout import report_types as choices
from invoice_print_layout.report_snapshot import snapshot
from invoice_print_layout.workbench import ExpenseStore


AUTO = '自动分类已保存：酒店；材料和金额仍需核对'


def old_registration(category: str = '酒店', name: str = '发票') -> dict[str, Any]:
    original = choices.validate_report_choice('other', name)
    return {**original, 'category': category, 'id': 'synthetic-lodging',
            'project': 'Synthetic project', 'project_id': '', 'merchant': 'Synthetic store',
            'amount_cents': 135300, 'expense_date': '', 'stage': 'draft',
            'material_match': {'mode': 'registration', 'recognition': {'report_choice': original}}}


def test_untouched_old_default_uses_saved_category_and_keeps_input() -> None:
    item = old_registration()
    before = deepcopy(item)
    history = [{'message': AUTO}, {'message': '指定费用费用归属人：未指定 → Synthetic person；金额、分类及核对状态不变'},
               {'message': '人工确认材料内容及金额'}]
    assert choices.legacy_auto_category_report_choice(item, history) == choices.validate_report_choice('living', '住宿费(附清单)')
    assert item == before
    material = old_registration('材料采购', 'Synthetic cable')
    assert choices.legacy_auto_category_report_choice(material, [
        {'message': AUTO.replace('酒店', '材料采购')}]) == choices.validate_report_choice('materials', 'Synthetic cable')


@pytest.mark.parametrize('message', ['更新事项；内容变更后需重新核对',
                                   '编辑报销项目类型、项目名称及金额；保留原件和关联，待重新核对',
                                   '批量修改报销项目类型：其他；原名称、金额及核对状态保留',
                                   '确认购买内容与实际支出：人工内容确认'])
@pytest.mark.parametrize('before_auto', [True, False])
def test_manual_edits_anywhere_in_history_keep_explicit_choice(message: str, before_auto: bool) -> None:
    history = [{'message': message}, {'message': AUTO}]
    if not before_auto:
        history.reverse()
    assert choices.legacy_auto_category_report_choice(old_registration(), history) == {}


@pytest.mark.parametrize('field,value', [('project_type', 'materials'), ('project_name', '人工保留用途'),
                                        ('report_section', 'hotel'), ('display_name', '人工保留名称'),
                                        ('title', '人工填写购买内容'), ('report_group', '人工分组')])
def test_changed_registration_fields_are_never_replaced(field: str, value: str) -> None:
    item = old_registration()
    item[field] = value
    assert choices.legacy_auto_category_report_choice(item, [{'message': AUTO}]) == {}


@pytest.mark.parametrize('alteration', ['no_history', 'wrong_category_event', 'no_registration',
                                      'missing_snapshot', 'incomplete_snapshot', 'invalid_snapshot', 'unknown_category'])
def test_incomplete_or_ambiguous_provenance_never_guesses(alteration: str) -> None:
    item = old_registration()
    history = [{'message': AUTO}]
    if alteration == 'no_history':
        history = []
    elif alteration == 'wrong_category_event':
        history = [{'message': AUTO.replace('酒店', '外卖')}]
    elif alteration == 'no_registration':
        item['material_match']['mode'] = 'matching'
    elif alteration == 'missing_snapshot':
        item['material_match'].pop('recognition')
    elif alteration == 'incomplete_snapshot':
        item['material_match']['recognition']['report_choice'].pop('display_name')
    elif alteration == 'invalid_snapshot':
        item['material_match']['recognition']['report_choice']['project_name'] = ''
    elif alteration == 'unknown_category':
        item['category'] = 'Unsupported category'
        history = [{'message': AUTO.replace('酒店', 'Unsupported category')}]
    assert choices.legacy_auto_category_report_choice(item, history) == {}


@pytest.mark.parametrize('merchant', ['Synthetic 宾馆', 'Synthetic 便利店', 'Synthetic 旅行社', ''])
def test_merchant_is_never_used_to_choose_or_change_category(merchant: str) -> None:
    item = old_registration()
    item['merchant'] = merchant
    assert choices.legacy_auto_category_report_choice(item, [{'message': AUTO}])['report_section'] == 'hotel'
    other = old_registration('其他')
    other['merchant'] = merchant
    assert choices.legacy_auto_category_report_choice(other, [{'message': AUTO}]) == {}


def create_old_expense(store: ExpenseStore, *, manual: bool = False) -> str:
    item = store.create({'title': '发票', 'category': '其他', 'project': 'Synthetic project',
                         'amount': '1353.00', 'material_basis': 'invoice_payment'})
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((20, 30), 'SYNTHETIC LODGING ORIGINAL')
        store.add_attachment(item['id'], 'synthetic.pdf', pdf.tobytes(), 'invoice')
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        saved.update(old_registration(), id=item['id'], verified=True)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), item['id']))
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item['id'], '2026-01-01', AUTO))
        if manual:
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item['id'], '2026-01-01', '更新事项；内容变更后需重新核对'))
        for index in range(45):
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item['id'], '2026-01-02', f'指定费用费用归属人：Synthetic {index}；金额、分类及核对状态不变'))
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                   (item['id'], '2026-01-03', '人工确认材料内容及金额'))
    return str(item['id'])


def ledger_contents(store: ExpenseStore) -> list[Any]:
    with store.connect() as db:
        return [[tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                for table in ('expenses', 'events', 'attachments')]


def test_read_projection_and_report_order_share_effective_choice_without_writes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    key = create_old_expense(store)
    before = ledger_contents(store)
    with monkeypatch.context() as patch:
        patch.setattr(choices, 'legacy_auto_category_report_choice', lambda *args: {})
        unprojected = store.get(key)
    original_path, _ = store.attachment_path(unprojected['attachments'][0]['id'])
    original_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
    item = store.get(key)
    assert all(event['message'] != AUTO for event in item['events'])  # outside the 30-row display window
    assert item['project_type'] == 'living' and item['project_name'] == item['title'] == '住宿费(附清单)'
    assert item['report_section'] == 'hotel' and item['report_choice'] == choices.validate_report_choice('living', '住宿费(附清单)')
    assert item['stored_report_choice'] == {field: unprojected.get(field, '') for field in item['stored_report_choice']}
    for field in ('version', 'amount', 'amount_cents', 'category', 'verified', 'ready', 'attachments', 'events'):
        assert item[field] == unprojected[field]
    material = {**old_registration('材料采购', 'Synthetic cable'), **choices.validate_report_choice('materials', 'Synthetic cable'),
                'id': 'synthetic-material', 'amount_cents': 100}
    report = snapshot([material, item])
    assert report['ordered_item_ids'] == [key, 'synthetic-material']
    assert [row['row'] for row in report['rows']] == [16, 24]
    assert report['total_cents'] == 135400
    assert snapshot(report['items'])['rows'] == report['rows']
    assert ledger_contents(store) == before
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == original_hash


def test_manual_edit_outside_display_event_window_still_blocks_compatibility(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    key = create_old_expense(store, manual=True)
    before = ledger_contents(store)
    item = store.get(key)
    assert all(not event['message'].startswith('更新事项') for event in item['events'])
    assert item['project_type'] == 'other' and item['project_name'] == '发票'
    assert 'stored_report_choice' not in item
    assert snapshot([item])['rows'][0]['row'] == 29
    assert ledger_contents(store) == before


@pytest.mark.parametrize('field,value', [('material_match', ['legacy record']),
                                        ('material_match', 'legacy record'),
                                        ('recognition', ['legacy record']),
                                        ('recognition', 'legacy record'),
                                        ('report_choice', ['legacy record']),
                                        ('report_choice', 'legacy record')])
def test_abnormal_legacy_nested_values_do_not_break_reads(tmp_path: Path, field: str, value: Any) -> None:
    store = ExpenseStore(tmp_path)
    key = create_old_expense(store)
    with store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
        if field == 'material_match':
            data[field] = value
        elif field == 'recognition':
            data['material_match'][field] = value
        else:
            data['material_match']['recognition'][field] = value
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
    before = ledger_contents(store)
    item = store.get(key)
    assert item['project_type'] == 'other' and 'stored_report_choice' not in item
    assert store.list_items()[0]['id'] == key
    assert ledger_contents(store) == before


def ready_old_expense(store: ExpenseStore) -> str:
    key = create_old_expense(store)
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((20, 30), 'SYNTHETIC PAYMENT ORIGINAL')
        store.add_attachment(key, 'payment.pdf', pdf.tobytes(), 'payment')
    store.transition(key, 'verify', expected_version=store.get(key)['version'])
    return key


def test_submission_accepts_projection_but_only_updates_persisted_lifecycle_fields(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    key = ready_old_expense(store)
    expected = snapshot([store.get(key)])['items'][0]
    expected_before = deepcopy(expected)
    files = {'pdf': 'synthetic.pdf', 'md': 'synthetic.md', 'xlsx': 'synthetic.xlsx'}
    with store.connect() as db:
        before = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
    store.submit_report([expected], files)
    with store.connect() as db:
        after = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
    assert after['stage'] == 'submitted' and after['report_files'] == files
    for field in expected['stored_report_choice']:
        assert after[field] == before[field]
    assert 'stored_report_choice' not in after and 'report_choice' not in after
    assert expected == expected_before


@pytest.mark.parametrize('change', ['note', 'event', 'attachment', 'expected_fields'])
def test_submission_still_rejects_concurrent_or_inconsistent_snapshots(tmp_path: Path, change: str) -> None:
    store = ExpenseStore(tmp_path)
    key = ready_old_expense(store)
    expected = snapshot([store.get(key)])['items'][0]
    if change == 'note':
        with store.connect() as db:
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
            data['note'] = 'Concurrent edit'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
    elif change == 'event':
        with store.connect() as db:
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (key, '2026-01-04', 'Concurrent event'))
    elif change == 'attachment':
        with store.connect() as db:
            db.execute('UPDATE attachments SET name=? WHERE item_id=?', ('concurrent.pdf', key))
    else:
        expected['note'] = 'Inconsistent snapshot with the same version'
    before = ledger_contents(store)
    with pytest.raises(ValueError, match='制作期间事项或材料已变化'):
        store.submit_report([expected], {'pdf': 'synthetic.pdf'})
    assert ledger_contents(store) == before


def manual_registration() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    initial_choice = choices.validate_report_choice('other', 'Synthetic purchase')
    original = choices.validate_report_choice('other', 'Receipt')
    matching = {'mode': 'registration', 'group_id': 'synthetic-registration',
                'recognition': {'report_choice': original}}
    initial = {**initial_choice, 'id': 'synthetic-expense', 'project_id': 'synthetic-project',
               'material_match': matching, 'amount_cents': 3180}
    history = [{'id': 1, 'at': '2026-01-01', 'message': '材料已登记；金额及对应关系待人工核对'},
               {'id': 2, 'at': '2026-01-02', 'message': AUTO.replace('酒店', '材料采购')},
               {'id': 3, 'at': '2026-01-03', 'message': '人工确认材料内容及金额'}]
    group = {'id': 'synthetic-registration', 'item_id': initial['id'], 'project_id': initial['project_id'],
             'schema_version': 3, 'mode': 'registration', 'status': 'provisional',
             'recognition': {'report_choice': original},
             'aggregate_after': {'data': deepcopy(initial), 'events': deepcopy(history[:1])}}
    return {**initial, 'category': '材料采购'}, history, group


def test_explicit_registration_survives_automatic_category_and_later_verification() -> None:
    item, history, group = manual_registration()
    before = deepcopy((item, history, group))
    expected = choices.validate_report_choice('other', 'Synthetic purchase')
    assert choices.explicit_manual_report_choice(item, history, group) == expected
    assert choices.legacy_manual_category_report_choice(item, history, group) == expected
    assert (item, history, group) == before
    item.update(amount_cents=4270, reimbursement_person_id='synthetic-person', verified=True)
    assert choices.legacy_manual_category_report_choice(item, history, group) == expected
    item['category'] = '其他'
    assert choices.explicit_manual_report_choice(item, history[:1], group) == expected
    assert choices.legacy_manual_category_report_choice(item, history[:1], group) == {}


@pytest.mark.parametrize('message', ['指定费用费用归属人：A → B；金额、分类及核对状态不变',
                                   '费用归属人更名：A → B；金额、分类、日期及核对状态不变',
                                   '费用归属人归并：A → B；金额、分类、日期及核对状态不变',
                                   '批量退回待核对；撤销本次核对及缺材料确认，金额、归属及原件保留',
                                   '人工确认材料内容及金额；已确认仍缺：发票',
                                   '收到材料：synthetic.pdf'])
def test_neutral_followup_actions_do_not_erase_manual_registration_choice(message: str) -> None:
    item, history, group = manual_registration()
    history.append({'message': message})
    assert choices.legacy_manual_category_report_choice(item, history, group)['category'] == '其他'


@pytest.mark.parametrize('alteration', [
    'no_group', 'broken_group', 'unknown_schema', 'wrong_group', 'wrong_item', 'wrong_project',
    'no_aggregate', 'no_data', 'missing_initial_choice_field', 'invalid_initial_choice',
    'same_as_recognition', 'recognition_disagrees', 'missing_recognition', 'no_initial_history',
    'wrong_initial_history', 'truncated_history', 'unknown_event', 'unknown_category',
    'manual_category', 'manual_project', 'generic_update', 'changed_title', 'changed_section',
    'wrong_auto_category', 'latest_auto_differs', 'auto_only_before_registration', 'undone_group',
])
def test_explicit_registration_projection_requires_complete_unambiguous_provenance(alteration: str) -> None:
    item, history, group = manual_registration()
    if alteration == 'no_group':
        group = None
    elif alteration == 'broken_group':
        group = ['legacy record']
    elif alteration == 'unknown_schema':
        group['schema_version'] = 99
    elif alteration == 'wrong_group':
        group['id'] = 'another-group'
    elif alteration == 'wrong_item':
        group['item_id'] = 'another-expense'
    elif alteration == 'wrong_project':
        group['project_id'] = 'another-project'
    elif alteration == 'no_aggregate':
        group.pop('aggregate_after')
    elif alteration == 'no_data':
        group['aggregate_after']['data'] = []
    elif alteration == 'missing_initial_choice_field':
        group['aggregate_after']['data'].pop('display_name')
    elif alteration == 'invalid_initial_choice':
        group['aggregate_after']['data']['project_type'] = 'unsupported'
    elif alteration == 'same_as_recognition':
        original = item['material_match']['recognition']['report_choice']
        item.update(original, category='材料采购')
        group['aggregate_after']['data'].update(original)
    elif alteration == 'recognition_disagrees':
        group['recognition']['report_choice'] = choices.validate_report_choice('other', 'Another receipt')
    elif alteration == 'missing_recognition':
        group['aggregate_after']['data']['material_match'].pop('recognition')
    elif alteration == 'no_initial_history':
        group['aggregate_after']['events'] = []
    elif alteration == 'wrong_initial_history':
        group['aggregate_after']['events'][0]['id'] = 7
    elif alteration == 'truncated_history':
        history = history[1:]
    elif alteration == 'unknown_event':
        history.append({'message': 'Historical action of unknown origin'})
    elif alteration == 'unknown_category':
        item['category'] = 'Unsupported category'
    elif alteration == 'manual_category':
        history.append({'message': '确认购买内容与实际支出：new manual category'})
    elif alteration == 'manual_project':
        history.append({'message': '编辑报销项目类型、项目名称及金额；保留原件和关联，待重新核对'})
    elif alteration == 'generic_update':
        history.insert(1, {'message': '更新事项；内容变更后需重新核对'})
    elif alteration == 'changed_title':
        item['title'] = 'Another purchase'
    elif alteration == 'changed_section':
        item['report_section'] = 'materials'
    elif alteration == 'wrong_auto_category':
        history[1]['message'] = AUTO
    elif alteration == 'latest_auto_differs':
        history.append({'message': AUTO})
    elif alteration == 'auto_only_before_registration':
        history = [history[1], history[0], history[2]]
        group['aggregate_after']['events'] = deepcopy(history[:2])
    elif alteration == 'undone_group':
        group['status'] = 'undone'
    assert choices.legacy_manual_category_report_choice(item, history, group) == {}


def create_explicit_registration(store: ExpenseStore) -> str:
    key = ready_old_expense(store)
    item, history, group = manual_registration()
    item.update(id=key, project_id='')
    group.update(item_id=key, project_id='')
    group['aggregate_after']['data'].update(id=key, project_id='')
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
        saved.update(item)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), key))
        db.execute('DELETE FROM events WHERE item_id=?', (key,))
        live = []
        for event in history:
            cursor = db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                                (key, event['at'], event['message']))
            live.append({**event, 'id': cursor.lastrowid})
        group['aggregate_after']['events'] = live[:1]
        db.execute('CREATE TABLE IF NOT EXISTS material_matches (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT INTO material_matches VALUES (?,?)', (group['id'], json.dumps(group, ensure_ascii=False)))
    return key


def test_manual_registration_read_projection_keeps_ledger_versions_amounts_and_originals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    key = create_explicit_registration(store)
    before = ledger_contents(store)
    with monkeypatch.context() as patch:
        patch.setattr(choices, 'legacy_manual_category_report_choice', lambda *args: {})
        raw_view = store.get(key)
    item = store.get(key)
    assert item['category'] == '其他' and raw_view['category'] == '材料采购'
    assert item['project_type'] == 'other' and item['project_name'] == 'Synthetic purchase'
    assert item['manual_report_choice'] == choices.validate_report_choice('other', 'Synthetic purchase')
    assert item['stored_report_choice']['category'] == '材料采购'
    for field in ('version', 'amount', 'amount_cents', 'verified', 'ready', 'attachments', 'events'):
        assert item[field] == raw_view[field]
    assert snapshot([item])['rows'][0]['row'] == 29
    assert ledger_contents(store) == before
    from contextlib import nullcontext
    from invoice_print_layout.automatic_category import AutomaticCategory
    from invoice_print_layout import expense_classifier
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
        saved['verified'] = False  # Exercise the manual guard, not the already-reviewed guard.
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), key))
    item = store.get(key)
    assert not item['verified'] and item['category'] == '其他'
    automatic = AutomaticCategory(store, nullcontext())
    monkeypatch.setattr(expense_classifier, 'collect', lambda *_args: pytest.fail('must not reclassify manual choice'))
    assert automatic.statuses([item])[key]['pending'] is False
    assert automatic.run(key, retry=True)['status'] == 'skipped'


def test_explicit_registration_submission_preserves_stored_choice(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    key = create_explicit_registration(store)
    expected = snapshot([store.get(key)])['items'][0]
    with store.connect() as db:
        before = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
    store.submit_report([expected], {'pdf': 'synthetic.pdf'})
    with store.connect() as db:
        after = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
    assert after['stage'] == 'submitted'
    for field in ('category', 'title', 'project_type', 'project_name', 'amount_cents', 'material_match'):
        assert after[field] == before[field]
    assert 'manual_report_choice' not in after and 'stored_report_choice' not in after


def test_submission_rejects_changed_projection_provenance_even_with_same_raw_version(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    key = create_explicit_registration(store)
    expected = snapshot([store.get(key)])['items'][0]
    with store.connect() as db:
        db.execute('UPDATE material_matches SET data=? WHERE id=?', ('{}', 'synthetic-registration'))
    assert store.get(key)['version'] == expected['version']
    before = ledger_contents(store)
    with pytest.raises(ValueError, match='制作期间事项或材料已变化'):
        store.submit_report([expected], {'pdf': 'synthetic.pdf'})
    assert ledger_contents(store) == before


def test_public_reused_registration_preserves_choice_after_pre_registration_note_edit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    from invoice_print_layout import expense_classifier
    from invoice_print_layout.automatic_category import AutomaticCategory
    from invoice_print_layout.intake_queue import IntakeQueue
    from invoice_print_layout.material_matching import MaterialMatching
    from invoice_print_layout.project_registry import ProjectRegistry

    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    matcher = MaterialMatching(store, IntakeQueue(store))
    item = store.create({'category': '其他', 'title': 'Receipt', 'project_id': project, 'amount': '31.80'})
    with pymupdf.open() as document:
        document.new_page().insert_text((20, 30), 'SYNTHETIC REUSED INVOICE')
        original = document.tobytes()
    item = store.add_attachment(item['id'], 'reused.pdf', original, 'invoice')
    item = store.update(item['id'], {'note': 'Saved before the registration form'})
    result = matcher.create_expense(['e:' + item['id']], [], project,
                                   expected_versions={item['id']: item['version']},
                                   fields={'project_type': 'other', 'project_name': 'Explicit name', 'amount': '31.80'})
    current = store.get(item['id'])
    assert result['created'] is False and result['item']['id'] == item['id']
    expected = choices.validate_report_choice('other', 'Explicit name')
    assert current['manual_report_choice'] == expected
    assert current['amount_cents'] == 3180 and not current['verified']
    path, _ = store.attachment_path(current['attachments'][0]['id'])
    assert path.read_bytes() == original
    automatic = AutomaticCategory(store, nullcontext())
    monkeypatch.setattr(expense_classifier, 'collect', lambda *_args: pytest.fail('manual registration must be protected'))
    assert automatic.statuses([current])[item['id']]['pending'] is False
    assert automatic.run(item['id'], retry=True)['status'] == 'skipped'
    # A later generic edit remains ambiguous: only the already-bound old edit
    # was superseded by the registration confirmation.
    current = store.update(item['id'], {'note': 'Saved after registration'})
    assert 'manual_report_choice' not in current


@pytest.mark.parametrize('automatic_category', ['打车', '顺丰'])
@pytest.mark.parametrize('old_acknowledgement', [False, True])
def test_effective_manual_choice_drives_material_requirements_and_invalidates_old_acknowledgements(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, automatic_category: str,
        old_acknowledgement: bool) -> None:
    store = ExpenseStore(tmp_path)
    key = create_explicit_registration(store)
    with store.connect() as db:
        saved = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
        saved['category'] = automatic_category
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(saved, ensure_ascii=False), key))
        db.execute('UPDATE events SET message=? WHERE item_id=? AND message=?',
                   (AUTO.replace('酒店', automatic_category), key, AUTO.replace('酒店', '材料采购')))
    if old_acknowledgement:
        with monkeypatch.context() as patch:
            patch.setattr(choices, 'legacy_manual_category_report_choice', lambda *args: {})
            unprojected = store.get(key)
            assert unprojected['missing']
            store.transition(key, 'verify', expected_version=unprojected['version'],
                             acknowledge_missing=True, expected_missing=unprojected['missing'])
    before = ledger_contents(store)
    with store.connect() as db:
        raw_version_input = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (key,)).fetchone()['data'])
        raw_version_input['attachments'] = [dict(row) for row in db.execute(
            'SELECT id,name,role,path FROM attachments WHERE item_id=?', (key,))]
        raw_version_input['events'] = [dict(row) for row in db.execute(
            'SELECT at,message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 30', (key,))]
    current = store.get(key)
    assert current['category'] == '其他' and current['material_basis'] == 'invoice_payment'
    assert current['complete'] and current['missing'] == []
    assert current['courier_waybill_required'] is False
    assert current['stored_report_choice']['category'] == automatic_category
    assert current['amount_cents'] == 3180
    if old_acknowledgement:
        assert current['missing_acknowledged'] is False and current['verified'] is False and current['ready'] is False
    else:
        assert current['ready'] and current['verified']
        assert current['version'] == hashlib.sha256(
            json.dumps(raw_version_input, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    assert ledger_contents(store) == before
