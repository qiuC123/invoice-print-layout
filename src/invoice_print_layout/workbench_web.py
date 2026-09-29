"""Loopback-only workbench. No third-party web assets or exposed credentials."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import secrets
import sqlite3
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, unquote, urlparse

from invoice_print_layout.workbench import ExpenseStore, CATEGORIES, ROLES, STAGES
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.reliability import read_json, save_json
from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.project_todos import ProjectTodos
from invoice_print_layout.photo_schedule import PhotoSchedules
from invoice_print_layout.photo_briefs import PhotoBriefs
from invoice_print_layout.wechat_inbox import WeChatInbox
from invoice_print_layout.workbench_runtime import runtime_info, source_build_id
from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.automatic_rides import organize_rides
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.expense_preferences import Preferences
from invoice_print_layout.report_snapshot import snapshot, REPORT_SECTIONS
from invoice_print_layout.site_photos import SitePhotos
from invoice_print_layout.daily_reports import DailyReports

# Freeze at module/process load; editing files cannot upgrade this value.
STARTUP_BUILD_ID = source_build_id()


def inbox_files(store: ExpenseStore) -> dict[str, Path]:
    folder = store.root / '邮件收件'
    folder.mkdir(exist_ok=True)
    with store.connect() as db:
        used = {r['digest'] for r in db.execute('SELECT digest FROM attachments UNION SELECT digest FROM automatic_ride_files')}
    return {hashlib.sha256(str(p).encode()).hexdigest(): p for p in folder.glob('*.pdf')
            if hashlib.sha256(p.read_bytes()).hexdigest() not in used}


def sync_mail(store: ExpenseStore) -> dict[str, Any]:
    from invoice_print_layout.mail163 import read_mail_settings, read_auth_code, import_pdf_attachments
    paths = ensure_workspace(store.workspace)
    cursor_path = store.root / 'mail-sync.json'
    previous = read_json(cursor_path).get('date')
    days = max(1, (date.today() - date.fromisoformat(previous)).days + 1) if previous else 1
    if days > 365:
        raise ValueError('超过365天未同步，请先人工确定补收范围')
    settings = read_mail_settings(paths.mail_config)
    folder = store.root / '邮件收件'
    result = import_pdf_attachments(paths, settings, read_auth_code(settings.address), days=days,
                                    destination=folder, all_invoice_types=True)
    save_json(cursor_path, {'date': date.today().isoformat()})
    rides = organize_rides(store)
    return {'message': f'补收完成：新增{result.downloaded}份PDF。打车自动整理{rides["created"]}笔，补充{rides["supplemented"]}笔；重复材料{result.duplicates + rides["duplicates"]}份，无效{result.invalid_pdf}份。费用已进入事项台账，未确定的材料留在收件箱。', 'rides': rides}


def make_server(workspace: Path, port: int = 8765) -> ThreadingHTTPServer:
    store = ExpenseStore(workspace)
    organize_rides(store)
    logistics = LogisticsStore(workspace / '后勤' / 'tasks.sqlite3')
    todos = ProjectTodos(logistics)
    projects = ExpenseProjects(store)
    projects.migrate()
    intake = IntakeQueue(store)
    review = ExpenseReview(store)
    preferences = Preferences(store)
    wechat_inbox = WeChatInbox(logistics)
    site_photos = SitePhotos(workspace, logistics)
    daily_reports = DailyReports(workspace, site_photos)
    schedules = PhotoSchedules(todos, site_photos)
    briefs = PhotoBriefs(workspace, todos, site_photos)
    token = secrets.token_urlsafe(32)
    write_lock = threading.Lock()
    automatic_category = AutomaticCategory(store, write_lock)
    assets = Path(__file__).parent / 'web'
    runtime = runtime_info(workspace, STARTUP_BUILD_ID)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass  # No invoice names, payloads or credentials in HTTP logs.

        def allowed(self) -> bool:
            actual_port = cast(ThreadingHTTPServer, self.server).server_port
            hosts = {f'127.0.0.1:{actual_port}', f'localhost:{actual_port}'}
            origin = self.headers.get('Origin')
            return self.headers.get('Host') in hosts and (origin is None or origin in {'http://' + h for h in hosts})

        def send(self, data: bytes, content_type: str, code: int = 200, filename: str | None = None) -> None:
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
            self.send_header('Referrer-Policy', 'no-referrer')
            if filename:
                self.send_header('Content-Disposition', "inline; filename*=UTF-8''" + quote(filename))
            self.end_headers()
            self.wfile.write(data)

        def respond(self, value: Any, code: int = 200) -> None:
            self.send(json.dumps(value, ensure_ascii=False).encode(), 'application/json; charset=utf-8', code)

        def do_GET(self) -> None:
            if not self.allowed():
                self.respond({'error': '仅允许本机访问'}, 403)
                return
            path = unquote(urlparse(self.path).path)
            try:
                if path == '/':
                    self.send((assets / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/logistics':
                    self.send((assets / 'logistics.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/photos':
                    self.send((assets / 'photos.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/api/photos':
                    pid = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond({**site_photos.snapshot(pid), 'briefs': briefs.view(pid), 'token': token})
                elif path == '/api/daily-reports':
                    pid = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond({**daily_reports.view(pid), 'token': token})
                elif path.startswith('/daily-report/'):
                    pid, key, name = path.removeprefix('/daily-report/').split('/')
                    file = daily_reports.file(pid, key, name)
                    self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream', filename=file.name)
                elif path == '/daily-reports.js':
                    self.send((assets / 'daily-reports.js').read_bytes(), 'text/javascript')
                elif path.startswith('/photo-file/'):
                    pid, key = path.removeprefix('/photo-file/').split('/')
                    file = site_photos.file(pid, key)
                    self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                elif path.startswith('/photo-review/'):
                    pid, key, index = path.removeprefix('/photo-review/').split('/')
                    file = briefs.review_file(pid, key, int(index))
                    self.send(file.read_bytes(), 'application/pdf', filename=file.name)
                elif path.startswith('/site-photos/'):
                    project_id, name = path.removeprefix('/site-photos/').split('/', 1)
                    if not project_id or project_id not in {p['id'] for p in logistics.snapshot()['projects']}:
                        raise ValueError('项目不存在')
                    if len(project_id) != 32 or any(c not in '0123456789abcdef' for c in project_id):
                        raise ValueError('无效项目')
                    base = (workspace / '现场进度').resolve()
                    folder = (base / project_id).resolve()
                    if folder.parent != base:
                        raise ValueError('无效目录')
                    manifest = folder / '照片台账.json'
                    if name == 'index.html' and not manifest.exists():
                        self.send('<!doctype html><meta charset="utf-8"><p>本项目尚无已核对的现场照片。</p>'.encode(), 'text/html; charset=utf-8')
                        return
                    entries = json.loads(manifest.read_text(encoding='utf-8'))
                    allowed_images = {row['relative_path'] for row in entries['photos']}
                    if entries['project_id'] != project_id or (name != 'index.html' and name not in allowed_images):
                        raise ValueError('照片未归入当前项目')
                    file = (folder / ('现场进度资料.html' if name == 'index.html' else name)).resolve()
                    if not file.is_relative_to(folder) or file.suffix.lower() not in {'.html', '.jpg', '.jpeg', '.png', '.webp'}:
                        raise ValueError('无效照片文件')
                    self.send(file.read_bytes(), 'text/html; charset=utf-8' if name == 'index.html' else mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                elif path in {'/app.js', '/intake.js', '/receipt.js', '/report.js', '/mail-search.js', '/expense-classifier.js', '/logistics.js', '/style.css', '/logistics.css', '/photos.js', '/photos.css'}:
                    self.send((assets / path[1:]).read_bytes(), 'text/javascript' if path.endswith('.js') else 'text/css')
                elif path == '/api/logistics':
                    self.respond({**logistics.snapshot(), 'token': token})
                elif path == '/api/logistics/inbox':
                    project_id = parse_qs(urlparse(self.path).query).get('project_id', [''])[0]
                    support = parse_qs(urlparse(self.path).query).get('scope', [''])[0] == 'support'
                    self.respond(wechat_inbox.snapshot(project_id, support_only=support))
                elif path == '/api/logistics/todos':
                    project_id = parse_qs(urlparse(self.path).query).get('project_id', [''])[0]
                    self.respond({'items': todos.list(project_id), 'photo_groups': site_photos.settings(project_id)['groups']})
                elif path == '/api/runtime':
                    self.respond(runtime)
                elif path == '/api/intake':
                    self.respond({'queue': [{k: v for k, v in row.items() if k != 'path'} for row in intake.list()]})
                elif path == '/api/preferences':
                    with store.connect() as db:
                        learned = [dict(r) for r in db.execute('SELECT e.*, d.evidence_key IS NULL AS enabled FROM category_experience e LEFT JOIN category_experience_disabled d ON d.evidence_key=e.evidence_key')]
                    self.respond({'rules': preferences.list(), 'sections': REPORT_SECTIONS, 'learned': learned})
                elif path == '/api/report-snapshot':
                    ids = parse_qs(urlparse(self.path).query).get('id', [])
                    if not ids or len(ids) > 200 or len(ids) != len(set(ids)):
                        raise ValueError('请选择不重复的事项')
                    self.respond(snapshot([store.get(key) for key in ids]))
                elif path.startswith('/intake-preview/') or path.startswith('/intake-page/'):
                    import pymupdf
                    from html import escape
                    key = path.rsplit('/', 1)[-1]
                    entry = intake.get(key)
                    with pymupdf.open(entry['path']) as document:  # type: ignore[no-untyped-call]
                        if path.startswith('/intake-page/'):
                            number = int(parse_qs(urlparse(self.path).query).get('page', ['0'])[0])
                            if not 0 <= number < len(document):
                                raise ValueError('无效页码')
                            self.send(document[number].get_pixmap(dpi=120, alpha=False).tobytes('png'), 'image/png')
                        else:
                            pages = ''.join(f'<img alt="原件第{i + 1}页" src="/intake-page/{quote(key)}?page={i}" style="width:100%;height:auto" loading="lazy">' for i in range(len(document)))
                            html = f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{escape(entry["name"])}</title><body style="margin:0">{pages}</body></html>'
                            self.send(html.encode(), 'text/html; charset=utf-8')
                elif path.startswith('/intake-file/'):
                    entry = intake.get(path.removeprefix('/intake-file/'))
                    file = Path(entry['path'])
                    self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream', filename=entry['name'])
                elif path == '/api/state':
                    from invoice_print_layout.report_excel import TEMPLATE_NAME
                    from invoice_print_layout.storage import read_person_name
                    items = store.list_items()
                    self.respond({'items': items, 'category_auto_ready': True,
                                  'projects': projects.list(),
                                  'classification': automatic_category.statuses(items),
                                  'categories': CATEGORIES, 'roles': ROLES,
                                  'stages': STAGES, 'token': token, 'mail_search_ready': True, 'category_suggestion_ready': True,
                                  'report_template_ready': (store.root / TEMPLATE_NAME).is_file(),
                                  'report_person': read_person_name(ensure_workspace(store.workspace).config) or '',
                                  'history_import': read_json(store.root / 'history-import.json'),
                                  'inbox': [{'id': k, 'name': p.name, 'reason': read_json(store.root / 'automatic-rides.json').get('issues', {}).get(k, '')} for k, p in inbox_files(store).items()]})
                elif path.startswith('/mail-candidate/'):
                    from invoice_print_layout.mail_search import candidate_file
                    search_id, candidate_id = path.removeprefix('/mail-candidate/').split('/')
                    file, name = candidate_file(store, search_id, candidate_id)
                    self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream', filename=name)
                elif path.startswith('/file/'):
                    file, name = store.attachment_path(path.removeprefix('/file/'))
                    self.send(file.read_bytes(), mimetypes.guess_type(name)[0] or 'application/octet-stream', filename=name)
                elif path.startswith('/inbox/'):
                    file = inbox_files(store)[path.removeprefix('/inbox/')]
                    self.send(file.read_bytes(), 'application/pdf', filename=file.name)
                elif path.startswith('/export/'):
                    key = path.removeprefix('/export/')
                    if '/' in key or '\\' in key or Path(key).suffix not in {'.pdf', '.md', '.xlsx', '.png'}:
                        raise ValueError('无效的文件')
                    file = store.root / '导出' / key
                    self.send(file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'text/plain; charset=utf-8', filename=file.name)
                else:
                    self.respond({'error': '页面不存在'}, 404)
            except (ValueError, KeyError, OSError):
                self.respond({'error': '文件或记录不可用'}, 404)

        def do_POST(self) -> None:
            if not self.allowed() or not secrets.compare_digest(self.headers.get('X-Workbench-Token', ''), token):
                self.respond({'error': '请求来源无效，请刷新工作台'}, 403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 29 * 1024 * 1024:
                    raise ValueError('请求大小无效；附件最多20MB')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求格式无效')
                path = urlparse(self.path).path
                if path == '/api/shutdown':
                    runtime['ready'] = False
                    self.respond({'ok': True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if path == '/api/category/auto':
                    self.respond(automatic_category.run(body['id'], retry=body.get('retry') is True))
                    return
                if path.startswith('/api/photos/'):
                    pid = str(body.get('project_id', ''))
                    action = path.removeprefix('/api/photos/')
                    photo_result: Any
                    if action == 'brief-settings':
                        photo_result = briefs.configure(pid, body['enabled'], body.get('include_pdf'))
                    elif action == 'daily-match':
                        photo_result = daily_reports.match(pid, str(body.get('date', '')))
                    elif action == 'daily-generate':
                        photo_result = daily_reports.generate(pid, body)
                    elif action == 'brief-preview':
                        photo_result = {'delivery_id': briefs.preview(pid)}
                        briefs.kick()
                    elif action == 'brief-read':
                        photo_result = briefs.read_and_send(pid, body)
                    elif action == 'receive':
                        photo_result = site_photos.receive(pid, body['name'], base64.b64decode(body['data'], validate=True))
                    elif action == 'settings':
                        photo_result = site_photos.save_settings(pid, body)
                    elif action == 'save':
                        photo_result = site_photos.save(pid, body['id'], body['revision'], body['fields'], body.get('confirmed') is True)
                    else:
                        photo_result = site_photos.start(pid, action, body)
                    self.respond(photo_result)
                    return
                if path == '/api/category/suggest':
                    from invoice_print_layout.expense_classifier import suggest
                    # OCR and Jev run outside the lock; only capture/recheck versions under it.
                    self.respond(suggest(store, body['id'], lock=write_lock))
                    return
                with write_lock:
                    result: Any = {}
                    if path == '/api/logistics/projects':
                        result = logistics.create_project(body['name'], body.get('site_name', ''))
                    elif path == '/api/logistics/todos':
                        schedules.validate(body['project_id'], body)
                        result = todos.save(body['project_id'], body)
                    elif path in {'/api/logistics/todos/delete', '/api/logistics/todos/restore'}:
                        result = todos.remove(body['project_id'], body['id'], body['revision'], restore=path.endswith('/restore'))
                    elif path == '/api/projects/move':
                        result = projects.move(body['ids'], body['project_id'], body['versions'])
                    elif path == '/api/intake/receive':
                        result = intake.receive(body['name'], base64.b64decode(body['data'], validate=True),
                                                project_id=body.get('project_id', ''), batch=body.get('batch', ''))
                    elif path == '/api/intake/analyze':
                        result = intake.analyze(body['id'])
                    elif path == '/api/intake/resolve':
                        result = intake.resolve(body['id'], body['revision'], body['fields'], body['role'],
                                                body.get('item_id', ''), body.get('expected_version', ''))
                    elif path == '/api/intake/mail':
                        result = intake.import_mail()
                    elif path == '/api/intake/claim':
                        from invoice_print_layout.invoice_claim import claim
                        url = str(body.get('url', ''))
                        if body.get('id'):
                            from invoice_print_layout.mail_search import qr_value
                            url = qr_value(Path(intake.get(body['id'])['path']).read_bytes())
                        result = claim(intake, url, body.get('project_id', ''))
                    elif path == '/api/review/confirm':
                        result = review.confirm(body['id'], body['version'], body)
                    elif path == '/api/review/merge-preview':
                        result = review.merge_preview(body['ids'], body['target'])
                    elif path == '/api/review/merge':
                        result = review.merge(body['ids'], body['target'], body['token'], body['reason'])
                    elif path == '/api/preferences/save':
                        result = preferences.save(body)
                    elif path == '/api/preferences/learned':
                        with store.connect() as db:
                            if body.get('category') not in CATEGORIES:
                                raise ValueError('费用类别无效')
                            db.execute('UPDATE category_experience SET category=?,origin=?,conflict=0 WHERE evidence_key=?',
                                       (body['category'], 'human', body['key']))
                            if body.get('enabled') is True:
                                db.execute('DELETE FROM category_experience_disabled WHERE evidence_key=?', (body['key'],))
                            else:
                                db.execute('INSERT OR IGNORE INTO category_experience_disabled VALUES (?)', (body['key'],))
                        result = {'ok': True}
                    elif path == '/api/preferences/apply':
                        current = store.get(body['id'])
                        if current['version'] != body['version']:
                            raise ValueError('事项已变化，请刷新')
                        applied = preferences.apply(current, goods=body.get('goods', ''), purpose=body.get('purpose', ''))
                        if applied['conflicts']:
                            raise ValueError('经验冲突，请停用冲突规则后重试')
                        result = store.update(current['id'], {**current, **applied['changes'], 'expected_version': current['version']})
                    elif path == '/api/report-preview':
                        from invoice_print_layout.reimbursement import create_report
                        import pymupdf
                        result = create_report(store, body['ids'], body.get('options'), submit=False)
                        folder = store.root / '导出'
                        pages = []
                        with pymupdf.open(folder / result['pdf']) as document:  # type: ignore[no-untyped-call]
                            for number, page in enumerate(document):
                                name = f"{Path(result['pdf']).stem}-preview-{number + 1}.png"
                                page.get_pixmap(dpi=120, alpha=False).save(folder / name)
                                pages.append(name)
                        result['preview_pages'] = pages
                    elif path == '/api/logistics/groups':
                        result = wechat_inbox.save_group(body)
                    elif path == '/api/logistics/groups/category':
                        wechat_inbox.change_group_category(body['project_id'], body['id'], body['category'])
                        result = {'ok': True}
                    elif path == '/api/logistics/messages/assign':
                        wechat_inbox.assign(body['key'], body['project_id'], body['category'])
                        result = {'ok': True}
                    elif path == '/api/create':
                        result = store.create(body)
                    elif path == '/api/update':
                        before = store.get(body['id'])
                        result = store.update(body['id'], body)
                        automatic_category.feedback(before, result)
                    elif path == '/api/transition':
                        before = store.get(body['id'])
                        result = store.transition(body['id'], body['action'], expected_version=body.get('expected_version'))
                        automatic_category.feedback(before, result, verified=body['action'] == 'verify')
                    elif path == '/api/upload':
                        result = store.add_attachment(body['id'], body['name'], base64.b64decode(body['data'], validate=True), body['role'])
                    elif path in {'/api/recognize', '/api/receipt'}:
                        from invoice_print_layout.receipt_intake import recognize, save_receipt
                        payload = base64.b64decode(body['data'], validate=True)
                        if path == '/api/recognize':
                            result = recognize(store, body['name'], payload)
                        else:
                            result = save_receipt(store, body['name'], payload, body['fields'], body['role'])
                    elif path == '/api/role':
                        store.set_role(body['attachment_id'], body['role'])
                    elif path == '/api/import':
                        result = store.import_history()
                    elif path == '/api/mail/search':
                        from invoice_print_layout.mail_search import search_invoices
                        result = search_invoices(store, body['id'], body['start'], body['end'])
                    elif path == '/api/mail/associate':
                        from invoice_print_layout.mail_search import associate
                        result = associate(store, body['id'], body['search_id'], body['candidate_id'], body.get('role', 'invoice'))
                    elif path == '/api/mail':
                        result = sync_mail(store)
                        intake.import_mail(body.get('project_id', ''))
                        for received in intake.list():
                            if received['status'] == 'received' and received['source'] == 'mail':
                                intake.analyze(received['id'])
                        result['intake'] = {'pending': sum(q['status'] in {'received', 'review'} for q in intake.list())}
                    elif path == '/api/mail/organize':
                        result = organize_rides(store)
                    elif path == '/api/assign':
                        file = inbox_files(store)[body['file_id']]
                        result = store.add_attachment(body['id'], file.name, file.read_bytes(), body['role'])
                    elif path == '/api/export':
                        from invoice_print_layout.workbench_export import export_items
                        result = export_items(store, body['ids'])
                    elif path == '/api/report':
                        from invoice_print_layout.reimbursement import create_report
                        result = create_report(store, body['ids'], body.get('options'))
                    elif path == '/api/report-template':
                        from invoice_print_layout.report_excel import install_template
                        install_template(store.root, base64.b64decode(body['data'], validate=True))
                        result = {'ok': True}
                    else:
                        raise ValueError('操作不存在')
                self.respond(result)
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
                self.respond({'error': str(exc)}, 400)
            except Exception:
                self.respond({'error': '操作未完成，原文件保留。请检查服务日志或重试。'}, 500)

    class WorkbenchServer(ThreadingHTTPServer):
        next_schedule_check = 0.0

        def service_actions(self) -> None:
            if not runtime['ready']:
                return
            if time.monotonic() >= self.next_schedule_check:
                self.next_schedule_check = time.monotonic() + 10
                try:
                    with write_lock:
                        schedules.tick()
                    briefs.kick()
                except (OSError, sqlite3.Error):
                    # A transient store error leaves durable occurrences for the next tick.
                    pass

    server = WorkbenchServer(('127.0.0.1', port), Handler)
    server.daemon_threads = False  # server_close waits for in-flight writes/exports.
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('workspace'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    # Windows process-owned byte lock: closing/crashing releases it automatically.
    # It also prevents launching the same workspace on a second port.
    import msvcrt
    args.workspace.mkdir(parents=True, exist_ok=True)
    instance = (args.workspace / '.workbench-instance.lock').open('a+b')
    if instance.tell() == 0:
        instance.write(b'0')
        instance.flush()
    instance.seek(0)
    try:
        msvcrt.locking(instance.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        instance.close()
        raise SystemExit('此工作区已有工作台实例，请使用已有入口。') from exc
    ExpenseStore(args.workspace).import_history()
    server = make_server(args.workspace, args.port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        instance.close()


if __name__ == '__main__':
    main()
