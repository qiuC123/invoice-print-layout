"""Synthetic device/v1 computer, bound to loopback; never contacts a real ledger.

Run: python android/tests/fake_computer.py --state android/build/fake-computer.json
Test controls are deliberately outside /device/v1 and only exist in this fake.
"""
import argparse
import base64
import hashlib
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CATEGORIES = ["材料采购", "酒店", "高铁", "外卖", "餐饮", "打车", "快递", "打印", "其他"]
KEY = "A" * 43
LOCK = threading.Lock()
STATE = {"mode": "normal", "entries": {}, "attempts": [], "paired": 0}
STATE_PATH = None
ADVERTISED_URL = "http://10.0.2.2:8769"


def persist():
    if STATE_PATH:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")


class Server(ThreadingHTTPServer):
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.dispatch(None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 29 * 1024 * 1024:
            self.respond(413, {"error": "图片过大", "code": "too_large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            self.respond(400, {"error": "JSON无效", "code": "invalid"})
            return
        self.dispatch(body)

    def dispatch(self, body):
        with LOCK:
            self.route(body)
            persist()

    def route(self, body):
        if self.path == "/__control" and body:
            STATE["mode"] = body["mode"]
            self.respond(200, {"ok": True})
            return
        if self.path == "/__state":
            self.respond(200, STATE)
            return
        if self.headers.get("Origin"):
            self.respond(403, {"error": "不接受浏览器来源", "code": "origin"})
            return
        if STATE["mode"] == "disconnected":
            self.close_connection = True
            self.connection.close()
            return
        addresses = [ADVERTISED_URL]
        if self.path == "/device/v1/pair" and body:
            if body.get("code") != "12345678":
                self.respond(400, {"error": "配对码无效或已过期", "code": "bad_code"})
            else:
                STATE["paired"] += 1
                self.respond(200, {"device_id": "synthetic-device", "device_key": KEY,
                                    "server_name": "验收电脑（模拟）", "urls": addresses})
            return
        if self.headers.get("Authorization") != "Bearer " + KEY or STATE["mode"] == "unpaired":
            self.respond(401, {"error": "设备已撤销", "code": "unpaired"})
            return
        if self.path == "/device/v1/catalog":
            if STATE["mode"] == "catalog_failure":
                self.respond(503, {"error": "合成目录失败", "code": "unavailable"})
                return
            self.respond(200, {"server_time": datetime.now().astimezone().isoformat(), "urls": addresses,
                                "categories": CATEGORIES, "projects": [
                                    {"id": "synthetic-project", "name": "收票验收项目", "start_date": "2020-01-01", "end_date": "",
                                     "main_person_id": "lead", "people": [{"id": "lead", "label": "合成负责人"}, {"id": "other", "label": "合成人员"}]},
                                    {"id": "synthetic-other", "name": "另一个合成项目", "start_date": "", "end_date": "",
                                     "main_person_id": "other-lead", "people": [{"id": "other-lead", "label": "另一负责人"}]}]})
            return
        if self.path == "/device/v1/receipts" and body:
            cid = body.get("client_id")
            STATE["attempts"].append(cid)
            if cid in STATE["entries"]:
                self.respond(200, STATE["entries"][cid]["reply"])
                return
            if STATE["mode"] in ("project_unavailable", "person_unavailable"):
                self.respond(409, {"error": "项目或所属人已变化，请重新选择", "code": STATE["mode"]})
                return
            if STATE["mode"] == "invalid":
                self.respond(400, {"error": "合成字段不合法", "code": "invalid"})
                return
            try:
                data = base64.b64decode(body.pop("data"), validate=True)
                assert data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG"
                assert body["project_id"] in ("synthetic-project", "synthetic-other")
                assert body["category"] in CATEGORIES
                assert body["amount_cents"] is None or type(body["amount_cents"]) is int and body["amount_cents"] >= 0
            except (ValueError, AssertionError, KeyError):
                self.respond(400, {"error": "票据字段无效", "code": "invalid"})
                return
            digest = hashlib.sha256(data).hexdigest()
            duplicate = any(e["sha256"] == digest for e in STATE["entries"].values())
            reply = {"status": "duplicate" if duplicate else "received", "entry_id": "entry-" + cid}
            STATE["entries"][cid] = {"fields": body, "sha256": digest, "bytes": len(data), "reply": reply}
            if STATE["mode"] == "drop_receipt_reply":
                STATE["mode"] = "normal"
                self.close_connection = True
                self.connection.close()
                return
            self.respond(200, reply)
            return
        self.respond(404, {"error": "接口不存在", "code": "not_found"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--url", default=ADVERTISED_URL)
    args = parser.parse_args()
    STATE_PATH = args.state
    ADVERTISED_URL = args.url
    if STATE_PATH and STATE_PATH.is_file():
        STATE.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        STATE["mode"] = "normal"
    Server((args.host, 8769), Handler).serve_forever()
