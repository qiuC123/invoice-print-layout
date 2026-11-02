"""Synthetic originals only; never seed expenses, recognition or matching results."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
import zipfile

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from invoice_print_layout.report_types import REPORT_LABELS, report_types
from invoice_print_layout.agent_evidence import material_facts
from invoice_print_layout.documents import _parse_positioned_invoice_amount


def prepare(run: Path) -> None:
    workspace, materials = run / 'data', run / 'materials'
    root = workspace / '工作台'
    root.mkdir(parents=True)
    materials.mkdir()
    # No provider discovery or references to the user's saved credentials.
    (root / 'invoice-ai.json').write_text(json.dumps({
        'version': 2, 'enabled': False, 'active': 'offline', 'providers': [{
            'id': 'offline', 'name': '合成验证 AI 关闭', 'kind': 'openai',
            'base_url': 'http://127.0.0.1:1/v1', 'model': 'offline',
            'key_variable': 'VERIFY_WORKBENCH_UNUSED_KEY'}]}), encoding='utf-8')
    (root / 'organize-settings.json').write_text(json.dumps({
        'version': 1, 'auto_after_upload': True, 'use_model': False}), encoding='utf-8')
    (workspace / 'config.toml').write_text('[person]\nname = "合成验证人"\n', encoding='utf-8')
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 36)
    pairs = []
    for index in range(1, 11):
        folder = materials / ('folder/nested' if index == 8 else 'drag-folder/nested' if index == 10 else 'files')
        folder.mkdir(parents=True, exist_ok=True)
        amount, merchant = f'{30 + index}.00', f'合成工具{index:02d}商店'
        invoice = folder / f'invoice-{index:02d}.pdf'
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=650, height=450)
            rows = [(20, 30, '电子发票（普通发票）'), (20, 50, f'发票号码：9900000000000000{index:04d}'),
                    (20, 70, '开票日期：2026年10月20日'), (20, 90, f'销售方名称：{merchant}'),
                    (20, 110, f'订单号：202610209900{index:04d}'),
                    (20, 160, '项目名称'), (200, 160, '单位'), (285, 160, '数量'),
                    (355, 160, '单价'), (425, 160, '金额'), (565, 160, '税额'),
                    (20, 190, '*五金*胶带'), (200, 190, '卷'), (285, 190, '1'),
                    (355, 190, amount), (425, 190, amount), (565, 190, '0.00'),
                    (20, 280, '合计'), (20, 310, '价税合计'), (460, 310, '（小写）'),
                    (505, 310, '¥' + amount)]
            for x, y, text in rows:
                page.insert_text((x, y), text, fontname='china-s', fontsize=10)
            page.draw_rect((10, 12, 635, 430), width=0.6)
            pdf.save(invoice)
        with pymupdf.open(invoice) as check:
            extracted = check[0].get_text()
            observed = str(_parse_positioned_invoice_amount(check[0]))
        facts = material_facts({'id': f'fixture-{index}', 'path': str(invoice), 'role': 'invoice',
                                'revision': 0, 'digest': hashlib.sha256(invoice.read_bytes()).hexdigest(),
                                'text': extracted, 'fields': {'amount': observed}})
        assert facts['complete'] and facts['goods_complete'], facts
        assert facts['total_cents'] == (30 + index) * 100, facts
        payment = folder / f'payment-{index:02d}.png'
        image = Image.new('RGB', (1000, 800), 'white')
        draw = ImageDraw.Draw(image)
        for n, row in enumerate(['微信支付', '支付成功', f'商家名称：{merchant}',
                                 f'订单号：202610209900{index:04d}',
                                 f'实付：￥{amount}', '支付时间：2026-10-20 10:30:00',
                                 f'交易单号：2026102000000000{index:04d}']):
            draw.text((50, 40 + n * 95), row, fill='black', font=font)
        image.save(payment)
        assert len(pymupdf.open(invoice)[0].get_text()) > 60
        pairs.append({'index': index, 'amount_cents': (30 + index) * 100,
                      'merchant': merchant, 'invoice': str(invoice), 'payment': str(payment)})
    (materials / 'folder' / 'skip.txt').write_text('SYNTHETIC unsupported material', encoding='utf-8')
    # Same cell contract as tests.test_report_types.synthetic_template, plus valid XLSX packaging.
    cells = {'A1': '活动运营费用统计表', 'B35': '费用合计：', 'D6': '实际消费', 'H6': '需报销'}
    cells.update({f'B{row}': label for row, label in REPORT_LABELS.items()})
    cells.update({f"A{section['rows'][0]}": section['label'] for section in report_types()})
    sheet = ET.Element('worksheet', xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main')
    data = ET.SubElement(sheet, 'sheetData')
    for number in range(1, 36):
        row = ET.SubElement(data, 'row', r=str(number))
        for address, value in cells.items():
            if int(''.join(filter(str.isdigit, address))) == number:
                cell = ET.SubElement(row, 'c', r=address, t='inlineStr')
                ET.SubElement(ET.SubElement(cell, 'is'), 't').text = value
    with zipfile.ZipFile(materials / 'synthetic-template.xlsx', 'w') as archive:
        archive.writestr('xl/worksheets/sheet1.xml', ET.tostring(sheet, encoding='utf-8'))
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="合成模板" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    manifest = {'pairs': pairs, 'sha256': {str(p.relative_to(materials)): hashlib.sha256(p.read_bytes()).hexdigest()
                                          for p in materials.rglob('*') if p.is_file()}}
    (run / 'fixture.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


def inspect_pdf(filename: Path, pages: int, markers: list[str]) -> None:
    with pymupdf.open(filename) as pdf:
        text = '\n'.join(page.get_text() for page in pdf)
        assert pdf.page_count == pages, (filename.name, pdf.page_count, pages)
        assert all(marker in text for marker in markers), (filename.name, markers, text)
        result = {'file': filename.name, 'page_count': pdf.page_count, 'expected_pages': pages,
                  'markers': markers, 'fonts_per_page': [len(page.get_fonts()) for page in pdf],
                  'drawings_per_page': [len(page.get_drawings()) for page in pdf], 'text': text}
        assert any(result['fonts_per_page']), 'No vector text fonts'
    filename.with_suffix('.inspection.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    with pymupdf.open(filename) as pdf:
        pdf[0].get_pixmap(matrix=pymupdf.Matrix(1, 1)).save(filename.with_suffix('.png'))


if __name__ == '__main__':
    if sys.argv[1] == 'prepare':
        prepare(Path(sys.argv[2]).resolve())
    elif sys.argv[1] == 'pdf':
        inspect_pdf(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4:])
    else:
        raise SystemExit('Expected prepare or pdf')
