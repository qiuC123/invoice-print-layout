"""Collect one exact chat/day into immutable local evidence attempts.

No SitePhotos instance, OCR, cloud classification, message delivery or scheduler
is involved. ``done`` means the observed source window was read successfully;
project attribution and the end-of-work batch still require report validation.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from invoice_print_layout.photo_wechat_bridge import requested_chat

TZ = timezone(timedelta(hours=8))
READER_FIELDS = ('python', 'helper_dir', 'audited_source', 'helper_hashes', 'media_sha256', 'ffmpeg')


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_once(path: Path, value: Any) -> None:
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(folder: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('采集证据必须使用批次内相对路径')
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError('采集证据路径越界或文件缺失')
    return path


def _message_key(row: dict[str, Any]) -> tuple[str, str, int, str]:
    return row['chat_id'], row['shard'], row['local_id'], row['sent_at']


def _validate_evidence(attempt: Path, report: dict[str, Any], config: dict[str, Any],
                       report_date: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    messages = _read(attempt / 'messages.json')
    if not isinstance(messages, list) or len(messages) > 5000:
        raise ValueError('消息证据清单缺失或超过读取上限')
    if 'messages' in report and report['messages'] != messages:
        raise ValueError('消息证据与采集结果不一致')
    photos, issues = report.get('photos', []), report.get('issues', [])
    if not isinstance(photos, list) or len(photos) > 500 or not isinstance(issues, list):
        raise ValueError('照片证据清单或未完成事项格式无效')
    if any(not isinstance(issue, dict) or not isinstance(issue.get('message'), str) for issue in issues):
        raise ValueError('未完成事项缺少具体说明')
    issues = list(issues)
    identities: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for row in messages:
        if (not isinstance(row, dict) or row.get('chat_id') != config['chat_id']
                or not isinstance(row.get('shard'), str) or not row['shard']
                or type(row.get('local_id')) is not int or row['local_id'] < 0
                or type(row.get('type')) is not int or type(row.get('sort_seq')) is not int
                or not isinstance(row.get('message_id'), str) or not isinstance(row.get('text'), str)
                or not isinstance(row.get('sender'), str) or not isinstance(row.get('sent_at'), str)):
            raise ValueError('消息来源身份或类型无效')
        when = datetime.fromisoformat(row['sent_at'])
        if when.utcoffset() is None or when.astimezone(TZ).date().isoformat() != report_date:
            raise ValueError('消息时间不属于请求日期或缺少时区')
        identity = _message_key(row)
        if identity in identities:
            raise ValueError('消息来源身份重复，无法确认批次顺序')
        identities[identity] = row
    normalized_photos = []
    image_keys: set[tuple[str, str, int, str]] = set()
    for photo in photos:
        if not isinstance(photo, dict) or not isinstance(photo.get('source'), dict):
            raise ValueError('照片缺少消息来源证据')
        source = photo['source']
        try:
            message = identities.get(_message_key(source))
        except (KeyError, TypeError) as exc:
            raise ValueError('照片消息身份无效') from exc
        if (not message or message['type'] != 3 or source.get('message_id') != message['message_id']
                or source.get('sender') != message['sender']):
            raise ValueError('照片未对应到本次会话日期内的图片消息')
        if _message_key(source) in image_keys:
            raise ValueError('同一图片消息出现重复照片结果')
        path = _inside(attempt, photo.get('file'))
        if _hash(path) != photo.get('sha256') or not isinstance(photo.get('quality'), str) or not photo['quality']:
            raise ValueError('照片校验不一致或缺少缓存质量标记')
        raw_dir = Path(str(source.get('raw_evidence', ''))).resolve()
        if not raw_dir.is_relative_to(attempt.resolve()):
            raise ValueError('原缓存证据路径越界')
        raw_file = _inside(attempt, (raw_dir / 'source.dat').relative_to(attempt.resolve()).as_posix())
        if _hash(raw_file) != source.get('dat_sha256'):
            raise ValueError('原缓存证据校验不一致')
        image_keys.add(_message_key(source))
        normalized_photos.append({**photo, 'path': str(path)})
    missing = [row for key, row in identities.items() if row['type'] == 3 and key not in image_keys]
    if missing:
        issues.append({'code': 'photos_missing', 'message': f'{len(missing)}条图片消息尚未取得可用照片',
                       'items': [{k: row[k] for k in ('chat_id', 'shard', 'local_id', 'sent_at')} for row in missing]})
    if not messages:
        issues.append({'code': 'messages_missing', 'message': '指定会话当天没有取得消息证据，无法确认收工批次'})
    if not normalized_photos:
        issues.append({'code': 'no_usable_photos', 'message': '指定会话当天没有取得可用照片'})
    return messages, normalized_photos, issues


def _existing(output_dir: Path, input_hash: str) -> dict[str, Any] | None:
    for path in sorted(output_dir.glob('attempt-*/collection.json'), reverse=True):
        try:
            receipt = _read(path)
            if not isinstance(receipt, dict) or receipt.get('input_hash') != input_hash or receipt.get('state') != 'done':
                continue
            hashes = receipt.get('file_hashes')
            if not isinstance(hashes, dict) or not {'request.json', 'messages.json', 'result.json'}.issubset(hashes):
                continue
            if any(_hash(_inside(path.parent, relative)) != digest for relative, digest in hashes.items()):
                continue
            if receipt.get('attempt_dir') != str(path.parent.resolve()):
                continue
            return {**receipt, 'reused': True}
        except (OSError, ValueError, TypeError):
            continue
    return None


def collect(config: dict[str, Any], report_date: str, output_dir: Path, *,
            refresh: bool = False, include_thumbnails: bool = False) -> dict[str, Any]:
    """Collect one source, or reuse its intact completed attempt.

    ``config`` requires source_id, chat_type (group/private), exact chat_id,
    account, and an absolute reader_config path. Invalid caller scope raises
    ValueError before collection. Offline/read/partial outcomes return a durable
    receipt with explicit issues; every retry gets a new attempt directory.
    """
    if not isinstance(report_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', report_date):
        raise ValueError('日报采集日期必须为YYYY-MM-DD')
    date.fromisoformat(report_date)
    provided = {key: config.get(key) for key in ('source_id', 'chat_type', 'chat_id', 'account', 'reader_config')}
    if any(not isinstance(value, str) or not value.strip() for value in provided.values()):
        raise ValueError('请完整配置来源编号、精确会话、账号及本机读取器配置路径')
    source = {key: str(value) for key, value in provided.items()}
    chat, chat_type = requested_chat(source)
    if not re.fullmatch(r'wxid_[A-Za-z0-9_-]+', source['account']):
        raise ValueError('采集账号标识无效')
    reader_path = Path(source['reader_config'])
    if not reader_path.is_absolute():
        raise ValueError('本机读取器配置必须使用绝对路径')
    if not output_dir.is_absolute() or output_dir.resolve() == Path(output_dir.anchor):
        raise ValueError('请选择独立采集目录的绝对路径，不能使用磁盘根目录')
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    bridge = Path(__file__).with_name('photo_wechat_bridge.py')
    reader: dict[str, Any] = {}
    setup_issue = ''
    try:
        raw = reader_path.read_bytes()
        loaded = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(loaded, dict) or not all(loaded.get(key) for key in READER_FIELDS[:-1]):
            raise ValueError('missing_reader_fields')
        reader = {key: loaded[key] for key in READER_FIELDS if key in loaded}
        reader_hash = hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        reader_hash = 'unavailable'
        setup_issue = '本机微信读取器配置缺失或无效，请恢复已核对的私有photo-reader.json'
    identity = {'source': source, 'report_date': report_date, 'reader_sha256': reader_hash,
                'bridge_sha256': _hash(bridge), 'include_thumbnails': include_thumbnails}
    input_hash = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not refresh and (existing := _existing(output_dir, input_hash)):
        return existing
    attempt = output_dir / ('attempt-' + datetime.now(TZ).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12])
    attempt.mkdir()
    request = {**reader, 'account': source['account'], 'chat_type': chat_type, 'chat_id': chat,
               'start': report_date, 'end': report_date, 'include_messages': True,
               'include_thumbnails': include_thumbnails}
    _write_once(attempt / 'request.json', request)
    execution_issue = setup_issue
    returncode = 1
    if not execution_issue:
        try:
            completed = subprocess.run([str(reader['python']), '-X', 'utf8', str(bridge), str(attempt / 'request.json')],
                capture_output=True, timeout=240, check=False,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            execution_issue = '微信读取超时；本次已取得证据保留，请检查登录状态后重新采集'
        except OSError:
            execution_issue = '本机微信读取器无法启动，请检查已配置的Python环境及微信登录状态'
    # The audited helper normally removes decrypted databases in its finally
    # block. A subprocess timeout can skip it; clean only this attempt's derived
    # cache directories, retaining requests, raw media and result evidence.
    for cache in attempt.glob('private-db-*'):
        if cache.is_dir() and not cache.is_symlink() and cache.resolve().is_relative_to(attempt):
            try:
                shutil.rmtree(cache.resolve())
            except OSError:
                execution_issue = '读取器临时数据库清理未完成，已保留批次供本机恢复处理'
    if not (attempt / 'result.json').exists():
        _write_once(attempt / 'result.json', {'state': 'failed', 'error': execution_issue or '读取器未返回结果文件'})
    if not (attempt / 'messages.json').exists():
        _write_once(attempt / 'messages.json', [])
    messages: list[dict[str, Any]] = []
    photos: list[dict[str, Any]] = []
    thumbnails: list[dict[str, Any]] = []
    profile: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    state, window = 'failed', {}
    try:
        report = _read(attempt / 'result.json')
        if not isinstance(report, dict) or report.get('state') not in {'done', 'partial', 'failed'}:
            raise ValueError('读取器返回结果格式无效')
        messages, photos, issues = _validate_evidence(attempt, report, source, report_date)
        if report.get('thumbnails'):
            _, thumbnails, _ = _validate_evidence(attempt, {**report, 'photos': report['thumbnails']}, source, report_date)
        if isinstance(report.get('chat_profile'), dict):
            profile = report['chat_profile']
        window = report.get('window', {})
        if execution_issue or returncode != 0 or report['state'] == 'failed':
            issues.insert(0, {'code': 'reader_failed', 'message': execution_issue or str(report.get('error') or '本机微信读取未完成，请检查登录及读取模块')})
        else:
            state = 'partial' if report['state'] == 'partial' or issues else 'done'
            if report['state'] == 'partial' and not issues:
                issues.append({'code': 'source_partial', 'message': '读取器报告未全部完成，需要核对来源窗口'})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        issues = [{'code': 'evidence_invalid', 'message': str(exc) if isinstance(exc, ValueError) else '采集证据不完整或格式无效'}]
    receipt = {'schema_version': 1, 'state': state, 'source_id': source['source_id'], 'report_date': report_date,
               'chat_type': chat_type, 'chat_id': chat, 'input_hash': input_hash, 'attempt_dir': str(attempt),
               'messages_path': str(attempt / 'messages.json'), 'result_path': str(attempt / 'result.json'),
               'receipt_path': str(attempt / 'collection.json'), 'window': window, 'messages': messages,
               'photos': photos, 'thumbnails': thumbnails, 'chat_profile': profile,
               'issues': issues, 'reused': False, 'created_at': datetime.now(TZ).isoformat(),
               'file_hashes': {path.relative_to(attempt).as_posix(): _hash(path) for path in sorted(attempt.rglob('*')) if path.is_file()}}
    _write_once(attempt / 'collection.json', receipt)
    return receipt
