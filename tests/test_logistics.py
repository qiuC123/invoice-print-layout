from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import itertools

_counter = itertools.count()
from concurrent.futures import ThreadPoolExecutor

import pytest

from invoice_print_layout.logistics import LogisticsStore, Project, SourceMessage, TZ, meal_changes

DAY = date(2026, 9, 23)


def clock(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=TZ)


def setup(tmp_path: Path) -> tuple[LogisticsStore, list[dict]]:
    store = LogisticsStore(tmp_path / 'tasks.sqlite3')
    store.add_project(Project('project', '合成测试项目', 'site', 'account', 'owner', 'worker',
                              'group', 'dm', 'supplier', DAY, DAY + timedelta(days=2)))
    return store, store.open_day('project', DAY)


def task(store: LogisticsStore, kind: str = 'lunch') -> dict:
    return next(t for t in store.snapshot()['tasks'] if t['kind'] == kind)


def message(mid: str = 'm1', text: str = '21份', hour: int = 9, minute: int = 1, **kwargs) -> SourceMessage:
    return replace(SourceMessage('account', 'group', 'worker', mid, clock(hour, minute) + timedelta(seconds=next(_counter) % 60), text), **kwargs)


def test_business_dates_schedules_and_idempotent_open(tmp_path):
    store, rows = setup(tmp_path)
    assert len(rows) == 5
    assert store.open_day('project', DAY) == rows
    assert task(store, 'breakfast')['business_day'] == '2026-09-24'
    assert task(store, 'breakfast')['ask_at'].endswith('20:00:00+08:00')
    assert task(store, 'lodging')['deadline'].endswith('23:30:00+08:00')
    assert len(store.open_day('project', DAY + timedelta(days=2))) == 4
    with pytest.raises(ValueError):
        store.open_day('project', DAY - timedelta(days=1))


def test_stages_restart_and_deadline_do_not_catch_up(tmp_path):
    store, _ = setup(tmp_path)
    assert not store.tick(clock(8, 59))
    group = store.tick(clock(9))[0]
    assert group['stage'] == 'group' and group['payload']['mentions'] == ['worker']
    assert store.claim(group['id'], now=clock(9, 10))['target'] == 'group'
    store.delivery_result(group['id'], verified=True, evidence='readback:group-message')
    store = LogisticsStore(store.path)
    assert len(store.tick(clock(9, 20))) == 1
    direct = store.tick(clock(9, 30))[-1]
    assert direct['stage'] == 'direct' and direct['payload']['target'] == 'dm'
    store.collector_status('project', clock(10), healthy=True)
    out = store.tick(clock(10))
    assert out[-1]['payload']['reason'] == 'reply_missing'
    assert out[1]['state'] == 'cancelled'
    assert len(store.tick(clock(10, 1))) == 3


def test_late_start_and_collector_failure_are_distinct(tmp_path):
    store, _ = setup(tmp_path)
    out = store.tick(clock(10))
    assert [a['stage'] for a in out] == ['escalate']
    assert out[0]['payload']['reason'] == 'collector_unavailable'
    assert task(store)['quantity'] is None


@pytest.mark.parametrize('offset,healthy', [(-121, True), (1, True), (0, False)])
def test_stale_future_or_failed_collection_not_no_reply(tmp_path, offset, healthy):
    store, _ = setup(tmp_path)
    store.collector_status('project', clock(10) + timedelta(seconds=offset), healthy=healthy)
    assert store.tick(clock(10))[0]['payload']['reason'] == 'collector_unavailable'


def test_direct_and_group_share_task_delta_dedup_and_restart(tmp_path):
    store, _ = setup(tmp_path)
    store.tick(clock(9))
    assert store.receive(message())['state'] == 'review'
    delta = message('m2', '再加2份', chat_id='dm')
    assert store.receive(delta)['state'] == 'review'
    assert task(store)['quantity'] == 23
    store = LogisticsStore(store.path)
    assert store.receive(delta)['duplicate'] is True
    assert task(store)['quantity'] == 23 and task(store)['revision'] == 2
    assert not any(a['state'] == 'pending' and a['stage'] in {'group', 'direct'} for a in store.tick(clock(9, 30)))


@pytest.mark.parametrize('fields', [{'sender_id': 'stranger'}, {'sender_id': 'self'}, {'chat_id': 'other'}, {'account_id': 'other'}])
def test_only_scoped_responsible_person(tmp_path, fields):
    store, _ = setup(tmp_path)
    assert store.receive(message(**fields))['state'] == 'unmatched'
    assert task(store)['quantity'] is None


def test_no_cross_project_guess(tmp_path):
    store, _ = setup(tmp_path)
    store.add_project(Project('second', '另一测试项目', 'site', 'account', 'owner', 'worker',
                              'othergroup', 'dm', 'supplier', DAY, DAY))
    store.open_day('second', DAY)
    assert store.receive(message(chat_id='dm'))['state'] == 'ambiguous_project'
    assert all(t['quantity'] is None for t in store.snapshot()['tasks'])


def test_night_and_breakfast_need_separate_counts(tmp_path):
    store, _ = setup(tmp_path)
    assert store.receive(message('bare', '21份', 20))['state'] == 'clarify'
    assert task(store, 'supper')['quantity'] is None
    assert task(store, 'breakfast')['quantity'] is None
    assert store.receive(message('both', '夜宵21份，早餐18份', 20))['state'] == 'review'
    assert task(store, 'supper')['quantity'] == 21
    assert task(store, 'breakfast')['quantity'] == 18


@pytest.mark.parametrize('text', ['再加2份', '收到', '等下', '大约21份', '21或者23份', '-1份', '21.5份', '10001份'])
def test_uncertain_counts_do_not_become_orders(tmp_path, text):
    store, _ = setup(tmp_path)
    result = store.receive(message(text=text))
    assert result['state'] in {'clarify', 'ignored'}
    assert task(store)['quantity'] is None


def test_zero_is_explicit_and_confirm_is_versioned(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message(text='0份'))
    row = task(store)
    with pytest.raises(ValueError):
        store.confirm('otherowner', row['id'], 1)
    action = store.confirm('owner', row['id'], 1)
    assert store.confirm('owner', row['id'], 1) == action
    assert len([a for a in store.snapshot()['outbox'] if a['stage'] == 'supplier']) == 1
    store.receive(message('correction', '改成23份'))
    assert store.claim(action, now=clock(9, 10)) is None
    with pytest.raises(ValueError):
        store.confirm('owner', row['id'], 1)
    assert store.claim(store.confirm('owner', row['id'], 2), now=clock(9, 10))['quantity'] == 23


def test_uncertain_correction_blocks_old_confirmation(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    row = task(store)
    old = store.confirm('owner', row['id'], 1)
    store.receive(message('uncertain', '可能改成23份'))
    assert store.claim(old, now=clock(9, 10)) is None
    with pytest.raises(ValueError):
        store.confirm('owner', row['id'], 1)


def test_ack_does_not_cancel_approved_quantity(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    action = store.confirm('owner', task(store)['id'], 1)
    assert store.receive(message('ack', '收到'))['state'] == 'ignored'
    assert store.claim(action, now=clock(9, 10))['quantity'] == 21


def test_unknown_delivery_never_blindly_retried(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    action = store.confirm('owner', task(store)['id'], 1)
    assert store.claim(action, now=clock(9, 10))
    assert LogisticsStore(store.path).claim(action, now=clock(9, 10)) is None
    store.delivery_result(action, verified=False, evidence='timeout:local-log')
    assert store.claim(action, now=clock(9, 10)) is None
    assert task(store)['state'] == 'confirmed'
    store.delivery_result(action, verified=True, evidence='readback:verified-id')
    assert task(store)['state'] == 'sent'
    assert store.confirm('owner', task(store)['id'], 1) == action
    assert store.claim(action, now=clock(9, 10)) is None


def test_sent_old_version_does_not_finish_new_version(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    old = store.confirm('owner', task(store)['id'], 1)
    store.claim(old, now=clock(9, 10))
    store.receive(message('m2', '改成23份'))
    store.delivery_result(old, verified=True, evidence='old-message')
    assert task(store)['state'] == 'review'


def test_two_workers_cannot_claim_same_action(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    action = store.confirm('owner', task(store)['id'], 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: LogisticsStore(store.path).claim(action, now=clock(9, 10)), range(2)))
    assert sum(result is not None for result in results) == 1


def test_pause_stops_outbox_and_new_tasks(tmp_path):
    store, _ = setup(tmp_path)
    action = store.tick(clock(9))[0]['id']
    store.pause('project')
    assert store.claim(action, now=clock(9, 10)) is None
    assert store.receive(message())['state'] == 'unmatched'
    assert all(a['state'] == 'cancelled' for a in store.tick(clock(10)))
    with pytest.raises(ValueError):
        store.open_day('project', DAY)


def test_naive_time_rejected(tmp_path):
    store, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        store.tick(datetime(2026, 9, 23, 9))
    with pytest.raises(ValueError):
        store.receive(message(occurred_at=datetime(2026, 9, 23, 9)))


def test_mixed_duplicate_labels_apply_nothing():
    for text in ['夜宵21份，夜宵23份', '夜宵21份，早餐18或者20份']:
        values, reason = meal_changes(text, {'supper', 'breakfast'})
        assert values == {} and reason


def test_late_old_message_never_overwrites_new_count(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message('new', '23份', occurred_at=clock(9, 10)))
    assert store.receive(message('old', '21份', occurred_at=clock(9, 5)))['state'] == 'older_message'
    assert task(store)['quantity'] == 23 and task(store)['revision'] == 1


def test_equal_second_conflicting_messages_require_clarification(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message('first', '23份', occurred_at=clock(9, 10)))
    result = store.receive(message('second', '21份', occurred_at=clock(9, 10)))
    assert result['state'] == 'clarify'
    assert task(store)['quantity'] == 23
    with pytest.raises(ValueError):
        store.confirm('owner', task(store)['id'], 1)


def test_same_source_identity_different_body_rejected(tmp_path):
    store, _ = setup(tmp_path)
    first = message()
    store.receive(first)
    with pytest.raises(ValueError):
        store.receive(replace(first, text='200份'))
    assert task(store)['quantity'] == 21


def test_uncertain_correction_escalates_at_deadline(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    store.receive(message('unsure', '数量需要改一下'))
    assert task(store)['state'] == 'clarify'
    assert any(a['stage'] == 'escalate' for a in store.tick(clock(10)))


def test_resume_restores_only_unsent_and_still_current_reminders(tmp_path):
    store, _ = setup(tmp_path)
    action = store.tick(clock(9))[0]['id']
    store.pause('project')
    store.pause('project', False)
    assert store.tick(clock(9, 5))[0]['state'] == 'pending'
    assert store.claim(action, now=clock(9, 10))
    store.pause('project')
    store.pause('project', False)
    assert store.tick(clock(9, 15))[0]['state'] == 'in_flight'


def test_claim_expired_reminder_without_tick_does_not_send(tmp_path):
    store, _ = setup(tmp_path)
    action = store.tick(clock(9))[0]['id']
    assert store.claim(action, now=clock(10, 10)) is None
    assert store.snapshot()['outbox'][0]['state'] == 'cancelled'


def test_explicit_supper_correction_after_lodging_start(tmp_path):
    store, _ = setup(tmp_path)
    result = store.receive(message('late', '夜宵23份', 22, 35))
    assert result['state'] == 'review'
    assert task(store, 'supper')['quantity'] == 23
    assert task(store, 'lodging')['quantity'] is None


def test_delivery_failure_not_person_missing_reply(tmp_path):
    store, _ = setup(tmp_path)
    store.tick(clock(9))
    store.collector_status('project', clock(10), healthy=True)
    assert store.tick(clock(10))[-1]['payload']['reason'] == 'inquiry_not_delivered'


def test_breakfast_late_reply_after_midnight_keeps_business_date(tmp_path):
    store, _ = setup(tmp_path)
    result = store.receive(message('late-breakfast', '早餐18份', occurred_at=clock(0, 30) + timedelta(days=1)))
    assert result['state'] == 'review'
    assert task(store, 'breakfast')['business_day'] == '2026-09-24'
    assert task(store, 'breakfast')['quantity'] == 18


def test_yesterday_tasks_do_not_steal_current_lunch_reply(tmp_path):
    store, _ = setup(tmp_path)
    store.open_day('project', DAY + timedelta(days=1))
    result = store.receive(message('tomorrow', '21份', occurred_at=clock(9, 5) + timedelta(days=1)))
    assert result['state'] == 'review'
    changed = [t for t in store.snapshot()['tasks'] if t['quantity'] is not None]
    assert len(changed) == 1 and changed[0]['kind'] == 'lunch' and changed[0]['business_day'] == '2026-09-24'


def test_unrelated_group_chat_creates_no_clarification(tmp_path):
    store, _ = setup(tmp_path)
    assert store.receive(message(text='我去仓库拿工具'))['state'] == 'ignored'
    assert store.snapshot()['outbox'] == []


def test_supplier_order_not_blindly_sent_after_business_day(tmp_path):
    store, _ = setup(tmp_path)
    store.receive(message())
    action = store.confirm('owner', task(store)['id'], 1)
    assert store.claim(action, now=clock(9) + timedelta(days=1)) is None
    assert task(store)['state'] == 'expired'
    with pytest.raises(ValueError):
        store.confirm('owner', task(store)['id'], 1)


def test_new_uncertain_revision_has_own_deadline_escalation(tmp_path):
    store, _ = setup(tmp_path)
    store.tick(clock(10))
    store.receive(message('late', '21份', occurred_at=clock(10, 5)))
    store.receive(message('uncertain', '可能改成23份', occurred_at=clock(10, 6)))
    out = store.tick(clock(10, 7))
    escalations = [a for a in out if a['stage'] == 'escalate']
    assert [a['revision'] for a in escalations] == [0, 1]
