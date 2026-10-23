"""Supervise read-only mail fetching with a cancellable hard wall-clock limit."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import math
import multiprocessing
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable

from invoice_print_layout.agent_jobs import terminate_worker
from invoice_print_layout.mail163 import Mail163Settings, MailImportError


MAX_RESULT_BYTES = 40 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
FetchWorker = Callable[[Mail163Settings, dict[str, Any], str], None]


def _write_manifest(directory: Path, value: dict[str, Any]) -> None:
    temporary = directory / 'manifest.tmp'
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(directory / 'manifest.json')


def _write_result(directory: Path, result: dict[str, Any]) -> None:
    """Serialize bounded originals separately from metadata; never serialize a key."""
    messages = []
    total = 0
    index = 0
    for message in result['messages']:
        files = []
        for original in message['files']:
            payload = original['payload']
            if not isinstance(payload, bytes) or not payload or len(payload) > MAX_FILE_BYTES:
                raise ValueError('invalid original size')
            total += len(payload)
            if total > MAX_RESULT_BYTES or index >= 1000:
                raise ValueError('batch exceeds limits')
            name = f'part-{index:04d}.bin'
            with (directory / name).open('xb') as stream:
                stream.write(payload)
            files.append({key: value for key, value in original.items() if key != 'payload'} | {
                'payload_file': name, 'payload_size': len(payload),
                'payload_sha256': hashlib.sha256(payload).hexdigest(),
            })
            index += 1
        messages.append({**message, 'files': files})
    _write_manifest(directory, {'ok': True, 'result': {**result, 'messages': messages}})


def _fetch_worker(settings: Mail163Settings, parameters: dict[str, Any], output: str) -> None:
    # Only the child process reads the local Vault. The parent and process args
    # contain mailbox/range information but never a password or authorization code.
    from invoice_print_layout.mail163 import read_auth_code
    from invoice_print_layout.mail_agent_source import fetch_messages

    directory = Path(output)
    code = ''
    try:
        code = read_auth_code(settings.address)
        result = fetch_messages(settings, code, since=date.fromisoformat(parameters['since']),
                                after_uid=parameters['after_uid'], uidvalidity=parameters['uidvalidity'],
                                limit=parameters['limit'])
        code = ''
        _write_result(directory, result)
    except Exception:
        # Do not write exception repr/traceback: remote errors may contain tokens.
        _write_manifest(directory, {'ok': False})
    finally:
        code = ''


def _read_result(directory: Path, limit: int) -> dict[str, Any]:
    manifest = directory / 'manifest.json'
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('missing or invalid manifest')
    value = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('ok') is not True or not isinstance(value.get('result'), dict):
        raise ValueError('fetch failed')
    result: dict[str, Any] = value['result']
    if (not isinstance(result.get('uidvalidity'), str)
            or not re.fullmatch(r'[0-9]{1,10}', result['uidvalidity'])
            or not 1 <= int(result['uidvalidity']) <= 2**32 - 1
            or type(result.get('more')) is not bool
            or not isinstance(result.get('messages'), list) or len(result['messages']) > limit):
        raise ValueError('invalid result metadata')
    total = 0
    names: set[str] = set()
    last_uid = 0
    for message in result['messages']:
        if (not isinstance(message, dict) or type(message.get('uid')) is not int
                or not last_uid < message['uid'] <= 2**32 - 1
                or any(not isinstance(message.get(key), str) for key in ('subject', 'sender', 'text', 'message_id'))
                or not isinstance(message.get('issues'), list)
                or any(not isinstance(issue, str) or len(issue) > 2000 for issue in message['issues'])
                or len(message['issues']) > 100
                or not isinstance(message.get('files'), list) or len(message['files']) > 20):
            raise ValueError('invalid message metadata')
        last_uid = message['uid']
        files = []
        for original in message['files']:
            if (not isinstance(original, dict) or not isinstance(original.get('name'), str)
                    or not isinstance(original.get('source'), str)
                    or not isinstance(original.get('payload_file'), str)
                    or not re.fullmatch(r'part-[0-9]{4}\.bin', original['payload_file'])
                    or original['payload_file'] in names
                    or type(original.get('payload_size')) is not int
                    or not 1 <= original['payload_size'] <= MAX_FILE_BYTES
                    or not isinstance(original.get('payload_sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', original['payload_sha256'])):
                raise ValueError('invalid original metadata')
            names.add(original['payload_file'])
            path = directory / original['payload_file']
            if (path.is_symlink() or path.resolve().parent != directory.resolve() or not path.is_file()
                    or path.stat().st_size != original['payload_size']):
                raise ValueError('original is outside result directory or changed')
            total += original['payload_size']
            if total > MAX_RESULT_BYTES:
                raise ValueError('originals exceed batch limit')
            payload = path.read_bytes()
            if hashlib.sha256(payload).hexdigest() != original['payload_sha256']:
                raise ValueError('original checksum mismatch')
            files.append({'name': original['name'], 'payload': payload, 'source': original['source']})
        message['files'] = files
    return result


def fetch_with_deadline(
    settings: Mail163Settings,
    *,
    since: date,
    after_uid: int = 0,
    uidvalidity: str = '',
    limit: int = 20,
    stop: threading.Event | None = None,
    timeout: float = 45,
    worker: FetchWorker = _fetch_worker,
) -> dict[str, Any]:
    """Only return a complete successful child result; cancel/timeout acknowledges nothing."""
    if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout)
            or not 0 < timeout <= 120 or not isinstance(since, date)
            or type(after_uid) is not int or not 0 <= after_uid <= 2**32 - 1
            or not isinstance(uidvalidity, str)
            or uidvalidity and not re.fullmatch(r'[0-9]{1,10}', uidvalidity)
            or type(limit) is not int or not 1 <= limit <= 50):
        raise MailImportError('自动收件范围或等待时间无效，请检查设置')
    if stop is not None and stop.is_set():
        raise MailImportError('自动收件已停止，本轮未确认任何邮件')
    process: Any = None
    started = False
    deadline = time.monotonic() + timeout
    try:
        # This context owns one exact new directory. No project/user file is a
        # cleanup target; the supervised child is dead before the context exits.
        with tempfile.TemporaryDirectory(prefix='invoice-mail-fetch-') as folder:
            parameters = {'since': since.isoformat(), 'after_uid': after_uid,
                          'uidvalidity': uidvalidity, 'limit': limit}
            process = multiprocessing.get_context('spawn').Process(
                target=worker, args=(settings, parameters, folder), daemon=True)
            try:
                process.start()
                started = True
                while process.is_alive():
                    if stop is not None and stop.is_set():
                        raise MailImportError('自动收件已停止，本轮未确认任何邮件')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MailImportError('自动收件等待超时，原有进度保留，请稍后重试')
                    process.join(min(.1, remaining))
                if stop is not None and stop.is_set():
                    raise MailImportError('自动收件已停止，本轮未确认任何邮件')
                if time.monotonic() > deadline or process.exitcode != 0:
                    raise MailImportError('自动收件未完成，原有进度保留，请稍后重试')
                return _read_result(Path(folder), limit)
            finally:
                if started:
                    if process.is_alive():
                        terminate_worker(process)
                    process.join()
                    process.close()
    except MailImportError:
        raise
    except Exception:
        raise MailImportError('自动收件未完成，原有进度保留，请检查连接后重试') from None
