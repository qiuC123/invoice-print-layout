"""Only synthetic ledgers: no live service, business records or external paths."""
from contextlib import closing
import json
import sqlite3
from threading import Lock

import pytest

from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.category_migration import MIGRATION, migrate_categories
from invoice_print_layout.expense_preferences import Preferences
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.missing_materials import acknowledgement_valid, expense_scope
from invoice_print_layout.category_migration import renamed_fields


def old_ledger(tmp_path):
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Synthetic title', 'amount': '12', 'category': '其他',
                         'merchant': 'Synthetic carrier', 'display_name': '顺丰同城'})
    AutomaticCategory(store, Lock())
    Preferences(store)
    with store.connect() as db:
        db.execute('DELETE FROM workbench_migrations')
        raw = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()[0])
        raw.update(category='顺丰', report_section='sf', project_type='other',
                   project_name='顺丰同城', title='顺丰', merchant='顺丰', note='顺丰')
        raw['agent_content'] = {'decision': {'category': '顺丰'}, 'goods': [{'name': '顺丰快递费'}]}
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(raw, ensure_ascii=False), item['id']))
        db.execute('INSERT INTO category_experience VALUES (?,?,?,?)', ('synthetic-key', '顺丰', 'human', 0))
        result = {'category': '顺丰', 'managed_category': '顺丰', 'reason': '顺丰',
                  'evidence': {'goods': '顺丰快递费'}}
        db.execute('INSERT INTO category_attempts VALUES (?,?,?,?)', (item['id'], 'input', 'evidence', json.dumps(result)))
        for kind, value in [('category', '顺丰'), ('report_section', 'sf'), ('display_name', '顺丰同城'),
                            ('report_group', '顺丰')]:
            db.execute('INSERT INTO expense_preferences VALUES (?,?)',
                       (kind, json.dumps({'kind': kind, 'value': value, 'match': '顺丰', 'purpose': '顺丰'})))
        db.execute('CREATE TABLE intake_queue (id TEXT PRIMARY KEY, digest TEXT, data TEXT NOT NULL)')
        db.execute('INSERT INTO intake_queue VALUES (?,?,?)', ('synthetic-material', 'digest',
                   json.dumps({'fields': {'category': '顺丰', 'merchant': '顺丰'}, 'text': '顺丰'})))
        db.execute('CREATE TABLE material_matches (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT INTO material_matches VALUES (?,?)', ('synthetic-group', json.dumps({
            'aggregate_after': {'data': raw}, 'member_before': {'synthetic-member': {'data': raw}},
            'queue_before': {'synthetic-material': {'fields': {'category': '顺丰'}}}})))
    return store, raw


def dump(database):
    with closing(sqlite3.connect(database)) as db:
        return '\n'.join(db.iterdump())


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_startup_backs_up_renames_only_categories_and_is_idempotent(tmp_path, journal):
    store, before = old_ledger(tmp_path)
    with store.connect() as db:
        db.execute('PRAGMA journal_mode=' + journal)
    store = ExpenseStore(tmp_path)
    with store.connect() as db:
        after = json.loads(db.execute('SELECT data FROM expenses').fetchone()[0])
        expected = {**before, 'category': '快递', 'report_section': 'express',
                    'agent_content': {'decision': {'category': '快递'}, 'goods': [{'name': '顺丰快递费'}]}}
        assert after == expected
        assert db.execute('SELECT category FROM category_experience').fetchone()[0] == '快递'
        attempt = json.loads(db.execute('SELECT result FROM category_attempts').fetchone()[0])
        assert attempt == {'category': '快递', 'managed_category': '快递', 'reason': '顺丰',
                           'evidence': {'goods': '顺丰快递费'}}
        prefs = {row[0]: json.loads(row[1]) for row in db.execute('SELECT * FROM expense_preferences')}
        assert {key: value['value'] for key, value in prefs.items()} == {
            'category': '快递', 'report_section': 'express', 'display_name': '顺丰同城', 'report_group': '顺丰'}
        assert all(value['match'] == value['purpose'] == '顺丰' for value in prefs.values())
        queued = json.loads(db.execute('SELECT data FROM intake_queue').fetchone()[0])
        assert queued == {'fields': {'category': '快递', 'merchant': '顺丰'}, 'text': '顺丰'}
        group = json.loads(db.execute('SELECT data FROM material_matches').fetchone()[0])
        assert group['aggregate_after']['data'] == group['member_before']['synthetic-member']['data'] == after
        assert group['queue_before']['synthetic-material']['fields']['category'] == '快递'
        marker = db.execute('SELECT * FROM workbench_migrations').fetchone()
        assert marker['name'] == MIGRATION
    backups = list((tmp_path / 'backups').glob('*/expenses.sqlite3'))
    assert len(backups) == 1 and str(backups[0]) == marker['backup_path']
    with closing(sqlite3.connect(backups[0])) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert json.loads(db.execute('SELECT data FROM expenses').fetchone()[0]) == before
        assert not db.execute('SELECT * FROM workbench_migrations').fetchall()
    first = dump(store.database)
    ExpenseStore(tmp_path)
    assert dump(store.database) == first
    assert list((tmp_path / 'backups').glob('*/expenses.sqlite3')) == backups
    assert store.get(before['id'])['display_name'] == '顺丰同城'
    # Documented rollback uses the SQLite backup API, not a live file overwrite.
    with closing(sqlite3.connect(backups[0])) as src, closing(sqlite3.connect(store.database)) as dst:
        src.backup(dst)
    with closing(sqlite3.connect(store.database)) as db:
        assert json.loads(db.execute('SELECT data FROM expenses').fetchone()[0]) == before


def test_backup_failure_leaves_categories_and_marker_untouched(tmp_path):
    store, _ = old_ledger(tmp_path)
    (tmp_path / 'backups').write_text('synthetic path obstruction', encoding='utf-8')
    before = dump(store.database)
    with pytest.raises(OSError):
        ExpenseStore(tmp_path)
    assert dump(store.database) == before


def test_update_failure_rolls_back_every_table_and_preserves_backup(tmp_path):
    store, _ = old_ledger(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TRIGGER reject_category BEFORE UPDATE ON category_experience "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    before = dump(store.database)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic failure'):
        ExpenseStore(tmp_path)
    assert dump(store.database) == before
    assert len(list((tmp_path / 'backups').glob('*/expenses.sqlite3'))) == 1


def test_old_minimal_schema_and_corrupt_json_fail_closed(tmp_path):
    database = tmp_path / '工作台' / 'expenses.sqlite3'
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE expenses (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        db.execute('INSERT INTO expenses VALUES (?,?)', ('synthetic', '{broken'))
    before = dump(database)
    with pytest.raises(json.JSONDecodeError):
        migrate_categories(database)
    assert dump(database) == before
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('UPDATE expenses SET data=?', (json.dumps({'category': '顺丰', 'title': '顺丰'}),))
    migrate_categories(database)
    with closing(sqlite3.connect(database)) as db:
        assert json.loads(db.execute('SELECT data FROM expenses').fetchone()[0]) == {'category': '快递', 'title': '顺丰'}


def test_new_database_does_not_create_pointless_backup_and_accepts_old_category_input(tmp_path):
    store = ExpenseStore(tmp_path)
    assert not (tmp_path / 'backups').exists()
    item = store.create({'title': 'Synthetic legacy import', 'category': '顺丰', 'report_section': 'sf'})
    assert item['category'] == '快递' and item['report_section'] == 'express'
    assert len(store.list_items()) == 1


def test_legacy_missing_material_confirmation_survives_rename_but_not_content_changes():
    original = {'category': '顺丰', 'report_section': 'sf', 'verified': True, 'title': 'Synthetic', 'amount_cents': 1200}
    original['missing_material_acknowledgement'] = {
        'expense_scope': expense_scope(original), 'missing': ['运单明细'], 'sources': []}
    migrated = renamed_fields(original)
    assert acknowledgement_valid(migrated, {'missing': ['运单明细']}, [])
    assert not acknowledgement_valid({**migrated, 'amount_cents': 1300}, {'missing': ['运单明细']}, [])
    assert not acknowledgement_valid({**migrated, 'category': '打印'}, {'missing': ['运单明细']}, [])
