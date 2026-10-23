"""Bounded local OCR cache, deliberately separate from the expense database."""
from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
import sqlite3
import threading

from invoice_print_layout.takeout import OcrLine

CACHE_VERSION = 1


class ReceiptOcrCache:
    def __init__(self, path: Path | None = None, capacity: int = 128):
        self.path = path
        self.capacity = capacity
        self.memory: OrderedDict[tuple[str, str], tuple[OcrLine, ...]] = OrderedDict()
        self.lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS ocr_results ('
                       'digest TEXT NOT NULL, signature TEXT NOT NULL, lines TEXT NOT NULL, '
                       'PRIMARY KEY(digest, signature))')
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _decode(value: str) -> tuple[OcrLine, ...] | None:
        try:
            rows = json.loads(value)
            if not isinstance(rows, list) or not rows or len(rows) > 10000:
                return None
            result = []
            for row in rows:
                if (not isinstance(row, list) or len(row) != 5 or not isinstance(row[0], str)
                        or any(type(coordinate) is not int for coordinate in row[1:])):
                    return None
                result.append(OcrLine(*row))
            return tuple(result) if any(line.text.strip() for line in result) else None
        except (ValueError, TypeError):
            return None

    def _remember(self, key: tuple[str, str], lines: tuple[OcrLine, ...]) -> None:
        self.memory[key] = lines
        self.memory.move_to_end(key)
        while len(self.memory) > self.capacity:
            self.memory.popitem(last=False)

    def get(self, digest: str, signature: str) -> list[OcrLine] | None:
        key = digest, signature
        with self.lock:
            lines = self.memory.get(key)
            if lines is not None:
                self.memory.move_to_end(key)
                return list(lines)
            if self.path is None:
                return None
            try:
                db = self._connect()
                try:
                    row = db.execute('SELECT lines FROM ocr_results WHERE digest=? AND signature=?', key).fetchone()
                finally:
                    db.close()
                lines = self._decode(row[0]) if row else None
            except (sqlite3.Error, OSError):
                return None
            if lines is not None:
                self._remember(key, lines)
                return list(lines)
            return None

    def put(self, digest: str, signature: str, lines: list[OcrLine]) -> None:
        if not lines or not any(line.text.strip() for line in lines):
            return
        values = tuple(lines)
        with self.lock:
            self._remember((digest, signature), values)
            if self.path is None:
                return
            try:
                encoded = json.dumps([[line.text, line.left, line.top, line.right, line.bottom]
                                      for line in lines], ensure_ascii=False)
                db = self._connect()
                try:
                    with db:
                        db.execute('INSERT OR REPLACE INTO ocr_results VALUES (?, ?, ?)',
                                   (digest, signature, encoded))
                finally:
                    db.close()
            except (sqlite3.Error, OSError):
                pass  # An unavailable cache must never prevent local recognition.
