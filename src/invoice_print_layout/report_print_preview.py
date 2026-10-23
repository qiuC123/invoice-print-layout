"""Read-only final printing: the real company form followed by original A4 proofs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

import pymupdf

from invoice_print_layout.expense_print_preview import (
    _source_hashes, _unchanged, create_expense_print_pdf,
)
from invoice_print_layout.reimbursement import normalize_options
from invoice_print_layout.report_excel import TEMPLATE_NAME, create_excel
from invoice_print_layout.report_snapshot import material_batches, snapshot
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_runtime import source_build_id

PREVIEW_FORMAT = 'report-print-preview-v3-invoice-two-up-courier'


def _cached(folder: Path, stem: str) -> dict[str, Any] | None:
    try:
        manifest = json.loads((folder / (stem + '.json')).read_text(encoding='utf-8'))
        result = manifest['result']
        names = [result['combined_pdf'], result['xlsx'], *result['combined_preview_pages']]
        if (result['table_page_count'] != 1 or result['page_count'] != len(result['combined_preview_pages']) or
                result['pdf'] != result['combined_pdf'] or result['preview_pages'] != result['combined_preview_pages'] or
                len(result['combined_page_sizes']) != result['page_count'] or set(names) != set(manifest['artifacts'])):
            return None
        if any(Path(name).name != name or hashlib.sha256((folder / name).read_bytes()).hexdigest() !=
               manifest['artifacts'][name] for name in names):
            return None
        return dict(result)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def create_report_print_preview(
    store: ExpenseStore, ids: list[str], options: dict[str, Any] | None = None, *, build_id: str | None = None,
) -> dict[str, Any]:
    """Preview any live/historical fee without reviewing, submitting or changing it."""
    if (not isinstance(ids, list) or not ids or len(ids) > 200 or
            not all(isinstance(key, str) and key for key in ids) or len(set(ids)) != len(ids)):
        raise ValueError('请选择1至200个不重复事项')
    current_items = store.get_many(ids)
    if any(item.get('deleted') or item.get('merged_into') or item.get('returned_to_synthesis') or
           item.get('stage') == 'cancelled' or
           item.get('synthesis_material_state') in {'archived', 'deleted'} for item in current_items):
        raise ValueError('所选费用已移除或退回材料合成，请刷新后重新选择')
    report = snapshot(current_items)
    items = report['items']
    normalized = normalize_options(options, items)
    template = store.root / TEMPLATE_NAME
    if not template.is_file():
        raise ValueError('请先在制作窗口上传公司报销模板.xlsx')
    template_hash = hashlib.sha256(template.read_bytes()).hexdigest()
    build = build_id or source_build_id()
    sources = {item['id']: _source_hashes(store, item) for item in items}
    fingerprint = {'format': PREVIEW_FORMAT, 'build': build, 'options': normalized, 'template': template_hash,
                   'items': [{'id': item['id'], 'version': item['version'], 'sources': sources[item['id']]}
                             for item in items], 'pymupdf': pymupdf.VersionBind}
    stem = 'report-preview-' + hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    folder = store.root / '导出'
    folder.mkdir(exist_ok=True)

    def unchanged() -> None:
        current = store.get_many([item['id'] for item in items])
        for item, latest in zip(items, current, strict=True):
            _unchanged(store, item, sources[item['id']], current=latest)
        if hashlib.sha256(template.read_bytes()).hexdigest() != template_hash:
            raise ValueError('预览期间公司模板已变化，请重新打开预览')

    cached = _cached(folder, stem)
    if cached is not None:
        unchanged()
        return cached
    published: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix='.report-preview-', dir=folder) as temporary:
            staging = Path(temporary)
            materials: list[Path] = []
            count = 1
            for batch in material_batches(report, store):
                item = batch[0]
                if len(batch) > 1:
                    from invoice_print_layout.invoice_two_up import append_invoice_pairs
                    paired = staging / ('invoices-' + item['id'] + '.pdf')
                    with pymupdf.open() as invoice_pages:  # type: ignore[no-untyped-call]
                        append_invoice_pairs(invoice_pages, store, batch)
                        invoice_pages.save(paired, garbage=4, deflate=True)
                    for invoice in batch:
                        invoice['pages'] = str(count + 1)
                    materials.append(paired)
                    count += 1
                    continue
                result = create_expense_print_pdf(store, item['id'], item['version'], build_id=build)
                pages = result['page_count']
                if pages:
                    item['pages'] = str(count + 1) if pages == 1 else f'{count + 1}-{count + pages}'
                    materials.append(folder / result['pdf'])
                    count += pages
                else:
                    item['pages'] = '无材料'
                item['layout'] = result['layout']
            pdf_name = stem + '.pdf'
            xlsx_name = stem + '.xlsx'
            summary = staging / 'summary.pdf'
            create_excel(template, items, normalized, staging / xlsx_name, summary_pdf=summary)
            with pymupdf.open(summary) as combined:  # type: ignore[no-untyped-call]
                if len(combined) != 1:
                    raise ValueError('报销主表应为一页，请检查公司模板')
                for material in materials:
                    with pymupdf.open(material) as document:  # type: ignore[no-untyped-call]
                        combined.insert_pdf(document)
                combined.save(staging / pdf_name, garbage=4, deflate=True)
            png_names: list[str] = []
            sizes: list[dict[str, float]] = []
            with pymupdf.open(staging / pdf_name) as document:  # type: ignore[no-untyped-call]
                for number, page in enumerate(document):
                    name = f'{stem}-{number + 1}.png'
                    page.get_pixmap(dpi=120, alpha=False).save(staging / name)
                    png_names.append(name)
                    sizes.append({'width': float(page.rect.width), 'height': float(page.rect.height)})
            result = {'combined_pdf': pdf_name, 'combined_preview_pages': png_names,
                      'combined_page_sizes': sizes, 'table_page_count': 1, 'page_count': len(png_names),
                      'pdf': pdf_name, 'preview_pages': png_names, 'xlsx': xlsx_name,
                      'ordered_item_ids': report['ordered_item_ids'], 'total_cents': report['total_cents']}
            names = [pdf_name, xlsx_name, *png_names]
            manifest_name = stem + '.json'
            manifest = {'result': result,
                        'artifacts': {name: hashlib.sha256((staging / name).read_bytes()).hexdigest() for name in names}}
            (staging / manifest_name).write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
            unchanged()
            for name in [*names, manifest_name]:
                final = folder / name
                (staging / name).replace(final)
                published.append(final)
        return result
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
