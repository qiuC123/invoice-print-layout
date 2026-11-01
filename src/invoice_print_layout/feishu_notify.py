"""Text-only Feishu delivery; the durable outbox is independent of mail fetching."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import tomllib
from typing import Any, Callable
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

import keyring
import pymupdf

from invoice_print_layout.documents import parse_amount
from invoice_print_layout.models import DocumentKind
from invoice_print_layout.workbench import ExpenseStore


Credentials = tuple[str, str, str]
_BASE = 'https://open.feishu.cn/open-apis/'
_CATEGORIES = {'auth': '授权失效', 'network': '网络连接异常', 'internal': '程序问题'}


def read_credentials(workspace: Path) -> Credentials:
    try:
        with (workspace / 'feishu_bot.toml').open('rb') as stream:
            value = tomllib.load(stream)['feishu']
        app, owner = value['app_id'], value['owner_open_id']
        if not isinstance(app, str) or not app.strip() or not isinstance(owner, str) or not owner.strip():
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('未找到完整的本机飞书配置') from None
    try:
        secret = keyring.get_password('invoice-print-layout-feishu', app)
    except Exception:
        secret = None
    if not secret:
        raise ValueError('未找到本机飞书应用凭据')
    return app, owner, secret


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def post_json(url: str, body: dict[str, Any], token: str = '') -> dict[str, Any]:
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(url, json.dumps(body).encode('utf-8'), headers, method='POST')
    with build_opener(_NoRedirect).open(request, timeout=10) as response:
        value = json.loads(response.read(1024 * 1024))
    if not isinstance(value, dict) or value.get('code') != 0:
        raise ValueError('飞书接口未接受请求')
    return value


class FeishuClient:
    def __init__(self, post: Callable[..., dict[str, Any]] = post_json) -> None:
        self.post = post
        self.token = ''
        self.expires_at = 0.0
        self.identity = ''

    def send(self, credentials: Credentials, text: str, identifier: str) -> None:
        app, owner, secret = credentials
        identity = hashlib.sha256((app + '\0' + secret).encode()).hexdigest()
        if identity != self.identity or time.time() >= self.expires_at:
            result = self.post(_BASE + 'auth/v3/tenant_access_token/internal',
                               {'app_id': app, 'app_secret': secret})
            token, expiry = result.get('tenant_access_token'), result.get('expire')
            if not isinstance(token, str) or not token or type(expiry) is not int or expiry <= 0:
                raise ValueError('飞书令牌不可用')
            self.token, self.identity = token, identity
            self.expires_at = time.time() + max(0, expiry - min(60, expiry / 2))
        try:
            self.post(_BASE + 'im/v1/messages?receive_id_type=open_id', {
                'receive_id': owner, 'msg_type': 'text',
                'content': json.dumps({'text': text}, ensure_ascii=False), 'uuid': identifier,
            }, self.token)
        except Exception:
            self.expires_at = 0  # Refresh after token revocation as well as ordinary expiry.
            raise


def _safe_label(value: str) -> str:
    value = re.sub(r'[\w.+-]+@[\w.-]+', '[邮箱已省略]', value)
    value = re.sub(r'(?:[A-Za-z]:[\\/]|/|\\\\)\S+', '[路径已省略]', value)
    value = re.sub(r'\d{8,}', '[号码已省略]', value)
    return ' '.join(value.split())[:100]


def mail_text(messages: list[dict[str, Any]]) -> str:
    lines = ['发票助手：收到新票']
    for message in messages:
        amounts: list[Decimal] = []
        unknown = False
        for file in message['files']:
            try:
                if Path(file['path']).suffix.lower() != '.pdf':
                    raise ValueError
                with pymupdf.open(file['path']) as document:  # type: ignore[no-untyped-call]
                    # Only the existing native-text amount parser; never OCR or model calls.
                    amounts.append(parse_amount('\n'.join(page.get_text() for page in document), DocumentKind.INVOICE))
            except Exception:
                unknown = True
        amount = ('价税合计 ¥' + format(sum(amounts, Decimal(0)), '.2f')) if amounts else ''
        if unknown or not amounts:
            amount += ('；' if amount else '') + '金额待识别'
        project = message.get('project_name') or ('需处理' if message.get('issues') else '待选项目')
        lines.append(f"{_safe_label(project)}｜附件 {len(message['files'])} 份｜{amount}｜{_safe_label(message['subject'])}")
    return '\n'.join(lines)


class FeishuNotifier:
    def __init__(self, store: ExpenseStore, enabled: Callable[[], bool], *,
                 credentials: Callable[[], Credentials] | None = None,
                 client: FeishuClient | None = None, clock: Callable[[], float] = time.time) -> None:
        self.store, self.enabled, self.clock = store, enabled, clock
        self.credentials = credentials or (lambda: read_credentials(store.workspace))
        self.client = client or FeishuClient()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_due = float('inf')
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS feishu_outbox
                    (id TEXT PRIMARY KEY, created REAL NOT NULL, retry_at REAL NOT NULL,
                     attempts INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feishu_delivery_state (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
            ''')
        self._schedule()

    def _schedule(self) -> None:
        with self.store.connect() as db:
            # Serialize with enqueue transactions so their wakeup cannot be overwritten.
            db.execute('BEGIN IMMEDIATE')
            with self._lock:
                due = db.execute('SELECT MIN(MIN(retry_at,created+86400)) FROM feishu_outbox').fetchone()[0]
                self._next_due = float('inf') if due is None else due
                if due is not None and not self.enabled():
                    self._next_due = self.clock()

    def wake(self) -> None:
        with self._lock:
            self._next_due = self.clock()

    def _result(self, db: Any, status: str, message: str) -> None:
        db.execute('INSERT OR REPLACE INTO feishu_delivery_state VALUES (1,?)',
                   (json.dumps({'status': status, 'message': message, 'at': self.clock()}),))

    def status(self) -> dict[str, Any]:
        try:
            self.credentials()
            available, reason = True, ''
        except Exception as error:
            available = False
            reason = str(error) if str(error) in {'未找到完整的本机飞书配置', '未找到本机飞书应用凭据'} else '本机飞书凭据不可用'
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM feishu_delivery_state WHERE id=1').fetchone()
        return {'available': available, 'enabled': available and self.enabled(), 'reason': reason,
                'last_result': json.loads(row['data']) if row else None}

    def enqueue(self, identifier: str, payload: dict[str, Any]) -> bool:
        if not self.enabled():
            return False
        with self.store.connect() as db:
            return self._insert(db, identifier, payload)

    def _insert(self, db: Any, identifier: str, payload: dict[str, Any]) -> bool:
        identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, 'invoice-feishu:' + identifier))
        inserted = db.execute('INSERT OR IGNORE INTO feishu_outbox VALUES (?,?,?,?,?)',
                             (identifier, self.clock(), self.clock(), 0, json.dumps(payload))).rowcount
        if inserted:
            self._result(db, 'queued', '提醒已加入待发队列')
            self.wake()
        return bool(inserted)

    def mail_batch(self, account: str) -> None:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT data FROM mail_agent_messages WHERE account_id=? "
                              "AND json_extract(data,'$.notify_pending')=1 ORDER BY rowid LIMIT 20", (account,)).fetchall()
            pending = [json.loads(row['data']) for row in rows]
            if not pending:
                return
            if self.enabled():
                self._insert(db, 'mail:' + ':'.join(sorted(m['id'] for m in pending)), {'messages': pending})
            for message in pending:
                message['notify_pending'] = False
                db.execute('UPDATE mail_agent_messages SET data=? WHERE id=?', (json.dumps(message), message['id']))

    def failure(self, account: str, episode: str, phase: str, category: str = '', *, db: Any = None) -> bool:
        text = {'first': '自动收件出错：' + _CATEGORIES.get(category, '程序问题'),
                'recovered': '自动收件已恢复'}[phase]
        identifier = f'{account}:{episode}:{phase}'
        payload = {'text': '发票助手：' + text}
        if db is not None:
            if self.enabled():
                return self._insert(db, identifier, payload)
            return False
        return self.enqueue(identifier, payload)

    def test_message(self) -> dict[str, Any]:
        status = self.status()
        if not status['available']:
            raise ValueError(status['reason'])
        if not self.enabled():
            raise ValueError('请先开启飞书提醒')
        self.enqueue('test:' + uuid.uuid4().hex, {'text': '发票助手：飞书文字提醒测试消息'})
        self.kick()
        return self.status()

    def kick(self) -> None:
        with self._lock:
            if (self._closed.is_set() or self.clock() < self._next_due
                    or self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._run, daemon=True, name='invoice-feishu-notify')
            self._thread.start()

    def _run(self) -> None:
        try:
            self.deliver_pending()
        except Exception:
            # Local DB failures leave the outbox intact for the next service tick.
            pass

    def deliver_pending(self) -> None:
        try:
            self._deliver_pending()
        finally:
            self._schedule()

    def _deliver_pending(self) -> None:
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM feishu_outbox ORDER BY created').fetchall()
        for row in rows:
            if self._closed.is_set():
                return
            now = self.clock()
            if now - row['created'] >= 86400 or not self.enabled():
                with self.store.connect() as db:
                    db.execute('DELETE FROM feishu_outbox WHERE id=?', (row['id'],))
                    self._result(db, 'discarded', '提醒超过24小时已丢弃' if self.enabled() else '提醒已关闭，待发提醒已清除')
                continue
            if row['retry_at'] > now:
                continue
            try:
                credentials = self.credentials()
                payload = json.loads(row['payload'])
                text = payload.get('text') or mail_text(payload['messages'])
                if self._closed.is_set() or not self.enabled():
                    return
                self.client.send(credentials, text, row['id'])
            except Exception:
                with self.store.connect() as db:
                    db.execute('UPDATE feishu_outbox SET attempts=attempts+1,retry_at=? WHERE id=?',
                               (now + min(3600, 60 * 2 ** min(row['attempts'], 6)), row['id']))
                    self._result(db, 'retrying', '飞书发送未完成，将自动重试')
            else:
                with self.store.connect() as db:
                    db.execute('DELETE FROM feishu_outbox WHERE id=?', (row['id'],))
                    self._result(db, 'sent', '飞书文字提醒已发送')

    def close(self) -> None:
        self._closed.set()
        if self._thread:
            self._thread.join(22)
