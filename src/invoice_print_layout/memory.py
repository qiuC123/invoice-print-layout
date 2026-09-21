from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_print_layout.storage import WorkspacePaths, read_history


SHORT_TERM_RETENTION_DAYS = 7
_LEGACY_OUTPUT_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<provider>[^_]+)_"
    r"(?P<amount>\d+(?:\.\d{1,2})?)元_(?P<person>.+)\.pdf$"
)


@dataclass(frozen=True)
class LongTermRecord:
    created_at: str
    invoice_date: str | None
    category: str | None
    provider: str | None
    amount: Decimal | None
    person_name: str | None
    output_name: str


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    age_days: int
    size_bytes: int


@dataclass(frozen=True)
class MemoryStatus:
    summary_path: Path
    record_count: int
    unresolved_count: int
    total_amount: Decimal
    cleanup_candidates: tuple[CleanupCandidate, ...]

    @property
    def cleanup_size_bytes(self) -> int:
        return sum(item.size_bytes for item in self.cleanup_candidates)


def _basename(raw_path: object) -> str:
    if not isinstance(raw_path, str):
        return "未知"
    return raw_path.replace("\\", "/").rsplit("/", 1)[-1]


def _optional_text(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _optional_amount(entry: dict[str, Any]) -> Decimal | None:
    value = entry.get("amount")
    if not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount >= 0 else None


def _record_from_entry(entry: dict[str, Any]) -> LongTermRecord:
    output_name = _basename(entry.get("output"))
    invoice_date = _optional_text(entry, "invoice_date")
    provider = _optional_text(entry, "provider")
    amount = _optional_amount(entry)
    person_name = _optional_text(entry, "person_name")

    legacy = _LEGACY_OUTPUT_PATTERN.fullmatch(output_name)
    if legacy is not None:
        invoice_date = invoice_date or legacy.group("date")
        provider = provider or legacy.group("provider")
        person_name = person_name or legacy.group("person")
        if amount is None:
            amount = Decimal(legacy.group("amount"))

    return LongTermRecord(
        created_at=_optional_text(entry, "created_at") or "未知",
        invoice_date=invoice_date,
        category=_optional_text(entry, "category") or "打车",
        provider=provider,
        amount=amount,
        person_name=person_name,
        output_name=output_name,
    )


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def _created_timestamp(path: Path) -> float:
    metadata_path = path / "metadata.json"
    if metadata_path.is_file():
        try:
            import json

            value = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = value.get("created_at") if isinstance(value, dict) else None
            if isinstance(created_at, str):
                return datetime.fromisoformat(created_at).timestamp()
        except (OSError, ValueError):
            pass
    return path.stat().st_mtime


def list_cleanup_candidates(
    paths: WorkspacePaths,
    *,
    retention_days: int = SHORT_TERM_RETENTION_DAYS,
    now: datetime | None = None,
) -> tuple[CleanupCandidate, ...]:
    """List expired working-memory directories without deleting anything."""
    if retention_days < 1:
        raise ValueError("工作记忆保留天数必须至少为 1 天")
    current_time = (now or datetime.now()).timestamp()
    cutoff_seconds = retention_days * 24 * 60 * 60
    roots = (
        paths.root / "机器人收件箱" / "已处理",
        paths.root / "机器人收件箱" / "已取消",
        paths.root / "机器人收件箱" / "已替换",
        paths.staging,
    )
    candidates: list[CleanupCandidate] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                age_seconds = current_time - _created_timestamp(child)
            except OSError:
                continue
            if age_seconds < cutoff_seconds:
                continue
            candidates.append(
                CleanupCandidate(
                    path=child,
                    age_days=max(0, int(age_seconds // (24 * 60 * 60))),
                    size_bytes=_directory_size(child),
                )
            )
    return tuple(sorted(candidates, key=lambda item: str(item.path)))


def _display(value: str | None) -> str:
    return value or "待补全"


def _render_summary(
    records: list[LongTermRecord],
    candidates: tuple[CleanupCandidate, ...],
    retention_days: int,
    generated_at: datetime,
) -> str:
    total_amount = sum(
        (record.amount for record in records if record.amount is not None),
        start=Decimal("0"),
    )
    unresolved = sum(
        any(
            value is None
            for value in (
                record.invoice_date,
                record.category,
                record.provider,
                record.amount,
                record.person_name,
            )
        )
        for record in records
    )
    lines = [
        "# 票据长期索引摘要",
        "",
        f"更新时间：{generated_at.isoformat(timespec='seconds')}",
        "",
        "该文件是可重建的结构化摘要，不替代原始电子发票、行程单或打印包。",
        "",
        "## 汇总",
        "",
        f"- 已成功处理票据组：{len(records)}",
        f"- 已索引金额合计：{total_amount:.2f} 元",
        f"- 字段待补全的旧记录：{unresolved}",
        f"- 超过 {retention_days} 天的工作记忆清理候选：{len(candidates)}",
        f"- 清理候选合计：{format_bytes(sum(item.size_bytes for item in candidates))}",
        "- 本次只列出候选，没有删除任何文件。",
        "",
        "## 票据记录",
        "",
        "| 处理时间 | 票据日期 | 类别 | 平台 | 金额 | 姓名 | 打印包 |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for record in records:
        amount = f"{record.amount:.2f} 元" if record.amount is not None else "待补全"
        lines.append(
            "| "
            + " | ".join(
                (
                    record.created_at,
                    _display(record.invoice_date),
                    _display(record.category),
                    _display(record.provider),
                    amount,
                    _display(record.person_name),
                    record.output_name,
                )
            )
            + " |"
        )
    if not records:
        lines.append("| — | — | — | — | — | — | — |")

    lines.extend(["", "## 工作记忆清理候选", ""])
    if candidates:
        lines.extend(
            (
                "| 路径 | 已保留 | 大小 |",
                "| --- | ---: | ---: |",
            )
        )
        for candidate in candidates:
            lines.append(
                f"| {candidate.path} | {candidate.age_days} 天 | "
                f"{format_bytes(candidate.size_bytes)} |"
            )
    else:
        lines.append("当前没有达到清理条件的工作记忆。")
    return "\n".join(lines).rstrip() + "\n"


def format_bytes(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def refresh_memory_status(
    paths: WorkspacePaths,
    *,
    retention_days: int = SHORT_TERM_RETENTION_DAYS,
    now: datetime | None = None,
) -> MemoryStatus:
    generated_at = now or datetime.now()
    records = [_record_from_entry(entry) for entry in read_history(paths.history)]
    candidates = list_cleanup_candidates(
        paths,
        retention_days=retention_days,
        now=generated_at,
    )
    content = _render_summary(records, candidates, retention_days, generated_at)
    temporary = paths.memory_summary.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, paths.memory_summary)
    total_amount = sum(
        (record.amount for record in records if record.amount is not None),
        start=Decimal("0"),
    )
    unresolved = sum(
        any(
            value is None
            for value in (
                record.invoice_date,
                record.category,
                record.provider,
                record.amount,
                record.person_name,
            )
        )
        for record in records
    )
    return MemoryStatus(
        summary_path=paths.memory_summary,
        record_count=len(records),
        unresolved_count=unresolved,
        total_amount=total_amount,
        cleanup_candidates=candidates,
    )
