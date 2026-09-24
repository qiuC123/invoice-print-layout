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
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, unquote, urlparse

from invoice_print_layout.workbench import ExpenseStore, CATEGORIES, ROLES, STAGES
from invoice_print_layout.storage import ensure_workspace
from invoice_print_layout.reliability import read_json, save_json
from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.wechat_inbox import WeChatInbox


def inbox_files(store: ExpenseStore) -> dict[str, Path]:
    folder = store.root / '邮件收件'
    folder.mkdir(exist_ok=True)
    with store.connect() as db:
        used = {r['digest'] for r in db.execute('SELECT digest FROM attachments')}
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
    return {'message': f'补收完成：新增{result.downloaded}份PDF，重复{result.duplicates}份，无效{result.invalid_pdf}份。请在邮件收件箱关联事项。'}


def make_server(workspace: Path, port: int = 8765) -> ThreadingHTTPServer:
    store = ExpenseStore(workspace)
    logistics = LogisticsStore(workspace / '后勤' / 'tasks.sqlite3')
    wechat_inbox = WeChatInbox(logistics)
    token = secrets.token_urlsafe(32)
    write_lock = threading.Lock()
    assets = Path(__file__).parent / 'web'

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
                elif path in {'/app.js', '/receipt.js', '/report.js', '/mail-search.js', '/expense-classifier.js', '/logistics.js', '/style.css', '/logistics.css'}:
                    self.send((assets / path[1:]).read_bytes(), 'text/javascript' if path.endswith('.js') else 'text/css')
                elif path == '/api/logistics':
                    self.respond({**logistics.snapshot(), 'token': token})
                elif path == '/api/logistics/inbox':
                    project_id = parse_qs(urlparse(self.path).query).get('project_id', [''])[0]
                    self.respond(wechat_inbox.snapshot(project_id))
                elif path == '/api/state':
                    from invoice_print_layout.report_excel import TEMPLATE_NAME
                    from invoice_print_layout.storage import read_person_name
                    self.respond({'items': store.list_items(), 'categories': CATEGORIES, 'roles': ROLES,
                                  'stages': STAGES, 'token': token, 'mail_search_ready': True, 'category_suggestion_ready': True,
                                  'report_template_ready': (store.root / TEMPLATE_NAME).is_file(),
                                  'report_person': read_person_name(ensure_workspace(store.workspace).config) or '',
                                  'history_import': read_json(store.root / 'history-import.json'),
                                  'inbox': [{'id': k, 'name': p.name} for k, p in inbox_files(store).items()]})
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
                    if '/' in key or '\\' in key or Path(key).suffix not in {'.pdf', '.md', '.xlsx'}:
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
                with write_lock:
                    path = urlparse(self.path).path
                    result: Any = {}
                    if path == '/api/logistics/projects':
                        result = logistics.create_project(body['name'], body.get('site_name', ''))
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
                        result = store.update(body['id'], body)
                    elif path == '/api/transition':
                        result = store.transition(body['id'], body['action'])
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
                    elif path == '/api/category/suggest':
                        from invoice_print_layout.expense_classifier import suggest
                        result = suggest(store, body['id'])
                    elif path == '/api/mail/search':
                        from invoice_print_layout.mail_search import search_invoices
                        result = search_invoices(store, body['id'], body['start'], body['end'])
                    elif path == '/api/mail/associate':
                        from invoice_print_layout.mail_search import associate
                        result = associate(store, body['id'], body['search_id'], body['candidate_id'], body.get('role', 'invoice'))
                    elif path == '/api/mail':
                        result = sync_mail(store)
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

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('workspace'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.workspace, args.port)
    ExpenseStore(args.workspace).import_history()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
