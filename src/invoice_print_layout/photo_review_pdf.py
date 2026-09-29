"""Local, three-column photo review booklets from a frozen collection snapshot."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps
import pymupdf

MAX_PHOTOS_PER_FILE = 24


def category(row: dict[str, Any]) -> str:
    fields = row.get('fields', {})
    if fields.get('kind') == '车辆':
        return '工厂装货' if fields.get('place') == '工厂' else '现场车辆' if fields.get('place') == '现场' else '车辆 / 位置待核对'
    return '人员到场' if fields.get('kind') == '人员' else str(fields.get('kind') or '其他照片')


def quality_label(raw: str) -> str:
    if raw.startswith('微信_h原图版本'):
        return '微信原图版本'
    if raw.startswith('用户'):
        return '用户提供 / 原图状态未确认'
    if '缓存' in raw or '显示副本' in raw:
        return '缓存显示副本 / 未取得原图'
    return '原图状态未确认'


def build_reviews(folder: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Never edit archive files; split large batches before the Feishu upload limit."""
    folder.mkdir(parents=True, exist_ok=True)
    rank = {'工厂装货': 0, '现场车辆': 1, '人员到场': 2}
    rows = sorted(payload['rows'], key=lambda r: (rank.get(category(r), 3), category(r), r['fields'].get('plate', ''),
                  r['fields'].get('captured_date', ''), r['fields'].get('captured_time', ''), r['fields'].get('label', ''), r['id']))
    for index, row in enumerate(rows, 1):
        row = dict(row)
        row['number'] = f'{index:02d}'
        rows[index - 1] = row
    batches = [rows[i:i + MAX_PHOTOS_PER_FILE] for i in range(0, len(rows), MAX_PHOTOS_PER_FILE)] or [[]]
    font_file = Path('C:/Windows/Fonts/msyh.ttc')
    font = pymupdf.Font(fontfile=str(font_file)) if font_file.is_file() else pymupdf.Font('china-s')
    results: list[dict[str, Any]] = []
    for part, batch in enumerate(batches, 1):
        doc = pymupdf.open()
        categories = list(dict.fromkeys(category(r) for r in batch))
        sheets = [(name, group[i:i+6]) for name in categories
                  for group in [[r for r in batch if category(r) == name]] for i in range(0, len(group), 6)] or [('本轮处理结果', [])]
        for page_no, (name, pictures) in enumerate(sheets, 1):
            page = doc.new_page(width=842, height=595)
            page.insert_font(fontname='review', fontbuffer=font.buffer)

            def line(x: float, y: float, value: Any, size: float = 9, width: float = 786,
                     color: tuple[float, float, float] = (.12, .16, .21)) -> None:
                value = ' '.join(str(value).split())
                while value and font.text_length(value, fontsize=size) > width:
                    value = value[:-2] + '…' if len(value) > 2 else ''
                page.insert_text((x, y), value, fontsize=size, fontname='review', color=color)

            blue = (.13, .35, .83)
            line(28, 34, payload['project']['name'], 20, 550)
            line(630, 33, f'照片审阅 / {part}-{len(batches)}', 10, 184)
            state_name = {'done': '处理完成', 'partial': '部分完成', 'failed': '读取失败', 'interrupted': '读取中断'}
            mode = '现有归档试用汇总' if payload.get('preview') else state_name.get(payload.get('run_state', ''), '待核对') + ' / ' + payload['day']
            job = payload.get('job', {})
            summary = (f'{mode} · 附件照片 {len(rows)} 张 · 待核对 {payload.get("pending", 0)} 张'
                       f' · 本次新增 {job.get("added", 0)} · 重复 {job.get("duplicates", 0)} · 未完成 {len(job.get("issues", []))} 项')
            if payload.get('preview'):
                summary = f'{mode} · 附件照片 {len(rows)} 张 · 待核对 {payload.get("pending", 0)} 张 · 并非当天新增'
            line(28, 55, summary, 9)
            page.draw_rect(pymupdf.Rect(28, 66, 814, 104), color=None, fill=(.945, .96, .98))
            line(40, 81, f'{name} · 本页 {len(pictures)} 张', 11, 480, blue)
            sources = set()
            for row in pictures:
                for source in row.get('sources', []):
                    group = source.get('group_name') or payload['groups'].get(source.get('chat_id', ''))
                    if group:
                        sources.add(group)
            line(40, 97, '来源：' + ('；'.join(sorted(sources)) or '用户提供 / 群消息未关联'), 8, 756)
            for i, row in enumerate(pictures):
                x, top = 28 + (i % 3) * 266, 116 + (i // 3) * 222
                data = Path(row['path']).read_bytes()
                if hashlib.sha256(data).hexdigest() != row['id']:
                    raise ValueError('照片原件校验不一致，PDF未发送')
                with Image.open(io.BytesIO(data)) as raw:
                    im = ImageOps.exif_transpose(raw).convert('RGB')
                    im.thumbnail((1800, 1800))
                    stream = io.BytesIO()
                    im.save(stream, format='JPEG', quality=88, optimize=True)
                page.insert_image(pymupdf.Rect(x, top, x+254, top+165), stream=stream.getvalue(), keep_proportion=True)
                fields = row['fields']
                label = fields.get('label') or category(row)
                if fields.get('plate'):
                    label = fields['plate'] + ' · ' + label.split('-')[-1]
                line(x, top+180, row['number']+'  '+label, 10, 254)
                line(x, top+194, (fields.get('captured_date') or '拍摄日期待核对')+' '+fields.get('captured_time', ''), 9, 254)
                line(x, top+208, quality_label(row.get('quality', '')), 8, 254, blue)
            if not pictures:
                line(40, 160, '本轮没有可纳入审阅册的已归档照片。', 14)
                line(40, 190, '读取结果：' + state_name.get(payload.get('run_state', ''), '待核对'), 11)
                line(40, 215, '零照片不代表群内没有图片；请核对缓存下载情况及工作台处理记录。', 10)
                if payload.get('pending'):
                    line(40, 240, '归属或分类待确认的照片未上传到飞书，请先在工作台核对。', 10)
            page.draw_line((28, 565), (814, 565), color=(.86, .89, .92), width=.5)
            line(28, 581, '本轮归档快照 · 完整水印 · PDF显示压缩，源文件不改 · 照片不代表工序完成或出勤人数', 7, 718)
            line(758, 581, f'{page_no}/{len(sheets)}', 8, 56)
        name = f'照片审阅-{payload["day"]}-{part:02d}.pdf'
        target = folder / name
        temp = target.with_suffix('.tmp')
        doc.subset_fonts()
        doc.save(str(temp), garbage=4, deflate=True)
        doc.close()
        if temp.stat().st_size >= 29 * 1024 * 1024:
            temp.unlink()
            raise ValueError('审阅册超出发送大小限制，请缩小读取范围后重试')
        temp.replace(target)
        results.append({'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                        'count': len(batch), 'pages': len(sheets), 'file_key': '', 'message_id': '',
                        'photo_ids': [r['id'] for r in batch]})
    return results
