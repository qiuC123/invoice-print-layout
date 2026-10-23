"""Local spreadsheet runtime and private company template integration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import re
from typing import Any
from xml.etree import ElementTree as ET
import zipfile

from invoice_print_layout.report_types import REPORT_LABELS, report_types

TEMPLATE_NAME = '报销模板.xlsx'
PAYMENTS = {'personal': ('H', '现场现金支付（需报销）')}


def validate_template(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(x.file_size for x in archive.infolist()) > 30 * 1024 * 1024:
                raise ValueError('模板解压后超过30MB')
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(x.itertext()) for x in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
            cells = {}
            for cell in root.findall('.//s:sheetData/s:row/s:c', ns):
                value = cell.find('s:v', ns)
                inline = cell.find('s:is', ns)
                text = ''.join(inline.itertext()) if inline is not None else value.text if value is not None else ''
                if cell.get('t') == 's':
                    text = strings[int(text or '0')]
                cells[cell.get('r')] = text or ''
            if cells.get('A1') != '活动运营费用统计表' or cells.get('B35') != '费用合计：':
                raise ValueError('请上传当前支持的活动运营费用统计表模板')
            for key, part in [('D6', '实际消费'), ('H6', '需报销'), ('A24', '材料')]:
                if part not in cells.get(key, ''):
                    raise ValueError('模板布局不同，不能自动填写')
            for row, label in REPORT_LABELS.items():
                if re.sub(r'\s+', '', cells.get(f'B{row}', '')) != label:
                    raise ValueError('模板二级分类不同，不能自动填写')
            for section in report_types():
                if re.sub(r'\s+', '', cells.get(f"A{section['rows'][0]}", '')) != section['label']:
                    raise ValueError('模板一级分类不同，不能自动填写')
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError) as exc:
        raise ValueError('报销模板不是有效的 XLSX 文件') from exc


def spreadsheet_runtime() -> tuple[Path, Path]:
    runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    node = Path(os.environ.get('INVOICE_REPORT_NODE', str(runtime / 'bin/node.exe')))
    modules = Path(os.environ.get('INVOICE_REPORT_NODE_MODULES', str(runtime / 'node_modules')))
    if not node.is_file() or not (modules / '@oai/artifact-tool').is_dir():
        raise ValueError('Excel运行环境未配置，请设置 INVOICE_REPORT_NODE 和 INVOICE_REPORT_NODE_MODULES')
    return node, modules


def create_excel(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path,
                 *, preview: Path | None = None, summary_pdf: Path | None = None) -> None:
    validate_template(template)
    node, modules = spreadsheet_runtime()
    if options['payment'] not in PAYMENTS:
        raise ValueError('当前报销表仅填写现场现金支付（需报销）列，请刷新工作台后重试')
    column, label = PAYMENTS[options['payment']]
    from invoice_print_layout.report_snapshot import snapshot
    report = snapshot(items)
    items = report['items']
    projects = list(dict.fromkeys(x['project'] for x in items))
    payload = {'template': str(template), 'output': str(output), 'items': items, 'snapshot': report,
               'project': projects[0] if len(projects) == 1 else '多个项目（详见明细）',
               'person': options['person'], 'period': options['period'],
               'payment_column': column, 'payment_label': label,
               'total_cents': report['total_cents'],
               'preview': str(preview) if preview else None,
               'summary_pdf': str(summary_pdf) if summary_pdf else None}
    source = output.with_suffix('.input.json')
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    try:
        result = subprocess.run([str(node), str(Path(__file__).with_name('report_xlsx.mjs')),
                                 str(source), str(modules)], capture_output=True, text=True,
                                encoding='utf-8', timeout=120,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('Excel制作失败：' + result.stderr[-1500:])
        if not output.is_file():
            raise ValueError('Excel未生成')
        _set_print_layout(output, len(items))
        if summary_pdf is not None:
            _export_summary_pdf(source)
            _fit_summary_pdf(summary_pdf, report['total_cents'])
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Excel制作超时，请减少本次事项数量后重试') from exc
    finally:
        source.unlink(missing_ok=True)


def _export_summary_pdf(payload: Path) -> None:
    """Render the existing template with the machine's offline native Excel."""
    script = Path(__file__).with_name('report_excel_pdf.ps1')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File', str(script),
                                 '-PayloadPath', str(payload.resolve())], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('本机Excel主表PDF导出未完成，请检查Excel是否可用后重试') from exc
    if result.returncode:
        raise ValueError('本机Excel主表PDF导出失败：' + result.stderr[-1200:])


def _fit_summary_pdf(path: Path, total_cents: int) -> None:
    """Retain the complete vector form using its existing fit-to-one-A4 rule."""
    import pymupdf
    from invoice_print_layout.layout import A4_HEIGHT, A4_WIDTH
    replacement = path.with_suffix('.a4.pdf')
    try:
        with pymupdf.open(path) as source:  # type: ignore[no-untyped-call]
            text = ''.join(source[0].get_text().split()) if len(source) else ''
            if (len(source) != 1 or '活动运营费用统计表' not in text or '费用合计' not in text or
                    f'{total_cents / 100:,.2f}'.replace(',', '') not in text.replace(',', '')):
                raise ValueError('报销主表PDF文字或金额不完整，未生成打印包')
            if (abs(source[0].rect.width - A4_WIDTH) < 1 and
                    abs(source[0].rect.height - A4_HEIGHT) < 1):
                return
            with pymupdf.open() as output:  # type: ignore[no-untyped-call]
                page = output.new_page(width=A4_WIDTH, height=A4_HEIGHT)
                page.show_pdf_page(pymupdf.Rect(12, 12, A4_WIDTH - 12, A4_HEIGHT - 12), source, 0)  # type: ignore[no-untyped-call]
                output.save(replacement, garbage=4, deflate=True)
        replacement.replace(path)
    finally:
        replacement.unlink(missing_ok=True)


def _set_print_layout(output: Path, count: int) -> None:
    """Fill print metadata omitted by artifact-tool; cell authoring stays in artifact-tool."""
    namespace = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    ns = {'s': namespace}
    def tag(name: str) -> str:
        return '{' + namespace + '}' + name
    replacement = output.with_suffix('.printing.xlsx')
    try:
        with zipfile.ZipFile(output) as source, zipfile.ZipFile(replacement, 'w', zipfile.ZIP_DEFLATED) as target:
            workbook = ET.fromstring(source.read('xl/workbook.xml'))
            sheets = workbook.findall('s:sheets/s:sheet', ns)
            names = ET.SubElement(workbook, tag('definedNames'))
            for index, sheet in enumerate(sheets[:2]):
                name = "'" + (sheet.get('name') or '').replace("'", "''") + "'"
                area = '$A$1:$M$36' if index == 0 else f'$A$1:$M${count+2}'
                ET.SubElement(names, tag('definedName'), {'name': '_xlnm.Print_Area', 'localSheetId': str(index)}).text = name + '!' + area
                if index == 1:
                    ET.SubElement(names, tag('definedName'), {'name': '_xlnm.Print_Titles', 'localSheetId': '1'}).text = name + '!$1:$1'
            for entry in source.infolist():
                payload = source.read(entry.filename)
                if entry.filename == 'xl/workbook.xml':
                    payload = ET.tostring(workbook, encoding='utf-8', xml_declaration=True)
                elif entry.filename in ('xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml'):
                    root = ET.fromstring(payload)
                    props = ET.Element(tag('sheetPr'))
                    ET.SubElement(props, tag('pageSetUpPr'), {'fitToPage': '1'})
                    root.insert(0, props)
                    main = entry.filename.endswith('sheet1.xml')
                    ET.SubElement(root, tag('pageSetup'), {'paperSize': '9', 'orientation': 'portrait' if main else 'landscape',
                                                         'fitToWidth': '1', 'fitToHeight': '1' if main else '0'})
                    payload = ET.tostring(root, encoding='utf-8', xml_declaration=True)
                target.writestr(entry, payload)
        replacement.replace(output)
    finally:
        replacement.unlink(missing_ok=True)


def install_template(root: Path, payload: bytes) -> None:
    if not payload or len(payload) > 5 * 1024 * 1024:
        raise ValueError('模板须为5MB以内的XLSX')
    path = root / (TEMPLATE_NAME + '.tmp')
    try:
        path.write_bytes(payload)
        validate_template(path)
        current = root / TEMPLATE_NAME
        if current.exists():
            shutil.copy2(current, root / '报销模板.previous.xlsx')
        path.replace(current)
    finally:
        path.unlink(missing_ok=True)
