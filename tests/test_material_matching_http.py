from __future__ import annotations

import base64
import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import pymupdf

from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server
from tests.helpers import make_invoice_pdf, make_trip_pdf


def test_material_board_http_preserves_originals_and_reverses_association(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    pid = ExpenseProjects(store).registry.create_project('Synthetic matching')['id']
    item = store.create({'title': 'One existing expense', 'project_id': pid,
                         'category': '餐饮', 'amount': '15', 'material_basis': 'invoice_payment'})
    invoice_bytes = make_invoice_pdf(tmp_path / 'invoice.pdf', amount='15.00').read_bytes()
    item = store.add_attachment(item['id'], 'invoice.pdf', invoice_bytes, 'invoice')
    payment_bytes = make_trip_pdf(tmp_path / 'payment.pdf', amount='15.00').read_bytes()
    server = make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'

    def get(route: str) -> Any:
        with urlopen(root + route) as response:
            return json.load(response)

    token = get('/api/state')['token']

    def post(route: str, body: dict[str, Any]) -> Any:
        with urlopen(Request(root + '/api/' + route, data=json.dumps(body).encode(),
                            headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})) as response:
            return json.load(response)

    try:
        received = post('intake/receive', {'name': 'payment.pdf', 'project_id': pid,
                                         'data': base64.b64encode(payment_bytes).decode()})
        classified = post('intake/classify', {'id': received['id'], 'revision': 0, 'role': 'payment',
                                             'fields': {'amount': '15.00', 'merchant': 'Synthetic payee'}})
        assert 'path' not in classified
        with pytest.raises(HTTPError) as stale:
            post('intake/classify', {'id': received['id'], 'revision': 0, 'role': 'receipt', 'fields': {}})
        assert stale.value.code == 400
        board = get('/api/material-matching?project=' + pid)
        assert any(card['key'] == 'e:' + item['id'] for card in board['vouchers'])
        assert any(card['id'] == received['id'] for card in board['payments'])
        assert '"path"' not in json.dumps(board)
        partial = post('expense-print-preview', {'id': item['id'], 'expected_version': item['version']})
        assert partial['partial'] and not partial['complete'] and partial['preview_pages']
        with urlopen(root + '/attachment-page/' + item['attachments'][0]['id']) as response:
            assert response.headers['Content-Type'] == 'image/png'
            assert response.read().startswith(b'\x89PNG')
        result = post('material-matching/match', {
            'voucher_key': 'e:' + item['id'], 'payment_ids': [received['id']], 'project_id': pid,
            'revisions': {received['id']: classified['revision']}, 'expected_version': item['version'],
            'reason': 'Synthetic manual match'})
        matched = result['item']
        assert matched['complete'] and matched['pending_review'] and not matched['verified']
        assert matched['amount_cents'] == item['amount_cents'] and len(store.list_items()) == 1
        group = matched['material_match']['group_id']
        restored = post('material-matching/undo', {'group_id': group, 'expected_version': matched['version']})['item']
        assert restored['missing'] == ['付款截图'] and not restored['pending_review']
        assert restored['amount_cents'] == 1500 and len(restored['attachments']) == 1
        assert IntakeQueue(store).get(received['id'])['status'] == 'review'
        assert Path(IntakeQueue(store).get(received['id'])['path']).read_bytes() == payment_bytes
        assert store.attachment_path(restored['attachments'][0]['id'])[0].read_bytes() == invoice_bytes
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('voucher_count', [1, 2])
def test_support_material_http_match_append_preview_and_undo(tmp_path: Path, voucher_count: int) -> None:
    store = ExpenseStore(tmp_path)
    pid = ExpenseProjects(store).registry.create_project('Synthetic support group')['id']
    queue = IntakeQueue(store)
    vouchers = []
    for index in range(voucher_count):
        entry = queue.receive(f'voucher-{index}.pdf', make_invoice_pdf(
            tmp_path / f'original-{index}.pdf', amount='10.00', invoice_number=f'{index + 7:020d}').read_bytes(),
            project_id=pid)
        from invoice_print_layout.workbench_web import classify_intake
        vouchers.append(classify_intake(queue, {'id': entry['id'], 'revision': entry['revision'],
                        'role': 'invoice', 'fields': {'amount': '10.00', 'title': entry['name'], 'category': '餐饮'}}))
    payment = queue.receive('payment.pdf', make_trip_pdf(tmp_path / 'original-payment.pdf',
                            amount=f'{voucher_count * 10:.2f}').read_bytes(), project_id=pid)
    payment = classify_intake(queue, {'id': payment['id'], 'revision': payment['revision'],
                            'role': 'payment', 'fields': {'amount': f'{voucher_count * 10:.2f}'}})

    def support_document(marker: str) -> bytes:
        with pymupdf.open() as document:
            document.new_page().insert_text((60, 720), marker)
            return document.tobytes()

    support_data = support_document('SUPPORT-UNKNOWN-AMOUNT')
    extra_data = support_document('SUPPORT-APPENDED-DETAIL')
    support = queue.receive('unknown-amount.pdf', support_data, project_id=pid)
    extra = queue.receive('detail.pdf', extra_data, project_id=pid)
    # Ordinary supporting material does not need monetary identification to be attached.
    extra.update(role='detail', fields={'amount': '999.00'})
    with store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(extra), extra['id']))
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
        request = {'voucher_keys': ['q:' + v['id'] for v in vouchers], 'payment_ids': [payment['id']],
                   'support_ids': [support['id']], 'project_id': pid,
                   'revisions': {e['id']: e['revision'] for e in [*vouchers, payment, support]}}
        with pytest.raises(HTTPError) as invalid:
            post('material-matching/match', {**request, 'support_ids': 'invalid-array'})
        assert invalid.value.code == 400 and not store.list_items()
        matched = post('material-matching/match', request)
        item = matched['item']
        assert matched['match']['support_count'] == 1 and matched['match']['can_supplement']
        assert item['amount_cents'] == voucher_count * 1000 and item['pending_review'] and not item['verified']
        assert len(item['material_match']['support_attachment_ids']) == 1
        supplement_request = {'group_id': matched['match']['group_id'], 'support_ids': [extra['id']],
                              'expected_version': item['version'], 'revisions': {extra['id']: extra['revision']}}
        with pytest.raises(HTTPError) as stale:
            post('material-matching/supplement', {**supplement_request, 'expected_version': 'stale'})
        assert stale.value.code == 400 and queue.get(extra['id'])['status'] == 'received'
        supplemented = post('material-matching/supplement', supplement_request)
        updated = supplemented['item']
        assert updated['amount_cents'] == item['amount_cents']
        assert updated['category'] == item['category'] and not updated['verified'] and updated['stage'] == 'draft'
        assert supplemented['match']['support_count'] == 2
        assert len(updated['material_match']['support_attachment_ids']) == 2
        assert len([v for v in store.list_items() if v['stage'] != 'cancelled']) == 1
        preview = post('expense-print-preview', {'id': updated['id'], 'expected_version': updated['version']})
        assert preview['complete'] and preview['preview_pages']
        with urlopen(root + '/export/' + preview['pdf']) as response:
            with pymupdf.open(stream=response.read(), filetype='pdf') as document:
                text = ''.join(page.get_text() for page in document)
                assert text.count('SUPPORT-UNKNOWN-AMOUNT') == text.count('SUPPORT-APPENDED-DETAIL') == 1
        with urlopen(root + '/api/material-matching?project=' + pid) as response:
            board = json.load(response)
        assert not board['other'] and board['matches'][0]['support_count'] == 2
        with pytest.raises(HTTPError):
            post('material-matching/supplement', supplement_request)
        post('material-matching/undo', {'group_id': matched['match']['group_id'], 'expected_version': updated['version']})
        for original, data in ((support, support_data), (extra, extra_data)):
            released = queue.get(original['id'])
            assert released['status'] == 'review' and not released.get('item_id')
            assert Path(released['path']).read_bytes() == data
        with urlopen(root + '/api/material-matching?project=' + pid) as response:
            restored_board = json.load(response)
        assert len(restored_board['other']) == 2 and not restored_board['matches']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_return_missing_review_to_materials_checks_version_and_keeps_proofs(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Incomplete review', 'category': '餐饮', 'amount': '15',
                         'material_basis': 'invoice_payment'})
    item = store.add_attachment(item['id'], 'invoice.pdf',
                                make_invoice_pdf(tmp_path / 'invoice.pdf', amount='15').read_bytes(), 'invoice')
    store.request_review([item['id']], {item['id']: item['version']})
    current = store.get(item['id'])
    with pytest.raises(ValueError, match='变化'):
        store.transition(item['id'], 'return_materials', expected_version=item['version'])
    after = store.transition(item['id'], 'return_materials', expected_version=current['version'])
    assert after['missing'] == current['missing'] and after['attachments'] == current['attachments']
    assert not after['pending_review'] and not after['verified'] and after['stage'] == 'draft'
    assert after['amount_cents'] == current['amount_cents']


def test_classification_cannot_convert_unclaimed_qr_or_linked_material(tmp_path: Path) -> None:
    from invoice_print_layout.workbench_web import classify_intake
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store)
    entry = queue.receive('qr.pdf', make_invoice_pdf(tmp_path / 'qr.pdf').read_bytes())
    entry['qr_status'] = 'manual'
    with store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(entry), entry['id']))
    with pytest.raises(ValueError, match='正式发票'):
        classify_intake(queue, {'id': entry['id'], 'revision': 0, 'role': 'invoice', 'fields': {}})
    assert queue.get(entry['id']) == entry
    with pytest.raises(ValueError):
        classify_intake(queue, {'id': entry['id'], 'revision': 0, 'role': 'other', 'fields': {'amount': '-15'}})


@pytest.mark.parametrize('existing_expenses', [False, True])
def test_many_vouchers_one_payment_http_counts_once_and_restores(tmp_path: Path, existing_expenses: bool) -> None:
    store = ExpenseStore(tmp_path)
    pid = ExpenseProjects(store).registry.create_project('Synthetic many-to-many')['id']
    originals = [make_invoice_pdf(tmp_path / f'bill-{index}.pdf', amount=value,
                                  invoice_number=f'{index + 1:020d}').read_bytes()
                 for index, value in enumerate(('6.00', '14.00'))]
    original_items = []
    if existing_expenses:
        for index, value in enumerate(('6.00', '14.00')):
            item = store.create({'title': f'Synthetic bill {index}', 'project_id': pid,
                                 'amount': value, 'category': '餐饮', 'material_basis': 'invoice_payment'})
            original_items.append(store.add_attachment(item['id'], f'bill-{index}.pdf', originals[index], 'invoice'))
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

    def receive(name: str, data: bytes, role: str, value: str) -> dict[str, Any]:
        received = post('intake/receive', {'name': name, 'data': base64.b64encode(data).decode(), 'project_id': pid})
        return post('intake/classify', {'id': received['id'], 'revision': received['revision'], 'role': role,
                                       'fields': {'amount': value, 'title': name, 'category': '餐饮'}})

    try:
        queue_vouchers = [] if existing_expenses else [
            receive(f'bill-{index}.pdf', data, 'invoice', value)
            for index, (data, value) in enumerate(zip(originals, ('6.00', '14.00')))]
        payment_data = make_trip_pdf(tmp_path / 'single-payment.pdf', amount='20.00').read_bytes()
        payment = receive('single-payment.pdf', payment_data, 'payment', '20.00')
        versions = {item['id']: item['version'] for item in original_items}
        request = {'voucher_keys': ['e:' + item['id'] for item in original_items] or
                   ['q:' + entry['id'] for entry in queue_vouchers],
                   'expected_versions': versions, 'payment_ids': [payment['id']], 'project_id': pid,
                   'revisions': {entry['id']: entry['revision'] for entry in [*queue_vouchers, payment]}}
        with pytest.raises(HTTPError) as invalid_selection:
            post('material-matching/match', {**request, 'voucher_keys': None})
        assert invalid_selection.value.code == 400
        if existing_expenses:
            with pytest.raises(HTTPError):
                post('material-matching/match', {**request, 'expected_versions': {}})
            assert all(store.get(item['id'])['stage'] == 'draft' for item in original_items)
        matched = post('material-matching/match', request)
        item = matched['item']
        assert item['amount_cents'] == 2000 and item['pending_review'] and not item['verified']
        assert matched['match']['voucher_count'] == 2 and matched['match']['payment_count'] == 1
        assert sum(a['role'] == 'payment' for a in item['attachments']) == 1
        assert sum(a['role'] == 'invoice' for a in item['attachments']) == 2
        active = [entry for entry in store.list_items() if entry['stage'] != 'cancelled']
        assert len(active) == 1 and sum(entry['amount_cents'] for entry in active) == 2000
        for original in original_items:
            assert store.get(original['id'])['stage'] == 'cancelled'
        preview = post('expense-print-preview', {'id': item['id'], 'expected_version': item['version']})
        assert preview['complete'] and preview['preview_pages']
        with pytest.raises(HTTPError):
            post('material-matching/match', request)
        post('material-matching/undo', {'group_id': matched['match']['group_id'], 'expected_version': item['version']})
        assert store.get(item['id'])['stage'] == 'cancelled'
        for original in original_items:
            restored = store.get(original['id'])
            assert restored['stage'] == 'draft' and restored['amount_cents'] == original['amount_cents']
            assert restored['attachments'] == original['attachments']
        for entry in [*queue_vouchers, payment]:
            restored_queue = IntakeQueue(store).get(entry['id'])
            assert restored_queue['status'] == 'review' and not restored_queue.get('item_id')
        with urlopen(root + '/api/material-matching?project=' + pid) as response:
            board = json.load(response)
        assert len(board['vouchers']) == 2 and len(board['payments']) == 1 and not board['matches']
        assert Path(IntakeQueue(store).get(payment['id'])['path']).read_bytes() == payment_data
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
