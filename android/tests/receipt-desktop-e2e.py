"""F22: wrap the supplied disposable desktop launcher; never use the real ledger."""
from __future__ import annotations

import argparse
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--evidence', type=Path, required=True)
parser.add_argument('--host', required=True)
parser.add_argument('--fixed-lan-fixture', action='store_true', help='Controlled continuation only; bypass slow OS address enumeration')
args = parser.parse_args()
out = args.evidence.resolve()
evidence_root = Path('E:/devlop/invoice-print-layout/workspace/handoff/receipt-intake-20261101/e2e').resolve()
assert out == evidence_root or out.is_relative_to(evidence_root)
out.mkdir(parents=True, exist_ok=True)
launcher_path = evidence_root.parent / 'device/start-synthetic-device.py'
spec = importlib.util.spec_from_file_location('synthetic_launcher', launcher_path)
assert spec and spec.loader
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.reimbursement_people import ReimbursementPeople
from invoice_print_layout.workbench import ExpenseStore
if args.fixed_lan_fixture:
    from invoice_print_layout import phone_link
    phone_link.addresses = lambda: [{'address': args.host, 'label': 'Measured LAN - test fixture'}]

stop = threading.Event()
context: dict = {}
desktop = None
devices = None
workspace = None
request_timings: list[dict] = []
timing_lock = threading.Lock()


def snapshot() -> dict:
    assert devices is not None
    with devices.store.connect() as db:
        uploads = [dict(row) for row in db.execute('SELECT * FROM receipt_device_uploads')]
        hashes = [dict(row) for row in db.execute('SELECT id,key_hash,revoked_at FROM receipt_devices')]
    return {'uploads': uploads, 'entries': [devices.intake.get(row['entry_id']) for row in uploads],
            'devices': devices.status()['devices'], 'key_hashes': hashes,
            'catalog': devices.catalog(), 'listener': devices.server is not None}


def pc(path: str, body=None):
    assert desktop is not None
    token = launcher.request(desktop.server_port, '/api/state')['token']
    return launcher.request(desktop.server_port, path, body, **{'X-Workbench-Token': token})


def screenshot(name: str):
    completed = subprocess.run(['node', str(Path(__file__).with_name('receipt-desktop-evidence.cjs')),
        context['desktop_url'], context['primary']['id'], str(out), name],
        capture_output=True, text=True, encoding='utf-8', timeout=70)
    (out / (name + '.browser.log')).write_text(completed.stdout + completed.stderr, encoding='utf-8')
    return {'exit_code': completed.returncode, 'log': name + '.browser.log'}


class Control(BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def do_GET(self):
        self.handle_control()

    def do_POST(self):
        self.handle_control()

    def handle_control(self):
        global devices
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            route = self.path
            result = {}
            assert devices is not None
            if route == '/context':
                pairing = pc('/api/phone-link')['receipt_app']
                if not pairing.get('code'):
                    pairing = pc('/api/phone-link/devices/pair', {})
                result = {**context, 'code': pairing['code'], 'pair_uri': pairing['pair_uri'],
                          'expected_catalog': devices.catalog()}
            elif route == '/pause':
                # Close only the test listener, preserving the actual service, DB and device key.
                with devices.lifecycle_lock:
                    server, worker = devices.server, devices.worker
                    devices.server = devices.worker = None
                    devices.listener_dirty = False
                    devices.pairing_expires_at = 0
                    if server:
                        server.shutdown()
                        server.server_close()
                    if worker:
                        worker.join()
                result = snapshot()
            elif route == '/resume':
                devices.sync_listener()
                result = snapshot()
            elif route == '/reimburse':
                project = next(p for p in devices.projects.list() if p['id'] == context['primary']['id'])
                result = pc('/api/projects/reimbursement', {'id': project['id'], 'status': 'reimbursed',
                            'expected_revision': project['reimbursement_revision']})
            elif route == '/revoke':
                active = [d for d in devices.status()['devices'] if not d['revoked_at']]
                assert len(active) == 1, active
                result = pc('/api/phone-link/devices/revoke', {'device_id': active[0]['id']})
            elif route == '/new-pairing':
                pairing = pc('/api/phone-link/devices/pair', {})
                result = {'code': pairing['code'], 'pair_uri': pairing['pair_uri']}
            elif route == '/snapshot':
                result = snapshot()
            elif route == '/checkpoint':
                name = body['name']
                assert name.replace('-', '').isalnum()
                result = snapshot()
                (out / (name + '.json')).write_text(json.dumps({'app': body, 'desktop': result},
                    ensure_ascii=False, indent=2), encoding='utf-8')
                if body.get('desktop_screenshot'):
                    result['screenshot'] = screenshot(name)
            elif route == '/stop':
                stop.set()
            else:
                raise ValueError('unknown test control')
            payload = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
        except Exception as exc:
            payload = json.dumps({'error': repr(exc)}).encode()
            self.send_response(500)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


real_make_server = launcher.make_server


def make_server(directory, port, **kwargs):
    global desktop, devices, workspace
    workspace = Path(directory)
    assert workspace.parent == out and workspace.name.startswith('synthetic-device-')
    store = ExpenseStore(workspace)
    projects, people = ExpenseProjects(store), ReimbursementPeople(store)
    primary = projects.list()[0]
    projects.set_period(primary['id'], '2026-11-01', '2026-11-01', primary['project_revision'])
    helper = people.create(primary['id'], 'Synthetic confirmed person')['person']
    retired = people.create(primary['id'], 'Synthetic retired person')['person']
    people.retire(primary['id'], retired['id'], retired['version'])
    other = projects.create('Synthetic alternate project', start_date='2026-11-02', end_date='2026-11-02')
    other_person = people.create(other['id'], 'Synthetic alternate main')['person']
    people.set_main_person(other['id'], other_person['id'], other['project_revision'], other_person['version'])
    desktop = real_make_server(directory, port, **kwargs)
    method = desktop.service_actions.__func__
    devices = next(cell.cell_contents for name, cell in zip(method.__code__.co_freevars, method.__closure__)
                   if name == 'receipt_devices')
    real_handler = devices._handler()

    class TimedHandler(real_handler):
        def send_response(self, code, message=None):
            self.response_status = code
            super().send_response(code, message)

        def handle_one_request(self):
            started = time.perf_counter()
            try:
                super().handle_one_request()
            finally:
                with timing_lock:
                    request_timings.append({'path': getattr(self, 'path', ''),
                        'status': getattr(self, 'response_status', None), 'seconds': time.perf_counter() - started})
                    (out / 'device-request-timings.json').write_text(
                        json.dumps(request_timings, indent=2), encoding='utf-8')

    devices._handler = lambda: TimedHandler
    context.update(desktop_url=f'http://127.0.0.1:{desktop.server_port}',
        device_url=f'http://{args.host}:8769', primary=primary, helper=helper, alternate=other,
        alternate_person=other_person, workspace=str(workspace), fixed_lan_fixture=args.fixed_lan_fixture)
    return desktop


launcher.make_server = make_server
# The supplied launcher owns its normal startup/shutdown and TemporaryDirectory cleanup.
launcher.tempfile = SimpleNamespace(TemporaryDirectory=lambda **kw:
    tempfile.TemporaryDirectory(prefix=kw['prefix'], dir=out))
launcher.threading = SimpleNamespace(Thread=threading.Thread, Event=lambda: stop)
control = ThreadingHTTPServer(('0.0.0.0', 0), Control)
control_thread = threading.Thread(target=control.serve_forever, daemon=True)
control_thread.start()
(out / 'control.json').write_text(json.dumps({'url': f'http://{args.host}:{control.server_port}'}), encoding='utf-8')
sys.argv = [str(launcher_path), '--device-port', '8769']
try:
    launcher.main()
finally:
    control.shutdown()
    control.server_close()
    control_thread.join()
    (out / 'cleanup.json').write_text(json.dumps({'workspace': str(workspace),
        'workspace_deleted': workspace is not None and not workspace.exists(),
        'device_listener_closed': devices is not None and devices.server is None}, indent=2), encoding='utf-8')
