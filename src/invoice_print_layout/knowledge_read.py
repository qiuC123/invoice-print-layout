"""Read-only source records for manual knowledge consolidation."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


@contextmanager
def connect(database: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')  # Count, rows and fingerprints share one read snapshot.
        yield db


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def snapshot_page(rows: list[dict[str, Any]], key: str, *, offset: int = 0, limit: int = 200,
                  expected: str = '') -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 1000:
        raise ValueError('分页参数无效')
    rows = sorted(rows, key=lambda row: row[key])
    hashes = {row[key]: fingerprint(row) for row in rows}
    version = fingerprint(hashes)
    if expected and expected != version:
        raise ValueError('快照已变化，请从第一页重新读取')
    return {'status': 'complete', 'items': rows[offset:offset + limit], 'total': len(rows),
            'offset': offset, 'limit': limit, 'has_more': offset + limit < len(rows),
            'fingerprint': version,
            'fingerprints': {row[key]: hashes[row[key]] for row in rows[offset:offset + limit]}}


def append_page(database: Path, source: str, *, after_id: int = 0, limit: int = 200) -> dict[str, Any]:
    if source not in {'audit', 'corrections'} or after_id < 0 or not 1 <= limit <= 1000:
        raise ValueError('分页参数无效')
    table = 'invoice_attention_audit' if source == 'audit' else 'recognition_corrections'
    with connect(database) as db:
        total = db.execute(f'SELECT COUNT(*) FROM {table} WHERE id>?', (after_id,)).fetchone()[0]
        rows = [dict(row) for row in db.execute(f'SELECT * FROM {table} WHERE id>? ORDER BY id LIMIT ?',
                                               (after_id, limit))]
    for row in rows:
        if source == 'audit':
            data = json.loads(row['data'])
            row.update(data=data, decided_by=data.get('decided_by'), evidence=data.get('evidence'),
                       project_id=(data.get('after') or {}).get('project_id')
                       or (data.get('before') or {}).get('project_id') or data.get('project_id'))
        else:
            for field in ('old_value', 'new_value'):
                row[field] = json.loads(row[field]) if row[field] is not None else None
    return {'status': 'complete', 'items': rows, 'total': total, 'after_id': after_id, 'limit': limit,
            'next_after_id': rows[-1]['id'] if rows else after_id, 'has_more': total > len(rows)}


def classification_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{'item_id': row['item_id'], 'input_key': row['input_key'], 'evidence_key': row['evidence_key'],
             'result': json.loads(row['result']), 'expense': json.loads(row['expense']) if row['expense'] else None}
            for row in db.execute('SELECT c.*, e.data AS expense FROM category_attempts c '
                                  'LEFT JOIN expenses e ON e.id=c.item_id ORDER BY c.item_id')]


def attention_rows(db: sqlite3.Connection, project: str = '') -> list[dict[str, Any]]:
    return [{**json.loads(row['data']), 'object': row['object'], 'status': row['status'],
             'project_id': row['project_id']}
            for row in db.execute('SELECT * FROM invoice_attention WHERE (?=\'\' OR project_id=?) ORDER BY object',
                                  (project, project))]


def preferences_snapshot(database: Path) -> dict[str, Any]:
    with connect(database) as db:
        learned = [dict(row) for row in db.execute(
            'SELECT e.*, d.evidence_key IS NULL AS enabled FROM category_experience e '
            'LEFT JOIN category_experience_disabled d ON d.evidence_key=e.evidence_key ORDER BY e.evidence_key')]
        rules = [json.loads(row['data']) for row in db.execute('SELECT data FROM expense_preferences ORDER BY rowid DESC')]
    return {'learned': learned, 'rules': rules}
