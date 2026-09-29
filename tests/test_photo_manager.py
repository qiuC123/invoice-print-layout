import base64
import hashlib
import io
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.site_photos import SitePhotos, hints
from invoice_print_layout.workbench_web import make_server


def picture(color='green'):
    stream = io.BytesIO()
    Image.new('RGB', (30, 30), color).save(stream, format='PNG')
    return stream.getvalue()


@pytest.fixture
def setup(tmp_path):
    logistics = LogisticsStore(tmp_path / '后勤/tasks.sqlite3')
    first, other = [logistics.create_project(name)['id'] for name in ['测试甲', '测试乙']]
    manager = SitePhotos(tmp_path, logistics)
    manager.save_settings(first, {'output_root': str(tmp_path / '用户照片')})
    return manager, first, other


def fields(**changes):
    return {'kind': '车辆', 'movement': '进场', 'place': '工厂', 'captured_date': '2026-09-24',
            'trip_date': '2026-09-26', 'plate': '测试A12345', 'label': '车头', **changes}


def test_receive_dedup_and_project_boundary(setup):
    manager, first, other = setup
    payload = picture()
    row = manager.receive(first, '测试.png', payload)
    repeated = manager.receive(first, '另一个名字.png', payload)
    assert repeated['duplicate'] is True
    assert len(repeated['sources']) == 2
    assert len(manager.snapshot(first)['photos']) == 1
    assert manager.snapshot(other)['photos'] == []
    with pytest.raises(ValueError):
        manager.file(other, row['id'])
    assert manager.file(first, row['id']).read_bytes() == payload


def test_save_correction_preserves_original_and_cleans_owned_copy(setup):
    manager, first, _ = setup
    payload = picture()
    row = manager.receive(first, '测试.png', payload)
    saved = manager.save(first, row['id'], 1, fields(), True)
    old = Path(saved['saved_path'])
    assert '测试A12345-2026-09-26' in str(old)
    assert '2026-09-24' in old.name and old.parent.name == '工厂'
    assert old.read_bytes() == payload
    corrected = manager.save(first, row['id'], saved['revision'], fields(place='现场'), True)
    assert Path(corrected['saved_path']).parent.name == '现场'
    assert not old.exists()
    assert manager.file(first, row['id']).read_bytes() == payload
    with pytest.raises(ValueError, match='更新'):
        manager.save(first, row['id'], 1, fields(), True)
    assert len(corrected['history']) == 2


def test_modified_old_copy_is_never_deleted(setup):
    manager, first, _ = setup
    row = manager.receive(first, '测试.png', picture())
    saved = manager.save(first, row['id'], 1, fields(), True)
    old = Path(saved['saved_path'])
    old.write_bytes(b'user edited')
    manager.save(first, row['id'], saved['revision'], fields(place='现场'), True)
    assert old.read_bytes() == b'user edited'


@pytest.mark.parametrize('change', [{'plate': '../escape'}, {'label': 'CON'}, {'captured_date': '2026-02-30'}, {'place': '其他'}, {'kind': ''}])
def test_invalid_classification_does_not_export(setup, change):
    manager, first, _ = setup
    row = manager.receive(first, '测试.png', picture())
    with pytest.raises(ValueError):
        manager.save(first, row['id'], 1, fields(**change), True)
    assert manager.get(first, row['id'])['state'] == 'pending'
    assert not Path(manager.settings(first)['output_root']).exists()


def test_confirmation_and_existing_target_protection(setup):
    manager, first, _ = setup
    row = manager.receive(first, '测试.png', picture())
    with pytest.raises(ValueError, match='核对'):
        manager.save(first, row['id'], 1, fields(), False)
    saved = manager.save(first, row['id'], 1, fields(), True)
    target = Path(saved['saved_path'])
    target.write_bytes(b'different user file')
    with pytest.raises(ValueError, match='未覆盖'):
        manager.save(first, row['id'], saved['revision'], saved['fields'], True)
    assert target.read_bytes() == b'different user file'


def test_recursive_folder_dedup_and_per_file_failure(setup, tmp_path):
    manager, first, _ = setup
    folder = tmp_path / '导入'
    (folder / 'sub').mkdir(parents=True)
    (folder / 'a.png').write_bytes(picture())
    (folder / 'sub/a-copy.png').write_bytes(picture())
    (folder / 'sub/bad.jpg').write_bytes(b'broken')
    job = {'id': 'test', 'project_id': first, 'state': 'running', 'issues': [], 'added': 0, 'duplicates': 0}
    manager.read_folder(job, str(folder))
    assert job['added'] == 1 and job['duplicates'] == 1 and len(job['issues']) == 1
    assert (folder / 'a.png').exists()


def test_ocr_ambiguous_dates_and_plates_not_guessed():
    result = hints('拍摄时间：2026.09.24 10:31\n地点：太仓市\n赣A·A0001')
    assert result['captured_date'] == '2026-09-24'
    assert result['captured_time'] == '10:31'
    assert result['plate'] == '赣AA0001'
    assert result.get('place') is None
    ambiguous = hints('2026.09.24 2026.09.26 赣AA0001 赣CB0002')
    assert 'plate' not in ambiguous and 'captured_date' not in ambiguous


def test_ocr_does_not_overwrite_reviewed_fields(setup, monkeypatch):
    manager, first, _ = setup
    row = manager.receive(first, '测试.png', picture())
    saved = manager.save(first, row['id'], 1, fields(), True)
    monkeypatch.setattr('invoice_print_layout.site_photos.read_receipt', lambda *args: [])
    monkeypatch.setattr('invoice_print_layout.site_photos.rows_of', lambda lines: ['2026.09.25 赣CB0002'])
    analyzed = manager.analyze(first, row['id'])
    assert analyzed['fields'] == saved['fields']
    assert analyzed['suggestions']['captured_date'] == '2026-09-25'
    assert analyzed['revision'] > saved['revision']


def test_people_save_does_not_require_plate_or_infer_identity(setup):
    manager, first, _ = setup
    row = manager.receive(first, '测试.png', picture())
    saved = manager.save(first, row['id'], 1, fields(kind='人员', plate='', visible_count='5', label='合影'), True)
    assert Path(saved['saved_path']).parent.name == '2026-09-24'
    assert saved['fields']['person_name'] == saved['fields']['person_role'] == ''


def test_legacy_migration_keeps_saved_path_and_quality(setup):
    manager, first, _ = setup
    folder = manager.root / first
    folder.mkdir()
    (folder / 'a.png').write_bytes(picture())
    destination = Path(manager.settings(first)['output_root']) / 'existing.png'
    destination.parent.mkdir()
    destination.write_bytes(picture())
    (folder / '照片台账.json').write_text(json.dumps({'project_id': first, 'photos': [{
        'relative_path': 'a.png', 'sha256': hashlib.sha256(picture()).hexdigest(),
        'captured_at_claim': '2026-09-26T09:48:00+08:00', 'category': '人员/进场',
        'user_photo_path': str(destination), 'format_note': '普通缓存'}]}), encoding='utf-8')
    first_snapshot = manager.snapshot(first)
    assert first_snapshot['photos'][0]['state'] == 'saved'
    assert first_snapshot['photos'][0]['quality'] == '普通缓存'
    assert len(manager.snapshot(first)['photos']) == 1


def test_running_job_recovers_as_interrupted(setup):
    manager, first, _ = setup
    manager.update_job({'id': 'interrupted', 'project_id': first, 'state': 'running'})
    recreated = SitePhotos(manager.workspace, manager.logistics)
    assert recreated.snapshot(first)['jobs'][0]['state'] == 'interrupted'


def test_wechat_unconfigured_and_wrong_group_fail_before_subprocess(setup, monkeypatch):
    manager, first, _ = setup
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: pytest.fail('must not launch'))
    with pytest.raises(ValueError, match='来源群'):
        manager.read_wechat({'project_id': first}, {'group_id': 'other@chatroom'})
    manager.save_settings(first, {'account': 'wxid_test', 'groups': [{'id': '123@chatroom', 'name': '测试'}]})
    with pytest.raises(ValueError, match='尚未配置'):
        manager.read_wechat({'project_id': first}, {'group_id': '123@chatroom', 'start': '2026-09-26', 'end': '2026-09-26'})


def test_photo_http_token_project_isolation_and_image_roundtrip(tmp_path):
    server = make_server(tmp_path, 0)
    logistics = LogisticsStore(tmp_path / '后勤/tasks.sqlite3')
    first, other = [logistics.create_project(n)['id'] for n in ['甲', '乙']]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/photos?project=' + first) as response:
            token = json.load(response)['token']
        payload = json.dumps({'project_id': first, 'name': 'test.png', 'data': base64.b64encode(picture()).decode()}).encode()
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + '/api/photos/receive', data=payload, headers={'Content-Type': 'application/json'}))
        assert error.value.code == 403
        request = Request(base + '/api/photos/receive', data=payload, headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})
        with urlopen(request) as response:
            key = json.load(response)['id']
        with urlopen(base + '/photo-file/' + first + '/' + key) as response:
            assert response.read() == picture()
        with pytest.raises(HTTPError) as error:
            urlopen(base + '/photo-file/' + other + '/' + key)
        assert error.value.code == 404
        with urlopen(base + '/api/photos?project=' + other) as response:
            assert json.load(response)['photos'] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
