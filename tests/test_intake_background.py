from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from invoice_print_layout import workbench_web
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.intake_background import BackgroundIntake
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


def saved(queue: IntakeQueue, tmp_path: Path, index: int, project: str = '') -> dict[str, Any]:
    return queue.receive(f'proof-{index}.pdf', make_invoice_pdf(tmp_path / f'{index}.pdf',
                         invoice_number=str(10000000000000000000 + index)).read_bytes(), project_id=project, batch='upload')


def finished(service: BackgroundIntake) -> dict[str, Any]:
    until = time.monotonic() + 5
    while time.monotonic() < until:
        status = service.progress()
        if status['batches'] and all(job['state'] == 'finished' for job in status['batches']):
            return status
        time.sleep(.005)
    pytest.fail('Background recognition did not finish')


def test_serial_idempotent_queue_counts_failures_and_scans_last(tmp_path: Path, monkeypatch: Any) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    project = ExpenseProjects(queue.store).registry.create_project('Synthetic Queue')['id']
    entries = [saved(queue, tmp_path, index, project) for index in range(3)]
    entered, release = threading.Event(), threading.Event()
    calls, scans = [], []

    def prepare(key: str, *, force_ai: bool = False) -> dict[str, Any]:
        assert force_ai is True
        calls.append(key)
        entered.set()
        assert release.wait(3)
        if key == entries[1]['id']:
            raise RuntimeError('Synthetic recognition failure with private diagnostics')
        return queue.get(key)

    monkeypatch.setattr(queue, 'prepare_batch_entry', prepare)
    service = BackgroundIntake(queue, threading.Lock(), lambda key: scans.append((key, list(calls))) or {'suggested': 2})
    ids = [entry['id'] for entry in entries]
    try:
        service.enqueue('upload', ids, force_ai=True)
        assert entered.wait(2)
        with ThreadPoolExecutor(4) as pool:
            repeated = list(pool.map(lambda _: service.enqueue('upload', ids, force_ai=True), range(8)))
        assert all(job['total'] == 3 for job in repeated)
        assert calls == ids[:1] and scans == []
        with pytest.raises(ValueError, match='已在识别队列'):
            service.enqueue('another-batch', ids, force_ai=True)
        release.set()
        result = finished(service)['batches'][0]
        assert (result['completed'], result['failed'], result['running'], result['suggested']) == (2, 1, 0, 2)
        assert calls == ids and scans == [(project, ids)]
        assert 'private diagnostics' not in json.dumps(result)
        assert result['failures'][0]['id'] == ids[1]
        result['ids'].clear()
        assert service.progress()['batches'][0]['ids'] == ids
    finally:
        release.set()
        service.close()


def test_queue_waits_for_manual_lock_and_restart_only_exposes_received_rows(tmp_path: Path, monkeypatch: Any) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    entry = saved(queue, tmp_path, 1)
    lock, entered = threading.Lock(), threading.Event()
    monkeypatch.setattr(queue, 'prepare_batch_entry', lambda key, **kw: entered.set() or queue.get(key))
    service = BackgroundIntake(queue, lock, lambda _: {})
    try:
        lock.acquire()
        service.enqueue('batch', [entry['id']])
        assert not entered.wait(.05)
        lock.release()
        assert entered.wait(2)
        finished(service)
    finally:
        service.close()
    new_service = BackgroundIntake(queue, lock, lambda _: {})
    try:
        assert new_service._thread is None
        assert new_service.progress()['pending'] == [{'id': entry['id'], 'batch': 'upload'}]
        assert new_service.progress('foreign')['pending_count'] == 0
        assert not queue.store.list_items()
    finally:
        new_service.close()


@pytest.mark.parametrize('batch,ids,flag', [('', ['x'], False), ('batch', [], False),
                                         ('batch', ['x', 'x'], False), ('batch', ['x'], 'true')])
def test_invalid_queue_request_never_starts(batch: Any, ids: Any, flag: Any, tmp_path: Path) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    service = BackgroundIntake(queue, threading.Lock(), lambda _: {})
    try:
        with pytest.raises(ValueError):
            service.enqueue(batch, ids, force_ai=flag)
        assert service._thread is None
    finally:
        service.close()


@pytest.mark.parametrize('path', ['intake/prepare', 'intake/enqueue', 'phone'])
def test_no_ai_recognition_allows_edits_rejects_duplicate_retry_and_preserves_revision(
        tmp_path: Path, monkeypatch: Any, path: str) -> None:
    monkeypatch.setattr(workbench_web, 'GlmInvoiceAI', lambda _root: LocalOnlyInvoiceAI())
    store = ExpenseStore(tmp_path)
    queue = IntakeQueue(store, ai=LocalOnlyInvoiceAI())
    project = ExpenseProjects(store).registry.create_project('Synthetic HTTP Queue')['id']
    entry = saved(queue, tmp_path, 0, project)
    entered, release = threading.Event(), threading.Event()
    original = IntakeQueue._finish_batch_analysis

    def finish(self: IntakeQueue, *args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(IntakeQueue, '_finish_batch_analysis', finish)
    phone_analyze = []
    phone_class = workbench_web.PhoneLink

    def phone_link(handler: Any, token: str, analyze: Any, workspace: Path) -> Any:
        phone_analyze.append(analyze)
        return phone_class(handler, token, analyze, workspace)

    monkeypatch.setattr(workbench_web, 'PhoneLink', phone_link)
    server = workbench_web.make_server(tmp_path, 0, mode='invoice')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    with urlopen(root + '/api/state') as response:
        state = json.load(response)
    assert state['background_intake_ready'] is True
    with urlopen(root + '/api/intake/progress?project=') as response:
        assert json.load(response)['pending_count'] == 0  # Empty scope is unassigned, not every project.
    with urlopen(root + '/api/intake/progress?project=' + project) as response:
        assert json.load(response)['pending_count'] == 1

    def post(route: str, body: Any) -> Any:
        request = Request(root + '/api/' + route, json.dumps(body).encode(),
                          {'Content-Type': 'application/json', 'X-Workbench-Token': state['token']})
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    with ThreadPoolExecutor(1) as pool:
        future = (pool.submit(phone_analyze[0], entry['id']) if path == 'phone' else
                  pool.submit(post, path, {'id': entry['id'], 'batch': 'batch', 'ids': [entry['id']]}))
        try:
            assert entered.wait(3)
            if path.endswith('enqueue'):
                assert future.result(1)['total'] == 1  # Responded while recognition was held.
            with pytest.raises(HTTPError) as error:
                post('intake/prepare', {'id': entry['id']})
            assert '没有重复执行' in json.load(error.value)['error']
            changed = post('intake/classify', {'id': entry['id'], 'revision': entry['revision'],
                           'role': 'invoice', 'fields': {'amount': '42.00'}})
            assert changed['fields']['amount'] == '42.00'
        finally:
            release.set()
            if path.endswith('prepare'):
                with pytest.raises(HTTPError):
                    future.result(4)
            elif path == 'phone':
                with pytest.raises(ValueError, match='收件已变化'):
                    future.result(4)
            server.shutdown()
            server.server_close()
            thread.join(3)
    assert queue.get(entry['id'])['fields']['amount'] == '42.00'
    assert not store.list_items()


def test_qr_replacement_rejects_target_changed_during_claim(tmp_path: Path, monkeypatch: Any) -> None:
    from invoice_print_layout import invoice_claim, mail_search
    from tests.test_intake_review import image_bytes
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    source = queue.receive('carrier.png', image_bytes())
    target = saved(queue, tmp_path, 1)
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: 'https://tax.example/synthetic')

    def claim(*_args: Any) -> Any:
        changed = {**target, 'revision': target['revision'] + 1, 'fields': {'amount': '42.00'}}
        with queue.store.connect() as db:
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(changed), target['id']))
        return {'status': 'received', 'entry': target}

    monkeypatch.setattr(invoice_claim, 'claim', claim)
    with pytest.raises(ValueError, match='对应发票已变化'):
        queue.prepare_batch_entry(source['id'])
    assert queue.get(source['id']) == source
    assert queue.get(target['id'])['fields']['amount'] == '42.00'
