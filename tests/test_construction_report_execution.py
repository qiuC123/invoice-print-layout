"""Synthetic execution cases; no PowerPoint, user documents or model calls."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from invoice_print_layout import construction_report_execution as execution


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sample(tmp_path):
    runtime_root = tmp_path / "runtimes with spaces"
    paths = {"node_executable": runtime_root / "node.exe", "python_executable": runtime_root / "python.exe",
             "artifact_module": runtime_root / "node_modules/@oai/artifact-tool/dist/artifact_tool.mjs",
             "powershell_executable": runtime_root / "powershell.exe"}
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture only, never execute")
    skill = runtime_root / "presentation skill"
    (skill / "container_tools").mkdir(parents=True)
    (skill / "container_tools/artifact_tool_utils.mjs").write_text("fixture only")
    runtime = {key: str(value) for key, value in paths.items()}
    runtime["skill_dir"] = str(skill)
    workspace = tmp_path / "run"
    build = workspace / "build"
    build.mkdir(parents=True)
    candidate = build / "candidate.pptx"
    candidate.write_bytes(b"synthetic candidate bytes")
    reference = tmp_path / "template.pptx"
    reference.write_bytes(b"synthetic reference bytes")
    options = dict(workspace_dir=workspace, candidate_path=candidate, final_path=workspace / "output/report.pptx",
                   expected_slide_count=3, expected_slide_size_emu="9144000,5143500", font_reference_path=reference,
                   required_native_table_owner_slides=[2, 3], receipt_path=build / "validation.json")
    return runtime, options


class FakeTools:
    def __init__(self, options, formal="ebusy", fail_step=None, mutate_import=False):
        self.options, self.formal, self.fail_step = options, formal, fail_step
        self.mutate_import, self.calls = mutate_import, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        name = kwargs["log_path"].stem.removeprefix("finalize-")
        if name == self.fail_step:
            raise execution.ExecutionError("synthetic failing gate")
        code = 0
        if name == "reference-fonts":
            data = {"font_families": ["Latin reference", "中文字体", "Additional font"], "normalized_font_families": []}
        elif name == "formal":
            spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
            if self.formal == "success":
                shutil.copyfile(self.options["candidate_path"], self.options["final_path"])
                put(self.options["receipt_path"], {"finalSha256": execution._sha(self.options["final_path"]), "verified_by": "synthetic finalizer"})
                put(Path(spec["status_path"]), {"passed": True, "finalizer_returned": True})
            else:
                code = 1
                put(Path(spec["status_path"]), {"failure_kind": "child_process_ebusy" if self.formal == "ebusy" else "other",
                                               "error": "Misleading EBUSY text must not enable fallback"})
            data = {}
        elif name == "package-integrity":
            data = {"finding_count": 0, "findings": [], "slide_count": 3}
        elif name == "chart-discovery":
            data = {"schema_version": "owner-private.actual-native-chart-discovery.v1", "chart_count": 0,
                    "chart_owner_slides": [], "slide_count": 3, "source_attachments_accessed": False}
        elif name == "chart-titles":
            data = {"passed": True, "finding_count": 0, "slide_count": 3, "native_chart_count": 0}
        elif name == "layout-fonts":
            data = {"finding_count": 0, "slide_count": 3, "font_policy": {"performed": True, "passed": True}}
        elif name == "table-arithmetic":
            data = {"passed": True, "finding_count": 0, "slide_count": 3}
        elif name == "first-party-import":
            data = {"passed": True, "slideCount": 3, "sha256": execution._sha(self.options["candidate_path"])}
            if self.mutate_import:
                self.options["candidate_path"].write_bytes(b"unexpected alteration")
        else:
            raise AssertionError(name)
        return subprocess.CompletedProcess(argv, code, json.dumps(data), "")


def test_formal_success_preserves_actual_receipt_and_all_reference_fonts(sample, monkeypatch):
    runtime, options = sample
    fake = FakeTools(options, formal="success")
    monkeypatch.setattr(execution, "run_command", fake)
    receipt = execution.finalize_presentation(runtime, **options)
    assert receipt["verified_by"] == "synthetic finalizer"
    assert receipt["execution"]["finalizer_returned"] is True
    assert receipt["execution"]["fallback_used"] is False
    assert len(fake.calls) == 2
    spec = json.loads((options["receipt_path"].parent / "finalize-job.json").read_text(encoding="utf-8"))
    assert spec["options"]["fontPolicy"]["families"] == ["Latin reference", "中文字体", "Additional font"]
    assert spec["options"]["requiredNativeTableOwnerSlides"] == [2, 3]


def test_structured_ebusy_runs_complete_gates_then_publishes_identical_bytes(sample, monkeypatch):
    runtime, options = sample
    fake = FakeTools(options)
    monkeypatch.setattr(execution, "run_command", fake)
    receipt = execution.finalize_presentation(runtime, **options)
    assert receipt["execution"]["finalizer_returned"] is False
    assert receipt["execution"]["fallback_used"] is True
    assert [step["name"] for step in receipt["execution"]["steps"]] == ["reference-fonts", "formal", "package-integrity",
             "chart-discovery", "chart-titles", "layout-fonts", "table-arithmetic", "first-party-import"]
    assert options["final_path"].read_bytes() == options["candidate_path"].read_bytes()
    layout = next(argv for argv, kw in fake.calls if kw["log_path"].stem == "finalize-layout-fonts")
    for font in ["Latin reference", "中文字体", "Additional font"]:
        assert font in layout
    assert layout.count("--require-native-table-slide") == 2
    assert receipt["firstPartyImport"]["passed"] is True


def test_non_ebusy_finalizer_error_stops_without_alternate_or_publication(sample, monkeypatch):
    runtime, options = sample
    fake = FakeTools(options, formal="layout-error")
    monkeypatch.setattr(execution, "run_command", fake)
    with pytest.raises(execution.ExecutionError, match="without a structured"):
        execution.finalize_presentation(runtime, **options)
    assert len(fake.calls) == 2
    assert not options["final_path"].exists()
    failure = json.loads((options["receipt_path"].parent / "finalization-failure.json").read_text())
    assert failure["execution"]["fallback_used"] is False


@pytest.mark.parametrize("gate", ["package-integrity", "chart-discovery", "chart-titles", "layout-fonts", "table-arithmetic", "first-party-import"])
def test_each_fallback_gate_failure_prevents_publication(sample, monkeypatch, gate):
    runtime, options = sample
    fake = FakeTools(options, fail_step=gate)
    monkeypatch.setattr(execution, "run_command", fake)
    with pytest.raises(execution.ExecutionError, match="failing gate"):
        execution.finalize_presentation(runtime, **options)
    assert not options["final_path"].exists()


def test_changed_candidate_during_import_cannot_be_published(sample, monkeypatch):
    runtime, options = sample
    monkeypatch.setattr(execution, "run_command", FakeTools(options, mutate_import=True))
    with pytest.raises(execution.ExecutionError, match="changed during validation"):
        execution.finalize_presentation(runtime, **options)
    assert not options["final_path"].exists()


def test_preexisting_final_is_not_overwritten_or_rechecked(sample, monkeypatch):
    runtime, options = sample
    options["final_path"].parent.mkdir()
    options["final_path"].write_bytes(b"existing delivery")
    fake = FakeTools(options)
    monkeypatch.setattr(execution, "run_command", fake)
    with pytest.raises(execution.ExecutionError, match="already exists"):
        execution.finalize_presentation(runtime, **options)
    assert options["final_path"].read_bytes() == b"existing delivery"
    assert not fake.calls


def test_attempt_cannot_repeat_marker(sample, monkeypatch):
    runtime, options = sample
    calls = []
    monkeypatch.setattr(execution, "run_command", lambda *a, **kw: calls.append((a, kw)))
    execution.mark_operation_started(runtime, options["receipt_path"].parent)
    with pytest.raises(FileExistsError):
        execution.mark_operation_started(runtime, options["receipt_path"].parent)
    assert len(calls) == 1


def test_process_runtime_is_explicit_utf8_and_stderr_is_saved(sample, monkeypatch):
    runtime, options = sample
    monkeypatch.setenv("RUNTIME_NODE", "stale")
    monkeypatch.setenv("PRESERVED_PARENT", "yes")
    captured = []
    def run(argv, **kw):
        captured.append(kw)
        return subprocess.CompletedProcess(argv, 7, "中文输出", "真实错误")
    monkeypatch.setattr(execution.subprocess, "run", run)
    log = options["receipt_path"].parent / "step.log"
    with pytest.raises(execution.ExecutionError, match=r"failed \(7\)"):
        execution.run_command([runtime["node_executable"], "fixture"], runtime=runtime, log_path=log)
    assert log.read_text(encoding="utf-8") == "中文输出\n真实错误"
    assert captured[0]["env"]["RUNTIME_NODE"] == runtime["node_executable"]
    assert captured[0]["env"]["PRESERVED_PARENT"] == "yes"
    assert captured[0]["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured[0]["encoding"] == "utf-8"


def test_outer_daily_pipeline_orders_stages_and_never_changes_execution_policy(sample, monkeypatch):
    runtime, options = sample
    workspace, build = options["workspace_dir"], options["receipt_path"].parent
    options["candidate_path"].unlink()
    job = build / "job.json"
    put(job, {"run_dir": str(workspace), "expected_slide_count": 3, "stage_template": str(options["font_reference_path"])})
    calls = []
    monkeypatch.setattr(execution, "mark_operation_started", lambda *a: calls.append("marker"))
    def run(argv, **kw):
        label = kw["log_path"].stem
        calls.append(label)
        assert "-ExecutionPolicy" not in argv and "Bypass" not in argv
        if label == "native-compose":
            put(build / "composition.json", {"slide_size_emu": "9144000,5143500"})
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(execution, "run_command", run)
    monkeypatch.setattr(execution, "finalize_presentation", lambda *a, **kw: calls.append("finalize"))
    execution.render_daily(job, runtime)
    assert calls == ["marker", "renderer", "native-compose", "finalize", "native-verify", "preview"]


def test_failing_native_compose_does_not_repeat_marker_or_run_finalizer(sample, monkeypatch):
    runtime, options = sample
    workspace, build = options["workspace_dir"], options["receipt_path"].parent
    options["candidate_path"].unlink()
    job = build / "job.json"
    put(job, {"run_dir": str(workspace)})
    calls = []
    monkeypatch.setattr(execution, "mark_operation_started", lambda *a: calls.append("marker"))
    def run(argv, **kw):
        calls.append(kw["log_path"].stem)
        if kw["log_path"].stem == "native-compose":
            raise execution.ExecutionError("native failed")
    monkeypatch.setattr(execution, "run_command", run)
    with pytest.raises(execution.ExecutionError, match="native failed"):
        execution.render_daily(job, runtime)
    assert calls == ["marker", "renderer", "native-compose"]


def test_native_guard_refuses_already_open_document_without_touching_application(tmp_path):
    shell = shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell unavailable; this test does not use Office")
    source = (execution.MODULE_DIR / "construction_report_native.ps1").read_text(encoding="utf-8-sig")
    function = source[source.index("function Open-PrivatePresentation"):source.index("function Save-Json")]
    script = tmp_path / "guard.ps1"
    script.write_text(function + r'''
$openedBefore = @([IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'user-open.pptx')))
$app = $null
try { Open-PrivatePresentation $openedBefore[0]; exit 5 } catch {
    if ($_.Exception.Message -ne 'Refusing to operate a presentation that was already open before this run') { throw }
}
''', encoding="utf-8-sig")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.parametrize("failure,kind", [
    ("Object.assign(new Error('busy'), {code:'EBUSY',syscall:'spawnSync python'})", "child_process_ebusy"),
    ("Object.assign(new Error('denied'), {code:'EPERM',syscall:'spawnSync python'})", "other"),
    ("new Error('EBUSY appearing only in arbitrary text')", "other"),
])
def test_javascript_adapter_distinguishes_spawn_ebusy_from_other_failures(sample, failure, kind):
    runtime, options = sample
    node = os.environ.get("RUNTIME_NODE") or shutil.which("node")
    if not node or not Path(node).is_file():
        pytest.skip("Node unavailable; no Office is used by this adapter test")
    utilities = Path(runtime["skill_dir"]) / "container_tools/artifact_tool_utils.mjs"
    utilities.write_text(f"export async function finalizePresentation() {{throw {failure};}}", encoding="utf-8")
    status = options["receipt_path"].parent / "status.json"
    spec = options["receipt_path"].parent / "adapter-job.json"
    put(spec, {"runtime": runtime, "options": {}, "status_path": str(status)})
    result = subprocess.run([node, str(execution.MODULE_DIR / "construction_report_finalize.mjs"), str(spec), "finalize"],
                            capture_output=True, timeout=20)
    assert result.returncode == 1
    report = json.loads(status.read_text(encoding="utf-8"))
    assert report["failure_kind"] == kind
    assert report["finalizer_returned"] is False
