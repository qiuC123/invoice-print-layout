"""Read-only previews from the same compositor used by final reimbursement PDFs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

import pymupdf

from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
from invoice_print_layout.payment_layout import append_invoice_payments, append_payments
from invoice_print_layout.reimbursement import (
    _full_pages, compose_matter, selected_attachments, validate_ride_originals, validate_courier_originals,
)
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_runtime import source_build_id
from invoice_print_layout.ride_proofs import invoice_trip_required
from invoice_print_layout.courier_proofs import invoice_waybill_required
from invoice_print_layout.missing_materials import acknowledged_missing_note


PREVIEW_FORMAT = 'expense-print-preview-v4-invoice-two-up-courier-120dpi'


def _preview_attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    if item['complete']:
        return selected_attachments(item)
    # Incomplete matters may contain still-unclassified proofs. Show them in
    # full, without guessing a role, a counterpart or a safe crop boundary.
    return [attachment for attachment in item['attachments'] if attachment['available']]


def _source_hashes(store: ExpenseStore, item: dict[str, Any]) -> list[dict[str, str]]:
    if item['complete'] and invoice_trip_required(item):
        validate_ride_originals(store, item)
    if item['complete'] and invoice_waybill_required(item):
        validate_courier_originals(store, item)
    return [{'id': attachment['id'], 'role': attachment['role'],
             'sha256': hashlib.sha256(store.attachment_path(attachment['id'])[0].read_bytes()).hexdigest()}
            for attachment in _preview_attachments(item)]


def _unchanged(store: ExpenseStore, item: dict[str, Any], sources: list[dict[str, str]], *,
               current: dict[str, Any] | None = None) -> None:
    current = store.get(item['id']) if current is None else current
    if (current['id'] != item['id'] or current['version'] != item['version'] or current['complete'] != item['complete'] or
            current['missing'] != item['missing'] or _source_hashes(store, current) != sources):
        raise ValueError('预览期间事项或材料已变化，请刷新后重试')


def _compose_partial(store: ExpenseStore, item: dict[str, Any], folder: Path) -> tuple[Path, str]:
    destination = folder / (item['id'] + '-partial.pdf')
    attachments = _preview_attachments(item)
    basis = item.get('material_basis')
    roles = {'invoice', 'receipt', 'payment'} if basis == 'invoice_payment' else {'payment'}
    grouped = (basis in {'invoice_payment', 'payment_only'} and not invoice_trip_required(item)
               and not invoice_waybill_required(item))
    packed = [attachment for attachment in attachments if grouped and attachment['role'] in roles]
    remaining = [attachment for attachment in attachments if attachment not in packed]
    note = (acknowledged_missing_note(item) if item.get('missing_acknowledged') else
            '仅已收材料预览（非正式报销包）；仍缺：' + '、'.join(item['missing']))
    if not packed:
        _full_pages([store.attachment_path(attachment['id'])[0] for attachment in remaining], destination)
        return destination, note + '；完整原件逐页A4，不推断材料用途或裁剪边界'
    with pymupdf.open() as output:  # type: ignore[no-untyped-call]
        if basis == 'invoice_payment':
            # Partial previews already append every remaining proof at full size.
            # Restrict this first pass to its half/quarter cells so explicitly
            # grouped supplementary originals cannot be appended twice.
            packed_item = {**item, 'attachments': packed, 'material_match': {}}
            append_invoice_payments(output, store, packed_item, allow_incomplete=True)
            note += '；已收票据A4纸1×2，已收付款截图2×2'
        else:
            append_payments(output, store, [item])
            note += '；已收付款截图A4纸2×2'
        if remaining:
            extra = folder / (item['id'] + '-unclassified.pdf')
            _full_pages([store.attachment_path(attachment['id'])[0] for attachment in remaining], extra)
            with pymupdf.open(extra) as original:  # type: ignore[no-untyped-call]
                output.insert_pdf(original)
            note += '；其他材料完整原件逐页A4，用途仍待核对'
        output.save(destination, garbage=4, deflate=True)
    return destination, note


def _cached_result(folder: Path, stem: str, item: dict[str, Any], *,
                   render_pages: bool = True) -> dict[str, Any] | None:
    try:
        manifest = json.loads((folder / (stem + '.json')).read_text(encoding='utf-8'))
        result = manifest['result']
        count = result['page_count']
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return None
        pages = [f'{stem}-{number + 1}.png' for number in range(count)] if render_pages else []
        names = [stem + '.pdf', *pages]
        if (result['id'] != item['id'] or result['version'] != item['version'] or
                result['pdf'] != names[0] or result['preview_pages'] != pages or
                result['complete'] != bool(item['complete']) or result['missing'] != item['missing'] or
                result['partial'] != (not item['complete']) or
                not isinstance(result['layout'], str) or set(manifest['artifacts']) != set(names)):
            return None
        for name in names:
            payload = (folder / name).read_bytes()
            if hashlib.sha256(payload).hexdigest() != manifest['artifacts'][name]:
                return None
            if name == names[0]:
                # PDF-only manifests have no PNG list to bind their page
                # count. Validate the actual document in both cache modes,
                # including when the other mode reuses this vector PDF.
                with pymupdf.open(stream=payload, filetype='pdf') as document:  # type: ignore[no-untyped-call]
                    if (not document.is_pdf or document.needs_pass or document.is_encrypted or
                            len(document) != count or
                            any(abs(page.rect.width - A4_WIDTH) > 1 or
                                abs(page.rect.height - A4_HEIGHT) > 1 for page in document)):
                        return None
        return {key: result[key] for key in
                ('id', 'version', 'complete', 'partial', 'missing', 'pdf', 'preview_pages', 'layout', 'page_count')}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None


def create_expense_print_preview(
    store: ExpenseStore, item_id: object, expected_version: object, *, build_id: str | None = None,
) -> dict[str, Any]:
    """Create or reuse final-layout pages without verifying or submitting the expense.

    A payment-only matter uses the final compositor's unchanged proof scale;
    different expense IDs always start separate pages. Incomplete matters show only received proofs and
    explicitly remain partial previews, never final reimbursement packages.
    """
    return _create_expense_print_preview(store, item_id, expected_version, build_id=build_id, render_pages=True)


def create_expense_print_pdf(
    store: ExpenseStore, item_id: object, expected_version: object, *, build_id: str | None = None,
) -> dict[str, Any]:
    """Reuse the final vector PDF without rendering images that a combined report will render again."""
    return _create_expense_print_preview(store, item_id, expected_version, build_id=build_id, render_pages=False)


def _create_expense_print_preview(
    store: ExpenseStore, item_id: object, expected_version: object, *, build_id: str | None,
    render_pages: bool,
) -> dict[str, Any]:
    if not isinstance(item_id, str) or not item_id or not isinstance(expected_version, str) or not expected_version:
        raise ValueError('请选择有效事项，并提供当前事项版本')
    item = store.get(item_id)
    if item['version'] != expected_version:
        raise ValueError('事项或材料已变化，请刷新后重新预览')
    result: dict[str, Any] = {
        'id': item['id'], 'version': item['version'], 'complete': bool(item['complete']),
        'partial': not bool(item['complete']),
        'missing': list(item['missing']), 'pdf': None, 'preview_pages': [],
        'layout': item.get('layout', ''), 'page_count': 0,
    }
    if not _preview_attachments(item):
        return result
    sources = _source_hashes(store, item)
    fingerprint = {'format': PREVIEW_FORMAT, 'id': item['id'], 'version': item['version'],
                   'complete': bool(item['complete']), 'missing': item['missing'],
                   'build': build_id or source_build_id(), 'pymupdf': pymupdf.VersionBind, 'sources': sources}
    cache_key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    stem = ('expense-preview-' if render_pages else 'expense-pdf-') + cache_key
    folder = store.root / '导出'
    folder.mkdir(exist_ok=True)
    cached = _cached_result(folder, stem, item, render_pages=render_pages)
    if cached is not None:
        _unchanged(store, item, sources)
        return cached
    # Keep the public image-preview cache compatible. Either route may reuse
    # the other route's validated PDF, but its own manifest still describes
    # exactly the artifacts published by this request.
    alternate_stem = ('expense-pdf-' if render_pages else 'expense-preview-') + cache_key
    alternate = _cached_result(folder, alternate_stem, item, render_pages=not render_pages)
    if alternate is not None and not render_pages:
        _unchanged(store, item, sources)
        return {**alternate, 'preview_pages': []}
    published: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix='.expense-preview-', dir=folder) as temporary:
            staging = Path(temporary)
            # Some compositors annotate their in-memory row with page/layout labels.
            if alternate is not None:
                package, layout = folder / alternate['pdf'], alternate['layout']
            else:
                compose = compose_matter if item['complete'] else _compose_partial
                package, layout = compose(store, copy.deepcopy(item), staging)
            pdf_name = stem + '.pdf'
            staged_pdf = staging / pdf_name
            if package != staged_pdf:
                staged_pdf.write_bytes(package.read_bytes())
            pages = []
            with pymupdf.open(staged_pdf) as document:  # type: ignore[no-untyped-call]
                if not len(document) or any(abs(page.rect.width - A4_WIDTH) > 1 or
                                            abs(page.rect.height - A4_HEIGHT) > 1 for page in document):
                    raise ValueError('最终打印预览包含非A4页面，请检查材料或原打印包')
                page_count = len(document)
                if render_pages:
                    for number, page in enumerate(document):
                        name = f'{stem}-{number + 1}.png'
                        page.get_pixmap(dpi=120, alpha=False).save(staging / name)
                        pages.append(name)
            if alternate is None and item.get('material_basis') == 'payment_only':
                layout += '；此预览仅显示当前费用，不同费用在正式报销包中也分别起页'
            result.update(pdf=pdf_name, preview_pages=pages, layout=layout, page_count=page_count)
            artifacts = {name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
                         for name in [pdf_name, *pages]}
            manifest_name = stem + '.json'
            (staging / manifest_name).write_text(json.dumps({'result': result, 'artifacts': artifacts},
                                                           ensure_ascii=False), encoding='utf-8')
            _unchanged(store, item, sources)
            for name in [pdf_name, *pages, manifest_name]:
                final = folder / name
                (staging / name).replace(final)
                published.append(final)
        return result
    except Exception as exc:
        for path in published:
            path.unlink(missing_ok=True)
        if isinstance(exc, (ValueError, OSError)):
            raise
        raise ValueError('无法生成最终打印预览，原件已保留，请检查材料：' + str(exc)) from exc
