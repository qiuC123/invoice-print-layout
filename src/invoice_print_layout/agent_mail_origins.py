"""Read authoritative, attachment-bound mail provenance without migrating a store.

Automatic scoped message/account rows and new manual-fetch manifest records can
authorize a MailOrigin. Legacy UID-only history, filenames and entry-supplied
mail metadata are insufficient. This module performs no mailbox or business work.
"""
from __future__ import annotations

from dataclasses import asdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, TypeGuard

from invoice_print_layout.agent_transport import MailOrigin
from invoice_print_layout.workbench import ExpenseStore


_DIGEST = re.compile(r'[0-9a-f]{64}')
_FOLDER = re.compile(r'[A-Za-z0-9._/-]{1,128}')
_ADDRESS = re.compile(r'[a-z0-9._%+-]+@163\.com')
_SUFFIXES = {'.pdf', '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _hash(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def mail_origin_fingerprint(origins: dict[str, MailOrigin]) -> str:
    """Stable ordering; empty/added/removed/changed authority changes the hash."""
    return _hash({key: asdict(value) for key, value in sorted(origins.items())})


def _epoch(value: Any) -> TypeGuard[str]:
    return (isinstance(value, str) and re.fullmatch(r'[0-9]{1,10}', value) is not None and
            0 < int(value) <= 2**32 - 1)


def _uid(value: Any) -> TypeGuard[int]:
    return type(value) is int and 0 < value <= 2**32 - 1


def _object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else None
    except (ValueError, TypeError):
        return None


def _entry_current(entry: dict[str, Any], stored: dict[str, Any]) -> bool:
    if (not isinstance(entry.get('id'), str) or not isinstance(entry.get('project_id'), str) or
            not entry['project_id'] or entry.get('source') not in {'mail_agent', 'mail'} or
            not isinstance(entry.get('batch'), str) or
            entry['source'] == 'mail_agent' and not _DIGEST.fullmatch(entry['batch']) or
            not isinstance(entry.get('digest'), str) or not _DIGEST.fullmatch(entry['digest']) or
            type(entry.get('revision')) is not int):
        return False
    for key in ('id', 'project_id', 'source', 'batch', 'digest', 'revision'):
        if stored.get(key) != entry.get(key):
            return False
    try:
        path = Path(entry['path']).resolve(strict=True)
        return (path.is_file() and path == Path(stored['path']).resolve(strict=True) and
                hashlib.sha256(path.read_bytes()).hexdigest() == entry['digest'])
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _verified_origin(store: ExpenseStore, entry: dict[str, Any], message_row: sqlite3.Row,
                     account_row: sqlite3.Row, hashes: dict[Path, str]) -> MailOrigin | None:
    message = _object(message_row['data'])
    account_data = _object(account_row['data'])
    if message is None or account_data is None:
        return None
    account = dict(account_row)
    address, mailbox = account['address'], account['folder']
    if (not isinstance(address, str) or _ADDRESS.fullmatch(address) is None or
            not isinstance(mailbox, str) or _FOLDER.fullmatch(mailbox) is None or
            account['id'] != _hash(['imap.163.com', address, mailbox]) or
            not _epoch(account['uidvalidity'])):
        return None
    # Producer state JSON may not shadow an authoritative scope column.
    if any(key in account_data and account_data[key] != account[key] for key in
           ('id', 'address', 'folder', 'uidvalidity')):
        return None
    if (message_row['account_id'] != account['id'] or not _epoch(message_row['uidvalidity']) or
            account['uidvalidity'] != message_row['uidvalidity'] or not _uid(message_row['uid']) or
            message_row['id'] != _hash([account['id'], message_row['uidvalidity'], message_row['uid']])):
        return None
    if any(message.get(key) != message_row[key] for key in ('id', 'account_id', 'uidvalidity', 'uid')):
        return None
    if (type(message.get('uid')) is not int or message.get('project_id') != entry['project_id'] or
            not isinstance(message.get('message_id'), str) or len(message['message_id']) > 1000 or
            message.get('source_issues') or
            any(key in message and message[key] != mailbox for key in ('mailbox', 'folder'))):
        return None
    members = message.get('material_ids')
    files = message.get('files')
    if (not isinstance(members, list) or any(not isinstance(key, str) for key in members) or
            members.count(entry['id']) != 1 or not isinstance(files, list) or not 1 <= len(files) <= 20):
        return None
    matching: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_folder = (store.root / '邮件Agent原件' / message_row['id']).resolve()
    for file in files:
        if (not isinstance(file, dict) or not isinstance(file.get('digest'), str) or
                not _DIGEST.fullmatch(file['digest']) or file['digest'] in seen or
                not isinstance(file.get('path'), str)):
            return None
        seen.add(file['digest'])
        try:
            path = Path(file['path']).resolve(strict=True)
            if (path.parent != expected_folder or path.stem != file['digest'] or
                    path.suffix not in _SUFFIXES or not path.is_file()):
                return None
            if path not in hashes:
                hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            if hashes[path] != file['digest']:
                return None
        except (ValueError, OSError):
            return None
        if file['digest'] == entry['digest']:
            matching.append(file)
    if len(matching) != 1:
        return None
    return MailOrigin(account['id'], mailbox, message_row['uidvalidity'], message_row['uid'],
                      message['message_id'], entry['digest'])


def _manual_manifest(store: ExpenseStore) -> tuple[bytes, dict[str, list[dict[str, Any]]]]:
    """Snapshot server history strictly, retaining conflicts instead of last-wins."""
    path = store.workspace / 'mail_imports.jsonl'
    if not path.is_file():
        return b'', {}
    raw = path.read_bytes()
    records: dict[str, list[dict[str, Any]]] = {}
    for line in raw.decode('utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('invalid mail manifest record')
        digest = row.get('attachment_hash')
        if isinstance(digest, str) and 'mail_origin_v1' in row:
            records.setdefault(digest, []).append(row)
    return raw, records


def _manual_origin(store: ExpenseStore, entry: dict[str, Any],
                   records: dict[str, list[dict[str, Any]]], hashes: dict[Path, str]) -> MailOrigin | None:
    rows = records.get(entry['digest'], [])
    if not rows:
        return None
    origins: set[MailOrigin] = set()
    try:
        current = Path(entry['path']).resolve(strict=True)
        if (current.parent != (store.root / '统一收件').resolve() or current.stem != entry['digest'] or
                current.suffix.lower() not in _SUFFIXES):
            return None
        for row in rows:
            origin = row.get('mail_origin_v1')
            if not isinstance(origin, dict):
                return None
            address, mailbox = origin.get('address'), origin.get('mailbox')
            account, epoch, uid = origin.get('account_id'), origin.get('uidvalidity'), origin.get('uid')
            message_id = origin.get('message_id')
            if (origin.get('host') != 'imap.163.com' or
                    not isinstance(address, str) or _ADDRESS.fullmatch(address) is None or
                    not isinstance(mailbox, str) or _FOLDER.fullmatch(mailbox) is None or
                    account != _hash(['imap.163.com', address, mailbox]) or
                    not _epoch(epoch) or not _uid(uid) or not isinstance(message_id, str) or
                    len(message_id) > 1000 or any(char in message_id for char in '\r\n\0') or
                    origin.get('message_key') != _hash([account, epoch, uid]) or
                    row.get('message_uid') != str(uid) or
                    row.get('source_kind') not in {'mail_attachment', 'mail_link'} or
                    not isinstance(row.get('saved_to'), str)):
                return None
            original = Path(row['saved_to']).resolve(strict=True)
            if (original.parent != (store.root / '邮件收件').resolve() or
                    original.suffix.lower() not in _SUFFIXES or not original.is_file()):
                return None
            if original not in hashes:
                hashes[original] = hashlib.sha256(original.read_bytes()).hexdigest()
            if hashes[original] != entry['digest']:
                return None
            origins.add(MailOrigin(account, mailbox, epoch, uid, message_id, entry['digest'],
                                   authority='mail_imports.jsonl'))
    except (KeyError, TypeError, ValueError, OSError):
        return None
    # Forwarded/repeated bytes in different messages cannot select one identity.
    return next(iter(origins)) if len(origins) == 1 else None


def load_trusted_mail_origins(store: ExpenseStore, entries: list[dict[str, Any]]) -> dict[str, MailOrigin]:
    """Load a read-only authority snapshot and verify both original byte copies.

    Missing/legacy/conflicting scope returns no authority for the affected
    record. Caller-provided fields, OCR and upload batches cannot create it.
    The automatic account's current epoch must still match. Manual records keep
    their fully scoped historical fetch identity, never a bare UID or entry hint.
    """
    if not entries or not store.database.is_file():
        return {}
    repeated = {str(e.get('id')) for e in entries if sum(other.get('id') == e.get('id') for other in entries) != 1}
    result: dict[str, MailOrigin] = {}
    hashes: dict[Path, str] = {}
    try:
        manifest_raw, manual = _manual_manifest(store) if any(e.get('source') == 'mail' for e in entries) else (b'', {})
        # Do not instantiate MailAgentService or use a constructor which installs
        # tables. mode=ro also prevents silently creating a missing database.
        with closing(sqlite3.connect(store.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            tables = {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'intake_queue' not in tables:
                return {}
            for entry in entries:
                key = entry.get('id')
                if not isinstance(key, str) or key in repeated:
                    continue
                stored_row = db.execute('SELECT data FROM intake_queue WHERE id=?', (key,)).fetchone()
                stored = _object(stored_row['data']) if stored_row else None
                if stored is None or not _entry_current(entry, stored):
                    continue
                if entry['source'] == 'mail':
                    origin = _manual_origin(store, entry, manual, hashes)
                else:
                    if not {'mail_agent_accounts', 'mail_agent_messages'} <= tables:
                        continue
                    message = db.execute('SELECT * FROM mail_agent_messages WHERE id=?', (entry['batch'],)).fetchone()
                    if message is None:
                        continue
                    account = db.execute('SELECT * FROM mail_agent_accounts WHERE id=?', (message['account_id'],)).fetchone()
                    if account is None:
                        continue
                    origin = _verified_origin(store, entry, message, account, hashes)
                # Detect current-original changes during authority file checks.
                if origin is not None and _entry_current(entry, stored):
                    result[key] = origin
        if manual and (store.workspace / 'mail_imports.jsonl').read_bytes() != manifest_raw:
            result = {key: value for key, value in result.items() if value.authority != 'mail_imports.jsonl'}
    except (sqlite3.Error, ValueError, OSError, TypeError):
        return {}
    return result
