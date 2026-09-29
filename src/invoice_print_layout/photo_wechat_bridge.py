"""Bounded, read-only worker for a separately configured local WeChat reader.

Run under the reader's Python environment. No GUI, cloud, key persistence or sends.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def unique_resource_id(packed_values: list[bytes]) -> str | None:
    """Identical index rows are duplicates; differing rows remain ambiguous."""
    distinct = set(packed_values)
    if len(distinct) != 1:
        return None
    identifiers = {x.decode().lower() for x in re.findall(
        rb'\x12\x22\x0a\x20([0-9a-fA-F]{32})', next(iter(distinct)))}
    return next(iter(identifiers)) if len(identifiers) == 1 else None


def run(request_file: Path) -> dict[str, Any]:
    config = json.loads(request_file.read_text(encoding='utf-8'))
    output = request_file.parent.resolve()
    helper_dir = Path(config['helper_dir'])
    for name in ['probe_filehelper.py', 'collect_sender.py']:
        if hashlib.sha256((helper_dir / name).read_bytes()).hexdigest() != config['helper_hashes'][name]:
            raise ValueError('本机读取模块版本发生变化，请先核对配置')
    media_path = Path(config['audited_source']) / 'wechatauto/media.py'
    if hashlib.sha256(media_path.read_bytes()).hexdigest() != config['media_sha256']:
        raise ValueError('图片读取模块版本发生变化')
    sys.path.insert(0, str(helper_dir))
    probe: Any = importlib.import_module('probe_filehelper')
    collector: Any = importlib.import_module('collect_sender')
    module: Any = probe.load_backend(Path(config['audited_source']))
    account, pid = probe.active_account(module)
    if account['account'] != config['account']:
        raise ValueError('当前微信账号与项目配置不一致')
    chat = config['group_id']
    if not re.fullmatch(r'\d+@chatroom', chat):
        raise ValueError('群ID无效')
    tz = timezone(timedelta(hours=8))
    start = int(datetime.fromisoformat(config['start']).replace(tzinfo=tz).timestamp())
    end = min(int((datetime.fromisoformat(config['end']).replace(tzinfo=tz) + timedelta(days=1)).timestamp()), int(datetime.now(tz).timestamp()) + 1)
    table = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
    candidates: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    with collector.scoped_reader(module, account, pid, output) as reader:
        if reader.unkeyed:
            raise ValueError('部分消息库不可读取，本次未完成；请检查微信登录状态')
        with contextlib.closing(collector.open_named(reader, 'contact.db')) as contact:
            if not contact.execute('SELECT 1 FROM contact WHERE username=?', (chat,)).fetchone():
                raise ValueError('当前微信账号中未找到配置的群')
        with contextlib.closing(collector.open_named(reader, 'message_resource.db')) as resource:
            chat_rows = resource.execute('SELECT rowid FROM ChatName2Id WHERE user_name=?', (chat,)).fetchall()
            if len(chat_rows) != 1:
                raise ValueError('未找到唯一群图片资源索引')
            for rel, _, _ in reader._db_files:
                if not re.fullmatch(r'message_\d+\.db', Path(rel).name):
                    continue
                with contextlib.closing(reader._open(rel)) as db:
                    if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                        continue
                    messages = db.execute(f'SELECT local_id,server_id,real_sender_id,create_time FROM "{table}" '
                        'WHERE create_time>=? AND create_time<? AND (local_type & 255)=3 ORDER BY create_time,sort_seq LIMIT 501', (start, end)).fetchall()
                    if len(messages) > 500 or len(candidates) + len(messages) > 500:
                        raise ValueError('图片超过单次500张，请缩短读取日期范围')
                    for msg in messages:
                        packs = resource.execute('SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? '
                            'AND message_local_id=? AND message_create_time=? AND (message_local_type & 255)=3',
                            (chat_rows[0][0], msg['local_id'], msg['create_time'])).fetchall()
                        digest = unique_resource_id([bytes(p[0] or b'') for p in packs])
                        when = datetime.fromtimestamp(msg['create_time'], tz)
                        identity = {'chat_id': chat, 'message_id': str(msg['server_id']), 'local_id': msg['local_id'],
                                    'shard': rel, 'sent_at': when.isoformat()}
                        if digest is None:
                            issues.append({'item': identity, 'message': '图片资源对应关系不唯一，未导入'})
                            continue
                        folder = Path(account['path']) / 'msg/attach' / hashlib.md5(chat.encode()).hexdigest() / when.strftime('%Y-%m') / 'Img'
                        file = next((folder / (digest + suffix) for suffix in ['_h.dat', '.dat'] if (folder / (digest + suffix)).is_file()), None)
                        if file is None:
                            issues.append({'item': identity, 'message': '图片尚未缓存，请在微信中打开或下载原图后重新读取'})
                            continue
                        sender = db.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (msg['real_sender_id'],)).fetchone()
                        candidates.append({'path': file, 'source': {**identity, 'resource_id': digest, 'sender': sender[0] if sender else ''}})
        if not candidates:
            return {'state': 'partial' if issues else 'done', 'photos': [], 'issues': issues}
        package: Any = types.ModuleType('photo_local_media')
        package.__path__ = []
        package.db = module
        sys.modules['photo_local_media'] = package
        spec = importlib.util.spec_from_file_location('photo_local_media.media', media_path)
        if spec is None or spec.loader is None:
            raise ValueError('本机图片模块无法加载')
        media: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(media)
        reader._find_weixin_pids = lambda: [pid]
        helper = media.MediaDownloader(reader)
        helper._probe_ct(str(candidates[0]['path']))
        cfg = module.extract_master_key_from_cfg(pid)
        if cfg and cfg[2] == reader.wxid:
            reader.cfg_dword = cfg[1]
        derived = helper._derive_cfg_key()
        if derived:
            key, xor_key = derived
        else:
            key, xor_key = helper._scan_aes_key(monitor=False), None
        if not key:
            raise ValueError('当前无法解码图片，请在微信打开一张照片后重新读取')
        photos = []
        for i, item in enumerate(candidates):
            try:
                dat = item['path']
                raw = dat.read_bytes()
                if len(raw) > 24 * 1024 * 1024:
                    raise ValueError('照片超过读取大小上限')
                decoded = helper.decrypt_image(str(dat), aes_key=key, xor_key=xor_key)
                folder = output / f'{i + 1:04}'
                folder.mkdir()
                (folder / 'source.dat').write_bytes(raw)
                if decoded.startswith(b'wxgf'):
                    (folder / 'source.wxgf').write_bytes(decoded)
                    offset = decoded.find(b'\x00\x00\x00\x01')
                    ffmpeg = config.get('ffmpeg') or shutil.which('ffmpeg')
                    if offset < 0 or not ffmpeg:
                        raise ValueError('此缓存格式需要本机ffmpeg，尚未取得可显示照片')
                    stream = folder / 'preview.hevc'
                    stream.write_bytes(decoded[offset:])
                    image = folder / 'image.png'
                    try:
                        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(stream), '-frames:v', '1', str(image)],
                            check=True, capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    finally:
                        stream.unlink(missing_ok=True)
                else:
                    extension = '.jpg' if decoded.startswith(b'\xff\xd8\xff') else '.png' if decoded.startswith(b'\x89PNG') else '.webp' if decoded.startswith(b'RIFF') else ''
                    if not extension:
                        raise ValueError('图片格式无法识别')
                    image = folder / ('image' + extension)
                    image.write_bytes(decoded)
                quality = '微信原图版本（本机_h缓存）' if dat.name.endswith('_h.dat') else '微信普通缓存，尚未取得原图版本'
                photos.append({'file': image.relative_to(output).as_posix(), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                    'quality': quality, 'source': {**item['source'], 'dat_sha256': hashlib.sha256(raw).hexdigest(),
                    'decoded_sha256': hashlib.sha256(decoded).hexdigest(), 'raw_evidence': str(folder)}})
            except Exception as exc:
                issues.append({'item': item['source'], 'message': str(exc)[:200] if isinstance(exc, ValueError) else '图片解码未完成，原始缓存保留'})
        return {'state': 'partial' if issues else 'done', 'photos': photos, 'issues': issues}


def main() -> None:
    request = Path(sys.argv[1])
    try:
        result = run(request)
    except Exception as exc:
        result = {'state': 'failed', 'error': str(exc)[:300] if isinstance(exc, ValueError) else '微信读取未完成，请检查本机登录及读取模块'}
    (request.parent / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if result['state'] == 'failed':
        sys.exit(1)


if __name__ == '__main__':
    main()
