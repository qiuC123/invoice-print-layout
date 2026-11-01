"""Paired receipt Apps share the desktop intake queue and organizer."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import secrets
import socket
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlparse
import uuid

from invoice_print_layout import phone_link
from invoice_print_layout.agent_jobs import AgentService
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_background import BackgroundIntake
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.receipt_intake import validate_image
from invoice_print_layout.reimbursement_people import ReimbursementPeople, validate_person
from invoice_print_layout.workbench import CATEGORIES, now

MAX_BODY = 29 * 1024 * 1024
PAIR_SECONDS = 600
SOCKET_TIMEOUT = 10.0
DISPATCH_RETRY_SECONDS = 15
ADDRESS_REFRESH_SECONDS = 300


class DeviceError(ValueError):
    def __init__(self, message: str, code: str = 'invalid', status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def allowed_source(address: str) -> bool:
    return address == '127.0.0.1' or phone_link.lan_address(address)


class DeviceServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = False


class ReceiptDevices:
    def __init__(self, intake: IntakeQueue, background: BackgroundIntake, agent: AgentService,
                 write_lock: threading.Lock, *, port: int = 8769):
        self.intake, self.store = intake, intake.store
        self.background, self.agent, self.write_lock = background, agent, write_lock
        self.organize = OrganizeSettings(self.store.root)
        self.projects, self.people = ExpenseProjects(self.store), ReimbursementPeople(self.store)
        self.port = port
        self.server: DeviceServer | None = None
        self.worker: threading.Thread | None = None
        self.dispatch_worker: threading.Thread | None = None
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.Lock()
        self.closed = threading.Event()
        self.error = ''
        self.pairing_expires_at = 0.0
        self.listener_dirty = False
        self.confirmation_due: float | None = None
        self.addresses: tuple[str, ...] = ()
        self.address_lock = threading.Lock()
        self.address_worker: threading.Thread | None = None
        self.address_refresh_at = 0.0
        with self.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS receipt_devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
                paired_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '')''')
            db.execute('''CREATE TABLE IF NOT EXISTS receipt_device_uploads (
                device_id TEXT NOT NULL, client_id TEXT NOT NULL, entry_id TEXT NOT NULL,
                status TEXT NOT NULL, dispatch_state TEXT NOT NULL,
                PRIMARY KEY(device_id,client_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS receipt_device_pairing (
                id INTEGER PRIMARY KEY CHECK(id=1), code TEXT NOT NULL,
                expires_at REAL NOT NULL, failures INTEGER NOT NULL)''')
            db.execute("INSERT OR IGNORE INTO receipt_device_pairing VALUES (1,'',0,0)")
            columns = {row['name'] for row in db.execute('PRAGMA table_info(receipt_devices)')}
            for column, definition in (('confirmed', 'INTEGER NOT NULL DEFAULT 1'),
                                       ('confirmation_expires_at', 'REAL NOT NULL DEFAULT 0')):
                if column not in columns:
                    db.execute(f'ALTER TABLE receipt_devices ADD COLUMN {column} {definition}')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(receipt_device_uploads)')}
            for column, definition in (('qr_checked', 'INTEGER NOT NULL DEFAULT 0'),
                                       ('retry_at', 'REAL NOT NULL DEFAULT 0')):
                if column not in columns:
                    db.execute(f'ALTER TABLE receipt_device_uploads ADD COLUMN {column} {definition}')
            self.dispatch_due: float | None = db.execute(
                "SELECT MIN(retry_at) FROM receipt_device_uploads WHERE dispatch_state='pending'").fetchone()[0]
        self.refresh_addresses()

    def refresh_addresses(self, *, force: bool = False) -> None:
        with self.address_lock:
            if self.closed.is_set() or self.address_worker and self.address_worker.is_alive():
                return
            if not force and time.time() < self.address_refresh_at:
                return
            self.address_refresh_at = time.time() + ADDRESS_REFRESH_SECONDS
            self.address_worker = threading.Thread(target=self._refresh_addresses, name='receipt-addresses', daemon=True)
            self.address_worker.start()

    def _refresh_addresses(self) -> None:
        try:
            addresses = tuple(row['address'] for row in phone_link.addresses() if phone_link.lan_address(row['address']))
        except OSError:
            return  # Keep the last usable addresses until the next background refresh.
        with self.address_lock:
            self.addresses = addresses

    def urls(self, local_address: str = '') -> list[str]:
        server = self.server
        port = server.server_port if server else self.port
        addresses = self.addresses or ((local_address,) if allowed_source(local_address) else ())
        return [f'http://{address}:{port}' for address in addresses]

    def status(self) -> dict[str, Any]:
        with self.lock, self.store.connect() as db:
            devices = [dict(row) for row in db.execute(
                'SELECT id,name,paired_at,last_seen_at,revoked_at,confirmed FROM receipt_devices ORDER BY rowid')]
            attention = db.execute("SELECT COUNT(*) FROM receipt_device_uploads WHERE dispatch_state='attention'").fetchone()[0]
            pairing = db.execute('SELECT * FROM receipt_device_pairing WHERE id=1').fetchone()
            server = self.server
            value: dict[str, Any] = {'enabled': server is not None, 'port': server.server_port if server else self.port,
                                     'urls': self.urls(), 'devices': devices, 'error': self.error,
                                     'expires_in': max(0, int(pairing['expires_at'] - time.time())),
                                     'attention_count': attention}
            if pairing['code'] and pairing['expires_at'] > time.time():
                import qrcode
                from qrcode.image.pil import PilImage
                pair_uri = 'invoicepair:1?' + urlencode([('code', pairing['code']), *[('url', url) for url in value['urls']]])
                output = io.BytesIO()
                qrcode.make(pair_uri, image_factory=PilImage, box_size=6, border=4).save(output, format='PNG')
                value.update(code=pairing['code'], pair_uri=pair_uri,
                             qr='data:image/png;base64,' + base64.b64encode(output.getvalue()).decode())
            return value

    def start_pairing(self) -> dict[str, Any]:
        with self.lock, self.store.connect() as db:
            if self.closed.is_set():
                raise ValueError('收票 App 服务正在关闭')
            db.execute('UPDATE receipt_device_pairing SET code=?,expires_at=?,failures=0 WHERE id=1',
                       (f'{secrets.randbelow(100_000_000):08d}', time.time() + PAIR_SECONDS))
        self.sync_listener()
        return self.status()

    def pair(self, body: dict[str, Any], local_address: str = '') -> dict[str, Any]:
        name, code = body.get('device_name'), body.get('code')
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise DeviceError('请填写60字以内的手机名称')
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            pairing = db.execute('SELECT * FROM receipt_device_pairing WHERE id=1').fetchone()
            valid = (pairing['code'] and time.time() < pairing['expires_at'] and isinstance(code, str) and code.isascii()
                     and secrets.compare_digest(code, pairing['code']))
            if valid:
                key, device_id = secrets.token_urlsafe(32), uuid.uuid4().hex
                expires = time.time() + PAIR_SECONDS
                db.execute('''INSERT INTO receipt_devices
                    (id,name,key_hash,paired_at,last_seen_at,revoked_at,confirmed,confirmation_expires_at)
                    VALUES (?,?,?,?,?,?,0,?)''',
                           (device_id, name.strip(), hashlib.sha256(key.encode()).hexdigest(), now(), '', '', expires))
                db.execute("UPDATE receipt_device_pairing SET code='',expires_at=0 WHERE id=1")
                self.confirmation_due = min(self.confirmation_due or expires, expires)
                # Leave the current pairing window open for the first authenticated request.
            else:
                db.execute('UPDATE receipt_device_pairing SET failures=failures+1 WHERE id=1')
                if pairing['failures'] >= 4:
                    db.execute("UPDATE receipt_device_pairing SET code='',expires_at=0 WHERE id=1")
                if pairing['failures'] >= 4 or pairing['expires_at'] <= time.time():
                    # Let the desktop tick close the listener outside this HTTP request thread.
                    self.pairing_expires_at = time.time()
        if not valid:
            raise DeviceError('配对码错误、已过期或已使用，请在电脑重新配对', 'bad_code')
        return {'device_id': device_id, 'device_key': key, 'server_name': socket.gethostname(), 'urls': self.urls(local_address)}

    def authenticate(self, authorization: str) -> str:
        if not authorization.startswith('Bearer ') or not authorization[7:]:
            raise DeviceError('手机尚未配对或已撤销，请重新配对', 'unpaired', 401)
        with self.lock, self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("""SELECT id,confirmed FROM receipt_devices WHERE key_hash=? AND revoked_at=''
                AND (confirmed=1 OR confirmation_expires_at>?)""",
                             (hashlib.sha256(authorization[7:].encode()).hexdigest(), time.time())).fetchone()
            if not row:
                raise DeviceError('手机尚未配对或已撤销，请重新配对', 'unpaired', 401)
            db.execute('UPDATE receipt_devices SET last_seen_at=?,confirmed=1,confirmation_expires_at=0 WHERE id=?',
                       (now(), row['id']))
            if not row['confirmed']:
                self.listener_dirty = True
            return str(row['id'])

    def revoke(self, device_id: Any) -> dict[str, Any]:
        if not isinstance(device_id, str):
            raise ValueError('设备不存在')
        with self.write_lock, self.lock, self.store.connect() as db:
            if not db.execute('SELECT 1 FROM receipt_devices WHERE id=?', (device_id,)).fetchone():
                raise ValueError('设备不存在')
            db.execute("UPDATE receipt_devices SET revoked_at=? WHERE id=? AND revoked_at=''", (now(), device_id))
        self.sync_listener()
        return self.status()

    def catalog(self, local_address: str = '') -> dict[str, Any]:
        projects = sorted((p for p in self.projects.list() if p['reimbursement_status'] == 'unreimbursed'),
                          key=lambda p: not p['pinned'])
        return {'server_time': datetime.now().astimezone().isoformat(timespec='seconds'), 'urls': self.urls(local_address),
                'categories': list(CATEGORIES), 'projects': [
                    {**{key: p.get(key, '') for key in ('id', 'name', 'start_date', 'end_date', 'main_person_id')},
                     'people': [{'id': person['id'], 'label': person['label']} for person in self.people.list(p['id'])]}
                    for p in projects]}

    def upload(self, device_id: str, body: dict[str, Any]) -> dict[str, str]:
        client_id = body.get('client_id')
        try:
            if not isinstance(client_id, str):
                raise ValueError
            uuid.UUID(client_id)
        except ValueError as exc:
            raise DeviceError('上传编号须为 UUID') from exc
        with self.write_lock:
            with self.store.connect() as db:
                if not db.execute("SELECT 1 FROM receipt_devices WHERE id=? AND revoked_at=''", (device_id,)).fetchone():
                    raise DeviceError('手机已撤销，请重新配对', 'unpaired', 401)
                prior = db.execute('SELECT status,entry_id FROM receipt_device_uploads WHERE device_id=? AND client_id=?',
                                   (device_id, client_id)).fetchone()
                if prior:
                    return dict(prior)
            project_id, person_id = body.get('project_id'), body.get('person_id')
            if not isinstance(project_id, str) or not any(
                    p['id'] == project_id and p['reimbursement_status'] == 'unreimbursed' for p in self.projects.list()):
                raise DeviceError('项目已不可用，请重新选择', 'project_unavailable', 409)
            if not isinstance(person_id, str):
                raise DeviceError('所属人已不可用，请重新选择', 'person_unavailable', 409)
            with self.store.connect() as db:
                try:
                    person = validate_person(db, project_id, person_id)
                    if person_id and person['reimbursement_person_id'] != person_id:
                        raise ValueError('人员已被合并')
                except ValueError as exc:
                    raise DeviceError('所属人已不可用，请重新选择', 'person_unavailable', 409) from exc
            amount, category, note = body.get('amount_cents'), body.get('category'), body.get('note', '')
            name, data, captured = body.get('name'), body.get('data'), body.get('captured_at')
            if amount is not None and (type(amount) is not int or not 0 <= amount <= 99_999_999_900):
                raise DeviceError('金额须为非负整数分或 null')
            if category not in CATEGORIES:
                raise DeviceError('类别不在可选列表中')
            if not isinstance(note, str) or len(note) > 200:
                raise DeviceError('备注须为200字以内的文字')
            if not isinstance(name, str) or not name.strip() or len(name) > 255 or not isinstance(data, str):
                raise DeviceError('图片名称或内容无效')
            try:
                if not isinstance(captured, str) or len(captured) > 40:
                    raise ValueError
                timestamp = datetime.fromisoformat(captured)
                if timestamp.tzinfo is not None or 'T' not in captured:
                    raise ValueError
                payload = base64.b64decode(data, validate=True)
                validate_image(name, payload)
            except (ValueError, OSError) as exc:
                raise DeviceError('图片无法解码或拍摄时间无效') from exc
            confirmed = {'category': category}
            if amount is not None:
                confirmed['amount'] = f'{amount // 100}.{amount % 100:02d}'
            entry = self.intake.receive(name, payload, source='upload', project_id=project_id,
                                        batch='device_' + device_id + '_' + client_id,
                                        reimbursement_person_id=person_id, confirmed_fields=confirmed,
                                        intake_note=note.strip(), captured_at=captured,
                                        device_upload=(device_id, client_id))
            result = {'status': 'duplicate' if entry.get('duplicate') or entry['status'] == 'duplicate' else 'received',
                      'entry_id': entry['id']}
        with self.lock:
            self.dispatch_due = 0.0
        self.kick_dispatch()
        return result

    def sync_listener(self) -> None:
        with self.lifecycle_lock:
            with self.lock, self.store.connect() as db:
                db.execute('DELETE FROM receipt_devices WHERE confirmed=0 AND confirmation_expires_at<=?', (time.time(),))
                self.confirmation_due = db.execute(
                    'SELECT MIN(confirmation_expires_at) FROM receipt_devices WHERE confirmed=0').fetchone()[0]
                pairing = db.execute('SELECT expires_at FROM receipt_device_pairing WHERE id=1').fetchone()
                self.listener_dirty = False
                self.pairing_expires_at = pairing['expires_at'] if pairing['expires_at'] > time.time() else 0.0
                wanted = not self.closed.is_set() and (pairing['expires_at'] > time.time() or bool(db.execute(
                    "SELECT 1 FROM receipt_devices WHERE confirmed=1 OR revoked_at!='' LIMIT 1").fetchone()))
            if wanted and self.server is None:
                try:
                    self.server = DeviceServer(('0.0.0.0', self.port), self._handler())
                    self.worker = threading.Thread(target=self.server.serve_forever, name='receipt-devices', daemon=True)
                    self.worker.start()
                    self.refresh_addresses(force=True)
                    self.error = ''
                except OSError:
                    self.error = f'收票 App 端口 {self.port} 无法监听，请检查端口占用后重新配对或重启'
            elif not wanted and self.server is not None:
                server, worker = self.server, self.worker
                self.server, self.worker = None, None
                server.shutdown()
                server.server_close()
                if worker:
                    worker.join()

    def tick(self) -> None:
        if self.closed.is_set():
            return
        if (self.listener_dirty or self.pairing_expires_at and self.pairing_expires_at <= time.time()
                or self.confirmation_due is not None and self.confirmation_due <= time.time()):
            self.sync_listener()
        if self.server is not None:  # Enumerating adapters costs ~5 s of PowerShell; skip while closed.
            self.refresh_addresses()
        self.kick_dispatch()

    def kick_dispatch(self) -> None:
        with self.lock:
            if self.closed.is_set() or self.dispatch_worker and self.dispatch_worker.is_alive():
                return
            if self.dispatch_due is None or self.dispatch_due > time.time():
                return
            self.dispatch_due = None
            self.dispatch_worker = threading.Thread(target=self._dispatch_pending, name='receipt-dispatch', daemon=True)
            self.dispatch_worker.start()

    def _dispatch_pending(self) -> None:
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM receipt_device_uploads WHERE dispatch_state='pending' AND retry_at<=? ORDER BY rowid",
                              (time.time(),)).fetchall()
        for row in rows:
            if self.closed.is_set():
                return
            state = 'done'
            try:
                entry = self.intake.get(row['entry_id'])
                if entry['status'] not in {'received', 'review', 'converted'} or entry.get('item_id') or entry.get('duplicate_of'):
                    continue
                if not any(p['id'] == entry['project_id'] and p['reimbursement_status'] == 'unreimbursed'
                           for p in self.projects.list()):
                    state = 'attention'
                    continue
                if self.organize.read()['auto_after_upload']:
                    request_id = 'device_' + hashlib.sha256((row['device_id'] + row['client_id']).encode()).hexdigest()
                    if row['retry_at']:
                        with self.store.connect() as db:
                            busy = db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND state IN ('queued','running')",
                                              (entry['project_id'],)).fetchone()
                            prior = db.execute('SELECT job_id FROM invoice_agent_requests WHERE id=?', (request_id,)).fetchone()
                        if busy and not prior:
                            state = 'pending'
                            continue
                    if not row['qr_checked']:
                        entry = self.intake.claim_batch_qr(entry['id'])
                        with self.store.connect() as db:
                            db.execute('UPDATE receipt_device_uploads SET qr_checked=1 WHERE device_id=? AND client_id=?',
                                       (row['device_id'], row['client_id']))
                    elif entry.get('qr_status') == 'converted' and entry.get('replacement_id'):
                        entry = self.intake.get(entry['replacement_id'])
                    if entry.get('qr_status') == 'manual':
                        state = 'attention'
                    elif entry['status'] in {'received', 'review'} and not entry.get('item_id') and not entry.get('duplicate_of'):
                        with self.write_lock:
                            if not any(p['id'] == entry['project_id'] and p['reimbursement_status'] == 'unreimbursed'
                                       for p in self.projects.list()):
                                state = 'attention'
                                continue
                            with self.store.connect() as db:
                                prior = db.execute('SELECT job_id FROM invoice_agent_requests WHERE id=?', (request_id,)).fetchone()
                                busy = db.execute("SELECT 1 FROM invoice_agent_jobs WHERE project_id=? AND state IN ('queued','running')",
                                                  (entry['project_id'],)).fetchone()
                            if prior:
                                pass
                            elif busy:
                                state = 'pending'
                            else:
                                self.agent.jobs.create(entry['project_id'], [entry['id']], request_id,
                                                       use_model=self.organize.effective_model(
                                                           True, self.intake.ai.public_status()['available']))
                        self.agent.kick()
                else:
                    self.background.enqueue(entry['batch'], [entry['id']])
            except (ValueError, OSError, sqlite3.Error):
                state = 'attention'  # Saved originals remain actionable in the existing desk.
            finally:
                with self.store.connect() as db:
                    db.execute('UPDATE receipt_device_uploads SET dispatch_state=?,retry_at=? WHERE device_id=? AND client_id=?',
                               (state, time.time() + DISPATCH_RETRY_SECONDS if state == 'pending' else 0,
                                row['device_id'], row['client_id']))
        with self.lock, self.store.connect() as db:
            self.dispatch_due = db.execute(
                "SELECT MIN(retry_at) FROM receipt_device_uploads WHERE dispatch_state='pending'").fetchone()[0]

    def close(self) -> None:
        self.closed.set()
        self.sync_listener()
        if self.dispatch_worker:
            self.dispatch_worker.join()
        if self.address_worker:
            self.address_worker.join()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        controller = self

        class Handler(BaseHTTPRequestHandler):
            device_id = ''

            def setup(self) -> None:
                self.request.settimeout(SOCKET_TIMEOUT)
                super().setup()

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def parse_request(self) -> bool:
                if not super().parse_request():
                    return False
                try:
                    if 'Origin' in self.headers or not allowed_source(self.client_address[0]):
                        raise DeviceError('请求来源无效', 'forbidden', 403)
                    if not (self.command == 'POST' and urlparse(self.path).path == '/device/v1/pair'):
                        self.device_id = controller.authenticate(self.headers.get('Authorization', ''))
                except DeviceError as exc:
                    if self.command == 'POST':
                        phone_link.drain_rejected_body(self)
                    self.respond({'error': str(exc), 'code': exc.code}, exc.status)
                    return False
                return True

            def respond(self, value: Any, status: int = 200) -> None:
                payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            def handle_device(self) -> None:
                body_read = self.command != 'POST'
                try:
                    path = urlparse(self.path).path
                    pairing = path == '/device/v1/pair' and self.command == 'POST'
                    if self.command == 'GET' and path == '/device/v1/catalog':
                        self.respond(controller.catalog(self.connection.getsockname()[0]))
                        return
                    if self.command != 'POST' or path not in {'/device/v1/pair', '/device/v1/receipts'}:
                        raise DeviceError('接口不存在', 'not_found', 404)
                    if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                        raise DeviceError('请求长度无效')
                    length = int(self.headers.get('Content-Length', '0'))
                    if length > (4096 if pairing else MAX_BODY):
                        raise DeviceError('请求体过大', 'too_large', 413)
                    if length <= 0:
                        raise DeviceError('请求体不能为空')
                    raw = self.rfile.read(length)
                    body_read = True
                    if len(raw) != length:
                        raise DeviceError('请求体不完整')
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise DeviceError('请求须为 JSON 对象')
                    self.respond(controller.pair(body, self.connection.getsockname()[0]) if pairing
                                 else controller.upload(self.device_id, body))
                except DeviceError as exc:
                    if not body_read:
                        phone_link.drain_rejected_body(self)
                    self.respond({'error': str(exc), 'code': exc.code}, exc.status)
                except (ValueError, UnicodeError, OSError, TypeError, RecursionError):
                    self.respond({'error': '请求内容无效，请检查后重试', 'code': 'invalid'}, 400)
                except sqlite3.Error:
                    self.respond({'error': '账本暂时不可用，原件若已保存可用同一编号重试', 'code': 'unavailable'}, 503)

            def do_GET(self) -> None:
                self.handle_device()

            def do_POST(self) -> None:
                self.handle_device()

        return Handler
