from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

import invoice_print_layout.project_events as events
from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ExpenseStore, str, list[float]]:
    clock = [100000.0]
    monkeypatch.setattr(events, 'time', SimpleNamespace(time=lambda: clock[0]))
    store = ExpenseStore(tmp_path / 'synthetic')
    project = ExpenseProjects(store).create('合成项目')
    return store, project['id'], clock


def fee(store: ExpenseStore, project: str, *, verified: bool = True) -> dict[str, Any]:
    item = store.create({'title': '合成住宿', 'project_id': project, 'category': '酒店', 'amount': '12.30'})
    source = make_invoice_pdf(store.workspace / ('synthetic-' + item['id'] + '.pdf'), amount='12.30')
    store.add_attachment(item['id'], source.name, source.read_bytes(), 'invoice')
    return store.transition(item['id'], 'verify') if verified else store.get(item['id'])


def outbox(store: ExpenseStore, status: str = 'pending') -> list[dict[str, Any]]:
    with store.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM invoice_event_outbox WHERE status=?', (status,))]


def test_snapshot_event_and_same_state_dedup(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, _ = scenario
    pending = fee(store, project, verified=False)
    assert not outbox(store)
    ready = fee(store, project)
    payload = json.loads(outbox(store)[0]['payload'])
    assert re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', payload['id'])
    assert payload['source'] == 'invoice-workbench' and payload['type'] == 'invoice.project_ready'
    assert payload['object'] == 'invoice-project:' + project and payload['data_class'] == 'invoice'
    assert '已核对 1 笔，合计 ¥12.30；还有 1 笔在处理' in payload['summary']
    assert payload['ref']['expense_ids'] == [ready['id']] and payload['ref']['pending_count'] == 1
    assert len(payload['ref']['workspace_id']) == 64
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO invoice_project_dirty VALUES (?)', (project,))
    assert len(outbox(store)) == 1
    store.transition(pending['id'], 'verify')
    assert len(outbox(store)) == 1 and json.loads(outbox(store)[0]['payload'])['ref']['verified_count'] == 2
    assert len(outbox(store, 'superseded')) == 1


def test_business_and_outbox_rollback_together(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, _ = scenario
    item = fee(store, project, verified=False)
    with pytest.raises(RuntimeError):
        with store.connect() as db:
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()[0])
            data['verified'] = True
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
            events.capture_changes(store, db)
            assert db.execute('SELECT COUNT(*) FROM invoice_event_outbox').fetchone()[0] == 1
            raise RuntimeError('synthetic rollback')
    assert not store.get(item['id'])['verified'] and not outbox(store)


def test_pending_count_refreshes_queued_summary_without_new_event(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, _ = scenario
    fee(store, project)
    before = outbox(store)[0]
    fee(store, project, verified=False)
    after = outbox(store)[0]
    assert before['id'] == after['id'] and before['version'] == after['version']
    assert json.loads(after['payload'])['ref']['pending_count'] == 1
    assert '还有 1 笔在处理' in json.loads(after['payload'])['summary']


def test_burst_returning_to_an_earlier_set_has_a_new_version(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, _ = scenario
    fee(store, project)
    original = outbox(store)[0]['id']
    second = fee(store, project)
    assert outbox(store)[0]['id'] != original
    store.transition(second['id'], 'cancelled')
    assert len(outbox(store)) == 1 and outbox(store)[0]['id'] != original
    assert json.loads(outbox(store)[0]['payload'])['ref']['verified_count'] == 1


def test_a_previously_delivered_set_and_each_empty_batch_can_notify_again(
        scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, clock = scenario
    sent: list[dict[str, Any]] = []
    sender = events.ProjectEvents(store, clock=lambda: clock[0], sender=lambda _, payload: sent.append(payload))
    first = fee(store, project)
    sender.deliver_pending()
    second = fee(store, project)
    clock[0] += 60
    sender.deliver_pending()
    store.transition(second['id'], 'cancelled')
    clock[0] += 60
    sender.deliver_pending()
    assert [event['ref']['verified_count'] for event in sent] == [1, 2, 1]
    store.transition(first['id'], 'cancelled')
    clock[0] += 60
    sender.deliver_pending()
    third = fee(store, project)
    clock[0] += 60
    sender.deliver_pending()
    store.transition(third['id'], 'cancelled')
    clock[0] += 60
    sender.deliver_pending()
    assert [event['ref']['verified_count'] for event in sent] == [1, 2, 1, 0, 1, 0]
    assert len({event['id'] for event in sent}) == 6


def test_periodic_refresh_detects_source_loss(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, clock = scenario
    item = fee(store, project)
    sent: list[dict[str, Any]] = []
    sender = events.ProjectEvents(store, clock=lambda: clock[0], sender=lambda _, payload: sent.append(payload))
    sender.deliver_pending()
    with store.connect() as db:
        original = Path(db.execute('SELECT path FROM attachments WHERE item_id=?', (item['id'],)).fetchone()[0])
    original.unlink()
    clock[0] += 60
    sender._run()
    assert len(sent) == 2 and sent[-1]['ref']['verified_count'] == 0
    assert not store.get(item['id'])['ready']


def test_coalescing_minute_throttle_and_empty_state(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, clock = scenario
    sent: list[dict[str, Any]] = []
    sender = events.ProjectEvents(store, clock=lambda: clock[0], sender=lambda _, payload: sent.append(payload))
    first = fee(store, project)
    sender.deliver_pending()
    assert len(sent) == 1
    clock[0] += 5
    second = fee(store, project)
    clock[0] += 5
    third = fee(store, project)
    sender.deliver_pending()
    assert len(sent) == 1 and len(outbox(store)) == 1
    clock[0] += 50
    sender.deliver_pending()
    assert len(sent) == 2 and sent[-1]['ref']['verified_count'] == 3
    for item in (first, second, third):
        store.transition(item['id'], 'cancelled')
    clock[0] += 60
    sender.deliver_pending()
    assert sent[-1]['ref']['verified_count'] == 0 and len(sent) == 3
    assert len({payload['id'] for payload in sent}) == 3


def test_delivery_retries_backoff_cap_and_expiry(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, clock = scenario
    fee(store, project)
    def fail(_: str, __: dict[str, Any]) -> None:
        raise OSError('synthetic offline')
    sender = events.ProjectEvents(store, clock=lambda: clock[0], sender=fail)
    for attempt in range(9):
        sender.deliver_pending()
        row = outbox(store)[0]
        assert row['attempts'] == attempt + 1
        assert row['retry_at'] - clock[0] == min(3600, 60 * 2 ** min(attempt, 6))
        sender.deliver_pending()
        assert outbox(store)[0]['attempts'] == attempt + 1
        clock[0] = row['retry_at']
    clock[0] = 100000 + 86400
    sender.deliver_pending()
    assert not outbox(store) and outbox(store, 'discarded')[0]['error'] == 'expired_24h'
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%超过24小时丢弃%'").fetchone()[0] == 1


def test_http_runs_outside_transaction_and_replaced_inflight_is_not_retried(
        scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, clock = scenario
    fee(store, project)
    def mutate(_: str, __: dict[str, Any]) -> None:
        fee(store, project)
        raise OSError('synthetic failed after business mutation')
    sender = events.ProjectEvents(store, clock=lambda: clock[0], sender=mutate)
    sender.deliver_pending()
    assert len(outbox(store)) == 1 and len(outbox(store, 'superseded')) == 1
    assert json.loads(outbox(store)[0]['payload'])['ref']['verified_count'] == 2


def test_crash_recovers_claimed_delivery_and_receiver_dedup(scenario: tuple[ExpenseStore, str, list[float]]) -> None:
    store, project, clock = scenario
    fee(store, project)
    original = outbox(store)[0]['id']
    with store.connect() as db:
        db.execute("UPDATE invoice_event_outbox SET status='sending'")
    sent: list[dict[str, Any]] = []
    sender = events.ProjectEvents(store, clock=lambda: clock[0], sender=lambda _, payload: sent.append(payload))
    sender.deliver_pending()
    assert sent[0]['id'] == original and len(outbox(store, 'sent')) == 1
    sender.deliver_pending()
    assert len(sent) == 1


def test_loopback_http_contract_ignores_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(202)
            self.end_headers()
            self.wfile.write(json.dumps({'id': calls[-1]['id'], 'accepted': False}).encode())
        def log_message(self, *_: Any) -> None:
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    try:
        events.post_event(f'http://127.0.0.1:{server.server_port}/events', {'id': 'synthetic-event'})
        assert calls == [{'id': 'synthetic-event'}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('url', ['https://127.0.0.1/events', 'http://example.com/events', 'http://user:secret@localhost/events', 5])
def test_configured_event_address_is_local(tmp_path: Path, url: Any) -> None:
    store = ExpenseStore(tmp_path)
    (store.root / 'project-events.json').write_text(json.dumps({'url': url}), encoding='utf-8')
    with pytest.raises(ValueError):
        events.event_url(store)
