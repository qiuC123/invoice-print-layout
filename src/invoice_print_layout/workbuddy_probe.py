"""Opt-in WorkBuddy local-assistant connectivity probe using synthetic text only.

No credentials are acquired here, no photo paths are accepted, and no background
poller is installed. A checked reply is not proof of native task completion.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from invoice_print_layout.construction_reports import read_object, run_lock, write_json

Json = dict[str, Any]
Transport = Callable[[str, str, Json | None], Json]
BASE_URL = "https://www.workbuddy.cn/openapi/v2"
TOKEN_ENV = "WORKBUDDY_OPENAPI_ACCESS_TOKEN"
RECORDS = [
    {"photo_id": "SYN-001", "provided_role": "项目经理"},
    {"photo_id": "SYN-003", "provided_role": "AV搭建工人"},
    {"photo_id": "SYN-004", "provided_role": "技术工程师"},
]


class ProbeError(ValueError):
    """A safe, machine-readable failure; never includes server bodies or tokens."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def request_api(method: str, path: str, payload: Json | None = None) -> Json:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ProbeError("needs_authorization")
    if method not in {"GET", "POST"} or not (
        path == "/localassistant" or path == "/localassistant/message"
        or (method == "GET" and path.startswith("/localassistant/message?"))
    ):
        raise ProbeError("unsupported_endpoint")
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    try:
        req = Request(BASE_URL + path, data=body, method=method, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "Content-Type": "application/json",
        })
        with build_opener(NoRedirect()).open(req, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ProbeError("response_too_large")
        value = json.loads(raw)
    except HTTPError as exc:
        code = "needs_authorization" if exc.code in (401, 403) else f"http_{exc.code}"
        raise ProbeError(code) from None
    except (URLError, OSError, ValueError):
        raise ProbeError("transport_or_response_error") from None
    if not isinstance(value, dict) or type(value.get("code")) is not int:
        raise ProbeError("invalid_api_envelope")
    if value["code"] != 0:
        raise ProbeError("api_rejected")
    if not isinstance(value.get("data"), dict):
        raise ProbeError("invalid_api_data")
    return dict(value["data"])


def online(api: Transport = request_api) -> Json:
    data = api("GET", "/localassistant", None)
    if type(data.get("online")) is not bool:
        raise ProbeError("invalid_online_status")
    return {"status": "online" if data["online"] else "offline"}


def prepare(folder: Path) -> Json:
    with run_lock(folder):
        if (folder / "state.json").exists() or (folder / "request.json").exists():
            raise ProbeError("probe_already_exists")
        request = {"schema_version": 1, "synthetic": True,
                   "task_id": "wb-probe-" + uuid4().hex,
                   "nonce": uuid4().hex, "records": RECORDS}
        write_json(folder / "request.json", request)
        state = {"schema_version": 1, "status": "prepared",
                 "request_sha256": digest(request)}
        write_json(folder / "state.json", state)
        return state


def load(folder: Path) -> tuple[Json, Json]:
    state = read_object(folder / "state.json")
    request = read_object(folder / "request.json")
    if digest(request) != state.get("request_sha256"):
        raise ProbeError("request_changed")
    if request.get("synthetic") is not True or request.get("records") != RECORDS:
        raise ProbeError("synthetic_fixture_required")
    if set(request) != {"schema_version", "synthetic", "task_id", "nonce", "records"}:
        raise ProbeError("unexpected_request_fields")
    if state.get("status") == "validated":
        receipt = read_object(folder / "result.json")
        if digest(receipt) != state.get("result_sha256"):
            raise ProbeError("result_changed")
        validate_reply(receipt, request)
    return state, request


def reply_header(request: Json) -> Json:
    return {"task_id": request["task_id"], "nonce": request["nonce"],
            "input_sha256": digest(request), "synthetic": True, "stage": "wb_roles"}


def prompt_for(request: Json) -> str:
    return (
        "这是用户批准的纯合成文字联通测试。只根据内联提供的岗位文字返回JSON。"
        "不要调用工具、读取或修改文件、检索网络、启动子Agent、识别人脸或跨照片匹配。"
        "不需要分析工时。photo_id只是虚构记录号。\n"
        "完整回复必须是单个JSON对象，不加Markdown。字段如下，并增加records数组：\n"
        + json.dumps(reply_header(request), ensure_ascii=False)
        + '\nrecords每条只含photo_id和role，role原样使用provided_role。输入：\n'
        + json.dumps(request["records"], ensure_ascii=False)
    )


def dispatch(folder: Path, api: Transport = request_api) -> Json:
    with run_lock(folder):
        state, request = load(folder)
        # A crash while sending cannot prove the remote end did not receive it.
        if state["status"] in {"sending", "dispatch_unknown"}:
            raise ProbeError("dispatch_unknown_do_not_resend")
        if state["status"] in {"waiting", "validated"}:
            return state
        if state["status"] != "prepared":
            raise ProbeError("unexpected_state")
        if online(api)["status"] != "online":
            raise ProbeError("assistant_offline")
        state["status"] = "sending"
        write_json(folder / "state.json", state)
        try:
            response = api("POST", "/localassistant/message",
                           {"content": prompt_for(request), "msg_type": "text"})
            message_id = response.get("message_id")
            if not isinstance(message_id, str) or not message_id.strip():
                raise ProbeError("missing_message_id")
        except Exception:
            # Never log exceptions carrying response bodies, headers or secrets.
            state["status"] = "dispatch_unknown"
            write_json(folder / "state.json", state)
            raise ProbeError("dispatch_unknown_do_not_resend") from None
        state.update(status="waiting", message_id=message_id)
        write_json(folder / "state.json", state)
        return state


def validate_reply(value: Any, request: Json) -> None:
    header = reply_header(request)
    if not isinstance(value, dict) or set(value) != set(header) | {"records"}:
        raise ProbeError("invalid_reply_fields")
    if value.get("synthetic") is not True or any(value.get(k) != v for k, v in header.items()):
        raise ProbeError("unrelated_reply")
    records = value["records"]
    expected = {row["photo_id"]: row["provided_role"] for row in RECORDS}
    if not isinstance(records, list) or len(records) != len(expected):
        raise ProbeError("invalid_records")
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != {"photo_id", "role"}:
            raise ProbeError("invalid_record_fields")
        key = row["photo_id"]
        if not isinstance(key, str) or key in seen or key not in expected or row["role"] != expected[key]:
            raise ProbeError("invalid_role_or_id")
        seen.add(key)


def poll(folder: Path, api: Transport = request_api) -> Json:
    """One bounded GET; keep the sent-message cursor to avoid losing partial replies."""
    with run_lock(folder):
        state, request = load(folder)
        if state["status"] == "validated":
            return state
        if state["status"] != "waiting":
            raise ProbeError("probe_not_waiting")
        data = api("GET", "/localassistant/message?" + urlencode({"message_id": state["message_id"]}), None)
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ProbeError("invalid_messages")
        matches: list[tuple[str, Json]] = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("msg_type") != "text":
                continue
            content = message.get("content")
            if not isinstance(content, list) or not all(isinstance(part, str) for part in content):
                continue
            try:
                value = json.loads("".join(content))
                validate_reply(value, request)
            except (ValueError, TypeError):
                continue
            response_id = message.get("message_id")
            if isinstance(response_id, str) and response_id and response_id != state["message_id"]:
                matches.append((response_id, value))
        if not matches:
            return state
        if len({digest(value) for _, value in matches}) > 1:
            raise ProbeError("conflicting_replies")
        response_id, result = matches[0]
        write_json(folder / "result.json", result)
        state.update(status="validated", response_message_id=response_id,
                     result_sha256=digest(result), native_task_completion_verified=False)
        write_json(folder / "state.json", state)
        return state


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy官方本地助理合成联通探针")
    parser.add_argument("action", choices=("online", "prepare", "dispatch", "poll", "status"))
    parser.add_argument("--folder", type=Path)
    args = parser.parse_args()
    if args.action != "online" and args.folder is None:
        parser.error("this action requires --folder")
    try:
        if args.action == "online":
            result = online()
        elif args.action == "status":
            result, _ = load(args.folder)
        else:
            result = {"prepare": prepare, "dispatch": dispatch, "poll": poll}[args.action](args.folder)
    except (ProbeError, OSError, ValueError) as exc:
        # Only our enumerated errors are safe to show; filesystem/JSON errors may contain data.
        reason = str(exc) if isinstance(exc, ProbeError) else "local_input_error"
        print(json.dumps({"status": "blocked", "reason": reason}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
