"""Explicit, read-only-to-ledger export of reviewed expense matters."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
import tempfile

import pymupdf

from invoice_print_layout.layout import A4_WIDTH, A4_HEIGHT
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.payment_layout import append_invoice_payments, append_payments
from invoice_print_layout.reimbursement import compose_matter, selected_attachments
from invoice_print_layout.report_snapshot import material_batches, snapshot
from invoice_print_layout.ride_proofs import invoice_trip_required
from invoice_print_layout.courier_proofs import invoice_waybill_required
from invoice_print_layout.missing_materials import acknowledged_missing_note, validate_acknowledged_sources


def export_items(store: ExpenseStore, ids: list[str]) -> dict[str, str]:
    if not ids or len(ids) > 200 or len(ids) != len(set(ids)):
        raise ValueError('请选择1至200个不重复事项')
    items = [store.get(key) for key in ids]
    if any(not x['ready'] or x['stage'] == 'cancelled' for x in items):
        raise ValueError('所选事项必须材料齐全且已核对，不能包含已取消事项')
    if all(item.get('missing_acknowledged') and not selected_attachments(item) for item in items):
        raise ValueError('所选费用均无材料，已核对记录保留；请补充实际材料后生成PDF')
    report_snapshot = snapshot(items)
    items = report_snapshot['items']
    folder = store.root / '导出'
    folder.mkdir(exist_ok=True)
    stem = datetime.now().strftime('%Y-%m-%d_%H%M%S') + '_报销材料_' + uuid.uuid4().hex[:6]
    pdf, md = folder / (stem + '.pdf'), folder / (stem + '.md')
    temp = folder / (stem + '.tmp')
    try:
        with tempfile.TemporaryDirectory(prefix='.ride-export-', dir=folder) as temporary, pymupdf.open() as output:
            staging = Path(temporary).resolve()
            if not staging.is_relative_to(folder.resolve()):
                raise ValueError('导出临时目录不在工作台内')
            for batch in material_batches(report_snapshot, store):
                item = batch[0]
                from invoice_print_layout.invoice_two_up import invoice_pairable, append_invoice_pairs
                if len(batch) > 1 or invoice_pairable(store, item):
                    append_invoice_pairs(output, store, batch)
                    continue
                if item.get('missing_acknowledged'):
                    if not selected_attachments(item):
                        item['pages'] = '无材料'
                        item['layout'] = acknowledged_missing_note(item) + '；无材料，不生成凭证页'
                        continue
                    acknowledged_package, note = compose_matter(store, item, staging)
                    start = len(output) + 1
                    with pymupdf.open(acknowledged_package) as source:
                        output.insert_pdf(source)
                    item['pages'] = ','.join(str(page) for page in range(start, len(output) + 1))
                    item['layout'] = note
                    continue
                if item.get('material_basis') == 'payment_only':
                    append_payments(output, store, batch)
                    continue
                if invoice_trip_required(item) or invoice_waybill_required(item):
                    ride_package, _ = compose_matter(store, item, staging)
                    with pymupdf.open(ride_package) as source:
                        output.insert_pdf(source)
                    continue
                if item.get('material_basis') == 'invoice_payment':
                    append_invoice_payments(output, store, item)
                    continue
                package = next((a for a in item['attachments'] if a['role'] == 'package' and a['available']), None)
                if item.get('legacy') and package:
                    path, _ = store.attachment_path(package['id'])
                    with pymupdf.open(path) as source:
                        output.insert_pdf(source)
                    continue
                attachments = selected_attachments(item)
                for attachment in attachments:
                    path, _ = store.attachment_path(attachment['id'])
                    with pymupdf.open(path) as original:
                        if original.needs_pass:
                            raise ValueError('附件已加密，请提供可读取的原始文件')
                        converted = None
                        if original.is_pdf:
                            original.bake(annots=True, widgets=False)
                            source = original
                        else:
                            converted = pymupdf.open('pdf', original.convert_to_pdf())
                            source = converted
                        try:
                            for page in range(len(source)):
                                # New, untemplated receipts use a full sheet for legibility.
                                # Existing validated print packages above retain their layout.
                                target = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                                target.show_pdf_page(pymupdf.Rect(12, 12, A4_WIDTH-12, A4_HEIGHT-12), source, page, keep_proportion=True)
                        finally:
                            if converted:
                                converted.close()
                # A new item always begins a fresh A4 page; never share spare slots.
            if not len(output):
                raise ValueError('没有可导出的页面')
            output.save(temp, garbage=4, deflate=True)
        with pymupdf.open(temp) as check:
            if any(abs(p.rect.width-A4_WIDTH)>1 or abs(p.rect.height-A4_HEIGHT)>1 for p in check):
                raise ValueError('导出页面不是A4')
        validate_acknowledged_sources(store, items)
        temp.replace(pdf)
        def clean(value: object) -> str:
            return str(value).replace('|', '/').replace('\n', ' ').replace('\r', ' ')
        lines = ['# 项目报销材料清单', '', '打印：A4、每张纸1页。材料按报销表从上到下的项目顺序排列，同一报销子项连续放置；同项目同类别同子项的合格单发票按原字号每张A4上下两张，奇数末张单独一页，额外必需材料保持完整，其他费用分别起页。快递采用发票＋运单明细，付款截图保留为附件。打车发票与行程单按已验证内容边界去除空白和装饰，上下拼版；同笔票据与付款材料仅在原印刷大小不变、原页完整保留且能减少页数时自动重排；其余材料完整缩放，保留原排版。不要再次设置多页合一。导出不代表已经提交或报销。', '',
                 '| 事项编号 | 项目 | 类别 | 事项 | 日期 | 金额 | 凭证方式 |', '| --- | --- | --- | --- | --- | ---: | --- |']
        totals: dict[str, int] = {}
        for x in items:
            lines.append('| ' + ' | '.join(clean(v) for v in (x['id'],x['project'],x['category'],x['title'],x['expense_date'],x['amount'],
                                                            x['layout'] if x.get('missing_acknowledged') else
                                                            '仅支付凭证（已确认）' if x.get('material_basis') == 'payment_only' else '发票＋行程单' if invoice_trip_required(x) else '发票＋运单明细' if invoice_waybill_required(x) else '发票／收据＋付款截图' if x.get('material_basis') == 'invoice_payment' else '无发票替代（扣费记录）' if x['alternative'] else '发票／历史打印包')) + ' |')
            totals[x['category']] = totals.get(x['category'], 0) + x['amount_cents']
        lines += ['', '## 分类合计', ''] + [f'- {k}：{v/100:.2f}元' for k,v in totals.items()]
        lines += [f'- 总计：{sum(totals.values())/100:.2f}元', '', '金额采用台账中人工核对后的支出金额，附件不会重复计费。']
        md.write_text('\n'.join(lines), encoding='utf-8')
        return {'pdf': pdf.name, 'md': md.name}
    except Exception:
        for file in (temp, pdf, md):
            file.unlink(missing_ok=True)
        raise
