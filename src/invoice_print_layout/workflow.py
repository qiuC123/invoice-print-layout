from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from invoice_print_layout.documents import (
    PdfInspectionError,
    inspect_pdf,
    pair_documents,
)
from invoice_print_layout.layout import (
    LayoutError,
    compose_print_package,
    required_source_fragments,
    validate_print_package,
)
from invoice_print_layout.memory import refresh_memory_status
from invoice_print_layout.models import BatchSummary, OutcomeStatus, TicketGroup
from invoice_print_layout.storage import (
    WorkspacePaths,
    append_history,
    choose_output_paths,
    ensure_workspace,
    is_duplicate,
    move_to_review,
    new_staging_directory,
    read_history,
    safe_filename_part,
    write_report,
)


def _base_name(group: TicketGroup, person_name: str) -> str:
    invoice_date = group.invoice.invoice_date
    if invoice_date is None:
        raise ValueError("电子发票缺少开票日期")
    return (
        f"{invoice_date.isoformat()}_{group.provider.value}_{group.invoice.amount:.2f}元_"
        f"{safe_filename_part(person_name)}"
    )


def _publish_group(
    paths: WorkspacePaths,
    group: TicketGroup,
    person_name: str,
) -> tuple[Path, Path]:
    output_path, archive_path, _ = choose_output_paths(paths, _base_name(group, person_name))
    stage = new_staging_directory(paths)
    staged_output = stage / "打印包.pdf"
    staged_archive = stage / "归档包"
    staged_archive.mkdir()
    published_output = False
    published_archive = False
    try:
        shutil.copy2(group.trip.path, staged_archive / "原始行程单.pdf")
        shutil.copy2(group.invoice.path, staged_archive / "原始电子发票.pdf")
        expected_pages = compose_print_package(
            group.trip.path,
            group.invoice.path,
            staged_output,
        )
        validate_print_package(
            staged_output,
            expected_pages,
            f"{group.invoice.amount:.2f}",
            required_source_fragments(group.trip.path, group.invoice.path),
        )

        os.replace(staged_archive, archive_path)
        published_archive = True
        os.replace(staged_output, output_path)
        published_output = True
    except Exception:
        if published_output and output_path.exists():
            output_path.unlink()
        if published_archive and archive_path.exists():
            shutil.rmtree(archive_path)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return output_path, archive_path


def _summary_lines(summary: BatchSummary) -> list[str]:
    lines = [
        f"处理时间：{datetime.now().isoformat(timespec='seconds')}",
        f"成功：{summary.success_count}",
        f"需要检查：{summary.review_count}",
        f"重复：{summary.duplicate_count}",
        "",
    ]
    for outcome in summary.outcomes:
        lines.append(f"[{outcome.status.value}] {outcome.label}：{outcome.message}")
    return lines


def process_workspace(workspace_root: Path, person_name: str) -> tuple[WorkspacePaths, BatchSummary]:
    paths = ensure_workspace(workspace_root)
    summary = BatchSummary()
    inspected = []
    for pdf_path in sorted(paths.incoming.glob("*.pdf")):
        try:
            inspected.append(inspect_pdf(pdf_path))
        except PdfInspectionError as exc:
            review_dir = move_to_review(paths, [pdf_path], str(exc))
            summary.add(OutcomeStatus.NEEDS_REVIEW, pdf_path.name, str(review_dir))

    groups, pairing_issues = pair_documents(inspected)
    for issue in pairing_issues:
        review_dir = move_to_review(paths, issue.paths, issue.reason)
        summary.add(OutcomeStatus.NEEDS_REVIEW, "配对失败", str(review_dir))

    try:
        history = read_history(paths.history)
    except (OSError, ValueError) as exc:
        summary.add(
            OutcomeStatus.NEEDS_REVIEW,
            "处理记录",
            f"无法读取 processed.jsonl，待处理文件未移动：{exc}",
        )
        write_report(paths.latest_report, _summary_lines(summary))
        return paths, summary
    for group in groups:
        duplicate = is_duplicate(
            history,
            group.invoice.invoice_number,
            group.invoice.sha256,
            group.trip.sha256,
        )
        if duplicate is not None:
            previous_output = str(duplicate.get("output", "未知"))
            reason = f"重复件；已有打印包：{previous_output}"
            review_dir = move_to_review(
                paths,
                [group.trip.path, group.invoice.path],
                reason,
                category="重复件",
            )
            summary.add(OutcomeStatus.DUPLICATE, group.invoice.path.name, str(review_dir))
            continue

        output_path: Path | None = None
        archive_path: Path | None = None
        try:
            output_path, archive_path = _publish_group(paths, group, person_name)
            entry = {
                "record_version": 2,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "category": "打车",
                "provider": group.provider.value,
                "amount": f"{group.invoice.amount:.2f}",
                "person_name": person_name,
                "invoice_date": group.invoice.invoice_date.isoformat()
                if group.invoice.invoice_date is not None
                else None,
                "invoice_number": group.invoice.invoice_number,
                "invoice_hash": group.invoice.sha256,
                "trip_hash": group.trip.sha256,
                "output": str(output_path),
                "archive": str(archive_path),
            }
            append_history(paths.history, entry)
        except Exception as exc:
            if output_path is not None and output_path.exists():
                output_path.unlink()
            if archive_path is not None and archive_path.exists():
                shutil.rmtree(archive_path)
            review_dir = move_to_review(
                paths,
                [group.trip.path, group.invoice.path],
                f"生成打印包失败：{exc}",
            )
            summary.add(OutcomeStatus.NEEDS_REVIEW, group.invoice.path.name, str(review_dir))
            continue

        assert output_path is not None
        assert archive_path is not None
        cleanup_warnings: list[str] = []
        for source in (group.trip.path, group.invoice.path):
            try:
                source.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_warnings.append(f"{source.name} 未能移出待处理：{exc}")
        history.append(entry)
        message = str(output_path)
        if cleanup_warnings:
            message += "；" + "；".join(cleanup_warnings)
        summary.add(OutcomeStatus.SUCCESS, output_path.name, message)

    try:
        refresh_memory_status(paths)
    except (OSError, ValueError) as exc:
        summary.add(
            OutcomeStatus.NEEDS_REVIEW,
            "长期索引摘要",
            f"打印包和原件归档已保留，但摘要未更新：{exc}",
        )
    write_report(paths.latest_report, _summary_lines(summary))
    return paths, summary
