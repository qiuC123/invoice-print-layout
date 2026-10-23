"""Opt-in cache -> chat -> cache -> bounded photo-save recovery workflow."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

from invoice_print_layout import construction_report_collect as cache
from invoice_print_layout.construction_reports import run_lock
from invoice_print_layout.wechat_photo_desktop import DesktopRecovery, image_info, verify_filename


def missing(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    present = {cache._message_key(p['source']) for p in receipt['photos']}
    return [m for m in receipt['messages'] if m['type'] == 3 and cache._message_key(m) not in present]


def merged(latest: dict[str, Any], saved: list[dict[str, Any]]) -> dict[str, Any]:
    messages = {cache._message_key(m): m for m in latest['messages']}
    photos = {cache._message_key(p['source']): p for p in latest['photos']}
    recovered: set[tuple[str, str, int, str]] = set()
    for photo in saved:
        source = photo['source']
        key = cache._message_key(source)
        message = messages.get(key)
        if (not message or message['type'] != 3 or message['message_id'] != source['message_id']
                or message['sender'] != source['sender'] or photo['verified_chat_id'] != message['chat_id']):
            raise ValueError('另存照片没有对应到采集窗口内的精确消息')
        verify_filename(photo['default_filename'], message)
        checked = image_info(Path(photo['path']))
        if any(photo.get(k) != v for k, v in checked.items()):
            raise ValueError('另存照片文件校验失败')
        # Prefer a validated cache export if re-reading also recovered this photo.
        photos.setdefault(key, photo)
        recovered.add(key)
    issues = []
    removable = {'image_not_cached', 'image_resource_ambiguous', 'image_decode_failed', 'thumbnail_decode_failed'}
    for issue in latest['issues']:
        code = issue.get('code')
        if code in {'photos_missing', 'no_usable_photos'}:
            continue
        item = issue.get('item')
        if (code in removable and isinstance(item, dict)
                and all(k in item for k in ('chat_id', 'shard', 'local_id', 'sent_at'))
                and cache._message_key(item) in recovered):
            continue
        issues.append(issue)
    remaining = [m for key, m in messages.items() if m['type'] == 3 and key not in photos]
    if remaining:
        issues.append({'code': 'photos_missing', 'message': f'{len(remaining)}条图片消息仍缺大图', 'items': remaining})
    if not photos:
        issues.append({'code': 'no_usable_photos', 'message': '没有可用大图'})
    return {**latest, 'photos': list(photos.values()), 'issues': issues,
            'state': 'failed' if latest['state'] == 'failed' else 'partial' if issues else 'done'}


def settings(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or not isinstance(value.get('nuphus_executable'), str):
        raise ValueError('补图配置需要nuphus_executable')
    executable = Path(value['nuphus_executable'])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('nuphus可执行文件缺失或路径不是绝对路径')
    for key, default, low, high in [('wait_seconds', 3, 1, 30), ('max_photos', 10, 1, 50), ('max_scrolls', 6, 0, 20)]:
        value.setdefault(key, default)
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError('补图参数超出范围：'+key)
    return value


def collect_recovering(config: dict[str, Any], report_date: str, output_dir: Path,
                       desktop_config: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Explicitly authorized GUI recovery; default ``collect`` stays read-only.

    Each cache scan and GUI attempt retains its original evidence. A recovered
    receipt is an aggregate and never overwrites the original partial result.
    """
    # Always begin with the collector's scope validation and a read-only attempt.
    baseline = cache.collect(config, report_date, output_dir, refresh=refresh, include_thumbnails=True)
    if baseline['state'] == 'failed' or not missing(baseline):
        return baseline
    cfg = settings(desktop_config)
    inputs = {'source': config, 'date': report_date, 'desktop': cfg,
        'baseline_input_hash': baseline['input_hash'],
        'code': {name: cache._hash(Path(__file__).with_name(name)) for name in
            ('construction_photo_recovery.py', 'wechat_photo_desktop.py', 'construction_report_collect.py')}}
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not refresh:
        for path in sorted(output_dir.glob('recovery-*/recovery.json'), reverse=True):
            try:
                if cache._hash(path) != path.with_suffix('.sha256').read_text(encoding='ascii').strip():
                    continue
                old = cache._read(path)
                if not isinstance(old, dict):
                    continue
            except (OSError, ValueError):
                continue
            if old.get('input_hash') != fingerprint or old.get('state') != 'done':
                continue
            # Reuse only when the observed message set is unchanged, even if the
            # same date now has additional messages.
            if old.get('messages') != baseline['messages']:
                continue
            try:
                hashes = old['recovery_file_hashes']
                if not hashes or any(cache._hash(cache._inside(output_dir, p)) != h for p, h in hashes.items()):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            return {**old, 'reused': True}
    folder = output_dir / ('recovery-'+datetime.now(cache.TZ).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:10])
    folder.mkdir()
    cache._write_once(folder/'request.json', inputs)
    steps: list[dict[str, Any]] = []

    def event(stage: str, **details: Any) -> None:
        row = {'stage': stage, 'at': datetime.now(cache.TZ).isoformat(), **details}
        steps.append(row)
        with (folder/'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False)+'\n')

    latest = baseline
    scans = [baseline]
    saved: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    event('cache_before', receipt=baseline['receipt_path'], missing=len(missing(baseline)))
    profile = baseline.get('chat_profile', {})
    try:
        if profile.get('chat_id') != config['chat_id'] or profile.get('unique_title') is not True:
            raise ValueError('会话显示名称无法唯一对应到本次会话ID')
        # Serialize all recovery clients on this Windows account, across projects.
        lock_root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'invoice-print-layout' / 'wechat-desktop-lock'
        lock_root.mkdir(parents=True, exist_ok=True)
        with run_lock(lock_root):
            driver = DesktopRecovery(cfg, profile, folder)
            try:
                driver.enter_chat()
                event('chat_entered')
                time.sleep(cfg['wait_seconds'])
                scanned = cache.collect(config, report_date, output_dir, refresh=True, include_thumbnails=True)
                scans.append(scanned)
                event('cache_after_chat', receipt=scanned['receipt_path'], missing=len(missing(scanned)))
                if scanned['state'] == 'failed':
                    raise ValueError('进入会话后读取失败，未继续点图')
                latest = scanned
                thumbs = {cache._message_key(p['source']): p for scan in scans for p in scan.get('thumbnails', [])}
                for target in missing(latest)[:cfg['max_photos']]:
                    identity = {k: target[k] for k in ('chat_id', 'shard', 'local_id', 'sent_at', 'message_id')}
                    thumb = thumbs.get(cache._message_key(target))
                    if not thumb:
                        failures.append({'code': 'thumbnail_unavailable', 'item': identity,
                                         'message': '没有可验证缩略图，需人工定位或补发'})
                        event('photo_not_located', target=identity)
                        continue
                    event('photo_started', target=identity)
                    photo = driver.recover_photo(target, thumb)
                    if not Path(photo['path']).resolve().is_relative_to(folder.resolve()):
                        raise ValueError('另存文件越过本批次目录')
                    # Validate before accepting any GUI output into the aggregate.
                    merged(latest, [photo])
                    saved.append(photo)
                    cache._write_once(folder/f'saved-{len(saved):03}.json', photo)
                    event('photo_saved', target=identity, sha256=photo['sha256'])
                if saved:
                    scanned = cache.collect(config, report_date, output_dir, refresh=True, include_thumbnails=True)
                    scans.append(scanned)
                    event('cache_after_photos', receipt=scanned['receipt_path'])
                    if scanned['state'] == 'failed':
                        raise ValueError('逐张补图后重新读取失败，已保存成果和旧消息保留')
                    latest = scanned
            finally:
                driver.close()
    except (ValueError, OSError, RuntimeError) as exc:
        failures.append({'code': 'recovery_stopped', 'message': str(exc)})
        event('stopped', message=str(exc))
    result = merged(latest, saved)
    # Keep navigation/lookup failures explicit, even if unrelated images arrived.
    if failures:
        result['issues'] += failures
        if result['state'] != 'failed':
            result['state'] = 'partial'
    paths = {p for scan in scans for p in
             [Path(scan['receipt_path']), *(Path(scan['attempt_dir'])/rel for rel in scan['file_hashes'])]}
    paths.update(p for p in folder.rglob('*') if p.is_file())
    result.update(input_hash=fingerprint, reused=False, recovery_steps=steps,
        recovery_receipts=[s['receipt_path'] for s in scans], recovery_saved=saved,
        recovery_file_hashes={p.relative_to(output_dir).as_posix(): cache._hash(p) for p in paths},
        receipt_path=str(folder/'recovery.json'), recovery_dir=str(folder),
        original_missing_count=len(missing(baseline)), remaining_missing_count=len(missing(result)))
    cache._write_once(folder/'recovery.json', result)
    with (folder/'recovery.sha256').open('x', encoding='ascii') as stream:
        stream.write(cache._hash(folder/'recovery.json'))
    return result
