"""Opt-in, owner-only photo briefs with durable, bounded idempotent delivery."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

from invoice_print_layout.bot import BotError, read_bot_settings, read_bot_secret
from invoice_print_layout.logistics import TZ
from invoice_print_layout.project_todos import ProjectTodos
from invoice_print_layout.site_photos import SitePhotos, dated
from invoice_print_layout.photo_review_pdf import build_reviews

Sender = Callable[[dict[str, Any]], str]
TERMINAL = ('done', 'partial', 'failed', 'interrupted')
DELIVERY_LABELS = {'pending': '等候发送', 'sending': '正在发送', 'sent': '已送达飞书',
                   'retry': '发送未完成，将自动重试', 'failed': '发送失败',
                   'uncertain': '送达状态待核对，请先查看飞书', 'cancelled': '已停止发送'}


def make_card(project: dict[str, Any], rows: list[dict[str, Any]], *, day: str,
              job: dict[str, Any] | None = None, run_state: str = 'done', preview: bool = False) -> dict[str, Any]:
    """Counts use scoped records, never inferred construction progress or people counts."""
    saved = [r for r in rows if r['state'] == 'saved']
    pending = len(rows) - len(saved)
    counts: Counter[str] = Counter()
    for row in saved:
        fields = row['fields']
        kind = fields.get('kind') or '其他'
        if kind == '车辆':
            kind += ' · ' + (fields.get('place') or '位置未填写')
        counts[kind] += 1
    issue_count = len((job or {}).get('issues', []))
    failed = run_state in ('failed', 'interrupted')
    partial = run_state == 'partial' or pending > 0 or issue_count > 0
    title = '项目照片汇总 · 试用简报' if preview else ('照片整理未完成' if failed else '照片整理部分完成' if partial else '照片整理完成')
    lines = [str(project['name'])[:100], ('截至 ' if preview else '读取日期：') + day]
    if preview:
        lines.append(f'现有照片 {len(rows)} 张：已归档 {len(saved)} 张，待核对 {pending} 张。')
        lines.append('这是现有项目照片汇总，包含不同拍摄日期；并非今天新增。')
    else:
        lines.append(f'本次新增记录 {len(rows)} 张：已归档 {len(saved)} 张，待核对 {pending} 张。')
        lines.append(f'重复记录 {(job or {}).get("duplicates", 0)} 条，未完成事项 {issue_count} 项。')
        if job and job.get('added', 0) != len(rows):
            lines.append('部分新增记录尚无法对应，数量需在电脑工作台核对。')
        if not rows and not failed and not partial:
            lines.append('本次没有新增照片；不代表群内没有尚未下载的图片。')
    for category, count in sorted(counts.items()):
        lines.append(f'{category}：{count} 张')
    if failed:
        lines.append('读取中断或失败，已保存的照片保留。请在电脑工作台查看来源群和处理记录后重试。')
    elif pending or issue_count:
        lines.append('待核对照片和未完成事项请到电脑工作台「现场照片」处理。')
    lines.append('照片保存在电脑上；此简报只汇总整理结果，不代表施工完成比例。')
    return {'config': {'wide_screen_mode': True, 'enable_forward': False},
            'header': {'template': 'red' if failed else 'orange' if partial else 'blue',
                       'title': {'tag': 'plain_text', 'content': title}},
            'elements': [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': '\n'.join(lines)}}]}


class PhotoBriefs:
    def __init__(self, workspace: Path, todos: ProjectTodos, photos: SitePhotos,
                 sender: Sender | None = None):
        self.workspace, self.todos, self.photos = workspace, todos, photos
        self.sender = sender or self.send
        self.worker_lock = threading.Lock()
        with todos.store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS photo_brief_settings (
                    project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL,
                    app_id TEXT NOT NULL, owner TEXT NOT NULL, enabled_from TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS photo_briefs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    app_id TEXT NOT NULL, owner TEXT NOT NULL, card TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, first_attempt REAL NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(project_id,source_id));
                CREATE TABLE IF NOT EXISTS photo_brief_jobs (
                    project_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, day TEXT NOT NULL);
            ''')
            for table, columns in {
                'photo_brief_settings': {'include_pdf': 'INTEGER NOT NULL DEFAULT 0'},
                'photo_briefs': {'review': "TEXT NOT NULL DEFAULT ''", 'attachments': "TEXT NOT NULL DEFAULT '[]'"},
            }.items():
                existing = {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
                for name, declaration in columns.items():
                    if name not in existing:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')

    def configure(self, pid: str, enabled: bool, include_pdf: bool | None = None) -> dict[str, Any]:
        self.photos.project(pid)
        if type(enabled) is not bool:
            raise ValueError('请选择是否启用飞书简报')
        if include_pdf is not None and type(include_pdf) is not bool:
            raise ValueError('请选择是否附带PDF审阅册')
        with self.todos.store.connect() as db:
            if enabled:
                settings = read_bot_settings(self.workspace / 'feishu_bot.toml')
                if not settings.owner_open_id:
                    raise ValueError('请先将飞书机器人绑定到本人账号')
                read_bot_secret(settings.app_id)
                previous = db.execute('SELECT * FROM photo_brief_settings WHERE project_id=?', (pid,)).fetchone()
                since = (previous['enabled_from'] if previous and previous['enabled'] and
                         previous['app_id'] == settings.app_id and previous['owner'] == settings.owner_open_id
                         else datetime.now(TZ).strftime('%Y-%m-%d %H:%M'))
                pdf = include_pdf if include_pdf is not None else bool(previous and previous['include_pdf'])
                db.execute('INSERT OR REPLACE INTO photo_brief_settings VALUES (?,?,?,?,?,?)',
                           (pid, 1, settings.app_id, settings.owner_open_id, since, int(pdf)))
            else:
                db.execute('UPDATE photo_brief_settings SET enabled=0 WHERE project_id=?', (pid,))
                db.execute("UPDATE photo_briefs SET state='cancelled' WHERE project_id=? AND state IN ('pending','retry')", (pid,))
                db.execute('DELETE FROM photo_brief_jobs WHERE project_id=?', (pid,))
        return self.view(pid)

    def view(self, pid: str) -> dict[str, Any]:
        with self.todos.store.connect() as db:
            settings = db.execute('SELECT * FROM photo_brief_settings WHERE project_id=?', (pid,)).fetchone()
            recent = db.execute('SELECT * FROM photo_briefs WHERE project_id=? ORDER BY created_at DESC LIMIT 1', (pid,)).fetchone()
            preparing = db.execute('''SELECT 1 FROM photo_brief_jobs j WHERE j.project_id=?
                AND NOT EXISTS (SELECT 1 FROM photo_briefs b WHERE b.project_id=j.project_id AND b.source_id=j.job_id) LIMIT 1''', (pid,)).fetchone()
        latest = None
        if recent:
            attachments = json.loads(recent['attachments'])
            pdf_done = bool(attachments) and all(a['message_id'] for a in attachments)
            label = DELIVERY_LABELS[recent['state']]
            if recent['review'] and recent['message_id'] and not pdf_done:
                label = '简报已送达，PDF尚未全部送达'
            elif recent['review'] and pdf_done:
                label = '简报与PDF已送达飞书'
            latest = {'state': recent['state'], 'label': label, 'error': recent['last_error'],
                      'created_at': recent['created_at'], 'card_sent': bool(recent['message_id']),
                      'pdf_count': len(attachments), 'pdf_sent': sum(bool(a['message_id']) for a in attachments),
                      'files': [{'name': Path(a['path']).name, 'url': f'/photo-review/{pid}/{recent["id"]}/{i}'} for i, a in enumerate(attachments)]}
        return {'enabled': bool(settings and settings['enabled']), 'include_pdf': bool(settings and settings['include_pdf']),
                'preparing': bool(settings and settings['enabled'] and preparing), 'recipient': '已绑定的本人飞书账号', 'latest': latest}

    def enqueue(self, pid: str, source: str, card: dict[str, Any], review: dict[str, Any] | None = None) -> str:
        key = hashlib.sha256((pid + ':' + source).encode()).hexdigest()[:32]
        with self.todos.store.connect() as db:
            setting = db.execute('SELECT * FROM photo_brief_settings WHERE project_id=? AND enabled=1', (pid,)).fetchone()
            if setting is None:
                raise ValueError('请先启用本项目飞书简报')
            review_json = json.dumps(review, ensure_ascii=False) if review and setting['include_pdf'] else ''
            if review_json and review is not None:
                card = json.loads(json.dumps(card))
                card['elements'][0]['text']['content'] += f'\n另发送三列PDF审阅册：{len(review["rows"])}张已归档照片（含本轮重复照片）。待归属照片不上传。'
            db.execute('INSERT OR IGNORE INTO photo_briefs (id,project_id,source_id,app_id,owner,card,created_at,review) VALUES (?,?,?,?,?,?,?,?)',
                       (key, pid, source, setting['app_id'], setting['owner'], json.dumps(card, ensure_ascii=False), time.time(), review_json))
        return key

    def review_payload(self, pid: str, ids: list[str], day: str, job: dict[str, Any] | None = None,
                       state: str = 'done', preview: bool = False) -> dict[str, Any]:
        rows = [self.photos.get(pid, key) for key in dict.fromkeys(ids)]
        saved = []
        for row in rows:
            if row['state'] == 'saved':
                saved.append({**row, 'path': str(self.photos.file(pid, row['id']))})
        return {'project': self.photos.project(pid), 'rows': saved, 'pending': len(rows)-len(saved),
                'day': day, 'job': job or {}, 'run_state': state, 'preview': preview,
                'groups': {g['id']: g['name'] for g in self.photos.settings(pid)['groups']}}

    def read_and_send(self, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.view(pid)['enabled']:
            raise ValueError('请先启用本项目飞书简报')
        start, end = dated(body.get('start', '')), dated(body.get('end', ''))
        if start != end:
            raise ValueError('读取并发送审阅请每次选择同一天')
        groups = body.get('group_ids')
        allowed = {g['id'] for g in self.photos.settings(pid)['groups']}
        if not isinstance(groups, list) or not groups or any(g not in allowed for g in groups):
            raise ValueError('请选择本项目的照片来源群')
        with self.photos.lock:
            job = self.photos.start(pid, 'wechat', {'start': start, 'end': end, 'group_ids': groups})
            with self.todos.store.connect() as db:
                db.execute('INSERT INTO photo_brief_jobs VALUES (?,?,?)', (pid, job['id'], start))
        return job

    def preview(self, pid: str) -> str:
        data = self.photos.snapshot(pid)
        card = make_card(data['project'], data['photos'], day=datetime.now(TZ).date().isoformat(), preview=True)
        # Repeated clicks with unchanged content reuse the same delivery record and UUID.
        review = self.review_payload(pid, [r['id'] for r in data['photos']], datetime.now(TZ).date().isoformat(), preview=True)
        source = 'preview:' + hashlib.sha256(json.dumps([card, review, self.view(pid)['include_pdf']], sort_keys=True).encode()).hexdigest()
        return self.enqueue(pid, source, card, review)

    def collect_completed(self) -> None:
        with self.todos.store.connect() as db:
            runs = [dict(r) for r in db.execute('''SELECT r.* FROM photo_schedule_runs r
                JOIN photo_brief_settings s ON s.project_id=r.project_id AND s.enabled=1
                WHERE r.occurrence>=s.enabled_from AND r.state IN ('done','partial','failed','interrupted')
                AND NOT EXISTS (SELECT 1 FROM photo_briefs b WHERE b.project_id=r.project_id AND b.source_id=r.id)
                ORDER BY r.occurrence LIMIT 100''')]
            manual = [dict(r) for r in db.execute('''SELECT j.* FROM photo_brief_jobs j
                JOIN photo_brief_settings s ON s.project_id=j.project_id AND s.enabled=1
                WHERE NOT EXISTS (SELECT 1 FROM photo_briefs b WHERE b.project_id=j.project_id AND b.source_id=j.job_id)''')]
        for item in manual:
            job = self.photos.get_job(item['project_id'], item['job_id'])
            if job and job['state'] in TERMINAL:
                runs.append({'project_id': item['project_id'], 'job_id': item['job_id'], 'id': item['job_id'],
                             'state': job['state'], 'occurrence': item['day']})
        for run in runs:
            job = self.photos.get_job(run['project_id'], run['job_id'])
            rows = []
            for key in dict.fromkeys((job or {}).get('added_ids', [])):
                rows.append(self.photos.get(run['project_id'], key))
            card = make_card(self.photos.project(run['project_id']), rows, day=run['occurrence'][:10],
                             job=job, run_state=run['state'])
            review = self.review_payload(run['project_id'], (job or {}).get('photo_ids', []), run['occurrence'][:10], job, run['state'])
            self.enqueue(run['project_id'], run['id'], card, review)

    def send(self, item: dict[str, Any]) -> str:
        import lark_oapi as lark
        from invoice_print_layout.feishu_bot import FeishuGateway
        settings = read_bot_settings(self.workspace / 'feishu_bot.toml')
        if settings.app_id != item['app_id'] or settings.owner_open_id != item['owner']:
            raise BotError('飞书绑定已变化，简报已保留；请核对接收账号')
        client = (lark.Client.builder().app_id(settings.app_id).app_secret(read_bot_secret(settings.app_id))
                  .timeout(30).log_level(lark.LogLevel.ERROR).build())
        gateway = FeishuGateway(client)
        message_id = str(item['message_id'])
        if not message_id:
            message_id = gateway.send_card(item['owner'], json.loads(item['card']), item['id'])
            with self.todos.store.connect() as db:
                db.execute('UPDATE photo_briefs SET message_id=? WHERE id=?', (message_id, item['id']))
        if item['review']:
            attachments = json.loads(item['attachments'])
            if not attachments:
                try:
                    attachments = build_reviews(self.photos.root / item['project_id'] / '审阅册' / item['id'], json.loads(item['review']))
                except (ValueError, OSError):
                    raise BotError('PDF生成未完成，请检查本机照片原件；简报已送达') from None
                self.save_attachments(item['id'], attachments)
            for index, attachment in enumerate(attachments):
                if attachment['message_id']:
                    continue
                path = Path(attachment['path'])
                if hashlib.sha256(path.read_bytes()).hexdigest() != attachment['sha256']:
                    raise BotError('PDF文件校验不一致，附件未发送')
                if not attachment['file_key']:
                    attachment['file_key'] = gateway.upload_review(path)
                    self.save_attachments(item['id'], attachments)
                delivery_id = hashlib.sha256((item['id'] + ':pdf:' + str(index)).encode()).hexdigest()[:32]
                attachment['message_id'] = gateway.send_review(item['owner'], attachment['file_key'], delivery_id)
                self.save_attachments(item['id'], attachments)
        return message_id

    def save_attachments(self, key: str, attachments: list[dict[str, Any]]) -> None:
        with self.todos.store.connect() as db:
            db.execute('UPDATE photo_briefs SET attachments=?,lease=? WHERE id=?', (json.dumps(attachments, ensure_ascii=False), time.time()+600, key))

    def review_file(self, pid: str, key: str, index: int) -> Path:
        self.photos.project(pid)
        with self.todos.store.connect() as db:
            row = db.execute('SELECT attachments FROM photo_briefs WHERE project_id=? AND id=?', (pid, key)).fetchone()
        attachments = json.loads(row['attachments']) if row else []
        if not 0 <= index < len(attachments):
            raise ValueError('此项目没有该审阅册')
        item = attachments[index]
        path = Path(item['path']).resolve()
        if not path.is_relative_to((self.photos.root / pid / '审阅册').resolve()):
            raise ValueError('无效审阅册路径')
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('审阅册校验不一致')
        return path

    def deliver_one(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.todos.store.connect() as db:
            row = db.execute('''SELECT b.* FROM photo_briefs b JOIN photo_brief_settings s
                ON s.project_id=b.project_id AND s.enabled=1
                WHERE (b.state IN ('pending','retry') AND b.next_try<=?) OR (b.state='sending' AND b.lease<=?)
                ORDER BY b.created_at LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return
            item = dict(row)
            # Feishu UUID dedup lasts one hour. Never blindly resend beyond that window.
            if item['first_attempt'] and now - item['first_attempt'] >= 45 * 60:
                db.execute("UPDATE photo_briefs SET state='uncertain',last_error=? WHERE id=?",
                           ('自动重试窗口已结束，请先核对飞书是否收到；原简报仍保留。', item['id']))
                return
            db.execute("UPDATE photo_briefs SET state='sending',attempts=attempts+1,first_attempt=?,lease=? WHERE id=?",
                       (item['first_attempt'] or now, now + 600, item['id']))
        try:
            message_id = self.sender(item)
            if not message_id:
                raise BotError('缺少消息回执，请先核对飞书是否收到')
        except Exception as exc:
            message = str(exc)[:200] if isinstance(exc, BotError) else '飞书连接未完成，请检查网络和机器人配置。'
            state = 'failed' if item['attempts'] >= 2 else 'retry'
            with self.todos.store.connect() as db:
                db.execute('UPDATE photo_briefs SET state=?,last_error=?,next_try=? WHERE id=?',
                           (state, message, now + 60, item['id']))
        else:
            with self.todos.store.connect() as db:
                db.execute("UPDATE photo_briefs SET state='sent',message_id=?,last_error='' WHERE id=?", (message_id, item['id']))

    def pump(self) -> None:
        self.collect_completed()
        self.deliver_one()

    def kick(self) -> None:
        if not self.worker_lock.acquire(blocking=False):
            return
        def run() -> None:
            try:
                self.pump()
            except Exception:
                logging.error('照片简报检查未完成，记录保留供下次检查')
            finally:
                self.worker_lock.release()
        threading.Thread(target=run, name='photo-brief-delivery', daemon=True).start()
