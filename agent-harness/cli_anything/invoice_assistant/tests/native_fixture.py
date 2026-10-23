"""Own synthetic native service, launched by the original project's Python."""
from pathlib import Path
import json
import sys
import threading

root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root)); sys.path.insert(0, str(root / 'src'))
from tests.test_material_matching import setup, auto_queued, proof
from invoice_print_layout import workbench_web, expense_classifier
from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI

workspace = Path(sys.argv[1]).resolve(); workspace.mkdir()
matcher, project, other = setup.__wrapped__(workspace)
voucher = auto_queued(matcher, project, 'CLI synthetic voucher', 'invoice', '17')
payment = auto_queued(matcher, project, 'CLI synthetic payment', 'payment', '17')
foreign = auto_queued(matcher, other, 'Foreign synthetic voucher', 'invoice', '23')
pending = matcher.intake.receive('Fresh synthetic.pdf', proof('SYNTHETIC FRESH PDF'), project_id=project)
item = matcher.store.create({'title': 'Synthetic classification', 'category': '其他', 'amount': '5.25',
                             'project_id': project, 'expense_date': '2026-01-02'})
# Keep all new-service tests offline. Real samples use their unchanged service.
workbench_web.GlmInvoiceAI = lambda _root: LocalOnlyInvoiceAI()
expense_classifier.decide = lambda evidence: {'category': None, 'source': 'synthetic-test', 'reason': 'No real model used.'}
server = workbench_web.make_server(workspace, 0, mode='invoice')
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
info = {'url': f'http://127.0.0.1:{server.server_port}', 'workspace_id': __import__('hashlib').sha256(__import__('os').path.normcase(str(workspace)).encode()).hexdigest(),
        'project': project, 'other_project': other, 'voucher': voucher['id'], 'payment': payment['id'],
        'foreign': foreign['id'], 'pending': pending['id'], 'expense': item['id']}
Path(sys.argv[2]).write_text(json.dumps(info), encoding='utf-8')
try: sys.stdin.read()
finally: server.shutdown(); server.server_close(); thread.join(timeout=10)
