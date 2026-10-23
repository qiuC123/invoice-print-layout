from __future__ import annotations

import math
import io
import re
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pymupdf
from PIL import Image

from invoice_print_layout.documents import PdfInspectionError, classify_text, compact_text, inspect_pdf
from invoice_print_layout.models import DocumentKind


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

_AD_TEXT = {"DIDIBRANDADVERTISEMENT", "PROMOTIONQRAREA"}
# Only fingerprints of separately reviewed, pure advertising resources belong
# here. No invoice, passenger, expense or original-document hash is registered.
_CONFIRMED_MASTHEAD_FINGERPRINTS: frozenset[str] = frozenset({
    # Didi logo, corporate-booking ad and dotted cutting-line image.
    "b3ed2058154a1da1dcb2fcef4e3adad1dc29605432db44ec96418a57805eec10",
    # Amap logo and corporate-booking ad image.
    "47049374ce2949c6c2328893a45c391b88c4ae15799b315049b7970f0cfbbd6e",
    # Tencent pale masthead rectangle and its two brand images.
    "3b2a6837e53941bc2ec662a907b0c8b1f77a2429304776be8cad82f3fd6f4f16",
})


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
        if all(page.search_for(marker) for marker in markers):
            return min(matches, key=lambda rect: rect.y0)
    return None


def _known_trip_heading(page: pymupdf.Page) -> bool:
    compact = re.sub(r"\s+", "", page.get_text("text")).upper()
    return any(marker in compact for marker in (
        "滴滴出行", "曹操出行", "享道出行", "腾讯出行服务", "高德地图—打车",
        "DIDITRAVEL", "CAOCAOTRAVEL", "XIANGDAOTRAVEL", "AMAPITINERARY",
    ))


def _canonical_mark(value: Any) -> Any:
    if isinstance(value, (pymupdf.Point, pymupdf.Rect, pymupdf.Quad)):
        return [_canonical_mark(part) for part in cast(Any, value)]
    if isinstance(value, (tuple, list)):
        return [_canonical_mark(part) for part in value]
    if isinstance(value, dict):
        return {key: _canonical_mark(part) for key, part in value.items()
                if key != "seqno"}
    return value


def _masthead_signature(page: pymupdf.Page, anchor: pymupdf.Rect) -> tuple[str, float] | None:
    """Fingerprint the complete header paint cluster, never a guessed region."""
    for block in _text_blocks(page):
        compact = re.sub(r"\s+", "", block[4]).upper()
        if block[1] < anchor.y0 - 1 and compact not in _AD_TEXT:
            return None
    source = page.parent
    if source is None:
        return None
    masks = {image[0]: image[1] for image in page.get_images(full=True)}
    images = []
    for image in page.get_image_info(xrefs=True):
        rect = pymupdf.Rect(image["bbox"])
        if rect.y0 >= anchor.y0:
            continue
        xref = image.get("xref", 0)
        if not xref or rect.y1 > anchor.y0:
            return None
        mask = masks.get(xref, 0)
        images.append({
            "bbox": image["bbox"], "transform": image["transform"],
            "width": image["width"], "height": image["height"],
            "bpc": image["bpc"], "colorspace": image["colorspace"],
            "cs_name": image["cs-name"],
            "resource": hashlib.sha256(source.xref_stream(xref)).hexdigest(),
            "mask": hashlib.sha256(source.xref_stream(mask)).hexdigest() if mask else "",
        })
    drawings = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        if rect.y0 >= anchor.y0:
            continue
        if rect.y1 > anchor.y0:
            return None
        drawings.append(drawing)
    paints = [(kind, rect) for kind, rect in page.get_bboxlog()
              if not kind.endswith("text") and rect[1] < anchor.y0]
    if not paints or any(rect[3] > anchor.y0 for _, rect in paints):
        return None
    payload = _canonical_mark({"images": images, "drawings": drawings, "paint_order": paints})
    signature = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return signature, max(rect[3] for _, rect in paints)


def _paint_bounds(
    page: pymupdf.Page, *, omit_text: list[pymupdf.Rect] | None = None,
) -> list[pymupdf.Rect]:
    """Native paint bounds retain images, QR paths, signatures and table edges."""
    white_fills = {
        drawing.get("seqno") for drawing in page.get_drawings()
        if drawing.get("type") == "f"
        and drawing.get("fill") is not None
        and all(float(channel) >= 0.999 for channel in drawing["fill"])
    }
    bounds = []
    for seqno, (kind, raw_rect) in enumerate(page.get_bboxlog()):
        if seqno in white_fills:
            continue
        rect = pymupdf.Rect(raw_rect) & page.rect
        if kind.endswith("text") and any(
            pymupdf.Rect(text.x0 - 3, text.y0 - 3, text.x1 + 3, text.y1 + 3).contains(rect)
            for text in omit_text or []
        ):
            continue
        if not rect.is_empty and not rect.is_infinite:
            bounds.append(rect)
    return bounds


def invoice_clip_rect(page: pymupdf.Page) -> pymupdf.Rect:
    """Remove only empty invoice margins; never infer content from pixels."""
    if page.rotation:
        raise LayoutError("旋转发票的安全裁剪边界尚未确认，请检查")
    bounds = _paint_bounds(page)
    if not bounds:
        raise LayoutError("发票没有可保留的原生文字或绘制内容")
    content = pymupdf.Rect(bounds[0])
    for rect in bounds[1:]:
        content |= rect
    return cast(pymupdf.Rect, pymupdf.Rect(
        content.x0 - CROP_MARGIN, content.y0 - CROP_MARGIN,
        content.x1 + CROP_MARGIN, content.y1 + CROP_MARGIN,
    ) & page.rect)


def trip_clip_rect(page: pymupdf.Page, page_index: int) -> pymupdf.Rect:
    if page.rotation:
        raise LayoutError(f"行程单第 {page_index + 1} 页已旋转，安全裁剪边界尚未确认")
    blocks = _text_blocks(page)
    if not blocks:
        raise LayoutError(f"行程单第 {page_index + 1} 页没有可提取正文")

    anchor = _find_first_anchor(page) if page_index == 0 else _find_continuation_anchor(page)
    if anchor is None and page_index == 0:
        try:
            if classify_text(page.get_text("text")) is DocumentKind.TRIP:
                anchor = _find_continuation_anchor(page)
        except PdfInspectionError:
            pass
    if anchor is None:
        label = "标题" if page_index == 0 else "续页表格标志"
        raise LayoutError(f"行程单第 {page_index + 1} 页未找到安全的{label}")

    top = max(page.rect.y0, anchor.y0 - CROP_MARGIN)
    known_heading = _known_trip_heading(page)
    body_blocks = []
    omitted_text = []
    for block in blocks:
        text = re.sub(r"\s+", "", block[4])
        if block[3] < top and text.upper() in _AD_TEXT:
            omitted_text.append(pymupdf.Rect(block[:4]))
            continue
        if _FOOTER_PATTERN.fullmatch(text):
            omitted_text.append(pymupdf.Rect(block[:4]))
            continue
        body_blocks.append(block)
    if not body_blocks:
        raise LayoutError(f"行程单第 {page_index + 1} 页未找到正文")

    if page_index == 0 and not known_heading:
        try:
            if classify_text(page.get_text("text")) is not DocumentKind.TRIP:
                raise PdfInspectionError("正文不是行程表")
        except PdfInspectionError as exc:
            raise LayoutError("行程单缺少可核对的行程汇总及完整表头，请检查裁剪边界") from exc

    top = min(top, min(block[1] for block in body_blocks) - CROP_MARGIN)
    text_bottom = max(block[3] for block in body_blocks)
    drawing_bottom = text_bottom
    masthead = _masthead_signature(page, anchor)
    confirmed_masthead = masthead is not None and masthead[0] in _CONFIRMED_MASTHEAD_FINGERPRINTS
    for rect in _paint_bounds(page, omit_text=omitted_text):
        if confirmed_masthead and rect.y1 <= anchor.y0:
            continue
        # Unmatched or modified clusters retain every image and vector mark.
        top = min(top, rect.y0 - CROP_MARGIN)
        drawing_bottom = max(drawing_bottom, rect.y1)

    top = max(page.rect.y0, top)
    if confirmed_masthead and masthead is not None:
        top = max(top, min(masthead[1] + 0.5, anchor.y0))
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


def _pair_slots(clips: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Keep equal halves unless a tall original needs more of the same A4."""
    if len(clips) != 2:
        return [_slot(0)]
    width = A4_WIDTH - 2 * SLOT_MARGIN
    height = A4_HEIGHT - 2 * SLOT_MARGIN - SLOT_GAP
    natural = [clip.height * width / clip.width for clip in clips]
    half = height / 2
    if max(natural) <= half:
        return [_slot(0), _slot(1)]
    if sum(natural) <= height:
        first_height = min(max(half, natural[0]), height - natural[1])
    else:
        first_height = height * natural[0] / sum(natural)
    split = SLOT_MARGIN + first_height
    return [
        pymupdf.Rect(SLOT_MARGIN, SLOT_MARGIN, A4_WIDTH - SLOT_MARGIN, split),
        pymupdf.Rect(SLOT_MARGIN, split + SLOT_GAP,
                     A4_WIDTH - SLOT_MARGIN, A4_HEIGHT - SLOT_MARGIN),
    ]


def _ride_page_batches(
    logical_pages: list[tuple[pymupdf.Document, int, pymupdf.Rect]], multi_page_invoice: bool,
) -> list[list[tuple[pymupdf.Document, int, pymupdf.Rect]]]:
    batches = []
    offset = 0
    while offset < len(logical_pages):
        entries = logical_pages[offset : offset + 2]
        if multi_page_invoice and len(entries) == 2:
            natural_height = sum(clip.height * (A4_WIDTH - 2 * SLOT_MARGIN) / clip.width
                                 for _, _, clip in entries)
            if natural_height + SLOT_GAP > A4_HEIGHT - 2 * SLOT_MARGIN:
                entries = entries[:1]
        batches.append(entries)
        offset += len(entries)
    return batches


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
        if invoice.page_count < 1:
            raise LayoutError("发票没有页面")
        multi_page_invoice = invoice.page_count > 1
        if multi_page_invoice:
            inspected = inspect_pdf(invoice_path)
            if inspected.kind is not DocumentKind.INVOICE:
                raise LayoutError("多页文件不是同一张完整发票")
        trip.bake(annots=True, widgets=False)
        invoice.bake(annots=True, widgets=False)

        logical_pages: list[tuple[pymupdf.Document, int, pymupdf.Rect]] = []
        for page_index in range(trip.page_count):
            page = trip.load_page(page_index)
            logical_pages.append((trip, page_index, trip_clip_rect(page, page_index)))
        for page_index in range(invoice.page_count):
            logical_pages.append((invoice, page_index, invoice_clip_rect(invoice[page_index])))

        batches = _ride_page_batches(logical_pages, multi_page_invoice)
        if invoice.page_count == 2:
            # A short continuation can share the trip's spare space. Reverse
            # only these two validated invoice pages, and only to save paper.
            alternative = logical_pages[:-2] + [logical_pages[-1], logical_pages[-2]]
            alternative_batches = _ride_page_batches(alternative, multi_page_invoice)
            if len(alternative_batches) < len(batches):
                batches = alternative_batches
        for entries in batches:
            target_page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
            slots = ([pymupdf.Rect(SLOT_MARGIN, SLOT_MARGIN, A4_WIDTH - SLOT_MARGIN, A4_HEIGHT - SLOT_MARGIN)]
                     if multi_page_invoice and len(entries) == 1
                     else _pair_slots([entry[2] for entry in entries]))
            for slot_index, (source, page_number, clip) in enumerate(entries):
                target_page.show_pdf_page(
                    slots[slot_index],
                    source,
                    page_number,
                    clip=clip,
                    keep_proportion=True,
                    overlay=True,
                )

        output.save(output_path, garbage=4, deflate=True)
        return int(output.page_count)
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
                source.bake(annots=True, widgets=False)
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
                compact = compact_text(line)
                if len(compact) >= 2 and not _FOOTER_PATTERN.fullmatch(compact):
                    fragments.append(compact)
        for page_index in range(invoice.page_count):
            page = invoice.load_page(page_index)
            fragments.extend(
                compact
                for line in page.get_text("text").splitlines()
                if len(compact := compact_text(line)) >= 2
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
        # Callers may supply native text or already compacted fragments. Use
        # the same Unicode/whitespace comparison as source inspection on both
        # sides, while still requiring every complete source fragment.
        compact_output = compact_text(text)
        if compact_text(amount_text) not in compact_output:
            raise LayoutError("输出 PDF 中未找到票据金额")
        for fragment in required_fragments or []:
            if compact_text(fragment) not in compact_output:
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
