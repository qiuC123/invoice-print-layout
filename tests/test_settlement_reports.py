"""Synthetic package/transaction tests; these do not certify real Office rendering."""
from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from invoice_print_layout import settlement_reports as reports
from invoice_print_layout.construction_reports import NeedsReview, read_object, sha, write_json

NS = reports.NS
REL = reports.REL
SIZE = [12192000, 6858000]
TABLE = [["项目", "规格", "说明", "数量", "单位", "实际验收", "单位"],
         [" 支撑柱 ", "200mm", "原说明", "8", "根", "", "根"],
         ["基层", "标准", "原说明", "2", "套", "", "套"],
         ["", "合并续行", "", "", "", "", ""]]
CONFIRMED = {"status": "confirmed", "evidence": "用户已接受的来源核对"}


def _text(parent: ET.Element, words: str, tag: str) -> None:
    container = ET.SubElement(parent, f'{{{NS["p"]}}}{tag}')
    body = ET.SubElement(container, f'{{{NS["a"]}}}txBody') if tag == "unused" else container
    paragraph = ET.SubElement(body, f'{{{NS["a"]}}}p')
    run = ET.SubElement(paragraph, f'{{{NS["a"]}}}r')
    ET.SubElement(run, f'{{{NS["a"]}}}t').text = words


def _package(path: Path, slides: list[dict], *, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as package:
        presentation = ET.Element(f'{{{NS["p"]}}}presentation')
        ids = ET.SubElement(presentation, f'{{{NS["p"]}}}sldIdLst')
        ET.SubElement(presentation, f'{{{NS["p"]}}}sldSz', cx=str(SIZE[0]), cy=str(SIZE[1]))
        rels = ET.Element(f"{{{REL}}}Relationships")
        # Names deliberately run backwards: verification must use relationships/display order.
        for index, slide in enumerate(slides, 1):
            number = len(slides) - index + 1
            filename = f"slide{number}.xml"
            ET.SubElement(ids, f'{{{NS["p"]}}}sldId', {"id": str(index+255), f'{{{NS["r"]}}}id': f"s{index}"})
            ET.SubElement(rels, f"{{{REL}}}Relationship", Id=f"s{index}", Target=f"slides/{filename}", Type="slide")
            root = ET.Element(f'{{{NS["p"]}}}sld')
            tree = ET.SubElement(ET.SubElement(root, f'{{{NS["p"]}}}cSld'), f'{{{NS["p"]}}}spTree')
            for rows in slide.get("tables", []):
                table = ET.SubElement(ET.SubElement(tree, f'{{{NS["p"]}}}graphicFrame'), f'{{{NS["a"]}}}tbl')
                for row in rows:
                    row_node = ET.SubElement(table, f'{{{NS["a"]}}}tr')
                    for value in row:
                        cell = ET.SubElement(row_node, f'{{{NS["a"]}}}tc')
                        body = ET.SubElement(cell, f'{{{NS["a"]}}}txBody')
                        para = ET.SubElement(body, f'{{{NS["a"]}}}p')
                        ET.SubElement(ET.SubElement(para, f'{{{NS["a"]}}}r'), f'{{{NS["a"]}}}t').text = value
            image_rels = ET.Element(f"{{{REL}}}Relationships")
            for picture_index, photo in enumerate(slide.get("photos", []), 1):
                name = f"image-{index}-{picture_index}.png"
                package.writestr("ppt/media/" + name, Path(photo["path"]).read_bytes())
                ET.SubElement(image_rels, f"{{{REL}}}Relationship", Id=f"i{picture_index}", Target="../media/"+name, Type="image")
                pic = ET.SubElement(tree, f'{{{NS["p"]}}}pic')
                nv = ET.SubElement(pic, f'{{{NS["p"]}}}nvPicPr')
                ET.SubElement(nv, f'{{{NS["p"]}}}cNvPr', **({} if slide.get("omit_alt") else {"descr": "settlement-photo:"+photo["photo_id"]}))
                ET.SubElement(ET.SubElement(pic, f'{{{NS["p"]}}}blipFill'), f'{{{NS["a"]}}}blip', {f'{{{NS["r"]}}}embed': f"i{picture_index}"})
                if not slide.get("omit_caption"):
                    shape = ET.SubElement(tree, f'{{{NS["p"]}}}sp')
                    body = ET.SubElement(shape, f'{{{NS["p"]}}}txBody')
                    para = ET.SubElement(body, f'{{{NS["a"]}}}p')
                    ET.SubElement(ET.SubElement(para, f'{{{NS["a"]}}}r'), f'{{{NS["a"]}}}t').text = photo["caption"]
            package.writestr("ppt/slides/"+filename, ET.tostring(root))
            package.writestr("ppt/slides/_rels/"+filename+".rels", ET.tostring(image_rels))
        package.writestr("ppt/presentation.xml", ET.tostring(presentation))
        package.writestr("ppt/_rels/presentation.xml.rels", ET.tostring(rels))
        notes = ET.Element(f'{{{NS["p"]}}}notes')
        para = ET.SubElement(notes, f'{{{NS["a"]}}}p')
        ET.SubElement(ET.SubElement(para, f'{{{NS["a"]}}}r'), f'{{{NS["a"]}}}t').text = note
        package.writestr("ppt/notesSlides/notesSlide1.xml", ET.tostring(notes))


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[Path, Path]:
    _package(tmp_path / "template.pptx", [{"tables": []}, {"tables": [TABLE]}])
    Image.new("RGB", (20, 15), "red").save(tmp_path / "photo.png")
    config = {"schema_version": 1, "project_id": "project-a", "settlement": {
        "template": "template.pptx", "template_sha256": sha(tmp_path / "template.pptx"),
        "slide_size_emu": SIZE, "font_family": "Arial", "item_name_column": 0,
        "actual_acceptance_column": 5, "reference_quantity_header": {"column": 3, "text": "参考数量"},
        "notes_policy": "clear", "edits": [], "texts": []}}
    manifest = {"schema_version": 1, "project_id": "project-a", "title": "照片核对稿",
                "report_date": "2026-10-03", "revision": "r1", "mode": "draft", "review": CONFIRMED,
                "values": {"station_label": "另一项目"}, "photos": [{"photo_id": "p1", "path": "photo.png",
                "sha256": sha(tmp_path / "photo.png"), "size": [20, 15], "project_id": "project-a",
                "source_id": "source-message-1", "captured_date": None, "review": CONFIRMED}],
                "slides": [{"template": 1, "kind": "cover", "title": "封面"},
                           {"template": 2, "kind": "items", "title": "清单",
                            "items": [{"row": 1, "name": "支撑柱", "photos": [{"photo_id": "p1", "review": CONFIRMED}]},
                                      {"row": 2, "name": "基层", "photos": [{"photo_id": "p1", "review": CONFIRMED}]}]},
                           {"template": 2, "kind": "groups", "title": "已确认搭建岗位",
                            "groups": [{"label": "09.26", **CONFIRMED, "photos": [{"photo_id": "p1", "caption": "09:30", "review": CONFIRMED}]}]}]}
    write_json(tmp_path / "config.json", config)
    write_json(tmp_path / "manifest.json", manifest)
    write_json(tmp_path / "registry.json", {"projects": {"project-a": {"config": "config.json", "aliases": ["A"]}}, "runs_root": "runs", "runtime": {}})
    return tmp_path / "registry.json", tmp_path / "manifest.json"


def _job(fixture: tuple[Path, Path]) -> dict:
    registry, manifest_path = fixture
    config = read_object(registry.parent / "config.json")
    return reports.validate_inputs(config, registry.parent / "config.json", read_object(manifest_path), manifest_path)[0]


def _render(job_path: Path, runtime: dict) -> None:
    job = read_object(job_path)
    attempt = Path(job["run_dir"])
    output, build = attempt / "output", attempt / "build"
    output.mkdir(exist_ok=True)
    slides = [{"tables": slide["expected_tables"], "photos": reports.slide_photos(slide)} for slide in job["slides"]]
    _package(output / "report.pptx", slides)
    page_receipts = []
    for index in range(1, len(slides)+1):
        page = output / "pages" / f"slide-{index:03d}.png"
        page.parent.mkdir(exist_ok=True)
        Image.new("RGB", (160, 90), "white").save(page)
        page_receipts.append({"slide": index, "sha256": sha(page)})
    (output / "preview.html").write_text("synthetic preview", encoding="utf-8")
    write_json(build / "validation.json", {"passed": True, "execution": {"finalizer_returned": True, "fallback_used": False}})
    write_json(build / "media-preparation.json", {"status": "passed", "original_image_bytes_verified": True, "raster_modified": False})
    write_json(build / "candidate-content.json", {"status": "passed"})
    write_json(build / "native-receipt.json", {"status": "passed", "reopened_final": True,
               "notes_cleared": True, "slide_count": len(slides), "final_sha256": sha(output / "report.pptx"), "slides": page_receipts})


def test_evidence_same_photo_multiple_items_and_display_order(fixture: tuple[Path, Path]) -> None:
    job = _job(fixture)
    assert [p["caption"] for p in job["slides"][1]["photos"]] == ["支撑柱", "基层"]
    assert job["slides"][1]["expected_tables"][0][1][0] == " 支撑柱 "
    assert job["required_native_table_owner_slides"] == [2, 3]
    assert job["slides"][1]["expected_tables"][0][0][3] == "参考数量"


@pytest.mark.parametrize("field", ["project", "hash", "size", "item", "merged", "values", "fee"])
def test_reject_conflict_or_corruption(fixture: tuple[Path, Path], field: str) -> None:
    manifest = read_object(fixture[1])
    if field == "project": manifest["photos"][0]["project_id"] = "another"
    elif field == "hash": manifest["photos"][0]["sha256"] = "0"*64
    elif field == "size": manifest["photos"][0]["size"] = [21, 15]
    elif field == "item": manifest["slides"][1]["items"][0]["name"] = "自造名称"
    elif field == "merged": manifest["slides"][1]["items"][0].update(row=3, name="新增项目")
    elif field == "values": manifest["values"]["title"] = "覆盖"
    elif field == "fee": manifest["fees"] = 100
    write_json(fixture[1], manifest)
    with pytest.raises(NeedsReview): _job(fixture)


@pytest.mark.parametrize("field", ["photo", "project", "role", "mapping", "review"])
def test_missing_evidence_returns_needs_input_without_render(fixture: tuple[Path, Path], field: str) -> None:
    manifest = read_object(fixture[1])
    if field == "photo": (fixture[1].parent / "photo.png").unlink()
    elif field == "project": manifest["photos"][0]["project_id"] = None
    elif field == "role": manifest["slides"][2]["groups"][0]["status"] = "pending"
    elif field == "mapping": manifest["slides"][1]["items"][0]["photos"][0].pop("review")
    elif field == "review": manifest["review"] = {"status": "pending"}
    write_json(fixture[1], manifest)
    result = reports.run_report(*fixture[:1], "A", fixture[1], renderer=lambda *_: pytest.fail("must not render"))
    assert result["state"] == "needs_input" and result["missing"]
    assert not (fixture[0].parent / "runs").exists()


def test_dirty_actual_acceptance_blocks_generation(fixture: tuple[Path, Path]) -> None:
    table = copy.deepcopy(TABLE)
    table[1][5] = "8"
    path = fixture[0].parent / "template.pptx"
    _package(path, [{"tables": []}, {"tables": [table]}])
    config = read_object(fixture[0].parent / "config.json")
    config["settlement"]["template_sha256"] = sha(path)
    write_json(fixture[0].parent / "config.json", config)
    with pytest.raises(NeedsReview, match="实收"): _job(fixture)


def test_empty_item_photos_and_many_original_placements_are_allowed(fixture: tuple[Path, Path]) -> None:
    manifest = read_object(fixture[1])
    manifest["slides"][1]["items"][0]["photos"] = []
    write_json(fixture[1], manifest)
    assert len(_job(fixture)["slides"][1]["photos"]) == 1
    for index in range(2, 9):
        manifest["photos"].append({**manifest["photos"][0], "photo_id": f"p{index}"})
    manifest["slides"][1]["items"][0]["photos"] = [{"photo_id": f"p{i}", "review": CONFIRMED} for i in range(1,9)]
    write_json(fixture[1], manifest)
    assert len(_job(fixture)["slides"][1]["photos"]) == 9


def test_native_verified_review_reuse_and_frozen_inputs(fixture: tuple[Path, Path]) -> None:
    originals = {str(path): sha(path) for path in fixture[0].parent.iterdir() if path.is_file()}
    state = reports.run_report(fixture[0], "A", fixture[1], renderer=_render)
    assert state["state"] == "native_verified" and state["visual_review"] == "pending"
    assert state["package"]["native_tables"] == 2 and state["package"]["notes_empty"]
    job = read_object(Path(state["job_path"]))
    for slide in job["slides"]:
        assert all("inputs" in Path(photo["path"]).parts for photo in reports.slide_photos(slide))
    assert {str(Path(path)): sha(Path(path)) for path in originals} == originals
    reused = reports.run_report(fixture[0], "A", fixture[1], renderer=lambda *_: pytest.fail("reused"))
    assert reused["reused"] is True
    complete = reports.record_visual_review(Path(state["run_path"]), "实际核验人", "逐页图片核验记录")
    assert complete["state"] == "complete"
    assert reports.status(Path(state["run_path"]))["state"] == "complete"
    Path(state["attempt_path"], "output/report.pptx").write_bytes(b"modified")
    with pytest.raises(NeedsReview, match="变化"):
        reports.run_report(fixture[0], "A", fixture[1], renderer=_render)


@pytest.mark.parametrize("fault", ["notes", "table", "caption", "photo", "receipt", "finalizer", "source"])
def test_bad_outputs_record_failed_and_retry_fresh_attempt(fixture: tuple[Path, Path], fault: str) -> None:
    def broken(job_path: Path, runtime: dict) -> None:
        _render(job_path, runtime)
        job = read_object(job_path)
        build = job_path.parent
        output = build.parent / "output"
        slides = [{"tables": copy.deepcopy(s["expected_tables"]), "photos": reports.slide_photos(s)} for s in job["slides"]]
        if fault == "notes": _package(output / "report.pptx", slides, note="private source")
        elif fault == "table":
            slides[1]["tables"][0][1][5] = "8"
            _package(output / "report.pptx", slides)
        elif fault == "caption":
            slides[1]["omit_caption"] = True
            _package(output / "report.pptx", slides)
        elif fault == "photo":
            slides[1]["photos"] = slides[1]["photos"][:1]
            _package(output / "report.pptx", slides)
        elif fault == "receipt": write_json(build / "native-receipt.json", {"status": "passed"})
        elif fault == "finalizer": write_json(build / "validation.json", {"passed": True})
        elif fault == "source": (fixture[0].parent / "photo.png").write_bytes(b"changed")
    with pytest.raises(NeedsReview):
        reports.run_report(fixture[0], "A", fixture[1], renderer=broken)
    states = list((fixture[0].parent / "runs").rglob("state.json"))
    assert len(states) == 1 and read_object(states[0])["state"] == "failed"
    failed = read_object(states[0])["attempt_path"]
    if fault != "source":
        state = reports.run_report(fixture[0], "A", fixture[1], renderer=_render)
        assert state["attempt_path"] != failed and Path(failed).exists()


def test_placement_cannot_override_original_hash(fixture: tuple[Path, Path]) -> None:
    manifest = read_object(fixture[1])
    manifest["slides"][1]["items"][0]["photos"][0].update(sha256="0"*64, size=[1,1], path="evil.png")
    write_json(fixture[1], manifest)
    photo = _job(fixture)["slides"][1]["photos"][0]
    assert photo["sha256"] == manifest["photos"][0]["sha256"] and photo["size"] == [20,15]


def test_missing_exporter_alt_clears_placeholder_without_losing_originals(fixture: tuple[Path, Path]) -> None:
    job = _job(fixture)
    folder = fixture[0].parent
    Image.new("RGB", (20, 15), "blue").save(folder / "placeholder.png")
    placeholder = {"photo_id": "placeholder", "path": str(folder / "placeholder.png"), "caption": ""}
    slides = [{"tables": s["expected_tables"], "photos": ([placeholder] if s["kind"] != "cover" else []) + reports.slide_photos(s), "omit_alt": True} for s in job["slides"]]
    authored, prepared, receipt_path = folder / "authored.pptx", folder / "prepared.pptx", folder / "preparation.json"
    _package(authored, slides)
    before = sha(authored)
    receipt = reports.prepare_authored_package(authored, prepared, job, receipt_path)
    assert sha(authored) == before
    assert [s["pictures_after"] for s in receipt["selected_slides"]] == [2, 1]
    assert all(s["removed_template_pictures"] == 1 for s in receipt["selected_slides"])
    assert receipt["merged_identical_media"] and receipt["raster_modified"] is False
    assert reports.verify_package(prepared, job)["per_slide_original_photo_bytes"]
    with zipfile.ZipFile(prepared) as package:
        media = [name for name in package.namelist() if name.startswith("ppt/media/")]
        assert len(media) == 2  # red originals and the now-unreferenced blue placeholder
        original_hash = hashlib.sha256((folder / "photo.png").read_bytes()).hexdigest()
        assert original_hash in [hashlib.sha256(package.read(name)).hexdigest() for name in media]


def test_preparation_refuses_unmatched_appended_original_order(fixture: tuple[Path, Path]) -> None:
    job = _job(fixture)
    folder = fixture[0].parent
    Image.new("RGB", (20, 15), "blue").save(folder / "wrong.png")
    slides = [{"tables": s["expected_tables"], "photos": copy.deepcopy(reports.slide_photos(s)), "omit_alt": True} for s in job["slides"]]
    slides[1]["photos"][-1]["path"] = str(folder / "wrong.png")
    _package(folder / "authored.pptx", slides)
    with pytest.raises(NeedsReview, match="顺序或原字节"):
        reports.prepare_authored_package(folder / "authored.pptx", folder / "prepared.pptx", job, folder / "preparation.json")
    assert not (folder / "prepared.pptx").exists() and not (folder / "preparation.json").exists()
