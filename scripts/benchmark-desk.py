"""F18 synthetic-only HTTP/Edge fixture; never opens the real workspace."""
import argparse
import cProfile
import hashlib
import json
import shutil
import pstats
import subprocess
import tempfile
from pathlib import Path

from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout import workbench_web
from tests.test_material_matching import proof, queued

parser = argparse.ArgumentParser()
parser.add_argument('--baseline-assets', action='store_true')
parser.add_argument('--profile')
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix='f18-synthetic-') as directory:
    workspace = Path(directory)
    store = ExpenseStore(workspace)
    project = ProjectRegistry(workspace / '后勤/tasks.sqlite3').create_project('F18 Synthetic')['id']
    matcher = MaterialMatching(store, IntakeQueue(store))
    template = store.create({'title': 'Synthetic', 'category': '酒店', 'project_id': project, 'amount': '10'})
    with store.connect() as db:
        original = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (template['id'],)).fetchone()[0])
        db.execute('DELETE FROM events')
        db.execute('DELETE FROM expenses')
        for i in range(150):
            item = {**original, 'id': f'expense{i:03}', 'title': f'Synthetic expense {i:03}'}
            db.execute('INSERT INTO expenses VALUES (?,?,?)', (item['id'], None, json.dumps(item)))
            for role in ('invoice', 'payment'):
                content = proof(f'{i}-{role}')
                path = store.files / f'{i}-{role}.pdf'
                path.write_bytes(content)
                db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                           (f'{i}-{role}', item['id'], path.name, role, str(path), hashlib.sha256(content).hexdigest()))
            for n in range(3):
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item['id'], item['created_at'], f'Synthetic event {n}'))
    for i in range(40):
        entry = queued(matcher, project, f'Pending {i:02}', 'invoice' if i < 16 else 'payment' if i < 32 else 'other',
                       str(100 + i) if i < 32 else None, batch='synthetic-batch')
        entry['fields']['merchant'] = f'Synthetic merchant {i}'
        with store.connect() as db:
            matcher._write_queue(db, entry)
    if args.baseline_assets:
        assets = workspace / 'assets/web'
        shutil.copytree(Path(workbench_web.__file__).parent / 'web', assets)
        for name in ('matching-desk.js', 'intake.js', 'reimbursement-people.js'):
            content = subprocess.check_output(['git', 'show', f'ce7c60c:src/invoice_print_layout/web/{name}'])
            (assets / name).write_bytes(content)
        workbench_web.__file__ = str(assets.parent / 'workbench_web.py')
    server = workbench_web.make_server(workspace, 0, mode='invoice')
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(matcher.board, project)
        with open(args.profile, 'w', encoding='utf-8') as output:
            pstats.Stats(profiler, stream=output).sort_stats('cumtime').print_stats(50)
    print(json.dumps({'base': f'http://127.0.0.1:{server.server_port}', 'project': project,
                      'workspace': str(workspace), 'expenses': 150, 'attachments': 300, 'pending': 40}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
