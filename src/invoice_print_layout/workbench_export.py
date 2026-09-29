"""Explicit, read-only-to-ledger export of reviewed expense matters."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pymupdf

from invoice_print_layout.layout import A4_WIDTH, A4_HEIGHT
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.payment_layout import append_payments, payment_groups
from invoice_print_layout.reimbursement import selected_attachments


def export_items(store: ExpenseStore, ids: list[str]) -> dict[str, str]:
    if not ids or len(ids) > 200 or len(ids) != len(set(ids)):
        raise ValueError('请选择1至200个不重复事项')
    items = [store.get(key) for key in ids]
    if any(not x['ready'] or x['stage'] == 'cancelled' for x in items):
        raise ValueError('所选事项必须材料齐全且已核对，不能包含已取消事项')
    folder = store.root / '导出'
    folder.mkdir(exist_ok=True)
    stem = datetime.now().strftime('%Y-%m-%d_%H%M%S') + '_报销材料_' + uuid.uuid4().hex[:6]
    pdf, md = folder / (stem + '.pdf'), folder / (stem + '.md')
    temp = folder / (stem + '.tmp')
    try:
        with pymupdf.open() as output:
            for item in items:
                if item.get('material_basis') == 'payment_only':
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
            for group in payment_groups(items):
                append_payments(output, store, group)
            if not len(output):
                raise ValueError('没有可导出的页面')
            output.save(temp, garbage=4, deflate=True)
        with pymupdf.open(temp) as check:
            if any(abs(p.rect.width-A4_WIDTH)>1 or abs(p.rect.height-A4_HEIGHT)>1 for p in check):
                raise ValueError('导出页面不是A4')
        temp.replace(pdf)
        def clean(value: object) -> str:
            return str(value).replace('|', '/').replace('\n', ' ').replace('\r', ' ')
        lines = ['# 项目报销材料清单', '', '打印：A4、每张纸1页。仅支付凭证按2×2排列，同项目每页最多4张；其余材料保持原排版。所有内容完整缩放不裁切。不要再次设置多页合一。导出不代表已经提交或报销。', '',
                 '| 事项编号 | 项目 | 类别 | 事项 | 日期 | 金额 | 凭证方式 |', '| --- | --- | --- | --- | --- | ---: | --- |']
        totals: dict[str, int] = {}
        for x in items:
            lines.append('| ' + ' | '.join(clean(v) for v in (x['id'],x['project'],x['category'],x['title'],x['expense_date'],x['amount'],
                                                            '仅支付凭证（已确认）' if x.get('material_basis') == 'payment_only' else '无发票替代（扣费记录）' if x['alternative'] else '发票／历史打印包')) + ' |')
            totals[x['category']] = totals.get(x['category'], 0) + x['amount_cents']
        lines += ['', '## 分类合计', ''] + [f'- {k}：{v/100:.2f}元' for k,v in totals.items()]
        lines += [f'- 总计：{sum(totals.values())/100:.2f}元', '', '金额采用台账中人工核对后的支出金额，附件不会重复计费。']
        md.write_text('\n'.join(lines), encoding='utf-8')
        return {'pdf': pdf.name, 'md': md.name}
    except Exception:
        for file in (temp, pdf, md):
            file.unlink(missing_ok=True)
        raise
