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


def requested_chat(config: dict[str, Any]) -> tuple[str, str]:
    """Keep legacy group requests; new callers must identify an exact chat type."""
    chat_type = config.get('chat_type', 'group')
    chat = config.get('chat_id', config.get('group_id', ''))
    pattern = r'\d+@chatroom' if chat_type == 'group' else r'wxid_[A-Za-z0-9_-]+'
    if chat_type not in {'group', 'private'} or not isinstance(chat, str) or not re.fullmatch(pattern, chat):
        raise ValueError('请提供与会话类型一致的精确微信会话ID')
    if config.get('group_id') and config['group_id'] != chat:
        raise ValueError('会话ID与兼容群ID不一致')
    if type(config.get('include_messages', False)) is not bool:
        raise ValueError('消息证据开关必须为布尔值')
    if type(config.get('include_thumbnails', False)) is not bool:
        raise ValueError('缩略图证据开关必须为布尔值')
    return chat, chat_type


def run(request_file: Path) -> dict[str, Any]:
    config = json.loads(request_file.read_text(encoding='utf-8'))
    chat, chat_type = requested_chat(config)
    include_messages = config.get('include_messages', False)
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
    tz = timezone(timedelta(hours=8))
    start = int(datetime.fromisoformat(config['start']).replace(tzinfo=tz).timestamp())
    end = min(int((datetime.fromisoformat(config['end']).replace(tzinfo=tz) + timedelta(days=1)).timestamp()), int(datetime.now(tz).timestamp()) + 1)
    table = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
    candidates: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    message_evidence: list[dict[str, Any]] = []
    thumbnails: list[dict[str, Any]] = []
    chat_profile: dict[str, Any] = {}
    scanned, image_count = 0, 0

    def evidence_result(photos: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {'state': 'partial' if issues else 'done', 'photos': photos, 'issues': issues}
        if include_messages:
            result.update(messages=message_evidence, chat_type=chat_type, chat_id=chat,
                          chat_profile=chat_profile, thumbnails=thumbnails,
                          window={'start': datetime.fromtimestamp(start, tz).isoformat(),
                                  'end_exclusive': datetime.fromtimestamp(end, tz).isoformat()})
        return result

    with collector.scoped_reader(module, account, pid, output) as reader:
        if reader.unkeyed:
            raise ValueError('部分消息库不可读取，本次未完成；请检查微信登录状态')
        with contextlib.closing(collector.open_named(reader, 'contact.db')) as contact:
            if not contact.execute('SELECT 1 FROM contact WHERE username=?', (chat,)).fetchone():
                raise ValueError('当前微信账号中未找到配置的会话')
            contact_columns = {row[1] for row in contact.execute('PRAGMA table_info(contact)')}
            if include_messages and {'nick_name', 'remark'}.issubset(contact_columns):
                row = contact.execute('SELECT nick_name,remark FROM contact WHERE username=?', (chat,)).fetchone()
                title = str(row['remark'] or row['nick_name'] or '')
                # Match visible names across all contacts, not just the target type.
                def normalized_title(value: str) -> str:
                    return re.sub(r'\s+', '', value.replace('（', '(').replace('）', ')'))
                matches = [r for r in contact.execute('SELECT username,nick_name,remark FROM contact')
                           if normalized_title(str(r['remark'] or r['nick_name'] or '')) == normalized_title(title)]
                chat_profile = {'chat_id': chat, 'title': title, 'unique_title': bool(title) and len(matches) == 1,
                                'process_id': pid}
        with contextlib.closing(collector.open_named(reader, 'message_resource.db')) as resource:
            chat_rows = resource.execute('SELECT rowid FROM ChatName2Id WHERE user_name=?', (chat,)).fetchall()
            if len(chat_rows) != 1 and not include_messages:
                raise ValueError('未找到唯一群图片资源索引')
            for rel, _, _ in reader._db_files:
                if not re.fullmatch(r'message_\d+\.db', Path(rel).name):
                    continue
                with contextlib.closing(reader._open(rel)) as db:
                    if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                        continue
                    columns = 'local_id,server_id,real_sender_id,create_time,local_type,sort_seq'
                    if include_messages:
                        columns += ',message_content,compress_content'
                    condition = '' if include_messages else ' AND (local_type & 255)=3'
                    limit = 5000 if include_messages else 500
                    messages = db.execute(f'SELECT {columns} FROM "{table}" '
                        f'WHERE create_time>=? AND create_time<?{condition} ORDER BY create_time,sort_seq LIMIT {limit + 1}', (start, end)).fetchall()
                    scanned += len(messages)
                    if scanned > limit:
                        raise ValueError(f'消息超过单次{limit}条，请缩短读取日期范围' if include_messages else '图片超过单次500张，请缩短读取日期范围')
                    for msg in messages:
                        when = datetime.fromtimestamp(msg['create_time'], tz)
                        identity = {'chat_id': chat, 'message_id': str(msg['server_id']), 'local_id': msg['local_id'],
                                    'shard': rel, 'sent_at': when.isoformat()}
                        sender_row = db.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (msg['real_sender_id'],)).fetchone()
                        sender = sender_row[0] if sender_row else ''
                        kind = msg['local_type'] & 255
                        if include_messages:
                            text = '[图片]' if kind == 3 else '[其他消息]'
                            if kind == 1:
                                try:
                                    text = probe.message_text(module, dict(msg))
                                    if not text:
                                        raise ValueError('empty_text')
                                except Exception:
                                    text = ''
                                    issues.append({'item': identity, 'message': '文本消息未能解码，无法确认照片批次连续性'})
                            if not sender:
                                issues.append({'item': identity, 'message': '消息发送者无法唯一确认'})
                            message_evidence.append({**identity, 'time': identity['sent_at'], 'sender': sender,
                                                     'type': kind, 'text': text, 'sort_seq': msg['sort_seq']})
                        if kind != 3:
                            continue
                        image_count += 1
                        if image_count > 500:
                            issues.append({'item': identity, 'message': '图片超过单次500张，此图片未读取'})
                            continue
                        if len(chat_rows) != 1:
                            issues.append({'item': identity, 'message': '未找到唯一会话图片资源索引'})
                            continue
                        packs = resource.execute('SELECT packed_info FROM MessageResourceInfo WHERE chat_id=? '
                            'AND message_local_id=? AND message_create_time=? AND (message_local_type & 255)=3',
                            (chat_rows[0][0], msg['local_id'], msg['create_time'])).fetchall()
                        digest = unique_resource_id([bytes(p[0] or b'') for p in packs])
                        if digest is None:
                            issues.append({'code': 'image_resource_ambiguous', 'item': identity, 'message': '图片资源对应关系不唯一，未导入'})
                            continue
                        folder = Path(account['path']) / 'msg/attach' / hashlib.md5(chat.encode()).hexdigest() / when.strftime('%Y-%m') / 'Img'
                        file = next((folder / (digest + suffix) for suffix in ['_h.dat', '.dat'] if (folder / (digest + suffix)).is_file()), None)
                        if file is None:
                            issues.append({'code': 'image_not_cached', 'item': identity, 'message': '图片尚未缓存，请在微信中打开或下载原图后重新读取'})
                            thumb = folder / (digest + '_t.dat')
                            if config.get('include_thumbnails') and thumb.is_file():
                                candidates.append({'path': thumb, 'thumbnail': True,
                                    'source': {**identity, 'resource_id': digest, 'sender': sender}})
                            continue
                        candidates.append({'path': file, 'thumbnail': False, 'source': {**identity, 'resource_id': digest, 'sender': sender}})
        if include_messages:
            message_evidence.sort(key=lambda row: (row['sent_at'], row['sort_seq'], row['shard'], row['local_id']))
            order_sources: dict[tuple[str, int], set[str]] = {}
            for row in message_evidence:
                order_sources.setdefault((row['sent_at'], row['sort_seq']), set()).add(row['shard'])
            if any(len(shards) > 1 for shards in order_sources.values()):
                issues.append({'item': chat, 'message': '同一时间与序号存在跨分片消息，批次顺序待核对'})
            # Preserve text evidence even if later media decoding cannot start.
            (output / 'messages.json').write_text(json.dumps(message_evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        if not candidates:
            return evidence_result([])
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
            if all(item['thumbnail'] for item in candidates):
                issues.append({'code': 'thumbnail_decode_failed', 'message': '缩略图暂不可解码，可先进入会话重试缓存'})
                return evidence_result([])
            raise ValueError('当前无法解码图片，请在微信打开一张照片后重新读取')
        photos: list[dict[str, Any]] = []
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
                destination = thumbnails if item['thumbnail'] else photos
                destination.append({'file': image.relative_to(output).as_posix(), 'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                    'quality': '缩略图，仅用于定位，不作为正式照片' if item['thumbnail'] else quality,
                    'source': {**item['source'], 'dat_sha256': hashlib.sha256(raw).hexdigest(),
                    'decoded_sha256': hashlib.sha256(decoded).hexdigest(), 'raw_evidence': str(folder)}})
            except Exception as exc:
                issues.append({'code': 'thumbnail_decode_failed' if item['thumbnail'] else 'image_decode_failed',
                    'item': item['source'], 'message': str(exc)[:200] if isinstance(exc, ValueError) else '图片解码未完成，原始缓存保留'})
        return evidence_result(photos)


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
