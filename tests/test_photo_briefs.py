import io
import json
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from PIL import Image
import pytest

from invoice_print_layout.bot import FeishuBotSettings
from invoice_print_layout.logistics import LogisticsStore, TZ
from invoice_print_layout.project_todos import ProjectTodos
from invoice_print_layout.site_photos import SitePhotos
from invoice_print_layout.photo_schedule import PhotoSchedules
from invoice_print_layout.photo_briefs import PhotoBriefs, make_card
from invoice_print_layout.workbench_web import make_server


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = LogisticsStore(tmp_path / '后勤/tasks.sqlite3')
    todos = ProjectTodos(store)
    photos = SitePhotos(tmp_path, store)
    pid = store.create_project('测试车展')['id']
    sent = []
    def send(item):
        sent.append(item)
        return 'om_test'
    monkeypatch.setattr('invoice_print_layout.photo_briefs.read_bot_settings', lambda _: FeishuBotSettings('cli_test_app', 'ou_test'))
    monkeypatch.setattr('invoice_print_layout.photo_briefs.read_bot_secret', lambda _: 'not-a-real-secret')
    briefs = PhotoBriefs(tmp_path, todos, photos, send)
    return briefs, photos, todos, pid, sent


def record(photos, pid, color='green', saved=True):
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), color).save(stream, format='PNG')
    row = photos.receive(pid, 'test.png', stream.getvalue())
    if saved:
        row.update(state='saved', fields={'kind': '车辆', 'place': '工厂'})
        photos.put(pid, row)
    return row


def row(briefs, key):
    with briefs.todos.store.connect() as db:
        return dict(db.execute('SELECT * FROM photo_briefs WHERE id=?', (key,)).fetchone())


def test_disabled_by_default_owner_only_preview_and_restart_dedup(setup):
    briefs, photos, todos, pid, sent = setup
    record(photos, pid)
    with pytest.raises(ValueError, match='启用'):
        briefs.preview(pid)
    briefs.pump()
    assert not sent
    assert briefs.configure(pid, True)['enabled']
    key = briefs.preview(pid)
    assert briefs.preview(pid) == key
    briefs.deliver_one()
    restarted = PhotoBriefs(photos.workspace, todos, photos, briefs.sender)
    restarted.preview(pid)
    restarted.pump()
    assert len(sent) == 1 and sent[0]['owner'] == 'ou_test'
    assert row(briefs, key)['state'] == 'sent'
    text = json.loads(sent[0]['card'])['elements'][0]['text']['content']
    assert '现有照片 1 张' in text and '并非今天新增' in text
    assert 'test.png' not in text and str(photos.workspace) not in text


def test_network_retry_preserves_uuid_and_stops_outside_dedup_window(setup):
    briefs, _, _, pid, sent = setup
    briefs.configure(pid, True)
    key = briefs.preview(pid)
    def fail(item):
        sent.append(item)
        raise TimeoutError('private network diagnostic must not leak')
    briefs.sender = fail
    briefs.deliver_one(now=10000)
    briefs.deliver_one(now=10030)
    assert len(sent) == 1 and row(briefs, key)['state'] == 'retry'
    briefs.deliver_one(now=10061)
    assert len(sent) == 2 and sent[0]['id'] == sent[1]['id']
    assert 'private' not in row(briefs, key)['last_error']
    briefs.deliver_one(now=14000)
    assert row(briefs, key)['state'] == 'uncertain' and len(sent) == 2


def test_sending_lease_prevents_concurrent_delivery_and_recovers(setup):
    briefs, photos, todos, pid, sent = setup
    briefs.configure(pid, True)
    key = briefs.preview(pid)
    with todos.store.connect() as db:
        db.execute("UPDATE photo_briefs SET state='sending', first_attempt=10000, lease=10120 WHERE id=?", (key,))
    other = PhotoBriefs(photos.workspace, todos, photos, briefs.sender)
    other.deliver_one(now=10060)
    assert not sent
    other.deliver_one(now=10121)
    assert len(sent) == 1 and row(briefs, key)['state'] == 'sent'


def test_disabled_project_does_not_deliver_and_another_project_is_isolated(setup):
    briefs, photos, todos, pid, sent = setup
    briefs.configure(pid, True)
    key = briefs.preview(pid)
    briefs.configure(pid, False)
    briefs.pump()
    assert not sent and row(briefs, key)['state'] == 'cancelled'
    other = todos.store.create_project('其他项目')['id']
    record(photos, other)
    assert briefs.view(other) == {'enabled': False, 'include_pdf': False, 'preparing': False, 'recipient': '已绑定的本人飞书账号', 'latest': None}


@pytest.mark.parametrize('state,words', [('done','整理完成'), ('partial','部分完成'), ('failed','未完成'), ('interrupted','未完成')])
def test_terminal_schedule_enqueues_once_and_only_new_project_photos(setup, state, words):
    briefs, photos, todos, pid, sent = setup
    old = record(photos, pid)
    new = record(photos, pid, 'red', saved=False)
    briefs.configure(pid, True)
    job = {'id': 'job', 'project_id': pid, 'state': state, 'added': 1, 'duplicates': 7,
           'issues': [{'message':'source unavailable'}] if state == 'partial' else [], 'added_ids':[new['id']], 'photo_ids':[old['id'],new['id']]}
    if state == 'done':
        new.update(state='saved',fields={'kind':'人员'})
        photos.put(pid,new)
    photos.update_job(job)
    with todos.store.connect() as db:
        db.execute("UPDATE photo_brief_settings SET enabled_from='2026-09-26 00:00' WHERE project_id=?",(pid,))
        db.execute('INSERT INTO photo_schedule_runs VALUES (?,?,?,?,?,?,?)',('run','task',pid,'2026-09-27 18:00',state,'job','result'))
        db.execute('INSERT INTO photo_schedule_runs VALUES (?,?,?,?,?,?,?)',('old-run','task',pid,'2026-09-25 18:00','done','job','old'))
    briefs.pump();briefs.pump()
    assert len(sent) == 1
    card = json.loads(sent[0]['card'])
    assert words in card['header']['title']['content']
    text = card['elements'][0]['text']['content']
    assert '本次新增记录 1 张' in text and '重复记录 7 条' in text and '工厂：1' not in text


def test_no_new_photos_and_failure_without_job_are_honest():
    card = make_card({'name':'测试'},[],day='2026-09-27',job={'added':0,'duplicates':4,'issues':[]})
    assert '没有新增照片' in card['elements'][0]['text']['content']
    failed = make_card({'name':'测试'},[],day='2026-09-27',run_state='failed')
    assert '未完成' in failed['header']['title']['content']
    assert '没有新增照片' not in failed['elements'][0]['text']['content']


def test_two_wechat_sources_use_separate_real_bridge_directories(setup, monkeypatch):
    _, photos, todos, pid, _ = setup
    photos.save_settings(pid,{'account':'wxid_test','groups':[{'id':'1@chatroom','name':'甲'},{'id':'2@chatroom','name':'乙'}]})
    (photos.workspace / 'photo-reader.json').write_text(json.dumps({k:'test' for k in ['python','helper_dir','audited_source','helper_hashes','media_sha256']}))
    requests=[]
    def bridge(args,**kwargs):
        path=Path(args[-1]);requests.append(path)
        (path.parent/'result.json').write_text(json.dumps({'photos':[],'issues':[]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('invoice_print_layout.site_photos.subprocess.run',bridge)
    job={'id':'multi','project_id':pid,'action':'wechat','state':'running','added':0,'duplicates':0,'issues':[],'photo_ids':[],'added_ids':[]}
    photos._run(job,{'group_ids':['1@chatroom','2@chatroom'],'start':'2026-09-27','end':'2026-09-27'})
    assert job['state']=='done' and len(requests)==2
    assert requests[0].parent != requests[1].parent and all(p.exists() for p in requests)


def test_each_tracks_added_and_duplicate_photo_ids(setup):
    _, photos, _, pid, _ = setup
    job={'id':'ids','project_id':pid,'action':'wechat','issues':[]}
    photos.each(job,[1,2],lambda value:{'id':'same','duplicate':value==2,'state':'saved'})
    assert job['photo_ids']==['same'] and job['added_ids']==['same']


def test_all_sources_unavailable_is_failed_not_partial_success(setup, monkeypatch):
    _, photos, _, pid, _ = setup
    photos.save_settings(pid,{'groups':[{'id':'1@chatroom','name':'甲'},{'id':'2@chatroom','name':'乙'}]})
    def unavailable(*args):raise ValueError('缓存不可用')
    monkeypatch.setattr(photos,'read_wechat',unavailable)
    job={'id':'unavailable','project_id':pid,'action':'wechat','state':'running','added':0,'duplicates':0,'issues':[]}
    photos._run(job,{'group_ids':['1@chatroom','2@chatroom']})
    assert job['state']=='failed' and len(job['issues'])==2


def test_gateway_card_uses_owner_uuid_and_message_receipt():
    from invoice_print_layout.feishu_bot import FeishuGateway
    requests=[]
    def create(request):
        requests.append(request)
        return SimpleNamespace(success=lambda:True,data=SimpleNamespace(message_id='om_receipt'))
    client=SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(create=create))))
    assert FeishuGateway(client).send_card('ou_owner',{'test':'card'},'unique')=='om_receipt'
    request=requests[0]
    assert request.receive_id_type=='open_id' and request.request_body.receive_id=='ou_owner'
    assert request.request_body.uuid=='unique' and request.request_body.msg_type=='interactive'


def test_schedule_to_delivery_pipeline_and_shared_photo_dedup(setup, monkeypatch):
    briefs, photos, todos, pid, sent = setup
    photos.save_settings(pid, {'account':'wxid_test','auto_classify':True,
                              'groups':[{'id':'1@chatroom','name':'甲'},{'id':'2@chatroom','name':'乙'}]})
    todos.save(pid, {'title':'每日照片','kind':'scheduled','schedule':{'enabled':True,'times':['18:00'],
               'start_on':'2026-09-27','end_on':'2026-09-27','group_ids':['1@chatroom','2@chatroom']}})
    briefs.configure(pid, True)
    with todos.store.connect() as db:
        db.execute("UPDATE photo_brief_settings SET enabled_from='2026-09-26 00:00'")
    stream=io.BytesIO();Image.new('RGB',(15,15),'blue').save(stream,format='PNG')
    def process(project,key):
        photo=photos.get(project,key);photo.update(state='saved',fields={'kind':'车辆','place':'现场'})
        photos.put(project,photo);return photo
    monkeypatch.setattr(photos,'process',process)
    def read(job,body):
        def ingest(_):
            photo=photos.receive(pid,'shared.png',stream.getvalue(),{'group_name':body['group_id']})
            job['duplicates' if photo.get('duplicate') else 'added']+=1
            return photo
        photos.each(job,[1],ingest)
    monkeypatch.setattr(photos,'read_wechat',read)
    scheduler=PhotoSchedules(todos,photos);now=datetime(2026,9,27,18,tzinfo=TZ)
    scheduler.tick(now)
    import time
    for _ in range(100):
        run=todos.list(pid)[0]['last_run']
        if photos.get_job(pid,run['job_id'])['state']!='running':break
        time.sleep(.01)
    scheduler.tick(now);briefs.pump();scheduler.tick(now);briefs.pump()
    assert len(sent)==1
    text=json.loads(sent[0]['card'])['elements'][0]['text']['content']
    assert '本次新增记录 1 张：已归档 1 张，待核对 0 张' in text and '重复记录 1 条' in text


def test_http_configuration_preview_and_visible_receipt(tmp_path,monkeypatch):
    monkeypatch.setattr('invoice_print_layout.photo_briefs.read_bot_settings',lambda _:FeishuBotSettings('cli_test_app','ou_test'))
    monkeypatch.setattr('invoice_print_layout.photo_briefs.read_bot_secret',lambda _:'test')
    sent=[]
    monkeypatch.setattr(PhotoBriefs,'send',lambda self,item:sent.append(item) or 'om_http')
    server=make_server(tmp_path,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        store=LogisticsStore(tmp_path/'后勤/tasks.sqlite3');pid=store.create_project('HTTP测试')['id']
        base=f'http://127.0.0.1:{server.server_port}'
        def get():
            with urlopen(base+'/api/photos?project='+pid) as response:return json.load(response)
        token=get()['token']
        def post(action,body):
            request=Request(base+'/api/photos/'+action,data=json.dumps({'project_id':pid,**body}).encode(),headers={'Content-Type':'application/json','X-Workbench-Token':token})
            with urlopen(request) as response:return json.load(response)
        assert not get()['briefs']['enabled']
        assert post('brief-settings',{'enabled':True})['enabled']
        post('brief-preview',{})
        import time
        for _ in range(100):
            if (get()['briefs']['latest'] or {}).get('state')=='sent':break
            time.sleep(.01)
        assert get()['briefs']['latest']['label']=='已送达飞书' and len(sent)==1
        post('brief-preview',{})
        assert len(sent)==1
        assert 'ou_test' not in json.dumps(get())
    finally:
        server.shutdown();server.server_close();thread.join(2)
