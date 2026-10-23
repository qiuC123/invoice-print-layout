"""Private, evidence-backed settlement photo drafts; never financial settlement."""
from __future__ import annotations

import hashlib
import html
import json
import posixpath
import shutil
import uuid
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from xml.dom import minidom
from xml.etree import ElementTree as ET

from PIL import Image

from invoice_print_layout.construction_reports import (
    NeedsReview, local_path, project_config, read_object, run_lock, sha, write_json,
)
from invoice_print_layout.construction_report_execution import (
    execution_code_files, finalize_presentation, mark_operation_started, run_command,
)

Json = dict[str, Any]
Renderer = Callable[[Path, Json], None]
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
FORBIDDEN = {"actual_acceptance", "actual_quantity", "person_days", "man_days", "fees", "cost", "amount"}


class NeedsInput(NeedsReview):
    """Business evidence can be supplied without repairing a corrupt input."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("；".join(missing))


def _relations(package: zipfile.ZipFile, part: str) -> dict[str, str]:
    folder, filename = posixpath.split(part)
    relfile = posixpath.join(folder, "_rels", filename + ".rels")
    if relfile not in package.namelist():
        return {}
    result: dict[str, str] = {}
    for rel in ET.fromstring(package.read(relfile)):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.attrib["Target"]
        result[rel.attrib["Id"]] = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
    return result


def ordered_slide_parts(package: zipfile.ZipFile) -> list[str]:
    presentation = ET.fromstring(package.read("ppt/presentation.xml"))
    relations = _relations(package, "ppt/presentation.xml")
    return [relations[node.attrib[f'{{{NS["r"]}}}id']]
            for node in presentation.findall("p:sldIdLst/p:sldId", NS)]


def _tables(root: ET.Element) -> list[list[list[str]]]:
    return [[["\n".join("".join(t.text or "" for t in p.findall(".//a:t", NS))
                         for p in cell.findall("a:txBody/a:p", NS))
               for cell in row.findall("a:tc", NS)]
              for row in table.findall("a:tr", NS)]
            for table in root.findall(".//a:tbl", NS)]


def _slide_size(package: zipfile.ZipFile) -> list[int]:
    size = ET.fromstring(package.read("ppt/presentation.xml")).find("p:sldSz", NS)
    if size is None:
        raise NeedsReview("模板缺少幻灯片尺寸")
    return [int(size.attrib["cx"]), int(size.attrib["cy"])]


def _nonfinancial(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN:
                raise NeedsReview(f"照片核对稿不得填写结算字段：{key}")
            _nonfinancial(item)
    elif isinstance(value, list):
        for item in value:
            _nonfinancial(item)


def validate_inputs(config: Json, config_path: Path, manifest: Json, manifest_path: Path) -> tuple[Json, list[Path]]:
    """Validate identity, original bytes, exact native rows and explicit evidence."""
    if manifest.get("schema_version") != 1 or manifest.get("project_id") != config["project_id"]:
        raise NeedsReview("结算清单版本或项目身份不符")
    if manifest.get("mode") not in {"draft", "replay"}:
        raise NeedsReview("结算照片入口仅支持 draft/replay")
    _nonfinancial(manifest)
    missing: list[str] = []

    def confirmed(value: Any, label: str) -> None:
        if not isinstance(value, dict) or value.get("status") != "confirmed" or not str(value.get("evidence", "")).strip():
            missing.append(label + "：需已确认状态与依据")

    confirmed(manifest.get("review"), "整份照片核对清单")
    for key in ("title", "report_date", "revision"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            missing.append("清单字段：" + key)
    if any(label.startswith("清单字段：") for label in missing):
        raise NeedsInput(missing)
    date.fromisoformat(manifest["report_date"])
    settings = config.get("settlement")
    if not isinstance(settings, dict) or settings.get("notes_policy") != "clear":
        raise NeedsReview("结算配置缺失或 notes_policy 不是 clear")
    template = local_path(config_path.parent, settings["template"])
    if not template.is_file() or sha(template) != str(settings.get("template_sha256", "")).lower():
        raise NeedsReview("结算模板缺失或哈希不符")
    name_column, actual_column = settings["item_name_column"], settings["actual_acceptance_column"]
    if not isinstance(name_column, int) or not isinstance(actual_column, int) or min(name_column, actual_column) < 0 or name_column == actual_column:
        raise NeedsReview("模板字段列配置不合法")
    files = [config_path, manifest_path, template]
    with zipfile.ZipFile(template) as package:
        parts = ordered_slide_parts(package)
        size = _slide_size(package)
        table_rows = [_tables(ET.fromstring(package.read(part))) for part in parts]
    if settings.get("slide_size_emu") != size or not str(settings.get("font_family", "")).strip():
        raise NeedsReview("模板尺寸或字体配置不符")
    photos: dict[str, Json] = {}
    for index, original in enumerate(manifest.get("photos", [])):
        photo = dict(original)
        photo_id = photo.get("photo_id")
        if not isinstance(photo_id, str) or not photo_id.strip() or photo_id in photos:
            raise NeedsReview("照片ID缺失或重复")
        if photo.get("project_id") is None:
            missing.append(f"照片 {photo_id}：项目待确认")
        elif photo["project_id"] != config["project_id"]:
            raise NeedsReview(f"照片项目不符：{photo_id}")
        confirmed(photo.get("review"), f"照片 {photo_id}")
        if not str(photo.get("source_id", "")).strip():
            missing.append(f"照片 {photo_id}：缺来源标识")
        if not photo.get("path"):
            missing.append(f"照片 {photo_id}：缺原图路径")
            continue
        path = local_path(manifest_path.parent, photo["path"])
        if not path.is_file():
            missing.append(f"照片 {photo_id}：原图不存在")
            continue
        if sha(path) != str(photo.get("sha256", "")).lower():
            raise NeedsReview(f"照片哈希不符：{photo_id}")
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG"} or list(image.size) != photo.get("size"):
                raise NeedsReview(f"照片格式或尺寸不符：{photo_id}")
            image.verify()
        if photo.get("captured_date") is not None:
            date.fromisoformat(photo["captured_date"])
        photo["path"] = str(path)
        photos[photo_id] = photo
        files.append(path)
    values: Json = {"title": manifest["title"], "report_date": manifest["report_date"],
                    "revision": manifest["revision"], "project_id": config["project_id"],
                    "year": manifest["report_date"][:4]}
    extra = manifest.get("values", {})
    if not isinstance(extra, dict) or any(key in values or key in FORBIDDEN or not isinstance(value, str) for key, value in extra.items()):
        raise NeedsReview("values 只能补充字符串文案，不可覆盖计算字段")
    values.update(extra)
    edits: list[Json] = []
    texts: list[Json] = []
    added_texts: list[Json] = []
    for key, target in (("edits", edits), ("texts", texts)):
        for instruction in settings.get(key, []):
            if not isinstance(instruction.get("id"), str) or not instruction["id"]:
                raise NeedsReview("模板修改必须指定已检查的锚点")
            target.append({**instruction, "text": str(instruction["text"]).format_map(values)})
    for instruction in settings.get("added_texts", []):
        if not isinstance(instruction.get("template"), int) or not 1 <= instruction["template"] <= len(parts):
            raise NeedsReview("新增文案必须指定模板页和位置")
        position = instruction.get("position", {})
        if any(not isinstance(position.get(key), (float, int)) for key in ("left", "top", "width", "height")):
            raise NeedsReview("新增文案缺完整位置")
        added_texts.append({**instruction, "text": str(instruction["text"]).format_map(values)})
    slides: list[Json] = []

    def placements(entries: list[Json], label: str, caption: str | None = None) -> list[Json]:
        result: list[Json] = []
        seen: set[str] = set()
        for entry in entries:
            photo_id = entry.get("photo_id")
            if photo_id in seen:
                raise NeedsReview(f"同一核对项重复照片：{label}")
            seen.add(str(photo_id))
            confirmed(entry.get("review"), label + "图片对应")
            if photo_id not in photos:
                missing.append(f"{label}：缺原图 {photo_id}")
                continue
            text = caption if caption is not None else entry.get("caption")
            if not isinstance(text, str) or not text.strip():
                missing.append(label + "：缺已确认图片说明")
                continue
            # Placement cannot override the verified original's hash, dimensions or identity.
            result.append({**entry, **photos[photo_id], "placement_review": entry.get("review"), "caption": text})
        return result

    for index, original in enumerate(manifest.get("slides", [])):
        slide = dict(original)
        source_index = slide.get("template")
        if not isinstance(source_index, int) or not 1 <= source_index <= len(parts):
            raise NeedsReview("引用模板页超出范围")
        if slide.get("kind") not in {"cover", "section", "overview", "items", "groups"}:
            raise NeedsReview("未知结算页面类型")
        rows = json.loads(json.dumps(table_rows[source_index - 1]))
        for table in rows:
            if not table or any(len(row) <= max(name_column, actual_column) for row in table):
                raise NeedsReview("模板表格列或数据行不完整")
            if any(row[actual_column].strip() for row in table[1:]):
                raise NeedsReview("模板实收栏已有内容，停止生成")
        if slide["kind"] in {"items", "groups"}:
            if not rows:
                raise NeedsReview("项目/人员页必须保留原生模板表格")
            header = settings.get("reference_quantity_header")
            if header:
                column = header["column"]
                if not isinstance(column, int) or not 0 <= column < len(rows[0][0]) or column == actual_column:
                    raise NeedsReview("参考数量列配置错误")
                rows[0][0][column] = header["text"]
        slide["expected_tables"] = rows
        if slide["kind"] == "items":
            selected: list[Json] = []
            seen_rows: set[int] = set()
            for item in slide.get("items", []):
                row = item.get("row")
                if not isinstance(row, int) or not 1 <= row < len(rows[0]) or row in seen_rows:
                    raise NeedsReview("项目必须引用唯一的模板数据行")
                seen_rows.add(row)
                name = rows[0][row][name_column]
                if not name.strip() or str(item.get("name", "")).strip() != name.strip():
                    raise NeedsReview("项目名称与模板不符或合并续行没有项目名")
                selected.extend(placements(item.get("photos", []), name.strip(), name.strip()))
            slide["photos"] = selected
        elif slide["kind"] == "groups":
            groups: list[Json] = []
            for group in slide.get("groups", []):
                confirmed(group, f"第 {index + 1} 页岗位组")
                if not str(group.get("label", "")).strip():
                    missing.append(f"第 {index + 1} 页：岗位标签待确认")
                groups.append({**group, "photos": placements(group.get("photos", []), str(group.get("label", "岗位")))})
            slide["groups"] = groups
            slide["photos"] = []
        else:
            slide["photos"] = placements(slide.get("photos", []), f"第 {index + 1} 页")
        slides.append(slide)
    if not slides:
        missing.append("未指定核对页面")
    for evidence in manifest.get("evidence_files", []):
        path = local_path(manifest_path.parent, evidence["path"])
        if not path.is_file() or sha(path) != str(evidence.get("sha256", "")).lower():
            raise NeedsReview("来源证据文件缺失或哈希不符")
        files.append(path)
    if missing:
        raise NeedsInput(missing)
    job: Json = {"report_kind": "settlement", "project_id": config["project_id"], "template": str(template),
                 "title": manifest["title"], "date": manifest["report_date"], "revision": manifest["revision"],
                 "mode": manifest["mode"], "values": values, "slides": slides, "settings": settings,
                 "edits": edits, "texts": texts, "added_texts": added_texts,
                 "expected_slide_count": len(slides), "slide_size_emu": size,
                 "required_native_table_owner_slides": [i + 1 for i, s in enumerate(slides) if s["expected_tables"]]}
    return job, list(dict.fromkeys(path.resolve() for path in files))


def slide_photos(slide: Json) -> list[Json]:
    return list(slide.get("photos", [])) + [photo for group in slide.get("groups", []) for photo in group["photos"]]


def prepare_authored_package(authored: Path, prepared: Path, job: Json, receipt_path: Path) -> Json:
    """Select verified appended pictures before native SaveAs; no raster changes.

    The artifact exporter currently omits image ``alt``. Metadata cannot identify
    new pictures until their ordered embedded bytes have independently matched.
    DOM preserves namespace declarations, including prefixes used by mc:Ignorable.
    Only byte-identical media aliases are merged, retaining all picture objects.
    """
    if prepared.exists() or receipt_path.exists():
        raise NeedsReview("图片准备已经执行，请使用新 attempt")
    authored_sha = sha(authored)
    changed: dict[str, bytes] = {}
    selected: list[Json] = []
    aliases: dict[str, str] = {}
    with zipfile.ZipFile(authored) as archive:
        parts = ordered_slide_parts(archive)
        if len(parts) < job["expected_slide_count"] or _slide_size(archive) != job["slide_size_emu"]:
            raise NeedsReview("作者包页数不足或尺寸不符")
        for index, spec in enumerate(job["slides"]):
            if spec["kind"] not in {"overview", "items", "groups"}:
                continue
            part = parts[index]
            dom = minidom.parseString(archive.read(part))
            pictures = list(dom.getElementsByTagNameNS(NS["p"], "pic"))
            expected = slide_photos(spec)
            if len(pictures) < len(expected):
                raise NeedsReview(f"第 {index + 1} 页作者包缺新增照片")
            # slide.images.add appends; never guess by an ambiguous hash search.
            kept = pictures[-len(expected):] if expected else []
            relations = _relations(archive, part)
            records: list[Json] = []
            for picture, photo in zip(kept, expected, strict=True):
                blips = picture.getElementsByTagNameNS(NS["a"], "blip")
                if len(blips) != 1 or not blips[0].hasAttributeNS(NS["r"], "embed"):
                    raise NeedsReview("新增照片必须是唯一嵌入原图")
                image_part = relations[blips[0].getAttributeNS(NS["r"], "embed")]
                image_hash = hashlib.sha256(archive.read(image_part)).hexdigest()
                if image_hash != photo["sha256"].lower() or sha(Path(photo["path"])) != image_hash:
                    raise NeedsReview(f"第 {index + 1} 页新增照片顺序或原字节不符")
                props = picture.getElementsByTagNameNS(NS["p"], "cNvPr")
                if len(props) != 1:
                    raise NeedsReview("新增照片没有唯一图片属性")
                props[0].setAttribute("descr", "settlement-photo:" + photo["photo_id"])
                records.append({"photo_id": photo["photo_id"], "sha256": image_hash,
                                "authored_part": image_part, "caption": photo["caption"]})
            for picture in pictures[:len(pictures) - len(kept)]:
                if picture.parentNode is None:
                    raise NeedsReview("旧占位图缺少父节点")
                picture.parentNode.removeChild(picture)
            changed[part] = dom.toxml(encoding="UTF-8")
            selected.append({"slide": index + 1, "pictures_before": len(pictures), "pictures_after": len(kept),
                             "removed_template_pictures": len(pictures) - len(kept), "photos": records})
        # Identical repeated placements can exceed the packaged validator's size
        # limit. Coalesce bytes only; image nodes, captions and positions survive.
        unique: dict[str, str] = {}
        for name in archive.namelist():
            if name.startswith("ppt/media/") and not name.endswith("/"):
                digest = hashlib.sha256(archive.read(name)).hexdigest()
                if digest in unique:
                    aliases[name] = unique[digest]
                else:
                    unique[digest] = name
        if aliases:
            for name in archive.namelist():
                if not name.endswith(".rels") or "/_rels/" not in name:
                    continue
                relfolder, relname = posixpath.split(name)
                owner = posixpath.join(posixpath.dirname(relfolder), relname[:-5])
                relations_dom = minidom.parseString(archive.read(name))
                modified = False
                for rel in relations_dom.getElementsByTagNameNS(REL, "Relationship"):
                    if rel.getAttribute("TargetMode") == "External":
                        continue
                    target = rel.getAttribute("Target")
                    full = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(owner), target))
                    if full in aliases:
                        rel.setAttribute("Target", posixpath.relpath(aliases[full], posixpath.dirname(owner)))
                        modified = True
                if modified:
                    changed[name] = relations_dom.toxml(encoding="UTF-8")
            if "[Content_Types].xml" in archive.namelist():
                content_types = minidom.parseString(archive.read("[Content_Types].xml"))
                content_root = content_types.documentElement
                if content_root is None:
                    raise NeedsReview("作者包缺少 Content Types 根节点")
                for node in list(content_root.childNodes):
                    if isinstance(node, minidom.Element) and node.localName == "Override" and node.getAttribute("PartName").lstrip("/") in aliases:
                        content_root.removeChild(node)
                changed["[Content_Types].xml"] = content_types.toxml(encoding="UTF-8")
        prepared.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(prepared, "x") as output:
            for info in archive.infolist():
                if info.filename not in aliases:
                    output.writestr(info, changed.get(info.filename, archive.read(info.filename)))
    if sha(authored) != authored_sha:
        raise NeedsReview("图片准备期间作者包变化")
    receipt: Json = {"status": "passed", "authored_sha256": authored_sha, "prepared_sha256": sha(prepared),
                     "selected_slides": selected, "merged_identical_media": aliases,
                     "original_image_bytes_verified": True, "raster_modified": False}
    write_json(receipt_path, receipt)
    return receipt


def verify_package(deck: Path, job: Json) -> Json:
    with zipfile.ZipFile(deck) as package:
        parts = ordered_slide_parts(package)
        if len(parts) != job["expected_slide_count"] or _slide_size(package) != job["slide_size_emu"]:
            raise NeedsReview("结算成品页数或尺寸不符")
        table_count = 0
        for index, (part, spec) in enumerate(zip(parts, job["slides"], strict=True), 1):
            root = ET.fromstring(package.read(part))
            tables = _tables(root)
            if tables != spec["expected_tables"]:
                raise NeedsReview(f"第 {index} 页原生表格发生变化（含清单/实收）")
            table_count += len(tables)
            # Table item names cannot stand in for the separate photo captions.
            paragraphs = ["".join(node.text or "" for node in para.findall(".//a:t", NS)) for para in root.findall(".//p:sp/p:txBody/a:p", NS)]
            actual_text = Counter(paragraphs)
            required_text = Counter(photo["caption"] for photo in slide_photos(spec))
            if any(actual_text[text] < count for text, count in required_text.items()):
                raise NeedsReview(f"第 {index} 页缺少已确认图片说明")
            relations = _relations(package, part)
            actual_images: Counter[str] = Counter()
            image_mapping: list[tuple[str, str]] = []
            for pic in root.findall(".//p:pic", NS):
                blip = pic.find(".//a:blip", NS)
                if blip is None or f'{{{NS["r"]}}}embed' not in blip.attrib:
                    raise NeedsReview("图片不是嵌入原图")
                image_part = relations[blip.attrib[f'{{{NS["r"]}}}embed']]
                image_hash = hashlib.sha256(package.read(image_part)).hexdigest()
                actual_images[image_hash] += 1
                properties = pic.find("p:nvPicPr/p:cNvPr", NS)
                image_mapping.append((properties.get("descr", "") if properties is not None else "", image_hash))
            required_images = Counter(photo["sha256"].lower() for photo in slide_photos(spec))
            if (spec["kind"] in {"overview", "items", "groups"} and actual_images != required_images) or any(actual_images[key] < count for key, count in required_images.items()):
                raise NeedsReview(f"第 {index} 页图片原字节/对应次数不符")
            if spec["kind"] in {"overview", "items", "groups"} and image_mapping != [("settlement-photo:" + photo["photo_id"], photo["sha256"].lower()) for photo in slide_photos(spec)]:
                raise NeedsReview(f"第 {index} 页原图映射或排列顺序不符")
        for name in package.namelist():
            if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml"):
                if any((node.text or "").strip() for node in ET.fromstring(package.read(name)).findall(".//a:t", NS)):
                    raise NeedsReview("成品备注必须清空；来源仅存私有 build")
    return {"slides": len(parts), "native_tables": table_count, "actual_acceptance_empty": True,
            "table_rows_preserved": True, "per_slide_original_photo_bytes": True, "notes_empty": True}


def _preview(attempt: Path, job: Json) -> None:
    images = "".join(f'<figure><img src="pages/slide-{i:03d}.png" alt="第{i}页"></figure>' for i in range(1, job["expected_slide_count"] + 1))
    text = f'<!doctype html><meta charset="utf-8"><title>{html.escape(job["title"])}</title><style>body{{max-width:1100px;margin:auto;font-family:sans-serif}}img{{width:100%}}figure{{margin:24px 0}}</style><h1>{html.escape(job["title"])}</h1><p>照片核对草稿，实际验收待填写。</p><a href="report.pptx">下载可编辑 PowerPoint</a>{images}'
    (attempt / "output/preview.html").write_text(text, encoding="utf-8")


def native_renderer(job_path: Path, runtime: Json) -> None:
    job = read_object(job_path)
    attempt = Path(job["run_dir"])
    build = attempt / "build"
    output = attempt / "output"
    output.mkdir(exist_ok=True)
    shell = runtime.get("powershell_executable") or shutil.which("powershell.exe")
    if not shell:
        raise NeedsReview("结算原生渲染要求 Windows PowerShell/PowerPoint")
    mark_operation_started(runtime, build)
    run_command([str(runtime["node_executable"]), str(Path(__file__).with_name("settlement_report_render.mjs")), str(job_path)], runtime=runtime, log_path=build / "author.log")
    prepare_authored_package(build / "authored.pptx", build / "authored-verified.pptx", job,
                             build / "media-preparation.json")
    native = str(Path(__file__).with_name("settlement_report_native.ps1"))
    command = [str(shell), "-NoProfile", "-NonInteractive", "-File", native, "-JobPath", str(job_path), "-Mode"]
    run_command([*command, "compose"], runtime=runtime, log_path=build / "native-compose.log")
    candidate_check = verify_package(build / "candidate.pptx", job)
    write_json(build / "candidate-content.json", {"status": "passed", "sha256": sha(build / "candidate.pptx"), "package": candidate_check})
    finalize_presentation(runtime, workspace_dir=attempt, candidate_path=build / "candidate.pptx",
                          final_path=output / "report.pptx", expected_slide_count=job["expected_slide_count"],
                          expected_slide_size_emu=",".join(map(str, job["slide_size_emu"])),
                          font_reference_path=Path(job["template"]),
                          required_native_table_owner_slides=job["required_native_table_owner_slides"],
                          receipt_path=build / "validation.json")
    run_command([*command, "verify"], runtime=runtime, log_path=build / "native-verify.log")
    _preview(attempt, job)


def _verify_artifacts(state: Json) -> None:
    for item in state.get("artifacts", []):
        path = Path(item["path"])
        if not path.is_file() or sha(path) != item["sha256"]:
            raise NeedsReview("已记录的输入/产物变化，请保留旧结果并建立新修订")


def run_report(registry_path: Path, project: str, manifest_path: Path, *, output_root: Path | None = None,
               renderer: Renderer = native_renderer) -> Json:
    registry_path, manifest_path = registry_path.resolve(), manifest_path.resolve()
    config, config_path, registry = project_config(registry_path, project)
    if not manifest_path.is_file():
        return {"state": "needs_input", "project_id": config["project_id"], "missing": ["结算照片清单"], "manifest_path": str(manifest_path)}
    manifest = read_object(manifest_path)
    try:
        job, files = validate_inputs(config, config_path, manifest, manifest_path)
    except NeedsInput as exc:
        return {"state": "needs_input", "project_id": config["project_id"], "missing": exc.missing, "manifest_path": str(manifest_path)}
    files.append(registry_path)
    inventory = [{"path": str(path), "sha256": sha(path)} for path in files]
    code = [Path(__file__), Path(__file__).with_name("settlement_report_render.mjs"), Path(__file__).with_name("settlement_report_native.ps1"), Path(__file__).with_name("construction_reports.py"), *execution_code_files()]
    identity = {"inputs": [item for item in inventory if item["path"] != str(registry_path)], "runtime": registry.get("runtime", {}), "code": {str(path): sha(path) for path in code}}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    root = output_root.resolve() if output_root else local_path(registry_path.parent, registry["runs_root"])
    folder = root / config["project_id"] / "settlement" / job["date"] / fingerprint[:20]
    with run_lock(folder):
        state_path = folder / "state.json"
        previous = read_object(state_path) if state_path.is_file() else {}
        if previous.get("fingerprint") not in (None, fingerprint):
            raise NeedsReview("运行指纹冲突")
        if previous.get("state") in {"native_verified", "complete"}:
            _verify_artifacts(previous)
            return {**previous, "reused": True}
        snapshot_dir = folder / "inputs"
        snapshot_dir.mkdir(exist_ok=True)
        snapshots: dict[str, str] = {}
        for index, item in enumerate(inventory):
            original = Path(item["path"])
            frozen = snapshot_dir / f'{index:03d}-{item["sha256"][:12]}{original.suffix}'
            if not frozen.exists():
                shutil.copy2(original, frozen)
            if sha(frozen) != item["sha256"]:
                raise NeedsReview("冻结输入损坏")
            snapshots[str(original)] = str(frozen)
        job["template"] = snapshots[job["template"]]
        for slide in job["slides"]:
            for photo in slide_photos(slide):
                photo["path"] = snapshots[photo["path"]]
        attempt = folder / ("attempt-" + uuid.uuid4().hex[:12])
        build = attempt / "build"
        build.mkdir(parents=True)
        job.update(run_dir=str(attempt), runtime=registry.get("runtime", {}))
        job_path = build / "job.json"
        write_json(job_path, job)
        write_json(build / "sources.json", {"inventory": inventory, "snapshots": snapshots, "manifest": manifest})
        state: Json = {"report_kind": "settlement", "state": "rendering", "project_id": config["project_id"],
                       "date": job["date"], "mode": job["mode"], "revision": job["revision"], "fingerprint": fingerprint,
                       "run_path": str(folder), "attempt_path": str(attempt), "job_path": str(job_path),
                       "inventory": inventory, "snapshots": snapshots, "sent": False, "project_delivery_written": False}
        write_json(state_path, state)
        try:
            renderer(job_path, registry.get("runtime", {}))
            deck = attempt / "output/report.pptx"
            package = verify_package(deck, job)
            receipt = read_object(build / "native-receipt.json")
            validation = read_object(build / "validation.json")
            preparation = read_object(build / "media-preparation.json")
            candidate_content = read_object(build / "candidate-content.json")
            if (preparation.get("status") != "passed" or preparation.get("original_image_bytes_verified") is not True
                    or preparation.get("raster_modified") is not False or candidate_content.get("status") != "passed"):
                raise NeedsReview("缺少保存前后原图核验回执")
            execution = validation.get("execution", {})
            if not (execution.get("finalizer_returned") or execution.get("fallback_used")):
                raise NeedsReview("缺少真实最终化回执")
            if (receipt.get("status") != "passed" or receipt.get("reopened_final") is not True
                    or receipt.get("notes_cleared") is not True or receipt.get("slide_count") != job["expected_slide_count"]
                    or receipt.get("final_sha256") != sha(deck)):
                raise NeedsReview("PowerPoint原生验证回执不完整")
            pages = [attempt / "output/pages" / f"slide-{i:03d}.png" for i in range(1, len(job["slides"]) + 1)]
            page_receipts = receipt.get("slides", [])
            if len(page_receipts) != len(pages) or sorted(item.get("slide", 0) for item in page_receipts) != list(range(1, len(pages) + 1)):
                raise NeedsReview("原生逐页导出回执缺页或重复")
            for item in page_receipts:
                if sha(pages[item["slide"] - 1]) != item.get("sha256"):
                    raise NeedsReview("原生导出 PNG 哈希不符")
            artifacts = [deck, attempt / "output/preview.html", *pages, build / "native-receipt.json", build / "validation.json", build / "media-preparation.json", build / "candidate-content.json", build / "sources.json", job_path, *map(Path, snapshots.values())]
            if any(not path.is_file() for path in artifacts):
                raise NeedsReview("交付预览或冻结输入不完整")
            for item in inventory:
                if not Path(item["path"]).is_file() or sha(Path(item["path"])) != item["sha256"]:
                    raise NeedsReview("制作期间原始材料变化")
            state.update(state="native_verified", package=package, sources_unchanged=True, finalization=execution,
                         visual_review="pending", artifacts=[{"path": str(path), "sha256": sha(path)} for path in artifacts],
                         updated_at=datetime.now().astimezone().isoformat())
            write_json(state_path, state)
            return state
        except Exception as exc:
            state.update(state="failed", error=str(exc), updated_at=datetime.now().astimezone().isoformat())
            write_json(state_path, state)
            raise


def status(run_path: Path) -> Json:
    state = read_object(run_path.resolve() / "state.json")
    if state.get("report_kind") != "settlement":
        raise NeedsReview("不是结算照片运行")
    if state.get("state") in {"native_verified", "complete"}:
        _verify_artifacts(state)
    return state


def record_visual_review(run_path: Path, reviewer: str, evidence: str) -> Json:
    """Only a real reviewer can advance native_verified to complete."""
    run_path = run_path.resolve()
    with run_lock(run_path):
        state = status(run_path)
        if state.get("state") not in {"native_verified", "complete"} or not reviewer.strip() or not evidence.strip():
            raise NeedsReview("先完成原生逐页导出，再提供核验人员和真实逐页证据")
        state.update(state="complete", visual_review={"reviewer": reviewer, "evidence": evidence,
                                                      "reviewed_at": datetime.now().astimezone().isoformat()})
        write_json(run_path / "state.json", state)
        return state
