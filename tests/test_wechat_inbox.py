from dataclasses import replace
from datetime import date

import pytest

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.wechat_inbox import WeChatInbox, category_for
from invoice_print_layout.wechat_reader import HistoryBatch, NormalizedMessage, WeChatReadError


ACCOUNT = 'wxid_test_self'
CHAT = 'test@chatroom'
START = 1_790_000_000


@pytest.fixture
def setup(tmp_path):
    store = LogisticsStore(tmp_path / 'tasks.sqlite3')
    inbox = WeChatInbox(store)
    first = store.create_project('项目一')
    second = store.create_project('项目二')
    return store, inbox, first['id'], second['id']


def bind(inbox, project, category='site', **kwargs):
    return inbox.save_group({'project_id': project, 'name': '测试现场群', 'category': category,
                             'account_id': ACCOUNT, 'chat_id': CHAT, **kwargs})


def batch(text='午饭21份', sender='wxid_test_person', key='source1', **kwargs):
    message = NormalizedMessage(CHAT, key, 1, sender, START + 5, text, '', text, 1)
    return HistoryBatch(CHAT, START, START + 100, (message,), 'ok', **{'issues': (), **kwargs})


def test_draft_projects_persist_without_scheduling_or_fake_bindings(setup):
    store, inbox, first, second = setup
    assert [p['id'] for p in LogisticsStore(store.path).snapshot()['projects']] == [first, second]
    assert inbox.snapshot(first)['groups'] == []
    with pytest.raises(ValueError, match='名称已存在'):
        store.create_project('项目一')
    with pytest.raises(ValueError, match='日期和负责人'):
        store.open_day(first, date(2026, 9, 23))
    assert store.snapshot()['tasks'] == store.snapshot()['outbox'] == []


def test_project_site_is_optional_display_metadata_not_routing_identity(setup):
    store, _, _, _ = setup
    project = store.create_project('有地点的项目', ' 测试地点 ')
    assert project['site_name'] == '测试地点' and project['site_id'] == ''
    assert LogisticsStore(store.path).snapshot()['projects'][-1]['site_name'] == '测试地点'
    with pytest.raises(ValueError, match='200字'):
        store.create_project('过长地点', '地' * 201)


def test_draft_group_can_be_bound_later_and_cannot_change_source(setup):
    store, inbox, first, second = setup
    group = bind(inbox, first, account_id='', chat_id='')
    assert group['status'] == 'unbound'
    with pytest.raises(ValueError, match='同时填写'):
        bind(inbox, first, id=group['id'], chat_id='')
    group = bind(inbox, first, id=group['id'])
    assert group['status'] == 'bound'
    with pytest.raises(ValueError, match='不可换绑'):
        bind(inbox, first, id=group['id'], chat_id='other@chatroom')
    with pytest.raises(ValueError, match='不属于'):
        bind(inbox, second, id=group['id'])
    with pytest.raises(ValueError, match='已绑定'):
        bind(inbox, first, name='重命名')


def test_group_category_edits_are_project_scoped(setup):
    _, inbox, first, second = setup
    group = bind(inbox, first)
    with pytest.raises(ValueError):
        inbox.change_group_category(second, group['id'], 'meals')
    inbox.change_group_category(first, group['id'], 'meals')
    assert inbox.snapshot(first)['groups'][0]['category'] == 'meals'
    assert inbox.snapshot(second)['groups'] == []
    with pytest.raises(ValueError):
        inbox.change_group_category(first, group['id'], 'bad')


def test_project_scope_classification_direction_and_durable_replay(setup):
    store, inbox, first, second = setup
    bind(inbox, first)
    result = inbox.receive_batch(ACCOUNT, batch(), verified_self_id=ACCOUNT)
    assert result['added'] == 1 and result['advanced']
    snapshot = inbox.snapshot(first)
    assert snapshot['messages'][0]['category'] == 'meals'
    assert snapshot['messages'][0]['direction'] == 'incoming'
    assert snapshot['receiver']['status'] == 'not_monitoring'
    assert inbox.snapshot(second)['messages'] == inbox.snapshot()['messages'] == []
    reopened = WeChatInbox(LogisticsStore(store.path))
    assert reopened.receive_batch(ACCOUNT, batch(), verified_self_id=ACCOUNT)['added'] == 0
    assert reopened.snapshot(first)['total'] == 1
    reopened.receive_batch(ACCOUNT, batch(sender=ACCOUNT, key='self1'), verified_self_id=ACCOUNT)
    assert any(m['direction'] == 'outgoing' for m in reopened.snapshot(first)['messages'])
    assert store.snapshot()['tasks'] == store.snapshot()['outbox'] == []


def test_support_query_filters_before_limit_and_preserves_original_messages(setup):
    _, inbox, first, second = setup
    bind(inbox, first)
    for key, text in [('meal', '午饭21份'), ('photo', '进场资料'), ('invoice', '发票报销')]:
        inbox.receive_batch(ACCOUNT, batch(text=text, key=key), verified_self_id=ACCOUNT)
    messages = inbox.snapshot(first)['messages']
    for message in messages:
        if message['text'] == '进场资料':
            inbox.assign(message['key'], first, 'materials')
    support = inbox.snapshot(first, support_only=True)
    assert support['total'] == 2
    assert {m['category'] for m in support['messages']} == {'materials', 'invoices'}
    assert inbox.snapshot(first)['total'] == 3
    assert inbox.snapshot(second, support_only=True)['total'] == 0


def test_shared_group_and_shared_private_chat_never_guess_project(setup):
    _, inbox, first, second = setup
    for chat in [CHAT, 'wxid_test_contact']:
        bind(inbox, first, chat_id=chat, name=chat)
        bind(inbox, second, chat_id=chat, name=chat)
        source = batch(key=chat)
        source = replace(source, chat_id=chat, messages=(replace(source.messages[0], chat_id=chat),))
        inbox.receive_batch(ACCOUNT, source, verified_self_id=ACCOUNT)
    assert inbox.snapshot(first)['messages'] == inbox.snapshot(second)['messages'] == []
    unassigned = inbox.snapshot()['messages']
    assert len(unassigned) == 2
    key = unassigned[0]['key']
    inbox.assign(key, second, 'meals')
    assert len(inbox.snapshot()['messages']) == 1
    assert inbox.snapshot(second)['messages'][0]['status'] == 'manual'
    # Replay cannot replace the operator's decision.
    source = batch(key=inbox.snapshot(second)['messages'][0]['chat_id'])
    chat = inbox.snapshot(second)['messages'][0]['chat_id']
    source = replace(source, chat_id=chat, messages=(replace(source.messages[0], chat_id=chat),))
    inbox.receive_batch(ACCOUNT, source, verified_self_id=ACCOUNT)
    assert inbox.snapshot(second)['total'] == 1


def test_quote_is_preserved_but_not_used_for_category(setup):
    _, inbox, first, _ = setup
    bind(inbox, first)
    source = batch(text='收到')
    source = replace(source, messages=(replace(source.messages[0], type_code=49, quoted_text='午饭21份'),))
    inbox.receive_batch(ACCOUNT, source, verified_self_id=ACCOUNT)
    message = inbox.snapshot(first)['messages'][0]
    assert message['category'] == 'unclassified'
    assert message['quoted_text'] == '午饭21份'


@pytest.mark.parametrize('text,code,purpose,expected', [
    ('午饭21份', 1, 'site', 'meals'), ('住宿减少2人', 1, 'meals', 'lodging'),
    ('发票收到了', 1, 'site', 'invoices'), ('9/23货物进出场请单', 1, '', 'materials'),
    ('午饭21，住宿18', 1, 'meals', 'unclassified'), ('21份', 1, 'meals', 'meals'),
    ('21份', 1, 'site', 'unclassified'), ('发票', 3, 'invoices', 'unclassified'),
    ('午飯', 10002, 'meals', 'unclassified'),
])
def test_classifier_is_conservative(text, code, purpose, expected):
    assert category_for(text, code, purpose) == expected


def test_unknown_identity_and_incomplete_batches_do_not_advance(setup):
    store, inbox, first, _ = setup
    bind(inbox, first)
    assert not inbox.receive_batch(ACCOUNT, batch())['advanced']
    message = inbox.snapshot(first)['messages'][0]
    assert message['direction'] == 'unknown'
    assert 'account_identity_unverified' in message['issues']
    with store.connect() as db:
        assert db.execute('SELECT * FROM wx_checkpoints').fetchall() == []
    incomplete = batch(key='other', issues=('window_at_limit',))
    assert not inbox.receive_batch(ACCOUNT, incomplete, verified_self_id=ACCOUNT)['advanced']
    assert inbox.snapshot(first)['total'] == 2
    with pytest.raises(ValueError, match='不一致'):
        inbox.receive_batch(ACCOUNT, batch(), verified_self_id='wxid_other')


def test_conflict_rolls_back_entire_batch_and_checkpoint(setup):
    store, inbox, first, _ = setup
    bind(inbox, first)
    inbox.receive_batch(ACCOUNT, batch(), verified_self_id=ACCOUNT)
    source = batch(text='午饭99份')
    source = replace(source, end_timestamp=START + 200,
                     messages=(replace(source.messages[0], message_key='fresh'), source.messages[0]))
    with pytest.raises(ValueError, match='不同内容'):
        inbox.receive_batch(ACCOUNT, source, verified_self_id=ACCOUNT)
    assert inbox.snapshot(first)['total'] == 1
    with store.connect() as db:
        assert db.execute('SELECT timestamp FROM wx_checkpoints').fetchone()[0] == START + 100


def test_unbound_and_wrong_chat_are_rejected(setup):
    _, inbox, first, _ = setup
    with pytest.raises(ValueError, match='接收范围'):
        inbox.receive_batch(ACCOUNT, batch(), verified_self_id=ACCOUNT)
    bind(inbox, first)
    source = batch()
    source = replace(source, messages=(replace(source.messages[0], chat_id='wrong@chatroom'),))
    with pytest.raises(ValueError, match='超出'):
        inbox.receive_batch(ACCOUNT, source, verified_self_id=ACCOUNT)
    assert inbox.snapshot(first)['total'] == 0


def test_collector_only_reads_bound_chats_and_keeps_checkpoint_on_failure(setup):
    store, inbox, first, second = setup
    bind(inbox, first)
    bind(inbox, second, account_id='', chat_id='')
    calls = []

    class Reader:
        fail = False

        def read_window(self, chat, start, end):
            calls.append((chat, start, end))
            if self.fail:
                raise WeChatReadError('synthetic failure')
            return replace(batch(), start_timestamp=start, end_timestamp=end)

    reader = Reader()
    result = inbox.collect_once(reader, ACCOUNT, first_timestamp=START, end_timestamp=START + 100, verified_self_id=ACCOUNT)
    assert result[CHAT]['advanced'] and len(calls) == 1
    reader.fail = True
    inbox.collect_once(reader, ACCOUNT, first_timestamp=START, end_timestamp=START + 200, verified_self_id=ACCOUNT)
    assert calls[1] == (CHAT, START, START + 200)
    assert inbox.snapshot(first)['receiver']['status'] == 'error'
    with store.connect() as db:
        assert db.execute('SELECT timestamp FROM wx_checkpoints').fetchone()[0] == START + 100
    store.pause(first)
    assert inbox.collect_once(reader, ACCOUNT, first_timestamp=START, end_timestamp=START + 200, verified_self_id=ACCOUNT) == {}


def test_manual_assignment_retains_source_and_audit(setup):
    store, inbox, first, second = setup
    bind(inbox, first)
    inbox.receive_batch(ACCOUNT, batch(), verified_self_id=ACCOUNT)
    message = inbox.snapshot(first)['messages'][0]
    inbox.assign(message['key'], second, 'lodging')
    assert inbox.snapshot(first)['messages'] == []
    moved = inbox.snapshot(second)['messages'][0]
    assert moved['text'] == message['text'] and moved['chat_id'] == CHAT
    assert moved['category'] == 'lodging'
    with store.connect() as db:
        row = db.execute('SELECT * FROM wx_assignments').fetchone()
        assert row['old_project_id'] == first and row['project_id'] == second
    with pytest.raises(ValueError):
        inbox.assign(message['key'], 'missing', 'meals')
    with pytest.raises(ValueError):
        inbox.assign(message['key'], second, 'missing')
