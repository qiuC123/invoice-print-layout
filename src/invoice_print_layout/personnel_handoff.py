"""Offline WorkBuddy role handoff; Codex owns time grouping and corrections."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable

from invoice_print_layout.construction_reports import (
    NeedsReview, read_object, run_lock, safe_key, sha, write_json,
)

Json = dict[str, Any]
Renderer = Callable[[Path, Json], None]
ROLES = ("项目经理", "技术工程师", "搭建工人", "AV技术工程师", "AV搭建工人", "待确认")
HEADERS = ("photo_id", "role", "basis", "status")
PROJECT_LABELS = {"included": "当前项目", "pending": "归属待核", "excluded": "异地排除"}
HERE = Path(__file__).parent


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NeedsReview(f"{field}需要非空文字")
    return value.strip()


def _catalog(folder: Path) -> Json:
    catalog = read_object(folder / "catalog.json")
    if catalog.get("schema_version") != 1:
        raise NeedsReview("catalog版本不支持")
    safe_key(_text(catalog.get("project_id"), "project_id"))
    safe_key(_text(catalog.get("batch_id"), "batch_id"))
    _text(catalog.get("project_title"), "project_title")
    if catalog.get("timezone") != "Asia/Shanghai":
        raise NeedsReview("拍摄时间须明确为Asia/Shanghai")
    photos = catalog.get("photos")
    if not isinstance(photos, list) or not photos:
        raise NeedsReview("photos需要至少一条照片记录")
    seen: set[str] = set()
    for photo in photos:
        if not isinstance(photo, dict):
            raise NeedsReview("照片记录须为对象")
        photo_id = safe_key(_text(photo.get("photo_id"), "photo_id"))
        if photo_id in seen:
            raise NeedsReview(f"照片ID重复：{photo_id}")
        seen.add(photo_id)
        day = _text(photo.get("date"), "date")
        stamp = _text(photo.get("photo_time"), "photo_time")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise NeedsReview(f"日期格式错误：{photo_id}")
        date.fromisoformat(day)
        if not re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", stamp):
            raise NeedsReview(f"时间格式错误：{photo_id}")
        time.fromisoformat(stamp)
        if photo.get("project_status") not in PROJECT_LABELS:
            raise NeedsReview(f"项目归属状态无效：{photo_id}")
        relative = Path(_text(photo.get("path"), "path"))
        path = (folder / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(folder.resolve()):
            raise NeedsReview(f"照片路径越出交接包：{photo_id}")
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise NeedsReview(f"照片文件类型不支持：{photo_id}")
        digest = _text(photo.get("sha256"), "sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or sha(path) != digest:
            raise NeedsReview(f"照片哈希不一致：{photo_id}")
        for field in ("sent_at", "location", "event_type", "note"):
            if field in photo and not isinstance(photo[field], str):
                raise NeedsReview(f"{field}须为文字：{photo_id}")
        if photo.get("sent_at"):
            sent = datetime.fromisoformat(photo["sent_at"])
            if sent.utcoffset() is None:
                raise NeedsReview(f"发群时间须为带时区的ISO时间：{photo_id}")
    return catalog


def _labels(folder: Path, catalog: Json) -> dict[str, Json]:
    with (folder / "classifications.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(HEADERS):
            raise NeedsReview("分类CSV列必须为photo_id,role,basis,status")
        result: dict[str, Json] = {}
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise NeedsReview("分类CSV存在缺列或多余列")
            photo_id = _text(row["photo_id"], "photo_id")
            if photo_id in result:
                raise NeedsReview(f"分类ID重复：{photo_id}")
            role = _text(row["role"], "role")
            status = _text(row["status"], "status")
            if role not in ROLES or status not in {"classified", "pending"}:
                raise NeedsReview(f"岗位或确认状态无效：{photo_id}")
            if (role == "待确认") != (status == "pending"):
                raise NeedsReview(f"待确认岗位与pending状态须对应：{photo_id}")
            result[photo_id] = {"role": role, "basis": _text(row["basis"], "basis"), "status": status}
    ids = {photo["photo_id"] for photo in catalog["photos"]}
    if set(result) != ids:
        raise NeedsReview(f"分类ID与照片不一致；缺少{sorted(ids - set(result))}，多出{sorted(set(result) - ids)}")
    return result


def _identity(folder: Path, catalog: Json) -> Json:
    return {"schema_version": 1, "state": "complete", "project_id": catalog["project_id"],
            "batch_id": catalog["batch_id"], "photo_count": len(catalog["photos"]),
            "catalog_sha256": sha(folder / "catalog.json"),
            "classifications_sha256": sha(folder / "classifications.csv")}


def prepare_package(catalog_path: Path, destination: Path) -> Json:
    """Freeze a Codex-verified catalog and bytes; never overwrite a package."""
    source = catalog_path.resolve()
    if source.name != "catalog.json":
        raise NeedsReview("源目录的核验清单须命名catalog.json")
    catalog = _catalog(source.parent)
    target = destination.resolve()
    if target.exists():
        raise NeedsReview("交接目录已存在，请保留旧包并使用新的批次目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        (temporary / "photos").mkdir(parents=True)
        frozen: list[Json] = []
        for photo in catalog["photos"]:
            original = source.parent / photo["path"]
            relative = f"photos/{photo['photo_id']}{original.suffix.lower()}"
            shutil.copyfile(original, temporary / relative)
            if sha(temporary / relative) != photo["sha256"]:
                raise NeedsReview(f"复制期间原件变化：{photo['photo_id']}")
            frozen.append({**photo, "path": relative})
        write_json(temporary / "catalog.json", {**catalog, "photos": frozen})
        with (temporary / "classifications.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADERS)
            writer.writerows((photo["photo_id"], "待确认", "尚未分类", "pending") for photo in frozen)
        (temporary / "交接说明.md").write_text(
            "仅填写classifications.csv中的岗位、依据、状态。不要修改catalog或照片。\n"
            "岗位：" + " / ".join(ROLES) + "。classified表示已有岗位标注，pending必须配待确认。\n"
            "完成后调用daily seal-roles；COMPLETE.json写入后不再修改此批文件。\n",
            encoding="utf-8")
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"state": "ready_for_classification", "package": str(target), "photo_count": len(frozen)}


def seal_package(folder: Path) -> Json:
    """Called after the supplied role CSV is finished; no recognition."""
    folder = folder.resolve()
    catalog = _catalog(folder)
    _labels(folder, catalog)
    marker = _identity(folder, catalog)
    existing = folder / "COMPLETE.json"
    if existing.exists() and read_object(existing) != marker:
        raise NeedsReview("已完成交接包发生变化，请创建新批次，不覆盖完成标记")
    if not existing.exists():
        write_json(existing, marker)
    return marker


def _sealed(folder: Path) -> tuple[Json, dict[str, Json], Json]:
    marker_path = folder / "COMPLETE.json"
    if not marker_path.is_file():
        raise NeedsReview("交接未完成：缺少COMPLETE.json")
    catalog_bytes = (folder / "catalog.json").read_bytes()
    labels_bytes = (folder / "classifications.csv").read_bytes()
    catalog = _catalog(folder)
    labels = _labels(folder, catalog)
    marker = _identity(folder, catalog)
    if read_object(marker_path) != marker or catalog_bytes != (folder / "catalog.json").read_bytes() or labels_bytes != (folder / "classifications.csv").read_bytes():
        raise NeedsReview("完成标记与交接文件不一致，请交回完整新批次")
    return catalog, labels, marker


def _corrections(path: Path, catalog: Json) -> tuple[Json, dict[str, Json]]:
    log = read_object(path) if path.exists() else {"schema_version": 1, "project_id": catalog["project_id"], "history": []}
    if log.get("schema_version") != 1 or log.get("project_id") != catalog["project_id"] or not isinstance(log.get("history"), list):
        raise NeedsReview("用户纠正记录格式或项目不一致")
    photos = {photo["photo_id"]: photo for photo in catalog["photos"]}
    latest: dict[str, Json] = {}
    for item in log["history"]:
        if not isinstance(item, dict) or item.get("role") not in ROLES:
            raise NeedsReview("用户纠正记录无效")
        _text(item.get("basis"), "纠正依据")
        photo_id = _text(item.get("photo_id"), "纠正photo_id")
        latest[photo_id] = item
    applied: dict[str, Json] = {}
    for photo_id, item in latest.items():
        if photo_id in photos:
            if item.get("photo_sha256") != photos[photo_id]["sha256"]:
                raise NeedsReview(f"纠正对应的照片已变化：{photo_id}，请使用新的照片ID")
            applied[photo_id] = item
    return log, applied


def correct_role(folder: Path, output_root: Path, photo_id: str, role: str, basis: str, note: str = "") -> Json:
    """Persist an explicitly user-supplied role, tied to the original bytes."""
    catalog, _, _ = _sealed(folder.resolve())
    if role not in ROLES:
        raise NeedsReview("纠正岗位不在支持列表中")
    photo = next((p for p in catalog["photos"] if p["photo_id"] == photo_id), None)
    if photo is None:
        raise NeedsReview("纠正照片ID不存在")
    project = output_root.resolve() / catalog["project_id"]
    with run_lock(project):
        path = project / "user-corrections.json"
        log, _ = _corrections(path, catalog)
        correction = {"photo_id": photo_id, "photo_sha256": photo["sha256"], "role": role,
                      "basis": _text(basis, "用户确认依据"), "note": note,
                      "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        log["history"].append(correction)
        write_json(path, log)
    return {"state": "correction_saved", "correction": correction, "path": str(path)}


def _ledger(catalog: Json, labels: dict[str, Json], corrections: dict[str, Json]) -> Json:
    records: list[Json] = []
    for photo in sorted(catalog["photos"], key=lambda p: (p["date"], time.fromisoformat(p["photo_time"]), p["photo_id"])):
        label = labels[photo["photo_id"]]
        override = corrections.get(photo["photo_id"])
        effective = override or label
        hour = int(photo["photo_time"][:2])
        records.append({**photo, "period": "上午" if hour < 12 else "下午" if hour < 18 else "晚间",
                        "project_status_label": PROJECT_LABELS[photo["project_status"]],
                        "path": f"photos/{photo['photo_id']}{Path(photo['path']).suffix.lower()}",
                        "role": effective["role"], "role_basis": effective["basis"],
                        "role_status": "user_confirmed" if override and override["role"] != "待确认" else label["status"] if not override else "pending",
                        "source_role": label["role"], "source_role_basis": label["basis"],
                        "user_note": override.get("note", "") if override else ""})
    groups: dict[tuple[str, str, str], Json] = {}
    for photo in records:
        key = (photo["date"], photo["role"], photo["project_status"])
        group = groups.setdefault(key, {"date": key[0], "role": key[1], "project_status": key[2],
                                        "project_status_label": PROJECT_LABELS[key[2]], "photo_ids": [], "times": []})
        group["photo_ids"].append(photo["photo_id"])
        group["times"].append(photo["photo_time"])
    ordered = sorted(groups.values(), key=lambda g: (g["date"], ROLES.index(g["role"]), g["project_status"]))
    return {"schema_version": 1, "project_id": catalog["project_id"], "project_title": catalog["project_title"],
            "batch_id": catalog["batch_id"], "timezone": catalog["timezone"],
            "scope": "照片事件与岗位时间；照片张数不等于人数，拍照跨度不作为个人工时。",
            "records": records, "groups": ordered, "role_photo_counts": dict(Counter(p["role"] for p in records)),
            "project_photo_counts": dict(Counter(p["project_status"] for p in records)),
            "applied_corrections": list(corrections.values())}


def _csv_cell(value: Any) -> Any:
    # CSV consumers may interpret free text as a formula; retain JSON as the exact source.
    return "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value


def _outputs(attempt: Path, folder: Path, catalog: Json, ledger: Json) -> None:
    (attempt / "photos").mkdir()
    sources = {photo["photo_id"]: photo["path"] for photo in catalog["photos"]}
    for photo in ledger["records"]:
        # Frozen catalogs use deterministic paths, but externally supplied packages may not.
        original = folder / sources[photo["photo_id"]]
        shutil.copyfile(original, attempt / photo["path"])
        if sha(attempt / photo["path"]) != photo["sha256"]:
            raise NeedsReview(f"接收时照片变化：{photo['photo_id']}")
    write_json(attempt / "time-records.json", ledger)
    fields = ["photo_id", "date", "photo_time", "period", "role", "project_status_label", "role_status",
              "role_basis", "source_role", "sent_at", "location", "event_type", "note", "user_note", "path", "sha256"]
    with (attempt / "time-records.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows([_csv_cell(photo.get(field, "")) for field in fields] for photo in ledger["records"])
    template = (HERE / "web" / "personnel-preview.html").read_text(encoding="utf-8")
    payload = json.dumps(ledger, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    (attempt / "preview.html").write_text(template.replace("__TITLE__", html.escape(ledger["project_title"])).replace("__DATA__", payload), encoding="utf-8")


def render_workbook(attempt: Path, runtime: Json) -> None:
    for key in ("node_executable", "artifact_module"):
        if not Path(_text(runtime.get(key), key)).is_file():
            raise NeedsReview(f"表格运行环境不存在：{key}")
    result = subprocess.run([runtime["node_executable"], str(HERE / "personnel_handoff_render.mjs"),
                             str(attempt), runtime["artifact_module"]], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=240)
    if result.returncode:
        raise NeedsReview("表格生成失败：" + result.stderr[-4000:])


def _verify_receipt(run: Path, receipt: Json) -> None:
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise NeedsReview("接收回执缺少文件校验")
    for name, digest in artifacts.items():
        path = (run / name).resolve()
        if not path.is_relative_to(run.resolve()) or not path.is_file() or sha(path) != digest:
            raise NeedsReview(f"旧输出被修改或缺失：{name}；请保留证据并使用新输出目录")


def receive_package(folder: Path, output_root: Path, runtime: Json, *, renderer: Renderer | None = None) -> Json:
    folder = folder.resolve()
    catalog, labels, identity = _sealed(folder)
    project = output_root.resolve() / catalog["project_id"]
    with run_lock(project):
        catalog, labels, identity = _sealed(folder)
        correction_path = project / "user-corrections.json"
        log, overrides = _corrections(correction_path, catalog)
        code_hashes = {p.name: sha(p) for p in (Path(__file__), HERE / "personnel_handoff_render.mjs", HERE / "web" / "personnel-preview.html")}
        runtime_identity = {key: runtime.get(key) for key in ("node_executable", "artifact_module")}
        if isinstance(runtime.get("artifact_module"), str) and Path(runtime["artifact_module"]).is_file():
            runtime_identity["artifact_module_sha256"] = sha(Path(runtime["artifact_module"]))
        fingerprint = hashlib.sha256(json.dumps({"identity": identity, "corrections": log, "code": code_hashes, "runtime": runtime_identity}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        run = project / catalog["batch_id"] / fingerprint[:24]
        run.mkdir(parents=True, exist_ok=True)
        receipt_path = run / "receipt.json"
        if receipt_path.exists():
            receipt = read_object(receipt_path)
            _verify_receipt(run, receipt)
            return {**receipt, "run": str(run), "reused": True}
        attempt = run / ("attempt-" + uuid.uuid4().hex)
        attempt.mkdir()
        write_json(run / "state.json", {"state": "running", "attempt": attempt.name, "fingerprint": fingerprint})
        try:
            inputs = attempt / "inputs"
            inputs.mkdir()
            for filename in ("catalog.json", "classifications.csv", "COMPLETE.json"):
                shutil.copyfile(folder / filename, inputs / filename)
            if read_object(inputs / "COMPLETE.json") != identity or sha(inputs / "catalog.json") != identity["catalog_sha256"] or sha(inputs / "classifications.csv") != identity["classifications_sha256"]:
                raise NeedsReview("冻结时交接文件发生变化")
            write_json(inputs / "user-corrections.json", log)
            ledger = _ledger(catalog, labels, overrides)
            _outputs(attempt, folder, catalog, ledger)
            (renderer or render_workbook)(attempt, runtime)
            workbook = attempt / "time-records.xlsx"
            if not zipfile.is_zipfile(workbook) or not (attempt / "QA" / "workbook-check.json").is_file():
                raise NeedsReview("表格或表格核验回执缺失")
            try:
                final_identity = _sealed(folder)[2]
            except (ValueError, OSError) as exc:
                raise NeedsReview("生成期间交接文件发生变化：" + str(exc)) from exc
            if final_identity != identity:
                raise NeedsReview("生成期间交接文件发生变化")
            artifacts = {p.relative_to(run).as_posix(): sha(p) for p in attempt.rglob("*") if p.is_file()}
            receipt = {"schema_version": 1, "state": "generated", "fingerprint": fingerprint,
                       "project_id": catalog["project_id"], "batch_id": catalog["batch_id"],
                       "photo_count": len(ledger["records"]), "role_photo_counts": ledger["role_photo_counts"],
                       "project_photo_counts": ledger["project_photo_counts"], "corrections_applied": list(overrides),
                       "output": str(attempt), "artifacts": artifacts,
                       "preview_validation": "静态结构与数据核验；浏览器显示需人工查看"}
            write_json(run / "state.json", {"state": "generated", "attempt": attempt.name, "fingerprint": fingerprint})
            write_json(receipt_path, receipt)
            return {**receipt, "run": str(run), "reused": False}
        except Exception as exc:
            write_json(run / "state.json", {"state": "failed", "attempt": attempt.name, "error": str(exc), "fingerprint": fingerprint})
            raise
