from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any
import uuid

from PIL import Image
import pytest

from invoice_print_layout import receipt_devices, workbench_web
from invoice_print_layout.agent_jobs import AgentService
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.organize_settings import OrganizeSettings
from invoice_print_layout.receipt_devices import DeviceError, ReceiptDevices
from invoice_print_layout.reimbursement_people import ReimbursementPeople
from invoice_print_layout.workbench import ExpenseStore, CATEGORIES
from tests.helpers import make_invoice_pdf


def image(color: str = 'white') -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (40, 60), color).save(output, format='PNG')
    return output.getvalue()


def upload_body(project: str, **values: Any) -> dict[str, Any]:
    return {'client_id': str(uuid.uuid4()), 'name': 'synthetic.png', 'data': base64.b64encode(image()).decode(),
            'captured_at': '2026-11-01T10:00:00', 'project_id': project,
            'amount_cents': 8930, 'category': '餐饮', 'person_id': '', 'note': '合成收件备注', **values}


@pytest.fixture
def devices(tmp_path: Path, monkeypatch: Any) -> Any:
    monkeypatch.setattr(receipt_devices.phone_link, 'addresses', lambda: [])
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    project = ExpenseProjects(queue.store).create('Synthetic device')['id']
    calls: list[Any] = []
    agent = AgentService(queue.store)
    monkeypatch.setattr(agent, 'kick', lambda: calls.append(('agent',)))
    background = SimpleNamespace(enqueue=lambda batch, ids: calls.append(('local', batch, ids)))
    service = ReceiptDevices(queue, background, agent, threading.Lock(), port=0)
    monkeypatch.setattr(service, 'kick_dispatch', lambda: None)
    try:
        yield service, project, calls
    finally:
        service.close()
        agent.close()


def pair(service: ReceiptDevices) -> dict[str, Any]:
    status = service.start_pairing()
    device = service.pair({'code': status['code'], 'device_name': 'Synthetic phone'})
    service.authenticate('Bearer ' + device['device_key'])
    return device


def test_pair_once_hash_only_expiry_and_wrong_code_limit(devices: Any) -> None:
    service, _, _ = devices
    first = service.start_pairing()
    assert len(first['code']) == 8 and first['code'].isdigit() and first['qr'].startswith('data:image/png;')
    assert first['pair_uri'].startswith('invoicepair:1?code=')
    result = service.pair({'code': first['code'], 'device_name': 'Synthetic phone'})
    key = result['device_key']
    assert len(key) == 43
    with service.store.connect() as db:
        stored = dict(db.execute('SELECT * FROM receipt_devices').fetchone())
    assert stored['key_hash'] == hashlib.sha256(key.encode()).hexdigest()
    assert key not in json.dumps(stored) and key not in json.dumps(service.status())
    with pytest.raises(DeviceError, match='已使用'):
        service.pair({'code': first['code'], 'device_name': 'Again'})
    service.start_pairing()
    with service.store.connect() as db:
        db.execute('UPDATE receipt_device_pairing SET expires_at=0')
    with pytest.raises(DeviceError) as expired:
        service.pair({'code': '00000000', 'device_name': 'Expired'})
    assert expired.value.code == 'bad_code'
    fresh = service.start_pairing()
    for _ in range(5):
        with pytest.raises(DeviceError):
            service.pair({'code': 'wrong', 'device_name': 'Wrong'})
    with pytest.raises(DeviceError):
        service.pair({'code': fresh['code'], 'device_name': 'Too late'})
    assert 'code' not in service.status()


@pytest.mark.parametrize('address,allowed', [('10.0.0.1', True), ('172.16.2.3', True),
    ('192.168.3.1', True), ('127.0.0.1', True), ('127.0.0.2', False), ('100.64.1.1', False),
    ('172.32.0.1', False), ('8.8.8.8', False), ('::1', False), ('169.254.1.1', False)])
def test_source_filter(address: str, allowed: bool) -> None:
    assert receipt_devices.allowed_source(address) is allowed


def test_catalog_active_projects_sidebar_order_people_main_person(devices: Any) -> None:
    service, project, _ = devices
    second = service.projects.create('Synthetic pinned')
    service.projects.pin(second['id'], True, second['project_revision'])
    reimbursed = service.projects.create('Synthetic reimbursed')
    service.projects.set_reimbursement_status(reimbursed['id'], 'reimbursed', 0)
    deleted = service.projects.create('Synthetic deleted')
    service.projects.delete_permanently(deleted['id'], deleted['project_revision'], confirmed=True,
                                        confirmation_name=deleted['name'])
    people = service.people
    owner = people.create(project, 'Main synthetic')['person']
    retired = people.create(project, 'Retired synthetic')['person']
    people.retire(project, retired['id'], retired['version'])
    people.set_main_person(project, owner['id'], 0, owner['version'])
    value = service.catalog()
    assert value['categories'] == list(CATEGORIES)
    assert [p['id'] for p in value['projects']] == [second['id'], project]
    assert value['projects'][1]['people'] == [{'id': owner['id'], 'label': owner['label']}]
    assert value['projects'][1]['main_person_id'] == owner['id']


def test_background_address_cache_and_socket_fallback_do_not_block_requests(devices: Any, monkeypatch: Any) -> None:
    service, _, _ = devices
    service.address_worker.join()
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []
    def enumerate_addresses() -> list[dict[str, str]]:
        calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5)
        return [{'address': '192.168.1.8'}, {'address': '8.8.8.8'}]
    monkeypatch.setattr(receipt_devices.phone_link, 'addresses', enumerate_addresses)
    service.addresses = ()
    service.address_refresh_at = 0
    assert service.server is None
    service.tick()  # Closed listener: no adapter enumeration.
    assert not entered.wait(0.3) and calls == []
    status = service.start_pairing()  # Opening the listener refreshes addresses in the background.
    assert entered.wait(1)
    try:
        port = service.server.server_port
        started = time.perf_counter()
        code, device = request(port, '/device/v1/pair', {'code': status['code'], 'device_name': 'Synthetic fallback'},
                               Host='8.8.8.8:8769')
        assert code == 200 and device['urls'] == [f'http://127.0.0.1:{port}']
        code, catalog = request(port, '/device/v1/catalog', Authorization='Bearer ' + device['device_key'])
        assert code == 200 and catalog['urls'] == device['urls']
        assert time.perf_counter() - started < 1
        assert calls == ['receipt-addresses']
    finally:
        release.set()
        service.address_worker.join()
    assert service.urls('127.0.0.1') == [f'http://192.168.1.8:{port}']
    service.tick()
    assert len(calls) == 1
    service.address_refresh_at = 0
    service.tick()
    service.address_worker.join()
    assert len(calls) == 2


def test_unconfirmed_device_first_authentication_and_ten_minute_cleanup(devices: Any, monkeypatch: Any) -> None:
    service, _, _ = devices
    clock = [time.time()]
    monkeypatch.setattr(receipt_devices, 'time', SimpleNamespace(time=lambda: clock[0]))
    status = service.start_pairing()
    pending = service.pair({'code': status['code'], 'device_name': 'Pending synthetic'})
    assert service.status()['devices'][0]['confirmed'] == 0
    service.sync_listener()
    assert service.server is None  # Pending devices do not retain the listener.
    clock[0] += receipt_devices.PAIR_SECONDS - 1
    service.tick()
    assert len(service.status()['devices']) == 1
    clock[0] += 1
    with pytest.raises(DeviceError) as expired:
        service.authenticate('Bearer ' + pending['device_key'])
    assert expired.value.status == 401
    service.tick()
    assert service.status()['devices'] == [] and service.confirmation_due is None
    confirmed = pair(service)
    assert service.status()['devices'][0]['confirmed'] == 1
    clock[0] += receipt_devices.PAIR_SECONDS
    service.tick()
    assert service.server is not None
    assert service.authenticate('Bearer ' + confirmed['device_key']) == confirmed['device_id']


def test_legacy_device_schema_migrates_as_confirmed(devices: Any) -> None:
    service, _, _ = devices
    device = pair(service)
    service.close()
    with service.store.connect() as db:
        db.execute('ALTER TABLE receipt_devices DROP COLUMN confirmed')
        db.execute('ALTER TABLE receipt_devices DROP COLUMN confirmation_expires_at')
    restarted = ReceiptDevices(service.intake, service.background, service.agent, threading.Lock(), port=0)
    try:
        restarted.sync_listener()
        assert restarted.status()['devices'][0]['confirmed'] == 1
        restarted.revoke(device['device_id'])
        assert restarted.server is not None
        code, rejected = request(restarted.server.server_port, '/device/v1/catalog',
                                 Authorization='Bearer ' + device['device_key'])
        assert code == 401 and rejected['code'] == 'unpaired'
    finally:
        restarted.close()


def test_upload_manual_fields_null_idempotency_and_content_duplicates(devices: Any, monkeypatch: Any) -> None:
    service, project, _ = devices
    device_id = pair(service)['device_id']
    owner = service.people.create(project, 'Synthetic owner')['person']
    body = upload_body(project, person_id=owner['id'])
    first = service.upload(device_id, body)
    assert first['status'] == 'received'
    entry = service.intake.get(first['entry_id'])
    assert entry['source'] == 'upload' and entry['fields'] == {'amount': '89.30', 'category': '餐饮'}
    assert entry['manual_fields'] == ['amount', 'category']
    assert entry['reimbursement_person_id'] == owner['id']
    assert entry['intake_note'] == body['note'] and 'note' not in entry['fields']
    assert Path(entry['path']).read_bytes() == image()
    # Run the real analysis merge against contrary OCR without invoking an engine.
    import invoice_print_layout.intake_queue as queue_module
    monkeypatch.setattr(queue_module, 'read_receipt', lambda *_: [])
    monkeypatch.setattr(queue_module, 'parse_receipt', lambda *_: {
        'role': 'receipt', 'fields': {'amount': '12.00', 'category': '其他', 'merchant': 'Synthetic merchant'},
        'warnings': [], 'text': 'Synthetic OCR'})
    analyzed = service.intake.analyze_local(entry['id'])
    assert analyzed['fields']['amount'] == '89.30' and analyzed['fields']['category'] == '餐饮'
    assert analyzed['reimbursement_person_id'] == owner['id']
    assert analyzed['intake_note'] == body['note'] and 'note' not in analyzed['fields']
    assert service.upload(device_id, {**body, 'project_id': 'now-unavailable'}) == first
    assert service.upload(device_id, upload_body(project)) == {'status': 'duplicate', 'entry_id': entry['id']}
    other = service.upload(device_id, upload_body(project, amount_cents=None,
                            data=base64.b64encode(image('red')).decode(), note=''))
    empty = service.intake.get(other['entry_id'])
    assert empty['manual_fields'] == ['category'] and 'amount' not in empty['fields'] and 'intake_note' not in empty
    assert len(service.intake.list()) == 2


def test_simultaneous_upload_one_receipt_and_replay_survives_restart(devices: Any) -> None:
    service, project, _ = devices
    device = pair(service)
    body = upload_body(project)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: service.upload(device['device_id'], body), range(8)))
    assert all(result == results[0] for result in results) and len(service.intake.list()) == 1
    service.close()
    restarted = ReceiptDevices(service.intake, service.background, service.agent, threading.Lock(), port=0)
    try:
        restarted.sync_listener()
        assert restarted.server is not None
        assert restarted.authenticate('Bearer ' + device['device_key']) == device['device_id']
        assert restarted.upload(device['device_id'], body) == results[0]
    finally:
        restarted.close()


def test_pending_pairing_and_failed_attempts_survive_restart(devices: Any) -> None:
    service, _, _ = devices
    status = service.start_pairing()
    for _ in range(4):
        with pytest.raises(DeviceError):
            service.pair({'code': '错误', 'device_name': 'Wrong code'})
    service.close()
    restarted = ReceiptDevices(service.intake, service.background, service.agent, threading.Lock(), port=0)
    try:
        restarted.sync_listener()
        assert restarted.server is not None and restarted.status()['code'] == status['code']
        with pytest.raises(DeviceError):
            restarted.pair({'code': 'wrong', 'device_name': 'Fifth mistake'})
        with pytest.raises(DeviceError):
            restarted.pair({'code': status['code'], 'device_name': 'Invalidated'})
        restarted.sync_listener()
        assert restarted.server is None
        fresh = restarted.start_pairing()
        paired = restarted.pair({'code': fresh['code'], 'device_name': 'Fresh code'})
        restarted.revoke(paired['device_id'])
        assert restarted.server is not None
    finally:
        restarted.close()


@pytest.mark.parametrize('changes,code', [({'project_id': 'missing'}, 'project_unavailable'),
    ({'person_id': 'missing'}, 'person_unavailable'), ({'amount_cents': -1}, 'invalid'),
    ({'amount_cents': True}, 'invalid'), ({'category': 'unknown'}, 'invalid'),
    ({'note': 'x' * 201}, 'invalid'), ({'data': 'bad'}, 'invalid'),
    ({'captured_at': '2026-11-01T10:00:00+08:00'}, 'invalid')])
def test_invalid_upload_has_no_side_effects(devices: Any, changes: Any, code: str) -> None:
    service, project, _ = devices
    device_id = pair(service)['device_id']
    with pytest.raises(DeviceError) as error:
        service.upload(device_id, upload_body(project, **changes))
    assert error.value.code == code and not service.intake.list()
    assert not list(service.intake.folder.iterdir())


def test_unavailable_person_and_project_conflicts(devices: Any) -> None:
    service, project, _ = devices
    device = pair(service)['device_id']
    person = service.people.create(project, 'Retired')['person']
    service.people.retire(project, person['id'], person['version'])
    other = service.projects.create('Other')['id']
    foreign = service.people.create(other, 'Foreign')['person']
    for person_id in (person['id'], foreign['id']):
        with pytest.raises(DeviceError) as error:
            service.upload(device, upload_body(project, person_id=person_id))
        assert (error.value.status, error.value.code) == (409, 'person_unavailable')
    service.projects.set_reimbursement_status(project, 'reimbursed', 0)
    with pytest.raises(DeviceError) as error:
        service.upload(device, upload_body(project))
    assert (error.value.status, error.value.code) == (409, 'project_unavailable')


@pytest.mark.parametrize('automatic', [False, True])
def test_dispatch_reuses_upload_organizer_or_local_queue(devices: Any, monkeypatch: Any, automatic: bool) -> None:
    service, project, calls = devices
    OrganizeSettings(service.store.root).update({'auto_after_upload': automatic, 'use_model': False})
    device_id = pair(service)['device_id']
    result = service.upload(device_id, upload_body(project))
    monkeypatch.setattr(service.intake, 'claim_batch_qr', lambda key: calls.append(('claim', key)) or service.intake.get(key))
    service._dispatch_pending()
    if automatic:
        assert calls[0] == ('claim', result['entry_id']) and calls[1] == ('agent',)
        job = service.agent.jobs.list(project)[0]
        assert job['material_ids'] == [result['entry_id']] and job['use_model'] is False
    else:
        assert len(calls) == 1 and calls[0][0] == 'local' and calls[0][2] == [result['entry_id']]
        assert not service.agent.jobs.list(project)
    service._dispatch_pending()
    assert len(calls) == (2 if automatic else 1)


def test_tax_qr_claim_copies_confirmations_to_new_pdf_and_dispatches_pdf(devices: Any, monkeypatch: Any, tmp_path: Path) -> None:
    service, project, calls = devices
    result = service.upload(pair(service)['device_id'], upload_body(project))
    pdf = make_invoice_pdf(tmp_path / 'synthetic-invoice.pdf').read_bytes()
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', lambda *_: 'https://synthetic.test/qr')
    def claim(queue: Any, url: str, project: str) -> dict[str, Any]:
        return {'status': 'received', 'entry': queue.receive('claimed.pdf', pdf, project_id=project)}
    monkeypatch.setattr('invoice_print_layout.invoice_claim.claim', claim)
    service._dispatch_pending()
    source = service.intake.get(result['entry_id'])
    replacement = service.intake.get(source['replacement_id'])
    assert source['status'] == 'converted'
    assert replacement['fields'] == {'amount': '89.30', 'category': '餐饮'}
    assert replacement['manual_fields'] == ['amount', 'category']
    assert replacement['intake_note'] == '合成收件备注' and 'note' not in replacement['fields']
    assert service.agent.jobs.list(project)[0]['material_ids'] == [replacement['id']]


def test_busy_project_retries_without_competing_job_and_stops_if_reimbursed(devices: Any, monkeypatch: Any) -> None:
    service, project, _ = devices
    clock = [time.time()]
    monkeypatch.setattr(receipt_devices, 'time', SimpleNamespace(time=lambda: clock[0]))
    device_id = pair(service)['device_id']
    first = service.upload(device_id, upload_body(project))
    monkeypatch.setattr(service.intake, 'claim_batch_qr', lambda key: service.intake.get(key))
    busy = service.agent.jobs.create(project, [first['entry_id']], 'synthetic_busy_job')
    with service.store.connect() as db:
        db.execute("UPDATE receipt_device_uploads SET dispatch_state='done'")
    second = service.upload(device_id, upload_body(project, data=base64.b64encode(image('red')).decode()))
    service._dispatch_pending()
    with service.store.connect() as db:
        assert db.execute('SELECT dispatch_state FROM receipt_device_uploads WHERE entry_id=?',
                          (second['entry_id'],)).fetchone()[0] == 'pending'
    assert len(service.agent.jobs.list(project)) == 1
    service.projects.set_reimbursement_status(project, 'reimbursed', 0)
    service.agent.jobs.stop(busy['id'], 'ready', 'Synthetic done')
    clock[0] += receipt_devices.DISPATCH_RETRY_SECONDS
    service._dispatch_pending()
    assert len(service.agent.jobs.list(project)) == 1
    with service.store.connect() as db:
        assert db.execute('SELECT dispatch_state FROM receipt_device_uploads WHERE entry_id=?',
                          (second['entry_id'],)).fetchone()[0] == 'attention'
    assert service.intake.get(second['entry_id'])['status'] == 'received'


@pytest.mark.parametrize('replacement', [False, True])
def test_busy_ticks_claim_once_wait_for_retry_and_dispatch_when_idle(
        devices: Any, monkeypatch: Any, tmp_path: Path, replacement: bool) -> None:
    service, project, _ = devices
    clock = [time.time()]
    monkeypatch.setattr(receipt_devices, 'time', SimpleNamespace(time=lambda: clock[0]))
    decoded: list[bytes] = []
    def qr(payload: bytes) -> str:
        decoded.append(payload)
        return 'https://synthetic.test/qr' if replacement else ''
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', qr)
    pdf = make_invoice_pdf(tmp_path / 'synthetic-retry.pdf').read_bytes()
    monkeypatch.setattr('invoice_print_layout.invoice_claim.claim',
                        lambda queue, url, project: {'status': 'received',
                                                     'entry': queue.receive('claimed.pdf', pdf, project_id=project)})
    result = service.upload(pair(service)['device_id'], upload_body(project))
    other = service.intake.receive('busy.png', image('red'), project_id=project)
    busy = service.agent.jobs.create(project, [other['id']], 'synthetic_busy_job')
    monkeypatch.setattr(service, 'kick_dispatch', ReceiptDevices.kick_dispatch.__get__(service))
    def tick() -> None:
        service.tick()
        if service.dispatch_worker:
            service.dispatch_worker.join(5)
            assert not service.dispatch_worker.is_alive()
    tick()
    assert decoded == [image()]
    with service.store.connect() as db:
        saved = dict(db.execute('SELECT * FROM receipt_device_uploads').fetchone())
    assert saved['qr_checked'] == 1 and saved['dispatch_state'] == 'pending'
    assert saved['retry_at'] == clock[0] + receipt_devices.DISPATCH_RETRY_SECONDS
    connect, queries = service.store.connect, []
    def counted_connect() -> Any:
        queries.append(1)
        return connect()
    with monkeypatch.context() as scope:
        scope.setattr(service.store, 'connect', counted_connect)
        for _ in range(29):
            clock[0] += .5
            tick()
        assert not queries
    clock[0] += .5
    tick()  # Still busy: check jobs first, without calling the QR reader again.
    assert decoded == [image()] and len(service.agent.jobs.list(project)) == 1
    assert service.dispatch_due == clock[0] + receipt_devices.DISPATCH_RETRY_SECONDS
    service.agent.jobs.stop(busy['id'], 'ready', 'Synthetic done')
    tick()
    assert len(service.agent.jobs.list(project)) == 1
    clock[0] += receipt_devices.DISPATCH_RETRY_SECONDS
    tick()
    source = service.intake.get(result['entry_id'])
    target = source['replacement_id'] if replacement else source['id']
    jobs = service.agent.jobs.list(project)
    assert len(jobs) == 2 and any(job['material_ids'] == [target] for job in jobs)
    assert decoded == [image()] and service.dispatch_due is None
    with service.store.connect() as db:
        assert db.execute('SELECT dispatch_state FROM receipt_device_uploads').fetchone()[0] == 'done'


@pytest.mark.parametrize('after_upload', [False, True])
def test_idle_ticks_do_not_open_database(devices: Any, monkeypatch: Any, after_upload: bool) -> None:
    service, project, _ = devices
    if after_upload:
        OrganizeSettings(service.store.root).update({'auto_after_upload': False})
        service.upload(pair(service)['device_id'], upload_body(project))
        service._dispatch_pending()
    service.sync_listener()
    monkeypatch.setattr(service, 'kick_dispatch', ReceiptDevices.kick_dispatch.__get__(service))
    connects: list[int] = []
    connect = service.store.connect
    def counted_connect() -> Any:
        connects.append(1)
        return connect()
    with monkeypatch.context() as scope:
        scope.setattr(service.store, 'connect', counted_connect)
        for _ in range(100):
            service.tick()
        assert not connects


def test_pairing_expiry_tick_stops_listener_without_devices(devices: Any, monkeypatch: Any) -> None:
    service, _, _ = devices
    clock = [time.time()]
    monkeypatch.setattr(receipt_devices, 'time', SimpleNamespace(time=lambda: clock[0]))
    service.start_pairing()
    assert service.server is not None
    clock[0] += receipt_devices.PAIR_SECONDS
    service.tick()
    assert service.server is None and service.pairing_expires_at == 0
    with monkeypatch.context() as scope:
        scope.setattr(service.store, 'connect', lambda: pytest.fail('idle tick opened database after expiry'))
        service.tick()


def test_pending_retry_and_qr_check_survive_restart(devices: Any, monkeypatch: Any) -> None:
    service, project, _ = devices
    clock = [time.time()]
    monkeypatch.setattr(receipt_devices, 'time', SimpleNamespace(time=lambda: clock[0]))
    result = service.upload(pair(service)['device_id'], upload_body(project))
    other = service.intake.receive('busy.png', image('red'), project_id=project)
    busy = service.agent.jobs.create(project, [other['id']], 'synthetic_restart_busy')
    decoded: list[bytes] = []
    monkeypatch.setattr('invoice_print_layout.mail_search.qr_value', lambda payload: decoded.append(payload) or '')
    service._dispatch_pending()
    assert len(decoded) == 1
    service.close()
    restarted = ReceiptDevices(service.intake, service.background, service.agent, threading.Lock(), port=0)
    try:
        restarted.sync_listener()
        assert restarted.dispatch_due == clock[0] + receipt_devices.DISPATCH_RETRY_SECONDS
        with monkeypatch.context() as scope:
            scope.setattr(service.store, 'connect', lambda: pytest.fail('retry woke before its deadline'))
            restarted.tick()
        service.agent.jobs.stop(busy['id'], 'ready', 'Synthetic done')
        clock[0] += receipt_devices.DISPATCH_RETRY_SECONDS
        restarted.tick()
        restarted.dispatch_worker.join(5)
        assert not restarted.dispatch_worker.is_alive()
        assert len(decoded) == 1
        assert any(job['material_ids'] == [result['entry_id']] for job in service.agent.jobs.list(project))
    finally:
        restarted.close()


def test_qr_needing_manual_claim_does_not_start_recognition_or_organizer(devices: Any, monkeypatch: Any) -> None:
    service, project, calls = devices
    result = service.upload(pair(service)['device_id'], upload_body(project))
    monkeypatch.setattr(service.intake, 'claim_batch_qr', lambda key: {**service.intake.get(key), 'qr_status': 'manual'})
    service._dispatch_pending()
    assert not calls and not service.agent.jobs.list(project)
    assert service.status()['attention_count'] == 1
    assert service.intake.get(result['entry_id'])['status'] == 'received'


def request(port: int, path: str, body: Any = None, **headers: str) -> tuple[int, Any]:
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('POST' if body is not None else 'GET', path,
                     json.dumps(body).encode() if body is not None else None,
                     {'Content-Type': 'application/json', **headers})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_real_http_pair_catalog_upload_security_and_shutdown(devices: Any, monkeypatch: Any) -> None:
    service, project, _ = devices
    status = service.start_pairing()
    port = status['port']
    status_code, paired = request(port, '/device/v1/pair', {'code': status['code'], 'device_name': 'HTTP phone'})
    assert status_code == 200
    auth = {'Authorization': 'Bearer ' + paired['device_key']}
    for headers in ({}, {'Authorization': 'Bearer wrong'}):
        assert request(port, '/device/v1/catalog', **headers) == (401, {
            'error': '手机尚未配对或已撤销，请重新配对', 'code': 'unpaired'})
    assert request(port, '/device/v1/catalog', **auth, Origin='')[0] == 403
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('OPTIONS', '/device/v1/receipts', headers={'Origin': 'http://evil.test'})
    response = conn.getresponse()
    assert response.status == 403 and json.loads(response.read())['code'] == 'forbidden'
    conn.close()
    assert request(port, '/device/v1/receipts', upload_body(project), **auth, Origin='http://evil.test')[0] == 403
    code, catalog = request(port, '/device/v1/catalog', **auth)
    assert code == 200 and catalog['projects'][0]['id'] == project
    body = upload_body(project)
    code, received = request(port, '/device/v1/receipts', body, **auth)
    assert code == 200 and received['status'] == 'received'
    assert request(port, '/device/v1/receipts', body, **auth) == (200, received)
    with monkeypatch.context() as scope:
        scope.setattr(receipt_devices, 'allowed_source', lambda _: False)
        assert request(port, '/device/v1/catalog', **auth)[0] == 403
    # Revoking the last device keeps the real listener available to return 401.
    service.revoke(paired['device_id'])
    assert request(port, '/device/v1/catalog', **auth)[0] == 401
    with service.store.connect() as db:
        db.execute('UPDATE receipt_device_pairing SET expires_at=0')
    service.sync_listener()
    assert service.server is not None
    code, rejected = request(port, '/device/v1/catalog', **auth)
    assert code == 401 and rejected['code'] == 'unpaired'


def test_body_limit_and_malformed_json(devices: Any) -> None:
    service, _, _ = devices
    paired = pair(service)
    port = service.server.server_port
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('POST', '/device/v1/receipts', b'', {'Authorization': 'Bearer ' + paired['device_key'],
                 'Content-Length': str(receipt_devices.MAX_BODY + 1)})
    response = conn.getresponse()
    assert response.status == 413 and json.loads(response.read())['code'] == 'too_large'
    conn.close()
    conn = HTTPConnection('127.0.0.1', port, timeout=5)
    conn.request('POST', '/device/v1/receipts', b'no json', {'Authorization': 'Bearer ' + paired['device_key']})
    response = conn.getresponse()
    assert response.status == 400 and json.loads(response.read())['code'] == 'invalid'
    conn.close()


def test_receipt_app_status_skips_phone_status_and_checks_source(devices: Any, monkeypatch: Any) -> None:
    service, _, _ = devices
    paired = pair(service)
    service.close()
    server = workbench_web.make_server(service.store.workspace, 0, mode='invoice', device_port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def slow_status(*args: Any, **kwargs: Any) -> Any:
            pytest.fail('receipt-app must not call browser phone status')

        with monkeypatch.context() as scope:
            scope.setattr(workbench_web.PhoneLink, 'status', slow_status)
            code, status = request(server.server_port, '/api/phone-link/receipt-app')
            assert code == 200
            assert status['enabled'] is True
            assert status['devices'][0]['id'] == paired['device_id']
            assert 'receipt_app' not in status
            for headers in ({'Origin': 'https://untrusted.invalid'}, {'Host': 'untrusted.invalid'}):
                assert request(server.server_port, '/api/phone-link/receipt-app', **headers)[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_occupied_device_port_does_not_prevent_desktop_start(devices: Any) -> None:
    service, _, _ = devices
    paired = pair(service)
    service.close()
    with socket.socket() as occupied:
        occupied.bind(('0.0.0.0', 0))
        occupied.listen()
        server = workbench_web.make_server(service.store.workspace, 0, mode='invoice', device_port=occupied.getsockname()[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert request(server.server_port, '/api/state')[0] == 200
            status = request(server.server_port, '/api/phone-link')[1]['receipt_app']
            assert status['enabled'] is False and '端口' in status['error']
            assert status['devices'][0]['id'] == paired['device_id']
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
