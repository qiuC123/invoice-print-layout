"""Local workbench with explicitly paired, optional LAN phone access."""
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
from typing import Any, TYPE_CHECKING, cast
from urllib.parse import parse_qs, quote, unquote, urlparse

from invoice_print_layout.workbench import ExpenseStore, CATEGORIES, ROLES, STAGES
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.reliability import read_json, save_json
from invoice_print_layout.workbench_runtime import APP_MODES, runtime_info, source_build_id
from invoice_print_layout.phone_link import PhoneLink, drain_rejected_body
from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.automatic_rides import organize_rides
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.intake_usage import rail_payment_not_required, RAIL_PAYMENT_UNUSED_REASON
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.invoice_ai_settings import InvoiceAISettings, KeyHostChanged
from invoice_print_layout.workbench_settings import WorkbenchSettings, SettingsError
from invoice_print_layout.mail163 import ImapFactory
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.expense_review import ExpenseReview
from invoice_print_layout.reimbursement_people import ReimbursementPeople
from invoice_print_layout.expense_preferences import Preferences
from invoice_print_layout.report_snapshot import snapshot, REPORT_SECTIONS

if TYPE_CHECKING:
    from invoice_print_layout.logistics import LogisticsStore
    from invoice_print_layout.project_todos import ProjectTodos
    from invoice_print_layout.photo_schedule import PhotoSchedules
    from invoice_print_layout.wechat_inbox import WeChatInbox
    from invoice_print_layout.site_photos import SitePhotos
    from invoice_print_layout.daily_reports import DailyReports

# Freeze at module/process load; editing files cannot upgrade this value.
STARTUP_BUILD_ID = source_build_id()

WORKBENCH_PATHS = {'/logistics', '/photos', '/logistics.js', '/logistics.css',
                   '/photos.js', '/photos.css', '/daily-reports.js'}
WORKBENCH_PREFIXES = ('/api/logistics', '/api/photos', '/api/daily-reports',
                      '/daily-report/', '/photo-file/', '/site-photos/')


def workbench_only(path: str) -> bool:
    return path in WORKBENCH_PATHS or path.startswith(WORKBENCH_PREFIXES)


def inbox_files(store: ExpenseStore) -> dict[str, Path]:
    folder = store.root / '邮件收件'
    folder.mkdir(exist_ok=True)
    with store.connect() as db:
        used = {r['digest'] for r in db.execute('SELECT digest FROM attachments UNION SELECT digest FROM automatic_ride_files')}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='expense_project_deleted_materials'").fetchone():
            used.update(row['digest'] for row in db.execute('SELECT digest FROM expense_project_deleted_materials'))
    return {hashlib.sha256(str(p).encode()).hexdigest(): p for p in folder.glob('*.pdf')
            if hashlib.sha256(p.read_bytes()).hexdigest() not in used}


def sync_mail(store: ExpenseStore, *, organize: bool = True) -> dict[str, Any]:
    from invoice_print_layout.mail163 import read_mail_settings, read_auth_code, import_pdf_attachments
    paths = ensure_workspace(store.workspace)
    cursor_path = store.root / 'mail-sync.json'
    cursor = read_json(cursor_path)
    previous = cursor.get('date')
    days = max(1, (date.today() - date.fromisoformat(previous)).days + 1) if previous else 1
    if days > 365:
        raise ValueError('超过365天未同步，请先人工确定补收范围')
    settings = read_mail_settings(paths.mail_config)
    folder = store.root / '邮件收件'
    result = import_pdf_attachments(paths, settings, read_auth_code(settings.address), days=days,
                                    destination=folder, all_invoice_types=True, include_link_materials=not organize)
    outstanding_links = cursor.get('incomplete') or read_json(store.root / 'mail-link-failures.json').get('failures')
    if not result.link_failures and (not organize or not outstanding_links):
        save_json(cursor_path, {'date': date.today().isoformat()})
    elif not previous:
        # Remember the initial retry window even when the first ever collection
        # partly fails; tomorrow must still include these messages.
        save_json(cursor_path, {'date': date.today().isoformat(), 'incomplete': True})
    if not organize:
        # Signed download URLs remain private; feedback exposes only mail identity
        # and stable reasons. A failed day must stay in the next retry window.
        save_json(store.root / 'mail-link-failures.json', {'failures': result.link_failures})
        failures = [{key: failure[key] for key in ('message_uid', 'subject', 'order_number', 'reason')
                     if key in failure} for failure in result.link_failures]
        retry = f' {len(failures)}个领票链接未收取，未推进补收日期；可再次补收重试。' if failures else ''
        return {'message': f'补收完成：新增{result.downloaded}份材料（正文链接{result.link_downloaded}份、图片{result.image_downloaded}张），重复{result.duplicates}份；原件已保存，材料留在合成台，确认创建后进入待核对。{retry}',
                'downloaded': result.downloaded, 'duplicates': result.duplicates,
                'link_downloaded': result.link_downloaded, 'image_downloaded': result.image_downloaded,
                'link_failures': failures}
    rides = organize_rides(store)
    return {'message': f'补收完成：新增{result.downloaded}份PDF。打车自动整理{rides["created"]}笔，补充{rides["supplemented"]}笔；重复材料{result.duplicates + rides["duplicates"]}份，无效{result.invalid_pdf}份。费用已进入事项台账，未确定的材料留在收件箱。', 'rides': rides}


def classify_intake(intake: IntakeQueue, values: dict[str, Any]) -> dict[str, Any]:
    """Correct a material card without creating or verifying an expense."""
    from invoice_print_layout.workbench import amount_cents
    role = values.get('role')
    fields = values.get('fields', {})
    if role not in {'invoice', 'receipt', 'payment', 'other'} or not isinstance(fields, dict):
        raise ValueError('请选择发票、收据、付款截图或待分类材料')
    allowed = {'title', 'merchant', 'amount', 'category', 'expense_date', 'order_number', 'note',
               'payment_time', 'payment_status', 'invoice_number', 'project_id',
               'shopping_platform', 'shop_name', 'payment_channel'}
    if any(key not in allowed or not isinstance(value, str) for key, value in fields.items()):
        raise ValueError('材料信息无效')
    if fields.get('amount') and amount_cents(fields['amount']) <= 0:
        raise ValueError('请输入大于零的实际金额')
    with intake.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        entry = intake.get(values['id'])
        if entry['status'] not in {'received', 'review'} or entry['item_id']:
            raise ValueError('材料已经关联，请刷新合成台')
        if entry['revision'] != values.get('revision'):
            raise ValueError('材料已变化，请刷新合成台')
        if entry.get('qr_status') == 'manual' and role != 'other':
            raise ValueError('请先领取二维码对应的正式发票')
        project_id = fields.get('project_id', '')
        if project_id:
            if not any(p['id'] == project_id for p in ExpenseProjects(intake.store).list()):
                raise ValueError('项目不存在')
            if entry.get('project_id') not in {'', project_id}:
                raise ValueError('材料属于另一项目，请切换项目后处理')
            entry['project_id'] = project_id
        entry.setdefault('transaction_source_role', entry['role'])
        from invoice_print_layout.recognition_corrections import record
        record(db, entry['id'], entry.get('fields', {}), fields, entry.get('field_sources', {}))
        entry.update(role=role, fields={**entry.get('fields', {}),
                                      **{k: v for k, v in fields.items() if k != 'project_id'}},
                     manual_fields=sorted(set(entry.get('manual_fields', [])) | (set(fields) - {'project_id'})),
                     manual_role=True,
                     status='review', revision=entry['revision'] + 1,
                     result='人工补充材料信息，尚未关联费用')
        if rail_payment_not_required(entry):
            entry.update(status='unused', unused_reason=RAIL_PAYMENT_UNUSED_REASON,
                         result=RAIL_PAYMENT_UNUSED_REASON)
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                   (json.dumps(entry, ensure_ascii=False), entry['id']))
    return {k: v for k, v in entry.items() if k != 'path'}


def make_server(workspace: Path, port: int = 8765, *, mode: str = 'workbench',
                imap_factory: ImapFactory | None = None) -> ThreadingHTTPServer:
    if mode not in APP_MODES:
        raise ValueError('Unsupported application mode')
    store = ExpenseStore(workspace)
    projects = ExpenseProjects(store)
    invoice_ai = GlmInvoiceAI(store.root)
    ai_settings = InvoiceAISettings(store.root)
    intake = IntakeQueue(store, ai=invoice_ai)
    matching = MaterialMatching(store, intake, ai=invoice_ai)
    from invoice_print_layout.synthesis_expenses import SynthesisExpenses
    synthesis_expenses = SynthesisExpenses(store)
    review = ExpenseReview(store)
    reimbursement_people = ReimbursementPeople(store)
    preferences = Preferences(store)
    if mode == 'workbench':
        from invoice_print_layout.logistics import LogisticsStore
        from invoice_print_layout.project_todos import ProjectTodos
        from invoice_print_layout.photo_schedule import PhotoSchedules
        from invoice_print_layout.wechat_inbox import WeChatInbox
        from invoice_print_layout.site_photos import SitePhotos
        from invoice_print_layout.daily_reports import DailyReports
        organize_rides(store)
        projects.migrate()
        logistics = LogisticsStore(workspace / '后勤' / 'tasks.sqlite3')
        todos = ProjectTodos(logistics)
        wechat_inbox = WeChatInbox(logistics)
        site_photos = SitePhotos(workspace, logistics)
        daily_reports = DailyReports(workspace, site_photos)
        schedules = PhotoSchedules(todos, site_photos)
    token = secrets.token_urlsafe(32)
    write_lock = threading.Lock()
    intake_run_lock = threading.Lock()
    matching_run_lock = threading.Lock()
    from invoice_print_layout.intake_background import BackgroundIntake

    def scan_received_batch(project_id: str) -> dict[str, Any]:
        with matching_run_lock:
            return matching.suggest(project_id)

    background_intake = BackgroundIntake(intake, intake_run_lock, scan_received_batch)
    automatic_category = AutomaticCategory(store, write_lock)
    from invoice_print_layout.agent_jobs import AgentService
    invoice_agent = AgentService(store)
    from invoice_print_layout.mail_agent import MailAgentService
    mail_agent = MailAgentService(store, invoice_agent)
    settings_service = WorkbenchSettings(store, mail_agent, invoice_ai, imap_factory=imap_factory)
    with store.connect() as db:
        from invoice_print_layout.expense_projects import install_project_guards
        install_project_guards(db)
    assets = Path(__file__).parent / 'web'
    runtime = runtime_info(workspace, STARTUP_BUILD_ID, mode)

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
            if mode == 'invoice' and workbench_only(path):
                self.respond({'error': '页面不存在'}, 404)
                return
            try:
                if path == '/':
                    self.send((assets / 'index.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/agent':
                    self.send((assets / 'agent.html').read_bytes(), 'text/html; charset=utf-8')
                elif path in {'/agent.js', '/agent.css', '/agent-projects.js', '/agent-projects.css',
                              '/agent-details.js', '/agent-details.css', '/manual-agent.js',
                              '/manual-agent.css', '/manual-agent-bridge.js'}:
                    self.send((assets / path[1:]).read_bytes(), 'text/javascript' if path.endswith('.js') else 'text/css')
                elif path == '/api/agent/mail':
                    self.respond(mail_agent.status())
                elif path == '/api/agent/jobs':
                    pid = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond({'jobs': invoice_agent.jobs.list(pid), 'token': token,
                                  'model': invoice_ai.public_status()})
                elif path == '/api/agent/job':
                    key = parse_qs(urlparse(self.path).query).get('id', [''])[0]
                    self.respond({'job': invoice_agent.jobs.get(key)})
                elif path == '/logistics':
                    self.send((assets / 'logistics.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/photos':
                    self.send((assets / 'photos.html').read_bytes(), 'text/html; charset=utf-8')
                elif path == '/api/photos':
                    pid = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond({**site_photos.snapshot(pid), 'token': token})
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
                elif path in {'/settings.js', '/settings.css', '/phone-link.js', '/phone-link.css', '/app.js', '/workbench-theme.js', '/workbench-theme.css', '/project-menu.js', '/project-menu.css', '/review-edit.js', '/review-edit.css', '/review-batch.js', '/review-batch.css', '/amount-pairing.js', '/amount-pairing.css', '/reimbursement-people.js', '/reimbursement-people.css', '/intake.js', '/intake-progress.js', '/matching-desk.js', '/matching-desk.css', '/synthesis.js', '/receipt.js', '/report.js', '/report-preview.css', '/multi-filter.js', '/multi-filter.css', '/mail-search.js', '/expense-classifier.js', '/logistics.js', '/style.css', '/logistics.css', '/photos.js', '/photos.css'}:
                    self.send((assets / path[1:]).read_bytes(), 'text/javascript' if path.endswith('.js') else 'text/css')
                elif path == '/favicon.svg':
                    self.send((assets / 'favicon.svg').read_bytes(), 'image/svg+xml')
                elif path == '/api/logistics':
                    self.respond({**logistics.snapshot(), 'token': token})
                elif path == '/api/logistics/inbox':
                    project_id = parse_qs(urlparse(self.path).query).get('project_id', [''])[0]
                    support = parse_qs(urlparse(self.path).query).get('scope', [''])[0] == 'support'
                    self.respond(wechat_inbox.snapshot(project_id, support_only=support))
                elif path == '/api/logistics/todos':
                    project_id = parse_qs(urlparse(self.path).query).get('project_id', [''])[0]
                    self.respond({'items': todos.list(project_id), 'photo_groups': site_photos.settings(project_id)['groups']})
                elif path == '/api/phone-link':
                    self.respond(phone_link.status(include_addresses=True))
                elif path == '/api/runtime':
                    self.respond(runtime)
                elif path == '/api/intake':
                    self.respond({'queue': [{k: v for k, v in row.items() if k != 'path'} for row in intake.list()]})
                elif path == '/api/intake/progress':
                    progress_project = parse_qs(urlparse(self.path).query, keep_blank_values=True).get('project', [None])[0]
                    self.respond(background_intake.progress(progress_project))
                elif path == '/api/reimbursement-people':
                    person_project = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond(reimbursement_people.snapshot(person_project))
                elif path == '/api/settings':
                    self.respond(settings_service.public())
                elif path == '/api/invoice-ai':
                    self.respond(invoice_ai.public_status())
                elif path == '/api/material-matching':
                    pid = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond({**matching.board(pid), 'ai': invoice_ai.public_status()})
                elif path == '/api/material-matching/suggestions':
                    pid = parse_qs(urlparse(self.path).query).get('project', [''])[0]
                    self.respond(matching.suggest(pid))
                elif path == '/api/preferences':
                    with store.connect() as db:
                        learned = [dict(r) for r in db.execute('SELECT e.*, d.evidence_key IS NULL AS enabled FROM category_experience e LEFT JOIN category_experience_disabled d ON d.evidence_key=e.evidence_key')]
                    self.respond({'rules': preferences.list(), 'sections': REPORT_SECTIONS, 'learned': learned})
                elif path == '/api/report-snapshot':
                    ids = parse_qs(urlparse(self.path).query).get('id', [])
                    if not ids or len(ids) > 200 or len(ids) != len(set(ids)):
                        raise ValueError('请选择不重复的事项')
                    items = store.get_many(ids)
                    if any(item['deleted'] for item in items):
                        raise ValueError('已删除事项不能参与报销汇总，请先恢复')
                    self.respond(snapshot(items))
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
                elif path.startswith('/attachment-preview/'):
                    import pymupdf
                    from html import escape
                    key = path.removeprefix('/attachment-preview/')
                    file, name = store.attachment_path(key)
                    with pymupdf.open(file) as document:  # type: ignore[no-untyped-call]
                        pages = ''.join(f'<img alt="原件第{i + 1}页" src="/attachment-page/{quote(key)}?page={i}" style="width:100%;height:auto" loading="lazy">' for i in range(len(document)))
                    self.send(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{escape(name)}</title><body style="margin:0">{pages}</body></html>'.encode(), 'text/html; charset=utf-8')
                elif path.startswith('/attachment-page/'):
                    import pymupdf
                    file, _ = store.attachment_path(path.removeprefix('/attachment-page/'))
                    number = int(parse_qs(urlparse(self.path).query).get('page', ['0'])[0])
                    with pymupdf.open(file) as document:  # type: ignore[no-untyped-call]
                        if not 0 <= number < len(document):
                            raise ValueError('无效页码')
                        self.send(document[number].get_pixmap(dpi=120, alpha=False).tobytes('png'), 'image/png')
                elif path == '/api/state':
                    from invoice_print_layout.report_excel import TEMPLATE_NAME
                    from invoice_print_layout.report_types import report_types
                    from invoice_print_layout.storage import read_person_name
                    items = store.list_items(include_deleted=True)
                    self.respond({'items': items, 'app_mode': mode,
                                  'logistics_available': mode == 'workbench', 'category_auto_ready': True,
                                  'projects': projects.list(),
                                  'classification': automatic_category.statuses([item for item in items if not item['deleted']]),
                                  'categories': CATEGORIES, 'roles': ROLES, 'report_types': report_types(),
                                  'stages': STAGES, 'token': token, 'mail_search_ready': True, 'category_suggestion_ready': True,
                                  'background_intake_ready': True,
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
                drain_rejected_body(self)
                self.respond({'error': '请求来源无效，请刷新工作台'}, 403)
                return
            path = unquote(urlparse(self.path).path)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 29 * 1024 * 1024:
                    raise ValueError('请求大小无效；附件最多20MB')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('请求格式无效')
                if path in {'/api/settings', '/api/settings/test', '/api/settings/models',
                            '/api/settings/organize', '/api/settings/mail', '/api/settings/mail/test'}:
                    try:
                        if length > 65536:
                            raise ValueError('设置请求过大')
                        if path == '/api/settings/organize':
                            settings_service.organize.update(body)
                            settings_result = settings_service.organize_status()
                        elif path == '/api/settings/mail':
                            settings_result = settings_service.save_mail(body)
                        elif path == '/api/settings/mail/test':
                            settings_result = settings_service.test_mail(body)
                        elif path == '/api/settings/test':
                            settings_result = ai_settings.test(body)
                        elif path == '/api/settings/models':
                            settings_result = ai_settings.models(body)
                        else:
                            ai_settings.update(body)
                            settings_result = settings_service.public()
                        supplied_key = body.get('api_key')
                        if isinstance(supplied_key, str) and supplied_key:
                            settings_result = json.loads(json.dumps(settings_result, ensure_ascii=False).replace(supplied_key, '••••'))
                        self.respond(settings_result)
                    except (KeyHostChanged, SettingsError) as exc:
                        self.respond({'error': str(exc)}, 400)
                    except (ValueError, OSError, UnicodeError, TypeError, KeyError, RecursionError):
                        self.respond({'error': '设置或连接不可用，请检查填写内容。'}, 400)
                    return
                if mode == 'invoice' and workbench_only(path):
                    self.respond({'error': '操作不存在'}, 404)
                    return
                if path in {'/api/phone-link/start', '/api/phone-link/stop', '/api/phone-link/rotate', '/api/phone-link/connect'}:
                    if path.endswith('/start'):
                        self.respond(phone_link.start_remote() if body.get('mode') == 'remote' else
                                     phone_link.start(str(body.get('address', ''))))
                    elif path.endswith('/connect'):
                        self.respond(phone_link.connect_remote())
                    elif path.endswith('/rotate'):
                        self.respond(phone_link.rotate())
                    else:
                        self.respond(phone_link.stop())
                    return
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
                    if action == 'daily-match':
                        photo_result = daily_reports.match(pid, str(body.get('date', '')))
                    elif action == 'daily-generate':
                        photo_result = daily_reports.generate(pid, body)
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
                if path == '/api/mail' and (invoice_ai.enabled or body.get('material_only') is True):
                    if not intake_run_lock.acquire(blocking=False):
                        raise ValueError('材料正在识别，请稍后补收；本次没有重复执行')
                    try:
                        mail_result = sync_mail(store, organize=False)
                        imported = intake.import_mail(body.get('project_id', ''))
                        received_ids = [entry['id'] for entry in intake.list()
                                        if entry['status'] == 'received' and entry['source'] == 'mail'
                                        and (not body.get('project_id') or entry.get('project_id') == body['project_id'])]
                        converted = claim_pending = 0
                        preparation_failures: list[dict[str, str]] = []
                        for key in received_ids:
                            try:
                                intake.prepare_batch_entry(key)
                                updated = intake.get(key)
                                converted += updated.get('qr_status') == 'converted'
                                claim_pending += updated.get('qr_status') == 'manual'
                            except (ValueError, OSError, RuntimeError):
                                preparation_failures.append({'id': key, 'reason': '材料整理未完成，原件保留；可在材料区重试。'})
                        entries = intake.list()
                        import_failures = imported.get('failures', []) if isinstance(imported, dict) else []
                        mail_result['intake'] = {'pending': sum(entry['status'] in {'received', 'review'}
                                                               for entry in entries),
                                                'received': imported.get('received', 0) if isinstance(imported, dict) else 0,
                                                'duplicates': imported.get('duplicate', 0) if isinstance(imported, dict) else 0,
                                                'prepared': len(received_ids) - len(preparation_failures),
                                                'converted_to_pdf': converted, 'claim_pending': claim_pending,
                                                'import_failures': import_failures,
                                                'preparation_failures': preparation_failures}
                        mail_result['message'] += f' 已领取{converted}份正式PDF，{claim_pending}份需手工领票。'
                        if preparation_failures:
                            mail_result['message'] += f' {len(preparation_failures)}份材料整理未完成，原件保留，可在材料区重试。'
                        if import_failures:
                            mail_result['message'] += f' {len(import_failures)}份已下载材料未能入队，原件保留在邮件收件，请检查或更换文件；其余材料已继续处理。'
                        if body.get('project_id') and matching_run_lock.acquire(blocking=False):
                            try:
                                paired = matching.suggest(body['project_id'])
                                mail_result['matching'] = paired
                                mail_result['message'] += f' 找到{paired["suggested"]}组待合成候选；确认创建后进入待核对。'
                            finally:
                                matching_run_lock.release()
                        self.respond(mail_result)
                    finally:
                        intake_run_lock.release()
                    return
                if path in {'/api/agent/mail/configure', '/api/agent/mail/collect', '/api/agent/mail/assign'}:
                    if path.endswith('/configure'):
                        mail_agent_result = mail_agent.configure(body)
                    elif path.endswith('/assign'):
                        mail_agent_result = mail_agent.assign(body.get('id', ''), body.get('project_id', ''))
                    else:
                        mail_agent_result = mail_agent.collect_now()
                    self.respond(mail_agent_result)
                    return
                if path == '/api/agent/facts/restore':
                    from invoice_print_layout.agent_confirmed_facts import restore
                    with write_lock:
                        fact = restore(invoice_agent.jobs, body)
                    self.respond({'fact': fact})
                    return
                if path in {'/api/agent/start', '/api/agent/cancel', '/api/agent/answer', '/api/agent/retry',
                            '/api/agent/refresh-question'}:
                    with write_lock:
                        if path.endswith('/start'):
                            job = invoice_agent.jobs.create(
                                body.get('project_id', ''), body.get('material_ids'), body.get('idempotency_key'),
                                use_model=settings_service.organize.effective_model(
                                    body.get('use_model', False), invoice_ai.public_status()['available']),
                                options=body.get('options'))
                        elif path.endswith('/retry'):
                            job = invoice_agent.jobs.retry(body.get('id', ''))
                        elif path.endswith('/cancel'):
                            job = invoice_agent.jobs.get(body.get('id', ''))
                            if job['state'] not in {'queued', 'running'}:
                                raise ValueError('任务已经结束')
                            job = invoice_agent.jobs.stop(job['id'], 'cancelled', '任务已取消，已有结果保留')
                        elif path.endswith('/refresh-question'):
                            from invoice_print_layout.agent_tools import InvoiceTools
                            job = invoice_agent.jobs.refresh_question(body.get('id', ''), body.get('expected_updated_at'),
                                                                     InvoiceTools(invoice_agent.jobs).refresh_question)
                        else:
                            job = invoice_agent.jobs.answer(body.get('id', ''), body.get('choice', ''),
                                                            body.get('question_id'))
                    if not path.endswith('/refresh-question'):
                        invoice_agent.kick()
                    self.respond({'job': job})
                    return
                if path in {'/api/intake/analyze', '/api/intake/prepare'} and type(body.get('force_ai', False)) is not bool:
                    raise ValueError('AI辅助识别选项必须是布尔值')
                if path == '/api/intake/enqueue':
                    self.respond(background_intake.enqueue(body.get('batch'), body.get('ids'),
                                                           force_ai=body.get('force_ai', False)))
                    return
                if path in {'/api/intake/analyze', '/api/intake/prepare', '/api/material-matching/auto'}:
                    # Single-proof writes recheck revisions in immediate transactions;
                    # OCR/model work must not hold the global editing lock. Legacy
                    # analyze may create expenses and still retains that lock.
                    run_lock = matching_run_lock if path.endswith('/auto') else intake_run_lock
                    if not run_lock.acquire(blocking=False):
                        raise ValueError('材料正在识别或配对，请稍后刷新；本次没有重复执行')
                    try:
                        if path.endswith('/auto'):
                            cloud_result = matching.suggest(body['project_id'])
                        elif path.endswith('/analyze') and not invoice_ai.enabled and not body.get('force_ai'):
                            with write_lock:
                                cloud_result = intake.analyze(body['id'])
                        else:
                            entry = (intake.prepare_batch_entry(body['id'], force_ai=True)
                                     if body.get('force_ai') else intake.prepare_batch_entry(body['id']))
                            cloud_result = ({'entry': {k: v for k, v in entry.items() if k != 'path'},
                                             'converted_to_pdf': bool(entry.get('source_entry_id')),
                                             'message': entry.get('result', '')}
                                            if path.endswith('/prepare') else entry)
                        self.respond(cloud_result)
                    finally:
                        run_lock.release()
                    return
                with write_lock:
                    result: Any = {}
                    if path == '/api/reimbursement-people/create':
                        result = reimbursement_people.create(
                            body.get('project_id', ''), body.get('label', body.get('name', '')),
                            external_sender_id=body.get('external_sender_id', ''), remark=body.get('remark', ''))
                    elif path == '/api/reimbursement-people/rename':
                        result = reimbursement_people.rename(body.get('project_id'), body.get('person_id'),
                                                             body.get('expected_version'), body.get('label'))
                    elif path == '/api/reimbursement-people/merge':
                        result = reimbursement_people.merge(body.get('project_id'), body.get('person_id'),
                                                            body.get('expected_version'), body.get('target_person_id'),
                                                            body.get('target_expected_version'))
                    elif path in {'/api/reimbursement-people/retire', '/api/reimbursement-people/restore'}:
                        result = reimbursement_people.retire(body.get('project_id'), body.get('person_id'),
                                                             body.get('expected_version'), restore=path.endswith('/restore'))
                    elif path == '/api/reimbursement-people/assign-materials':
                        result = reimbursement_people.assign_materials(
                            body.get('project_id', ''), body.get('ids', []), body.get('revisions', {}),
                            body.get('person_id', ''), body.get('target_version'))
                    elif path == '/api/reimbursement-people/assign-expense':
                        result = reimbursement_people.assign_expense(
                            body.get('id', ''), body.get('expected_version', ''), body.get('person_id', ''), body.get('target_version'))
                    elif path == '/api/reimbursement-people/assign-expenses':
                        result = reimbursement_people.assign_expenses(
                            body.get('project_id'), body.get('ids'), body.get('versions'),
                            body.get('person_id', ''), body.get('target_version'))
                    elif path == '/api/material-matching/amount-preview':
                        result = matching.amount_preview(body.get('project_id'))
                    elif path == '/api/material-matching/amount-confirm':
                        result = matching.amount_confirm(body.get('project_id'), body.get('snapshot_token'),
                                                         body.get('idempotency_key'), body.get('amount_only_confirmed'))
                    elif path == '/api/synthesis-expense/edit-fields':
                        result = synthesis_expenses.edit_fields(body.get('id'), body.get('expected_version'),
                                                               body.get('project_id'), body.get('amount'), body.get('title'))
                    elif path in {'/api/synthesis-expense/archive', '/api/synthesis-expense/restore',
                                  '/api/synthesis-expense/delete', '/api/synthesis-expense/restore-deleted'}:
                        result = synthesis_expenses.transition(body.get('id'), body.get('expected_version'),
                                                              body.get('project_id'), path.removeprefix('/api/synthesis-expense/'))
                    elif path == '/api/projects':
                        result = projects.create(body['name'], body.get('site_name', ''),
                                                 body.get('start_date', ''), body.get('end_date', ''))
                        if result['start_date']:
                            mail_agent.reroute_unassigned()
                    elif path == '/api/projects/period':
                        result = projects.set_period(body.get('project_id', ''), body.get('start_date', ''),
                                                     body.get('end_date', ''), body.get('expected_revision'))
                        result['rerouted'] = mail_agent.reroute_unassigned()
                    elif path == '/api/projects/rename':
                        result = projects.rename(body.get('id', ''), body.get('name'), body.get('expected_revision'))
                    elif path == '/api/projects/pin':
                        result = projects.pin(body.get('id', ''), body.get('pinned'), body.get('expected_revision'))
                    elif path == '/api/projects/delete-permanently':
                        result = projects.delete_permanently(body.get('id', ''), body.get('expected_revision'),
                            confirmed=body.get('confirmed'), confirmation_name=body.get('confirmation_name'))
                    elif path == '/api/projects/reimbursement':
                        result = projects.set_reimbursement_status(body['id'], body['status'], body['expected_revision'])
                    elif path == '/api/logistics/projects':
                        result = logistics.create_project(body['name'], body.get('site_name', ''))
                    elif path == '/api/logistics/todos':
                        schedules.validate(body['project_id'], body)
                        result = todos.save(body['project_id'], body)
                    elif path in {'/api/logistics/todos/delete', '/api/logistics/todos/restore'}:
                        result = todos.remove(body['project_id'], body['id'], body['revision'], restore=path.endswith('/restore'))
                    elif path == '/api/projects/move':
                        result = projects.move(body['ids'], body['project_id'], body['versions'])
                    elif path in {'/api/expenses/delete', '/api/expenses/restore'}:
                        result = store.remove(body['id'], body['expected_version'], restore=path.endswith('/restore'))
                    elif path == '/api/intake/receive':
                        result = intake.receive(body['name'], base64.b64decode(body['data'], validate=True),
                                                project_id=body.get('project_id', ''), batch=body.get('batch', ''), reimbursement_person_id=body.get('reimbursement_person_id', ''))
                    elif path == '/api/intake/classify':
                        result = classify_intake(intake, body)
                    elif path in {'/api/intake/archive', '/api/intake/restore', '/api/intake/delete', '/api/intake/restore-deleted'}:
                        result = intake.archive_action(body.get('id', ''), body.get('revision'),
                                                       body.get('project_id'), path.removeprefix('/api/intake/'))
                    elif path == '/api/intake/rail-payments-unused':
                        marked = intake.mark_rail_payments_unused(body.get('ids', []), body.get('revisions', {}))
                        result = {**marked, 'entries': [{key: value for key, value in entry.items() if key != 'path'}
                                                       for entry in marked['entries']]}
                    elif path == '/api/material-matching/create-batch':
                        result = matching.create_batch(body.get('project_id', ''), body.get('groups'),
                                                       body.get('idempotency_key'))
                    elif path == '/api/material-matching/create':
                        result = matching.create_expense(
                            body.get('voucher_keys', []), body.get('payment_ids', []), body['project_id'],
                            support_ids=body.get('support_ids', []), revisions=body.get('revisions', {}),
                            expected_versions=body.get('expected_versions', {}), fields=body.get('fields', {}),
                            suggestion_id=body.get('suggestion_id', ''), suggestion_token=body.get('suggestion_token', ''))
                    elif path == '/api/material-matching/match':
                        selected_vouchers = body.get('voucher_keys', body.get('voucher_key', ''))
                        if not isinstance(selected_vouchers, (str, list)):
                            raise ValueError('请选择发票／收据')
                        result = matching.match(selected_vouchers,
                                                body['payment_ids'], body['project_id'],
                                                expected_version=body.get('expected_version', ''),
                                                expected_versions=body.get('expected_versions', {}),
                                                support_ids=body.get('support_ids', []),
                                                revisions=body.get('revisions', {}), fields=body.get('fields', {}),
                                                provisional=True, reason=body.get('reason', '人工选择匹配，待核对'))
                    elif path == '/api/material-matching/supplement':
                        result = matching.supplement(body['group_id'], body['support_ids'],
                                                     expected_version=body['expected_version'],
                                                     revisions=body.get('revisions', {}))
                    elif path == '/api/material-matching/undo':
                        result = matching.undo(body['group_id'], body['expected_version'])
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
                            source_entry = intake.get(body['id'])
                            if source_entry['status'] in {'archived', 'deleted', 'unused'}:
                                raise ValueError('材料已归档、删除或无需使用，请在归档区处理')
                            url = qr_value(Path(source_entry['path']).read_bytes())
                        result = claim(intake, url, body.get('project_id', ''))
                    elif path == '/api/review/edit-fields':
                        result = store.edit_review_fields(body.get('id'), body.get('expected_version'),
                                                          body.get('project_type'), body.get('project_name'), body.get('amount'))
                    elif path == '/api/review/batch-project-type':
                        result = review.batch_project_type(body.get('project_id'), body.get('ids'), body.get('versions'),
                                                           body.get('project_type'), body.get('project_name'))
                    elif path == '/api/review/return':
                        from invoice_print_layout.review_return import return_to_review
                        result = return_to_review(store, body.get('ids'), body.get('versions'), body.get('project_id'))
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
                    elif path == '/api/expense-print-preview':
                        from invoice_print_layout.expense_print_preview import create_expense_print_preview
                        result = create_expense_print_preview(store, body.get('id'), body.get('expected_version'),
                                                              build_id=STARTUP_BUILD_ID)
                    elif path == '/api/report-preview':
                        from invoice_print_layout.report_print_preview import create_report_print_preview
                        result = create_report_print_preview(store, body['ids'], body.get('options'),
                                                             build_id=STARTUP_BUILD_ID)
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
                    elif path == '/api/review/request':
                        result = store.request_review(body['ids'], body['versions'])
                    elif path == '/api/transition':
                        before = store.get(body['id'])
                        result = store.transition(body['id'], body['action'], expected_version=body.get('expected_version'),
                                                  acknowledge_missing=body.get('acknowledge_missing', False),
                                                  expected_missing=body.get('expected_missing'))
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
                        result = create_report(store, body['ids'], body.get('options'), include_table=True)
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

        def server_close(self) -> None:
            background_intake.close()
            mail_agent.close()
            invoice_agent.close()
            phone_link.stop()
            super().server_close()

        def service_actions(self) -> None:
            invoice_agent.kick()
            if runtime['ready']:
                mail_agent.kick()
            if mode == 'invoice' or not runtime['ready']:
                return
            if time.monotonic() >= self.next_schedule_check:
                self.next_schedule_check = time.monotonic() + 10
                try:
                    with write_lock:
                        schedules.tick()
                except (OSError, sqlite3.Error):
                    # A transient store error leaves durable occurrences for the next tick.
                    pass

    def analyze_phone_material(key: str) -> dict[str, Any]:
        if not intake_run_lock.acquire(blocking=False):
            raise ValueError('材料正在识别，请稍后刷新；本次没有重复执行')
        try:
            return intake.analyze_local(key)
        finally:
            intake_run_lock.release()

    phone_link = PhoneLink(Handler, token, analyze_phone_material, workspace)
    server = WorkbenchServer(('127.0.0.1', port), Handler)
    server.daemon_threads = False  # server_close waits for in-flight writes/exports.
    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('workspace'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--mode', choices=APP_MODES, default='workbench')
    args = parser.parse_args()
    from invoice_print_layout.receipt_intake import configure_ocr_cache
    configure_ocr_cache(args.workspace / '工作台' / 'ocr-cache.sqlite3')
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
    if args.mode == 'workbench':
        ExpenseStore(args.workspace).import_history()
    server = make_server(args.workspace, args.port, mode=args.mode)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        instance.close()


if __name__ == '__main__':
    main()
