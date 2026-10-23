"""Offline behavior checks; all projects, evidence and decks below are synthetic."""

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from invoice_print_layout.construction_reports import (
    NeedsReview,
    closeout_batches,
    native_renderer,
    project_config,
    read_object,
    record_visual_review,
    run_report,
    sha,
    validate_inputs,
    verify_package,
)


DAY = "2026-07-14"
PREVIOUS_DAY = "2026-07-13"
CONFIRMED = {"status": "confirmed", "evidence": "Synthetic fixture review"}


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def synthetic_deck(path, slides, images=(), note=""):
    """Only enough OOXML for package checks; never claim this is a native deck."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as package:
        for index in range(1, slides + 1):
            package.writestr(
                f"ppt/slides/slide{index}.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
            )
        package.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<a:t>{note}</a:t></p:notes>",
        )
        for index, content in enumerate(images):
            package.writestr(f"ppt/media/image{index}.png", content)


class Scenario:
    def __init__(self, root, project="project-a", *, stage="assembly", first_day="2026-07-12"):
        self.root = root
        self.project = project
        self.directory = root / project
        self.config_path = self.directory / "config.json"
        self.manifest_path = self.directory / "days" / f"{DAY}.json"
        self.messages_path = self.manifest_path.parent / "messages.json"
        self.registry_path = root / "registry.json"
        self.output = root / "runs"
        self.directory.mkdir(parents=True)
        self.template = self.directory / f"{stage}.pptx"
        self.base = self.manifest_path.parent / "base.pptx"
        synthetic_deck(self.template, 12)
        synthetic_deck(self.base, 7)
        self.photos = []
        for index in range(2):
            photo_path = self.manifest_path.parent / f"photo-{index}.png"
            Image.new("RGB", (12, 8), (index * 80, 100, 140)).save(photo_path)
            self.photos.append(photo_path)
        self.messages = [
            {"message_id": "image-0", "type": 3, "time": f"{DAY}T18:00:00+08:00"},
            {"message_id": "image-1", "type": 3, "time": f"{DAY}T18:00:03+08:00"},
            {"message_id": "closeout", "type": 1, "time": f"{DAY}T18:00:05+08:00", "text": "搭建结束"},
        ]
        self.work = {
            **CONFIRMED, "date": DAY, "start": "09:00", "end": "18:00",
            "prior_as_of": PREVIOUS_DAY, "prior_hours": "12",
        }
        self.config = {
            "schema_version": 1, "project_id": project, "report_title": f"Synthetic {project}",
            "first_day": first_day,
            "source": {"source_id": f"test-source-{project}", "batch_policy": "closeout", "closeout_marker": "搭建结束"},
            "confirmed_work": {DAY: copy.deepcopy(self.work)},
            "stages": {
                stage: {
                    "template": self.template.name, "template_sha256": sha(self.template),
                    "block_start": 4, "block_end": 7, "image_slots": ["photo-one", "photo-two"],
                    "plan_rule": {**CONFIRMED, "rule": f"Next {stage} step"},
                    "edits": [{"id": "heading", "old": "placeholder", "text": "{project_title} {date} 第{ordinal}天"}],
                    "texts": [{"id": "progress", "text": "{progress}; {hours}; {cumulative}"}],
                },
            },
        }
        self.manifest = {
            "schema_version": 1, "project_id": project, "report_date": DAY, "mode": "replay",
            "source_id": f"test-source-{project}", "messages_path": self.messages_path.name,
            "batch": {**CONFIRMED, "message_ids": ["image-0", "image-1"], "closeout_message_id": "closeout"},
            "photos": [
                {"photo_id": f"photo-{index}", "message_id": f"image-{index}", "path": path.name,
                 "sha256": sha(path), "project_id": project, "captured_date": DAY,
                 "review": CONFIRMED.copy(), "quality": "synthetic original"}
                for index, path in enumerate(self.photos)
            ],
            "selected_photos": ["photo-0", "photo-1"],
            "facts": {**CONFIRMED, "stage": stage, "stage_review": CONFIRMED.copy(),
                      "values": {"progress": f"{project} {stage} verified progress"}},
            "base": {**CONFIRMED, "project_id": project, "as_of": PREVIOUS_DAY,
                     "path": self.base.name, "sha256": sha(self.base), "slides": 7},
        }
        registry = read_object(self.registry_path) if self.registry_path.exists() else {"projects": {}, "runs_root": "runs"}
        registry["projects"][project] = {"config": f"{project}/config.json", "aliases": [f"alias-{project}"]}
        dump(self.registry_path, registry)
        self.save()

    def save(self):
        dump(self.config_path, self.config)
        dump(self.manifest_path, self.manifest)
        dump(self.messages_path, self.messages)

    def validate(self):
        self.save()
        return validate_inputs(self.config, self.config_path, self.manifest, self.manifest_path, DAY)

    def run(self, renderer):
        self.save()
        return run_report(self.registry_path, self.project, DAY, renderer=renderer)


class FakeRenderer:
    def __init__(self, *, failure=None, note="", omit_photo=False, missing=None, opened=True):
        self.calls = []
        self.failure = failure
        self.note = note
        self.omit_photo = omit_photo
        self.missing = missing
        self.opened = opened

    def __call__(self, job_path, runtime):
        job = read_object(job_path)
        self.calls.append(job)
        attempt = Path(job["run_dir"])
        if self.failure:
            (attempt / "partial-render.txt").write_text("interrupted synthetic render")
            raise RuntimeError(self.failure)
        output = attempt / "output"
        image_bytes = [Path(item["path"]).read_bytes() for item in job["images"]]
        if self.omit_photo:
            image_bytes.pop()
        synthetic_deck(output / "report.pptx", job["expected_slide_count"], image_bytes, self.note)
        dump(job_path.parent / "validation.json", {"finalSha256": sha(output / "report.pptx"),
             "execution": {"finalizer_returned": True, "fallback_used": False}})
        dump(job_path.parent / "operation-marker.json", {"status": "passed"})
        for mode in ("compose", "verify"):
            dump(job_path.parent / f"window-protection-{mode}.json", {"preserved": True, "quit_called": False})
        if self.missing != "preview":
            (output / "preview.html").write_text("<!doctype html><title>Synthetic preview</title>", encoding="utf-8")
        pages = output / "pages"
        pages.mkdir()
        for index in range(1, job["expected_slide_count"] + 1):
            if self.missing == "page" and index == 2:
                continue
            Image.new("RGB", (16, 9), "white").save(pages / f"slide-{index:03d}.png")
        if self.missing != "receipt":
            dump(job_path.parent / "native-receipt.json", {
                "status": "passed" if self.opened else "failed", "reopened_final": self.opened,
                "slide_count": job["expected_slide_count"], "notes_cleared": True,
                "history_preserved": True,
                "slides": [
                    {"slide": index, "sha256": sha(pages / f"slide-{index:03d}.png"),
                     "editable": {"text": 1, "image": 1 if index == 6 else 0}}
                    for index in range(1, job["expected_slide_count"] + 1)
                    if (pages / f"slide-{index:03d}.png").is_file()
                ],
                "history": [],
            })


@pytest.fixture
def scenario(tmp_path):
    return Scenario(tmp_path)


def test_project_and_stage_configuration_remain_isolated(scenario):
    other = Scenario(scenario.root, "project-b", stage="finishing", first_day="2026-07-11")
    other.work.update(start="08:30", end="20:30", prior_hours="20")
    other.config["confirmed_work"][DAY] = copy.deepcopy(other.work)
    a, _ = scenario.validate()
    b, _ = other.validate()
    assert (a["values"]["ordinal"], a["values"]["hours"], a["values"]["cumulative"]) == ("三", "9", "21")
    assert (b["values"]["ordinal"], b["values"]["hours"], b["values"]["cumulative"]) == ("四", "12", "32")
    assert a["stage_template"] != b["stage_template"]
    assert "project-a assembly" in a["texts"][0]["text"]
    assert "project-b finishing" in b["texts"][0]["text"]
    assert a["insert_after"] == b["insert_after"] == 3
    resolved, _, _ = project_config(scenario.registry_path, "alias-project-b")
    assert resolved["project_id"] == "project-b"
    other.manifest = copy.deepcopy(scenario.manifest)
    with pytest.raises(NeedsReview, match="项目配置不匹配"):
        other.validate()


@pytest.mark.parametrize("project_name", ["project-a", "alias-project-a"])
def test_project_config_selects_only_exact_historical_date_including_alias(scenario, project_name):
    historical = scenario.directory / "historical-config.json"
    historical_config = copy.deepcopy(scenario.config)
    historical_config["report_title"] = "Historical source"
    dump(historical, historical_config)
    registry = read_object(scenario.registry_path)
    registry["projects"][scenario.project]["config_by_date"] = {DAY: str(historical)}
    dump(scenario.registry_path, registry)
    selected, selected_path, _ = project_config(scenario.registry_path, project_name, DAY)
    assert selected["report_title"] == "Historical source"
    assert selected_path == historical
    current, current_path, _ = project_config(scenario.registry_path, project_name)
    assert current_path == scenario.config_path
    assert current["report_title"] != selected["report_title"]


@pytest.mark.parametrize("requested", ["2026-07-13", "2026-07-15", "2027-01-01"])
def test_unmapped_date_uses_current_config_not_nearest_or_latest_history(scenario, requested):
    registry = read_object(scenario.registry_path)
    # These intentionally absent history paths must never be read for another date.
    registry["projects"][scenario.project]["config_by_date"] = {
        "2026-07-12": "missing-older.json", DAY: "missing-nearest.json", "2026-07-20": "missing-latest.json"}
    dump(scenario.registry_path, registry)
    selected, selected_path, _ = project_config(scenario.registry_path, scenario.project, requested)
    assert selected_path == scenario.config_path
    assert selected == scenario.config


@pytest.mark.parametrize("requested", ["20260714", "2026-02-30", "2026-7-14", "yesterday"])
def test_project_config_rejects_invalid_or_noncanonical_iso_date(scenario, requested):
    with pytest.raises(NeedsReview, match="合法的YYYY-MM-DD"):
        project_config(scenario.registry_path, scenario.project, requested)


def test_historical_config_cannot_change_registered_project_identity(scenario):
    other = Scenario(scenario.root, "project-b")
    registry = read_object(scenario.registry_path)
    registry["projects"][scenario.project]["config_by_date"] = {DAY: str(other.config_path)}
    dump(scenario.registry_path, registry)
    with pytest.raises(NeedsReview, match="配置身份或版本不匹配"):
        project_config(scenario.registry_path, scenario.project, DAY)


def test_daily_run_passes_date_to_configuration_and_freezes_chosen_history(scenario):
    historical = scenario.directory / "historical-config.json"
    dump(historical, scenario.config)
    registry = read_object(scenario.registry_path)
    registry["projects"][scenario.project]["config_by_date"] = {DAY: str(historical)}
    dump(scenario.registry_path, registry)
    current = copy.deepcopy(scenario.config)
    current["source"]["source_id"] = "different-current-source"
    dump(scenario.config_path, current)
    state = scenario.run(FakeRenderer())
    assert state["state"] == "native_verified"
    assert str(historical) in state["snapshots"]
    assert str(scenario.config_path) not in state["snapshots"]


def test_adding_unrelated_project_does_not_rerender_or_duplicate_existing_project_snapshots(scenario):
    renderer = FakeRenderer()
    original = scenario.run(renderer)
    folder = Path(original["run_path"])
    snapshots_before = {path.name: (sha(path), path.stat().st_mtime_ns) for path in (folder / "inputs").iterdir()}
    registry_snapshot = Path(original["snapshots"][str(scenario.registry_path)])
    registry_before = registry_snapshot.read_bytes()
    Scenario(scenario.root, "project-b", stage="finishing")
    assert "project-b" in read_object(scenario.registry_path)["projects"]
    repeated = scenario.run(renderer)
    assert repeated["reused"] is True
    assert repeated["fingerprint"] == original["fingerprint"]
    assert repeated["attempt_path"] == original["attempt_path"]
    assert len(renderer.calls) == 1
    assert len(list(folder.glob("attempt-*"))) == 1
    assert snapshots_before == {path.name: (sha(path), path.stat().st_mtime_ns) for path in (folder / "inputs").iterdir()}
    assert registry_snapshot.read_bytes() == registry_before
    assert "project-b" not in read_object(registry_snapshot)["projects"]


def test_same_named_project_and_day_evidence_are_both_frozen_from_their_own_directories(scenario):
    project_evidence = scenario.config_path.parent / "evidence.md"
    day_evidence = scenario.manifest_path.parent / "evidence.md"
    project_evidence.write_text("Synthetic project-level rule", encoding="utf-8")
    day_evidence.write_text("Synthetic day-specific observation", encoding="utf-8")
    scenario.config["evidence_files"] = ["evidence.md"]
    scenario.manifest["evidence_files"] = ["evidence.md"]
    renderer = FakeRenderer()
    original = scenario.run(renderer)
    for source in [project_evidence, day_evidence]:
        assert str(source) in {item["path"] for item in original["inventory"]}
        assert Path(original["snapshots"][str(source)]).read_bytes() == source.read_bytes()
    assert original["snapshots"][str(project_evidence)] != original["snapshots"][str(day_evidence)]
    day_evidence.write_text("Corrected synthetic daily observation", encoding="utf-8")
    revised = scenario.run(renderer)
    assert revised["fingerprint"] != original["fingerprint"]
    assert len(renderer.calls) == 2
    assert Path(original["snapshots"][str(day_evidence)]).read_text(encoding="utf-8") == "Synthetic day-specific observation"


@pytest.mark.parametrize("field,value,match", [
    ("project_id", "project-b", "项目配置不匹配"),
    ("source_id", "test-source-other", "会话不属于"),
    ("report_date", "2026-07-13", "报告日期不匹配"),
    ("mode", "guess", "历史重放"),
    ("issues", ["missing original photo"], "采集尚有缺项"),
])
def test_wrong_project_date_source_or_incomplete_collection_blocks_render(scenario, field, value, match):
    scenario.manifest[field] = value
    renderer = FakeRenderer()
    with pytest.raises(NeedsReview, match=match):
        scenario.run(renderer)
    assert renderer.calls == []
    assert not scenario.output.exists()


def test_missing_day_has_explicit_input_request_and_does_not_reuse_history(scenario):
    renderer = FakeRenderer()
    result = run_report(scenario.registry_path, scenario.project, "2026-07-15", renderer=renderer)
    assert result["state"] == "needs_input"
    assert result["date"] == "2026-07-15"
    assert "起止时间和此前累计" in result["missing"]
    assert renderer.calls == []
    assert not scenario.output.exists()


@pytest.mark.parametrize("timestamp", ["2026-07-13T18:00:00+08:00", f"{DAY}T18:00:00+00:00", f"{DAY}T18:00:00"])
def test_messages_must_be_from_requested_day_in_china_timezone(scenario, timestamp):
    scenario.messages[0]["time"] = timestamp
    with pytest.raises(NeedsReview, match="日期或时区不匹配"):
        scenario.validate()


def test_multiple_closeouts_require_selection_evidence_instead_of_latest_guess(scenario):
    scenario.messages += [
        {"message_id": "later-image", "type": 3, "time": f"{DAY}T20:00:00+08:00"},
        {"message_id": "later-closeout", "type": 1, "time": f"{DAY}T20:00:03+08:00", "text": "搭建结束"},
    ]
    with pytest.raises(NeedsReview, match="多个收工批次"):
        scenario.validate()
    scenario.manifest["batch"]["selection_evidence"] = "Review confirms first batch belongs to this project"
    job, _ = scenario.validate()
    assert job["batch"]["message_ids"] == ["image-0", "image-1"]
    assert job["batch"]["closeout_message_id"] == "closeout"


@pytest.mark.parametrize("change", ["interleaved", "long_gap", "wrong_closeout", "duplicate_id", "missing_member"])
def test_closeout_batch_must_be_exact_contiguous_unique_message_run(scenario, change):
    if change == "interleaved":
        scenario.messages.insert(1, {"message_id": "text-between", "type": 1, "time": f"{DAY}T18:00:01+08:00", "text": "unrelated"})
    elif change == "long_gap":
        scenario.messages[0]["time"] = f"{DAY}T17:00:00+08:00"
    elif change == "wrong_closeout":
        scenario.manifest["batch"]["closeout_message_id"] = "missing-closeout"
    elif change == "duplicate_id":
        scenario.messages.append(copy.deepcopy(scenario.messages[0]))
    else:
        scenario.manifest["batch"]["message_ids"] = ["image-0", "unknown"]
    with pytest.raises(NeedsReview):
        scenario.validate()


def test_explicit_private_chat_batch_requires_evidence_and_contiguous_messages(scenario):
    scenario.config["source"]["batch_policy"] = "explicit"
    scenario.messages.pop()
    with pytest.raises(NeedsReview, match="私聊收工批次"):
        scenario.validate()
    scenario.manifest["batch"]["selection_evidence"] = "User selected exact two photos"
    assert scenario.validate()[0]["batch"]["message_ids"] == ["image-0", "image-1"]
    scenario.messages.insert(1, {"message_id": "between", "type": 1, "time": f"{DAY}T18:00:01+08:00", "text": "other discussion"})
    with pytest.raises(NeedsReview, match="不是连续消息"):
        scenario.validate()


def test_same_second_messages_need_reliable_sequence_including_private_chat(scenario):
    for message in scenario.messages:
        message["time"] = f"{DAY}T18:00:00+08:00"
    with pytest.raises(NeedsReview, match="同秒消息顺序不明"):
        scenario.validate()
    for sequence, message in enumerate(scenario.messages):
        message["sort_seq"] = sequence
    # Input file order and lexicographic IDs are not chronology.
    scenario.messages.reverse()
    assert scenario.validate()[0]["batch"]["message_ids"] == ["image-0", "image-1"]
    scenario.config["source"]["batch_policy"] = "explicit"
    scenario.manifest["batch"]["selection_evidence"] = "Verified exact private-chat photos"
    assert scenario.validate()[0]["batch"]["message_ids"] == ["image-0", "image-1"]
    scenario.messages[0]["sort_seq"] = scenario.messages[1]["sort_seq"]
    with pytest.raises(NeedsReview, match="同秒消息顺序不明"):
        scenario.validate()


@pytest.mark.parametrize("field,value,match", [
    ("captured_date", PREVIOUS_DAY, "拍摄日期或项目不匹配"),
    ("project_id", "project-b", "拍摄日期或项目不匹配"),
    ("review", {"status": "pending", "evidence": "unreadable watermark"}, "水印日期/地点/项目"),
    ("quality", "", "质量说明"),
    ("sha256", "0" * 64, "内容已变化"),
])
def test_photo_watermark_project_quality_and_original_bytes_are_gated(scenario, field, value, match):
    scenario.manifest["photos"][0][field] = value
    with pytest.raises(NeedsReview, match=match):
        scenario.validate()


def test_missing_batch_photo_blocks_even_when_selection_is_adjusted(scenario):
    scenario.manifest["photos"].pop()
    scenario.manifest["selected_photos"].pop()
    with pytest.raises(NeedsReview, match="照片未补齐"):
        scenario.validate()


def test_duplicate_selected_photo_bytes_are_rejected(scenario):
    scenario.photos[1].write_bytes(scenario.photos[0].read_bytes())
    scenario.manifest["photos"][1]["sha256"] = sha(scenario.photos[1])
    with pytest.raises(NeedsReview, match="所选照片内容重复"):
        scenario.validate()


@pytest.mark.parametrize("selected", [["photo-0"], ["photo-0", "photo-0"], ["photo-0", "foreign-photo"]])
def test_selection_must_fill_slots_with_unique_verified_photos(scenario, selected):
    scenario.manifest["selected_photos"] = selected
    with pytest.raises(NeedsReview, match="选图"):
        scenario.validate()


def test_work_never_falls_back_to_a_different_date(scenario):
    scenario.config["confirmed_work"] = {PREVIOUS_DAY: scenario.work}
    with pytest.raises(NeedsReview, match="当天起止时间"):
        scenario.validate()


@pytest.mark.parametrize("field,value,match", [
    ("date", PREVIOUS_DAY, "工时或此前累计的截止日期"),
    ("prior_as_of", "2026-07-12", "工时或此前累计的截止日期"),
    ("status", "pending", "当天起止时间"),
    ("evidence", "", "当天起止时间"),
])
def test_work_requires_same_day_confirmation_and_previous_day_cumulative_cutoff(scenario, field, value, match):
    scenario.manifest["work"] = {**scenario.work, field: value}
    with pytest.raises(NeedsReview, match=match):
        scenario.validate()


def test_confirmed_work_can_be_overridden_only_explicitly_and_facts_cannot_override_hours(scenario):
    scenario.manifest["work"] = {**scenario.work, "start": "08:00", "end": "20:00", "prior_hours": "15"}
    values = scenario.validate()[0]["values"]
    assert (values["hours"], values["cumulative"], values["ordinal"]) == ("12", "27", "三")
    assert values["project_title"] == "Synthetic project-a"
    scenario.manifest["facts"]["values"].update(hours="99", cumulative="999", ordinal="九十九", project_title="Foreign project")
    with pytest.raises(NeedsReview):
        scenario.validate()


@pytest.mark.parametrize("change,match", [
    ("unknown_stage", "模板映射"),
    ("unconfirmed_stage", "当天施工阶段"),
    ("unconfirmed_plan", "明日计划"),
    ("missing_fact", "缺少当天内容字段"),
    ("template_changed", "阶段模板已变化"),
    ("base_changed", "阶段模板已变化"),
    ("base_project", "历史基底"),
    ("base_cutoff", "历史基底"),
    ("not_four_pages", "四页"),
])
def test_stage_facts_and_project_history_must_match_confirmed_template(scenario, change, match):
    stage = scenario.config["stages"]["assembly"]
    if change == "unknown_stage":
        scenario.manifest["facts"]["stage"] = "finishing"
    elif change == "unconfirmed_stage":
        scenario.manifest["facts"]["stage_review"]["status"] = "pending"
    elif change == "unconfirmed_plan":
        stage["plan_rule"]["status"] = "pending"
    elif change == "missing_fact":
        scenario.manifest["facts"]["values"].pop("progress")
    elif change == "template_changed":
        scenario.template.write_bytes(b"changed template")
    elif change == "base_changed":
        scenario.base.write_bytes(b"changed history")
    elif change == "base_project":
        scenario.manifest["base"]["project_id"] = "project-b"
    elif change == "base_cutoff":
        scenario.manifest["base"]["as_of"] = "2026-07-12"
    else:
        stage["block_end"] = 8
    with pytest.raises(NeedsReview, match=match):
        scenario.validate()


@pytest.mark.parametrize("drop_blank_last", [False, True])
def test_confirmed_final_stage_runs_three_pages_without_next_day_plan(scenario, drop_blank_last):
    stage = scenario.config["stages"]["assembly"]
    stage.update(block_end=6, is_final_stage=True)
    stage["plan_rule"] = {**CONFIRMED, "omit_next_day_plan": True}
    scenario.manifest["base"]["drop_blank_last"] = drop_blank_last
    expected_slides = 7 - int(drop_blank_last) + 3
    job, _ = scenario.validate()
    assert (job["block_start"], job["block_end"], job["expected_slide_count"]) == (4, 6, expected_slides)
    renderer = FakeRenderer()
    result = scenario.run(renderer)
    assert result["state"] == "native_verified"
    assert result["package"]["slides"] == expected_slides
    assert renderer.calls[0]["expected_slide_count"] == expected_slides
    assert scenario.run(renderer)["reused"] is True
    assert len(renderer.calls) == 1


@pytest.mark.parametrize("change,match", [
    ("ordinary_three_pages", "四页"),
    ("missing_omit_rule", "四页"),
    ("missing_final_marker", "明确标记"),
    ("string_final_marker", "明确标记"),
    ("numeric_final_marker", "明确标记"),
    ("string_omit_rule", "布尔值"),
    ("pending_rule", "最终阶段省略明日计划"),
    ("missing_rule_evidence", "最终阶段省略明日计划"),
    ("unconfirmed_day_stage", "当天施工阶段"),
    ("two_pages", "三页"),
    ("four_pages", "三页"),
])
def test_three_page_stage_requires_explicit_confirmed_final_stage_policy(scenario, change, match):
    stage = scenario.config["stages"]["assembly"]
    stage.update(block_end=6, is_final_stage=True)
    stage["plan_rule"] = {**CONFIRMED, "omit_next_day_plan": True}
    if change == "ordinary_three_pages":
        stage.pop("is_final_stage")
        stage["plan_rule"].pop("omit_next_day_plan")
    elif change == "missing_omit_rule":
        stage["plan_rule"].pop("omit_next_day_plan")
    elif change == "missing_final_marker":
        stage.pop("is_final_stage")
    elif change == "string_final_marker":
        stage["is_final_stage"] = "true"
    elif change == "numeric_final_marker":
        stage["is_final_stage"] = 1
    elif change == "string_omit_rule":
        stage["plan_rule"]["omit_next_day_plan"] = "true"
    elif change == "pending_rule":
        stage["plan_rule"]["status"] = "pending"
    elif change == "missing_rule_evidence":
        stage["plan_rule"]["evidence"] = ""
    elif change == "unconfirmed_day_stage":
        scenario.manifest["facts"]["stage_review"]["status"] = "pending"
    elif change == "two_pages":
        stage["block_end"] = 5
    else:
        stage["block_end"] = 7
    renderer = FakeRenderer()
    with pytest.raises(NeedsReview, match=match):
        scenario.run(renderer)
    assert renderer.calls == []
    assert not scenario.output.exists()


def test_four_page_stage_still_uses_next_day_plan_when_omission_is_false(scenario):
    stage = scenario.config["stages"]["assembly"]
    stage["is_final_stage"] = True
    stage["plan_rule"]["omit_next_day_plan"] = False
    job, _ = scenario.validate()
    assert (job["block_start"], job["block_end"], job["expected_slide_count"]) == (4, 7, 11)
    stage["plan_rule"]["status"] = "pending"
    with pytest.raises(NeedsReview, match="对应阶段的明日计划"):
        scenario.validate()


def test_success_is_native_verified_until_explicit_visual_review_and_reuses_exact_artifacts(scenario):
    renderer = FakeRenderer()
    source_before = {path: path.read_bytes() for path in [scenario.base, scenario.template, *scenario.photos]}
    result = scenario.run(renderer)
    assert result["state"] == "native_verified"
    assert result["visual_review"] == "pending"
    assert result["mode"] == "replay"
    assert result["sent"] is result["project_delivery_written"] is False
    assert result["sources_unchanged"] is True
    assert result["package"] == {"slides": 11, "embedded_source_bytes_verified": True, "notes_empty": True}
    folder = Path(result["run_path"])
    snapshots_before = {path.name: (sha(path), path.stat().st_mtime_ns) for path in (folder / "inputs").iterdir()}
    for original, snapshot in result["snapshots"].items():
        assert Path(original).read_bytes() == Path(snapshot).read_bytes()
    job = renderer.calls[0]
    assert Path(job["base_pptx"]).parent == folder / "inputs"
    assert Path(job["stage_template"]).parent == folder / "inputs"
    assert all(Path(picture["path"]).parent == folder / "inputs" for picture in job["images"])
    again = scenario.run(renderer)
    assert again["reused"] is True
    assert again["attempt_path"] == result["attempt_path"]
    assert again["artifacts"] == result["artifacts"]
    assert len(renderer.calls) == 1
    assert len(list(folder.glob("attempt-*"))) == 1
    assert snapshots_before == {path.name: (sha(path), path.stat().st_mtime_ns) for path in (folder / "inputs").iterdir()}
    assert source_before == {path: path.read_bytes() for path in source_before}
    with pytest.raises(NeedsReview, match="核验人员与证据"):
        record_visual_review(folder, "", "every synthetic page inspected")
    completed = record_visual_review(folder, "test reviewer", "Synthetic page review, not a native PowerPoint acceptance")
    assert completed["state"] == "complete"
    assert completed["visual_review"]["reviewer"] == "test reviewer"
    assert scenario.run(renderer)["state"] == "complete"
    assert len(renderer.calls) == 1


def test_failed_attempt_preserved_and_retry_uses_same_snapshots_in_new_attempt(scenario):
    failed_renderer = FakeRenderer(failure="synthetic interruption")
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        scenario.run(failed_renderer)
    state_path = next(scenario.output.rglob("state.json"))
    failed = read_object(state_path)
    assert failed["state"] == "failed"
    first_attempt = Path(failed["attempt_path"])
    assert (first_attempt / "partial-render.txt").read_text() == "interrupted synthetic render"
    snapshot_paths = sorted((state_path.parent / "inputs").iterdir())
    snapshot_before = {p: (sha(p), p.stat().st_mtime_ns) for p in snapshot_paths}
    with pytest.raises(NeedsReview, match="先完成原生渲染"):
        record_visual_review(state_path.parent, "test reviewer", "cannot approve failed output")
    recovered = scenario.run(FakeRenderer())
    assert recovered["state"] == "native_verified"
    assert recovered["fingerprint"] == failed["fingerprint"]
    assert recovered["attempt_path"] != failed["attempt_path"]
    assert (first_attempt / "partial-render.txt").exists()
    assert snapshot_before == {p: (sha(p), p.stat().st_mtime_ns) for p in snapshot_paths}
    assert sorted((state_path.parent / "inputs").iterdir()) == snapshot_paths


@pytest.mark.parametrize("artifact", ["report.pptx", "preview.html", "slide-002.png"])
def test_modified_output_is_neither_reused_nor_visually_approved(scenario, artifact):
    renderer = FakeRenderer()
    result = scenario.run(renderer)
    path = next(Path(item["path"]) for item in result["artifacts"] if Path(item["path"]).name == artifact)
    path.write_bytes(b"user modified output")
    with pytest.raises(NeedsReview, match="既有产物发生变化"):
        scenario.run(renderer)
    with pytest.raises(NeedsReview, match="待核验产物已变化"):
        record_visual_review(Path(result["run_path"]), "test reviewer", "cannot approve modified output")
    assert len(renderer.calls) == 1
    assert path.read_bytes() == b"user modified output"


def test_original_native_receipt_is_part_of_immutable_review_evidence(scenario):
    renderer = FakeRenderer()
    result = scenario.run(renderer)
    receipt_path = Path(result["native_receipt"])
    receipt = read_object(receipt_path)
    receipt["history_preserved"] = False
    dump(receipt_path, receipt)
    with pytest.raises(NeedsReview, match="既有产物发生变化"):
        scenario.run(renderer)
    with pytest.raises(NeedsReview, match="待核验产物已变化"):
        record_visual_review(Path(result["run_path"]), "test reviewer", "cannot approve changed native evidence")
    assert len(renderer.calls) == 1


@pytest.mark.parametrize("change", ["wrong_page_hash", "missing_page_record", "duplicate_page_record"])
def test_native_receipt_must_describe_each_actual_exported_page(scenario, change):
    renderer = FakeRenderer()

    def stale_receipt(job_path, runtime):
        renderer(job_path, runtime)
        receipt_path = job_path.parent / "native-receipt.json"
        receipt = read_object(receipt_path)
        if change == "wrong_page_hash":
            receipt["slides"][0]["sha256"] = "0" * 64
        elif change == "missing_page_record":
            receipt["slides"].pop()
        else:
            receipt["slides"][0] = copy.deepcopy(receipt["slides"][1])
        dump(receipt_path, receipt)

    with pytest.raises(NeedsReview):
        scenario.run(stale_receipt)
    assert read_object(next(scenario.output.rglob("state.json")))["state"] == "failed"


def test_new_confirmed_revision_retains_previous_completed_delivery(scenario):
    renderer = FakeRenderer()
    original = scenario.run(renderer)
    record_visual_review(Path(original["run_path"]), "test reviewer", "Synthetic original revision reviewed")
    original_artifacts = {item["path"]: item["sha256"] for item in original["artifacts"]}
    scenario.manifest["facts"]["values"]["progress"] = "Explicitly corrected synthetic progress"
    revised = scenario.run(renderer)
    assert revised["fingerprint"] != original["fingerprint"]
    assert revised["run_path"] != original["run_path"]
    assert revised["state"] == "native_verified"
    assert read_object(Path(original["run_path"]) / "state.json")["state"] == "complete"
    assert all(sha(Path(path)) == digest for path, digest in original_artifacts.items())
    assert len(renderer.calls) == 2


@pytest.mark.parametrize("options,match", [
    ({"note": "production note must not appear"}, "制作备注"),
    ({"omit_photo": True}, "缺少所选照片原字节"),
    ({"missing": "preview"}, "HTML预览缺失"),
    ({"missing": "page"}, "原生页面"),
    ({"opened": False}, "原生验证回执不完整"),
])
def test_bad_renderer_result_stays_failed_and_cannot_be_reviewed(scenario, options, match):
    with pytest.raises(NeedsReview, match=match):
        scenario.run(FakeRenderer(**options))
    state_path = next(scenario.output.rglob("state.json"))
    state = read_object(state_path)
    assert state["state"] == "failed"
    assert state["sent"] is False
    assert "artifacts" not in state
    with pytest.raises(NeedsReview, match="先完成原生渲染"):
        record_visual_review(state_path.parent, "test reviewer", "not approved")


def test_source_changed_during_render_blocks_completion(scenario):
    renderer = FakeRenderer()

    def modifying_renderer(job_path, runtime):
        renderer(job_path, runtime)
        scenario.photos[0].write_bytes(b"external source change during render")

    with pytest.raises(NeedsReview, match="原件发生变化"):
        scenario.run(modifying_renderer)
    state = read_object(next(scenario.output.rglob("state.json")))
    assert state["state"] == "failed"
    assert "sources_unchanged" not in state


def test_package_verification_requires_real_byte_hashes_and_exact_slide_count(tmp_path):
    path = tmp_path / "synthetic.pptx"
    content = b"synthetic image payload"
    synthetic_deck(path, 4, [content])
    verify_package(path, 4, [hashlib.sha256(content).hexdigest()])
    with pytest.raises(NeedsReview, match="页数不符"):
        verify_package(path, 5, [])
    with pytest.raises(NeedsReview, match="缺少所选照片"):
        verify_package(path, 4, [hashlib.sha256(b"other photo").hexdigest()])


@pytest.mark.parametrize("failed", [False, True])
def test_native_renderer_uses_outer_execution_and_preserves_failure(tmp_path, monkeypatch, failed):
    from invoice_print_layout.construction_report_execution import ExecutionError
    job_path = tmp_path / "build" / "job.json"
    dump(job_path, {"run_dir": str(tmp_path / "run")})
    runtime = {"test": "configured runtime"}
    calls = []
    def execute(path, configured):
        calls.append((path, configured))
        if failed:
            raise ExecutionError("synthetic finalization failure")
    monkeypatch.setattr("invoice_print_layout.construction_report_execution.render_daily", execute)
    if failed:
        with pytest.raises(NeedsReview, match="渲染或PowerPoint验证失败"):
            native_renderer(job_path, runtime)
    else:
        native_renderer(job_path, runtime)
    assert calls == [(job_path, runtime)]


def test_execution_helper_change_creates_new_fingerprint(scenario, tmp_path, monkeypatch):
    helper = tmp_path / "execution-helper.py"
    helper.write_text("version one")
    monkeypatch.setattr("invoice_print_layout.construction_report_execution.execution_code_files", lambda: [helper])
    first = scenario.run(FakeRenderer())
    helper.write_text("version two")
    second = scenario.run(FakeRenderer())
    assert first["fingerprint"] != second["fingerprint"]
    assert first["run_path"] != second["run_path"]


@pytest.mark.parametrize("proof", ["validation.json", "operation-marker.json", "window-protection-verify.json"])
def test_execution_evidence_is_immutable_for_reuse_and_review(scenario, proof):
    state = scenario.run(FakeRenderer())
    folder = Path(state["run_path"])
    path = Path(state["attempt_path"]) / "build" / proof
    dump(path, {"tampered": True})
    with pytest.raises(NeedsReview, match="既有产物发生变化"):
        scenario.run(FakeRenderer())
    with pytest.raises(NeedsReview, match="待核验产物已变化"):
        record_visual_review(folder, "reviewer", "must not approve changed evidence")


@pytest.mark.parametrize("proof,value,match", [
    ("validation.json", {"finalSha256": "wrong"}, "最终化回执"),
    ("window-protection-compose.json", {"preserved": False, "quit_called": False}, "窗口保护"),
    ("operation-marker.json", {"status": "failed"}, "编辑开始标记"),
])
def test_invalid_execution_proof_blocks_state_completion(scenario, proof, value, match):
    def bad_proof(job_path, runtime):
        FakeRenderer()(job_path, runtime)
        dump(job_path.parent / proof, value)
    with pytest.raises(NeedsReview, match=match):
        scenario.run(bad_proof)
    state = read_object(next(scenario.output.rglob("state.json")))
    assert state["state"] == "failed"
    assert "artifacts" not in state


def test_closeout_discovery_does_not_cross_unrelated_text_or_long_silence():
    messages = [
        {"message_id": "morning", "type": 3, "time": f"{DAY}T09:00:00+08:00"},
        {"message_id": "image", "type": 3, "time": f"{DAY}T18:00:00+08:00"},
        {"message_id": "text", "type": 1, "time": f"{DAY}T18:00:01+08:00", "text": "unrelated"},
        {"message_id": "closeout", "type": 1, "time": f"{DAY}T18:00:02+08:00", "text": "搭建结束"},
    ]
    assert closeout_batches(messages, DAY, "搭建结束") == []
    messages.pop(2)
    assert closeout_batches(messages, DAY, "搭建结束") == [{"message_ids": ["image"], "closeout_message_id": "closeout"}]
