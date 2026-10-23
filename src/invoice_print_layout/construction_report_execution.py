"""Local presentation execution, shared by report workflows.

Python owns process orchestration so Node does not need to launch PowerPoint.
Only a structured spawn EBUSY from the packaged finalizer permits the explicit
direct-validator alternative. No permission or execution-policy changes occur.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

Json = dict[str, Any]
MODULE_DIR = Path(__file__).parent


class ExecutionError(ValueError):
    """A recorded report step did not pass."""


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path: Path, value: Json) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _object(text: str, label: str) -> Json:
    try:
        result = json.loads(text.lstrip("\ufeff"))
    except (ValueError, TypeError) as exc:
        raise ExecutionError(f"{label} did not return JSON") from exc
    if not isinstance(result, dict):
        raise ExecutionError(f"{label} did not return a JSON object")
    return result


def runtime_environment(runtime: Json) -> dict[str, str]:
    for field in ("node_executable", "python_executable", "artifact_module"):
        value = Path(runtime.get(field, ""))
        if not value.is_absolute() or not value.is_file():
            raise ExecutionError(f"Missing absolute runtime file: {field}")
    skill = Path(runtime.get("skill_dir", ""))
    if not skill.is_absolute() or not (skill / "container_tools/artifact_tool_utils.mjs").is_file():
        raise ExecutionError("Missing presentation skill runtime")
    artifact = Path(runtime["artifact_module"])
    return {**os.environ, "RUNTIME_NODE": str(runtime["node_executable"]),
            "RUNTIME_NODE_MODULES": str(artifact.parents[3]),
            "RUNTIME_PYTHON": str(runtime["python_executable"]),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}


def run_command(argv: Sequence[str], *, runtime: Json, log_path: Path,
                cwd: Path | None = None, timeout: int = 600,
                allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    """Run one bounded command and save stdout/stderr before reporting failure."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout,
                                cwd=cwd, env=runtime_environment(runtime),
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_path.write_text(f"Process could not complete: {exc}\n", encoding="utf-8")
        raise ExecutionError(f"{log_path.stem} could not complete; see {log_path}") from exc
    log_path.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    _write(log_path.with_suffix(".exit.json"), {"exit_code": result.returncode,
                                               "log": str(log_path), "executable": argv[0]})
    if result.returncode and not allow_failure:
        raise ExecutionError(f"{log_path.stem} failed ({result.returncode}); see {log_path}")
    return result


def mark_operation_started(runtime: Json, build_dir: Path) -> None:
    """Mark exactly one authoring operation in a fresh attempt, before editing."""
    marker = build_dir / "operation-marker.json"
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"status": "starting", "operation": "edit"}, stream)
    try:
        run_command([str(runtime["node_executable"]), "container_tools/mark_artifact_operation_started.mjs",
                     "--operation-kind", "edit", "--expected-output-count", "1", "--output-format", "pptx"],
                    runtime=runtime, cwd=Path(runtime["skill_dir"]), log_path=build_dir / "operation-marker.log")
    except Exception:
        _write(marker, {"status": "failed", "operation": "edit"})
        raise
    _write(marker, {"status": "passed", "operation": "edit"})


def _inside(workspace: Path, path: Path, label: str) -> Path:
    resolved = path.resolve()
    if resolved == workspace or not resolved.is_relative_to(workspace):
        raise ExecutionError(f"{label} must be inside the run workspace")
    return resolved


def finalize_presentation(runtime: Json, *, workspace_dir: Path, candidate_path: Path,
                          final_path: Path, expected_slide_count: int,
                          expected_slide_size_emu: str, font_reference_path: Path,
                          required_native_table_owner_slides: Sequence[int] = (),
                          receipt_path: Path) -> Json:
    """Finalize one staged deck, with an audited EBUSY-only alternative.

    Both routes use the packaged validators and actual reference font families.
    Callers still own native rendering, content verification and visual review.
    No source workbook materialization or chart exceptions are requested.
    """
    runtime_environment(runtime)
    workspace = workspace_dir.resolve(strict=True)
    candidate = _inside(workspace, candidate_path, "Candidate")
    final = _inside(workspace, final_path, "Final")
    receipt_path = _inside(workspace, receipt_path, "Receipt")
    reference = font_reference_path.resolve(strict=True)
    if candidate == final or not candidate.is_file() or final.exists():
        raise ExecutionError("Candidate is missing or final already exists")
    if candidate.suffix.lower() != ".pptx" or final.suffix.lower() != ".pptx":
        raise ExecutionError("Candidate and final must be PPTX files")
    if reference in (candidate, final):
        raise ExecutionError("Generated decks cannot approve their own reference fonts")
    if isinstance(expected_slide_count, bool) or not isinstance(expected_slide_count, int) or expected_slide_count < 1:
        raise ExecutionError("Invalid expected slide count")
    if not re.fullmatch(r"[1-9]\d*,[1-9]\d*", expected_slide_size_emu):
        raise ExecutionError("Invalid expected slide dimensions")
    owners = list(required_native_table_owner_slides)
    if len(owners) != len(set(owners)) or any(type(n) is not int or not 1 <= n <= expected_slide_count for n in owners):
        raise ExecutionError("Invalid required native table slides")
    final.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    build = receipt_path.parent
    package = Path(runtime["skill_dir"]) / "container_tools"
    candidate_hash, reference_hash = _sha(candidate), _sha(reference)
    execution: Json = {"finalizer_returned": False, "fallback_used": False, "steps": []}

    def command(name: str, argv: Sequence[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        result = run_command(argv, runtime=runtime, log_path=build / f"finalize-{name}.log", allow_failure=allow_failure)
        execution["steps"].append({"name": name, "exit_code": result.returncode,
                                   "log": str(build / f"finalize-{name}.log")})
        return result

    def validator(name: str, script: str, arguments: Sequence[str]) -> Json:
        result = command(name, [str(runtime["python_executable"]), "-B", str(package / script), *arguments])
        return _object(result.stdout, name)

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ExecutionError(message)

    try:
        fonts = validator("reference-fonts", "inspect_presentation_layout_geometry.py", [str(reference), "--inspect-font-families"])
        source_families = fonts.get("font_families")
        require(isinstance(source_families, list) and bool(source_families) and all(isinstance(f, str) and f.strip() for f in source_families),
                "Reference fonts were not completely inspected")
        families: list[str] = fonts["font_families"]
        options: Json = {"workspaceDir": str(workspace), "candidatePath": str(candidate), "finalPath": str(final),
                         "explicitTotalSlideCount": expected_slide_count,
                         "pythonExecutable": str(runtime["python_executable"]),
                         "integrityValidatorPath": str(package / "inspect_presentation_package_integrity.py"),
                         "layoutValidatorPath": str(package / "inspect_presentation_layout_geometry.py"),
                         "layoutArgs": ["--expected-slide-size-emu", expected_slide_size_emu,
                                        "--validate-bullet-geometry", "--validate-heading-fit"],
                         "requiredNativeTableOwnerSlides": owners,
                         "fontPolicy": {"basis": "reference", "families": families,
                                        "referencePath": str(reference), "referenceSha256": reference_hash},
                         "verifyArtifactToolImport": True, "receiptPath": str(receipt_path)}
        spec_path = build / "finalize-job.json"
        status_path = build / "finalize-status.json"
        if spec_path.exists() or status_path.exists():
            raise ExecutionError("Finalization already attempted; use a fresh attempt")
        _write(spec_path, {"runtime": runtime, "options": options, "status_path": str(status_path)})
        helper = MODULE_DIR / "construction_report_finalize.mjs"
        result = command("formal", [str(runtime["node_executable"]), str(helper), str(spec_path), "finalize"], allow_failure=True)
        status = _object(status_path.read_text(encoding="utf-8-sig"), "finalizer status") if status_path.is_file() else {}
        if result.returncode == 0:
            require(status.get("passed") is True and status.get("finalizer_returned") is True, "Finalizer success receipt is missing")
            require(final.is_file() and _sha(final) == candidate_hash, "Finalizer output differs from the candidate")
            require(_sha(candidate) == candidate_hash and _sha(reference) == reference_hash, "Inputs changed during finalization")
            execution["finalizer_returned"] = True
            receipt = _object(receipt_path.read_text(encoding="utf-8-sig"), "finalizer receipt")
            receipt["execution"] = execution
            _write(receipt_path, receipt)
            return receipt

        require(status.get("failure_kind") == "child_process_ebusy", "Finalizer failed without a structured child-process EBUSY; see finalize-formal.log")
        require(not final.exists(), "Finalizer failed after producing an output; a new attempt is required")
        execution.update(fallback_used=True, fallback_reason="Packaged finalizer child-process EBUSY; validators launched directly by the outer Python process")
        integrity = validator("package-integrity", "inspect_presentation_package_integrity.py", [str(candidate), "--fail-on-findings"])
        require(integrity.get("finding_count") == 0 and integrity.get("findings") == [] and integrity.get("slide_count") == expected_slide_count,
                "Package integrity or slide inventory failed")
        discovered = validator("chart-discovery", "native_quantitative_chart_gate.py", [str(candidate), "--discover-chart-slides"])
        chart_owners = discovered.get("chart_owner_slides", [])
        chart_count = discovered.get("chart_count")
        require(discovered.get("schema_version") == "owner-private.actual-native-chart-discovery.v1"
                and discovered.get("slide_count") == expected_slide_count and isinstance(chart_owners, list)
                and isinstance(chart_count, int) and chart_count >= 0
                and all(type(n) is int and 1 <= n <= expected_slide_count for n in chart_owners)
                and discovered.get("source_attachments_accessed") is False, "Chart discovery returned invalid coverage")
        titles = validator("chart-titles", "native_chart_title_gate.py", [str(candidate)])
        require(titles.get("passed") is True and titles.get("finding_count") == 0
                and titles.get("slide_count") == expected_slide_count and titles.get("native_chart_count") == chart_count,
                "Chart title gate failed")
        layout_arguments = [str(candidate), "--fail-on-findings", "--expected-slide-size-emu", expected_slide_size_emu,
                            "--expected-slide-count", str(expected_slide_count), "--validate-bullet-geometry", "--validate-heading-fit"]
        for owner in owners:
            layout_arguments.extend(["--require-native-table-slide", str(owner)])
        for family in families:
            layout_arguments.extend(["--approved-font-family", family])
        geometry = validator("layout-fonts", "inspect_presentation_layout_geometry.py", layout_arguments)
        require(geometry.get("finding_count") == 0 and geometry.get("slide_count") == expected_slide_count
                and geometry.get("font_policy", {}).get("performed") is True
                and geometry.get("font_policy", {}).get("passed") is True, "Layout or font validation failed")
        arithmetic = validator("table-arithmetic", "native_table_arithmetic_gate.py", [str(candidate), "--fail-on-findings"])
        require(arithmetic.get("passed") is True and arithmetic.get("finding_count") == 0
                and arithmetic.get("slide_count") == expected_slide_count, "Native table arithmetic gate failed")
        charts: Json = {"performed": False, "detectedChartCount": chart_count}
        if chart_count:
            charts = validator("quantitative-charts", "native_quantitative_chart_gate.py",
                               [str(candidate), "--expected-chart-slides", ",".join(str(n) for n in chart_owners)])
            require(charts.get("passed") is True and charts.get("failed_slide_count") == 0
                    and charts.get("validated_slide_count") == len(chart_owners)
                    and charts.get("expected_quantitative_chart_slides") == sorted(chart_owners), "Native quantitative chart gate failed")
        imported = command("first-party-import", [str(runtime["node_executable"]), str(helper), str(spec_path), "import"])
        import_receipt = _object(imported.stdout, "first-party import")
        require(import_receipt.get("passed") is True and import_receipt.get("slideCount") == expected_slide_count
                and import_receipt.get("sha256") == candidate_hash, "First-party import failed or changed source bytes")
        require(_sha(candidate) == candidate_hash and _sha(reference) == reference_hash, "Candidate or font reference changed during validation")
        # Exclusive publication cannot overwrite a former delivery.
        with candidate.open("rb") as source, final.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        require(_sha(final) == candidate_hash, "Published bytes differ from candidate")
        receipt = {"schemaVersion": "presentation-direct-validation.v1", "passed": True,
                   "mode": "direct-packaged-validators-after-ebusy", "finalSha256": candidate_hash,
                   "byteCount": final.stat().st_size, "packageIntegrity": integrity,
                   "referenceFonts": fonts, "fontSelection": options["fontPolicy"],
                   "presentationLayout": geometry, "nativeChartDiscovery": discovered,
                   "nativeChartTitles": titles, "nativeChartValidation": charts,
                   "nativeTableArithmetic": arithmetic, "firstPartyImport": import_receipt, "execution": execution}
        _write(receipt_path, receipt)
        return receipt
    except Exception as exc:
        _write(build / "finalization-failure.json", {"passed": False, "error": str(exc), "execution": execution})
        raise


def render_daily(job_path: Path, runtime: Json) -> None:
    """Execute the daily pipeline without Node spawning native processes."""
    job = _object(job_path.read_text(encoding="utf-8-sig"), "daily job")
    workspace = Path(job["run_dir"])
    build, output = workspace / "build", workspace / "output"
    runtime_environment(runtime)
    shell = runtime.get("powershell_executable") or shutil.which("powershell.exe")
    if not shell or not Path(shell).is_file():
        raise ExecutionError("Windows PowerShell is unavailable")
    for existing in (build / "stage-edited.pptx", build / "candidate.pptx", output / "report.pptx"):
        if existing.exists():
            raise ExecutionError("Attempt already contains a render; use a new attempt")
    output.mkdir(parents=True, exist_ok=True)
    mark_operation_started(runtime, build)
    node = str(runtime["node_executable"])
    editor = str(MODULE_DIR / "construction_report_render.mjs")
    native = str(MODULE_DIR / "construction_report_native.ps1")
    run_command([node, editor, str(job_path), "edit"], runtime=runtime, log_path=build / "renderer.log")
    run_command([str(shell), "-NoProfile", "-NonInteractive", "-File", native, "-JobPath", str(job_path), "-Mode", "compose"],
                runtime=runtime, log_path=build / "native-compose.log")
    composition = _object((build / "composition.json").read_text(encoding="utf-8-sig"), "composition")
    finalize_presentation(runtime, workspace_dir=workspace, candidate_path=build / "candidate.pptx",
                          final_path=output / "report.pptx", expected_slide_count=job["expected_slide_count"],
                          expected_slide_size_emu=composition["slide_size_emu"], font_reference_path=Path(job["stage_template"]),
                          receipt_path=build / "validation.json")
    run_command([str(shell), "-NoProfile", "-NonInteractive", "-File", native, "-JobPath", str(job_path), "-Mode", "verify"],
                runtime=runtime, log_path=build / "native-verify.log")
    run_command([node, editor, str(job_path), "preview"], runtime=runtime, log_path=build / "preview.log")


def execution_code_files() -> list[Path]:
    return [Path(__file__), MODULE_DIR / "construction_report_finalize.mjs"]
