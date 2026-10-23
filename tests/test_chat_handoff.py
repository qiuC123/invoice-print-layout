from __future__ import annotations
import copy
import json
from pathlib import Path

from PIL import Image
import pytest
from typer.testing import CliRunner

from invoice_print_layout import chat_handoff as handoff
from invoice_print_layout.chat_handoff_complete import complete
from invoice_print_layout import workbuddy_desktop_handoff as desktop
from invoice_print_layout.construction_reports import sha, write_json
from invoice_print_layout.cli import app


@pytest.fixture
def source(tmp_path):
    root = tmp_path/'source'
    root.mkdir()
    messages = [dict(chat_id='test@chatroom', message_id=str(i), local_id=i,
        shard='message.db', sent_at=f'2026-10-02T08:00:0{i}+08:00', sort_seq=i,
        type=1 if i==1 else 3, sender='synthetic', text='照片2标注为项目经理' if i==1 else '[图片]') for i in range(1,4)]
    image = root/'photo.png'
    Image.new('RGB', (320,400), '#abcdef').save(image)
    photos = [dict(path=str(image), sha256=sha(image), quality='synthetic fixture', source=messages[1])]
    value = dict(schema_version=1, state='partial', source_id='test', chat_id='test@chatroom',
        window=dict(start='2026-10-02T00:00:00+08:00', end_exclusive='2026-10-03T00:00:00+08:00'),
        messages=messages, photos=photos, issues=[dict(code='photos_missing',message='missing sample')],
        attempt_dir=str(root))
    write_json(root/'messages.json', messages)
    write_json(root/'result.json', value)
    write_json(root/'request.json', {'test':True})
    value['file_hashes'] = {p.name:sha(p) for p in root.iterdir()}
    write_json(root/'collection.json', value)
    return root/'collection.json'


@pytest.fixture
def package(source, tmp_path):
    folder = tmp_path/'package'
    handoff.prepare_chat([source], folder, 'test', '合成项目', 'batch')
    return folder


def result(folder):
    request = handoff.verify_inputs(folder)
    return dict(schema_version=1, task_id=request['task_id'], nonce=request['nonce'],
        request_sha256=sha(folder/'request.json'), records=[dict(photo_id=r['photo_id'],
        kind='personnel' if r['availability']=='available' else 'uncertain',
        status='classified' if r['availability']=='available' else 'pending',
        basis='合成用途说明', role='待确认', role_basis_type='unavailable',
        role_basis='缺少明确文字', evidence_message_ids=[]) for r in request['records']])


def finish(folder):
    write_json(folder/'output/result.json', result(folder))
    complete(folder)


def test_package_carries_all_messages_missing_photos_and_null_capture(package, source):
    request=handoff.verify_inputs(package)
    assert len(json.loads((package/'inputs/messages.json').read_text(encoding='utf-8')))==3
    assert [r['availability'] for r in request['records']]==['available','missing']
    assert all(r['captured_at'] is None for r in request['records'])
    assert sha(package/request['records'][0]['path'])==sha(source.parent/'photo.png')
    with pytest.raises(ValueError,match='已存在'):
        handoff.prepare_chat([source],package,'test','合成项目','batch')


def test_source_corruption_stops_before_destination(source,tmp_path):
    (source.parent/'photo.png').write_bytes(b'changed')
    with pytest.raises(ValueError):
        handoff.prepare_chat([source],tmp_path/'new','test','test','new')
    assert not (tmp_path/'new').exists()


def test_receive_waits_seals_preserves_issues_and_reuses(package):
    assert handoff.receive_chat(package)['state']=='waiting'
    write_json(package/'output/result.json', result(package))
    assert handoff.receive_chat(package)['state']=='waiting'
    complete(package)
    received=handoff.receive_chat(package)
    assert received['state']=='received' and received['missing']==1
    assert received['source_issue_count']==1
    assert handoff.receive_chat(package)['reused'] is True
    data=json.loads((package/'codex/intake.json').read_text(encoding='utf-8'))
    assert all(r['project_status']=='pending' for r in data['records'])
    (package/'codex/preview.html').write_text('tampered')
    with pytest.raises(ValueError,match='产物'):
        handoff.receive_chat(package)


@pytest.mark.parametrize('mutation', ['nonce','hash','missing','duplicate','unknown_id','extra','missing_classified','role_without_text','outside_message','bad_status'])
def test_bad_results_rejected(package,mutation):
    data=result(package)
    if mutation=='nonce': data['nonce']='other'
    elif mutation=='hash': data['request_sha256']='0'*64
    elif mutation=='missing': data['records'].pop()
    elif mutation=='duplicate': data['records'][1]=copy.deepcopy(data['records'][0])
    elif mutation=='unknown_id': data['records'][0]['photo_id']='alien'
    elif mutation=='extra': data['records'][0]['name']='not requested'
    elif mutation=='missing_classified': data['records'][1].update(kind='construction',status='classified')
    elif mutation=='role_without_text': data['records'][0].update(role='AV搭建工人',role_basis_type='explicit_text',evidence_message_ids=['1'])
    elif mutation=='outside_message': data['records'][0]['evidence_message_ids']=['alien']
    elif mutation=='bad_status': data['records'][0]['status']='pending'
    with pytest.raises(ValueError): handoff.validate_result(package,data)


def test_explicit_role_and_input_change(package):
    data=result(package)
    data['records'][0].update(role='项目经理',role_basis_type='explicit_text',role_basis='消息1明确标注',evidence_message_ids=['1'])
    handoff.validate_result(package,data)
    (package/'README.md').write_text('changed')
    with pytest.raises(ValueError,match='输入'):
        handoff.validate_result(package,data)


def test_result_change_after_complete_rejected(package):
    finish(package)
    data=result(package)
    data['records'][0]['basis']='changed'
    write_json(package/'output/result.json',data)
    with pytest.raises(ValueError): complete(package)
    with pytest.raises(ValueError): handoff.receive_chat(package)


class Driver:
    calls=[]
    failure=None
    def __init__(self,config,folder): self.folder=folder
    def prepare(self,prompt):
        self.calls.append('prepare')
        assert '不做人脸识别' in prompt
        if self.failure=='prepare': raise ValueError('not ready')
    def submit(self):
        self.calls.append('send')
        assert json.loads((self.folder/'state.json').read_text(encoding='utf-8'))['state']=='sending'
        if self.failure=='send': raise ValueError('timeout')
    def close(self): self.calls.append('close')


@pytest.mark.parametrize('failure,expected',[(None,'waiting'),('prepare','preparation_failed'),('send','dispatch_unknown')])
def test_dispatch_no_resend_including_uncertain(package,tmp_path,failure,expected):
    cfg=tmp_path/'desktop.json'
    exe=tmp_path/'dummy.exe'; exe.touch()
    write_json(cfg,{'nuphus_executable':str(exe)})
    Driver.calls=[]; Driver.failure=failure
    value=desktop.dispatch_chat(package,cfg,driver_factory=Driver)
    assert value['state']==expected
    calls=list(Driver.calls)
    assert desktop.dispatch_chat(package,cfg,driver_factory=Driver)['reused']
    assert Driver.calls==calls


def test_cli_pack_and_receive(source,tmp_path):
    folder=tmp_path/'cli'
    runner=CliRunner()
    value=runner.invoke(app,['daily','chat-pack','--receipt',str(source),'--destination',str(folder),'--project','test','--title','测试','--batch','test'])
    assert value.exit_code==0,value.output
    assert runner.invoke(app,['daily','receive-chat','--package',str(folder)]).exit_code==2
    finish(folder)
    value=runner.invoke(app,['daily','receive-chat','--package',str(folder)])
    assert value.exit_code==0,value.output


def test_wait_bound_and_no_gui_on_receive(package):
    with pytest.raises(ValueError): desktop.poll_chat(package,61)
    assert desktop.poll_chat(package,0)['state']=='waiting'


def test_confirmed_manual_supplement_preserved(source,tmp_path):
    src=json.loads(source.read_text(encoding='utf-8'))
    p=tmp_path/'manual.json'
    photo=dict(path=str(source.parent/'photo.png'),sha256=sha(source.parent/'photo.png'),
               source=src['messages'][2],user_confirmation=dict(status='confirmed',basis='合成用户确认第3条'))
    write_json(p,{'photos':[photo]})
    out=tmp_path/'completed'
    value=handoff.prepare_chat([source],out,'test','test','test',supplements=p)
    assert value['missing']==0 and value['source_issue_count']==1
    record=handoff.verify_inputs(out)['records'][1]
    assert record['acquisition']=='user_confirmed_supplement'
    assert record['user_confirmation']['basis']=='合成用户确认第3条'


@pytest.mark.parametrize('problem',['no_confirmation','wrong_message','already_present','wrong_hash'])
def test_manual_supplement_rejects_unconfirmed_and_conflicts(source,tmp_path,problem):
    src=json.loads(source.read_text(encoding='utf-8'))
    photo=dict(path=str(source.parent/'photo.png'),sha256=sha(source.parent/'photo.png'),
               source=copy.deepcopy(src['messages'][2]),user_confirmation=dict(status='confirmed',basis='合成确认'))
    if problem=='no_confirmation': photo.pop('user_confirmation')
    elif problem=='wrong_message': photo['source']['message_id']='wrong'
    elif problem=='already_present': photo['source']=src['messages'][1]
    else: photo['sha256']='0'*64
    p=tmp_path/'manual.json';write_json(p,{'photos':[photo]})
    with pytest.raises(ValueError): handoff.prepare_chat([source],tmp_path/'out','test','test','test',supplements=p)


def test_receipt_pins_dispatched_request(package):
    finish(package)
    write_json(package/'bridge/state.json',{'request_sha256':'changed'})
    with pytest.raises(ValueError,match='派发前'): handoff.receive_chat(package)


def test_repeated_cli_dispatch_after_receive_is_success_without_gui(package,tmp_path):
    write_json(package/'bridge/state.json',{'request_sha256':sha(package/'request.json'),'state':'waiting'})
    finish(package)
    handoff.receive_chat(package)
    value=CliRunner().invoke(app,['daily','dispatch-chat','--package',str(package),'--desktop-config',str(tmp_path/'unused.json')])
    assert value.exit_code==0,value.output
    assert json.loads(value.output)['state']=='received'
    assert json.loads(value.output)['reused'] is True
