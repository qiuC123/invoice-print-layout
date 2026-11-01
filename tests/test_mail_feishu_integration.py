"""Automatic intake and settings boundaries use only fake credentials and transport."""
from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from invoice_print_layout import feishu_notify
from invoice_print_layout.mail163 import MailImportError
from tests.test_mail_agent import env, enable, message, run  # noqa: F401
from tests.test_settings_workflow import web, secrets  # noqa: F401


def pending(service: Any) -> list[dict[str, Any]]:
    with service.store.connect() as db:
        return [json.loads(row['payload']) for row in db.execute('SELECT payload FROM feishu_outbox ORDER BY rowid')]


def test_auto_new_mail_one_round_one_notice_and_replay_does_not_repeat(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    results.extend([message(1), message(2, '未知项目'), {**message(3), 'files': []}])
    enable(service)
    run(service)
    notices = pending(service)
    assert len(notices) == 1 and len(notices[0]['messages']) == 2
    assert {m['project_name'] for m in notices[0]['messages']} == {'测试项目', ''}
    run(service)
    assert pending(service) == notices
    service._run(False)
    assert pending(service) == notices


@pytest.mark.parametrize('category,label', [('auth', '授权失效'), ('network', '网络'), ('internal', '程序问题')])
def test_failure_episode_threshold_and_recovery_persist_without_retry_spam(env: Any, category: str, label: str) -> None:
    service, store, agent, _, _, _, _, _ = env
    original = service._fetcher

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise MailImportError('private raw transport error', category=category)

    enable(service)
    service._fetcher = fail
    run(service)
    assert len(pending(service)) == (1 if category == 'auth' else 0)
    # Resume the service over the same durable account and outbox.
    from invoice_print_layout.mail_agent import MailAgentService
    resumed = MailAgentService(store, agent, fetcher=fail)
    run(resumed)
    assert len(pending(service)) == 1 and label in pending(service)[0]['text']
    run(resumed)
    assert len(pending(service)) == 1
    run(resumed)
    assert len(pending(service)) == 1
    resumed._fetcher = original
    run(resumed)
    run(resumed)
    texts = [notice['text'] for notice in pending(service)]
    assert len(texts) == 2 and '已恢复' in texts[1]
    assert 'private raw' not in str(texts)
    resumed._fetcher = fail
    run(resumed)
    assert len(pending(service)) == (3 if category == 'auth' else 2)
    run(resumed)
    assert len(pending(service)) == 3


@pytest.mark.parametrize('category', ['network', 'internal'])
def test_single_transient_failure_and_recovery_are_silent(env: Any, category: str) -> None:
    service, _, _, _, _, _, _, _ = env
    enable(service)
    fetch = service._fetcher

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise MailImportError('synthetic transient failure', category=category)

    service._fetcher = fail
    run(service)
    assert pending(service) == []
    service._fetcher = fetch
    run(service)
    assert pending(service) == []


@pytest.mark.parametrize('category', ['auth', 'network', 'internal'])
@pytest.mark.parametrize('recover_disabled', [False, True])
def test_disabled_failure_episode_is_never_backfilled(env: Any, category: str, recover_disabled: bool) -> None:
    from invoice_print_layout.mail_agent import MailAgentService
    service, store, agent, _, _, _, _, _ = env
    enable(service, feishu_enabled=False)
    fetch = service._fetcher

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise MailImportError('synthetic disabled failure', category=category)

    service._fetcher = fail
    run(service)
    if recover_disabled:
        service._fetcher = fetch
        run(service)
    assert pending(service) == []
    enable(service, feishu_enabled=True)
    resumed = MailAgentService(store, agent, fetcher=fetch if recover_disabled else fail)
    run(resumed)
    run(resumed)
    resumed._fetcher = fetch
    run(resumed)
    assert pending(service) == []
    # A new failure episode after recovery can notify normally.
    resumed._fetcher = fail
    run(resumed)
    run(resumed)
    assert len(pending(service)) == 1


def test_disabled_notifies_nothing_and_pending_flags_survive_restart_gap(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    enable(service, feishu_enabled=False)
    results.append(message(1))
    run(service)
    assert pending(service) == []
    enable(service, feishu_enabled=True)
    settings = service._settings()
    account = service._account(settings, service._config())
    service._persist_message(account, message(2), service._config())
    # Crash after saving originals, before batching: the persisted flag is the recovery boundary.
    assert len(pending(service)) == 0
    service.notifier.mail_batch(account['id'])
    assert len(pending(service)) == 1
    service.notifier.mail_batch(account['id'])
    assert len(pending(service)) == 1


def test_manual_collect_now_does_not_notify(env: Any) -> None:
    from invoice_print_layout.mail_agent import MailAgentService
    service, _, _, _, _, _, results, _ = env
    enable(service)
    results.append(message(1))
    service.collect_now()
    MailAgentService.kick(service)
    service._thread.join(5)
    assert not service._thread.is_alive() and service._messages()
    assert pending(service) == []


def test_disabling_notifications_wakes_and_clears_future_retry(env: Any) -> None:
    service, _, _, _, _, _, _, _ = env
    enable(service)
    service.notifier.enqueue('future', {'text': '合成提醒'})
    with service.store.connect() as db:
        db.execute('UPDATE feishu_outbox SET retry_at=retry_at+3600')
    service.notifier._schedule()
    enable(service, feishu_enabled=False)
    service.notifier.kick()
    service.notifier._thread.join(3)
    assert pending(service) == []
    assert service.notifier.status()['last_result']['status'] == 'discarded'


def test_manual_failure_and_recovery_cannot_consume_auto_notifications(env: Any) -> None:
    service, _, _, _, _, _, _, _ = env
    enable(service)
    fetch = service._fetcher

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise MailImportError('synthetic', category='network')

    service._fetcher = fail
    for _ in range(3):
        service._run(True, notify=False)
    assert pending(service) == []
    run(service)
    assert len(pending(service)) == 1 and '出错' in pending(service)[0]['text']
    run(service)
    assert len(pending(service)) == 1 and '出错' in pending(service)[0]['text']
    run(service)
    assert len(pending(service)) == 1
    service._fetcher = fetch
    service._run(True, notify=False)
    assert len(pending(service)) == 1
    run(service)
    run(service)
    assert len(pending(service)) == 2 and '已恢复' in pending(service)[1]['text']


def test_slow_delivery_does_not_block_next_mail_cycle(env: Any) -> None:
    service, _, _, _, _, _, results, _ = env
    started, release, collected = threading.Event(), threading.Event(), threading.Event()

    class SlowClient:
        def send(self, *args: Any) -> None:
            started.set()
            release.wait(10)

    service.notifier.credentials = lambda: ('fake-app', 'fake-owner', 'fake-secret')
    service.notifier.client = SlowClient()
    enable(service)
    results.append(message(1))
    run(service)
    service.notifier.kick()
    try:
        assert started.wait(3)
        results.append(message(2))

        def collect() -> None:
            run(service)
            collected.set()

        thread = threading.Thread(target=collect)
        thread.start()
        assert collected.wait(3), 'mail fetching waited for the blocked Feishu transport'
        assert len(service._messages()) == 2
        thread.join(3)
    finally:
        release.set()
        service.notifier.close()


def test_feishu_settings_missing_credentials_and_test_token(web: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout.feishu_notify import FeishuNotifier
    monkeypatch.setattr(FeishuNotifier, 'kick', lambda self: None)
    monkeypatch.setattr(feishu_notify, 'read_credentials', lambda path: ('fake-app', 'fake-owner', 'fake-secret'))
    client = web.client
    state = client.request('/api/settings')[1]['mail']['feishu']
    assert state['available'] and state['enabled'] and 'fake-secret' not in json.dumps(state)
    assert client.request('/api/settings/feishu/test', {}, **{'X-Workbench-Token': 'bad'})[0] == 403
    code, result = client.request('/api/settings/feishu/test', {})
    assert code == 200 and result['last_result']['status'] == 'queued'
    assert client.request('/api/settings/feishu', {'enabled': False})[1]['enabled'] is False
    assert json.loads((web.store.root / 'mail-agent.json').read_text(encoding='utf-8'))['feishu_enabled'] is False
    assert client.request('/api/settings/feishu/test', {})[0] == 400
    assert client.request('/api/settings/feishu', {'enabled': True})[1]['enabled'] is True

    def missing(path: Any) -> Any:
        raise ValueError('private credential detail')

    monkeypatch.setattr(feishu_notify, 'read_credentials', missing)
    result = client.request('/api/settings')[1]['mail']['feishu']
    assert not result['available'] and not result['enabled'] and result['reason']
    assert 'private credential detail' not in str(result)
    assert client.request('/api/settings/feishu/test', {})[0] == 400
    assert client.request('/api/settings/feishu', {'enabled': True})[0] == 400
