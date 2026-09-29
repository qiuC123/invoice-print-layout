"""Daily photo collection while the local workbench server is running."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from invoice_print_layout.logistics import TZ
from invoice_print_layout.project_todos import ProjectTodos
from invoice_print_layout.site_photos import SitePhotos


class PhotoSchedules:
    def __init__(self, todos: ProjectTodos, photos: SitePhotos):
        self.todos, self.photos = todos, photos

    def validate(self, pid: str, body: dict[str, Any]) -> None:
        kind, config = self.todos.validate_schedule(body)
        if kind != 'scheduled' or not config['enabled']:
            return
        settings = self.photos.settings(pid)
        if not set(config['group_ids']).issubset({g['id'] for g in settings['groups']}):
            raise ValueError('所选群不是当前项目的照片来源群')
        if not settings['account'] or not settings.get('auto_classify'):
            raise ValueError('请先在现场照片中配置微信账号并开启自动分类归档')

    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(TZ)
        if now.utcoffset() is None:
            raise ValueError('调度时间必须包含时区')
        now = now.astimezone(TZ)
        today = now.date().isoformat()
        with self.todos.store.connect() as db:
            rows = db.execute('SELECT t.*,p.paused FROM project_todos t JOIN projects p ON p.id=t.project_id').fetchall()
            tasks = {r['id']: self.todos.public(r) for r in rows}
            for task in tasks.values():
                config = task['schedule']
                if task['deleted_at'] or task['kind'] != 'scheduled' or not config.get('enabled') or task['paused']:
                    continue
                if today < config['start_on'] or (config['end_on'] and today > config['end_on']):
                    continue
                due = [t for t in config['times'] if t <= now.strftime('%H:%M')]
                if not due:
                    continue
                # If the computer starts late, catch up only the latest time today.
                occurrence = today + ' ' + max(due)
                db.execute('INSERT OR IGNORE INTO photo_schedule_runs VALUES (?,?,?,?,?,?,?)',
                           (uuid.uuid4().hex, task['id'], task['project_id'], occurrence, 'queued', uuid.uuid4().hex, '等待读取'))
            runs = [dict(r) for r in db.execute("SELECT * FROM photo_schedule_runs WHERE state IN ('queued','running') ORDER BY occurrence")]
        for run in runs:
            try:
                task = tasks[run['task_id']]
                job = self.photos.get_job(run['project_id'], run['job_id'])
                if job is None:
                    if run['occurrence'][:10] < today:
                        self.finish(run['id'], 'cancelled', '已错过执行日期，可到现场照片手动补读')
                        continue
                    config = task['schedule']
                    if (task['deleted_at'] or task['kind'] != 'scheduled' or not config.get('enabled') or task['paused']
                            or not config['start_on'] <= run['occurrence'][:10]
                            or (config['end_on'] and run['occurrence'][:10] > config['end_on'])):
                        self.finish(run['id'], 'cancelled', '任务已暂停或执行日期已调整，本次未读取')
                        continue
                    self.validate(run['project_id'], task)
                    job = self.photos.start(run['project_id'], 'wechat', {
                        'group_ids': config['group_ids'], 'start': run['occurrence'][:10], 'end': run['occurrence'][:10],
                    }, job_id=run['job_id'])
                message = str(job['message'])
                if job['state'] in ('done', 'partial'):
                    message = f"新增 {job['added']} · 重复 {job['duplicates']} · 待核对 {len(job['issues'])}"
                self.finish(run['id'], job['state'], message)
            except ValueError as exc:
                if '已有读取或识别任务' in str(exc):
                    self.finish(run['id'], 'queued', '本项目正在处理照片，完成后继续')
                else:
                    self.finish(run['id'], 'failed', str(exc)[:300])
            except Exception:
                self.finish(run['id'], 'failed', '读取未完成，请在现场照片查看并手动重试')

    def finish(self, run_id: str, state: str, message: str) -> None:
        with self.todos.store.connect() as db:
            db.execute('UPDATE photo_schedule_runs SET state=?,message=? WHERE id=?', (state, message, run_id))
