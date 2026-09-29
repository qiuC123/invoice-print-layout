"""Build a report, then atomically mark its matters submitted; preserve originals."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import hashlib
from pathlib import Path
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
from invoice_print_layout.payment_layout import append_payments, payment_groups
from invoice_print_layout.storage import safe_filename_part
from invoice_print_layout.workbench import ExpenseStore


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
    if item.get('material_basis') == 'payment_only':
        return [a for a in item['attachments'] if a['role'] == 'payment' and a['available']]
    if item.get('legacy'):
        packages = [a for a in item['attachments'] if a['role'] == 'package' and a['available']]
        if len(packages) != 1:
            raise ValueError('历史打印包缺失或不唯一')
        return packages
    required = {'酒店': {'invoice'}, '高铁': {'invoice'}, '打车': {'trip', 'invoice'},
                '外卖': {'order', 'invoice'}, '顺丰': {'detail', 'invoice'},
                '餐饮': {'invoice', 'order', 'purchase'},
                '材料采购': {'purchase', 'payment' if item['alternative'] else 'invoice'}}
    return sorted((a for a in item['attachments'] if a['role'] in required[item['category']] and a['available']),
                  key=lambda a: (a['role'] in {'invoice', 'payment'}, a['name']))


def compose_matter(store: ExpenseStore, item: dict[str, Any], folder: Path) -> tuple[Path, str]:
    destination = folder / (item['id'] + '.pdf')
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
        if len(grouped['trip']) != 1 or len(grouped['invoice']) != 1 or any(p.suffix.lower() != '.pdf' for _, p in paths):
            raise ValueError('打车请保留一份完整行程单PDF和一份对应电子发票PDF')
        trip, invoice = grouped['trip'][0], grouped['invoice'][0]
        pairs, issues = pair_documents([inspect_pdf(trip), inspect_pdf(invoice)])
        if issues or len(pairs) != 1 or pairs[0].invoice.amount != amount:
            raise ValueError('打车行程与发票配对或台账金额不一致，请检查')
        pages = compose_print_package(trip, invoice, destination)
        validate_print_package(destination, pages, item['amount'], required_source_fragments(trip, invoice))
        return destination, '原助手安全裁剪＋上下拼版'
    if item['category'] == '顺丰':
        if len(grouped['detail']) != 1 or len(grouped['invoice']) != 1 or any(p.suffix.lower() != '.pdf' for _, p in paths):
            raise ValueError('顺丰请保留一份运单明细PDF和一份对应电子发票PDF')
        detail, invoice = grouped['detail'][0], grouped['invoice'][0]
        d, bill = inspect_courier_pdf(detail), inspect_courier_pdf(invoice)
        if d.is_invoice or not bill.is_invoice or d.kind != bill.kind or d.amount != bill.amount or bill.amount != amount:
            raise ValueError('顺丰明细、发票与台账金额不一致')
        if d.kind == '顺丰' and d.invoice_number != bill.invoice_number:
            raise ValueError('顺丰明细与发票号码不对应')
        compose_courier_package(detail, invoice, destination, amount)
        return destination, '原助手顺丰／同城拼版'
    if item['category'] == '外卖':
        from invoice_print_layout.receipt_intake import read_receipt
        from invoice_print_layout.takeout import _parse_order, _parse_image_invoice, _parse_pdf_invoice
        if len(grouped['order']) != 1 or grouped['order'][0].suffix.lower() == '.pdf':
            raise ValueError('外卖需要一张带订单号和实付金额的完整订单截图')
        order_path = grouped['order'][0]
        order = _parse_order(order_path, read_receipt(order_path.read_bytes(), order_path.suffix))
        if order.paid_amount != amount or (item['order_number'] and order.order_number != item['order_number']):
            raise ValueError('订单截图的金额或订单号与台账不一致')
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


def create_report(store: ExpenseStore, ids: list[str], options: dict[str, Any] | None = None, *, submit: bool = True) -> dict[str, str]:
    if not isinstance(ids, list) or not ids or len(ids) > 200 or not all(isinstance(x, str) for x in ids) or len(ids) != len(set(ids)):
        raise ValueError('请选择1至200个不重复事项')
    items = [store.get(key) for key in ids]
    invalid = [x['title'] for x in items if not (x['ready'] if submit else x['complete']) or x['stage'] != 'draft']
    if invalid:
        raise ValueError('制作新报销包只接收材料齐全、已核对且未提交的事项：' + '、'.join(invalid[:5]))
    options = normalize_options(options, items)
    template = store.root / TEMPLATE_NAME
    if not template.is_file():
        raise ValueError('请先在制作窗口上传公司报销模板.xlsx')
    validate_template(template)
    spreadsheet_runtime()
    # Same source/explicit order cannot be counted under two selected matters.
    seen: dict[str, str] = {}
    for item in items:
        keys = ['order:'+item['order_number']] if item['order_number'] else []
        for a in selected_attachments(item):
            path, _ = store.attachment_path(a['id'])
            keys.append('file:'+hashlib.sha256(path.read_bytes()).hexdigest())
        for key in keys:
            if key in seen and seen[key] != item['id']:
                raise ValueError('所选事项存在同一订单或相同凭证，请取消重复事项后制作')
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
                for item in items:
                    if item.get('material_basis') == 'payment_only':
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
                for group in payment_groups(items):
                    append_payments(output, store, group)
                output.save(staging / 'report.pdf', garbage=4, deflate=True)
            with pymupdf.open(staging / 'report.pdf') as checked:
                if not len(checked) or any(abs(p.rect.width-A4_WIDTH)>1 or abs(p.rect.height-A4_HEIGHT)>1 for p in checked):
                    raise ValueError('报销包含非A4页面，请检查原打印包')
            create_excel(template, items, options, staging / 'report.xlsx')
            def clean(value: object) -> str:
                return str(value).replace('|', '/').replace('\n', ' ').replace('\r', ' ')
            lines = ['# 报销材料清单', '', '打印：A4、每张纸1页。已确认仅需支付凭证的事项按2×2拼版，同项目每页最多4张；其他事项保持各自排版。不要在打印窗口再次设置多页合一。',
                     f"报销人：{clean(options['person'] or '待填写')}；期间：{clean(options['period'])}",
                     f"付款来源：{PAYMENTS[options['payment']][1]}。{'制作成功后自动标记已提交，不代表报销到账。' if submit else '试生成，仅供审阅，未改变提交状态。'}", '',
                     '| 事项编号 | 项目 | 类别 | 事项 | 消费日期 | 金额 | PDF页码 | 排版／凭证方式 |',
                     '| --- | --- | --- | --- | --- | ---: | --- | --- |']
            totals: dict[str, int] = {}
            for x in items:
                note = x['layout'] + ('；无发票，扣费记录替代' if x['alternative'] else '')
                lines.append('| '+' | '.join(clean(v) for v in (x['id'],x['project'],x['category'],x['title'],x['expense_date'],x['amount'],x['pages'],note))+' |')
                label = '咖啡' if x['category'] == '外卖' and '咖啡' in (x['title']+x['merchant']) else x['category']
                totals[label] = totals.get(label,0)+x['amount_cents']
            lines += ['', '## 分类合计', '']+[f'- {key}：{value/100:.2f}元' for key,value in totals.items()]
            from invoice_print_layout.report_snapshot import snapshot
            lines += ['', '## 报表汇总（与HTML及Excel相同）', ''] + [f"- H{r['row']} {r['name']}：{r['amount_cents']/100:.2f}元" for r in snapshot(items)['rows']]
            lines += [f'- 总计：{total/100:.2f}元', '', 'Excel使用同一份已核对台账金额；附件不另行计费。活动日期、签字等未知内容留空。']
            (staging / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
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
