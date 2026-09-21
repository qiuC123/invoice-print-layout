from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pymupdf

from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.storage import (
    WorkspacePaths,
    new_staging_directory,
    read_history,
    safe_filename_part,
)


class DeliveryError(ValueError):
    """Raised when a complete delivery bundle cannot be produced safely."""


@dataclass(frozen=True)
class DeliveryItem:
    invoice_date: str
    category: str
    provider: str
    amount: Decimal
    person_name: str
    print_package: Path


@dataclass(frozen=True)
class DeliveryBundle:
    pdf_path: Path
    markdown_path: Path
    item_count: int
    total_amount: Decimal


def _required_text(entry: dict[str, Any], key: str, label: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError(f"处理记录缺少{label}")
    return value.strip()


def _item_from_entry(entry: dict[str, Any], output_path: Path) -> DeliveryItem:
    raw_amount = entry.get("amount")
    try:
        amount = Decimal(str(raw_amount))
    except (InvalidOperation, ValueError) as exc:
        raise DeliveryError("处理记录中的金额无效") from exc
    if amount < 0:
        raise DeliveryError("处理记录中的金额不能为负数")
    return DeliveryItem(
        invoice_date=_required_text(entry, "invoice_date", "票据日期"),
        category=_required_text(entry, "category", "费用类别"),
        provider=_required_text(entry, "provider", "平台"),
        amount=amount,
        person_name=_required_text(entry, "person_name", "姓名"),
        print_package=output_path,
    )


def _resolve_items(paths: WorkspacePaths, output_paths: list[Path]) -> list[DeliveryItem]:
    entries = read_history(paths.history)
    by_output: dict[Path, dict[str, Any]] = {}
    for entry in entries:
        raw_output = entry.get("output")
        if isinstance(raw_output, str):
            by_output[Path(raw_output).resolve()] = entry

    items: list[DeliveryItem] = []
    for output_path in output_paths:
        resolved = output_path.resolve()
        if not resolved.is_file():
            raise DeliveryError(f"打印包不存在：{resolved.name}")
        matched_entry = by_output.get(resolved)
        if matched_entry is None:
            raise DeliveryError(f"打印包没有对应处理记录：{resolved.name}")
        items.append(_item_from_entry(matched_entry, resolved))
    if not items:
        raise DeliveryError("没有可交付的打印包")
    return items


def outputs_for_processing_date(paths: WorkspacePaths, target: date) -> list[Path]:
    """Select completed ledger entries, never input files or previous delivery bundles."""
    selected: dict[Path, None] = {}
    for entry in read_history(paths.history):
        raw_created = entry.get("created_at")
        if not isinstance(raw_created, str):
            raise DeliveryError("处理记录缺少处理时间，无法按日期汇总")
        try:
            created = datetime.fromisoformat(raw_created)
        except ValueError as exc:
            raise DeliveryError("处理记录的处理时间无效，无法按日期汇总") from exc
        # Older local records are naive Beijing times; normalize offset-aware ones.
        if created.tzinfo is not None:
            created = created.astimezone(timezone(timedelta(hours=8)))
        if created.date() == target:
            selected[Path(_required_text(entry, "output", "打印包路径")).resolve()] = None
    outputs = list(selected)
    if outputs:
        _resolve_items(paths, outputs)  # Fail before creating a job if old metadata is incomplete.
    return outputs


def _unique_delivery_paths(paths: WorkspacePaths, base_name: str) -> tuple[Path, Path]:
    safe_base = safe_filename_part(base_name)
    counter = 1
    while True:
        suffix = "" if counter == 1 else f"_{counter}"
        pdf_path = paths.delivery / f"{safe_base}{suffix}.pdf"
        markdown_path = paths.delivery / f"{safe_base}{suffix}.md"
        if not pdf_path.exists() and not markdown_path.exists():
            return pdf_path, markdown_path
        counter += 1


def _normalized_page_text(page: pymupdf.Page) -> str:
    return re.sub(r"\s+", "", page.get_text("text"))


def _page_visual_digest(page: pymupdf.Page) -> str:
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(0.75, 0.75), colorspace=pymupdf.csGRAY)
    return hashlib.sha256(pixmap.samples).hexdigest()


def _compose_total_pdf(source_paths: list[Path], output_path: Path) -> None:
    output: pymupdf.Document | None = None
    sources: list[pymupdf.Document] = []
    try:
        output = pymupdf.open()
        for source_path in source_paths:
            source = pymupdf.open(source_path)
            sources.append(source)
            if source.needs_pass:
                raise DeliveryError(f"打印包已加密：{source_path.name}")
            if source.page_count < 1:
                raise DeliveryError(f"打印包没有页面：{source_path.name}")
            output.insert_pdf(source)
        output.save(output_path, garbage=4, deflate=True)
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"合并总 PDF 失败：{exc}") from exc
    finally:
        if output is not None:
            output.close()
        for source in sources:
            source.close()


def _validate_total_pdf(output_path: Path, source_paths: list[Path]) -> None:
    expected_pages: list[tuple[str, str]] = []
    try:
        for source_path in source_paths:
            with pymupdf.open(source_path) as source:
                expected_pages.extend(
                    (
                        _normalized_page_text(source.load_page(index)),
                        _page_visual_digest(source.load_page(index)),
                    )
                    for index in range(source.page_count)
                )
        with pymupdf.open(output_path) as output:
            if output.page_count != len(expected_pages):
                raise DeliveryError(
                    f"总 PDF 页数错误：预期 {len(expected_pages)}，实际 {output.page_count}"
                )
            for page_index, (expected_text, expected_visual) in enumerate(expected_pages):
                page = output.load_page(page_index)
                if abs(page.rect.width - A4_WIDTH) > 1 or abs(page.rect.height - A4_HEIGHT) > 1:
                    raise DeliveryError(f"总 PDF 第 {page_index + 1} 页不是 A4")
                text_matches = bool(expected_text) and _normalized_page_text(page) == expected_text
                visual_matches = _page_visual_digest(page) == expected_visual
                if not text_matches and not visual_matches:
                    raise DeliveryError(f"总 PDF 第 {page_index + 1} 页内容校验失败")
    except DeliveryError:
        raise
    except Exception as exc:
        raise DeliveryError(f"总 PDF 无法校验：{exc}") from exc


def _render_markdown(
    items: list[DeliveryItem], generated_at: datetime, report_date: date | None = None
) -> str:
    category_totals: dict[str, Decimal] = {}
    for item in items:
        category_totals[item.category] = category_totals.get(
            item.category, Decimal("0")
        ) + item.amount
    total = sum(category_totals.values(), start=Decimal("0"))
    lines = [
        f"# {(report_date or generated_at.date()).isoformat()} 费用清单",
        "",
        f"生成时间：{generated_at.isoformat(timespec='seconds')}",
        "",
        *([
            f"汇总范围：处理日期为 {report_date.isoformat()}（北京时间）的已完成票据，截至本次生成时间；不是按消费日期或开票日期筛选。",
            "仅合并已完成打印包，不重新处理、不重复计费；不包含待处理和需要检查的材料。",
            "",
        ] if report_date else []),
        "打印设置：最终总 PDF 选择 A4、1×1（每张纸 1 页）。票据已在 PDF 内拼好，不要再次选择 1×2。",
        "",
        "| 序号 | 票据日期 | 类别 | 平台 | 金额 | 对应打印包 |",
        "| ---: | --- | --- | --- | ---: | --- |",
    ]
    for index, item in enumerate(items, start=1):
        provider = (
            "网约车（新平台待命名）" if item.provider == "网约车" else item.provider
        )
        lines.append(
            f"| {index} | {item.invoice_date} | {item.category} | {provider} | "
            f"{item.amount:.2f} 元 | {item.print_package.name} |"
        )
    lines.extend(("", "## 分类合计", ""))
    for category, amount in sorted(category_totals.items()):
        lines.append(f"- {category}：{amount:.2f} 元")
    lines.extend(
        (
            f"- 总计：{total:.2f} 元",
            "",
            "总 PDF 按上表顺序完整追加各打印包，不会用其他票据填补单个票据组的空白半页。",
            "原始电子发票、行程单或订单凭证，以及各自打印包仍保留在工作区中。",
        )
    )
    if any(item.provider == "网约车" for item in items):
        lines.extend(
            (
                "",
                "提示：标记为“新平台待命名”的票据已通过结构、金额和唯一配对校验，"
                "但平台名称尚未加入映射。",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def create_delivery_bundle(
    paths: WorkspacePaths,
    output_paths: list[Path],
    *,
    generated_at: datetime | None = None,
    report_date: date | None = None,
) -> DeliveryBundle:
    created_at = generated_at or datetime.now()
    items = _resolve_items(paths, output_paths)
    people = {item.person_name for item in items}
    if len(people) != 1:
        raise DeliveryError("同一次交付中出现了多个姓名")
    categories = {item.category for item in items}
    category_label = next(iter(categories)) if len(categories) == 1 else "票据"
    total = sum((item.amount for item in items), start=Decimal("0"))
    base_name = (
        f"{(report_date or created_at.date()).isoformat()}_"
        f"{'按处理日_' if report_date else ''}{category_label}汇总_{total:.2f}元_"
        f"{next(iter(people))}"
    )
    pdf_path, markdown_path = _unique_delivery_paths(paths, base_name)
    stage = new_staging_directory(paths)
    staged_pdf = stage / "总打印包.pdf"
    staged_markdown = stage / "费用清单.md"
    published_pdf = False
    published_markdown = False
    try:
        _compose_total_pdf([item.print_package for item in items], staged_pdf)
        _validate_total_pdf(staged_pdf, [item.print_package for item in items])
        staged_markdown.write_text(
            _render_markdown(items, created_at, report_date),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(staged_pdf, pdf_path)
        published_pdf = True
        os.replace(staged_markdown, markdown_path)
        published_markdown = True
    except Exception:
        if published_markdown and markdown_path.exists():
            markdown_path.unlink()
        if published_pdf and pdf_path.exists():
            pdf_path.unlink()
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return DeliveryBundle(pdf_path, markdown_path, len(items), total)
