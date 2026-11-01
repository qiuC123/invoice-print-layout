"""Feishu boundaries use synthetic credentials, HTTP and temporary stores only."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import pytest

from invoice_print_layout import feishu_notify as notify
from invoice_print_layout.workbench import ExpenseStore
from tests.test_mail_agent import pdf


CREDENTIALS = ('synthetic-app', 'synthetic-owner', 'synthetic-secret')


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notify.keyring, 'get_password', lambda *args: pytest.fail('unexpected real Vault access'))


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = False

    def send(self, credentials: Any, text: str, identifier: str) -> None:
        self.calls.append((credentials, text, identifier))
        if self.fail:
            raise OSError('synthetic transport failure with private-secret')


def outbox(store: ExpenseStore) -> list[dict[str, Any]]:
    with store.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM feishu_outbox ORDER BY created,id')]


def make_notifier(tmp_path: Path, *, enabled: Any = lambda: True,
                  clock: Any = time.time, client: Any = None) -> notify.FeishuNotifier:
    return notify.FeishuNotifier(ExpenseStore(tmp_path), enabled, credentials=lambda: CREDENTIALS,
                                 client=client or FakeClient(), clock=clock)


def test_client_posts_text_only_and_caches_until_before_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(notify.time, 'time', lambda: now[0])
    calls: list[tuple[Any, ...]] = []

    def post(*args: Any) -> dict[str, Any]:
        calls.append(args)
        if 'tenant_access_token' in args[0]:
            return {'code': 0, 'tenant_access_token': 'synthetic-token', 'expire': 120}
        return {'code': 0}

    client = notify.FeishuClient(post)
    identifier = str(uuid.uuid4())
    client.send(CREDENTIALS, '合成提醒', identifier)
    assert calls[0] == ('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                        {'app_id': CREDENTIALS[0], 'app_secret': CREDENTIALS[2]})
    assert calls[1] == ('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
                        {'receive_id': CREDENTIALS[1], 'msg_type': 'text',
                         'content': json.dumps({'text': '合成提醒'}, ensure_ascii=False), 'uuid': identifier},
                        'synthetic-token')
    now[0] = 1059
    client.send(CREDENTIALS, '再次提醒', identifier)
    assert len(calls) == 3
    now[0] = 1060
    client.send(CREDENTIALS, '刷新后提醒', identifier)
    assert len(calls) == 5
    client.send((CREDENTIALS[0], CREDENTIALS[1], 'changed-secret'), '更新凭据', identifier)
    assert len(calls) == 7


def test_client_refreshes_after_rejected_send() -> None:
    tokens = []
    sends = []

    def post(url: str, body: dict[str, Any], token: str = '') -> dict[str, Any]:
        if 'tenant_access_token' in url:
            tokens.append(body)
            return {'tenant_access_token': 'synthetic-token', 'expire': 7200}
        sends.append(token)
        if len(sends) == 1:
            raise ValueError('synthetic revoked token')
        return {'code': 0}

    client = notify.FeishuClient(post)
    with pytest.raises(ValueError):
        client.send(CREDENTIALS, '文字', 'same-id')
    client.send(CREDENTIALS, '文字', 'same-id')
    assert len(tokens) == 2 and len(sends) == 2


@pytest.mark.parametrize('result', [{}, {'tenant_access_token': '', 'expire': 100},
                                   {'tenant_access_token': 'token', 'expire': 0},
                                   {'tenant_access_token': 'token', 'expire': True}])
def test_client_rejects_invalid_token(result: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match='令牌'):
        notify.FeishuClient(lambda *args: result).send(CREDENTIALS, '文字', 'id')


def test_credentials_only_read_expected_local_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match='配置'):
        notify.read_credentials(tmp_path)
    config = tmp_path / 'feishu_bot.toml'
    config.write_text('[feishu]\napp_id="synthetic-app"\nowner_open_id="synthetic-owner"\n', encoding='utf-8')
    calls = []

    def get_password(*args: Any) -> str:
        calls.append(args)
        return CREDENTIALS[2]

    monkeypatch.setattr(notify.keyring, 'get_password', get_password)
    assert notify.read_credentials(tmp_path) == CREDENTIALS
    assert calls == [('invoice-print-layout-feishu', CREDENTIALS[0])]
    monkeypatch.setattr(notify.keyring, 'get_password', lambda *args: None)
    with pytest.raises(ValueError, match='凭据'):
        notify.read_credentials(tmp_path)
    config.write_text('[feishu]\napp_id=12\nowner_open_id="owner"\n', encoding='utf-8')
    with pytest.raises(ValueError, match='配置'):
        notify.read_credentials(tmp_path)


def test_mail_text_combines_native_pdf_amounts_and_redacts_private_labels(tmp_path: Path) -> None:
    first, second = tmp_path / 'first.pdf', tmp_path / 'second.pdf'
    first.write_bytes(pdf('价税合计（小写）￥12.30'))
    second.write_bytes(pdf('价税合计（小写）￥4.20'))
    text = notify.mail_text([
        {'project_name': '合成项目', 'subject': '电子票 sender@example.invalid 发票号码12345678901234567890 C:/private/ticket.pdf',
         'sender': 'other@example.invalid', 'files': [{'path': str(first)}, {'path': str(second)}]},
        {'subject': '待分配票', 'files': [{'path': str(tmp_path / 'missing.pdf')}]},
        {'subject': '待核查票', 'issues': ['待处理'], 'files': [{'path': str(tmp_path / 'image.png')}]},
    ])
    assert text.count('发票助手：收到新票') == 1
    assert '合成项目｜附件 2 份｜价税合计 ¥16.50' in text
    assert '待选项目｜附件 1 份｜金额待识别｜待分配票' in text
    assert '需处理｜附件 1 份｜金额待识别｜待核查票' in text
    for private in ['sender@example.invalid', 'other@example.invalid', '12345678901234567890', 'C:/private', str(tmp_path)]:
        assert private not in text


def test_partial_and_ambiguous_native_amounts_remain_unresolved(tmp_path: Path) -> None:
    known, ambiguous = tmp_path / 'known.pdf', tmp_path / 'ambiguous.pdf'
    known.write_bytes(pdf('价税合计（小写）￥8.50'))
    ambiguous.write_bytes(pdf('价税合计（小写）￥12.00\n价税合计（小写）￥14.00'))
    text = notify.mail_text([{'subject': '合成邮件', 'files': [{'path': str(known)}, {'path': str(ambiguous)}]}])
    assert '价税合计 ¥8.50；金额待识别' in text
    assert '20.50' not in text and '22.50' not in text


def test_retry_backoff_restart_and_stable_uuid(tmp_path: Path) -> None:
    now = [1000.0]
    client = FakeClient()
    client.fail = True
    notifier = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    notifier.enqueue('stable-source-event', {'text': '合成新票'})
    notifier.enqueue('stable-source-event', {'text': '重复事件'})
    pending = outbox(notifier.store)
    assert len(pending) == 1
    identifier = pending[0]['id']
    assert str(uuid.UUID(identifier)) == identifier
    notifier.deliver_pending()
    assert outbox(notifier.store)[0]['retry_at'] == 1060
    assert outbox(notifier.store)[0]['attempts'] == 1
    now[0] = 1059
    notifier.deliver_pending()
    assert len(client.calls) == 1
    now[0] = 1060
    notifier.deliver_pending()
    assert outbox(notifier.store)[0]['retry_at'] == 1180
    assert outbox(notifier.store)[0]['attempts'] == 2
    restarted = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    client.fail = False
    now[0] = 1180
    restarted.deliver_pending()
    assert outbox(restarted.store) == []
    assert {call[2] for call in client.calls} == {identifier}
    assert all(call[0] == CREDENTIALS for call in client.calls)
    assert restarted.status()['last_result']['status'] == 'sent'
    assert 'private-secret' not in json.dumps(restarted.status())


def test_expired_record_discarded_once_without_send(tmp_path: Path) -> None:
    now = [1000.0]
    client = FakeClient()
    notifier = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    notifier.enqueue('expires', {'text': '过期提醒'})
    now[0] += 86400
    notifier.deliver_pending()
    result = notifier.status()['last_result']
    assert result['status'] == 'discarded' and '24小时' in result['message']
    assert client.calls == [] and outbox(notifier.store) == []
    now[0] += 100
    notifier.deliver_pending()
    assert notifier.status()['last_result'] == result


def test_disable_prevents_enqueue_and_clears_previous_pending(tmp_path: Path) -> None:
    enabled = [False]
    client = FakeClient()
    notifier = make_notifier(tmp_path, enabled=lambda: enabled[0], client=client)
    notifier.enqueue('disabled', {'text': '不发送'})
    assert outbox(notifier.store) == []
    enabled[0] = True
    notifier.enqueue('before-disable', {'text': '尚未发送'})
    enabled[0] = False
    notifier.deliver_pending()
    assert outbox(notifier.store) == [] and client.calls == []
    assert notifier.status()['enabled'] is False
    assert notifier.status()['last_result']['status'] == 'discarded'


def test_missing_credentials_status_and_test_message(tmp_path: Path) -> None:
    def missing() -> Any:
        raise ValueError('未找到本机飞书应用凭据')

    notifier = make_notifier(tmp_path)
    notifier.credentials = missing
    assert notifier.status() == {'available': False, 'enabled': False,
                                 'reason': '未找到本机飞书应用凭据', 'last_result': None}
    with pytest.raises(ValueError, match='凭据'):
        notifier.test_message()
    assert outbox(notifier.store) == []


@pytest.mark.parametrize('category,description', [('auth', '授权失效'), ('network', '网络连接异常'), ('internal', '程序问题')])
def test_failure_episode_has_two_distinct_phases_without_duplicate_enqueue(tmp_path: Path, category: str, description: str) -> None:
    client = FakeClient()
    notifier = make_notifier(tmp_path, client=client)
    for phase in ['first', 'first', 'recovered', 'recovered']:
        notifier.failure('synthetic-account', 'one-episode', phase, category)
    assert len(outbox(notifier.store)) == 2
    notifier.deliver_pending()
    texts = [call[1] for call in client.calls]
    assert len(texts) == 2
    assert any('自动收件出错：' + description in text for text in texts)
    assert not any('暂停' in text for text in texts)
    assert texts.count('发票助手：自动收件已恢复') == 1


def test_slow_sender_does_not_block_enqueue_or_kick(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()

    class SlowClient:
        def send(self, *args: Any) -> None:
            entered.set()
            assert release.wait(5)

    notifier = make_notifier(tmp_path, client=SlowClient())
    try:
        notifier.enqueue('slow', {'text': '缓慢发送'})
        before = time.monotonic()
        notifier.kick()
        assert time.monotonic() - before < .5
        assert entered.wait(2)
        before = time.monotonic()
        notifier.enqueue('next', {'text': '下个提醒'})
        notifier.kick()
        assert time.monotonic() - before < .5
        assert len(outbox(notifier.store)) == 2
        release.set()
        notifier._thread.join(3)
        assert not notifier._thread.is_alive()
        notifier.kick()
        notifier._thread.join(3)
        assert outbox(notifier.store) == []
    finally:
        release.set()
        notifier.close()


def test_idle_and_backoff_kicks_do_not_connect_or_create_threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    client = FakeClient()
    notifier = make_notifier(tmp_path, clock=lambda: now[0], client=client)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('idle kick connected to SQLite or created a thread')

    def idle_ticks() -> None:
        with monkeypatch.context() as patch:
            patch.setattr(notifier.store, 'connect', forbidden)
            patch.setattr(notify.threading, 'Thread', forbidden)
            for _ in range(1000):
                notifier.kick()

    idle_ticks()
    client.fail = True
    notifier.enqueue('retry', {'text': '合成提醒'})
    notifier.kick()
    notifier._thread.join(3)
    assert len(client.calls) == 1
    idle_ticks()
    # Startup reads the persisted retry time once, then stays idle until due.
    notifier = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    now[0] = 1059
    idle_ticks()
    now[0] = 1060
    client.fail = False
    notifier.kick()
    notifier._thread.join(3)
    assert len(client.calls) == 2 and outbox(notifier.store) == []
    idle_ticks()
    notifier.enqueue('new', {'text': '新提醒唤醒空队列'})
    notifier.kick()
    notifier._thread.join(3)
    assert len(client.calls) == 3 and outbox(notifier.store) == []


def test_enqueue_wakes_backoff_and_expiry_precedes_late_retry(tmp_path: Path) -> None:
    now = [1000.0]
    client = FakeClient()
    notifier = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    client.fail = True
    notifier.enqueue('retry', {'text': '重试'})
    notifier.deliver_pending()
    client.fail = False
    now[0] += 1
    notifier.enqueue('new', {'text': '无需等待旧消息重试'})
    notifier.kick()
    notifier._thread.join(3)
    assert len(client.calls) == 2 and client.calls[-1][1] == '无需等待旧消息重试'
    with notifier.store.connect() as db:
        db.execute('UPDATE feishu_outbox SET retry_at=?', (1000 + 86400 + 300,))
    restarted = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    now[0] = 1000 + 86400
    restarted.kick()
    restarted._thread.join(3)
    assert outbox(notifier.store) == [] and len(client.calls) == 2
    assert restarted.status()['last_result']['status'] == 'discarded'


def test_disabled_restart_clears_backoff_without_waiting(tmp_path: Path) -> None:
    now = [1000.0]
    client = FakeClient()
    notifier = make_notifier(tmp_path, clock=lambda: now[0], client=client)
    client.fail = True
    notifier.enqueue('retry', {'text': '关闭后应清除'})
    notifier.deliver_pending()
    restarted = make_notifier(tmp_path, enabled=lambda: False, clock=lambda: now[0], client=client)
    restarted.kick()
    restarted._thread.join(3)
    assert outbox(notifier.store) == [] and len(client.calls) == 1
