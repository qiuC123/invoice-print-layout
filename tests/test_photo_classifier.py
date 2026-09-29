import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from invoice_print_layout import photo_classifier as classifier
from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.site_photos import SitePhotos

SCHEDULE = {'timezone': 'Asia/Shanghai', 'phases': [
    {'key': 'build', 'name': '搭建', 'starts_on': '2026-09-26', 'ends_on': '2026-09-30'},
    {'key': 'exhibition', 'name': '展期', 'starts_on': '2026-10-01', 'ends_at': '2026-10-04T17:00:00+08:00'},
    {'key': 'dismantle', 'name': '撤展', 'starts_at': '2026-10-04T17:00:00+08:00', 'ends_on': '2026-10-05'},
]}


def decision(project='belongs', kind='vehicle', place='site', confidence=.99):
    return {'model': classifier.MODEL, 'answers': {k: {'type': 'choice', 'choice': v, 'confidence': confidence}
        for k, v in [('project', project), ('kind', kind), ('place', place)]}}


def picture(color='blue'):
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), color).save(stream, format='PNG')
    return stream.getvalue()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    logistics = LogisticsStore(tmp_path / '后勤/tasks.sqlite3')
    project = logistics.create_project('测试南昌车展')
    project['schedule'] = SCHEDULE
    with logistics.connect() as db:
        db.execute('UPDATE projects SET payload=? WHERE id=?', (json.dumps(project), project['id']))
    manager = SitePhotos(tmp_path, logistics)
    manager.save_settings(project['id'], {'auto_classify': True, 'project_location': '南昌'})
    monkeypatch.setattr('invoice_print_layout.site_photos.read_receipt', lambda *a: [])
    monkeypatch.setattr('invoice_print_layout.site_photos.rows_of', lambda *a:
        ['施工内容：测试南昌车展', '拍摄时间：2026.09.26 09:54', '地点：南昌国际博览中心', '赣A·A0001'])
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda evidence: decision())
    return manager, project['id']


@pytest.mark.parametrize('day,clock,phase', [
    ('2026-09-24', '', '前期准备'), ('2026-09-26', '', '搭建'), ('2026-09-30', '', '搭建'),
    ('2026-10-01', '', '展期'), ('2026-10-04', '16:59', '展期'), ('2026-10-04', '17:00', '撤展'),
    ('2026-10-04', '', None), ('2026-10-05', '', '撤展'), ('2026-10-06', '', None),
    ('', '', None), ('2026-09-26', '25:00', None),
])
def test_schedule_boundaries(day, clock, phase):
    assert classifier.phase_for({'schedule': SCHEDULE}, {'captured_date': day, 'captured_time': clock}).get('name') == phase


def test_receive_runs_entire_pipeline_and_preserves_bytes(setup, monkeypatch):
    manager, pid = setup
    seen = []
    def classify(evidence):
        seen.append(evidence)
        return decision()
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', classify)
    payload = picture()
    row = manager.receive(pid, 'photo.png', payload, {'type': '上传', 'path': 'secret-local-path', 'account': 'secret-account'})
    assert row['state'] == 'saved' and row['saved_by'] == 'automatic'
    assert row['phase']['name'] == '搭建'
    path = Path(row['saved_path'])
    assert '车辆/进场/赣AA0001-2026-09-26/现场' in path.as_posix()
    assert path.read_bytes() == payload == manager.file(pid, row['id']).read_bytes()
    assert 'secret-' not in json.dumps(seen)
    assert manager.receive(pid, 'copy.png', payload)['duplicate']
    assert len(seen) == 1


@pytest.mark.parametrize('answer', [decision(project='other'), decision(project='unknown'),
    decision(confidence=.6), decision(kind='unknown'), decision(place='unknown')])
def test_uncertain_or_other_project_stays_pending(setup, monkeypatch, answer):
    manager, pid = setup
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda _: answer)
    row = manager.receive(pid, 'photo.png', picture())
    assert row['state'] == 'pending' and not row['saved_path'] and row['warnings']
    assert not Path(manager.settings(pid)['output_root']).exists()


def test_api_failure_retains_original_and_explicit_retry_saves(setup, monkeypatch):
    manager, pid = setup
    def failed(_):
        raise ValueError('Jev暂时不可用')
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', failed)
    row = manager.receive(pid, 'photo.png', picture())
    assert row['state'] == 'pending' and 'Jev' in row['warnings'][0]
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda _: decision())
    row = manager.process(pid, row['id'])
    assert row['state'] == 'saved'


def test_manual_correction_wins_during_remote_request(setup, monkeypatch):
    manager, pid = setup
    row = manager.receive(pid, 'photo.png', picture(), automatic=False)
    fields = {'kind': '人员', 'movement': '进场', 'captured_date': '2026-09-26', 'label': '人工修正'}
    def classify(_):
        manager.save(pid, row['id'], row['revision'], fields, True)
        return decision()
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', classify)
    result = manager.process(pid, row['id'])
    assert result['fields']['kind'] == '人员' and result['saved_by'] == 'manual'


def test_factory_uses_unique_known_trip_not_capture_date(setup, monkeypatch):
    manager, pid = setup
    site = manager.receive(pid, 'site.png', picture())
    monkeypatch.setattr('invoice_print_layout.site_photos.rows_of', lambda _: ['2026.09.24', '赣AA0001', '地点：太仓工厂'])
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda e: decision(place='factory'))
    factory = manager.receive(pid, 'factory.png', picture('green'))
    assert factory['fields']['captured_date'] == '2026-09-24'
    assert factory['fields']['trip_date'] == site['fields']['trip_date'] == '2026-09-26'
    assert factory['phase']['name'] == '前期准备' and factory['state'] == 'saved'


def test_factory_unknown_trip_not_guessed(setup, monkeypatch):
    manager, pid = setup
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda _: decision(place='factory'))
    row = manager.receive(pid, 'factory.png', picture())
    assert row['state'] == 'pending' and '批次' in ' '.join(row['warnings'])


def test_folder_automatic_people_and_project_opt_out(setup, monkeypatch, tmp_path):
    manager, pid = setup
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda _: decision(kind='people'))
    folder = tmp_path / 'input'
    folder.mkdir()
    (folder / 'a.png').write_bytes(picture())
    job = {'id': 'auto-folder', 'project_id': pid, 'state': 'running', 'issues': [], 'added': 0, 'duplicates': 0}
    manager.read_folder(job, str(folder))
    row = manager.snapshot(pid)['photos'][0]
    assert row['state'] == 'saved' and '/人员/进场/2026-09-26/' in Path(row['saved_path']).as_posix()
    assert not row['fields']['person_name'] and not row['fields']['person_role']
    manager.save_settings(pid, {'auto_classify': False})
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda _: pytest.fail('cloud must be disabled'))
    assert manager.receive(pid, 'offline.png', picture('green'))['state'] == 'pending'


@pytest.mark.parametrize('confidence', [True, -1, 1.5, float('nan'), '0.99'])
def test_bad_remote_response_rejected(monkeypatch, confidence):
    monkeypatch.setattr(classifier, 'api_key', lambda: 'test-only')
    monkeypatch.setattr(classifier.urllib.request, 'build_opener', lambda *a: SimpleNamespace(
        open=lambda *a, **kw: io.BytesIO(json.dumps(decision(confidence=confidence)).encode())))
    with pytest.raises(ValueError, match='无效'):
        classifier.classify({'ocr_text': 'test'})


def test_remote_request_contract(monkeypatch):
    monkeypatch.setattr(classifier, 'api_key', lambda: 'test-only')
    def send(request, timeout):
        assert request.full_url == 'https://api.typesafe.ai/v1/systemone' and timeout == 20
        payload = json.loads(request.data)
        assert payload['state'] == {'ocr_text': 'test'} and set(payload['questions']) == {'project', 'kind', 'place'}
        return io.BytesIO(json.dumps(decision()).encode())
    monkeypatch.setattr(classifier.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=send))
    assert classifier.classify({'ocr_text': 'test'})['model'] == classifier.MODEL


def test_disable_during_classification_prevents_export(setup, monkeypatch):
    manager, pid = setup
    def classify(_):
        manager.save_settings(pid, {'auto_classify': False})
        return decision()
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', classify)
    row = manager.receive(pid, 'photo.png', picture())
    assert row['state'] == 'pending' and not row['saved_path']
    assert '设置已变化' in row['warnings'][0]


def test_wechat_worker_result_also_runs_automatic_pipeline(setup, monkeypatch, tmp_path):
    import hashlib
    manager, pid = setup
    manager.save_settings(pid, {'account': 'wxid_test', 'groups': [{'id': '123@chatroom', 'name': '南昌现场群'}]})
    (tmp_path / 'photo-reader.json').write_text(json.dumps({'python': 'test-python', 'helper_dir': 'test-dir',
        'audited_source': 'test-source', 'helper_hashes': {'a': 'b'}, 'media_sha256': 'test'}))
    def reader(args, **kwargs):
        directory = Path(args[-1]).parent
        payload = picture()
        (directory / 'test.png').write_bytes(payload)
        (directory / 'result.json').write_text(json.dumps({'photos': [{'file': 'test.png',
            'sha256': hashlib.sha256(payload).hexdigest(), 'quality': '测试原件',
            'source': {'chat_id': '123@chatroom', 'message_id': 'test-message', 'sent_at': '2026-09-27T09:00:00+08:00'}}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('invoice_print_layout.site_photos.subprocess.run', reader)
    job = {'id': 'wechat-auto', 'project_id': pid, 'state': 'running', 'issues': [], 'added': 0, 'duplicates': 0}
    manager.read_wechat(job, {'group_id': '123@chatroom', 'start': '2026-09-27', 'end': '2026-09-27'})
    photo = manager.snapshot(pid)['photos'][0]
    assert photo['state'] == 'saved' and job['added'] == 1 and not job['issues']
    assert photo['fields']['captured_date'] == '2026-09-26'  # Not the message date.


def test_empty_ocr_keeps_original_without_calling_jev(setup, monkeypatch):
    manager, pid = setup
    monkeypatch.setattr('invoice_print_layout.site_photos.rows_of', lambda _: [])
    monkeypatch.setattr('invoice_print_layout.site_photos.classify', lambda _: pytest.fail('empty text'))
    row = manager.receive(pid, 'empty.png', picture())
    assert row['state'] == 'pending' and manager.file(pid, row['id']).read_bytes() == picture()


def test_classified_copy_failure_is_visible_and_retryable(setup, monkeypatch):
    manager, pid = setup
    save = manager.save
    def unavailable(*a, **kw):
        raise OSError('disk not available')
    monkeypatch.setattr(manager, 'save', unavailable)
    row = manager.receive(pid, 'photo.png', picture())
    assert row['state'] == 'pending' and row['warnings']
    monkeypatch.setattr(manager, 'save', save)
    assert manager.process(pid, row['id'])['state'] == 'saved'
