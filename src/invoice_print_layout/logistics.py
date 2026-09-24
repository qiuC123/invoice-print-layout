"""Local logistics task ledger. Transport workers are separate and opt-in.

No method here sends messages or writes the lodging sheet. An outbox entry is
an intention, never evidence of delivery. All times on the boundary are aware.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

TZ = timezone(timedelta(hours=8))
Kind = Literal['lunch', 'dinner', 'supper', 'breakfast', 'lodging']
SCHEDULE: dict[str, tuple[int, int]] = {
    'lunch': (9, 0), 'dinner': (14, 30), 'supper': (20, 0),
    'breakfast': (20, 0), 'lodging': (22, 30),
}
LABELS = {'lunch': '午饭', 'dinner': '晚饭', 'supper': '夜宵',
          'breakfast': '早餐', 'lodging': '住宿'}
ALIASES = {'午饭': 'lunch', '午餐': 'lunch', '晚饭': 'dinner', '晚餐': 'dinner',
           '夜宵': 'supper', '宵夜': 'supper', '早餐': 'breakfast', '早饭': 'breakfast'}


def aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError('时间必须包含时区')
    return value.astimezone(TZ)


def count(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 10000:
        raise ValueError('人数或份数必须是0—10000的整数')
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('项目、会话及人员必须有明确标识')
    return value


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    site_id: str
    account_id: str
    owner_id: str
    responsible_id: str
    group_id: str
    direct_chat_id: str
    supplier_group_id: str
    start: date
    end: date

    def validate(self) -> None:
        for field in ('id', 'name', 'site_id', 'account_id', 'owner_id',
                      'responsible_id', 'group_id', 'direct_chat_id', 'supplier_group_id'):
            identifier(getattr(self, field))
        if self.start > self.end or (self.end - self.start).days > 366:
            raise ValueError('请设置明确的项目起止日期，单次最多367天')
        if self.group_id == self.direct_chat_id:
            raise ValueError('群聊与私聊不能使用同一会话标识')

    def payload(self) -> dict[str, Any]:
        self.validate()
        return {**asdict(self), 'start': self.start.isoformat(), 'end': self.end.isoformat()}


@dataclass(frozen=True)
class SourceMessage:
    account_id: str
    chat_id: str
    sender_id: str
    message_id: str
    occurred_at: datetime
    text: str

    @property
    def key(self) -> str:
        for value in (self.account_id, self.chat_id, self.sender_id, self.message_id):
            identifier(value)
        return hashlib.sha256(json.dumps([self.account_id, self.chat_id, self.message_id]).encode()).hexdigest()


@dataclass(frozen=True)
class CountChange:
    operation: Literal['set', 'add', 'subtract']
    amount: int


def parse_count(text: str) -> CountChange | None:
    """Accept complete, unambiguous phrases, never a number found in prose."""
    clean = text.strip().rstrip('。.!！').strip()
    match = re.fullmatch(r'(?:一共|总共|共|改成|改为|改到|人数是|是)?\s*([0-9]{1,5})\s*(?:份|人)?', clean)
    if match:
        return CountChange('set', count(int(match[1])))
    match = re.fullmatch(r'(再加|增加|加|减少|减掉|减)\s*([0-9]{1,5})\s*(?:份|人)?', clean)
    if match:
        return CountChange('subtract' if match[1] in {'减少', '减掉', '减'} else 'add', count(int(match[2])))
    if clean in {'不用', '不需要', '不吃', '零份'}:
        return CountChange('set', 0)
    return None


def meal_changes(text: str, candidates: set[str]) -> tuple[dict[str, CountChange], str]:
    """Explicit labels disambiguate simultaneous supper and breakfast tasks."""
    pieces = [p.strip() for p in re.split(r'[，,；;\n]+', text.strip()) if p.strip()]
    changes: dict[str, CountChange] = {}
    for piece in pieces:
        match = re.fullmatch(r'(午饭|午餐|晚饭|晚餐|夜宵|宵夜|早餐|早饭)\s*[:：]?\s*(.+)', piece)
        if match:
            kind, phrase = ALIASES[match[1]], match[2]
        elif len(pieces) == 1 and len(candidates) == 1:
            kind, phrase = next(iter(candidates)), piece
        else:
            return {}, '请分别注明餐次和份数，例如“夜宵21份，早餐18份”。'
        if kind not in candidates or kind in changes:
            return {}, '餐次不在当前询问范围或重复出现，请明确本次报数。'
        try:
            parsed = parse_count(phrase)
        except ValueError:
            parsed = None
        if parsed is None:
            return {}, '尚未得到明确数量，请说明餐次及最终份数；没有需求请明确回复0份。'
        changes[kind] = parsed
    return (changes, '') if changes else ({}, '请提供餐次及份数。')


class LogisticsStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                    inquiry_day TEXT NOT NULL, business_day TEXT NOT NULL,
                    ask_at TEXT NOT NULL, direct_at TEXT NOT NULL, deadline TEXT NOT NULL,
                    quantity INTEGER, revision INTEGER NOT NULL DEFAULT 0,
                    confirmed_revision INTEGER, state TEXT NOT NULL DEFAULT 'waiting',
                    clarification TEXT NOT NULL DEFAULT '', source_key TEXT, last_event_at TEXT,
                    UNIQUE(project_id,kind,inquiry_day));
                CREATE TABLE IF NOT EXISTS inbox (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS changes (
                    task_id TEXT NOT NULL, revision INTEGER NOT NULL, source_key TEXT NOT NULL,
                    quantity INTEGER NOT NULL, PRIMARY KEY(task_id,revision));
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, stage TEXT NOT NULL,
                    revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', evidence TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS collector (
                    project_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
                    healthy INTEGER NOT NULL, detail TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db
        finally:
            db.close()

    def add_project(self, project: Project) -> None:
        payload = json.dumps(project.payload(), ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            row = db.execute('SELECT payload FROM projects WHERE id=?', (project.id,)).fetchone()
            if row and row['payload'] != payload:
                raise ValueError('已有项目绑定不能直接覆盖，请先完成当前任务并建立新配置版本')
            db.execute('INSERT OR IGNORE INTO projects(id,payload) VALUES (?,?)', (project.id, payload))

    def create_project(self, name: str, site_name: str = '') -> dict[str, Any]:
        name = identifier(name).strip()
        if not isinstance(site_name, str) or len(site_name) > 200:
            raise ValueError('工作地点应为200字以内的文本')
        project: dict[str, Any] = {'id': uuid.uuid4().hex, 'name': name, 'site_name': site_name.strip(), 'status': 'draft',
            **{field: '' for field in ('site_id', 'account_id', 'owner_id', 'responsible_id',
                                      'group_id', 'direct_chat_id', 'supplier_group_id', 'start', 'end')}}
        with self.connect() as db:
            if any(json.loads(row['payload'])['name'] == name for row in db.execute('SELECT payload FROM projects')):
                raise ValueError('项目名称已存在，请选择已有项目或使用不同名称')
            db.execute('INSERT INTO projects(id,payload) VALUES (?,?)',
                       (project['id'], json.dumps(project, ensure_ascii=False)))
        return project

    def pause(self, project_id: str, paused: bool = True) -> None:
        with self.connect() as db:
            self._project(db, project_id)
            db.execute('UPDATE projects SET paused=? WHERE id=?', (int(paused), project_id))
            if paused:
                db.execute("UPDATE outbox SET state='cancelled',evidence='paused' WHERE state='pending' AND task_id IN "
                           '(SELECT id FROM tasks WHERE project_id=?)', (project_id,))

    @staticmethod
    def _project(db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        row = db.execute('SELECT payload,paused FROM projects WHERE id=?', (project_id,)).fetchone()
        if row is None:
            raise ValueError('项目不存在')
        result: dict[str, Any] = json.loads(row['payload'])
        result['paused'] = bool(row['paused'])
        return result

    def open_day(self, project_id: str, inquiry_day: date) -> list[dict[str, Any]]:
        with self.connect() as db:
            project = self._project(db, project_id)
            if project.get('status') == 'draft':
                raise ValueError('请先配置项目日期和负责人，再启用定时询问')
            if project['paused']:
                raise ValueError('项目已暂停')
            if not project['start'] <= inquiry_day.isoformat() <= project['end']:
                raise ValueError('询问日期不在项目期间')
            for kind, (hour, minute) in SCHEDULE.items():
                business_day = inquiry_day + timedelta(days=kind == 'breakfast')
                # Project ends on a business date; do not order breakfast after its end.
                if business_day.isoformat() > project['end']:
                    continue
                ask = datetime.combine(inquiry_day, time(hour, minute), TZ)
                task_id = hashlib.sha256(json.dumps([project_id, inquiry_day.isoformat(), kind]).encode()).hexdigest()[:24]
                db.execute('''INSERT OR IGNORE INTO tasks
                    (id,project_id,kind,inquiry_day,business_day,ask_at,direct_at,deadline)
                    VALUES (?,?,?,?,?,?,?,?)''', (task_id, project_id, kind, inquiry_day.isoformat(),
                    business_day.isoformat(), ask.isoformat(), (ask + timedelta(minutes=30)).isoformat(),
                    (ask + timedelta(hours=1)).isoformat()))
            return [dict(r) for r in db.execute('SELECT * FROM tasks WHERE project_id=? AND inquiry_day=? ORDER BY ask_at,kind',
                                                (project_id, inquiry_day.isoformat()))]

    def collector_status(self, project_id: str, at: datetime, *, healthy: bool, detail: str = '') -> None:
        at = aware(at)
        with self.connect() as db:
            self._project(db, project_id)
            db.execute('INSERT INTO collector VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET '
                       'checked_at=excluded.checked_at,healthy=excluded.healthy,detail=excluded.detail',
                       (project_id, at.isoformat(), int(healthy), detail[:500]))

    @staticmethod
    def _action(db: sqlite3.Connection, task: sqlite3.Row, stage: str, revision: int,
                payload: dict[str, Any]) -> str:
        action_id = f"{task['id']}:{stage}:{revision}"
        db.execute("INSERT INTO outbox(id,task_id,stage,revision,payload) VALUES (?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET state='pending',evidence='' "
                   "WHERE outbox.state='cancelled' AND outbox.evidence='paused'",
                   (action_id, task['id'], stage, revision, json.dumps(payload, ensure_ascii=False)))
        return action_id

    def tick(self, now: datetime) -> list[dict[str, Any]]:
        now = aware(now)
        with self.connect() as db:
            tasks = db.execute("SELECT tasks.* FROM tasks JOIN projects ON projects.id=tasks.project_id "
                               "WHERE projects.paused=0 AND tasks.state IN ('waiting','clarify','escalated')").fetchall()
            for task in tasks:
                ask, direct, deadline = (datetime.fromisoformat(task[k]) for k in ('ask_at', 'direct_at', 'deadline'))
                if now < ask:
                    continue
                project = self._project(db, task['project_id'])
                base = {'account_id': project['account_id'], 'project_id': project['id'],
                        'task_id': task['id'], 'business_day': task['business_day'], 'kind': task['kind']}
                if now >= deadline:
                    # At a missed deadline, never catch up by sending old inquiries.
                    db.execute("UPDATE outbox SET state='cancelled' WHERE task_id=? AND state='pending' "
                               "AND stage IN ('group','direct','clarify')", (task['id'],))
                    health = db.execute('SELECT * FROM collector WHERE project_id=?', (project['id'],)).fetchone()
                    fresh = health and health['healthy'] and timedelta(0) <= now - datetime.fromisoformat(health['checked_at']) <= timedelta(minutes=2)
                    delivered = db.execute("SELECT 1 FROM outbox WHERE task_id=? AND stage IN ('group','direct') AND state='sent'", (task['id'],)).fetchone()
                    reason = ('reply_missing' if delivered else 'inquiry_not_delivered') if fresh else 'collector_unavailable'
                    explanation = {'reply_missing': '尚未收到明确回复，请人工跟进。',
                                   'inquiry_not_delivered': '询问消息尚无送达证明，请人工核查。',
                                   'collector_unavailable': '微信采集未正常完成，无法判断是否已回复，请人工核查。'}
                    self._action(db, task, 'escalate', task['revision'], {**base, 'channel': 'feishu', 'target': project['owner_id'],
                        'reason': reason, 'text': f"{project['name']} {task['business_day']} {LABELS[task['kind']]}：" +
                        explanation[reason]})
                    db.execute("UPDATE tasks SET state='escalated' WHERE id=?", (task['id'],))
                    continue
                stage = 'direct' if now >= direct else 'group'
                if stage == 'direct':
                    db.execute("UPDATE outbox SET state='cancelled' WHERE task_id=? AND stage='group' AND state='pending'", (task['id'],))
                target = project['direct_chat_id'] if stage == 'direct' else project['group_id']
                question = '住宿人员有变化吗？请说明当前总人数，或增加、减少的人数。' if task['kind'] == 'lodging' else f"{task['business_day']} {LABELS[task['kind']]}需要多少份？请注明餐次。"
                self._action(db, task, stage, 0, {**base, 'channel': 'wechat', 'target': target,
                    'mentions': [project['responsible_id']] if stage == 'group' else [], 'text': question})
            return self._outbox(db)

    def receive(self, message: SourceMessage) -> dict[str, Any]:
        occurred = aware(message.occurred_at)
        key = message.key
        if not isinstance(message.text, str) or not message.text.strip() or len(message.text) > 5000:
            raise ValueError('消息正文为空或过长')
        payload = {**asdict(message), 'occurred_at': occurred.isoformat()}
        encoded_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            seen = db.execute('SELECT result,payload FROM inbox WHERE key=?', (key,)).fetchone()
            if seen:
                if seen['payload'] != encoded_payload:
                    raise ValueError('同一来源消息标识出现不同内容，请核验采集结果')
                prior: dict[str, Any] = json.loads(seen['result'])
                return {**prior, 'duplicate': True}
            # Only configured responsible person's current text is accepted. Transport
            # must remove quoted history and verify freshness before calling receive.
            matches: list[tuple[dict[str, Any], list[sqlite3.Row]]] = []
            for p in db.execute('SELECT id FROM projects WHERE paused=0').fetchall():
                project = self._project(db, p['id'])
                if (message.account_id != project['account_id'] or message.sender_id != project['responsible_id']
                        or message.chat_id not in {project['group_id'], project['direct_chat_id']}):
                    continue
                tasks = [t for t in db.execute('SELECT * FROM tasks WHERE project_id=?', (project['id'],))
                         if datetime.fromisoformat(t['ask_at']) <= occurred < datetime.fromisoformat(t['ask_at']) + timedelta(days=1)]
                if tasks:
                    matches.append((project, tasks))
            result: dict[str, Any] = {'state': 'unmatched', 'changed': []}
            if len(matches) > 1:
                result = {'state': 'ambiguous_project', 'changed': [], 'reason': '同一负责人有多个项目，请明确项目。'}
            elif matches:
                project, tasks = matches[0]
                explicit_kinds = {kind for label, kind in ALIASES.items() if label in message.text}
                if explicit_kinds:
                    tasks = [t for t in tasks if t['kind'] in explicit_kinds]
                else:
                    current = [t for t in tasks if occurred <= datetime.fromisoformat(t['deadline'])]
                    if current:
                        tasks = current
                meals = {t['kind']: t for t in tasks if t['kind'] != 'lodging'}
                # Lodging replies use a separate entry point after a baseline has been
                # verified. Do not silently treat people as a meal order at 22:30.
                explicit_meal = bool(explicit_kinds)
                if message.text.strip().rstrip('。!！') in {'收到', '好的', '好', '稍后', '等下', '谢谢', '知道了'}:
                    result = {'state': 'ignored', 'changed': []}
                elif not re.search(r'[0-9一二三四五六七八九十两零]|改|变|加|增|减|不用|不吃|不需要|无变化|没变', message.text):
                    result = {'state': 'ignored', 'changed': []}
                elif any(t['kind'] == 'lodging' for t in tasks) and not explicit_meal:
                    result = {'state': 'needs_lodging_context', 'changed': []}
                elif meals:
                    changes, reason = meal_changes(message.text, set(meals))
                    relevant = [meals[k] for k in changes] if changes else list(meals.values())
                    newest = max((datetime.fromisoformat(t['last_event_at']) for t in relevant if t['last_event_at']), default=None)
                    older = newest is not None and occurred < newest
                    if newest is not None and occurred == newest:
                        reason = '同一秒内有多条不同回复，无法确定先后；请再回复最终份数。'
                    values: dict[str, int] = {}
                    for kind, change in changes.items():
                        old = meals[kind]['quantity']
                        if change.operation != 'set' and old is None:
                            reason = '尚无人数基准，请回复餐次及最终份数。'
                            break
                        try:
                            values[kind] = count(change.amount if change.operation == 'set' else
                                                 old + (change.amount if change.operation == 'add' else -change.amount))
                        except ValueError:
                            reason = '变更后数量无效，请回复最终份数。'
                            break
                    if older:
                        result = {'state': 'older_message', 'changed': [], 'reason': '旧消息已保留，不覆盖较新的回复。'}
                    elif reason:
                        result = {'state': 'clarify', 'changed': [], 'reason': reason}
                        for task in meals.values():
                            db.execute("UPDATE tasks SET clarification=?,state='clarify',confirmed_revision=NULL,last_event_at=? WHERE id=?", (reason, occurred.isoformat(), task['id']))
                            # Block old confirmation/send until ambiguous corrections resolve.
                            db.execute("UPDATE outbox SET state='cancelled' WHERE task_id=? AND state='pending' AND stage IN ('supplier','review')", (task['id'],))
                        task = next(iter(meals.values()))
                        self._action(db, task, 'clarify', int(key[:12], 16), {'channel': 'wechat',
                            'account_id': project['account_id'], 'target': message.chat_id,
                            'mentions': [project['responsible_id']] if message.chat_id == project['group_id'] else [], 'text': reason})
                    else:
                        changed: list[str] = []
                        for kind, value in values.items():
                            task = meals[kind]
                            revision = task['revision'] + 1
                            db.execute("UPDATE tasks SET quantity=?,revision=?,confirmed_revision=NULL,state='review',"
                                       "clarification='',source_key=?,last_event_at=? WHERE id=?", (value, revision, key, occurred.isoformat(), task['id']))
                            db.execute('INSERT INTO changes VALUES (?,?,?,?)', (task['id'], revision, key, value))
                            db.execute("UPDATE outbox SET state='cancelled' WHERE task_id=? AND state='pending'", (task['id'],))
                            self._action(db, task, 'review', revision, {'channel': 'feishu', 'target': project['owner_id'],
                                'project_id': project['id'], 'task_id': task['id'], 'revision': revision,
                                'quantity': value, 'business_day': task['business_day'], 'kind': kind,
                                'text': f"{project['name']} {task['business_day']} {LABELS[kind]} {value}份，待你确认。"})
                            changed.append(task['id'])
                        result = {'state': 'review', 'changed': changed}
            db.execute('INSERT INTO inbox VALUES (?,?,?)', (key, encoded_payload, json.dumps(result, ensure_ascii=False)))
            return result

    def confirm(self, owner_id: str, task_id: str, revision: int) -> str:
        with self.connect() as db:
            task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task is None:
                raise ValueError('任务不存在')
            project = self._project(db, task['project_id'])
            if owner_id != project['owner_id'] or project['paused']:
                raise ValueError('当前用户无权确认或项目已暂停')
            if task['kind'] == 'lodging':
                raise ValueError('住宿须先核对房间安排，不能直接按人数写表')
            if type(revision) is not int or revision != task['revision'] or revision < 1 or task['quantity'] is None or task['clarification']:
                raise ValueError('数量已变化或仍有疑问，请刷新后重新核对')
            action_id = self._action(db, task, 'supplier', revision, {'channel': 'wechat',
                'account_id': project['account_id'], 'project_id': project['id'], 'target': project['supplier_group_id'],
                'mentions': [], 'task_id': task_id, 'revision': revision, 'quantity': task['quantity'],
                'business_day': task['business_day'], 'kind': task['kind'],
                'text': f"{project['name']} {task['business_day']} {LABELS[task['kind']]}：{task['quantity']}份。"})
            db.execute("UPDATE tasks SET confirmed_revision=?,state=CASE WHEN state='sent' THEN state ELSE 'confirmed' END WHERE id=?", (revision, task_id))
            return action_id

    def claim(self, action_id: str, *, now: datetime) -> dict[str, Any] | None:
        """Single dispatcher claims before I/O; crashes remain in_flight, no retry."""
        now = aware(now)
        with self.connect() as db:
            row = db.execute('SELECT * FROM outbox WHERE id=?', (action_id,)).fetchone()
            if row is None or row['state'] != 'pending':
                return None
            task = db.execute('SELECT * FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
            project = self._project(db, task['project_id'])
            if project['paused']:
                return None
            if row['stage'] == 'supplier' and now.date().isoformat() > task['business_day']:
                db.execute("UPDATE outbox SET state='cancelled',evidence='expired' WHERE id=?", (action_id,))
                db.execute("UPDATE tasks SET state='expired',confirmed_revision=NULL,clarification=? WHERE id=? AND revision=?",
                           ('此餐业务日期已过，请人工核对，不自动补发订餐通知。', task['id'], row['revision']))
                return None
            if row['stage'] in {'group', 'direct', 'clarify'}:
                start = datetime.fromisoformat(task['direct_at'] if row['stage'] == 'direct' else task['ask_at'])
                end = datetime.fromisoformat(task['direct_at'] if row['stage'] == 'group' else task['deadline'])
                if now < start:
                    return None
                if now >= end:
                    db.execute("UPDATE outbox SET state='cancelled',evidence='expired' WHERE id=?", (action_id,))
                    return None
            if row['stage'] == 'supplier' and (task['confirmed_revision'] != row['revision'] or task['revision'] != row['revision'] or task['clarification']):
                db.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (action_id,))
                return None
            db.execute("UPDATE outbox SET state='in_flight' WHERE id=?", (action_id,))
            return {'id': action_id, **json.loads(row['payload'])}

    def delivery_result(self, action_id: str, *, verified: bool, evidence: str) -> None:
        """Evidence should reference a read-back message ID or a local failure log."""
        identifier(evidence)
        with self.connect() as db:
            row = db.execute('SELECT * FROM outbox WHERE id=?', (action_id,)).fetchone()
            if row is None or row['state'] not in {'in_flight', 'uncertain'}:
                raise ValueError('发送动作未领取，或已经结束')
            db.execute('UPDATE outbox SET state=?,evidence=? WHERE id=?', ('sent' if verified else 'uncertain', evidence, action_id))
            if verified and row['stage'] == 'supplier':
                db.execute("UPDATE tasks SET state='sent' WHERE id=? AND revision=? AND clarification=''", (row['task_id'], row['revision']))

    @staticmethod
    def _outbox(db: sqlite3.Connection) -> list[dict[str, Any]]:
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in db.execute('SELECT * FROM outbox ORDER BY rowid')]

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as db:
            return {'transport_enabled': False, 'projects': [self._project(db, r['id']) for r in db.execute('SELECT id FROM projects ORDER BY rowid')],
                    'tasks': [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY ask_at,kind')],
                    'outbox': self._outbox(db), 'collector': [dict(r) for r in db.execute('SELECT * FROM collector')]}
