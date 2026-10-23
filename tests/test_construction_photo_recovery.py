from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from PIL import Image
import numpy as np
import pytest

from invoice_print_layout import construction_photo_recovery as recovery
from invoice_print_layout import construction_report_collect as collection
from invoice_print_layout.wechat_photo_desktop import header_matches, verify_filename, locate_thumbnail, image_info, local_result_heading
from tests.test_construction_report_collect import environment, DAY, dump


@pytest.fixture
def setup(environment, monkeypatch):
    env = environment
    with sqlite3.connect(env.db_paths['contact.db']) as db:
        db.execute('ALTER TABLE contact ADD COLUMN nick_name TEXT')
        db.execute('ALTER TABLE contact ADD COLUMN remark TEXT')
        db.execute('UPDATE contact SET nick_name=? WHERE username=?', ('测试群', env.config['chat_id']))
    executable = env.root/'nuphus.exe'
    executable.write_bytes(b'synthetic')
    settings = env.root/'desktop.json'
    dump(settings, {'nuphus_executable': str(executable), 'max_photos': 2, 'wait_seconds': 1})
    monkeypatch.setenv('LOCALAPPDATA', str(env.root))
    monkeypatch.setattr(recovery.time, 'sleep', lambda _: None)
    env.settings = settings
    env.mode = 'enter-cache'
    env.driver_calls = []
    env.folder = env.root/'wechat-account/msg/attach'/hashlib.md5(env.config['chat_id'].encode()).hexdigest()/'2026-09/Img'
    env.folder.mkdir(parents=True)

    class Driver:
        def __init__(self, config, profile, folder):
            env.driver_calls.append('init')
            self.folder = folder

        def enter_chat(self):
            env.driver_calls.append('enter')
            if env.mode == 'enter-cache':
                (env.folder/('0'*31+'1.dat')).write_bytes(b'now-cached')
            if env.mode == 'wrong-chat':
                raise ValueError('会话标题不一致')

        def recover_photo(self, target, thumbnail):
            env.driver_calls.append(('photo', target['local_id']))
            image = self.folder/(str(target['local_id'])+'.jpg')
            Image.new('RGB', (800, 600), 'blue').save(image)
            suffix = 9000+target['local_id']
            stamp = target['sent_at'].split('+')[0].replace('-', '').replace(':', '').replace('T', '')
            info = {**image_info(image), 'source': dict(target), 'quality': 'GUI另存',
                    'verified_chat_id': target['chat_id'], 'acquisition': 'wechat_gui_save',
                    'default_filename': f'微信图片_{stamp}_{target["local_id"]}_{suffix}.jpg'}
            if env.mode == 'wrong-source':
                info['source']['message_id'] = 'other'
            return info

        def close(self):
            env.driver_calls.append('close')

    monkeypatch.setattr(recovery, 'DesktopRecovery', Driver)
    return env


def invoke(env, **kw):
    return recovery.collect_recovering(env.config, DAY, env.root/'out', env.settings, **kw)


def add_missing(env, number=1, thumbnail=True):
    env.add(number, cached=False)
    if thumbnail:
        (env.folder/(f'{number:032x}'+'_t.dat')).write_bytes(b'synthetic-thumb')


def test_complete_cache_never_opens_desktop(setup):
    env = setup
    env.add(1)
    result = invoke(env)
    assert result['state'] == 'done' and not env.driver_calls


def test_enter_chat_then_recollect_without_individual_clicks(setup):
    env = setup
    add_missing(env)
    result = invoke(env)
    assert result['state'] == 'done' and result['remaining_missing_count'] == 0
    assert env.driver_calls == ['init', 'enter', 'close']
    assert len(result['recovery_receipts']) == 2
    old = json.loads(Path(result['recovery_receipts'][0]).read_text(encoding='utf-8'))
    assert old['state'] == 'partial' and not old['photos']


def test_gui_save_integrates_and_repeat_verifies_all_evidence(setup):
    env = setup
    env.mode = 'save'
    add_missing(env)
    result = invoke(env)
    assert result['state'] == 'done' and len(result['photos']) == 1
    assert env.driver_calls == ['init', 'enter', ('photo', 1), 'close']
    original = Path(result['receipt_path']).read_bytes()
    again = invoke(env)
    assert again['reused'] and Path(result['receipt_path']).read_bytes() == original
    assert len(env.driver_calls) == 4
    Path(result['photos'][0]['path']).write_bytes(b'changed')
    third = invoke(env)
    assert not third['reused'] and third['state'] == 'done'
    assert len(env.driver_calls) == 8


@pytest.mark.parametrize('mode', ['wrong-chat', 'wrong-source'])
def test_wrong_chat_or_photo_never_completes(setup, mode):
    env = setup
    env.mode = mode
    add_missing(env)
    result = invoke(env)
    assert result['state'] == 'partial' and result['remaining_missing_count'] == 1
    assert not result['recovery_saved'] and env.driver_calls[-1] == 'close'


def test_no_thumbnail_or_limit_keeps_explicit_missing_items(setup):
    env = setup
    env.mode = 'save'
    add_missing(env, 1, thumbnail=False)
    add_missing(env, 2)
    add_missing(env, 3)
    result = invoke(env)
    assert result['state'] == 'partial' and result['remaining_missing_count'] == 2
    assert ('photo', 2) in env.driver_calls and ('photo', 3) not in env.driver_calls
    assert any(i['code'] == 'thumbnail_unavailable' for i in result['issues'])


def test_ambiguous_title_does_not_open_desktop(setup):
    env = setup
    add_missing(env)
    with sqlite3.connect(env.db_paths['contact.db']) as db:
        db.execute('UPDATE contact SET nick_name=?', ('测试群',))
    result = invoke(env)
    assert result['state'] == 'partial' and not env.driver_calls


def test_text_error_is_not_removed_by_recovered_photo(setup):
    env = setup
    env.mode = 'save'
    add_missing(env)
    env.add(2, kind=1, text='')
    result = invoke(env)
    assert result['state'] == 'partial' and result['remaining_missing_count'] == 0
    assert any('文本消息' in i['message'] for i in result['issues'])


def test_after_save_reader_failure_keeps_saved_photo_and_failure(setup, monkeypatch):
    env = setup
    env.mode = 'save'
    add_missing(env)
    real = recovery.cache.collect
    calls = 0
    def collect(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = real(*args, **kwargs)
        if calls == 3:
            return {**result, 'state': 'failed', 'messages': [], 'photos': []}
        return result
    monkeypatch.setattr(recovery.cache, 'collect', collect)
    result = invoke(env)
    assert result['state'] == 'partial' and len(result['photos']) == 1
    assert any(i['code'] == 'recovery_stopped' for i in result['issues'])


def test_corrupted_aggregate_receipt_not_reused(setup):
    env = setup
    env.mode = 'save'
    add_missing(env)
    result = invoke(env)
    Path(result['receipt_path']).write_text('{broken', encoding='utf-8')
    fresh = invoke(env)
    assert fresh['state'] == 'done' and not fresh['reused']
    assert fresh['receipt_path'] != result['receipt_path']


def test_reader_failure_does_not_start_desktop(setup):
    env = setup
    env.reader.unkeyed = ['unavailable']
    result = invoke(env)
    assert result['state'] == 'failed' and not env.driver_calls


def test_refresh_does_not_reuse_gui_aggregate(setup):
    env = setup
    env.mode = 'save'
    add_missing(env)
    first = invoke(env)
    second = invoke(env, refresh=True)
    assert first['state'] == second['state'] == 'done'
    assert not second['reused'] and len(env.driver_calls) == 8


@pytest.mark.parametrize('name', ['微信图片_20261003000024_23_172.jpg',
    '微信图片_20261003000025_24_23.jpg', '微信图片_20261003000025_23_172.jpg.exe'])
def test_save_source_mismatch(name):
    with pytest.raises(ValueError):
        verify_filename(name, {'sent_at': '2026-10-03T00:00:25+08:00', 'local_id': 23})


def test_filename_opaque_suffix_and_header_validation():
    assert verify_filename('微信图片_20261003000025_23_172.jpg',
        {'sent_at': '2026-10-03T00:00:25+08:00', 'local_id': 23}) == 'jpg'
    assert header_matches('测试群（12）', '测试群', True)
    assert not header_matches('测试群（12）', '测试群', False)
    assert not header_matches('其他测试群', '测试群', True)


@pytest.mark.parametrize('title,allowed', [('联系人', True), ('群聊', True), ('最常使用', True),
    ('搜索网络结果', False), ('网络查找微信号', False), ('聊天文件', False)])
def test_search_heading_rejects_web_suggestions(title, allowed):
    assert local_result_heading(title) is allowed


def test_whole_photo_matching_rejects_duplicates_and_supports_scale():
    rng = np.random.default_rng(11)
    anchor = Image.fromarray(rng.integers(0, 256, (80, 60, 3), dtype=np.uint8))
    screen = Image.new('RGB', (600, 600), 'black')
    assert locate_thumbnail(screen, anchor) is None
    screen.paste(anchor, (80, 80))
    point = locate_thumbnail(screen, anchor)
    assert point and abs(point[0]-110) <= 2 and abs(point[1]-120) <= 2
    screen.paste(anchor, (400, 400))
    with pytest.raises(ValueError, match='多张'):
        locate_thumbnail(screen, anchor)


@pytest.mark.parametrize('key,value', [('max_scrolls', 100), ('max_photos', 0), ('wait_seconds', True)])
def test_bounds(setup, key, value):
    data = json.loads(setup.settings.read_text(encoding='utf-8'))
    data[key] = value
    dump(setup.settings, data)
    with pytest.raises(ValueError, match='范围'):
        recovery.settings(setup.settings)


def test_cli_routes_refresh_and_opt_in_recovery(setup, monkeypatch):
    from typer.testing import CliRunner
    from invoice_print_layout.cli import app
    env = setup
    config = env.root/'project.json'
    registry = env.root/'registry.json'
    dump(config, {'schema_version': 1, 'project_id': 'test', 'source': env.config})
    dump(registry, {'projects': {'test': {'config': 'project.json'}}})
    received = []
    def run(source, day, output, desktop, **kwargs):
        received.append((source, day, output, desktop, kwargs))
        return {'state': 'done', 'photos': []}
    monkeypatch.setattr(recovery, 'collect_recovering', run)
    args = ['daily', 'collect', '--project', 'test', '--date', DAY, '--registry', str(registry)]
    result = CliRunner().invoke(app, [*args, '--refresh', '--recover-missing', '--desktop-config', str(env.settings)])
    assert result.exit_code == 0 and received[0][-1] == {'refresh': True}
    assert received[0][0]['chat_id'] == env.config['chat_id']
    assert received[0][3] == env.settings
    denied = CliRunner().invoke(app, [*args, '--recover-missing'])
    assert denied.exit_code != 0 and len(received) == 1
