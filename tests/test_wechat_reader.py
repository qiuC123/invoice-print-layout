from pathlib import Path
import json
import subprocess
from typing import Any

import pytest

from invoice_print_layout.wechat_reader import (
    WeChatReadError, WxHistoryReader, overlap_start, parse_history,
)


CHAT = "test-only@chatroom"
START, END = 1_790_000_000, 1_790_000_120


def history(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": CHAT, "chat_type": "group", "is_group": True,
        "count": len(rows), "messages": list(rows),
        "meta": {"status": "windowed", "unknown_shards": [], "shards_scanned": 1,
                 "chat_latest_timestamp": END, "session_last_timestamp": END},
    }


def row(**changes: Any) -> dict[str, Any]:
    return {"timestamp": START + 5, "local_id": 1, "type_code": 1,
            "sender_username": "wxid_test_sender", "content": "21份", **changes}


def parse(payload: object, limit: int = 500) -> Any:
    return parse_history(payload, chat_id=CHAT, start_timestamp=START,
                         end_timestamp=END, limit=limit)


def test_quote_excludes_original_count_and_preserves_current_correction() -> None:
    batch = parse(history(row(type_code=49, appmsg_type=57, content="[引用] 改成23份\n  ↳ 午饭21份")))
    message = batch.messages[0]
    assert message.current_text == "改成23份"
    assert message.quoted_text == "午饭21份"
    assert message.processable and batch.safe_to_advance


def test_quote_without_current_reply_and_media_are_not_counts() -> None:
    batch = parse(history(
        row(type_code=49, appmsg_type=57, content="[引用]\n  ↳ 21份"),
        row(local_id=2, type_code=49, appmsg_type=19, content="[合并聊天记录] 21份"),
        row(local_id=3, type_code=10002, content="[撤回] 21份"),
    ))
    assert all(not message.processable for message in batch.messages)


def test_private_chat_does_not_invent_sender_from_chat_username() -> None:
    item = row()
    del item["sender_username"]
    payload = history(item)
    payload.update(username="wxid_test_contact", chat_type="private", is_group=False)
    batch = parse_history(payload, chat_id="wxid_test_contact", start_timestamp=START,
                          end_timestamp=END, limit=500)
    assert batch.messages[0].sender_id is None
    assert not batch.messages[0].processable
    assert not batch.safe_to_advance
    assert "sender_identity_missing" in batch.issues


@pytest.mark.parametrize("meta_change,expected", [
    ({"status": "possibly_stale"}, "source_not_fresh"),
    ({"unknown_shards": ["message/message_2.db"]}, "unknown_shards"),
    ({"chat_latest_timestamp": START}, "session_ahead_of_history"),
    ({"session_last_timestamp": None}, "freshness_unverifiable"),
])
def test_stale_and_incomplete_are_not_no_reply(meta_change: dict[str, Any], expected: str) -> None:
    payload = history()
    payload["meta"].update(meta_change)
    batch = parse(payload)
    assert expected in batch.issues
    assert not batch.safe_to_advance


def test_full_window_does_not_advance_past_equal_second_messages() -> None:
    batch = parse(history(row(), row(local_id=2)), limit=2)
    assert len(batch.messages) == 2
    assert "window_at_limit" in batch.issues
    assert not batch.safe_to_advance


def test_overlap_replays_same_keys_and_id_reuse_at_other_time_is_distinct() -> None:
    batch = parse(history(row(), row(), row(timestamp=START + 6)))
    assert len(batch.messages) == 2
    assert batch.messages[0].message_key == parse(history(row())).messages[0].message_key
    assert overlap_start(START + 100, START) == START
    assert overlap_start(START + 500, START) == START + 380
    with pytest.raises(WeChatReadError, match="Conflicting"):
        parse(history(row(), row(content="22份")))


@pytest.mark.parametrize("change", [
    {"timestamp": START - 1}, {"timestamp": END + 1}, {"local_id": True},
    {"sender_username": ""}, {"content": 21},
])
def test_invalid_provenance_is_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(WeChatReadError):
        parse(history(row(**change)))


def test_wrong_chat_and_missing_meta_are_rejected() -> None:
    payload = history(row())
    payload["username"] = "another@chatroom"
    with pytest.raises(WeChatReadError, match="requested chat"):
        parse(payload)
    payload = history(row())
    del payload["meta"]
    with pytest.raises(WeChatReadError, match="metadata"):
        parse(payload)


def test_cli_arguments_are_bounded_exact_and_have_no_shared_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "wx.exe"
    executable.touch()
    runtime = tmp_path / 'private runtime'
    runtime.mkdir()
    (runtime / 'config.json').write_text('{}', encoding='utf-8')
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 7
        assert kwargs["cwd"] == runtime.resolve()
        return subprocess.CompletedProcess(command, 0, json.dumps(history(row())), "")

    monkeypatch.setattr(subprocess, "run", run)
    reader = WxHistoryReader(executable, runtime_dir=runtime, timeout_seconds=7, limit=30)
    assert reader.read_window(CHAT, START, END).safe_to_advance
    assert calls == [[str(executable.resolve()), "history", CHAT, "--before", str(START - 1),
                      "--after", str(END + 1), "--limit", "30", "--json", "--with-meta"]]
    with pytest.raises(ValueError, match="explicit"):
        reader.read_window("张三", START, END)


@pytest.mark.parametrize("mode", ["timeout", "nonzero", "invalid_json"])
def test_transport_failure_does_not_look_like_empty_history_or_leak_data(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "wx.exe"
    executable.touch()
    (tmp_path / 'config.json').write_text('{}', encoding='utf-8')

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if mode == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 1 if mode == "nonzero" else 0,
                                           "not JSON", "sensitive chat content")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(WeChatReadError) as caught:
        WxHistoryReader(executable, runtime_dir=tmp_path).read_window(CHAT, START, END)
    assert "sensitive" not in str(caught.value)


def test_missing_runtime_configuration_is_not_silently_read_from_elsewhere(tmp_path):
    executable = tmp_path / 'wx.exe'
    executable.touch()
    with pytest.raises(ValueError, match='initialized private runtime'):
        WxHistoryReader(executable, runtime_dir=tmp_path)
