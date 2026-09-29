import threading
import time
import io
from datetime import datetime, timedelta

import pytest
from PIL import Image

from invoice_print_layout.logistics import LogisticsStore, TZ
from invoice_print_layout.project_todos import ProjectTodos
from invoice_print_layout.photo_schedule import PhotoSchedules
from invoice_print_layout.site_photos import SitePhotos
from invoice_print_layout.workbench_web import make_server


@pytest.fixture
def setup(tmp_path):
    store = LogisticsStore(tmp_path / '后勤/tasks.sqlite3')
    pid = store.create_project('定时读取测试')['id']
    todos = ProjectTodos(store)
    photos = SitePhotos(tmp_path, store)
    photos.save_settings(pid, {'account': 'wxid_test', 'auto_classify': True,
                              'groups': [{'id': '123@chatroom', 'name': '现场'}, {'id': '456@chatroom', 'name': '工厂'}]})
    return todos, photos, PhotoSchedules(todos, photos), pid


def task(todos, pid, **config):
    return todos.save(pid, {'title': '整理飞检照片', 'kind': 'scheduled', 'schedule': {
        'enabled': True, 'times': ['18:00'], 'start_on': '2026-09-27', 'end_on': '2026-10-05',
        'group_ids': ['123@chatroom', '456@chatroom'], **config}})


def terminal(photos, pid, key):
    for _ in range(100):
        job = photos.get_job(pid, key)
        if job and job['state'] != 'running':
            return job
        time.sleep(.01)
    pytest.fail('isolated photo job did not finish')


def test_daily_time_dedup_restart_and_next_day(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    task(todos, pid)
    calls = []
    def read(job, body):
        calls.append((body['group_id'], body['start'], body['end']))
        job['duplicates'] += 3
        job.update(done=3, total=3)
    monkeypatch.setattr(photos, 'read_wechat', read)
    moment = datetime(2026, 9, 27, 18, tzinfo=TZ)
    scheduler.tick(moment - timedelta(seconds=1))
    assert todos.list(pid)[0]['last_run'] is None
    scheduler.tick(moment)
    run = todos.list(pid)[0]['last_run']
    assert terminal(photos, pid, run['job_id'])['duplicates'] == 6
    assert photos.get_job(pid, run['job_id'])['total'] == 6
    scheduler.tick(moment)
    assert todos.list(pid)[0]['last_run']['state'] == 'done'
    PhotoSchedules(ProjectTodos(todos.store), photos).tick(moment + timedelta(hours=2))
    assert len(calls) == 2
    scheduler.tick(moment + timedelta(days=1))
    terminal(photos, pid, todos.list(pid)[0]['last_run']['job_id'])
    assert calls[-1][1:] == ('2026-09-28', '2026-09-28')
    assert len(calls) == 4
    scheduler.tick(datetime(2026, 10, 6, 18, tzinfo=TZ))
    assert len(calls) == 4


def test_catchup_latest_today_and_pause_preserves_history(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    item = task(todos, pid, times=['09:00', '18:00'])
    monkeypatch.setattr(photos, 'read_wechat', lambda job, body: None)
    scheduler.tick(datetime(2026, 9, 29, 21, tzinfo=TZ))
    run = todos.list(pid)[0]['last_run']
    assert run['occurrence'] == '2026-09-29 18:00'
    terminal(photos, pid, run['job_id'])
    todos.save(pid, {**item, 'schedule': {**item['schedule'], 'enabled': False}})
    scheduler.tick(datetime(2026, 9, 30, 21, tzinfo=TZ))
    with todos.store.connect() as db:
        assert db.execute('SELECT count(*) FROM photo_schedule_runs').fetchone()[0] == 1
    assert todos.list(pid)[0]['last_run']['state'] == 'done'


def test_busy_queue_then_restart_recovers_without_duplicate_start(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    task(todos, pid)
    photos.update_job({'id': 'manual', 'project_id': pid, 'state': 'running'})
    moment = datetime(2026, 9, 27, 18, tzinfo=TZ)
    scheduler.tick(moment)
    run = todos.list(pid)[0]['last_run']
    assert run['state'] == 'queued'
    photos.update_job({'id': 'manual', 'project_id': pid, 'state': 'done'})
    calls = []
    monkeypatch.setattr(photos, 'read_wechat', lambda job, body: calls.append(body['group_id']))
    scheduler.tick(moment)
    terminal(photos, pid, run['job_id'])
    # Simulate a crash after the durable photo job was created but before run status was saved.
    scheduler.finish(run['id'], 'queued', '等待读取')
    scheduler.tick(moment)
    assert len(calls) == 2
    assert todos.list(pid)[0]['last_run']['state'] == 'done'


def test_failed_group_does_not_block_other_group_and_interruption_is_visible(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    task(todos, pid)
    calls = []
    def read(job, body):
        calls.append(body['group_id'])
        if body['group_id'] == '123@chatroom':
            raise ValueError('现场缓存尚不可用')
    monkeypatch.setattr(photos, 'read_wechat', read)
    moment = datetime(2026, 9, 27, 18, tzinfo=TZ)
    scheduler.tick(moment)
    run = todos.list(pid)[0]['last_run']
    terminal(photos, pid, run['job_id'])
    scheduler.tick(moment)
    assert todos.list(pid)[0]['last_run']['state'] == 'partial'
    assert len(calls) == 2
    scheduler.finish(run['id'], 'running', '读取中')
    photos.update_job({**photos.get_job(pid, run['job_id']), 'state': 'running'})
    recreated = SitePhotos(photos.workspace, todos.store)
    PhotoSchedules(todos, recreated).tick(moment)
    assert todos.list(pid)[0]['last_run']['state'] == 'interrupted'


def test_configuration_scope_and_existing_manual_rows(setup):
    todos, photos, scheduler, pid = setup
    item = task(todos, pid)
    other = todos.store.create_project('另一项目')['id']
    with pytest.raises(ValueError, match='不是当前项目'):
        scheduler.validate(other, item)
    photos.save_settings(pid, {'auto_classify': False})
    with pytest.raises(ValueError, match='开启自动分类'):
        scheduler.validate(pid, item)
    scheduler.validate(pid, {**item, 'schedule': {**item['schedule'], 'enabled': False}})
    manual = todos.save(pid, {'title': '临时补充照片'})
    assert manual['kind'] == 'temporary' and manual['schedule'] == {}
    with pytest.raises(ValueError, match='本项目没有'):
        todos.save(other, item)


def test_busy_queue_does_not_read_previous_day_on_late_restart(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    task(todos, pid)
    photos.update_job({'id': 'manual', 'project_id': pid, 'state': 'running'})
    moment = datetime(2026, 9, 27, 18, tzinfo=TZ)
    scheduler.tick(moment)
    old_run = todos.list(pid)[0]['last_run']
    photos.update_job({'id': 'manual', 'project_id': pid, 'state': 'done'})
    calls = []
    monkeypatch.setattr(photos, 'read_wechat', lambda job, body: calls.append(body['start']))
    scheduler.tick(moment + timedelta(days=1))
    terminal(photos, pid, todos.list(pid)[0]['last_run']['job_id'])
    with todos.store.connect() as db:
        assert db.execute('SELECT state FROM photo_schedule_runs WHERE id=?', (old_run['id'],)).fetchone()[0] == 'cancelled'
    assert calls == ['2026-09-28', '2026-09-28']


@pytest.mark.parametrize('config', [
    {'times': []}, {'times': ['24:00']}, {'times': ['18:60']}, {'times': ['18：00']},
    {'enabled': 'true'}, {'group_ids': []}, {'group_ids': [3]}, {'start_on': ''},
    {'end_on': '2026-09-26'}, {'start_on': '2026-02-30'},
])
def test_schedule_validation(setup, config):
    todos, _, _, pid = setup
    with pytest.raises(ValueError):
        task(todos, pid, **config)
    assert todos.list(pid) == []


def test_schema_upgrade_keeps_old_todo_values(tmp_path):
    store = LogisticsStore(tmp_path / 'old.sqlite3')
    pid = store.create_project('旧项目')['id']
    with store.connect() as db:
        db.execute('CREATE TABLE project_todos(id TEXT PRIMARY KEY,project_id TEXT,title TEXT,note TEXT,due_date TEXT,status TEXT,revision INTEGER,created_at TEXT,updated_at TEXT)')
        db.execute("INSERT INTO project_todos VALUES ('old',?,'旧事项','','','done',3,'2026-09-26','2026-09-26')", (pid,))
    old = ProjectTodos(store).list(pid)[0]
    assert old['kind'] == 'temporary' and old['status'] == 'done' and old['revision'] == 3


def test_server_loop_invokes_scheduler_without_open_browser(tmp_path, monkeypatch):
    called = threading.Event()
    monkeypatch.setattr(PhotoSchedules, 'tick', lambda self: called.set())
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert called.wait(2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_scheduled_intake_uses_existing_automatic_pipeline_and_image_dedup(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    task(todos, pid)
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), 'green').save(stream, format='PNG')
    original = stream.getvalue()
    processed = []
    def process(project, key):
        processed.append((project, key))
        return photos.get(project, key)
    monkeypatch.setattr(photos, 'process', process)
    def read(job, body):
        row = photos.receive(pid, '现场.png', original, {'group_name': body['group_id']})
        job['duplicates' if row.get('duplicate') else 'added'] += 1
    monkeypatch.setattr(photos, 'read_wechat', read)
    now = datetime(2026, 9, 27, 18, tzinfo=TZ)
    scheduler.tick(now)
    result = terminal(photos, pid, todos.list(pid)[0]['last_run']['job_id'])
    assert result['added'] == result['duplicates'] == 1
    assert len(processed) == 1
    assert photos.file(*processed[0]).read_bytes() == original


def test_deleted_schedule_cancels_queue_and_cannot_trigger_on_later_days(setup, monkeypatch):
    todos, photos, scheduler, pid = setup
    item = task(todos, pid)
    photos.update_job({'id': 'manual', 'project_id': pid, 'state': 'running'})
    moment = datetime(2026, 9, 27, 18, tzinfo=TZ)
    scheduler.tick(moment)
    run = todos.list(pid)[0]['last_run']
    assert run['state'] == 'queued'
    deleted = todos.remove(pid, item['id'], item['revision'])
    assert deleted['schedule']['enabled'] is False
    photos.update_job({'id': 'manual', 'project_id': pid, 'state': 'done'})
    monkeypatch.setattr(photos, 'start', lambda *a, **kw: pytest.fail('deleted task must not start'))
    scheduler.tick(moment)
    scheduler.tick(moment + timedelta(days=1))
    with todos.store.connect() as db:
        assert db.execute('SELECT state FROM photo_schedule_runs WHERE id=?', (run['id'],)).fetchone()[0] == 'cancelled'
        assert db.execute('SELECT count(*) FROM photo_schedule_runs').fetchone()[0] == 1
    restored = todos.remove(pid, item['id'], deleted['revision'], restore=True)
    assert restored['schedule']['enabled'] is False
    scheduler.tick(moment + timedelta(days=2))
