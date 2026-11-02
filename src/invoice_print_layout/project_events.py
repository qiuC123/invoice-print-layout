"""Transactional project readiness snapshots and a local, durable event outbox."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Callable, TYPE_CHECKING
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from invoice_print_layout.reliability import read_json
from invoice_print_layout.workbench_runtime import identity

if TYPE_CHECKING:
    from invoice_print_layout.workbench import ExpenseStore


def install(db: sqlite3.Connection) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS invoice_project_dirty (project_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS invoice_project_state
            (project_id TEXT PRIMARY KEY, version TEXT NOT NULL, verified_count INTEGER NOT NULL,
             last_attempt REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS invoice_event_outbox
            (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version TEXT NOT NULL,
             created REAL NOT NULL, retry_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
             status TEXT NOT NULL, payload TEXT NOT NULL, error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS invoice_event_due ON invoice_event_outbox(status,retry_at);
        CREATE TRIGGER IF NOT EXISTS invoice_event_expense_insert AFTER INSERT ON expenses BEGIN
            INSERT OR IGNORE INTO invoice_project_dirty VALUES (COALESCE(json_extract(NEW.data,'$.project_id'),''));
        END;
        CREATE TRIGGER IF NOT EXISTS invoice_event_expense_update AFTER UPDATE ON expenses BEGIN
            INSERT OR IGNORE INTO invoice_project_dirty VALUES (COALESCE(json_extract(OLD.data,'$.project_id'),''));
            INSERT OR IGNORE INTO invoice_project_dirty VALUES (COALESCE(json_extract(NEW.data,'$.project_id'),''));
        END;
        CREATE TRIGGER IF NOT EXISTS invoice_event_expense_delete AFTER DELETE ON expenses BEGIN
            INSERT OR IGNORE INTO invoice_project_dirty VALUES (COALESCE(json_extract(OLD.data,'$.project_id'),''));
        END;
    ''')
    if 'fingerprint' not in {row['name'] for row in db.execute('PRAGMA table_info(invoice_project_state)')}:
        db.execute("ALTER TABLE invoice_project_state ADD COLUMN fingerprint TEXT NOT NULL DEFAULT ''")
    for operation in ('INSERT', 'UPDATE', 'DELETE'):
        records = ('OLD', 'NEW') if operation == 'UPDATE' else ('OLD',) if operation == 'DELETE' else ('NEW',)
        statements = ''.join(
            "INSERT OR IGNORE INTO invoice_project_dirty SELECT COALESCE(json_extract(data,'$.project_id'),'') "
            f'FROM expenses WHERE id={record}.item_id;' for record in records)
        db.execute(f'CREATE TRIGGER IF NOT EXISTS invoice_event_attachment_{operation.lower()} '
                   f'AFTER {operation} ON attachments BEGIN {statements} END')
    db.execute("INSERT OR IGNORE INTO invoice_project_dirty SELECT DISTINCT "
               "COALESCE(json_extract(data,'$.project_id'),'') FROM expenses")


def project_snapshot(store: ExpenseStore, db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    from invoice_print_layout.expense_projects import project_views
    from invoice_print_layout.workbench import returned_material_state
    project = next((p for p in project_views(store, db=db) if p['id'] == project_id), None)
    if project is None:
        raise ValueError('项目不存在')
    rows = db.execute("SELECT id,source,data FROM expenses WHERE json_extract(data,'$.project_id')=? "
                      'ORDER BY id', (project_id,)).fetchall()
    stored = [(row, json.loads(row['data'])) for row in rows]
    active = [row for row, data in stored if data.get('stage') == 'draft' and not data.get('deleted')
              and not data.get('merged_into') and not returned_material_state(db, data)]
    # Unreviewed or partial records need no full material/template projection.
    candidates = store._read_items(db, [row for row in active if json.loads(row['data']).get('verified')])
    verified = [item for item in candidates if item['ready']]
    versions = {item['id']: item['version'] for item in verified}
    fingerprint = hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
    previous = db.execute('SELECT version,fingerprint FROM invoice_project_state WHERE project_id=?', (project_id,)).fetchone()
    # Returning to an earlier set is a new transition, including each emptied batch.
    version = (previous['version'] if previous and previous['fingerprint'] == fingerprint else
               hashlib.sha256(((previous['version'] if previous else '') + ':' + fingerprint).encode()).hexdigest())
    return {'workspace_id': identity(store.workspace, '', 'invoice')['workspace_id'],
            'project_id': project_id, 'project_name': project['name'], 'version': version,
            'expense_ids': list(versions), 'versions': versions, 'verified_count': len(verified),
            'total_cents': sum(item['amount_cents'] for item in verified),
            'pending_count': len(active) - len(verified)}


def capture_changes(store: ExpenseStore, db: sqlite3.Connection, *, at: float | None = None) -> None:
    """Called before the business transaction commits; never performs HTTP."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='invoice_project_dirty'").fetchone():
        return
    dirty = [row[0] for row in db.execute('SELECT project_id FROM invoice_project_dirty') if row[0]]
    if not dirty:
        return
    at = time.time() if at is None else at
    for project_id in dirty:
        try:
            snapshot = project_snapshot(store, db, project_id)
        except ValueError:
            # Permanently deleted/legacy name-only projects have no package target.
            continue
        previous = db.execute('SELECT * FROM invoice_project_state WHERE project_id=?', (project_id,)).fetchone()
        summary = (f"{snapshot['project_name']}：已核对 {snapshot['verified_count']} 笔，"
                   f"合计 ¥{snapshot['total_cents'] / 100:.2f}；还有 {snapshot['pending_count']} 笔在处理")
        if previous and previous['version'] == snapshot['version']:
            db.execute("UPDATE invoice_event_outbox SET payload=json_set(payload,'$.ref.pending_count',?,'$.summary',?) "
                       "WHERE project_id=? AND status='pending' AND json_extract(payload,'$.type')='invoice.project_ready'",
                       (snapshot['pending_count'], summary, project_id))
            continue
        last_attempt = previous['last_attempt'] if previous else 0
        fingerprint = hashlib.sha256(json.dumps(snapshot['versions'], sort_keys=True).encode()).hexdigest()
        db.execute('INSERT INTO invoice_project_state (project_id,version,verified_count,last_attempt,fingerprint) VALUES (?,?,?,?,?) '
                   'ON CONFLICT(project_id) DO UPDATE SET version=excluded.version,verified_count=excluded.verified_count,'
                   'fingerprint=excluded.fingerprint',
                   (project_id, snapshot['version'], snapshot['verified_count'], last_attempt, fingerprint))
        db.execute("UPDATE invoice_event_outbox SET status='superseded' WHERE project_id=? AND status='pending' "
                   "AND json_extract(payload,'$.type')='invoice.project_ready'",
                   (project_id,))
        if not snapshot['verified_count'] and not (previous and previous['verified_count']):
            continue
        event_id = 'invoice-ready-' + hashlib.sha256(
            f"{snapshot['workspace_id']}:{project_id}:{snapshot['version']}".encode()).hexdigest()
        payload = {'id': event_id, 'source': 'invoice-workbench', 'type': 'invoice.project_ready',
                   'object': 'invoice-project:' + project_id, 'data_class': 'invoice',
                   'summary': summary,
                   'ref': snapshot, 'occurred_at': datetime.fromtimestamp(at, timezone.utc).isoformat()}
        db.execute('INSERT INTO invoice_event_outbox '
                   '(id,project_id,version,created,retry_at,status,payload) VALUES (?,?,?,?,?,?,?) '
                   "ON CONFLICT(id) DO UPDATE SET status='pending',payload=excluded.payload,retry_at=excluded.retry_at "
                   "WHERE invoice_event_outbox.status='superseded'",
                   (event_id, project_id, snapshot['version'], at, max(at, last_attempt + 60), 'pending',
                    json.dumps(payload, ensure_ascii=False)))
    db.execute('DELETE FROM invoice_project_dirty')


def event_url(store: ExpenseStore) -> str:
    url = read_json(store.root / 'project-events.json').get('url', 'http://127.0.0.1:25820/events')
    if not isinstance(url, str):
        raise ValueError('事件地址必须为本机 HTTP 地址')
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError('事件地址必须为本机 HTTP 地址')
    return url


def post_event(url: str, payload: dict[str, Any]) -> None:
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    # Loopback delivery must never enter the user's system proxy.
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
            return None
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
        result = json.loads(response.read(65537))
        if (response.status != 202 or result.get('id') != payload['id']
                or not isinstance(result.get('accepted'), bool)):
            raise ValueError('事件接收器未确认接收')


class ProjectEvents:
    def __init__(self, store: ExpenseStore, *, clock: Callable[[], float] = time.time,
                 sender: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.store, self.clock, self.sender = store, clock, sender or post_event
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_scan = 0.0
        with store.connect() as db:
            # The process instance lock ensures a previous sender is no longer alive.
            db.execute("UPDATE invoice_event_outbox SET status='pending' WHERE status='sending'")

    def kick(self) -> None:
        with self._lock:
            if self._closed.is_set() or self._thread and self._thread.is_alive():
                return
            if self.clock() < self._next_scan:
                return
            self._next_scan = self.clock() + 5
            self._thread = threading.Thread(target=self._run, daemon=True, name='invoice-project-events')
            self._thread.start()

    def _run(self) -> None:
        try:
            from invoice_print_layout.auto_verify import scan
            scan(self.store)
            # Also detect source loss/replacement that invalidates the effective review.
            with self.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO invoice_project_dirty SELECT DISTINCT "
                           "COALESCE(json_extract(data,'$.project_id'),'') FROM expenses")
            self.deliver_pending()
        except Exception:
            # Store/scan errors leave all persistent work for the next tick.
            pass

    def deliver_pending(self) -> None:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            expired = db.execute("SELECT id FROM invoice_event_outbox WHERE status='pending' AND created<=?",
                                 (self.clock() - 86400,)).fetchall()
            for row in expired:
                db.execute("UPDATE invoice_event_outbox SET status='discarded',error='expired_24h' WHERE id=?", (row['id'],))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (NULL,?,?)',
                           (datetime.now(timezone.utc).isoformat(), '项目事件超过24小时丢弃：' + row['id']))
            rows = db.execute("SELECT * FROM invoice_event_outbox WHERE status='pending' AND retry_at<=? "
                              'ORDER BY created LIMIT 20', (self.clock(),)).fetchall()
        for row in rows:
            if self._closed.is_set():
                break
            is_project = json.loads(row['payload'])['type'] == 'invoice.project_ready'
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                state = db.execute('SELECT last_attempt FROM invoice_project_state WHERE project_id=?',
                                   (row['project_id'],)).fetchone()
                if is_project and state and state['last_attempt'] and self.clock() < state['last_attempt'] + 60:
                    continue
                if not db.execute("UPDATE invoice_event_outbox SET status='sending' WHERE id=? AND status='pending'",
                                  (row['id'],)).rowcount:
                    continue
                if is_project:
                    db.execute('UPDATE invoice_project_state SET last_attempt=? WHERE project_id=?',
                               (self.clock(), row['project_id']))
            error = ''
            try:
                self.sender(event_url(self.store), json.loads(row['payload']))
            except Exception as exc:
                error = type(exc).__name__
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT version FROM invoice_project_state WHERE project_id=?',
                                     (row['project_id'],)).fetchone()
                status = ('pending' if not is_project or current and current['version'] == row['version'] else 'superseded') if error else 'sent'
                delay = min(3600, 60 * 2 ** min(row['attempts'], 6))
                db.execute("UPDATE invoice_event_outbox SET status=?,attempts=attempts+1,retry_at=?,error=? WHERE id=? AND status='sending'",
                           (status, self.clock() + delay, error, row['id']))

    def close(self) -> None:
        self._closed.set()
        if self._thread:
            self._thread.join(timeout=6)
