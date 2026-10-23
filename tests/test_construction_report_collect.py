from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from invoice_print_layout import construction_report_collect as collection
from invoice_print_layout import photo_wechat_bridge as bridge

DAY = '2026-09-28'
TZ = timezone(timedelta(hours=8))


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def environment(tmp_path, monkeypatch):
    chat = '123@chatroom'
    helper = tmp_path / 'helpers'
    helper.mkdir()
    for name in ('probe_filehelper.py', 'collect_sender.py'):
        (helper / name).write_text('# synthetic audited helper', encoding='utf-8')
    audited = tmp_path / 'backend'
    media = audited / 'wechatauto' / 'media.py'
    media.parent.mkdir(parents=True)
    media.write_text('''class MediaDownloader:
    def __init__(self, reader): pass
    def _probe_ct(self, path): pass
    def _derive_cfg_key(self): return (b"test", 0)
    def decrypt_image(self, path, **kwargs): return b"\\x89PNGsynthetic-image"
''', encoding='utf-8')
    db_dir = tmp_path / 'databases'
    db_dir.mkdir()
    db_paths = {name: db_dir / name for name in ('contact.db', 'message_resource.db', 'message_0.db')}
    with sqlite3.connect(db_paths['contact.db']) as db:
        db.execute('CREATE TABLE contact(username TEXT)')
        db.executemany('INSERT INTO contact VALUES (?)', [(chat,), ('wxid_private',)])
    with sqlite3.connect(db_paths['message_resource.db']) as db:
        db.execute('CREATE TABLE ChatName2Id(user_name TEXT)')
        db.execute('CREATE TABLE MessageResourceInfo(chat_id INTEGER,message_local_id INTEGER,message_create_time INTEGER,message_local_type INTEGER,packed_info BLOB)')
        db.executemany('INSERT INTO ChatName2Id VALUES (?)', [(chat,), ('wxid_private',)])
    with sqlite3.connect(db_paths['message_0.db']) as db:
        db.execute('CREATE TABLE Name2Id(user_name TEXT)')
        db.execute('INSERT INTO Name2Id VALUES (?)', ('wxid_sender',))
        for name in (chat, 'wxid_private'):
            table = 'Msg_' + hashlib.md5(name.encode()).hexdigest()
            db.execute(f'CREATE TABLE "{table}"(local_id INTEGER,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER,sort_seq INTEGER,message_content TEXT,compress_content BLOB)')
    account = {'account': 'wxid_account', 'path': str(tmp_path / 'wechat-account')}

    def connect(path):
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db

    reader = SimpleNamespace(unkeyed=[], wxid='wxid_account')
    reader._db_files = [(name, str(path), 0) for name, path in db_paths.items()]
    reader._open = lambda rel: connect(db_paths[rel])

    @contextlib.contextmanager
    def scoped_reader(*args):
        yield reader

    backend = SimpleNamespace(extract_master_key_from_cfg=lambda pid: None)
    probe = SimpleNamespace(load_backend=lambda path: backend, active_account=lambda module: (account, 101),
                            message_text=lambda module, row: row['message_content'])
    collector = SimpleNamespace(scoped_reader=scoped_reader, open_named=lambda reader, name: connect(db_paths[name]))
    original_import = bridge.importlib.import_module
    monkeypatch.setattr(bridge.importlib, 'import_module', lambda name, *args: (
        probe if name == 'probe_filehelper' else collector if name == 'collect_sender' else original_import(name, *args)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 9, 29, 12, tzinfo=TZ)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr(bridge, 'datetime', Clock)
    reader_config = tmp_path / 'photo-reader.json'
    values = {'python': 'synthetic-python', 'helper_dir': str(helper), 'audited_source': str(audited),
              'helper_hashes': {name: collection._hash(helper / name) for name in ('probe_filehelper.py', 'collect_sender.py')},
              'media_sha256': collection._hash(media)}
    dump(reader_config, values)
    config = {'source_id': 'group-source', 'chat_type': 'group', 'chat_id': chat,
              'account': account['account'], 'reader_config': str(reader_config)}
    calls = []

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        request = Path(args[-1])
        try:
            report = bridge.run(request)
        except Exception as exc:
            report = {'state': 'failed', 'error': str(exc) if isinstance(exc, ValueError) else '微信读取未完成，请检查本机登录及读取模块'}
        dump(request.parent / 'result.json', report)
        return SimpleNamespace(returncode=int(report['state'] == 'failed'))

    monkeypatch.setattr(collection.subprocess, 'run', launch)

    def add(local_id, kind=3, text='', *, chat_id=chat, cached=True, sent_day=DAY, sort_seq=None, original=False):
        when = datetime.fromisoformat(sent_day + 'T20:00:00').replace(tzinfo=TZ) + timedelta(seconds=local_id)
        table = 'Msg_' + hashlib.md5(chat_id.encode()).hexdigest()
        with sqlite3.connect(db_paths['message_0.db']) as db:
            db.execute(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?)',
                       (local_id, 1000 + local_id, 1, int(when.timestamp()), kind,
                        local_id if sort_seq is None else sort_seq, text, None))
        if kind == 3:
            digest = f'{local_id:032x}'
            with sqlite3.connect(db_paths['message_resource.db']) as db:
                resource_id = db.execute('SELECT rowid FROM ChatName2Id WHERE user_name=?', (chat_id,)).fetchone()[0]
                db.execute('INSERT INTO MessageResourceInfo VALUES (?,?,?,?,?)',
                           (resource_id, local_id, int(when.timestamp()), 3, b'\x12\x22\x0a\x20' + digest.encode()))
            if cached:
                folder = Path(account['path']) / 'msg/attach' / hashlib.md5(chat_id.encode()).hexdigest() / when.strftime('%Y-%m') / 'Img'
                folder.mkdir(parents=True, exist_ok=True)
                (folder / (digest + ('_h.dat' if original else '.dat'))).write_bytes(b'synthetic-source-' + str(local_id).encode())

    return SimpleNamespace(config=config, reader_values=values, reader=reader, probe=probe, db_paths=db_paths,
                           add=add, calls=calls, launch=launch, root=tmp_path, helper=helper)


@pytest.mark.parametrize('private', [False, True])
def test_exact_chat_collects_messages_media_and_preserves_original(environment, private):
    env = environment
    if private:
        env.config.update(source_id='private-source', chat_type='private', chat_id='wxid_private')
    for local_id, kind, text in [(1, 1, '收工前记录'), (2, 3, ''), (3, 49, ''), (4, 3, ''), (5, 1, '搭建结束')]:
        env.add(local_id, kind, text, chat_id=env.config['chat_id'])
    env.add(6, 1, '次日消息不进入日报', chat_id=env.config['chat_id'], sent_day='2026-09-29')
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'done' and not result['reused'] and len(result['photos']) == 2
    assert [row['type'] for row in result['messages']] == [1, 3, 49, 3, 1]
    assert result['messages'][-1]['text'] == '搭建结束'
    assert all(row['chat_id'] == env.config['chat_id'] for row in result['messages'])
    assert all(row['sent_at'] == row['time'] for row in result['messages'])
    assert all('普通缓存' in photo['quality'] for photo in result['photos'])
    for photo in result['photos']:
        assert collection._hash(Path(photo['path'])) == photo['sha256']
        assert (Path(photo['source']['raw_evidence']) / 'source.dat').read_bytes().startswith(b'synthetic-source-')
    assert len(env.calls) == 1
    assert env.calls[0][1]['capture_output'] and env.calls[0][1]['timeout'] == 240
    request = json.loads((Path(result['attempt_dir']) / 'request.json').read_text(encoding='utf-8'))
    assert request['include_messages'] is True and request['start'] == request['end'] == DAY
    assert not {'groups', 'auto_classify', 'output_root'}.intersection(request)


def test_replay_completed_attempt_does_not_launch_or_overwrite_evidence(environment):
    env = environment
    env.add(1)
    env.add(2, 1, '搭建结束')
    first = collection.collect(env.config, DAY, env.root / 'output')
    before = {path: path.read_bytes() for path in Path(first['attempt_dir']).rglob('*') if path.is_file()}
    second = collection.collect(env.config, DAY, env.root / 'output')
    assert second['reused'] and second['attempt_dir'] == first['attempt_dir'] and len(env.calls) == 1
    assert all(path.read_bytes() == payload for path, payload in before.items())
    assert all(second[key] == first[key] for key in ('messages', 'photos', 'file_hashes'))


def test_corrupt_completed_evidence_creates_new_attempt_and_preserves_old(environment):
    env = environment
    env.add(1)
    first = collection.collect(env.config, DAY, env.root / 'output')
    photo = Path(first['photos'][0]['path'])
    photo.write_bytes(b'edited-by-user')
    second = collection.collect(env.config, DAY, env.root / 'output')
    assert second['state'] == 'done' and not second['reused'] and second['attempt_dir'] != first['attempt_dir']
    assert photo.read_bytes() == b'edited-by-user' and len(env.calls) == 2


def test_config_and_date_isolation(environment):
    env = environment
    env.add(1)
    env.add(2, chat_id='wxid_private')
    first = collection.collect(env.config, DAY, env.root / 'output')
    private = {**env.config, 'source_id': 'private-source', 'chat_type': 'private', 'chat_id': 'wxid_private'}
    other = collection.collect(private, DAY, env.root / 'output')
    next_day = collection.collect(env.config, '2026-09-29', env.root / 'output')
    assert first['input_hash'] != other['input_hash'] != next_day['input_hash']
    assert len(other['photos']) == 1 and other['photos'][0]['source']['chat_id'] == 'wxid_private'
    assert next_day['state'] == 'partial' and not next_day['photos']


def test_missing_cache_is_partial_and_retry_preserves_prior_attempt(environment):
    env = environment
    env.add(1, cached=False)
    env.add(2, 1, '搭建结束')
    first = collection.collect(env.config, DAY, env.root / 'output')
    assert first['state'] == 'partial' and len(first['messages']) == 2 and not first['photos']
    assert '缓存' in json.dumps(first['issues'], ensure_ascii=False)
    previous = Path(first['result_path']).read_bytes()
    second = collection.collect(env.config, DAY, env.root / 'output')
    assert second['attempt_dir'] != first['attempt_dir'] and len(env.calls) == 2
    assert Path(first['result_path']).read_bytes() == previous


@pytest.mark.parametrize('failure', ['offline', 'unkeyed', 'wrong-account', 'changed-helper'])
def test_reader_failures_are_explicit_without_empty_success(environment, failure):
    env = environment
    env.add(1)
    if failure == 'offline':
        env.probe.active_account = lambda _: (_ for _ in ()).throw(RuntimeError('active_account_not_unique'))
    elif failure == 'unkeyed':
        env.reader.unkeyed = ['message_0.db']
    elif failure == 'wrong-account':
        env.config['account'] = 'wxid_another'
    else:
        (env.helper / 'collect_sender.py').write_text('changed', encoding='utf-8')
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'failed' and result['issues'][0]['code'] == 'reader_failed'
    assert Path(result['messages_path']).is_file() and Path(result['result_path']).is_file()
    assert not result['photos']


def test_missing_reader_configuration_is_durable_failure_without_launch(environment):
    env = environment
    Path(env.config['reader_config']).unlink()
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'failed' and 'photo-reader.json' in result['issues'][0]['message']
    assert not env.calls


def test_timeout_does_not_expose_subprocess_output(environment, monkeypatch):
    env = environment
    def timeout(*args, **kwargs):
        directory = Path(args[0][-1]).parent
        cache = directory / 'private-db-synthetic'
        cache.mkdir()
        (cache / 'decrypted.sqlite').write_bytes(b'derived-private-database')
        raise subprocess.TimeoutExpired('synthetic', 240, output=b'private-output', stderr=b'private-error')
    monkeypatch.setattr(collection.subprocess, 'run', timeout)
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'failed' and '超时' in result['issues'][0]['message']
    assert 'private-output' not in json.dumps(result) and 'private-error' not in json.dumps(result)
    assert not list(Path(result['attempt_dir']).glob('private-db-*'))


@pytest.mark.parametrize('changes', [
    {'chat_type': 'private'}, {'chat_type': 'other'}, {'chat_id': 'display name'},
    {'chat_id': '../escape'}, {'account': ''}, {'reader_config': 'relative.json'},
])
def test_invalid_source_rejected_before_reader(environment, changes):
    env = environment
    with pytest.raises(ValueError):
        collection.collect({**env.config, **changes}, DAY, env.root / 'output')
    assert not env.calls


@pytest.mark.parametrize('damage', ['hash', 'outside', 'chat', 'message-day', 'raw-hash', 'duplicate-photo'])
def test_source_path_hash_and_message_identity_are_validated(environment, monkeypatch, damage):
    env = environment
    env.add(1)
    outside = env.root / 'outside.png'
    outside.write_bytes(b'outside')
    def launch(args, **kwargs):
        completed = env.launch(args, **kwargs)
        directory = Path(args[-1]).parent
        report = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
        if damage == 'hash':
            report['photos'][0]['sha256'] = '0' * 64
        elif damage == 'outside':
            report['photos'][0]['file'] = str(outside)
        elif damage == 'chat':
            report['photos'][0]['source']['chat_id'] = '999@chatroom'
        elif damage == 'raw-hash':
            report['photos'][0]['source']['dat_sha256'] = '0' * 64
        elif damage == 'duplicate-photo':
            report['photos'].append(report['photos'][0])
        else:
            report['messages'][0]['sent_at'] = '2026-09-27T20:00:01+08:00'
            dump(directory / 'messages.json', report['messages'])
        dump(directory / 'result.json', report)
        return completed
    monkeypatch.setattr(collection.subprocess, 'run', launch)
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'failed' and result['issues'][0]['code'] == 'evidence_invalid'
    assert outside.read_bytes() == b'outside'


def test_legacy_group_bridge_remains_photo_only(environment):
    env = environment
    env.add(1, 1, '文字不在旧结果中')
    env.add(2, original=True)
    output = env.root / 'legacy'
    output.mkdir()
    request = output / 'request.json'
    dump(request, {**env.reader_values, 'account': env.config['account'], 'group_id': env.config['chat_id'], 'start': DAY, 'end': DAY})
    result = bridge.run(request)
    assert result['state'] == 'done' and len(result['photos']) == 1
    assert 'messages' not in result and not (output / 'messages.json').exists()
    assert '_h' in result['photos'][0]['quality']


def test_text_decode_failure_retains_order_and_marks_partial(environment):
    env = environment
    env.add(1)
    env.add(2, 1, '')
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'partial' and len(result['messages']) == 2
    assert result['messages'][1]['text'] == '' and '解码' in json.dumps(result['issues'], ensure_ascii=False)


def test_text_only_chat_without_photo_index_keeps_messages(environment):
    env = environment
    env.add(1, 1, '今天没有施工照片')
    with sqlite3.connect(env.db_paths['message_resource.db']) as db:
        db.execute('DELETE FROM ChatName2Id')
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'partial' and result['messages'][0]['text'] == '今天没有施工照片'
    assert not result['photos']


def test_message_limit_is_global_across_shards(environment):
    env = environment
    table = 'Msg_' + hashlib.md5(env.config['chat_id'].encode()).hexdigest()
    timestamp = int(datetime(2026, 9, 28, 20, tzinfo=TZ).timestamp())
    with sqlite3.connect(env.db_paths['message_0.db']) as db:
        db.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?)',
                       [(i, i, 1, timestamp, 1, i, 'synthetic', None) for i in range(2501)])
    extra = env.root / 'databases' / 'message_1.db'
    extra.write_bytes(env.db_paths['message_0.db'].read_bytes())
    env.db_paths['message_1.db'] = extra
    env.reader._db_files.append(('message_1.db', str(extra), 0))
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'failed' and '5000' in result['issues'][0]['message']


def test_cross_shard_equal_order_is_explicitly_ambiguous(environment):
    env = environment
    env.add(1)
    extra = env.root / 'databases' / 'message_1.db'
    extra.write_bytes(env.db_paths['message_0.db'].read_bytes())
    env.db_paths['message_1.db'] = extra
    env.reader._db_files.append(('message_1.db', str(extra), 0))
    result = collection.collect(env.config, DAY, env.root / 'output')
    assert result['state'] == 'partial' and '跨分片' in json.dumps(result['issues'], ensure_ascii=False)


def test_scope_guard_rejects_conflicting_legacy_id():
    with pytest.raises(ValueError, match='不一致'):
        bridge.requested_chat({'chat_type': 'group', 'chat_id': '123@chatroom', 'group_id': '456@chatroom'})
    with pytest.raises(ValueError, match='布尔'):
        bridge.requested_chat({'group_id': '123@chatroom', 'include_messages': 'true'})


def test_refresh_reads_new_messages_and_retains_completed_attempt(environment):
    env = environment
    env.add(1)
    old = collection.collect(env.config, DAY, env.root/'out')
    original = Path(old['receipt_path']).read_bytes()
    env.add(2)
    fresh = collection.collect(env.config, DAY, env.root/'out', refresh=True)
    assert len(fresh['photos']) == 2 and not fresh['reused']
    assert Path(old['receipt_path']).read_bytes() == original


def test_thumbnail_is_validated_but_not_counted_as_collected_photo(environment):
    env = environment
    env.add(1, cached=False)
    folder = env.root/'wechat-account/msg/attach'/hashlib.md5(env.config['chat_id'].encode()).hexdigest()/'2026-09/Img'
    folder.mkdir(parents=True)
    (folder/('0'*31+'1_t.dat')).write_bytes(b'thumbnail-source')
    result = collection.collect(env.config, DAY, env.root/'out', include_thumbnails=True)
    assert result['state'] == 'partial' and not result['photos']
    assert len(result['thumbnails']) == 1
    assert result['thumbnails'][0]['source']['local_id'] == 1
    assert '缩略图' in result['thumbnails'][0]['quality']


def test_chat_title_uniqueness_uses_same_normalization_as_ui(environment):
    env = environment
    env.add(1)
    with sqlite3.connect(env.db_paths['contact.db']) as db:
        db.execute('ALTER TABLE contact ADD COLUMN nick_name TEXT')
        db.execute('ALTER TABLE contact ADD COLUMN remark TEXT')
        db.execute('UPDATE contact SET nick_name=? WHERE username=?', ('示例 群（12）', env.config['chat_id']))
    first = collection.collect(env.config, DAY, env.root/'out')
    assert first['chat_profile']['unique_title'] is True
    assert first['chat_profile']['process_id'] == 101
    with sqlite3.connect(env.db_paths['contact.db']) as db:
        db.execute('UPDATE contact SET nick_name=? WHERE username=?', ('示例群(12)', 'wxid_private'))
    second = collection.collect(env.config, DAY, env.root/'out', refresh=True)
    assert second['chat_profile']['unique_title'] is False
