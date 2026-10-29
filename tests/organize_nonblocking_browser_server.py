"""Disposable real HTTP desk, synthetic PDFs only, never the production port."""
import json
from pathlib import Path
import tempfile
import sys
import threading

from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from invoice_print_layout.agent_tools import InvoiceTools
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.test_invoice_agent import material, start
from tests.test_organize_nonblocking import native_ride_rows


def no_network(*args, **kwargs):
    raise AssertionError('Synthetic browser fixture cannot call a model')


GlmInvoiceAI._request = no_network
GlmInvoiceAI.public_status = lambda self: {'available': False, 'enabled': False, 'model': '', 'reason': '合成工作区不调用模型'}

with tempfile.TemporaryDirectory(prefix='organize-nonblocking-ui-') as directory:
    root = Path(directory)
    store = ExpenseStore(root)
    registry = ProjectRegistry(root / '后勤/tasks.sqlite3')
    project = registry.create_project('合成整理验收项目')['id']
    ride_project = registry.create_project('合成曹操组合项目')['id']
    jobs = Jobs(store)
    tools = InvoiceTools(jobs)
    env = jobs, tools, project
    rows = [material(tools, project, 'receipt', amount, marker, text=
                     '收据\n商家名称：合成工具商店\n商品名称：' + goods + '\n实付：' + amount)
            for amount, marker, goods in [('17', '未知商品凭证', '未知合成用品'), ('19', '胶带购买凭证', '胶带')]]
    rows += [material(tools, project, 'payment', amount, '合成付款' + amount) for amount in ['17', '19']]
    rows.append(material(tools, project, 'payment', '9', '待人工组合的付款凭证'))
    job = AgentLoop(jobs).run(start(env, rows)['id'], tools)
    assert job['state'] == 'ready' and len(job['item_ids']) == 2 and job['remaining_material_count'] == 1, job
    sources = root / 'native-sources'
    sources.mkdir()
    native_ride_rows((jobs, tools, ride_project), sources)
    server = make_server(root, 0, mode='invoice')
    print(json.dumps({'base':f'http://127.0.0.1:{server.server_port}', 'project':project,
                      'ride_project':ride_project, 'job':job['id']}, ensure_ascii=False), flush=True)
    threading.Thread(target=lambda: (sys.stdin.readline(), server.shutdown()), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
