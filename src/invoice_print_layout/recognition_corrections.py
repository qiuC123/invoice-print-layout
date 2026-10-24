"""Transactional audit of actual human field changes, separate from protection flags."""
from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from typing import Any

TRACKED = frozenset({'amount', 'merchant', 'invoice_number', 'category', 'expense_date', 'title',
                     'order_number', 'goods', 'purchase_content', 'payment_status'})


def equal(field: str, old: Any, new: Any) -> bool:
    if field == 'amount' and old not in ('', None) and new not in ('', None):
        from invoice_print_layout.workbench import amount_cents
        try:
            return bool(amount_cents(old) == amount_cents(new))
        except ValueError:
            pass
    return ('' if old is None else old) == ('' if new is None else new)


def record(db: sqlite3.Connection, material_id: str, old: dict[str, Any], new: dict[str, Any],
           sources: dict[str, Any]) -> None:
    from invoice_print_layout.workbench import now
    for field in sorted(TRACKED & set(new)):
        if not equal(field, old.get(field), new[field]):
            source = str(sources.get(field) or 'unknown')
            latest = db.execute('SELECT new_value FROM recognition_corrections WHERE material_id=? AND field=? ORDER BY id DESC LIMIT 1',
                                (material_id, field)).fetchone()
            if latest and equal(field, json.loads(latest[0]), old.get(field)):
                source = 'manual'
            db.execute('INSERT INTO recognition_corrections(material_id,field,old_value,new_value,source,at) VALUES (?,?,?,?,?,?)',
                       (material_id, field, json.dumps(old.get(field), ensure_ascii=False),
                        json.dumps(new[field], ensure_ascii=False), source, now()))


def record_expense(db: sqlite3.Connection, item_id: str, old: dict[str, Any], new: dict[str, Any]) -> None:
    old = {**old, 'amount': '' if old.get('amount_pending') else f"{old.get('amount_cents', 0)/100:.2f}"}
    new = {**new, 'amount': '' if new.get('amount_pending') else f"{new.get('amount_cents', 0)/100:.2f}"}
    entries = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
        entries = [entry for row in db.execute('SELECT data FROM intake_queue')
                   if (entry := json.loads(row[0])).get('item_id') == item_id]
    for field in sorted(TRACKED & set(new)):
        candidates = [entry for entry in entries if equal(field, entry.get('fields', {}).get(field), old.get(field))]
        entry = candidates[0] if len(candidates) == 1 else entries[0] if len(entries) == 1 else None
        record(db, entry['id'] if entry else 'expense:' + item_id, {field: old.get(field)}, {field: new[field]},
               entry.get('field_sources', {}) if entry else {field: 'stored_expense'})


def summary(db: sqlite3.Connection) -> list[dict[str, Any]]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='recognition_corrections'").fetchone():
        return []
    changed: dict[tuple[str, str], set[str]] = defaultdict(set)
    observed: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in db.execute('SELECT material_id,field,source FROM recognition_corrections'):
        key = (row[1], row[2]);changed[key].add(row[0]);observed[key].add(row[0])
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
        for row in db.execute('SELECT data FROM intake_queue'):
            entry = json.loads(row[0])
            for field, source in entry.get('field_sources', {}).items():
                if source != 'manual' and field in TRACKED and entry.get('fields', {}).get(field) not in ('', None):
                    observed[(field, source)].add(entry['id'])
    return [{'field': field, 'source': source, 'corrected': len(changed[(field, source)]),
             'observed': len(ids), 'rate': len(changed[(field, source)]) / len(ids)}
            for (field, source), ids in sorted(observed.items()) if ids]
