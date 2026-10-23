"""Small, workspace-local cache for immutable, source-bound material facts.

The caller validates the original on every lookup.  This cache saves local PDF
parsing/OCR, not the byte read or source hash.  Cache failures remain optional:
the caller computes the facts normally, rather than trusting partial results.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable


MAX_ENTRIES = 256
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_VALUE_BYTES = 128 * 1024
MEMORY_ENTRIES = 128
_MEMORY: OrderedDict[tuple[str, str], str] = OrderedDict()
_MEMORY_LOCK = threading.Lock()
_KEY_LOCKS = tuple(threading.RLock() for _ in range(32))


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def clear_memory_cache() -> None:
    """Forget only the optional process cache; persisted facts remain intact."""
    with _MEMORY_LOCK:
        _MEMORY.clear()


def _memory_get(identity: tuple[str, str]) -> dict[str, Any] | None:
    with _MEMORY_LOCK:
        payload = _MEMORY.get(identity)
        if payload is not None:
            _MEMORY.move_to_end(identity)
    if payload is None:
        return None
    # JSON decoding gives every consumer an independent nested value.
    value: dict[str, Any] = json.loads(payload)
    return value


def _memory_put(identity: tuple[str, str], payload: str) -> None:
    with _MEMORY_LOCK:
        _MEMORY[identity] = payload
        _MEMORY.move_to_end(identity)
        while len(_MEMORY) > MEMORY_ENTRIES:
            _MEMORY.popitem(last=False)


class _DiskCache:
    def __init__(self, root: Path):
        directory = root / 'agent-material-cache'
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'facts.sqlite3'

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA busy_timeout=5000')
        # Bound the physical DB too. Deleted pages are reused by later writes.
        db.execute('PRAGMA max_page_count=2048')
        db.execute('''CREATE TABLE IF NOT EXISTS material_facts_cache (
            cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL, payload_bytes INTEGER NOT NULL,
            accessed INTEGER NOT NULL)''')
        return db

    def get(self, key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db, db:
            row = db.execute('SELECT payload,payload_sha256 FROM material_facts_cache WHERE cache_key=?',
                             (key,)).fetchone()
            if row is None:
                return None
            payload = str(row[0])
            if (len(payload.encode('utf-8')) > MAX_VALUE_BYTES or
                    hashlib.sha256(payload.encode('utf-8')).hexdigest() != row[1]):
                db.execute('DELETE FROM material_facts_cache WHERE cache_key=?', (key,))
                return None
            try:
                value = json.loads(payload)
            except (ValueError, TypeError):
                db.execute('DELETE FROM material_facts_cache WHERE cache_key=?', (key,))
                return None
            if not isinstance(value, dict):
                db.execute('DELETE FROM material_facts_cache WHERE cache_key=?', (key,))
                return None
            db.execute('UPDATE material_facts_cache SET accessed=? WHERE cache_key=?', (time.time_ns(), key))
            return value

    def put(self, key: str, payload: str) -> None:
        size = len(payload.encode('utf-8'))
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO material_facts_cache VALUES (?,?,?,?,?)',
                       (key, payload, hashlib.sha256(payload.encode('utf-8')).hexdigest(), size, time.time_ns()))
            # Do not scan/evict business data; this table is solely disposable facts.
            count, total = db.execute('SELECT COUNT(*),COALESCE(SUM(payload_bytes),0) FROM material_facts_cache').fetchone()
            if count > MAX_ENTRIES or total > MAX_PAYLOAD_BYTES:
                for stale_key, stale_bytes in db.execute(
                        'SELECT cache_key,payload_bytes FROM material_facts_cache ORDER BY accessed,cache_key').fetchall():
                    if count <= MAX_ENTRIES and total <= MAX_PAYLOAD_BYTES:
                        break
                    db.execute('DELETE FROM material_facts_cache WHERE cache_key=?', (stale_key,))
                    count -= 1
                    total -= stale_bytes


def source_facts(key: str, factory: Callable[[], dict[str, Any]], *, root: Path | None = None,
                 info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a separate copy; cache only successful, bounded fact descriptions.

    ``key`` must include parser versions and all source/material input.  The
    source byte check belongs before this function, including on a warm hit.
    Locks collapse concurrent same-key work in one process. SQLite transactions
    keep other processes coherent; they may harmlessly compute a miss twice.
    """
    identity = (str(root.resolve()) if root is not None else '<process-only>', key)
    lock = _KEY_LOCKS[int(hashlib.sha256(repr(identity).encode()).hexdigest()[:8], 16) % len(_KEY_LOCKS)]
    with lock:
        cached = _memory_get(identity)
        if cached is not None:
            if info is not None:
                info.update(hit=True, storage='memory')
            return cached
        disk: _DiskCache | None = None
        if root is not None:
            try:
                disk = _DiskCache(root.resolve())
                cached = disk.get(key)
            except (OSError, sqlite3.Error):
                # Permission, lock contention, corrupt/full cache: parse normally.
                disk = None
            if cached is not None:
                payload = encoded(cached)
                _memory_put(identity, payload)
                if info is not None:
                    info.update(hit=True, storage='disk')
                return cached
        value = factory()
        if info is not None:
            info.update(hit=False, storage='computed')
        # A read/parse failure must be retried, never turned into a durable miss.
        if value.get('issues'):
            return value
        try:
            payload = encoded(value)
        except (ValueError, TypeError):
            return value
        if len(payload.encode('utf-8')) <= MAX_VALUE_BYTES:
            _memory_put(identity, payload)
            if disk is not None:
                try:
                    disk.put(key, payload)
                except (OSError, sqlite3.Error):
                    pass
        return value
