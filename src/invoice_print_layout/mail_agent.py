"""Account-scoped, durable mail intake feeding the existing bounded Agent.

The poller never searches an old intake directory, chooses the browser's current
project, or verifies/submits an expense. Every remote attachment is durable before
its mailbox cursor advances; Agent request keys make crash recovery idempotent.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from invoice_print_layout.agent_jobs import AgentService, encoded
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.mail163 import Mail163Settings, read_auth_code, read_mail_settings
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.workbench import ExpenseStore

Fetcher = Callable[..., dict[str, Any]]
_ZONE = timezone(timedelta(hours=8))
_RETRY_LIMIT = 3


def _today() -> str:
    return datetime.now(_ZONE).date().isoformat()


def _hash(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def _atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class MailAgentService:
    """Short asynchronous polling cycles; kick is safe in server_actions."""

    def __init__(self, store: ExpenseStore, agent: AgentService, *, fetcher: Fetcher | None = None):
        self.store = store
        self.agent = agent
        self.config_path = store.root / 'mail-agent.json'
        self.organize = OrganizeSettings(store.root)
        self.originals = store.root / '邮件Agent原件'
        self.originals.mkdir(exist_ok=True)
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._force = False
        self._dispatch_at = 0.0
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mail_agent_accounts
                    (id TEXT PRIMARY KEY, address TEXT NOT NULL, folder TEXT NOT NULL,
                     uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mail_agent_messages
                    (id TEXT PRIMARY KEY, account_id TEXT NOT NULL, uidvalidity TEXT NOT NULL,
                     uid INTEGER NOT NULL, data TEXT NOT NULL,
                     UNIQUE(account_id,uidvalidity,uid));
            ''')

    def _settings(self) -> Mail163Settings:
        return read_mail_settings(self.store.workspace / 'mail163.toml')

    def _config(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            'enabled': False, 'interval_seconds': 120, 'since': _today(),
            'approved_since': _today(), 'use_model': self.organize.read()['use_model'], 'default_project_id': '',
            'default_project_account': '',
        }
        if not self.config_path.exists():
            return defaults
        try:
            value = json.loads(self.config_path.read_text(encoding='utf-8'))
            if not isinstance(value, dict):
                raise ValueError('invalid config')
            defaults.update(value)
            defaults['use_model'] = self.organize.read()['use_model']
            self._validate_config(defaults)
            return defaults
        except (OSError, ValueError, TypeError):
            defaults.update(enabled=False, configuration_error='自动收件配置无法读取，请重新保存设置')
            return defaults

    @staticmethod
    def _validate_config(value: dict[str, Any]) -> None:
        if (type(value.get('enabled')) is not bool or type(value.get('use_model')) is not bool or
                type(value.get('interval_seconds')) is not int or
                not 60 <= value['interval_seconds'] <= 3600 or
                not isinstance(value.get('default_project_id'), str)):
            raise ValueError('自动收件设置无效')
        for key in ('since', 'approved_since'):
            if not isinstance(value.get(key), str) or date.fromisoformat(value[key]).isoformat() != value[key]:
                raise ValueError('收件起始日期无效')
        if value['since'] < value['approved_since']:
            raise ValueError('收件日期不能早于已批准的起始日期')

    def configure(self, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {'enabled', 'interval_seconds', 'since', 'use_model', 'default_project_id'}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError('自动收件设置无效')
        with self._lock:
            value = self._config()
            value.pop('configuration_error', None)
            value.update(body)
            try:
                settings = self._settings()
            except Exception:
                if value['enabled'] or body.get('default_project_id'):
                    raise ValueError('请先配置可用的163邮箱') from None
                settings = None
            if settings is not None:
                account = self._account_key(settings)
                if 'default_project_id' not in body and value.get('default_project_account') != account:
                    value['default_project_id'] = ''
                value['default_project_account'] = account
            self._validate_config(value)
            projects = ExpenseProjects(self.store).list()
            if value['default_project_id'] and not any(p['id'] == value['default_project_id'] for p in projects):
                raise ValueError('请选择有效默认项目，或留空自动判断')
            if 'use_model' in body:
                self.organize.update({'use_model': body['use_model']})
            _atomic(self.config_path, encoded({k: v for k, v in value.items() if k != 'use_model'}).encode('utf-8'))
            self._force = bool(value['enabled'])
        self.kick()
        return self.status()

    @staticmethod
    def _account_key(settings: Mail163Settings) -> str:
        return _hash(['imap.163.com', settings.address, settings.folder])

    def _scoped_config(self, config: dict[str, Any], settings: Mail163Settings) -> dict[str, Any]:
        if config.get('default_project_account') != self._account_key(settings):
            return {**config, 'default_project_id': ''}
        return config

    def _account(self, settings: Mail163Settings, config: dict[str, Any]) -> dict[str, Any]:
        key = self._account_key(settings)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM mail_agent_accounts WHERE id=?', (key,)).fetchone()
            if row:
                current = {**dict(row), **json.loads(row['data'])}
                if current['since'] != config['since']:
                    state = json.loads(row['data'])
                    state.update(since=config['since'], next_check_at=0.0)
                    db.execute('UPDATE mail_agent_accounts SET last_uid=0,data=? WHERE id=?', (encoded(state), key))
                    current.update(state, last_uid=0)
                return current
            state = {'since': config['since'], 'last_checked_at': None, 'last_error': '',
                     'next_check_at': 0.0, 'failures': 0, 'suspended': False}
            db.execute('INSERT INTO mail_agent_accounts VALUES (?,?,?,?,?,?)',
                       (key, settings.address, settings.folder, '', 0, encoded(state)))
        return {'id': key, 'address': settings.address, 'folder': settings.folder,
                'uidvalidity': '', 'last_uid': 0, **state}

    def _account_update(self, key: str, **changes: Any) -> None:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM mail_agent_accounts WHERE id=?', (key,)).fetchone()
            if row is None:
                return
            state = json.loads(row['data'])
            state.update({k: v for k, v in changes.items() if k not in {'last_uid', 'uidvalidity'}})
            db.execute('UPDATE mail_agent_accounts SET last_uid=?,uidvalidity=?,data=? WHERE id=?',
                       (changes.get('last_uid', row['last_uid']), changes.get('uidvalidity', row['uidvalidity']),
                        encoded(state), key))

    def _messages(self, account: str = '') -> list[dict[str, Any]]:
        with self.store.connect() as db:
            rows = db.execute('SELECT data FROM mail_agent_messages ' +
                              ('WHERE account_id=? ' if account else '') + 'ORDER BY rowid',
                              (account,) if account else ()).fetchall()
        return [json.loads(row['data']) for row in rows]

    def _message(self, key: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM mail_agent_messages WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('邮件记录不存在')
        result: dict[str, Any] = json.loads(row['data'])
        return result

    def _message_update(self, key: str, **changes: Any) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM mail_agent_messages WHERE id=?', (key,)).fetchone()
            if row is None:
                raise ValueError('邮件记录不存在')
            message: dict[str, Any] = json.loads(row['data'])
            message.update(changes)
            db.execute('UPDATE mail_agent_messages SET data=? WHERE id=?', (encoded(message), key))
        return message

    def status(self) -> dict[str, Any]:
        config = self._config()
        account: dict[str, Any] = {}
        address = folder = ''
        error = config.get('configuration_error', '')
        try:
            settings = self._settings()
            config = self._scoped_config(config, settings)
            address, folder = settings.address, settings.folder
            key = self._account_key(settings)
            with self.store.connect() as db:
                row = db.execute('SELECT data FROM mail_agent_accounts WHERE id=?', (key,)).fetchone()
            account = json.loads(row['data']) if row else {}
            messages = [m for m in self._messages(key) if m['status'] != 'ignored'][::-1]
        except Exception:
            messages = []
            error = error or '尚未配置可用的163邮箱'
        visible = []
        for message in messages:
            state = message['status']
            if message.get('job_id'):
                try:
                    state = self.agent.jobs.get(message['job_id'])['state']
                except ValueError:
                    state = 'attention'
            visible.append({k: message.get(k, '') for k in (
                'id', 'subject', 'sender', 'issues', 'project_id', 'job_id', 'received_at', 'sent_date', 'route_reason')} |
                           {'status': state, 'material_count': len(message.get('files', [])),
                            'route_dates': message.get('route_dates', []),
                            'material_ids': message.get('material_ids', [])})
        unresolved = {'unassigned', 'attention', 'waiting_agent', 'queued', 'running', 'needs_input', 'failed', 'stopped'}
        unresolved_count = sum(m['status'] in unresolved for m in visible)
        # A stream of recent ready/duplicate notices cannot hide a missing-fact or
        # project-selection action. Keep recency order within each priority band.
        visible.sort(key=lambda m: m['status'] not in unresolved)
        has_more = len(visible) > 30
        visible = visible[:30]
        return {k: config[k] for k in ('enabled', 'interval_seconds', 'since', 'approved_since', 'use_model', 'default_project_id')} | {
            'address': address, 'folder': folder,
            'last_checked_at': account.get('last_checked_at'),
            'last_error': error or account.get('last_error', ''),
            'next_check_at': account.get('next_check_at'),
            'suspended': bool(account.get('suspended')), 'collecting': bool(self._thread and self._thread.is_alive()),
            'messages': visible, 'pending': visible, 'unresolved_count': unresolved_count, 'has_more': has_more,
        }

    def collect_now(self) -> dict[str, Any]:
        if not self._config()['enabled']:
            raise ValueError('请先开启自动收件')
        with self._lock:
            self._force = True
        self.kick()
        return self.status()

    def kick(self) -> None:
        with self._lock:
            if self._closed.is_set() or (self._thread is not None and self._thread.is_alive()):
                return
            config = self._config()
            if not config['enabled']:
                return
            try:
                account = self._account(self._settings(), config)
            except Exception:
                return
            network = self._force or (not account.get('suspended') and time.time() >= account['next_check_at'])
            if not network and time.time() < self._dispatch_at:
                return
            self._force = False
            self._dispatch_at = time.time() + 3
            self._thread = threading.Thread(target=self._run, args=(network,), daemon=True, name='invoice-mail-agent')
            self._thread.start()

    def _run(self, network: bool) -> None:
        config = self._config()
        if not config['enabled'] or self._closed.is_set():
            return
        try:
            settings = self._settings()
            config = self._scoped_config(config, settings)
            account = self._account(settings, config)
        except Exception:
            return
        if network:
            try:
                if self._fetcher is None:
                    from invoice_print_layout.mail_agent_fetch import fetch_with_deadline
                    result = fetch_with_deadline(settings, since=date.fromisoformat(account['since']),
                                                 after_uid=account['last_uid'], uidvalidity=account['uidvalidity'],
                                                 limit=20, stop=self._closed)
                else:
                    auth_code = read_auth_code(settings.address)
                    try:
                        result = self._fetcher(settings, auth_code, since=date.fromisoformat(account['since']),
                                               after_uid=account['last_uid'], uidvalidity=account['uidvalidity'], limit=20)
                    finally:
                        auth_code = ''
                if self._settings() != settings or not self._config()['enabled']:
                    return  # A concurrent account switch cannot send old results to a new account.
                epoch = str(result.get('uidvalidity', ''))
                if not epoch:
                    raise ValueError('missing UIDVALIDITY')
                if epoch != account['uidvalidity']:
                    self._account_update(account['id'], uidvalidity=epoch, last_uid=0)
                    account.update(uidvalidity=epoch, last_uid=0)
                for remote in sorted(result.get('messages', []), key=lambda m: m['uid']):
                    if self._closed.is_set():
                        return
                    if type(remote.get('uid')) is not int or remote['uid'] <= 0:
                        raise ValueError('invalid message UID')
                    self._persist_message(account, remote, config)
                    self._account_update(account['id'], last_uid=max(account['last_uid'], remote['uid']))
                    account['last_uid'] = max(account['last_uid'], remote['uid'])
                # Optional scanned-through UID includes ignored non-invoice messages.
                if type(result.get('last_uid')) is int and result['last_uid'] > account['last_uid']:
                    self._account_update(account['id'], last_uid=result['last_uid'])
                self._account_update(account['id'], last_checked_at=time.time(), last_error='', failures=0,
                                     suspended=False, next_check_at=time.time() + (3 if result.get('more') else
                                                                                config['interval_seconds']))
            except Exception:
                if self._closed.is_set():
                    return
                failures = int(account.get('failures', 0)) + 1
                self._account_update(account['id'], last_checked_at=time.time(), failures=failures,
                                     suspended=failures >= _RETRY_LIMIT,
                                     next_check_at=time.time() + min(3600, 60 * 2 ** min(failures - 1, 6)),
                                     last_error=('收件连续失败已暂停，请检查网络或授权后点立即收件' if failures >= _RETRY_LIMIT
                                                 else '收件未完成，原件和处理进度已保留，将稍后重试'))
        if not self._closed.is_set() and self._config()['enabled']:
            try:
                if self._settings() == settings:
                    self._dispatch_pending(account['id'], config)
            except Exception:
                # Each message has its own durable retry counter; no raw transport/model exception is exposed.
                pass

    def _route_details(self, text: str, config: dict[str, Any], dates: list[str]) -> tuple[str, str]:
        projects = ExpenseProjects(self.store).list()
        found = [p['id'] for p in projects if p['name'] and p['name'] in text]
        if len(found) == 1:
            return found[0], 'name'
        candidates = [p['id'] for p in projects if dates and p.get('start_date') and
                      p.get('reimbursement_status') != 'reimbursed' and
                      all(p['start_date'] <= day and (not p.get('end_date') or day <= p['end_date']) for day in dates)]
        if len(candidates) == 1:
            return candidates[0], 'date'
        default = next((p['id'] for p in projects if p['id'] == config.get('default_project_id')), '')
        return default, 'default' if default else ''

    def _route(self, text: str, config: dict[str, Any], dates: list[str] | None = None) -> str:
        return self._route_details(text, config, dates or [])[0]

    def _persist_message(self, account: dict[str, Any], remote: dict[str, Any], config: dict[str, Any]) -> None:
        key = _hash([account['id'], account['uidvalidity'], remote['uid']])
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM mail_agent_messages WHERE id=?', (key,)).fetchone():
                return
        folder = self.originals / key
        folder.mkdir(exist_ok=True)
        files: list[dict[str, Any]] = []
        for attachment in remote.get('files', []):
            payload = attachment['payload']
            if not isinstance(payload, bytes) or not payload or len(payload) > 20 * 1024 * 1024:
                raise ValueError('attachment size invalid')
            name = Path(str(attachment['name']).replace('\\', '/')).name[:200]
            suffix = Path(name).suffix.lower()
            if suffix not in {'.pdf', '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}:
                raise ValueError('unsupported attachment')
            checksum = hashlib.sha256(payload).hexdigest()
            path = folder / (checksum + suffix)
            if not path.exists():
                _atomic(path, payload)
            elif hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError('original changed')
            if not any(f['digest'] == checksum for f in files):
                files.append({'name': name, 'path': str(path), 'digest': checksum,
                              'source': str(attachment.get('source', 'attachment'))[:1000]})
        subject = str(remote.get('subject', ''))[:500]
        text = str(remote.get('text', ''))[:24000]
        from invoice_print_layout.mail_project_dates import attachment_dates
        sent_date = str(remote.get('sent_date') or '')
        try:
            if date.fromisoformat(sent_date).isoformat() != sent_date:
                sent_date = ''
        except ValueError:
            sent_date = ''
        dates = sorted({day for file in files for day in attachment_dates(Path(file['path']))})
        dates = dates or ([sent_date] if sent_date else [])
        project, reason = self._route_details(subject + '\n' + text, config, dates)
        message = {'id': key, 'account_id': account['id'], 'uidvalidity': account['uidvalidity'],
                   'uid': remote['uid'], 'subject': subject, 'sender': str(remote.get('sender', ''))[:500],
                   'text': text, 'message_id': str(remote.get('message_id', ''))[:1000],
                   'received_at': time.time(), 'files': files, 'issues': [str(i)[:500] for i in remote.get('issues', [])],
                   'sent_date': sent_date, 'route_dates': dates, 'route_reason': reason,
                   'source_issues': [str(i)[:500] for i in remote.get('issues', [])],
                   'project_id': project, 'job_id': '', 'material_ids': [], 'dispatch_attempts': 0,
                   'retry_at': 0.0, 'status': ('attention' if remote.get('issues') else
                                              (('waiting_agent' if project else 'unassigned') if files else 'ignored'))}
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO mail_agent_messages VALUES (?,?,?,?,?)',
                       (key, account['id'], account['uidvalidity'], remote['uid'], encoded(message)))

    def _assign_message(self, message: dict[str, Any], project_id: str, **changes: Any) -> None:
        if message['account_id'] != self._account_key(self._settings()):
            raise ValueError('邮件属于另一邮箱，请切回对应邮箱操作')
        if (message.get('job_id') or message.get('material_ids') or message['status'] != 'unassigned' or
                message.get('project_id')):
            raise ValueError('邮件已开始整理，不能直接变更项目')
        self._message_update(message['id'], project_id=project_id, status='waiting_agent',
                             dispatch_attempts=0, retry_at=0.0, **changes)
        self._dispatch_at = 0.0

    def assign(self, message_key: str, project_id: str) -> dict[str, Any]:
        if not isinstance(message_key, str) or not isinstance(project_id, str):
            raise ValueError('邮件或项目无效')
        if not any(p['id'] == project_id for p in ExpenseProjects(self.store).list()):
            raise ValueError('请选择有效项目')
        with self._lock:
            message = self._message(message_key)
            self._assign_message(message, project_id)
        self.kick()
        return self.status()

    def reroute_unassigned(self) -> int:
        count = 0
        with self._lock:
            try:
                settings = self._settings()
            except Exception:
                return 0
            config = self._scoped_config(self._config(), settings)
            for message in self._messages(self._account_key(settings)):
                if (message['status'] != 'unassigned' or message.get('job_id') or
                        message.get('material_ids') or message.get('project_id')):
                    continue
                dates = message.get('route_dates') or ([message['sent_date']] if message.get('sent_date') else [])
                if not dates:
                    continue
                project, reason = self._route_details('', config, dates)
                if project:
                    self._assign_message(message, project, route_reason=reason)
                    count += 1
        if count:
            self.kick()
        return count

    def _dispatch_pending(self, account_key: str, config: dict[str, Any]) -> None:
        for message in self._messages(account_key):
            if self._closed.is_set():
                return
            if (message['status'] != 'waiting_agent' or not message['project_id'] or
                    message.get('dispatch_attempts', 0) >= _RETRY_LIMIT or message.get('retry_at', 0) > time.time()):
                continue
            try:
                with self._lock:
                    current = self._config()
                    if not current['enabled'] or self._account_key(self._settings()) != account_key:
                        return
                    current = self._scoped_config(current, self._settings())
                    message = self._message(message['id'])
                    if message['status'] != 'waiting_agent':
                        continue
                    if message.get('source_issues'):
                        self._message_update(message['id'], status='attention')
                        continue
                    if not any(p['id'] == message['project_id'] for p in ExpenseProjects(self.store).list()):
                        self._message_update(message['id'], status='attention',
                                             issues=[*message['issues'], '原项目已不可用，请在人工工作台处理'])
                        continue
                    self._dispatch(message, current)
            except Exception:
                attempts = message.get('dispatch_attempts', 0) + 1
                self._message_update(message['id'], dispatch_attempts=attempts,
                                     retry_at=time.time() + 30 * 2 ** (attempts - 1),
                                     status='attention' if attempts >= _RETRY_LIMIT else 'waiting_agent',
                                     issues=list(dict.fromkeys([*message['issues'], '自动整理暂未开始，原件已保存，可在人工工作台继续'])))

    def _dispatch(self, message: dict[str, Any], config: dict[str, Any]) -> None:
        request = 'mail_' + message['id']
        with self.store.connect() as db:
            prior = db.execute('SELECT job_id FROM invoice_agent_requests WHERE id=?', (request,)).fetchone()
            supplement = db.execute('SELECT job_id FROM invoice_agent_actions WHERE key=?',
                                    ('mail-supplement:' + request,)).fetchone()
        prior = prior or supplement
        if prior:
            self._message_update(message['id'], job_id=prior['job_id'], status='processing')
            self.agent.kick()
            return
        queue = IntakeQueue(self.store)
        ids = []
        for file in message['files']:
            payload = Path(file['path']).read_bytes()
            if hashlib.sha256(payload).hexdigest() != file['digest']:
                raise ValueError('original changed')
            entry = queue.receive(file['name'], payload, source='mail_agent', project_id=message['project_id'],
                                  batch=message['id'])
            if (entry.get('source') == 'mail_agent' and entry.get('batch') == message['id'] and
                    entry['project_id'] == message['project_id'] and entry['status'] in {'received', 'review'} and
                    not entry.get('item_id')):
                ids.append(entry['id'])
        ids = list(dict.fromkeys(ids))
        self._message_update(message['id'], material_ids=ids)
        if not ids:
            self._message_update(message['id'], status='duplicate', issues=list(dict.fromkeys([
                *message['issues'], '材料已存在，保留原归属和处理状态，不重复创建费用'])))
            return
        if len(ids) > 20:
            raise ValueError('too many materials')
        jobs = self.agent.jobs.list(message['project_id'])
        busy = next((j for j in jobs if j['state'] in {'queued', 'running', 'needs_input'}), None)
        if busy:
            owned = any(m.get('job_id') == busy['id'] for m in self._messages())
            if owned and busy['state'] in {'needs_input', 'queued'} and not busy.get('item_ids'):
                job = self.agent.jobs.append_materials(busy['id'], ids, request)
            else:
                return  # Wait for the existing project task; never spin up a competing model task.
        else:
            job = self.agent.jobs.create(message['project_id'], ids, request, use_model=self.organize.effective_model(
                True, GlmInvoiceAI(self.store.root).public_status()['available']))
        self._message_update(message['id'], job_id=job['id'], status='processing')
        self.agent.kick()

    def close(self) -> None:
        self._closed.set()
        thread = self._thread
        if thread is not None:
            thread.join(12)
            if thread.is_alive():
                raise RuntimeError('自动收件后台尚未安全退出，请稍后重试关闭')
