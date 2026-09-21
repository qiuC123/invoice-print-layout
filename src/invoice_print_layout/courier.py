"""SF express and SF same-city invoices with PDF waybill evidence."""
from __future__ import annotations

import math
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pymupdf

from invoice_print_layout.documents import (
    _parse_positioned_invoice_amount, compact_text,
    parse_invoice_date, parse_invoice_number, sha256_file,
)
from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH, _slot, validate_print_package
from invoice_print_layout.memory import refresh_memory_status
from invoice_print_layout.models import BatchSummary, OutcomeStatus
from invoice_print_layout.storage import (
    WorkspacePaths, append_history, choose_output_paths, ensure_workspace, is_duplicate,
    move_to_review, new_staging_directory, read_history, safe_filename_part, write_report,
)

KINDS = {"顺丰": ("顺丰", "快递"), "同城": ("顺丰同城", "同城配送")}
_FOOTER = re.compile(r"^(?:页码[:：]?)?\d+[/\-]\d+$")


@dataclass(frozen=True)
class CourierPdf:
    path: Path
    kind: str
    is_invoice: bool
    amount: Decimal
    sha256: str
    invoice_number: str | None = None
    invoice_date: date | None = None


def inspect_courier_pdf(path: Path, kind: str | None = None) -> CourierPdf:
    if kind is not None and kind not in KINDS:
        raise ValueError("不支持的快递类型")
    with pymupdf.open(path) as document:
        if document.needs_pass or not len(document):
            raise ValueError("PDF 已加密或没有页面")
        text = "\n".join(page.get_text() for page in document)
        compact = compact_text(text)
        if not compact:
            raise ValueError("PDF 无可提取文字，请提供原始 PDF")
        express_detail = "顺丰电子发票" in compact and "运单明细" in compact
        city_detail = all(word in compact for word in ("运单起止日期", "订单号", "发件信息", "收件信息"))
        if express_detail or city_detail:
            detected = "顺丰" if express_detail else "同城"
            if kind is not None and detected != kind:
                raise ValueError("运单明细类型与当前批次不符")
            kind = detected
            pattern = (r"发票总金额[（(]元[）)]([0-9]+(?:\.[0-9]{1,2})?)"
                       if express_detail else r"共\d+笔运单[，,]?合计([0-9]+(?:\.[0-9]{1,2})?)元")
            amounts = {Decimal(v).quantize(Decimal("0.01")) for v in re.findall(pattern, compact)}
            if len(amounts) != 1:
                raise ValueError("未找到唯一的运单总金额")
            number = None
            if express_detail:
                numbers = set(re.findall(r"(?<!\d)\d{20}(?!\d)", text))
                if len(numbers) != 1:
                    raise ValueError("顺丰运单明细缺少唯一发票号码")
                number = numbers.pop()
            return CourierPdf(path, kind, False, amounts.pop(), sha256_file(path), number)
        if "电子发票" not in compact or "价税合计" not in compact or len(document) != 1:
            raise ValueError("不支持的顺丰票据，请提供电子发票及配套运单明细 PDF")
        invoice_kind = "同城" if "顺丰同城" in compact else "顺丰" if "顺丰速运" in compact else None
        if invoice_kind is None or (kind is not None and invoice_kind != kind):
            raise ValueError("发票服务商与当前顺丰类型不符")
        # PDF extraction order can put the tax amount last. Never use last-currency fallback.
        amount = _parse_positioned_invoice_amount(document[0])
        return CourierPdf(path, invoice_kind, True, amount, sha256_file(path),
                          parse_invoice_number(text), parse_invoice_date(text))


def _detail_clip(page: pymupdf.Page) -> pymupdf.Rect:
    blocks = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0
              and compact_text(b[4]) and not _FOOTER.fullmatch(compact_text(b[4]))]
    if not blocks:
        raise ValueError("运单页面没有可确认的正文")
    top = max(page.rect.y0, min(b[1] for b in blocks) - 12)
    bottom = max(b[3] for b in blocks)
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if isinstance(rect, pymupdf.Rect) and rect.y1 >= top and rect.y0 <= bottom + 36:
            bottom = max(bottom, min(rect.y1, bottom + 36))
    # Keep any embedded graphic in the evidence rather than clipping unknown content.
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 1:
            top = min(top, block["bbox"][1])
            bottom = max(bottom, block["bbox"][3])
    return pymupdf.Rect(0, top, page.rect.width, min(page.rect.height, bottom + 12))


def compose_courier_package(detail: Path, invoice: Path, output: Path, amount: Decimal) -> None:
    fragments: list[str] = []
    with pymupdf.open(detail) as evidence, pymupdf.open(invoice) as bill, pymupdf.open() as result:
        # show_pdf_page does not copy annotations. Preserve their visible appearance
        # on these in-memory print copies; archived source PDFs remain untouched.
        evidence.bake(annots=True, widgets=False)
        bill.bake(annots=True, widgets=False)
        pages = [(evidence, i, _detail_clip(p)) for i, p in enumerate(evidence)]
        pages.append((bill, 0, bill[0].rect))
        for source, index, _ in pages:
            fragments.extend(compact_text(line) for line in source[index].get_text().splitlines()
                             if len(compact_text(line)) >= 2 and not _FOOTER.fullmatch(compact_text(line)))
        for offset in range(0, len(pages), 2):
            target = result.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            for slot, (source, index, clip) in enumerate(pages[offset:offset + 2]):
                target.show_pdf_page(_slot(slot), source, index, clip=clip, keep_proportion=True)
        result.save(output, garbage=4, deflate=True)
        expected = math.ceil(len(pages) / 2)
    validate_print_package(output, expected, f"{amount:.2f}", fragments)


def process_courier_batch(workspace: Path, files: list[Path], person: str, kind: str | None = None) -> tuple[WorkspacePaths, BatchSummary]:
    if kind is not None and kind not in KINDS:
        raise ValueError("不支持的快递类型")
    paths = ensure_workspace(workspace)
    summary = BatchSummary()
    documents: list[CourierPdf] = []
    for file in files:
        try:
            if file.suffix.lower() != ".pdf":
                raise ValueError("快递批次仅支持原始 PDF；OFD/XML 不需上传")
            documents.append(inspect_courier_pdf(file, kind))
        except Exception as exc:
            review = move_to_review(paths, [file], str(exc), category=kind or "顺丰")
            summary.add(OutcomeStatus.NEEDS_REVIEW, file.name, str(review))
    history = read_history(paths.history)
    invoices = [doc for doc in documents if doc.is_invoice]
    details = [doc for doc in documents if not doc.is_invoice]
    def matches(bill: CourierPdf, detail: CourierPdf) -> bool:
        return bill.kind == detail.kind and bill.amount == detail.amount and (bill.kind == "同城" or bill.invoice_number == detail.invoice_number)
    consumed: set[Path] = set()
    for bill in invoices:
        candidates = [d for d in details if matches(bill, d)]
        if len(candidates) != 1 or sum(matches(other, candidates[0]) for other in invoices) != 1:
            continue
        detail = candidates[0]
        consumed.update((bill.path, detail.path))
        duplicate = is_duplicate(history, bill.invoice_number, bill.sha256, detail.sha256)
        if duplicate:
            review = move_to_review(paths, [bill.path, detail.path],
                                    f"重复件；已有打印包：{duplicate.get('output')}", category="重复件")
            summary.add(OutcomeStatus.DUPLICATE, bill.path.name, str(review))
            continue
        provider, category = KINDS[bill.kind]
        assert bill.invoice_date and bill.invoice_number
        base = f"{bill.invoice_date.isoformat()}_{provider}_{bill.amount:.2f}元_{safe_filename_part(person)}"
        output, archive, _ = choose_output_paths(paths, base)
        stage = new_staging_directory(paths)
        try:
            staged_archive = stage / "archive"
            staged_archive.mkdir()
            shutil.copy2(bill.path, staged_archive / "原始电子发票.pdf")
            shutil.copy2(detail.path, staged_archive / "原始运单明细.pdf")
            compose_courier_package(detail.path, bill.path, stage / "print.pdf", bill.amount)
            os.replace(staged_archive, archive)
            os.replace(stage / "print.pdf", output)
            entry = {"record_version": 4, "created_at": datetime.now().isoformat(timespec="seconds"),
                     "category": category, "provider": provider, "amount": f"{bill.amount:.2f}",
                     "person_name": person, "invoice_date": bill.invoice_date.isoformat(),
                     "invoice_number": bill.invoice_number, "invoice_hash": bill.sha256,
                     "trip_hash": detail.sha256, "output": str(output), "archive": str(archive)}
            append_history(paths.history, entry)
            history.append(entry)
        except Exception as exc:
            if output.exists():
                output.unlink()
            if archive.exists():
                shutil.rmtree(archive)
            review = move_to_review(paths, [bill.path, detail.path], str(exc), category=bill.kind)
            summary.add(OutcomeStatus.NEEDS_REVIEW, bill.path.name, str(review))
        else:
            warning = ""
            for source in (bill.path, detail.path):
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    warning = "；原文件已归档，但工作副本未能清理"
            summary.add(OutcomeStatus.SUCCESS, output.name, str(output) + warning)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    remaining = [doc.path for doc in documents if doc.path not in consumed]
    if remaining:
        review = move_to_review(paths, remaining, "发票与运单明细缺失、金额不一致或存在多个候选，无法唯一配对", category=kind or "顺丰")
        summary.add(OutcomeStatus.NEEDS_REVIEW, kind or "顺丰", str(review))
    try:
        refresh_memory_status(paths)
    except (OSError, ValueError) as exc:
        summary.add(OutcomeStatus.NEEDS_REVIEW, "摘要更新", str(exc))
    write_report(paths.latest_report, [f"成功：{summary.success_count}，需要检查：{summary.review_count}，重复：{summary.duplicate_count}"])
    return paths, summary
