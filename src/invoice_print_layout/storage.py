from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    incoming: Path
    completed: Path
    archived: Path
    review: Path
    delivery: Path
    config: Path
    history: Path
    staging: Path
    latest_report: Path
    mail_config: Path
    mail_history: Path
    memory_summary: Path


def ensure_workspace(root: Path) -> WorkspacePaths:
    root = root.resolve()
    paths = WorkspacePaths(
        root=root,
        incoming=root / "待处理",
        completed=root / "已完成",
        archived=root / "已归档",
        review=root / "需要检查",
        delivery=root / "交付",
        config=root / "config.toml",
        history=root / "processed.jsonl",
        staging=root / ".staging",
        latest_report=root / "本次处理结果.txt",
        mail_config=root / "mail163.toml",
        mail_history=root / "mail_imports.jsonl",
        memory_summary=root / "长期索引摘要.md",
    )
    for directory in (
        paths.incoming,
        paths.completed,
        paths.archived,
        paths.review,
        paths.delivery,
        paths.staging,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def read_person_name(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    with config_path.open("rb") as stream:
        data = tomllib.load(stream)
    raw = data.get("person", {}).get("name")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def write_person_name(config_path: Path, name: str) -> None:
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    config_path.write_text(f'[person]\nname = "{escaped}"\n', encoding="utf-8")


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "未命名"


def choose_output_paths(paths: WorkspacePaths, base_name: str) -> tuple[Path, Path, str]:
    safe_base = safe_filename_part(base_name)
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"_{counter}"
        candidate = f"{safe_base}{suffix}"
        output = paths.completed / f"{candidate}.pdf"
        archive = paths.archived / candidate
        if not output.exists() and not archive.exists():
            return output, archive, candidate
        counter += 1


def new_staging_directory(paths: WorkspacePaths) -> Path:
    directory = paths.staging / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _unique_child(directory: Path, name: str) -> Path:
    source = Path(name)
    candidate = directory / source.name
    counter = 2
    while candidate.exists():
        candidate = directory / f"{source.stem}_{counter}{source.suffix}"
        counter += 1
    return candidate


def move_to_review(
    paths: WorkspacePaths,
    source_files: tuple[Path, ...] | list[Path],
    reason: str,
    category: str | None = None,
) -> Path:
    parent = paths.review / safe_filename_part(category) if category else paths.review
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    issue_dir = parent / f"{stamp}_{uuid.uuid4().hex[:8]}"
    issue_dir.mkdir(parents=True, exist_ok=False)
    for source in source_files:
        if source.exists():
            shutil.move(str(source), _unique_child(issue_dir, source.name))
    (issue_dir / "原因.txt").write_text(reason + "\n", encoding="utf-8")
    return issue_dir


def read_history(history_path: Path) -> list[dict[str, Any]]:
    if not history_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                entries.append(value)
    return entries


def append_history(history_path: Path, entry: dict[str, Any]) -> None:
    with history_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def is_duplicate(
    history: list[dict[str, Any]],
    invoice_number: str | None,
    invoice_hash: str,
    trip_hash: str,
) -> dict[str, Any] | None:
    for entry in history:
        same_invoice = bool(invoice_number) and entry.get("invoice_number") == invoice_number
        same_hash = (
            entry.get("invoice_hash") == invoice_hash
            or entry.get("trip_hash") == trip_hash
        )
        if same_invoice or same_hash:
            return entry
    return None


def write_report(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
