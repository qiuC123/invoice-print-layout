"""Bulk views keep the single-item contract without per-expense SQL."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from invoice_print_layout.reimbursement_people import ReimbursementPeople
from tests.test_material_matching import setup, expense, queued, proof


def test_bulk_matches_single_reads_and_has_bounded_sql(setup: Any) -> None:
    matcher, project, _ = setup
    store = matcher.store
    people = ReimbursementPeople(store)
    first = people.create(project, 'Original')['person']
    target = people.create(project, 'Current')['person']
    items = [expense(matcher, project, f'Bulk {index}') for index in range(12)]
    store.add_attachment(items[0]['id'], 'second.pdf', proof('second'), 'other')
    store.add_attachment(items[0]['id'], 'third.pdf', proof('third'), 'payment')
    entry = queued(matcher, project, 'Saved recognition', 'invoice', '14')
    entry.update(item_id=items[0]['id'], qr_check={'status': 'checked'})
    with store.connect() as db:
        matcher._write_queue(db, entry)
        db.execute("UPDATE reimbursement_people SET state='merged',merged_into=? WHERE id=?", (target['id'], first['id']))
        db.execute("UPDATE reimbursement_people SET display_label='Current display' WHERE id=?", (target['id'],))
        for index, item in enumerate(items):
            row = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()
            saved = json.loads(row['data'])
            saved.update(reimbursement_person_id=first['id'], reimbursement_person_label='Old label')
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(saved), item['id']))
            for number in range(35):
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           (item['id'], '2026-01-01', f'Event {index}-{number}'))
        rows = db.execute('SELECT id,source,data FROM expenses ORDER BY rowid DESC').fetchall()
        expected = [store._read_item(db, row) for row in rows]
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        assert store._read_items(db, rows[:1]) == expected[:1]
        single_count = len(statements)
        statements.clear()
        actual = store._read_items(db, rows)
        assert actual == expected
        assert len(statements) <= single_count + 1
        assert all(item['reimbursement_person_label'] == 'Current display' for item in actual)
        assert all(len(item['events']) == 30 for item in actual)
    ids = [items[2]['id'], items[0]['id'], items[2]['id']]
    assert store.get_many(ids) == [store.get(key) for key in ids]


def test_bulk_legacy_return_and_external_source_loss_stay_live(setup: Any) -> None:
    matcher, project, _ = setup
    store = matcher.store
    item = expense(matcher, project, 'Legacy withdrawal')
    with store.connect() as db:
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                   (item['id'], '2026-01-01', '撤回材料登记；保留原件'))
        db.execute('INSERT INTO material_matches(id,data) VALUES (?,?)', ('legacy', json.dumps({
            'id': 'legacy', 'item_id': item['id'], 'status': 'undone', 'undone_at': '9999-01-01'})))
        path = Path(db.execute('SELECT path FROM attachments WHERE item_id=?', (item['id'],)).fetchone()['path'])
    first = store.list_items()[0]
    assert first == store.get(item['id']) and first['returned_to_synthesis']
    assert first['attachments'][0]['available']
    path.unlink()
    missing = store.list_items()[0]
    assert missing == store.get(item['id'])
    assert not missing['attachments'][0]['available']
    assert missing['version'] != first['version']
