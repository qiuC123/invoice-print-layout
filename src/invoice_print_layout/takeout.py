from __future__ import annotations

import itertools
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from PIL import Image
from rapidocr_onnxruntime import RapidOCR

from invoice_print_layout.documents import (
    PdfInspectionError,
    inspect_pdf,
    sha256_file,
)
from invoice_print_layout.layout import (
    LayoutError,
    compose_takeout_print_package,
    validate_takeout_print_package,
)
from invoice_print_layout.memory import refresh_memory_status
from invoice_print_layout.models import BatchSummary, OutcomeStatus
from invoice_print_layout.storage import (
    WorkspacePaths,
    append_history,
    choose_output_paths,
    ensure_workspace,
    move_to_review,
    new_staging_directory,
    read_history,
    safe_filename_part,
    write_report,
)


class TakeoutError(ValueError):
    """Raised when a takeout ticket group cannot be determined without guessing."""


@dataclass(frozen=True)
class OcrLine:
    text: str
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class OrderReceipt:
    path: Path
    order_number: str
    merchant: str
    paid_amount: Decimal
    sha256: str
    crop: tuple[int, int, int, int]


@dataclass(frozen=True)
class TakeoutInvoice:
    render_path: Path
    source_path: Path
    amount: Decimal
    invoice_date: date
    invoice_number: str
    sha256: str
    crop: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class TakeoutGroup:
    order: OrderReceipt
    invoices: tuple[TakeoutInvoice, ...]


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_ARCHIVE_MEMBER_BYTES = 30 * 1024 * 1024


def _decimal(value: str, label: str) -> Decimal:
    try:
        return Decimal(value.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise TakeoutError(f"{label}格式无效") from exc


def _ocr_lines(path: Path, engine: Any) -> list[OcrLine]:
    try:
        result, _ = engine(str(path))
    except Exception as exc:
        raise TakeoutError(f"图片无法读取或识别：{path.name}") from exc
    lines: list[OcrLine] = []
    for raw in result or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        box, raw_text = raw[0], raw[1]
        if not isinstance(raw_text, str) or not isinstance(box, (list, tuple)):
            continue
        points = [point for point in box if isinstance(point, (list, tuple)) and len(point) >= 2]
        if not points:
            continue
        xs = [int(float(point[0])) for point in points]
        ys = [int(float(point[1])) for point in points]
        lines.append(OcrLine(raw_text.strip(), min(xs), min(ys), max(xs), max(ys)))
    if not lines:
        raise TakeoutError(f"图片未识别出文字：{path.name}")
    return lines


def _compact(lines: list[OcrLine]) -> str:
    return re.sub(r"\s+", "", "\n".join(line.text for line in lines))


def _crop_between(
    path: Path,
    lines: list[OcrLine],
    start_markers: tuple[str, ...],
    end_markers: tuple[str, ...],
    bottom_margin: int = 64,
) -> tuple[int, int, int, int]:
    with Image.open(path) as image:
        width, height = image.size
    starts = [line.top for line in lines if any(marker in line.text for marker in start_markers)]
    ends = [line.bottom for line in lines if any(marker in line.text for marker in end_markers)]
    top = max(0, min(starts) - 32) if starts else 0
    bottom = min(height, max(ends) + bottom_margin) if ends else height
    if bottom - top < height * 0.2:
        return (0, 0, width, height)
    return (0, top, width, bottom)


def _parse_order(path: Path, lines: list[OcrLine]) -> OrderReceipt:
    compact = _compact(lines)
    if "订单号" not in compact or "实付" not in compact:
        raise TakeoutError("不是淘宝外卖订单截图")
    order_matches = re.findall(r"(?<!\d)(\d{16,24})(?!\d)", compact)
    if len(set(order_matches)) != 1:
        raise TakeoutError("订单截图未找到唯一订单号")
    amount_matches = re.findall(r"实付[¥￥]?([0-9]+(?:\.[0-9]{1,2})?)", compact)
    if len(set(amount_matches)) != 1:
        raise TakeoutError("订单截图未找到唯一实付金额")
    merchant = "淘宝闪购"
    for line in lines:
        if "闪购" in line.text and "订单" not in line.text:
            candidate = re.sub(r"^.*?闪购", "", line.text).strip()
            if candidate:
                merchant = candidate.rstrip("）)")
                break
    crop = _crop_between(
        path, lines, ("订单已",), ("订单号", order_matches[0]), bottom_margin=20
    )
    return OrderReceipt(
        path=path,
        order_number=order_matches[0],
        merchant=merchant,
        paid_amount=_decimal(amount_matches[0], "实付金额"),
        sha256=sha256_file(path),
        crop=crop,
    )


def _parse_image_invoice(path: Path, lines: list[OcrLine]) -> TakeoutInvoice:
    compact = _compact(lines)
    if "电子发票" not in compact or "发票号码" not in compact:
        raise TakeoutError("不是电子发票图片")
    number_match = re.search(r"发票号码[:：]?([0-9]{8,24})", compact)
    date_match = re.search(r"开票日期[:：]?(\d{4})年(\d{1,2})月(\d{1,2})日", compact)
    amount_match = re.search(
        r"(?:（小写）|\(小写\))[¥￥]?([0-9]+(?:\.[0-9]{1,2})?)",
        compact,
    )
    if number_match is None:
        raise TakeoutError("发票图片未找到发票号码")
    if date_match is None:
        raise TakeoutError("发票图片未找到开票日期")
    if amount_match is None:
        raise TakeoutError("发票图片未找到价税合计")
    try:
        invoice_date = date(*(int(value) for value in date_match.groups()))
    except ValueError as exc:
        raise TakeoutError("发票图片开票日期无效") from exc
    crop = _crop_between(path, lines, ("电子发票",), ("开票人", "备注"))
    return TakeoutInvoice(
        render_path=path,
        source_path=path,
        amount=_decimal(amount_match.group(1), "发票金额"),
        invoice_date=invoice_date,
        invoice_number=number_match.group(1),
        sha256=sha256_file(path),
        crop=crop,
    )


def _parse_pdf_invoice(render_path: Path, source_path: Path) -> TakeoutInvoice:
    try:
        inspected = inspect_pdf(render_path)
    except PdfInspectionError as exc:
        raise TakeoutError(f"电子发票 PDF 无法识别：{render_path.name}：{exc}") from exc
    if inspected.invoice_date is None or inspected.invoice_number is None:
        raise TakeoutError(f"电子发票 PDF 字段不完整：{render_path.name}")
    return TakeoutInvoice(
        render_path=render_path,
        source_path=source_path,
        amount=inspected.amount,
        invoice_date=inspected.invoice_date,
        invoice_number=inspected.invoice_number,
        sha256=inspected.sha256,
        crop=None,
    )


def _extract_invoice_pdfs(archive: Path, directory: Path) -> list[Path]:
    results: list[Path] = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            for index, info in enumerate(bundle.infolist(), start=1):
                if info.is_dir() or Path(info.filename).suffix.lower() != ".pdf":
                    continue
                if info.flag_bits & 0x1:
                    raise TakeoutError(f"压缩包中的 PDF 已加密：{archive.name}")
                if info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise TakeoutError(f"压缩包中的 PDF 超过30MB：{archive.name}")
                payload = bundle.read(info)
                if b"%PDF-" not in payload[:1024]:
                    raise TakeoutError(f"压缩包中的 .pdf 不是有效 PDF：{archive.name}")
                destination = directory / f"{archive.stem}_{index}.pdf"
                destination.write_bytes(payload)
                results.append(destination)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise TakeoutError(f"发票压缩包无法读取：{archive.name}：{exc}") from exc
    if not results:
        raise TakeoutError(f"发票压缩包中没有 PDF：{archive.name}")
    return results


def inspect_takeout_files(
    files: list[Path], staging: Path
) -> tuple[list[OrderReceipt], list[TakeoutInvoice]]:
    engine = RapidOCR()
    orders: list[OrderReceipt] = []
    invoices: list[TakeoutInvoice] = []
    derived = staging / "extracted"
    derived.mkdir()
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".zip":
            for extracted in _extract_invoice_pdfs(path, derived):
                invoices.append(_parse_pdf_invoice(extracted, path))
            continue
        if suffix == ".pdf":
            invoices.append(_parse_pdf_invoice(path, path))
            continue
        if suffix not in _IMAGE_SUFFIXES:
            raise TakeoutError(f"不支持的外卖附件：{path.name}")
        lines = _ocr_lines(path, engine)
        compact = _compact(lines)
        if "订单号" in compact and "实付" in compact:
            orders.append(_parse_order(path, lines))
        elif "电子发票" in compact and "发票号码" in compact:
            invoices.append(_parse_image_invoice(path, lines))
        else:
            raise TakeoutError(f"图片既不是订单记录也不是电子发票：{path.name}")

    deduplicated: dict[str, TakeoutInvoice] = {}
    for invoice in invoices:
        previous = deduplicated.get(invoice.invoice_number)
        if previous is None or (
            previous.render_path.suffix.lower() != ".pdf"
            and invoice.render_path.suffix.lower() == ".pdf"
        ):
            deduplicated[invoice.invoice_number] = invoice
    return orders, list(deduplicated.values())


def _matching_subsets(order: OrderReceipt, invoices: list[TakeoutInvoice]) -> list[tuple[int, ...]]:
    named = [index for index, item in enumerate(invoices) if order.order_number in item.source_path.name]
    if named:
        return [tuple(named)] if sum((invoices[i].amount for i in named), Decimal("0")) == order.paid_amount else []
    if len(invoices) > 12:
        raise TakeoutError("单批发票超过12张，无法安全自动组合")
    matches: list[tuple[int, ...]] = []
    for size in range(1, len(invoices) + 1):
        for indices in itertools.combinations(range(len(invoices)), size):
            total = sum((invoices[index].amount for index in indices), Decimal("0"))
            if total == order.paid_amount:
                matches.append(indices)
    return matches


def pair_takeout(
    orders: list[OrderReceipt], invoices: list[TakeoutInvoice]
) -> list[TakeoutGroup]:
    if not orders:
        raise TakeoutError("没有找到订单记录截图")
    if not invoices:
        raise TakeoutError("没有找到电子发票")
    if len({order.order_number for order in orders}) != len(orders):
        raise TakeoutError("同一订单截图被重复上传")
    candidates = [_matching_subsets(order, invoices) for order in orders]
    if any(not value for value in candidates):
        raise TakeoutError("至少一个订单的实付金额与其发票合计不一致")

    solutions: list[list[tuple[int, ...]]] = []

    def search(position: int, used: set[int], selected: list[tuple[int, ...]]) -> None:
        if len(solutions) > 1:
            return
        if position == len(orders):
            if used == set(range(len(invoices))):
                solutions.append(selected.copy())
            return
        for subset in candidates[position]:
            subset_set = set(subset)
            if used.isdisjoint(subset_set):
                search(position + 1, used | subset_set, [*selected, subset])

    search(0, set(), [])
    if len(solutions) != 1:
        raise TakeoutError("订单与发票无法唯一匹配，请人工检查")
    return [
        TakeoutGroup(order, tuple(invoices[index] for index in subset))
        for order, subset in zip(orders, solutions[0], strict=True)
    ]


def _category(group: TakeoutGroup) -> str:
    return "咖啡" if "咖啡" in group.order.merchant else "外卖"


def _base_name(group: TakeoutGroup, person_name: str) -> str:
    invoice_date = min(invoice.invoice_date for invoice in group.invoices)
    return (
        f"{invoice_date.isoformat()}_淘宝闪购_{group.order.paid_amount:.2f}元_"
        f"{safe_filename_part(person_name)}"
    )


def _publish_group(
    paths: WorkspacePaths,
    group: TakeoutGroup,
    person_name: str,
    all_source_files: list[Path],
) -> tuple[Path, Path]:
    output_path, archive_path, _ = choose_output_paths(paths, _base_name(group, person_name))
    stage = new_staging_directory(paths)
    staged_output = stage / "打印包.pdf"
    staged_archive = stage / "归档包"
    staged_archive.mkdir()
    published_output = False
    published_archive = False
    try:
        group_sources = {group.order.path.resolve()} | {
            invoice.source_path.resolve() for invoice in group.invoices
        }
        for index, source in enumerate(all_source_files, start=1):
            if source.resolve() in group_sources:
                shutil.copy2(source, staged_archive / f"{index:02d}_{source.name}")
        expected_pages = compose_takeout_print_package(
            group.order.path,
            group.order.crop,
            [(invoice.render_path, invoice.crop) for invoice in group.invoices],
            staged_output,
        )
        validate_takeout_print_package(staged_output, expected_pages)
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
    return [
        f"处理时间：{datetime.now().isoformat(timespec='seconds')}",
        f"成功：{summary.success_count}",
        f"需要检查：{summary.review_count}",
        f"重复：{summary.duplicate_count}",
        "",
        *(f"[{item.status.value}] {item.label}：{item.message}" for item in summary.outcomes),
    ]


def _duplicate_entry(history: list[dict[str, Any]], group: TakeoutGroup) -> dict[str, Any] | None:
    numbers = {invoice.invoice_number for invoice in group.invoices}
    for entry in history:
        if entry.get("order_number") == group.order.order_number:
            return entry
        existing = entry.get("invoice_numbers")
        if isinstance(existing, list) and numbers.intersection(str(value) for value in existing):
            return entry
    return None


def process_takeout_batch(
    workspace_root: Path,
    source_files: list[Path],
    person_name: str,
) -> tuple[WorkspacePaths, BatchSummary]:
    paths = ensure_workspace(workspace_root)
    summary = BatchSummary()
    stage = new_staging_directory(paths)
    try:
        try:
            orders, invoices = inspect_takeout_files(source_files, stage)
            groups = pair_takeout(orders, invoices)
            history = read_history(paths.history)
        except (TakeoutError, OSError, ValueError) as exc:
            review = move_to_review(paths, source_files, str(exc), category="淘宝外卖")
            summary.add(OutcomeStatus.NEEDS_REVIEW, "淘宝外卖", str(review))
            write_report(paths.latest_report, _summary_lines(summary))
            return paths, summary

        for group in groups:
            duplicate = _duplicate_entry(history, group)
            if duplicate is not None:
                review = move_to_review(
                    paths,
                    source_files,
                    f"重复件；已有打印包：{duplicate.get('output', '未知')}",
                    category="重复件",
                )
                summary.add(OutcomeStatus.DUPLICATE, group.order.order_number, str(review))
                break
            try:
                output_path, archive_path = _publish_group(
                    paths, group, person_name, source_files
                )
                entry: dict[str, Any] = {
                    "record_version": 3,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "category": _category(group),
                    "provider": "淘宝闪购",
                    "amount": f"{group.order.paid_amount:.2f}",
                    "person_name": person_name,
                    "invoice_date": min(item.invoice_date for item in group.invoices).isoformat(),
                    "invoice_number": group.invoices[0].invoice_number,
                    "invoice_numbers": [item.invoice_number for item in group.invoices],
                    "invoice_hash": group.invoices[0].sha256,
                    "invoice_hashes": [item.sha256 for item in group.invoices],
                    "trip_hash": group.order.sha256,
                    "order_hash": group.order.sha256,
                    "order_number": group.order.order_number,
                    "output": str(output_path),
                    "archive": str(archive_path),
                }
                append_history(paths.history, entry)
                history.append(entry)
                summary.add(OutcomeStatus.SUCCESS, output_path.name, str(output_path))
            except (LayoutError, OSError, ValueError) as exc:
                review = move_to_review(
                    paths,
                    source_files,
                    f"生成外卖打印包失败：{exc}",
                    category="淘宝外卖",
                )
                summary.add(OutcomeStatus.NEEDS_REVIEW, group.order.order_number, str(review))
                break

        if summary.success_count:
            for source in source_files:
                try:
                    source.unlink()
                except FileNotFoundError:
                    pass
        try:
            refresh_memory_status(paths)
        except (OSError, ValueError) as exc:
            summary.add(OutcomeStatus.NEEDS_REVIEW, "长期索引摘要", str(exc))
        write_report(paths.latest_report, _summary_lines(summary))
        return paths, summary
    finally:
        shutil.rmtree(stage, ignore_errors=True)
