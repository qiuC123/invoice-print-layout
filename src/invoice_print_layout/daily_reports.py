"""Private, project-scoped fixed PPTX templates and explicit photo-slot matching."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import subprocess
import threading
import uuid
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

from invoice_print_layout.reliability import read_json, save_json
from invoice_print_layout.site_photos import SitePhotos

NS = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
Renderer = Callable[[Path, Path], dict[str, Any]]
NATIVE_LOCK = threading.Lock()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def native_render(deck: Path, folder: Path) -> dict[str, Any]:
    if os.name != 'nt':
        raise ValueError('日报导出需要 Windows 与本机 PowerPoint')
    with NATIVE_LOCK:
        try:
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                 str(Path(__file__).with_name('daily_report_render.ps1')),
                 '-InputDeck', str(deck), '-EvidenceDir', str(folder)],
                capture_output=True, timeout=150, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('PowerPoint 检查未完成，请确认其可正常运行后重试') from exc
    receipt = folder / 'powerpoint.json'
    if result.returncode or not receipt.is_file():
        raise ValueError('PowerPoint 无法正常打开日报，已停止导出；请关闭其弹窗后重试')
    return dict(json.loads(receipt.read_text(encoding='utf-8-sig')))


def numeric_hours(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 10000:
            raise ValueError('累计工时需为0—10000之间的数字')
        return number
    except InvalidOperation as exc:
        raise ValueError('请填写此前已确认的累计工时') from exc


def number_text(value: Decimal) -> str:
    return format(value.quantize(Decimal('.01')), 'f').rstrip('0').rstrip('.') or '0'


def day_text(day: int) -> str:
    chars = '零一二三四五六七八九'
    if day < 10:
        return chars[day]
    return (chars[day // 10] if day >= 20 else '') + '十' + (chars[day % 10] if day % 10 else '')


def inputs(config: dict[str, Any], body: dict[str, Any]) -> dict[str, str]:
    try:
        day = date.fromisoformat(str(body.get('date', '')))
        first = date.fromisoformat(config['first_day'])
        ordinal = (day - first).days + 1
        if not 1 <= ordinal <= 99:
            raise ValueError('报告日期需在项目开始后的99天内')
        start, end = str(body.get('start', '')), str(body.get('end', ''))
        if not all(re.fullmatch(r'\d{2}:\d{2}', t) for t in (start, end)):
            raise ValueError('请填写开始和结束时间')
        duration = datetime.strptime(end, '%H:%M') - datetime.strptime(start, '%H:%M')
        if duration.total_seconds() <= 0:
            raise ValueError('结束时间需晚于开始时间；当前模板填写同一天的连续施工时段')
        prior = numeric_hours(body.get('prior_hours', ''))
        hours = Decimal(int(duration.total_seconds())) / Decimal(3600)
    except (KeyError, TypeError) as exc:
        raise ValueError('日报参数不完整') from exc
    tomorrow = day + timedelta(days=1)
    return {'date': day.isoformat(), 'month': f'{day.month:02}', 'day': f'{day.day:02}',
            'ordinal': day_text(ordinal), 'start': start, 'end': end,
            'start_hour': start[:2], 'start_minute': start[3:],
            'end_hour': end[:2], 'end_minute': end[3:],
            'minute_to_hour': start[3:] + '-' + end[:2],
            'hours': number_text(hours), 'prior_hours': number_text(prior),
            'cumulative': number_text(hours + prior),
            'next_month': f'{tomorrow.month:02}', 'next_day': f'{tomorrow.day:02}'}


def make_pptx(template: Path, config: dict[str, Any], values: dict[str, str],
              photos: list[dict[str, Any]], target: Path) -> None:
    """Edit original XML runs/relationships in place. Never clone shape-owned tags."""
    with zipfile.ZipFile(template) as archive:
        parts = {n: archive.read(n) for n in archive.namelist()}
    for part, changes in config['text_fields'].items():
        edits = {int(c['index']): c for c in changes}
        index = -1
        seen: set[int] = set()

        def replace(match: re.Match[str]) -> str:
            nonlocal index
            index += 1
            if index not in edits:
                return match[0]
            item = edits[index]
            if html.unescape(match[1]) != item['expected']:
                raise ValueError('模板文字锚点不一致，已停止修改')
            seen.add(index)
            return '<a:t>' + html.escape(values[item['field']]) + '</a:t>'

        parts[part] = re.sub(r'<a:t>(.*?)</a:t>', replace, parts[part].decode(), flags=re.S).encode()
        if seen != set(edits):
            raise ValueError('模板文字位置缺失')
    rel_part = config['photo_rel_part']
    relationships = parts[rel_part].decode()
    picture_xml = parts[config['photo_part']].decode()
    image_types = {e.get('Extension'): e.get('ContentType') for e in ET.fromstring(parts['[Content_Types].xml'])}
    for index, (slot, photo) in enumerate(zip(config['slots'], photos, strict=True)):
        path = Path(photo['path'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != photo['id']:
            raise ValueError('照片内容已变化，请重新配图')
        suffix = '.png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else '.jpeg' if raw.startswith(b'\xff\xd8\xff') else ''
        if not suffix or image_types.get(suffix[1:]) not in ('image/png', 'image/jpeg'):
            raise ValueError('此日报模板只接受PNG或JPEG照片')
        name = f'daily-report-{index + 1}{suffix}'
        parts['ppt/media/' + name] = raw
        pattern = r'<Relationship\b[^>]*\bId="' + re.escape(slot['relationship']) + r'"[^>]*/>'
        matches = list(re.finditer(pattern, relationships))
        if len(matches) != 1:
            raise ValueError('照片关系不唯一，已停止生成')
        match = matches[0]
        replacement = re.sub(r'Target="[^"]+"', f'Target="../media/{name}"', match[0])
        relationships = relationships[:match.start()] + replacement + relationships[match.end():]
        old = '<a:t>' + html.escape(slot['caption']) + '</a:t>'
        if picture_xml.count(old) != 1:
            raise ValueError('照片图注锚点不一致')
        label = photo['fields']['plate'] + ' · ' + ('工厂装货' if slot['place'] == '工厂' else '现场卸货')
        picture_xml = picture_xml.replace(old, '<a:t>' + html.escape(label) + '</a:t>')
    # Each shape-owned tags relationship may only belong to one shape.
    tags = ET.fromstring(picture_xml).findall('.//p:tags', NS)
    ids = [t.get('{' + NS['r'] + '}id') for t in tags]
    if len(ids) != len(set(ids)):
        raise ValueError('模板含重复图形标签，PowerPoint可能无法打开')
    parts[rel_part] = relationships.encode()
    parts[config['photo_part']] = picture_xml.encode()
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


class DailyReports:
    def __init__(self, workspace: Path, photos: SitePhotos, renderer: Renderer = native_render):
        self.workspace, self.photos, self.renderer = workspace, photos, renderer
        self.lock = threading.Lock()

    def root(self, pid: str) -> Path:
        self.photos.project(pid)
        return self.workspace / '现场进度' / pid / '日报'

    def config(self, pid: str) -> dict[str, Any]:
        folder = self.root(pid) / '模板'
        config = read_json(folder / 'template.json')
        if not config:
            raise ValueError('本项目尚未配置日报模板')
        template = folder / 'template.pptx'
        if not template.is_file() or digest(template) != config['sha256']:
            raise ValueError('日报模板校验不一致，请先恢复已确认的模板')
        return config

    def view(self, pid: str) -> dict[str, Any]:
        root = self.root(pid)
        if not (root / '模板/template.json').exists():
            return {'available': False, 'reports': []}
        config = self.config(pid)
        reports = []
        for path in (root / '结果').glob('*/receipt.json'):
            row = read_json(path)
            if row.get('state') == 'ready':
                reports.append({'id': path.parent.name, 'date': row['values']['date'],
                                'hours': row['values']['hours'], 'created': row['created'],
                                'preview': f'/daily-report/{pid}/{path.parent.name}/preview.html',
                                'download': f'/daily-report/{pid}/{path.parent.name}/report.pptx'})
        return {'available': True, 'name': config['name'], 'first_day': config['first_day'],
                'default_start': config['default_start'], 'default_end': config['default_end'],
                'fixed_content': config['fixed_content'], 'capacity': len(config['slots']) // 2,
                'reports': sorted(reports, key=lambda x: x['created'], reverse=True)[:20]}

    def match(self, pid: str, day: str) -> dict[str, Any]:
        config = self.config(pid)
        date.fromisoformat(day)
        rows = [p for p in self.photos.snapshot(pid)['photos'] if p['state'] == 'saved'
                and p['fields'].get('kind') == '车辆' and p['fields'].get('movement') == '进场'
                and p['fields'].get('trip_date') == day]
        plates = sorted({p['fields'].get('plate', '') for p in rows} - {''})
        capacity = len(config['slots']) // 2
        issues = []
        if len(plates) != capacity:
            issues.append(f'此模板需要{capacity}辆车，所选运输日期已归档{len(plates)}辆；请先补齐资料或调整模板。')
        slots = []
        for index, slot in enumerate(config['slots']):
            plate = plates[index // 2] if index // 2 < len(plates) else ''
            candidates = []
            for row in rows:
                fields = row['fields']
                if fields.get('plate') != plate or fields.get('place') != slot['place'] or '车头' not in fields.get('label', ''):
                    continue
                captured = fields.get('captured_date', '')
                if not captured or captured > day or (slot['place'] == '现场' and captured != day):
                    continue
                candidates.append({'id': row['id'], 'label': fields['label'], 'date': captured,
                                   'quality': row.get('quality', ''), 'revision': row['revision']})
            candidates.sort(key=lambda p: p['id'])
            slots.append({'key': str(index), 'plate': plate, 'place': slot['place'], 'candidates': candidates,
                          'selected': candidates[0]['id'] if len(candidates) == 1 else '',
                          'message': '缺少车头照片' if not candidates else '有多张候选，请选择' if len(candidates) > 1 else '已匹配'})
        return {'date': day, 'slots': slots, 'issues': issues,
                'scope': '仅按此项目已归档车辆核对；尚未收集的车辆无法自动发现。'}

    def generate(self, pid: str, body: dict[str, Any]) -> dict[str, Any]:
        if body.get('confirmed') is not True:
            raise ValueError('请确认固定施工内容与计划适用于这份日报')
        if not self.lock.acquire(blocking=False):
            raise ValueError('正在生成日报，请等待当前检查完成')
        folder: Path | None = None
        try:
            config = self.config(pid)
            values = inputs(config, body)
            match = self.match(pid, values['date'])
            if match['issues']:
                raise ValueError('；'.join(match['issues']))
            selections = body.get('selections', {})
            if not isinstance(selections, dict):
                raise ValueError('配图选择格式无效')
            selected = []
            for slot in match['slots']:
                key = selections.get(slot['key'], slot['selected'])
                if key not in {p['id'] for p in slot['candidates']}:
                    raise ValueError(slot['plate'] + ' ' + slot['place'] + '：请先补图或选择唯一车头照片')
                photo = self.photos.get(pid, key)
                path = self.photos.file(pid, key)
                if digest(path) != key:
                    raise ValueError('照片原件校验失败')
                selected.append({'id': key, 'path': str(path), 'fields': photo['fields'], 'revision': photo['revision']})
            key = uuid.uuid4().hex
            folder = self.root(pid) / '结果' / key
            folder.mkdir(parents=True)
            deck = folder / 'report.pptx'
            template = self.root(pid) / '模板/template.pptx'
            make_pptx(template, config, values, selected, deck)
            native = self.renderer(deck, folder)
            if not native.get('opened') or native.get('slides') != config['slide_count']:
                raise ValueError('PowerPoint页面检查未通过')
            if native['items'][config['photo_page'] - 1]['pictures'] != len(selected):
                raise ValueError('PowerPoint照片数量检查未通过，已停止导出')
            # Don't release an outdated assignment if the user reclassified photos while rendering.
            for row in selected:
                current = self.photos.get(pid, row['id'])
                if current['state'] != 'saved' or current['fields'] != row['fields'] or current['revision'] != row['revision']:
                    raise ValueError('生成期间照片分类发生变化，请重新配图')
            images = []
            for index in range(1, config['slide_count'] + 1):
                image = folder / f'slide-{index}.png'
                if not image.is_file():
                    raise ValueError('PowerPoint预览图片缺失，已停止导出')
                images.append('<figure><figcaption>第'+str(index)+'页</figcaption><img alt="日报第'+str(index)+
                              '页" src="data:image/png;base64,'+base64.b64encode(image.read_bytes()).decode()+'"></figure>')
            summary = html.escape(values['date'] + ' · ' + values['start']+'–'+values['end']+' · 当日 '+values['hours']+' 小时 · 累计 '+values['cumulative']+' 小时')
            preview = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>日报预览</title><style>body{margin:0;background:#181818;color:#eee;font:16px sans-serif}header,main{max-width:1100px;margin:auto;padding:20px}figure{margin:0 0 24px}img{width:100%}figcaption{padding:10px 0;color:#aaa}a{color:#d7e8ff}header{position:sticky;top:0;background:#181818}</style><header><h2>卸货日报预览</h2><p>'+summary+'</p><a href="report.pptx" download>下载 PPTX</a></header><main>'+''.join(images)+'</main></html>'
            (folder / 'preview.html').write_text(preview, encoding='utf-8')
            receipt = {'state': 'ready', 'project_id': pid, 'created': datetime.now().isoformat(),
                       'template_sha256': config['sha256'], 'values': values, 'photos': selected,
                       'files': {n: digest(folder / n) for n in ('report.pptx', 'preview.html')}, 'native': native}
            save_json(folder / 'receipt.json', receipt)
            return {'id': key, 'preview': f'/daily-report/{pid}/{key}/preview.html',
                    'download': f'/daily-report/{pid}/{key}/report.pptx', 'values': values}
        except Exception as exc:
            if folder:
                save_json(folder / 'receipt.json', {'state': 'failed', 'project_id': pid})
            if isinstance(exc, (ValueError, OSError)):
                raise
            raise ValueError('日报生成未完成，未开放导出，请检查模板和PowerPoint') from exc
        finally:
            self.lock.release()

    def file(self, pid: str, key: str, name: str) -> Path:
        root = self.root(pid).resolve()
        if not re.fullmatch(r'[0-9a-f]{32}', key) or name not in ('report.pptx', 'preview.html'):
            raise ValueError('无效日报文件')
        folder = (root / '结果' / key).resolve()
        if not folder.is_relative_to(root):
            raise ValueError('日报目录无效')
        receipt = read_json(folder / 'receipt.json')
        file = (folder / name).resolve()
        if not file.is_relative_to(folder) or receipt.get('state') != 'ready' or receipt.get('project_id') != pid:
            raise ValueError('日报尚未通过检查或不属于本项目')
        if not file.is_file() or digest(file) != receipt['files'].get(name):
            raise ValueError('日报文件校验不一致')
        return file
