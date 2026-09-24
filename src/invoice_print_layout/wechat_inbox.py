"""Project-scoped local inbox. Classification never sends or confirms an order."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import re
import time
from typing import Any
import uuid

from invoice_print_layout.logistics import LogisticsStore, identifier
from invoice_print_layout.wechat_reader import HistoryBatch, WxHistoryReader, overlap_start

GROUP_CATEGORIES = {'site': '现场沟通', 'meals': '订餐', 'lodging': '住宿', 'invoices': '票据', 'other': '其他'}
MESSAGE_CATEGORIES = {'meals': '餐饮', 'lodging': '住宿', 'invoices': '票据',
                      'materials': '进出场资料', 'other': '其他', 'unclassified': '待分类'}


def category_for(text: str, type_code: int, purpose: str = '') -> str:
    # Classify the reply only, never quoted history, file names or image guesses.
    if type_code not in {1, 49} or not text.strip():
        return 'unclassified'
    matches = {category for category, pattern in {
        'meals': r'午[饭餐]|晚[饭餐]|早[饭餐]|夜宵|宵夜|订餐|送餐|餐费',
        'lodging': r'住宿|入住|退房|房间|酒店|大床|双床',
        'invoices': r'发票|报销|开票|税号',
        'materials': r'进出场|进场|出场|放行单|通行证',
    }.items() if re.search(pattern, text)}
    if len(matches) == 1:
        return matches.pop()
    if len(matches) > 1:
        return 'unclassified'
    # A terse reply can inherit a dedicated group's purpose, not a shared group.
    if purpose in {'meals', 'lodging', 'invoices'}:
        return purpose
    return 'unclassified'


class WeChatInbox:
    def __init__(self, logistics: LogisticsStore):
        self.store = logistics
        with self.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS wx_groups (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, account_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
                    UNIQUE(project_id, name));
                CREATE TABLE IF NOT EXISTS wx_messages (
                    key TEXT PRIMARY KEY, account_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    sender_id TEXT, timestamp INTEGER NOT NULL, source TEXT NOT NULL,
                    text TEXT NOT NULL, quoted_text TEXT NOT NULL, type_code INTEGER NOT NULL,
                    direction TEXT NOT NULL, project_id TEXT, category TEXT NOT NULL,
                    status TEXT NOT NULL, issues TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wx_assignments (
                    id INTEGER PRIMARY KEY, message_key TEXT NOT NULL, old_project_id TEXT,
                    project_id TEXT NOT NULL, category TEXT NOT NULL, at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS wx_checkpoints (
                    account_id TEXT NOT NULL, chat_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    PRIMARY KEY(account_id, chat_id));
                CREATE TABLE IF NOT EXISTS wx_receiver (
                    account_id TEXT NOT NULL, chat_id TEXT NOT NULL, checked_at INTEGER NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(account_id, chat_id));
            ''')

    def save_group(self, body: dict[str, Any]) -> dict[str, Any]:
        project_id = identifier(body['project_id'])
        name = identifier(body['name']).strip()
        category = body['category']
        if category not in GROUP_CATEGORIES:
            raise ValueError('未知微信群用途')
        account, chat = body.get('account_id', ''), body.get('chat_id', '')
        if not isinstance(account, str) or not isinstance(chat, str):
            raise ValueError('微信标识必须是文本')
        account, chat = account.strip(), chat.strip()
        if bool(account) != bool(chat):
            raise ValueError('请同时填写账号和会话的稳定标识，或先全部留空')
        if account:
            identifier(account)
            identifier(chat)
            if any(c.isspace() for c in account + chat) or not account.startswith('wxid_') or not (
                chat.endswith('@chatroom') or chat.startswith('wxid_')
            ):
                raise ValueError('请使用已核验的 wxid_ 账号及 @chatroom 群或 wxid_ 联系人标识')
        key = identifier(body['id']) if body.get('id') else uuid.uuid4().hex
        with self.store.connect() as db:
            self.store._project(db, project_id)
            old = db.execute('SELECT * FROM wx_groups WHERE id=?', (key,)).fetchone()
            if body.get('id') and (old is None or old['project_id'] != project_id):
                raise ValueError('该会话不属于当前项目')
            if old and old['account_id'] and (account, chat) != (old['account_id'], old['chat_id']):
                raise ValueError('已绑定会话不可换绑；请新建会话，保留历史来源')
            if account and db.execute('SELECT 1 FROM wx_groups WHERE project_id=? AND account_id=? AND chat_id=? AND id<>?',
                                      (project_id, account, chat, key)).fetchone():
                raise ValueError('当前项目已绑定该会话，请修改已有会话用途')
            if db.execute('SELECT 1 FROM wx_groups WHERE project_id=? AND name=? AND id<>?',
                          (project_id, name, key)).fetchone():
                raise ValueError('当前项目已有同名会话')
            db.execute('INSERT INTO wx_groups VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'account_id=excluded.account_id,chat_id=excluded.chat_id,name=excluded.name,category=excluded.category',
                       (key, project_id, account, chat, name, category))
        return {'id': key, 'project_id': project_id, 'account_id': account, 'chat_id': chat,
                'name': name, 'category': category, 'status': 'bound' if account else 'unbound'}

    def change_group_category(self, project_id: str, key: str, category: str) -> None:
        if category not in GROUP_CATEGORIES:
            raise ValueError('未知微信群用途')
        with self.store.connect() as db:
            if db.execute('UPDATE wx_groups SET category=? WHERE id=? AND project_id=?',
                          (category, key, project_id)).rowcount != 1:
                raise ValueError('该会话不属于当前项目')

    def assign(self, key: str, project_id: str, category: str) -> None:
        if category not in MESSAGE_CATEGORIES:
            raise ValueError('未知消息分类')
        with self.store.connect() as db:
            self.store._project(db, project_id)
            row = db.execute('SELECT project_id FROM wx_messages WHERE key=?', (key,)).fetchone()
            if row is None:
                raise ValueError('消息不存在')
            db.execute('UPDATE wx_messages SET project_id=?,category=?,status=? WHERE key=?',
                       (project_id, category, 'manual', key))
            db.execute('INSERT INTO wx_assignments(message_key,old_project_id,project_id,category,at) VALUES (?,?,?,?,?)',
                       (key, row['project_id'], project_id, category, int(time.time())))

    def receive_batch(self, account_id: str, batch: HistoryBatch, *, verified_self_id: str | None = None) -> dict[str, Any]:
        """Commit complete source window and cursor together. Unknown identity stays unknown.

        verified_self_id must come from independently checked runtime account identity,
        not from a directory name or the operator's unverified binding form.
        """
        identifier(account_id)
        if verified_self_id is not None and verified_self_id != account_id:
            raise ValueError('读取账号与已核验身份不一致')
        added = 0
        with self.store.connect() as db:
            groups = db.execute('SELECT g.* FROM wx_groups g JOIN projects p ON p.id=g.project_id '
                                'WHERE g.account_id=? AND g.chat_id=? AND p.paused=0',
                                (account_id, batch.chat_id)).fetchall()
            if not groups:
                raise ValueError('会话未列入当前项目接收范围')
            projects = {g['project_id'] for g in groups}
            project_id = next(iter(projects)) if len(projects) == 1 else None
            purposes = {g['category'] for g in groups}
            purpose = next(iter(purposes)) if len(purposes) == 1 and project_id else ''
            for msg in batch.messages:
                if msg.chat_id != batch.chat_id or not batch.start_timestamp <= msg.timestamp <= batch.end_timestamp:
                    raise ValueError('消息来源超出请求会话或时间范围')
                source = json.dumps(asdict(msg), ensure_ascii=False, sort_keys=True)
                key = hashlib.sha256(json.dumps([account_id, msg.message_key]).encode()).hexdigest()
                old = db.execute('SELECT source FROM wx_messages WHERE key=?', (key,)).fetchone()
                if old:
                    if old['source'] != source:
                        raise ValueError('同一来源标识出现不同内容，已保留原记录')
                    continue
                direction = ('outgoing' if msg.sender_id == verified_self_id else 'incoming') if verified_self_id and msg.sender_id else 'unknown'
                category = category_for(msg.current_text, msg.type_code, purpose)
                issues = list(dict.fromkeys([*batch.issues, *msg.issues,
                    *(['account_identity_unverified'] if not verified_self_id else []),
                    *(['ambiguous_project'] if project_id is None else [])]))
                status = 'pending_assignment' if project_id is None else ('review' if issues or category == 'unclassified' else 'classified')
                db.execute('INSERT INTO wx_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (key, account_id, batch.chat_id, msg.sender_id, msg.timestamp, source,
                            msg.current_text, msg.quoted_text, msg.type_code, direction, project_id,
                            category, status, json.dumps(issues)))
                added += 1
            advance = batch.safe_to_advance and verified_self_id is not None
            if advance:
                db.execute('INSERT INTO wx_checkpoints VALUES (?,?,?) ON CONFLICT(account_id,chat_id) DO UPDATE SET '
                           'timestamp=MAX(timestamp,excluded.timestamp)', (account_id, batch.chat_id, batch.end_timestamp))
            status = 'read_ok' if advance else 'needs_review'
            # A history read is not evidence that a live monitor is running.
            detail = '已保存本次读取；尚不能据此证明持续在线接收' if advance else '已保存可读取消息；来源或身份待核验，接收进度未前移'
            db.execute('INSERT OR REPLACE INTO wx_receiver VALUES (?,?,?,?,?)',
                       (account_id, batch.chat_id, int(time.time()), status, detail))
        return {'added': added, 'advanced': advance, 'status': status}

    def collect_once(self, reader: WxHistoryReader, account_id: str, *, first_timestamp: int,
                     end_timestamp: int, verified_self_id: str) -> dict[str, Any]:
        """Read only bound chats. Caller must verify account before invoking; never auto-starts."""
        if verified_self_id != account_id or not 1_000_000_001 < first_timestamp <= end_timestamp:
            raise ValueError('请核验读取账号和明确的接收时间窗口')
        with self.store.connect() as db:
            chats = [r['chat_id'] for r in db.execute('SELECT DISTINCT g.chat_id FROM wx_groups g JOIN projects p '
                'ON p.id=g.project_id WHERE g.account_id=? AND g.chat_id<>\'\' AND p.paused=0', (account_id,))]
        results: dict[str, Any] = {}
        for chat in chats:
            with self.store.connect() as db:
                row = db.execute('SELECT timestamp FROM wx_checkpoints WHERE account_id=? AND chat_id=?', (account_id, chat)).fetchone()
            start = overlap_start(max(first_timestamp, row['timestamp']), first_timestamp) if row else first_timestamp
            if start > end_timestamp:
                results[chat] = {'status': 'skipped_older_window'}
                continue
            try:
                batch = reader.read_window(chat, start, end_timestamp)
                results[chat] = self.receive_batch(account_id, batch, verified_self_id=verified_self_id)
            except (RuntimeError, ValueError, OSError):
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO wx_receiver VALUES (?,?,?,?,?)',
                        (account_id, chat, int(time.time()), 'error', '本次接收失败，进度保留；不能据此判断无人回复'))
                results[chat] = {'status': 'error'}
        return results

    def snapshot(self, project_id: str = '') -> dict[str, Any]:
        with self.store.connect() as db:
            if project_id:
                self.store._project(db, project_id)
            groups = [dict(r) for r in db.execute('SELECT * FROM wx_groups WHERE project_id=? ORDER BY rowid', (project_id,))]
            for group in groups:
                group['status'] = 'bound' if group['account_id'] else 'unbound'
            rows = db.execute('SELECT * FROM wx_messages WHERE project_id IS ? ORDER BY timestamp DESC,key LIMIT 500',
                              (project_id or None,)).fetchall()
            messages = [{k: r[k] for k in r.keys() if k != 'source'} for r in rows]
            for message in messages:
                message['issues'] = json.loads(message['issues'])
            checks = [dict(r) for r in db.execute('SELECT DISTINCT r.* FROM wx_receiver r JOIN wx_groups g '
                'ON r.account_id=g.account_id AND r.chat_id=g.chat_id WHERE g.project_id=?', (project_id,))]
            receiver: dict[str, Any] = {'status': 'not_connected',
                'detail': '微信实时接收尚未联通；可先建立项目和群分类。', 'checks': checks}
            if checks:
                receiver['status'] = 'error' if any(c['status'] == 'error' for c in checks) else 'not_monitoring'
                receiver['detail'] = '最近读取失败，请检查接收状态。' if receiver['status'] == 'error' else '已有读取记录，持续在线接收尚未启用。'
            total = db.execute('SELECT count(*) FROM wx_messages WHERE project_id IS ?', (project_id or None,)).fetchone()[0]
        return {'groups': groups, 'messages': messages, 'total': total, 'receiver': receiver,
                'group_categories': GROUP_CATEGORIES, 'message_categories': MESSAGE_CATEGORIES}
