"""Build a report, then atomically mark its matters submitted; preserve originals."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid

import pymupdf

from invoice_print_layout.courier import compose_courier_package, inspect_courier_pdf
from invoice_print_layout.documents import inspect_pdf, pair_documents
from invoice_print_layout.layout import (A4_WIDTH, A4_HEIGHT, compose_print_package,
    compose_takeout_print_package, required_source_fragments, validate_print_package,
    validate_takeout_print_package)
from invoice_print_layout.report_excel import create_excel, validate_template, spreadsheet_runtime, TEMPLATE_NAME, PAYMENTS
from invoice_print_layout.payment_layout import append_invoice_payments, append_payments, supplementary_attachments
from invoice_print_layout.storage import safe_filename_part
from invoice_print_layout.workbench import ExpenseStore, expense_identifier_keys
from invoice_print_layout.report_snapshot import material_batches, snapshot
from invoice_print_layout.ride_proofs import invoice_trip_required
from invoice_print_layout.courier_proofs import invoice_waybill_required
from invoice_print_layout.missing_materials import acknowledged_missing_note, validate_acknowledged_sources
from invoice_print_layout.transaction_evidence import (
    extract_transaction_evidence, identifiers_for_record,
)


def _full_pages(paths: list[Path], destination: Path) -> None:
    with pymupdf.open() as output:
        for path in paths:
            with pymupdf.open(path) as original:
                if original.needs_pass:
                    raise ValueError('附件已加密')
                if original.is_pdf:
                    original.bake(annots=True, widgets=False)
                    source = original
                else:
                    source = pymupdf.open('pdf', original.convert_to_pdf())
                try:
                    for page in range(len(source)):
                        target = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                        target.show_pdf_page(pymupdf.Rect(12, 12, A4_WIDTH-12, A4_HEIGHT-12), source, page)
                finally:
                    if source is not original:
                        source.close()
        output.save(destination, garbage=4, deflate=True)


def selected_attachments(item: dict[str, Any]) -> list[dict[str, Any]]:
    if item.get('missing_acknowledged') and not item['complete']:
        return [attachment for attachment in item['attachments'] if attachment['available']]
    if item.get('material_basis') == 'payment_only':
        return [a for a in item['attachments'] if a['role'] == 'payment' and a['available']]
    if invoice_trip_required(item):
        return sorted((a for a in item['attachments']
                       if a['role'] in {'trip', 'invoice'} and a['available']),
                      key=lambda a: (a['role'] == 'invoice', a['name']))
    if invoice_waybill_required(item):
        return sorted((a for a in item['attachments']
                       if a['role'] in {'detail', 'invoice'} and a['available']),
                      key=lambda a: (a['role'] == 'invoice', a['name']))
    if item.get('material_basis') == 'invoice_payment':
        return sorted((a for a in item['attachments']
                       if a['role'] in {'invoice', 'receipt', 'payment'} and a['available']),
                      key=lambda a: (a['role'] == 'payment', a['name'])) + supplementary_attachments(item)
    if item.get('legacy'):
        packages = [a for a in item['attachments'] if a['role'] == 'package' and a['available']]
        if len(packages) != 1:
            raise ValueError('历史打印包缺失或不唯一')
        return packages
    # Hotel payments already bound to this fee are supporting originals, not
    # additional charges. Preserve them alongside the invoice when printing.
    required = {'酒店': {'invoice', 'payment'}, '高铁': {'invoice'}, '打车': {'trip', 'invoice'},
                '外卖': {'order', 'invoice'}, '顺丰': {'detail', 'invoice'},
                '餐饮': {'invoice', 'order', 'purchase'},
                '材料采购': {'purchase', 'payment' if item['alternative'] else 'invoice'}}
    return sorted((a for a in item['attachments'] if a['role'] in required[item['category']] and a['available']),
                  key=lambda a: (a['role'] in {'invoice', 'payment'}, a['name']))


def validate_ride_originals(store: ExpenseStore, item: dict[str, Any]) -> None:
    """A previously checked taxi proof cannot silently become a different PDF."""
    with store.connect() as db:
        digests = {row['id']: row['digest'] for row in db.execute(
            'SELECT id,digest FROM attachments WHERE item_id=?', (item['id'],))}
    for attachment in selected_attachments(item):
        path, _ = store.attachment_path(attachment['id'])
        digest = digests.get(attachment['id'])
        if not digest or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('打车原件与保存时不一致，请重新检查材料；原记录已保留')


def validate_courier_originals(store: ExpenseStore, item: dict[str, Any]) -> None:
    """Keep the selected invoice and waybill bound to their saved originals."""
    with store.connect() as db:
        digests = {row['id']: row['digest'] for row in db.execute(
            'SELECT id,digest FROM attachments WHERE item_id=?', (item['id'],))}
    for attachment in selected_attachments(item):
        path, _ = store.attachment_path(attachment['id'])
        digest = digests.get(attachment['id'])
        if not digest or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('快递原件与保存时不一致，请重新检查材料；原记录已保留')


def compose_matter(store: ExpenseStore, item: dict[str, Any], folder: Path) -> tuple[Path, str]:
    destination = folder / (item['id'] + '.pdf')
    if item.get('missing_acknowledged') and not item['complete']:
        if not selected_attachments(item):
            raise ValueError('该费用无材料；已核对记录保留，不生成凭证页')
        # The partial compositor preserves every received proof, including
        # unclassified originals. Import only when called, avoiding the preview
        # module's existing import of this final compositor.
        from invoice_print_layout.expense_print_preview import _compose_partial
        return _compose_partial(store, item, folder)
    if (item.get('material_basis') == 'invoice_payment' and not invoice_trip_required(item)
            and not invoice_waybill_required(item)):
        with pymupdf.open() as output:
            append_invoice_payments(output, store, item)
            output.save(destination, garbage=4, deflate=True)
        return destination, item['layout']
    from invoice_print_layout.invoice_two_up import invoice_pairable, append_invoice_pairs
    if invoice_pairable(store, item):
        with pymupdf.open() as output:
            append_invoice_pairs(output, store, [item])
            output.save(destination, garbage=4, deflate=True)
        return destination, item['layout']
    if item.get('material_basis') == 'payment_only':
        with pymupdf.open() as output:
            append_payments(output, store, [item])
            output.save(destination, garbage=4, deflate=True)
        return destination, item['layout']
    attachments = selected_attachments(item)
    paths = [(a['role'], store.attachment_path(a['id'])[0]) for a in attachments]
    if item.get('legacy'):
        return paths[0][1], '保留原助手历史拼版'
    grouped = {role: [p for r, p in paths if r == role] for role, _ in paths}
    amount = Decimal(item['amount'])
    if item['category'] == '打车':
        validate_ride_originals(store, item)
        if len(grouped.get('trip', [])) != 1 or len(grouped.get('invoice', [])) != 1 or any(p.suffix.lower() != '.pdf' for _, p in paths):
            raise ValueError('打车请保留一份完整行程单PDF和一份对应电子发票PDF')
        trip, invoice = grouped['trip'][0], grouped['invoice'][0]
        pairs, issues = pair_documents([inspect_pdf(trip), inspect_pdf(invoice)])
        if issues or len(pairs) != 1 or pairs[0].invoice.amount != amount:
            raise ValueError('打车行程与发票配对或台账金额不一致，请检查')
        pages = compose_print_package(trip, invoice, destination)
        validate_print_package(destination, pages, item['amount'], required_source_fragments(trip, invoice))
        validate_ride_originals(store, item)
        note = ('行程单安全裁剪；同一发票全部页面保留，短续页在能减少纸张时优先合排，原票页码保留'
                if pairs[0].invoice.page_count > 1 else '原助手安全裁剪＋上下拼版')
        return destination, note
    if invoice_waybill_required(item):
        validate_courier_originals(store, item)
        if len(grouped.get('detail', [])) != 1 or len(grouped.get('invoice', [])) != 1 or any(p.suffix.lower() != '.pdf' for _, p in paths):
            raise ValueError('顺丰请保留一份运单明细PDF和一份对应电子发票PDF')
        detail, invoice = grouped['detail'][0], grouped['invoice'][0]
        d, bill = inspect_courier_pdf(detail), inspect_courier_pdf(invoice)
        if d.is_invoice or not bill.is_invoice or d.kind != bill.kind or d.amount != bill.amount or bill.amount != amount:
            raise ValueError('顺丰明细、发票与台账金额不一致')
        if d.kind == '顺丰' and d.invoice_number != bill.invoice_number:
            raise ValueError('顺丰明细与发票号码不对应')
        compose_courier_package(detail, invoice, destination, amount)
        validate_courier_originals(store, item)
        return destination, '快递发票＋运单明细；付款截图保留为附件，不重复打印'
    if item['category'] == '外卖':
        from invoice_print_layout.receipt_intake import read_receipt, rows_of
        from invoice_print_layout.takeout import _parse_order, _parse_image_invoice, _parse_pdf_invoice
        if len(grouped['order']) != 1 or grouped['order'][0].suffix.lower() == '.pdf':
            raise ValueError('外卖需要一张带订单号和实付金额的完整订单截图')
        order_path = grouped['order'][0]
        order_lines = read_receipt(order_path.read_bytes(), order_path.suffix)
        order = _parse_order(order_path, order_lines)
        if order.paid_amount != amount:
            raise ValueError('订单截图的金额与台账不一致')
        original_identifiers = extract_transaction_evidence('\n'.join(rows_of(order_lines)), 'order')['identifiers']
        ledger_identifiers = identifiers_for_record(item)
        original_orders: dict[str, set[str]] = {}
        ledger_orders: dict[str, set[str]] = {}
        for identifiers, by_namespace in ((original_identifiers, original_orders), (ledger_identifiers, ledger_orders)):
            for identifier in identifiers:
                if identifier.get('kind') == 'shopping_order' and identifier.get('namespace'):
                    by_namespace.setdefault(identifier['namespace'], set()).add(identifier['value'])
        for namespace in original_orders.keys() & ledger_orders.keys():
            if original_orders[namespace] != ledger_orders[namespace]:
                raise ValueError('订单截图与台账中同一购物平台的业务订单号不一致')
        bills = []
        numbers: set[str] = set()
        for path in grouped['invoice']:
            takeaway_bill = (_parse_pdf_invoice(path, path) if path.suffix.lower() == '.pdf' else
                    _parse_image_invoice(path, read_receipt(path.read_bytes(), path.suffix)))
            if takeaway_bill.invoice_number in numbers:
                raise ValueError('同一事项包含重复发票号码，请只保留一份发票')
            numbers.add(takeaway_bill.invoice_number)
            bills.append(takeaway_bill)
        if sum((bill.amount for bill in bills), Decimal('0')) != amount:
            raise ValueError('外卖商家／平台发票合计与订单实付不一致')
        pages = compose_takeout_print_package(order_path, order.crop, [(x.render_path, x.crop) for x in bills], destination)
        validate_takeout_print_package(destination, pages)
        return destination, '原助手订单＋全部配套发票拼版'
    # No proven crop template for material receipts/hotels/rail: retain all content.
    _full_pages([p for _, p in paths], destination)
    return destination, '完整原件逐页A4（长凭证不裁切）'


def normalize_options(options: dict[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, str]:
    raw = options or {}
    if not isinstance(raw, dict):
        raise ValueError('制作参数无效')
    result = {key: str(raw.get(key, '')).strip() for key in ('person', 'period')}
    if any(len(v) > 120 for v in result.values()):
        raise ValueError('报销人或期间填写过长')
    result['payment'] = str(raw.get('payment', 'personal'))
    if result['payment'] not in PAYMENTS:
        raise ValueError('请选择有效付款来源')
    if not result['period']:
        dates = sorted(x['expense_date'] for x in items if x['expense_date'])
        result['period'] = (dates[0] + '至' + dates[-1] if dates else '消费日期待填写')
        if dates and len(dates) != len(items):
            result['period'] += '（部分消费日期待填写）'
    return result


def person_allocation_lines(report: dict[str, Any]) -> list[str]:
    """Internal allocation of this batch, independent of its reporting leader."""
    allocation = report['person_allocation']
    if allocation['total_cents'] != report['total_cents']:
        raise ValueError('人员金额明细与报销合计不一致')
    lines = ['', '## 每人费用清单与金额汇总', '',
             '与公司报销表使用同一批费用、同一金额。按费用编号逐笔统计，每笔费用只计一次，附件不另行计费；未指定费用归属人的金额单列。', '',
             '| 费用归属人 | 费用笔数 | 同批报销金额（元） |', '| --- | ---: | ---: |']
    for row in allocation['rows']:
        label = str(row['label']).replace('|', '/').replace('\n', ' ').replace('\r', ' ')
        lines.append(f"| {label} | {row['expense_count']} | {row['total_cents']/100:.2f} |")
    lines.append(f"| 合计 | {allocation['expense_count']} | {allocation['total_cents']/100:.2f} |")
    return lines


def create_report(store: ExpenseStore, ids: list[str], options: dict[str, Any] | None = None, *,
                  submit: bool = True, include_table: bool = False) -> dict[str, str]:
    if not isinstance(ids, list) or not ids or len(ids) > 200 or not all(isinstance(x, str) for x in ids) or len(ids) != len(set(ids)):
        raise ValueError('请选择1至200个不重复事项')
    items = [store.get(key) for key in ids]
    invalid = [x['title'] for x in items if x.get('deleted') or x.get('merged_into') or x.get('returned_to_synthesis') or
               x.get('synthesis_material_state') in {'archived', 'deleted'} or
               not (x['ready'] if submit else x['complete'] or x.get('missing_acknowledged')) or x['stage'] != 'draft']
    if invalid:
        raise ValueError('制作新报销包只接收材料齐全、已核对且未提交的事项：' + '、'.join(invalid[:5]))
    if all(item.get('missing_acknowledged') and not selected_attachments(item) for item in items):
        raise ValueError('所选费用均无材料，已核对记录保留；请补充实际材料后生成PDF')
    report_snapshot = snapshot(items)
    items = report_snapshot['items']
    options = normalize_options(options, items)
    template = store.root / TEMPLATE_NAME
    if not template.is_file():
        raise ValueError('请先在制作窗口上传公司报销模板.xlsx')
    validate_template(template)
    spreadsheet_runtime()
    # Keep source deduplication and compare identifiers only inside an explicit
    # system. A payment order number is not a shopping business order number.
    seen: dict[str, str] = {}
    with store.connect() as db:
        for item in items:
            source_row = db.execute('SELECT source FROM expenses WHERE id=?', (item['id'],)).fetchone()
            source = str(source_row['source'] or '') if source_row else ''
            keys = ['transaction:' + key for key in expense_identifier_keys(db, item['id'], item, source=source)]
            for a in selected_attachments(item):
                path, _ = store.attachment_path(a['id'])
                keys.append('file:'+hashlib.sha256(path.read_bytes()).hexdigest())
            for key in keys:
                if key in seen and seen[key] != item['id']:
                    raise ValueError('所选事项存在同一体系的交易编号或相同凭证，请取消重复事项后制作')
                seen[key] = item['id']
    folder = store.root / '导出'
    folder.mkdir(exist_ok=True)
    total = sum(x['amount_cents'] for x in items)
    stem = (datetime.now().strftime('%Y-%m-%d_%H%M%S') + f'_报销包_{total/100:.2f}元_'
            + safe_filename_part(options['person'] or '待填姓名') + '_' + uuid.uuid4().hex[:6])
    outputs = {key: folder / (stem + '.' + key) for key in ('pdf', 'md', 'xlsx')}
    published: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix='.report-', dir=folder) as temporary:
            staging = Path(temporary)
            with pymupdf.open() as output:
                for batch in material_batches(report_snapshot, store):
                    item = batch[0]
                    if len(batch) > 1:
                        from invoice_print_layout.invoice_two_up import append_invoice_pairs
                        append_invoice_pairs(output, store, batch)
                        continue
                    if item.get('missing_acknowledged') and not selected_attachments(item):
                        item['pages'] = '无材料'
                        item['layout'] = acknowledged_missing_note(item) + '；无材料，不生成凭证页'
                        continue
                    if item.get('material_basis') == 'payment_only' and not item.get('missing_acknowledged'):
                        append_payments(output, store, batch)
                        continue
                    try:
                        package, note = compose_matter(store, item, staging)
                        start = len(output) + 1
                        with pymupdf.open(package) as source:
                            output.insert_pdf(source)
                        item['pages'] = f'{start}-{len(output)}' if len(output) != start else str(start)
                        item['layout'] = note
                    except Exception as exc:
                        raise ValueError(f"{item['title']}：{exc}") from exc
                output.save(staging / 'report.pdf', garbage=4, deflate=True)
            with pymupdf.open(staging / 'report.pdf') as checked:
                if not len(checked) or any(abs(p.rect.width-A4_WIDTH)>1 or abs(p.rect.height-A4_HEIGHT)>1 for p in checked):
                    raise ValueError('报销包含非A4页面，请检查原打印包')
            if include_table:
                # The table is the first physical PDF page, including the page
                # references written into the same workbook and MD checklist.
                for item in items:
                    if item.get('pages') != '无材料':
                        item['pages'] = re.sub(r'\d+', lambda match: str(int(match.group()) + 1), item['pages'])
                summary = staging / 'summary.pdf'
                create_excel(template, items, options, staging / 'report.xlsx', summary_pdf=summary)
                with pymupdf.open(summary) as combined, pymupdf.open(staging / 'report.pdf') as materials:
                    combined.insert_pdf(materials)
                    combined.save(staging / 'combined.pdf', garbage=4, deflate=True)
                (staging / 'combined.pdf').replace(staging / 'report.pdf')
            else:
                create_excel(template, items, options, staging / 'report.xlsx')
            def clean(value: object) -> str:
                return str(value).replace('|', '/').replace('\n', ' ').replace('\r', ' ')
            lines = ['# 报销材料清单', '', '打印：A4、每张纸1页。材料按报销表从上到下的项目顺序排列，同一报销子项连续放置；同项目同类别同子项的合格单发票按原字号每张A4上下两张，奇数末张单独一页，额外必需材料保持完整，其他费用分别起页。快递采用发票＋运单明细，付款截图保留为附件。同笔票据与付款材料仅在原印刷大小不变、原页完整保留且能减少页数时自动重排；其他事项保持各自排版。不要在打印窗口再次设置多页合一。',
                     f"报销人：{clean(options['person'] or '待填写')}；期间：{clean(options['period'])}",
                     f"付款来源：{PAYMENTS[options['payment']][1]}。{'制作成功后自动标记已提交，不代表报销到账。' if submit else '试生成，仅供审阅，未改变提交状态。'}", '',
                     '| 事项编号 | 项目 | 类别 | 事项 | 消费日期 | 金额 | PDF页码 | 排版／凭证方式 | 费用归属人 |',
                     '| --- | --- | --- | --- | --- | ---: | --- | --- | --- |']
            totals: dict[str, int] = {}
            for x in items:
                note = x['layout'] + ('；无发票，扣费记录替代' if x['alternative'] else '')
                person_label = (x.get('reimbursement_person_label') or '未指定') if x.get('reimbursement_person_id') else '未指定'
                lines.append('| '+' | '.join(clean(v) for v in (x['id'],x['project'],x['category'],x['title'],x['expense_date'],x['amount'],x['pages'],note,person_label))+' |')
                label = '咖啡' if x['category'] == '外卖' and '咖啡' in (x['title']+x['merchant']) else x['category']
                totals[label] = totals.get(label,0)+x['amount_cents']
            lines += ['', '## 分类合计', '']+[f'- {key}：{value/100:.2f}元' for key,value in totals.items()]
            lines += ['', '## 报表汇总（与HTML及Excel相同）', ''] + [f"- H{r['row']} {r['name']}：{r['amount_cents']/100:.2f}元" for r in report_snapshot['rows']]
            lines += [f'- 总计：{total/100:.2f}元', '', 'Excel使用同一份已核对台账金额；附件不另行计费。活动日期、签字等未知内容留空。']
            lines += person_allocation_lines(report_snapshot)
            (staging / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
            validate_acknowledged_sources(store, items)
            for kind, final in outputs.items():
                (staging / ('report.'+kind)).replace(final)
                published.append(final)
        result = {kind: path.name for kind,path in outputs.items()}
        if submit:
            store.submit_report(items, result)
        return result
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
