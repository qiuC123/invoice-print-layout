import hashlib
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pymupdf
import pytest

from invoice_print_layout.photo_review_pdf import build_reviews
from invoice_print_layout.photo_briefs import PhotoBriefs
from invoice_print_layout.feishu_bot import FeishuGateway
from tests.test_photo_briefs import setup, record, row  # noqa: F401


def payload(tmp_path, count=15):
    rows=[]
    for n in range(count):
        buf=io.BytesIO();Image.new('RGB',(400,300),(n*7%255,70,90)).save(buf,format='PNG')
        path=tmp_path/f'{n}.png';path.write_bytes(buf.getvalue())
        rows.append({'id':hashlib.sha256(buf.getvalue()).hexdigest(),'path':str(path),
                     'fields':{'kind':'车辆' if n<12 else '人员','place':'工厂' if n<6 else '现场',
                               'captured_date':'2026-09-26','captured_time':'09:50','label':f'照片{n}'},
                     'sources':[{'chat_id':'test@chatroom'}],'quality':'缓存显示副本'})
    return {'project':{'name':'测试车展'},'rows':rows,'day':'2026-09-26','pending':2,
            'job':{'added':3,'duplicates':12,'issues':[{'message':'private path should not appear'}]},
            'groups':{'test@chatroom':'测试群'},'run_state':'partial'}


def test_compact_pdf_groups_three_columns_watermark_bounds_and_numbering(tmp_path):
    p=payload(tmp_path)
    files=build_reviews(tmp_path/'out',p)
    assert len(files)==1 and files[0]['count']==15 and files[0]['pages']==3
    with pymupdf.open(files[0]['path']) as pdf:
        assert [len(page.get_images()) for page in pdf]==[6,6,3]
        text=''.join(page.get_text() for page in pdf).replace('\xa0',' ')
        assert all(re.search(rf'\b{n:02d}\s', text) for n in range(1,16))
        assert '待核对 2' in text and '本次新增 3' in text and '重复 12' in text
        assert 'private path' not in text
        for page in pdf:
            assert all(pdf.extract_font(font[0])[3] for font in page.get_fonts())
            for image in page.get_images():
                rect=page.get_image_rects(image[0])[0]
                assert 27<=rect.x0<rect.x1<=815 and 110<=rect.y0<rect.y1<510
                assert abs(rect.width/rect.height-4/3)<.01
    assert all(hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()==r['id'] for r in p['rows'])


def test_empty_and_large_batch_split_without_omission(tmp_path):
    p=payload(tmp_path,25)
    files=build_reviews(tmp_path/'large',p)
    assert [f['count'] for f in files]==[24,1]
    assert set(i for f in files for i in f['photo_ids'])=={r['id'] for r in p['rows']}
    p['rows']=[];p['run_state']='failed'
    files=build_reviews(tmp_path/'empty',p)
    with pymupdf.open(files[0]['path']) as pdf:
        assert len(pdf)==1 and not pdf[0].get_images()
        assert '没有可纳入' in pdf[0].get_text()


def test_corrupt_source_never_generates_final_pdf(tmp_path):
    p=payload(tmp_path,1);Path(p['rows'][0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='校验'):
        build_reviews(tmp_path/'out',p)
    assert not list((tmp_path/'out').glob('*.pdf'))


def test_pdf_delivery_resumes_attachment_without_resending_card(setup,monkeypatch):
    briefs,photos,todos,pid,sent=setup
    record(photos,pid);record(photos,pid,'red',saved=False)
    briefs.configure(pid,True,True)
    calls=[]
    class Gateway:
        def __init__(self,client):pass
        def send_card(self,owner,card,key):calls.append(('card',key));return 'om_card'
        def upload_review(self,path):calls.append(('upload',str(path)));return 'file_test'
        def send_review(self,owner,key,delivery):
            calls.append(('file',delivery))
            if len([c for c in calls if c[0]=='file'])==1:raise TimeoutError()
            return 'om_pdf'
    monkeypatch.setattr('invoice_print_layout.feishu_bot.FeishuGateway',Gateway)
    briefs.sender=briefs.send
    key=briefs.preview(pid)
    frozen=json.loads(row(briefs,key)['review'])
    assert len(frozen['rows'])==1 and frozen['pending']==1
    briefs.deliver_one(now=10000)
    state=row(briefs,key)
    assert state['message_id']=='om_card' and state['state']=='retry'
    assert 'PDF尚未' in briefs.view(pid)['latest']['label']
    attachments=json.loads(state['attachments']);assert len(attachments)==1 and attachments[0]['count']==1
    restarted=PhotoBriefs(photos.workspace,todos,photos)
    restarted.deliver_one(now=10061)
    assert row(briefs,key)['state']=='sent' and briefs.view(pid)['latest']['pdf_sent']==1
    assert [c[0] for c in calls]==['card','upload','file','file']
    assert calls[-1][1]==calls[-2][1]
    assert briefs.review_file(pid,key,0).is_file()
    other=todos.store.create_project('其他项目')['id']
    with pytest.raises(ValueError):briefs.review_file(other,key,0)


def test_collection_pdf_includes_duplicates_but_not_uncertain_or_other_project(setup):
    briefs,photos,todos,pid,_=setup
    old=record(photos,pid);new=record(photos,pid,'red',saved=False)
    other=todos.store.create_project('其他项目')['id'];record(photos,other,'blue')
    briefs.configure(pid,True,True)
    job={'id':'manual','project_id':pid,'state':'partial','added':1,'duplicates':1,
         'photo_ids':[old['id'],new['id']],'added_ids':[new['id']],'issues':[]}
    photos.update_job(job)
    with todos.store.connect() as db:db.execute('INSERT INTO photo_brief_jobs VALUES (?,?,?)',(pid,'manual','2026-09-26'))
    assert briefs.view(pid)['preparing']
    briefs.collect_completed();briefs.collect_completed()
    assert not briefs.view(pid)['preparing']
    with todos.store.connect() as db:items=db.execute('SELECT * FROM photo_briefs').fetchall()
    assert len(items)==1
    review=json.loads(items[0]['review'])
    assert review['day']=='2026-09-26' and [r['id'] for r in review['rows']]==[old['id']]
    assert review['pending']==1
    card=json.loads(items[0]['card'])['elements'][0]['text']['content']
    assert '本次新增记录 1' in card and '1张已归档照片' in card


def test_pdf_opt_in_and_empty_report_and_manual_validation(setup):
    briefs,photos,_,pid,_=setup
    briefs.configure(pid,True)
    key=briefs.preview(pid);assert not row(briefs,key)['review']
    briefs.configure(pid,True,True)
    second=briefs.preview(pid);assert second!=key and json.loads(row(briefs,second)['review'])['rows']==[]
    with pytest.raises(ValueError,match='同一天'):briefs.read_and_send(pid,{'start':'2026-09-26','end':'2026-09-27'})
    with pytest.raises(ValueError,match='来源群'):briefs.read_and_send(pid,{'start':'2026-09-26','end':'2026-09-26','group_ids':['foreign']})


def test_gateway_pdf_uses_open_id_and_stable_uuid(tmp_path):
    path=tmp_path/'review.pdf';path.write_bytes(b'%PDF-test')
    uploads=[];messages=[]
    def upload(req):uploads.append(req);return SimpleNamespace(success=lambda:True,data=SimpleNamespace(file_key='file_test'))
    def send(req):messages.append(req);return SimpleNamespace(success=lambda:True,data=SimpleNamespace(message_id='om_file'))
    client=SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(file=SimpleNamespace(create=upload),message=SimpleNamespace(create=send))))
    gateway=FeishuGateway(client)
    assert gateway.upload_review(path)=='file_test'
    assert gateway.send_review('ou_owner','file_test','fixed_uuid')=='om_file'
    assert uploads[0].request_body.file_type=='pdf'
    assert messages[0].receive_id_type=='open_id'
    assert messages[0].request_body.uuid=='fixed_uuid' and messages[0].request_body.receive_id=='ou_owner'
