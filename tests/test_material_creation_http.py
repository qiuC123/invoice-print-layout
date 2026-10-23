from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf
import pytest

from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server


@pytest.mark.parametrize('role', ['invoice', 'receipt', 'payment', 'detail', 'purchase', 'other'])
def test_create_from_one_material_http_required_choice_preview_and_undo(tmp_path: Path, role: str) -> None:
    store = ExpenseStore(tmp_path)
    pid = ExpenseProjects(store).registry.create_project('Synthetic project')['id']
    queue = IntakeQueue(store)
    with pymupdf.open() as document:
        document.new_page().insert_text((60, 100), 'SINGLE-MATERIAL-SOURCE')
        original = document.tobytes()
    entry = queue.receive('original.pdf', original, project_id=pid)
    fields = {'title': 'Machine expense', 'category': '酒店'}
    if role != 'other':
        fields['amount'] = '12.30'
    entry.update(role=role, fields=fields)
    with store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']

    def post(route: str, body: dict[str, Any]) -> Any:
        with urlopen(Request(root + '/api/' + route, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
            return json.load(response)

    try:
        with urlopen(root + '/api/material-matching?project=' + pid) as response:
            board = json.load(response)
        assert [choice['label'] for choice in board['report_types']] == [
            '拓展', '车费', '生活费', '人员', '现场购材料', '其他']
        card = next(card for lane in ('vouchers', 'payments', 'other') for card in board[lane]
                    if card['id'] == entry['id'])
        assert card['report_choice']['project_type'] == 'living'
        assert card['report_choice']['project_name'] == '住宿费(附清单)'
        request = {'voucher_keys': ['q:' + entry['id']] if role in {'invoice', 'receipt'} else [],
                   'payment_ids': [entry['id']] if role == 'payment' else [],
                   'support_ids': [entry['id']] if role not in {'invoice', 'receipt', 'payment'} else [],
                   'project_id': pid, 'revisions': {entry['id']: entry['revision']},
                   'fields': {'project_type': 'living', 'project_name': '住宿费(附清单)', 'amount': '12.30'}}
        for field in ('project_type', 'project_name', 'amount'):
            with pytest.raises(HTTPError) as missing:
                post('material-matching/create', {**request, 'fields': {**request['fields'], field: ''}})
            assert missing.value.code == 400 and not store.list_items()
        with pytest.raises(HTTPError) as invalid:
            post('material-matching/create', {**request, 'fields': {
                'project_type': 'living', 'project_name': '火车、飞机', 'amount': '12.30'}})
        assert invalid.value.code == 400 and not store.list_items()
        with pytest.raises(HTTPError) as stale:
            post('material-matching/create', {**request, 'revisions': {entry['id']: -1}})
        assert stale.value.code == 400 and not store.list_items()
        result = post('material-matching/create', request)
        item = result['item']
        assert item['title'] == '住宿费(附清单)' and item['category'] == '酒店'
        assert item['project_type'] == 'living' and item['project_name'] == '住宿费(附清单)'
        assert item['amount_cents'] == 1230 and not item.get('amount_pending', False)
        assert item['stage'] == 'draft' and not item['verified'] and not item['ready']
        assert len(item['attachments']) == 1 and not item['complete']
        assert result['match']['voucher_count'] == (1 if role in {'invoice', 'receipt'} else 0)
        assert result['match']['payment_count'] == (1 if role == 'payment' else 0)
        preview = post('expense-print-preview', {'id': item['id'], 'expected_version': item['version']})
        assert preview['partial'] and preview['preview_pages']
        with pymupdf.open(store.root / '导出' / preview['pdf']) as document:
            assert all(abs(page.rect.width - 595.276) < 1 and abs(page.rect.height - 841.89) < 1 for page in document)
            assert ''.join(page.get_text() for page in document).count('SINGLE-MATERIAL-SOURCE') == 1
        with pytest.raises(HTTPError) as duplicate:
            post('material-matching/create', request)
        assert duplicate.value.code == 400 and len(store.list_items()) == 1
        post('material-matching/undo', {'group_id': result['match']['group_id'],
                                      'expected_version': item['version']})
        released = queue.get(entry['id'])
        assert not released.get('item_id') and released['status'] == 'review'
        assert Path(released['path']).read_bytes() == original
        assert not [item for item in store.list_items() if item['stage'] == 'draft']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_custom_expense_http_keeps_unequal_proofs_and_manual_amount(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    pid = ExpenseProjects(store).registry.create_project('Synthetic mismatch')['id']
    queue = IntakeQueue(store)
    entries = []
    for role, amount in [('invoice', '18.00'), ('payment', '20.00')]:
        with pymupdf.open() as document:
            document.new_page().insert_text((60, 100), role.upper() + '-PROOF')
            original = document.tobytes()
        entry = queue.receive(role + '.pdf', original, project_id=pid)
        entry.update(role=role, fields={'amount': amount, 'category': '其他'})
        with store.connect() as db:
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
        entries.append(entry)
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        token = json.load(response)['token']
    try:
        request = {'voucher_keys': ['q:' + entries[0]['id']], 'payment_ids': [entries[1]['id']],
                   'support_ids': [], 'project_id': pid,
                   'revisions': {entry['id']: entry['revision'] for entry in entries},
                   'fields': {'project_type': 'other', 'amount': '17.50', 'project_name': '停车费'}}
        with urlopen(Request(root + '/api/material-matching/create', data=json.dumps(request).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
            result = json.load(response)
        item = result['item']
        assert item['title'] == '停车费' and item['category'] == '其他' and item['amount_cents'] == 1750
        assert item['project_type'] == 'other' and item['project_name'] == '停车费'
        assert not item['verified'] and not item['ready'] and len(item['attachments']) == 2
        assert item['material_match']['payment_total_cents'] == 2000
        assert queue.get(entries[0]['id'])['fields']['amount'] == '18.00'
        assert queue.get(entries[1]['id'])['fields']['amount'] == '20.00'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
