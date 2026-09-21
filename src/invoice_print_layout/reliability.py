from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'状态文件格式无效：{path.name}')
    return value


@contextmanager
def single_instance(workspace: Path, filename: str = 'bot.lock') -> Iterator[None]:
    """The OS releases the lock on exit, including a killed process."""
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / filename).open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise RuntimeError('后台机器人当前只支持 Windows')
        except OSError as exc:
            raise RuntimeError('这个工作区的机器人已经在运行') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class DurableQueue:
    """SQLite commits reception before the event callback acknowledges it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                due REAL NOT NULL DEFAULT 0)''')
            db.execute("UPDATE messages SET state='pending' WHERE state='running'")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, message_id: str, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO messages(id,payload) VALUES (?,?)',
                       (message_id, json.dumps(payload, ensure_ascii=False)))

    def next(self) -> tuple[str, dict[str, Any]] | None:
        with self.connect() as db:
            row = db.execute("SELECT id,payload,due FROM messages WHERE state='pending' ORDER BY seq LIMIT 1").fetchone()
            if row is None or float(row[2]) > time.time():
                return None
            db.execute("UPDATE messages SET state='running' WHERE id=?", (row[0],))
            return str(row[0]), json.loads(row[1])

    def done(self, message_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE messages SET state='done' WHERE id=?", (message_id,))

    def fail(self, message_id: str) -> bool:
        with self.connect() as db:
            row = db.execute('SELECT attempts FROM messages WHERE id=?', (message_id,)).fetchone()
            attempts = int(row[0]) + 1
            exhausted = attempts >= 3
            db.execute('UPDATE messages SET state=?,attempts=?,due=? WHERE id=?',
                       ('failed' if exhausted else 'pending', attempts,
                        time.time() + 15 * attempts, message_id))
        return exhausted
