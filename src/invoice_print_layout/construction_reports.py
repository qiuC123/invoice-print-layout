"""Codex-invoked, private construction report runs. No schedules or sends."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.etree import ElementTree as ET

from invoice_print_layout.daily_reports import inputs as work_inputs

Json = dict[str, Any]
Renderer = Callable[[Path, Json], None]


class NeedsReview(ValueError):
    """Evidence is incomplete or conflicts; no report may be generated."""


def read_object(path: Path) -> Json:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise NeedsReview(f"需要JSON对象：{path.name}")
    return value


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def local_path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def safe_key(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise NeedsReview("项目键只能使用字母、数字、下划线和连字符")
    return value


@contextlib.contextmanager
def run_lock(folder: Path) -> Iterator[None]:
    """OS-released lock survives process interruption without stale PID files."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise NeedsReview("该项目日期正在运行，请读取状态后再继续") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)


def project_config(registry_path: Path, project: str, report_date: str | None = None) -> tuple[Json, Path, Json]:
    if report_date is not None:
        try:
            if date.fromisoformat(report_date).isoformat() != report_date:
                raise ValueError("non-canonical date")
        except ValueError as exc:
            raise NeedsReview("报告日期必须为合法的YYYY-MM-DD") from exc
    registry = read_object(registry_path)
    projects = registry.get("projects", {})
    matches = [(key, item) for key, item in projects.items()
               if project == key or project in item.get("aliases", [])]
    if len(matches) != 1:
        raise NeedsReview("项目不存在或名称不唯一，请使用已登记的项目键")
    key, entry = matches[0]
    safe_key(key)
    config_name = entry["config"]
    if report_date is not None:
        by_date = entry.get("config_by_date", {})
        if not isinstance(by_date, dict):
            raise NeedsReview("config_by_date必须为日期到配置路径的映射")
        # Exact keys only: neither the latest nor nearest historical source is
        # a safe default for a different day's project/chat configuration.
        config_name = by_date.get(report_date, config_name)
    if not isinstance(config_name, str) or not config_name.strip():
        raise NeedsReview("项目配置路径必须为非空字符串")
    path = local_path(registry_path.parent, config_name)
    config = read_object(path)
    if config.get("project_id") != key or config.get("schema_version") != 1:
        raise NeedsReview("项目配置身份或版本不匹配")
    return config, path, registry


def _confirmed(item: Json, label: str) -> None:
    if item.get("status") != "confirmed" or not str(item.get("evidence", "")).strip():
        raise NeedsReview(f"待确认：{label}，请记录依据")


def _message_time(message: Json, report_date: str) -> datetime:
    value = datetime.fromisoformat(str(message.get("time", "")))
    if value.utcoffset() != timedelta(hours=8) or value.date().isoformat() != report_date:
        raise NeedsReview("消息日期或时区不匹配，需中国时区的指定当天消息")
    return value


def closeout_batches(messages: list[Json], report_date: str, marker: str,
                     gap_seconds: int = 180) -> list[Json]:
    """Find adjacent image runs followed by a closeout marker; never pick latest."""
    by_time: dict[datetime, list[Json]] = {}
    for message in messages:
        by_time.setdefault(_message_time(message, report_date), []).append(message)
    for group in by_time.values():
        if len(group) > 1 and (any("sort_seq" not in m for m in group)
                              or len({m.get("sort_seq") for m in group}) != len(group)):
            raise NeedsReview("同秒消息顺序不明，需补齐可靠的排序证据")
    ordered = sorted(messages, key=lambda m: (_message_time(m, report_date), int(m.get("sort_seq", 0))))
    candidates: list[Json] = []
    current: list[str] = []
    previous: datetime | None = None
    seen: set[str] = set()
    for message in ordered:
        message_id = str(message["message_id"])
        if message_id in seen:
            raise NeedsReview("消息ID重复，需先消除消息来源歧义")
        seen.add(message_id)
        stamp = _message_time(message, report_date)
        if previous and (stamp - previous).total_seconds() > gap_seconds:
            current = []
        if message.get("type") == 3:
            current.append(message_id)
        else:
            if marker and marker in str(message.get("text", "")) and current:
                candidates.append({"message_ids": current[:], "closeout_message_id": message_id})
            current = []
        previous = stamp
    return candidates


def validate_inputs(config: Json, config_path: Path, manifest: Json,
                    manifest_path: Path, report_date: str) -> tuple[Json, list[Path]]:
    day = date.fromisoformat(report_date)
    if manifest.get("schema_version") != 1 or manifest.get("report_date") != report_date:
        raise NeedsReview("运行清单版本或报告日期不匹配")
    if manifest.get("project_id") != config["project_id"]:
        raise NeedsReview("运行清单与项目配置不匹配")
    if manifest.get("mode") not in {"replay", "daily"}:
        raise NeedsReview("请明确历史重放(replay)或当日制作(daily)")
    if manifest.get("issues"):
        raise NeedsReview("采集尚有缺项：" + "; ".join(map(str, manifest["issues"])))
    source = config["source"]
    if manifest.get("source_id") != source["source_id"]:
        raise NeedsReview("清单会话不属于项目配置的来源")
    messages_path = local_path(manifest_path.parent, manifest["messages_path"])
    messages = json.loads(messages_path.read_text(encoding="utf-8-sig"))
    if not isinstance(messages, list) or not messages:
        raise NeedsReview("缺少当天原始消息证据")
    candidates = closeout_batches(messages, report_date, source.get("closeout_marker", "搭建结束"))
    batch = manifest.get("batch", {})
    _confirmed(batch, "收工批次及项目归属")
    message_ids = batch.get("message_ids", [])
    if not message_ids or len(set(message_ids)) != len(message_ids):
        raise NeedsReview("收工批次为空或包含重复图片消息")
    message_map = {str(m["message_id"]): m for m in messages}
    if any(mid not in message_map or message_map[mid]["type"] != 3 for mid in message_ids):
        raise NeedsReview("收工批次含不在当天消息中的图片")
    if source.get("batch_policy", "closeout") == "closeout":
        matched = [item for item in candidates if item["message_ids"] == message_ids
                   and item["closeout_message_id"] == batch.get("closeout_message_id")]
        if len(matched) != 1:
            raise NeedsReview("所选收工批次与连续图片/收工消息不匹配")
        if len(candidates) > 1 and not batch.get("selection_evidence"):
            raise NeedsReview("存在多个收工批次，需要明确选择依据")
    elif source.get("batch_policy") == "explicit":
        # Private chats may contain no closeout text, so require a confirmed exact run.
        ordered_ids = [str(m["message_id"]) for m in sorted(messages, key=lambda m: (_message_time(m, report_date), int(m.get("sort_seq", 0))))]
        offset = ordered_ids.index(message_ids[0])
        if ordered_ids[offset:offset + len(message_ids)] != message_ids:
            raise NeedsReview("人工指定批次不是连续消息")
        if not batch.get("selection_evidence"):
            raise NeedsReview("私聊收工批次需要用户指定或逐图核验依据")
    else:
        raise NeedsReview("未知收工批次规则")
    photos = manifest.get("photos", [])
    if sorted(str(p.get("message_id")) for p in photos) != sorted(message_ids):
        raise NeedsReview("收工批次照片未补齐或存在多余照片")
    paths: list[Path] = [config_path, manifest_path, messages_path]
    photo_map: Json = {}
    for photo in photos:
        pid = str(photo["photo_id"])
        if pid in photo_map:
            raise NeedsReview("照片标识重复")
        _confirmed(photo.get("review", {}), f"照片 {pid} 的水印日期/地点/项目")
        if photo.get("captured_date") != report_date or photo.get("project_id") != config["project_id"]:
            raise NeedsReview(f"照片 {pid} 的拍摄日期或项目不匹配")
        if not photo.get("quality"):
            raise NeedsReview(f"照片 {pid} 缺少原图/缓存质量说明")
        path = local_path(manifest_path.parent, photo["path"])
        if sha(path) != photo.get("sha256"):
            raise NeedsReview(f"照片 {pid} 内容已变化")
        photo_map[pid] = {**photo, "path": str(path)}
        paths.append(path)
    facts = manifest.get("facts", {})
    _confirmed(facts, "当天进度、安装内容和图片说明")
    _confirmed(facts.get("stage_review", {}), "当天施工阶段")
    stage_name = facts.get("stage")
    if stage_name not in config.get("stages", {}):
        raise NeedsReview("当天施工阶段尚无确认的模板映射")
    stage = config["stages"][stage_name]
    plan_rule = stage.get("plan_rule", {})
    omit_plan = plan_rule.get("omit_next_day_plan", False)
    if not isinstance(omit_plan, bool):
        raise NeedsReview("omit_next_day_plan 必须为布尔值")
    _confirmed(plan_rule, "最终阶段省略明日计划" if omit_plan else "对应阶段的明日计划")
    if omit_plan and stage.get("is_final_stage") is not True:
        raise NeedsReview("仅明确标记的最终搭建阶段可省略明日计划")
    block_start, block_end = stage["block_start"], stage["block_end"]
    block_count = block_end - block_start + 1
    if block_count != (3 if omit_plan else 4):
        if omit_plan:
            raise NeedsReview("最终阶段无明日计划模板需为进度、内容、照片三页")
        raise NeedsReview("阶段模板需为进度、内容、照片、明日计划四页")
    work = manifest.get("work") or config.get("confirmed_work", {}).get(report_date, {})
    _confirmed(work, "当天起止时间与工时来源")
    if work.get("date") != report_date or work.get("prior_as_of") != (day - timedelta(days=1)).isoformat():
        raise NeedsReview("当天工时或此前累计的截止日期不匹配")
    values = work_inputs({"first_day": config["first_day"]}, {**work, "date": report_date})
    if set(values) & set(facts.get("values", {})):
        raise NeedsReview("当天内容字段不能覆盖日期、天数或工时计算值")
    values.update({str(k): str(v) for k, v in facts.get("values", {}).items()
                   if k not in values})
    values["project_title"] = config["report_title"]
    selected = manifest.get("selected_photos", [])
    slots = stage["image_slots"]
    if len(selected) != len(slots) or len(set(selected)) != len(selected):
        raise NeedsReview("选图数量与模板不符或选图重复")
    if any(pid not in photo_map for pid in selected):
        raise NeedsReview("选图不在已核验的收工批次")
    if len({photo_map[pid]["sha256"] for pid in selected}) != len(selected):
        raise NeedsReview("所选照片内容重复")
    base = manifest.get("base", {})
    _confirmed(base, "项目历史基底")
    if base.get("project_id") != config["project_id"] or base.get("as_of") != (day - timedelta(days=1)).isoformat():
        raise NeedsReview("历史基底所属项目或截止日期错误")
    base_pptx = local_path(manifest_path.parent, base["path"])
    template = local_path(config_path.parent, stage["template"])
    if sha(base_pptx) != base["sha256"] or sha(template) != stage["template_sha256"]:
        raise NeedsReview("基底或阶段模板已变化，需核对后更新配置")
    paths.extend([base_pptx, template])
    for evidence_base, references in [(config_path.parent, config.get("evidence_files", [])),
                                      (manifest_path.parent, manifest.get("evidence_files", []))]:
        for reference in references:
            path = local_path(evidence_base, reference)
            if not path.is_file():
                raise NeedsReview("规则来源文件不存在")
            paths.append(path)
    try:
        edits = [{"id": item["id"], "old": item["old"], "text": item["text"].format_map(values)}
                 for item in stage.get("edits", [])]
        texts = [{"id": item["id"], "text": item["text"].format_map(values)} for item in stage.get("texts", [])]
    except KeyError as exc:
        raise NeedsReview(f"缺少当天内容字段：{exc.args[0]}") from exc
    job: Json = {"stage_template": str(template), "base_pptx": str(base_pptx),
                 "expected_base_slides": base["slides"], "drop_blank_last": base.get("drop_blank_last", False),
                 "insert_after": 3, "block_start": block_start, "block_end": block_end,
                 "edits": edits, "texts": texts,
                 "images": [{"id": slot, "path": photo_map[pid]["path"]} for slot, pid in zip(slots, selected)],
                 "cover_replacements": base.get("cover_replacements", []),
                 "expected_slide_count": base["slides"] - int(base.get("drop_blank_last", False)) + block_count,
                 "report_title": f'{config["report_title"]} · {report_date}',
                 "values": values, "batch": batch, "mode": manifest["mode"],
                 "selected_photo_hashes": [photo_map[pid]["sha256"] for pid in selected]}
    return job, list(dict.fromkeys(paths))


def native_renderer(job_path: Path, runtime: Json) -> None:
    from invoice_print_layout.construction_report_execution import ExecutionError, render_daily
    try:
        render_daily(job_path, runtime)
    except ExecutionError as exc:
        raise NeedsReview(f"日报渲染或PowerPoint验证失败：{exc}") from exc


def verify_package(deck: Path, expected_slides: int, photo_hashes: list[str]) -> Json:
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
          "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    with zipfile.ZipFile(deck) as package:
        slides = [name for name in package.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
        if len(slides) != expected_slides:
            raise NeedsReview("成品PPT页数不符")
        media = {hashlib.sha256(package.read(name)).hexdigest() for name in package.namelist() if name.startswith("ppt/media/")}
        if not set(photo_hashes).issubset(media):
            raise NeedsReview("成品缺少所选照片原字节，停止交付")
        for name in package.namelist():
            if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name):
                if any((node.text or "").strip() for node in ET.fromstring(package.read(name)).findall(".//a:t", ns)):
                    raise NeedsReview("成品仍有制作备注")
    return {"slides": len(slides), "embedded_source_bytes_verified": True, "notes_empty": True}


def run_report(registry_path: Path, project: str, report_date: str, *, manifest_path: Path | None = None,
               output_root: Path | None = None, renderer: Renderer = native_renderer) -> Json:
    registry_path = registry_path.resolve()
    date.fromisoformat(report_date)
    config, config_path, registry = project_config(registry_path, project, report_date)
    if manifest_path is None:
        manifest_path = config_path.parent / "days" / f"{report_date}.json"
    manifest_path = manifest_path.resolve()
    if not manifest_path.is_file():
        return {"state": "needs_input", "project_id": config["project_id"], "date": report_date,
                "missing": ["当天消息与收工批次", "逐图核验及选图", "当天施工内容与阶段", "起止时间和此前累计", "上一日项目历史基底"],
                "manifest_path": str(manifest_path)}
    manifest = read_object(manifest_path)
    job, source_files = validate_inputs(config, config_path, manifest, manifest_path, report_date)
    source_files.append(registry_path)
    from invoice_print_layout.construction_report_execution import execution_code_files
    code_files = [Path(__file__), Path(__file__).with_name("construction_report_render.mjs"),
                  Path(__file__).with_name("construction_report_native.ps1"), *execution_code_files()]
    inventory = [{"path": str(path), "sha256": sha(path)} for path in source_files]
    identity = {"inputs": [item for item in inventory if item["path"] != str(registry_path)],
                "runtime": registry.get("runtime", {}),
                "code": {p.name: sha(p) for p in code_files if p.is_file()}}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    root = output_root.resolve() if output_root else local_path(registry_path.parent, registry["runs_root"])
    folder = root / config["project_id"] / report_date / fingerprint[:20]
    with run_lock(folder):
        state_path = folder / "state.json"
        previous = read_object(state_path) if state_path.is_file() else {}
        if previous.get("fingerprint") not in (None, fingerprint):
            raise NeedsReview("运行标识冲突")
        if previous.get("state") in {"native_verified", "complete"}:
            for item in previous["artifacts"]:
                path = Path(item["path"])
                if not path.is_file() or sha(path) != item["sha256"]:
                    raise NeedsReview("既有产物发生变化；保留原结果，请建立新的修订清单")
            return {**previous, "reused": True}
        snapshot_dir = folder / "inputs"
        snapshot_dir.mkdir(exist_ok=True)
        snapshot_map: dict[str, str] = {}
        for index, item in enumerate(inventory):
            original = Path(item["path"])
            snapshot = snapshot_dir / f'{index:03d}-{item["sha256"][:12]}{original.suffix}'
            if not snapshot.exists():
                shutil.copy2(original, snapshot)
            if sha(snapshot) != item["sha256"]:
                raise NeedsReview("输入快照校验失败")
            snapshot_map[str(original)] = str(snapshot)
        # Render only immutable local copies, protecting source/history even on interruption.
        job["base_pptx"] = snapshot_map[job["base_pptx"]]
        job["stage_template"] = snapshot_map[job["stage_template"]]
        for picture in job["images"]:
            picture["path"] = snapshot_map[picture["path"]]
        attempt = folder / ("attempt-" + uuid.uuid4().hex[:12])
        build = attempt / "build"
        build.mkdir(parents=True)
        job.update(registry.get("runtime", {}))
        job["run_dir"] = str(attempt)
        job_path = build / "job.json"
        write_json(job_path, job)
        state: Json = {"state": "rendering", "project_id": config["project_id"], "date": report_date,
                       "mode": manifest["mode"], "fingerprint": fingerprint, "run_path": str(folder),
                       "attempt_path": str(attempt), "job_path": str(job_path), "inventory": inventory,
                       "snapshots": snapshot_map, "values": job["values"], "batch": job["batch"],
                       "sent": False, "project_delivery_written": False}
        write_json(state_path, state)
        try:
            renderer(job_path, registry.get("runtime", {}))
            deck, preview = attempt / "output/report.pptx", attempt / "output/preview.html"
            package = verify_package(deck, job["expected_slide_count"], job["selected_photo_hashes"])
            receipt = read_object(build / "native-receipt.json")
            if (receipt.get("status") != "passed" or not receipt.get("reopened_final")
                    or receipt.get("slide_count") != job["expected_slide_count"]
                    or not receipt.get("history_preserved") or not receipt.get("notes_cleared")):
                raise NeedsReview("PowerPoint原生验证回执不完整")
            validation_path = build / "validation.json"
            validation = read_object(validation_path)
            execution = validation.get("execution", {})
            formal = execution.get("finalizer_returned") is True and execution.get("fallback_used") is False
            alternate = (execution.get("finalizer_returned") is False and execution.get("fallback_used") is True
                         and validation.get("passed") is True)
            if validation.get("finalSha256") != sha(deck) or not (formal or alternate):
                raise NeedsReview("最终化回执与成品或实际执行路径不符")
            marker_path = build / "operation-marker.json"
            if read_object(marker_path).get("status") != "passed":
                raise NeedsReview("缺少本次编辑开始标记")
            protection_paths = [build / f"window-protection-{mode}.json" for mode in ("compose", "verify")]
            for protection_path in protection_paths:
                protection = read_object(protection_path)
                if protection.get("preserved") is not True or protection.get("quit_called") is not False:
                    raise NeedsReview("已有PowerPoint窗口保护检查未通过")
            pages = [attempt / "output/pages" / f"slide-{i:03d}.png" for i in range(1, job["expected_slide_count"] + 1)]
            artifacts = [deck, preview, *pages, build / "native-receipt.json", job_path,
                         validation_path, marker_path, *protection_paths,
                         *[Path(p) for p in snapshot_map.values()]]
            if any(not path.is_file() for path in artifacts):
                raise NeedsReview("原生页面或HTML预览缺失")
            page_receipts = receipt.get("slides", [])
            if (len(page_receipts) != len(pages)
                    or sorted(item.get("slide", 0) for item in page_receipts) != list(range(1, len(pages) + 1))):
                raise NeedsReview("PowerPoint逐页回执缺页或页码重复")
            for item in page_receipts:
                if sha(pages[item["slide"] - 1]) != str(item.get("sha256", "")).lower():
                    raise NeedsReview("PowerPoint逐页回执与实际页面哈希不符")
            for item in inventory:
                if sha(Path(item["path"])) != item["sha256"]:
                    raise NeedsReview("制作期间原件发生变化")
            state.update(state="native_verified", package=package, sources_unchanged=True,
                         native_receipt=str(build / "native-receipt.json"),
                         finalization_receipt=str(validation_path), execution=execution,
                         artifacts=[{"path": str(p), "sha256": sha(p)} for p in artifacts],
                         visual_review="pending", updated_at=datetime.now().astimezone().isoformat())
            write_json(state_path, state)
            return state
        except Exception as exc:
            state.update(state="failed", error=str(exc), updated_at=datetime.now().astimezone().isoformat())
            write_json(state_path, state)
            raise


def record_visual_review(run_path: Path, reviewer: str, evidence: str) -> Json:
    """Record a real page-by-page review; never infer it from parser success."""
    with run_lock(run_path):
        state = read_object(run_path / "state.json")
        if state.get("state") not in {"native_verified", "complete"}:
            raise NeedsReview("先完成原生渲染和验证")
        if not reviewer.strip() or not evidence.strip():
            raise NeedsReview("需要逐页核验人员与证据说明")
        for artifact in state["artifacts"]:
            if sha(Path(artifact["path"])) != artifact["sha256"]:
                raise NeedsReview("待核验产物已变化")
        state.update(state="complete", visual_review={"reviewer": reviewer, "evidence": evidence,
                     "reviewed_at": datetime.now().astimezone().isoformat()})
        write_json(run_path / "state.json", state)
        return state
