from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

from invoice_print_layout import personnel_handoff as handoff
from invoice_print_layout.cli import app
from invoice_print_layout.construction_reports import NeedsReview, read_object, sha, write_json


def catalog(tmp_path: Path, project: str = "test-project") -> Path:
    source = tmp_path / (project + "-source")
    source.mkdir()
    photos = []
    for photo_id, day, stamp, status in [
        ("PHOTO-003", "2000-01-02", "18:00", "excluded"),
        ("PHOTO-002", "2000-01-01", "13:32", "pending"),
        ("PHOTO-001", "2000-01-01", "08:30:01", "included"),
    ]:
        path = source / (photo_id + ".jpg")
        Image.new("RGB", (8, 8), (len(photos) * 60, 20, 40)).save(path)
        photos.append({"photo_id": photo_id, "date": day, "photo_time": stamp,
                       "project_status": status, "path": path.name, "sha256": sha(path),
                       "sent_at": "", "location": "合成测试场地", "note": "合成记录"})
    write_json(source / "catalog.json", {"schema_version": 1, "project_id": project,
               "project_title": "合成测试项目", "batch_id": "test-batch", "timezone": "Asia/Shanghai", "photos": photos})
    return source / "catalog.json"


def labels(folder: Path, rows: list[list[str]] | None = None) -> None:
    if rows is None:
        rows = [[photo["photo_id"], "搭建工人", "已有文字标注", "classified"] for photo in read_object(folder / "catalog.json")["photos"]]
    with (folder / "classifications.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(handoff.HEADERS)
        writer.writerows(rows)


def package(tmp_path: Path) -> Path:
    path = tmp_path / "package"
    handoff.prepare_package(catalog(tmp_path), path)
    labels(path)
    handoff.seal_package(path)
    return path


def fake_renderer(attempt: Path, runtime: dict) -> None:
    with zipfile.ZipFile(attempt / "time-records.xlsx", "w") as stream:
        stream.writestr("xl/workbook.xml", "<workbook/>")
    write_json(attempt / "QA" / "workbook-check.json", {"synthetic_test_renderer": True})


def files(folder: Path) -> dict[str, tuple[str, int]]:
    return {p.relative_to(folder).as_posix(): (sha(p), p.stat().st_mtime_ns) for p in folder.rglob("*") if p.is_file()}


def receive(folder: Path, tmp_path: Path) -> dict:
    return handoff.receive_package(folder, tmp_path / "outputs", {}, renderer=fake_renderer)


def test_prepare_preserves_bytes_and_refuses_overwrite(tmp_path):
    source = catalog(tmp_path)
    before = files(source.parent)
    target = tmp_path / "handoff"
    result = handoff.prepare_package(source, target)
    assert result["photo_count"] == 3
    for photo in read_object(target / "catalog.json")["photos"]:
        assert sha(target / photo["path"]) == photo["sha256"]
    assert files(source.parent) == before
    assert not (target / "COMPLETE.json").exists()
    with pytest.raises(NeedsReview, match="已存在"):
        handoff.prepare_package(source, target)


def test_receive_sorting_pending_exclusion_and_idempotent_reuse(tmp_path):
    folder = package(tmp_path)
    original = files(folder)
    first = receive(folder, tmp_path)
    ledger = read_object(Path(first["output"]) / "time-records.json")
    assert [r["photo_id"] for r in ledger["records"]] == ["PHOTO-001", "PHOTO-002", "PHOTO-003"]
    assert [r["period"] for r in ledger["records"]] == ["上午", "下午", "晚间"]
    assert ledger["project_photo_counts"] == {"included": 1, "pending": 1, "excluded": 1}
    assert "net_hours" not in ledger["records"][0]
    before = files(tmp_path / "outputs")
    second = receive(folder, tmp_path)
    assert not first["reused"] and second["reused"]
    assert second["output"] == first["output"]
    assert files(tmp_path / "outputs") == before
    assert files(folder) == original


def test_correction_survives_reimport_and_keeps_original_role(tmp_path):
    folder = package(tmp_path)
    original = receive(folder, tmp_path)
    original_files = files(Path(original["output"]))
    handoff.correct_role(folder, tmp_path / "outputs", "PHOTO-002", "AV搭建工人", "用户确认", "卸货人员不同")
    current = receive(folder, tmp_path)
    ledger = read_object(Path(current["output"]) / "time-records.json")
    corrected = ledger["records"][1]
    assert corrected["role"] == "AV搭建工人"
    assert corrected["source_role"] == "搭建工人"
    assert corrected["role_status"] == "user_confirmed"
    assert corrected["user_note"] == "卸货人员不同"
    assert current["output"] != original["output"]
    assert files(Path(original["output"])) == original_files
    assert receive(folder, tmp_path)["reused"]
    # A newly sealed revision of WorkBuddy's old label still cannot erase the correction.
    revision = tmp_path / "revision"
    handoff.prepare_package(folder / "catalog.json", revision)
    data = read_object(revision / "catalog.json")
    data["batch_id"] = "test-batch-revision"
    write_json(revision / "catalog.json", data)
    labels(revision)
    handoff.seal_package(revision)
    result = receive(revision, tmp_path)
    assert read_object(Path(result["output"]) / "time-records.json")["records"][1]["role"] == "AV搭建工人"


def test_latest_user_correction_wins_and_history_is_retained(tmp_path):
    folder = package(tmp_path)
    for role in ("AV搭建工人", "项目经理"):
        handoff.correct_role(folder, tmp_path / "outputs", "PHOTO-001", role, "用户新确认")
    result = receive(folder, tmp_path)
    assert read_object(Path(result["output"]) / "time-records.json")["records"][0]["role"] == "项目经理"
    assert len(read_object(tmp_path / "outputs/test-project/user-corrections.json")["history"]) == 2


def test_correction_rejects_same_id_different_photo(tmp_path):
    folder = package(tmp_path)
    handoff.correct_role(folder, tmp_path / "outputs", "PHOTO-001", "项目经理", "用户确认")
    replacement = tmp_path / "replacement"
    handoff.prepare_package(folder / "catalog.json", replacement)
    data = read_object(replacement / "catalog.json")
    photo = next(p for p in data["photos"] if p["photo_id"] == "PHOTO-001")
    Image.new("RGB", (9, 9), "blue").save(replacement / photo["path"])
    photo["sha256"] = sha(replacement / photo["path"])
    write_json(replacement / "catalog.json", data)
    labels(replacement)
    handoff.seal_package(replacement)
    with pytest.raises(NeedsReview, match="纠正对应的照片已变化"):
        receive(replacement, tmp_path)


def test_projects_have_separate_corrections(tmp_path):
    first = package(tmp_path)
    handoff.correct_role(first, tmp_path / "outputs", "PHOTO-001", "项目经理", "用户确认")
    second = tmp_path / "other-package"
    handoff.prepare_package(catalog(tmp_path, "other-project"), second)
    labels(second)
    handoff.seal_package(second)
    result = receive(second, tmp_path)
    assert result["corrections_applied"] == []
    assert read_object(Path(result["output"]) / "time-records.json")["records"][0]["role"] == "搭建工人"


@pytest.mark.parametrize("mutation", ["missing", "csv", "catalog", "photo"])
def test_incomplete_or_changed_input_cannot_be_received(tmp_path, mutation):
    folder = package(tmp_path)
    if mutation == "missing":
        (folder / "COMPLETE.json").unlink()
    elif mutation == "csv":
        (folder / "classifications.csv").write_text("wrong", encoding="utf-8")
    elif mutation == "catalog":
        data = read_object(folder / "catalog.json")
        data["project_title"] = "改动"
        write_json(folder / "catalog.json", data)
    else:
        (folder / "photos/PHOTO-001.jpg").write_bytes(b"changed")
    with pytest.raises(NeedsReview):
        receive(folder, tmp_path)
    assert not (tmp_path / "outputs").exists()


@pytest.mark.parametrize("field,value", [
    ("date", "2000-02-30"), ("date", "01-01-2000"), ("photo_time", "24:00"),
    ("photo_time", "8:30"), ("project_status", "guess"), ("sha256", "0"*64),
    ("path", "../escape.jpg"), ("sent_at", "2000-01-01T08:30:00"), ("sent_at", "未知"),
])
def test_invalid_catalog_is_rejected(tmp_path, field, value):
    source = catalog(tmp_path)
    data = read_object(source)
    data["photos"][0][field] = value
    write_json(source, data)
    with pytest.raises((ValueError, OSError)):
        handoff.prepare_package(source, tmp_path / "handoff")


@pytest.mark.parametrize("rows", [
    [["PHOTO-001","搭建工人","依据","classified"]],
    [["UNKNOWN","搭建工人","依据","classified"]],
    [["PHOTO-001","搭建工人","依据","classified"]]*3,
    [["PHOTO-001","老板","依据","classified"], ["PHOTO-002","搭建工人","依据","classified"], ["PHOTO-003","搭建工人","依据","classified"]],
    [["PHOTO-001","待确认","依据","classified"], ["PHOTO-002","搭建工人","依据","classified"], ["PHOTO-003","搭建工人","依据","classified"]],
])
def test_bad_classification_cannot_be_sealed(tmp_path, rows):
    folder = tmp_path / "package"
    handoff.prepare_package(catalog(tmp_path), folder)
    labels(folder, rows)
    with pytest.raises(NeedsReview):
        handoff.seal_package(folder)
    assert not (folder / "COMPLETE.json").exists()


def test_explicit_pending_role_is_preserved(tmp_path):
    folder = tmp_path / "package"
    handoff.prepare_package(catalog(tmp_path), folder)
    handoff.seal_package(folder)
    result = receive(folder, tmp_path)
    assert result["role_photo_counts"] == {"待确认": 3}


def test_completion_seal_is_idempotent_and_cannot_overwrite(tmp_path):
    folder = package(tmp_path)
    before = files(folder)
    handoff.seal_package(folder)
    assert files(folder) == before
    rows = [[p["photo_id"], "项目经理", "变更标注", "classified"] for p in read_object(folder / "catalog.json")["photos"]]
    labels(folder, rows)
    with pytest.raises(NeedsReview, match="已完成交接包发生变化"):
        handoff.seal_package(folder)


def test_failed_generation_recovers_without_overwriting_failed_attempt(tmp_path):
    folder = package(tmp_path)
    def fail(attempt, runtime):
        (attempt / "failed-evidence.txt").write_text("故障证据", encoding="utf-8")
        raise RuntimeError("模拟中断")
    with pytest.raises(RuntimeError, match="模拟中断"):
        handoff.receive_package(folder, tmp_path / "outputs", {}, renderer=fail)
    failed = next((tmp_path / "outputs").rglob("failed-evidence.txt"))
    before = (sha(failed), failed.stat().st_mtime_ns)
    result = receive(folder, tmp_path)
    assert result["state"] == "generated" and not result["reused"]
    assert (sha(failed), failed.stat().st_mtime_ns) == before
    assert receive(folder, tmp_path)["reused"]


def test_output_corruption_is_not_returned_as_reusable(tmp_path):
    folder = package(tmp_path)
    result = receive(folder, tmp_path)
    corrupted = Path(result["output"]) / "preview.html"
    corrupted.write_text("corrupted", encoding="utf-8")
    with pytest.raises(NeedsReview, match="旧输出被修改"):
        receive(folder, tmp_path)
    assert corrupted.read_text() == "corrupted"


def test_change_during_generation_cannot_commit_receipt(tmp_path):
    folder = package(tmp_path)
    def change(attempt, runtime):
        fake_renderer(attempt, runtime)
        (folder / "classifications.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(NeedsReview, match="生成期间"):
        handoff.receive_package(folder, tmp_path / "outputs", {}, renderer=change)
    assert not list((tmp_path / "outputs").rglob("receipt.json"))


def test_preview_escapes_supplied_text_and_csv_formula(tmp_path):
    folder = tmp_path / "package"
    handoff.prepare_package(catalog(tmp_path), folder)
    basis = '=formula() </script><script>alert("test")</script>'
    labels(folder, [[p["photo_id"], "搭建工人", basis, "classified"] for p in read_object(folder / "catalog.json")["photos"]])
    handoff.seal_package(folder)
    result = receive(folder, tmp_path)
    output = Path(result["output"])
    preview = (output / "preview.html").read_text(encoding="utf-8")
    assert preview.count("<script>") == 1
    assert "\\u003c/script\\u003e" in preview
    payload = preview.split("const DATA = ", 1)[1].split(";\n// FILTERS_BEGIN", 1)[0]
    assert json.loads(payload)["records"][0]["role_basis"] == basis
    with (output / "time-records.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert next(csv.DictReader(stream))["role_basis"] == "'" + basis


def test_cli_local_sequence(tmp_path, monkeypatch):
    runner = CliRunner()
    source = catalog(tmp_path)
    folder, outputs = tmp_path / "package", tmp_path / "outputs"
    registry = tmp_path / "registry.json"
    write_json(registry, {"runtime": {}})
    monkeypatch.setattr(handoff, "render_workbook", fake_renderer)
    commands = [
        ["role-pack", "--catalog", str(source), "--destination", str(folder)],
        ["seal-roles", "--package", str(folder)],
        ["correct-role", "--package", str(folder), "--output-root", str(outputs), "--photo-id", "PHOTO-001", "--role", "AV搭建工人", "--basis", "用户确认"],
        ["receive-roles", "--package", str(folder), "--output-root", str(outputs), "--registry", str(registry)],
    ]
    for args in commands:
        result = runner.invoke(app, ["daily", *args])
        assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["daily", *commands[-1]])
    assert result.exit_code == 0 and json.loads(result.output)["reused"]
