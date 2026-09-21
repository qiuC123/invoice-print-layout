from __future__ import annotations

import math
import io
import re
from pathlib import Path
from typing import cast

import pymupdf
from PIL import Image


class LayoutError(ValueError):
    """Raised when safe clipping or composition cannot be guaranteed."""


A4_WIDTH = 595.2756
A4_HEIGHT = 841.8898
SLOT_MARGIN = 6.0
SLOT_GAP = 4.0
CROP_MARGIN = 12.0

_FOOTER_PATTERN = re.compile(
    r"^(?:页码\s*[:：]?\s*)?\d+\s*/\s*\d+$|^PAGE\s*[:：]?\s*\d+\s*/\s*\d+$",
    re.IGNORECASE,
)


def _text_blocks(page: pymupdf.Page) -> list[tuple[float, float, float, float, str]]:
    blocks: list[tuple[float, float, float, float, str]] = []
    for raw in page.get_text("blocks"):
        if len(raw) < 7 or int(raw[6]) != 0:
            continue
        text = str(raw[4]).strip()
        if text:
            blocks.append((float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]), text))
    return blocks


def _find_first_anchor(page: pymupdf.Page) -> pymupdf.Rect | None:
    for marker in (
        "滴滴出行-行程单",
        "第三方网约车服务提供方",
        "CAOCAO TRAVEL - TRIP TABLE",
        "XIANGDAO TRAVEL - TRIP TABLE",
        "DIDI TRAVEL - TRIP TABLE",
    ):
        matches = page.search_for(marker)
        if matches:
            return cast(pymupdf.Rect, min(matches, key=lambda rect: rect.y0))
    for generic_marker in ("行程单", "TRIP TABLE"):
        matches = page.search_for(generic_marker)
        if matches:
            return cast(pymupdf.Rect, min(matches, key=lambda rect: rect.y0))
    return None


def _find_continuation_anchor(page: pymupdf.Page) -> pymupdf.Rect | None:
    marker_groups = (
        ("序号", "上车时间", "起点", "终点"),
        ("TRIP PAGE", "TRIP DETAILS", "PICKUP", "DESTINATION"),
    )
    for markers in marker_groups:
        matches: list[pymupdf.Rect] = []
        for marker in markers:
            matches.extend(page.search_for(marker))
        if matches:
            return min(matches, key=lambda rect: rect.y0)
    return None


def trip_clip_rect(page: pymupdf.Page, page_index: int) -> pymupdf.Rect:
    blocks = _text_blocks(page)
    if not blocks:
        raise LayoutError(f"行程单第 {page_index + 1} 页没有可提取正文")

    anchor = _find_first_anchor(page) if page_index == 0 else _find_continuation_anchor(page)
    if anchor is None:
        label = "标题" if page_index == 0 else "续页表格标志"
        raise LayoutError(f"行程单第 {page_index + 1} 页未找到安全的{label}")

    top = max(page.rect.y0, anchor.y0 - CROP_MARGIN)
    body_blocks = []
    for block in blocks:
        text = re.sub(r"\s+", "", block[4])
        if block[3] < top:
            continue
        if _FOOTER_PATTERN.fullmatch(text):
            continue
        body_blocks.append(block)
    if not body_blocks:
        raise LayoutError(f"行程单第 {page_index + 1} 页未找到正文")

    text_bottom = max(block[3] for block in body_blocks)
    drawing_bottom = text_bottom
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if not isinstance(rect, pymupdf.Rect):
            continue
        if rect.y1 < top or rect.y0 > text_bottom + 36:
            continue
        drawing_bottom = max(drawing_bottom, min(rect.y1, text_bottom + 36))

    bottom = min(page.rect.y1, max(text_bottom, drawing_bottom) + CROP_MARGIN)
    if bottom - top < 48:
        raise LayoutError(f"行程单第 {page_index + 1} 页正文高度异常")
    return pymupdf.Rect(page.rect.x0, top, page.rect.x1, bottom)


def _slot(index: int) -> pymupdf.Rect:
    usable_height = A4_HEIGHT - SLOT_GAP
    half = usable_height / 2
    if index == 0:
        return pymupdf.Rect(
            SLOT_MARGIN,
            SLOT_MARGIN,
            A4_WIDTH - SLOT_MARGIN,
            half - SLOT_MARGIN,
        )
    return pymupdf.Rect(
        SLOT_MARGIN,
        half + SLOT_GAP + SLOT_MARGIN,
        A4_WIDTH - SLOT_MARGIN,
        A4_HEIGHT - SLOT_MARGIN,
    )


def compose_print_package(trip_path: Path, invoice_path: Path, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    trip: pymupdf.Document | None = None
    invoice: pymupdf.Document | None = None
    output: pymupdf.Document | None = None
    try:
        trip = pymupdf.open(trip_path)
        invoice = pymupdf.open(invoice_path)
        output = pymupdf.open()
        if trip.needs_pass or invoice.needs_pass:
            raise LayoutError("加密 PDF 不能拼版")
        if invoice.page_count != 1:
            raise LayoutError("第一版只支持单页电子发票")

        logical_pages: list[tuple[pymupdf.Document, int, pymupdf.Rect]] = []
        for page_index in range(trip.page_count):
            page = trip.load_page(page_index)
            logical_pages.append((trip, page_index, trip_clip_rect(page, page_index)))
        logical_pages.append((invoice, 0, invoice[0].rect))

        for offset in range(0, len(logical_pages), 2):
            target_page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            for slot_index, (source, page_number, clip) in enumerate(
                logical_pages[offset : offset + 2]
            ):
                target_page.show_pdf_page(
                    _slot(slot_index),
                    source,
                    page_number,
                    clip=clip,
                    keep_proportion=True,
                    overlay=True,
                )

        output.save(output_path, garbage=4, deflate=True)
        return math.ceil(len(logical_pages) / 2)
    except LayoutError:
        raise
    except Exception as exc:
        raise LayoutError(f"PDF 拼版失败：{exc}") from exc
    finally:
        if output is not None:
            output.close()
        if invoice is not None:
            invoice.close()
        if trip is not None:
            trip.close()


def _insert_image_source(
    target_page: pymupdf.Page,
    target: pymupdf.Rect,
    image_path: Path,
    crop: tuple[int, int, int, int] | None,
) -> None:
    try:
        with Image.open(image_path) as source:
            image = source.convert("RGB")
            if crop is not None:
                image = image.crop(crop)
            stream = io.BytesIO()
            image.save(stream, format="JPEG", quality=95, optimize=True)
    except Exception as exc:
        raise LayoutError(f"图片无法排版：{image_path.name}：{exc}") from exc
    target_page.insert_image(target, stream=stream.getvalue(), keep_proportion=True)


def compose_takeout_print_package(
    order_path: Path,
    order_crop: tuple[int, int, int, int],
    invoices: list[tuple[Path, tuple[int, int, int, int] | None]],
    output_path: Path,
) -> int:
    """Compose one takeout order and all of its invoices without cross-order filling."""
    if not invoices:
        raise LayoutError("外卖订单没有电子发票")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output: pymupdf.Document | None = None
    opened_pdfs: list[pymupdf.Document] = []
    try:
        output = pymupdf.open()
        logical: list[tuple[str, Path, tuple[int, int, int, int] | None]] = [
            ("image", order_path, order_crop)
        ]
        for path, crop in invoices:
            logical.append(("pdf" if path.suffix.lower() == ".pdf" else "image", path, crop))

        for offset in range(0, len(logical), 2):
            target_page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            for slot_index, (kind, path, crop) in enumerate(logical[offset : offset + 2]):
                target = _slot(slot_index)
                if kind == "image":
                    _insert_image_source(target_page, target, path, crop)
                    continue
                source = pymupdf.open(path)
                opened_pdfs.append(source)
                if source.needs_pass or source.page_count != 1:
                    raise LayoutError(f"外卖电子发票必须是未加密的单页 PDF：{path.name}")
                target_page.show_pdf_page(
                    target,
                    source,
                    0,
                    clip=source[0].rect,
                    keep_proportion=True,
                    overlay=True,
                )
        output.save(output_path, garbage=4, deflate=True)
        return math.ceil(len(logical) / 2)
    except LayoutError:
        raise
    except Exception as exc:
        raise LayoutError(f"外卖票据拼版失败：{exc}") from exc
    finally:
        if output is not None:
            output.close()
        for source in opened_pdfs:
            source.close()


def validate_takeout_print_package(output_path: Path, expected_pages: int) -> None:
    try:
        with pymupdf.open(output_path) as document:
            if document.page_count != expected_pages:
                raise LayoutError(
                    f"外卖打印包页数错误：预期 {expected_pages}，实际 {document.page_count}"
                )
            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                if abs(page.rect.width - A4_WIDTH) > 1 or abs(page.rect.height - A4_HEIGHT) > 1:
                    raise LayoutError(f"外卖打印包第 {page_index + 1} 页不是 A4")
                if not page.get_images(full=True) and not page.get_text("text").strip():
                    raise LayoutError(f"外卖打印包第 {page_index + 1} 页没有票据内容")
    except LayoutError:
        raise
    except Exception as exc:
        raise LayoutError(f"外卖打印包无法校验：{exc}") from exc


def required_source_fragments(trip_path: Path, invoice_path: Path) -> list[str]:
    fragments: list[str] = []
    trip: pymupdf.Document | None = None
    invoice: pymupdf.Document | None = None
    try:
        trip = pymupdf.open(trip_path)
        invoice = pymupdf.open(invoice_path)
        for page_index in range(trip.page_count):
            page = trip.load_page(page_index)
            clip = trip_clip_rect(page, page_index)
            for line in page.get_text("text", clip=clip).splitlines():
                compact = re.sub(r"\s+", "", line)
                if len(compact) >= 2 and not _FOOTER_PATTERN.fullmatch(compact):
                    fragments.append(compact)
        invoice_text = invoice.load_page(0).get_text("text")
        fragments.extend(
            compact
            for line in invoice_text.splitlines()
            if len(compact := re.sub(r"\s+", "", line)) >= 2
        )
        return fragments
    except LayoutError:
        raise
    except Exception as exc:
        raise LayoutError(f"源票据正文校验失败：{exc}") from exc
    finally:
        if invoice is not None:
            invoice.close()
        if trip is not None:
            trip.close()


def validate_print_package(
    output_path: Path,
    expected_pages: int,
    amount_text: str,
    required_fragments: list[str] | None = None,
) -> None:
    try:
        document = pymupdf.open(output_path)
    except Exception as exc:
        raise LayoutError(f"输出 PDF 无法重新打开：{exc}") from exc
    try:
        if document.page_count != expected_pages:
            raise LayoutError(
                f"输出页数错误：预期 {expected_pages}，实际 {document.page_count}"
            )
        text = "".join(
            document.load_page(index).get_text("text")
            for index in range(document.page_count)
        )
        compact_output = re.sub(r"\s+", "", text)
        if amount_text not in compact_output:
            raise LayoutError("输出 PDF 中未找到票据金额")
        for fragment in required_fragments or []:
            if fragment not in compact_output:
                raise LayoutError(f"输出 PDF 缺少源票据正文：{fragment[:24]}")
        for page_offset in range(document.page_count):
            index = page_offset + 1
            page = document.load_page(page_offset)
            if abs(page.rect.width - A4_WIDTH) > 1 or abs(page.rect.height - A4_HEIGHT) > 1:
                raise LayoutError(f"输出第 {index} 页不是 A4")
            if not page.get_text("text").strip():
                raise LayoutError(f"输出第 {index} 页没有可提取文字")
    except LayoutError:
        raise
    except Exception as exc:
        raise LayoutError(f"输出 PDF 校验失败：{exc}") from exc
    finally:
        document.close()
