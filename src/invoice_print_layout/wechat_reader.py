"""Bounded, read-only wx-cli history adapter; never owns a shared CLI cursor."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


class WeChatReadError(RuntimeError):
    """The source could not supply a validated history response."""


@dataclass(frozen=True)
class NormalizedMessage:
    chat_id: str
    message_key: str
    local_id: int
    sender_id: str | None
    timestamp: int
    current_text: str
    quoted_text: str
    raw_content: str
    type_code: int
    issues: tuple[str, ...] = ()

    @property
    def processable(self) -> bool:
        return bool(self.sender_id and self.current_text.strip() and not self.issues)


@dataclass(frozen=True)
class HistoryBatch:
    chat_id: str
    start_timestamp: int
    end_timestamp: int
    messages: tuple[NormalizedMessage, ...]
    source_status: str
    issues: tuple[str, ...]

    @property
    def safe_to_advance(self) -> bool:
        """Caller must durably store ALL messages before advancing its own cursor."""
        return not self.issues


def overlap_start(checkpoint: int, first_timestamp: int, overlap_seconds: int = 120) -> int:
    """Replay overlap even after restart; a checkpoint is owned by the caller."""
    if overlap_seconds < 1 or checkpoint < first_timestamp:
        raise ValueError("Invalid checkpoint or overlap")
    return max(first_timestamp, checkpoint - overlap_seconds)


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WeChatReadError(f"Invalid integer field: {field}")
    return value


def _window(chat_id: str, start: int, end: int) -> None:
    # No display-name lookup: upstream accepts fuzzy names, which can select a
    # different person. Returned username is checked as a second guard below.
    if not chat_id or chat_id.startswith("-") or any(c.isspace() for c in chat_id):
        raise ValueError("An exact WeChat username is required")
    if not (chat_id.startswith("wxid_") or chat_id.endswith("@chatroom")):
        raise ValueError("Only explicit wxid_ or @chatroom IDs are accepted")
    if type(start) is not int or type(end) is not int or not 1_000_000_001 < start <= end:
        raise ValueError("Invalid Unix-second history window")


def parse_history(
    payload: object, *, chat_id: str, start_timestamp: int, end_timestamp: int, limit: int
) -> HistoryBatch:
    """Parse the actual wx-cli history JSON, preserving uncertainty explicitly."""
    _window(chat_id, start_timestamp, end_timestamp)
    if limit < 1:
        raise ValueError("limit must be positive")
    if not isinstance(payload, dict) or payload.get("username") != chat_id:
        raise WeChatReadError("History response does not match the requested chat")
    chat_type = payload.get("chat_type")
    expected_type = "group" if chat_id.endswith("@chatroom") else "private"
    if chat_type != expected_type or payload.get("is_group") is not (expected_type == "group"):
        raise WeChatReadError("History chat type does not match the requested chat")
    rows, meta = payload.get("messages"), payload.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise WeChatReadError("History messages or freshness metadata missing")
    if _integer(payload.get("count"), "count") != len(rows) or len(rows) > limit:
        raise WeChatReadError("History count does not match the bounded response")
    issues: list[str] = []
    status = meta.get("status")
    if not isinstance(status, str):
        raise WeChatReadError("Freshness status missing")
    if status not in {"ok", "windowed"}:
        issues.append("source_not_fresh")
    unknown = meta.get("unknown_shards")
    if not isinstance(unknown, list) or any(not isinstance(x, str) for x in unknown):
        raise WeChatReadError("Invalid unknown-shard metadata")
    if unknown:
        issues.append("unknown_shards")
    if _integer(meta.get("shards_scanned"), "shards_scanned") < 1:
        issues.append("no_shards_scanned")
    # Upstream windowed status bypasses its stale heuristic. Check the actual
    # session timestamp ourselves when the latest session event is in this window.
    session_ts = meta.get("session_last_timestamp")
    latest_ts = meta.get("chat_latest_timestamp")
    if session_ts is None or latest_ts is None:
        issues.append("freshness_unverifiable")
    else:
        session = _integer(session_ts, "session_last_timestamp")
        latest = _integer(latest_ts, "chat_latest_timestamp")
        if session <= end_timestamp and session > latest:
            issues.append("session_ahead_of_history")
    if len(rows) == limit:
        # No timestamp-only pagination: equal-second messages could be skipped.
        issues.append("window_at_limit")
    messages: dict[str, NormalizedMessage] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise WeChatReadError("History message is not an object")
        ts = _integer(row.get("timestamp"), "timestamp")
        local_id = _integer(row.get("local_id"), "local_id")
        code = _integer(row.get("type_code"), "type_code")
        if not start_timestamp <= ts <= end_timestamp or local_id < 0 or code < 0:
            raise WeChatReadError("Message identity or timestamp is outside the requested window")
        content = row.get("content")
        if not isinstance(content, str):
            raise WeChatReadError("History content is not text")
        sender = row.get("sender_username")
        if sender is not None and (not isinstance(sender, str) or not sender.strip()):
            raise WeChatReadError("Invalid sender identity")
        message_issues: list[str] = []
        if sender is None:
            message_issues.append("sender_identity_missing")
            issues.append("sender_identity_missing")
        text, quote = "", ""
        if code == 1:
            text = content
        elif code == 49 and row.get("appmsg_type") == 57:
            if content == "[引用]" or content.startswith("[引用]\n"):
                remainder = content[len("[引用]"):]
            elif content.startswith("[引用] "):
                remainder = content[len("[引用] "):]
            else:
                remainder = ""
                message_issues.append("unrecognized_quote_format")
            text, separator, quote = remainder.partition("\n  ↳ ")
            if not separator:
                quote = ""
        # Unsupported media/system/revoke bodies are never fed to count parsing.
        identity = json.dumps([chat_id, local_id, ts, sender, code], ensure_ascii=False)
        key = "wx-history:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        message = NormalizedMessage(
            chat_id, key, local_id, sender, ts, text, quote, content, code,
            tuple(message_issues),
        )
        previous = messages.get(key)
        if previous is not None and previous != message:
            raise WeChatReadError("Conflicting messages share the available source identity")
        messages[key] = message
    return HistoryBatch(
        chat_id, start_timestamp, end_timestamp,
        tuple(sorted(messages.values(), key=lambda m: (m.timestamp, m.message_key))),
        status, tuple(dict.fromkeys(issues)),
    )


class WxHistoryReader:
    """Execute only a bounded history read; setup/login belongs to the operator."""

    def __init__(self, executable: Path, *, runtime_dir: Path, timeout_seconds: float = 30, limit: int = 500) -> None:
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("wx executable must be an existing absolute file path")
        if not 0 < timeout_seconds <= 120 or not 1 <= limit <= 5000:
            raise ValueError("Invalid timeout or bounded history limit")
        if not runtime_dir.is_absolute() or not runtime_dir.is_dir() or not (runtime_dir / 'config.json').is_file():
            raise ValueError("An initialized private runtime directory is required")
        self.executable = executable.resolve()
        self.runtime_dir = runtime_dir.resolve()
        self.timeout_seconds = timeout_seconds
        self.limit = limit

    def read_window(self, chat_id: str, start_timestamp: int, end_timestamp: int) -> HistoryBatch:
        _window(chat_id, start_timestamp, end_timestamp)
        command = [
            str(self.executable), "history", chat_id,
            "--before", str(start_timestamp - 1), "--after", str(end_timestamp + 1),
            "--limit", str(self.limit), "--json", "--with-meta",
        ]
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="strict",
                timeout=self.timeout_seconds, check=False, shell=False, cwd=self.runtime_dir, **options,
            )
        except subprocess.TimeoutExpired as exc:
            raise WeChatReadError("wx history timed out; retain the previous checkpoint") from exc
        except (OSError, UnicodeError) as exc:
            raise WeChatReadError("wx history could not return UTF-8 output") from exc
        if result.returncode != 0:
            # Do not leak stderr containing contacts, messages, or local data paths.
            raise WeChatReadError(f"wx history failed with exit code {result.returncode}")
        try:
            payload: object = json.loads(result.stdout)
        except ValueError as exc:
            raise WeChatReadError("wx history returned invalid JSON") from exc
        return parse_history(
            payload, chat_id=chat_id, start_timestamp=start_timestamp,
            end_timestamp=end_timestamp, limit=self.limit,
        )
