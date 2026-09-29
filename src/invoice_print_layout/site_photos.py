"""Project photo intake, local OCR, review and byte-preserving folder export."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from PIL import Image

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.receipt_intake import read_receipt, rows_of, validate_image
from invoice_print_layout.reliability import read_json
from invoice_print_layout.photo_classifier import LABELS, accepted, classify, phase_for

SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}
PLATE = re.compile(r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}')


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def component(raw: Any, label: str) -> str:
    value = str(raw).strip()
    if not value or len(value) > 100 or re.search(r'[\\/:*?"<>|\x00-\x1f]', value) or value in {'.', '..'} or value.endswith(('.', ' ')):
        raise ValueError(f'{label}不能包含路径符号或为空')
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
        raise ValueError(f'{label}是系统保留名称')
    return value


def dated(value: Any) -> str:
    text = str(value)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        raise ValueError('请填写YYYY-MM-DD日期')
    return date.fromisoformat(text).isoformat()


def hints(text: str) -> dict[str, str]:
    compact = re.sub(r'\s+', '', text)
    fields: dict[str, str] = {}
    dates = set(re.findall(r'(20\d{2})[.年/-](\d{1,2})[.月/-](\d{1,2})', compact))
    valid: set[str] = set()
    for y, m, d in dates:
        try:
            valid.add(date(int(y), int(m), int(d)).isoformat())
        except ValueError:
            pass
    if len(valid) == 1:
        fields['captured_date'] = next(iter(valid))
    clock = re.search(r'(?:拍摄时间|时间).*?(\d{1,2})[:：](\d{2})', compact)
    if clock and int(clock[1]) < 24 and int(clock[2]) < 60:
        fields['captured_time'] = f'{int(clock[1]):02}:{clock[2]}'
    plates = set(PLATE.findall(compact.upper().replace('·', '').replace('•', '')))
    if len(plates) == 1:
        fields['plate'] = next(iter(plates))
        fields['kind'] = '车辆'
    location = re.search(r'地点[:：](.*?)(?:今日水印|防伪|验真|$)', compact)
    if location:
        fields['location'] = location[1][:160]
    project = re.search(r'施工(?:内容|区域)[:：](.*?)(?:拍摄时间|天气|地点|$)', compact)
    if project and not any(x in project[1] for x in ['请输入', '...']):
        fields['watermark_project'] = project[1][:100]
    # Place labels are suggestions only; never auto-admit by a city or a group.
    if any(x in fields.get('location', '') for x in ['博览', '展览', '会展', '庆典广场']):
        fields['place'] = '现场'
    return fields


class SitePhotos:
    def __init__(self, workspace: Path, logistics: LogisticsStore):
        self.workspace = workspace.resolve()
        self.root = self.workspace / '现场进度'
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'photos.sqlite3'
        self.logistics = logistics
        self.lock = threading.RLock()
        self.reader_lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS photos(project TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(project,id));
                CREATE TABLE IF NOT EXISTS settings(project TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(project TEXT, id TEXT PRIMARY KEY, data TEXT NOT NULL);
            ''')
            for row in db.execute('SELECT id,data FROM jobs').fetchall():
                job = json.loads(row['data'])
                if job['state'] == 'running':
                    job.update(state='interrupted', message='服务已重启；已读取照片保留，可重新读取去重。')
                    db.execute('UPDATE jobs SET data=? WHERE id=?', (json.dumps(job, ensure_ascii=False), row['id']))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def project(self, pid: str) -> dict[str, Any]:
        if not re.fullmatch(r'[0-9a-f]{32}', pid):
            raise ValueError('项目编号无效')
        for item in self.logistics.snapshot()['projects']:
            if item['id'] == pid:
                return dict(item)
        raise ValueError('项目不存在')

    def put(self, pid: str, row: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO photos VALUES (?,?,?)', (pid, row['id'], json.dumps(row, ensure_ascii=False)))

    def get(self, pid: str, key: str) -> dict[str, Any]:
        self.project(pid)
        with self.connect() as db:
            row = db.execute('SELECT data FROM photos WHERE project=? AND id=?', (pid, key)).fetchone()
        if row is None:
            raise ValueError('此项目没有该照片')
        return dict(json.loads(row[0]))

    def settings(self, pid: str) -> dict[str, Any]:
        self.project(pid)
        with self.connect() as db:
            row = db.execute('SELECT data FROM settings WHERE project=?', (pid,)).fetchone()
        if row:
            return dict(json.loads(row[0]))
        scope = read_json(self.root / pid / '采集范围.json')
        groups = []
        if scope.get('chat_id'):
            groups.append({'id': scope['chat_id'], 'name': scope.get('group_name', scope['chat_id'])})
        factory = scope.get('factory_source_group', {})
        if factory.get('chat_id'):
            groups.append({'id': factory['chat_id'], 'name': factory['name']})
        return {'output_root': scope.get('user_photo_directory', str(self.root / pid / '分类照片')),
                'groups': groups, 'account': scope.get('account_directory', ''), 'migration_done': False,
                'auto_classify': False, 'project_location': ''}

    def save_settings(self, pid: str, values: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = self.settings(pid)
            path = Path(str(values.get('output_root', settings['output_root']))).expanduser()
            if not path.is_absolute() or path.resolve() == Path(path.anchor) or path.is_file():
                raise ValueError('请填写项目专用的绝对文件夹路径，不能使用磁盘根目录')
            groups = values.get('groups', settings['groups'])
            if not isinstance(groups, list) or len(groups) > 20:
                raise ValueError('群列表无效')
            normalized: list[dict[str, str]] = []
            for group in groups:
                gid = str(group['id']).strip()
                if not re.fullmatch(r'\d+@chatroom', gid) or any(x['id'] == gid for x in normalized):
                    raise ValueError('请填写不重复的稳定群ID，例如123@chatroom')
                normalized.append({'id': gid, 'name': str(group['name']).strip()[:120] or gid})
            settings.update(output_root=str(path.resolve()), groups=normalized)
            account = str(values.get('account', settings['account'])).strip()
            if account and not re.fullmatch(r'wxid_[A-Za-z0-9_]+', account):
                raise ValueError('微信账号目录标识无效')
            settings['account'] = account
            if 'auto_classify' in values:
                if type(values['auto_classify']) is not bool:
                    raise ValueError('自动分类设置无效')
                settings['auto_classify'] = values['auto_classify']
            settings['project_location'] = str(values.get('project_location', settings.get('project_location', ''))).strip()[:100]
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (pid, json.dumps(settings, ensure_ascii=False)))
            return settings

    def receive(self, pid: str, name: str, payload: bytes, source: dict[str, Any] | None = None,
                quality: str = '用户导入，原图状态未核实', *, automatic: bool = True) -> dict[str, Any]:
        row = self._receive(pid, name, payload, source, quality)
        if automatic and not row.get('duplicate') and self.settings(pid).get('auto_classify'):
            return self.process(pid, row['id'])
        return row

    def _receive(self, pid: str, name: str, payload: bytes, source: dict[str, Any] | None,
                 quality: str) -> dict[str, Any]:
        self.project(pid)
        suffix = validate_image(name, payload)
        digest = hashlib.sha256(payload).hexdigest()
        with self.lock:
            with self.connect() as db:
                found = db.execute('SELECT data FROM photos WHERE project=? AND id=?', (pid, digest)).fetchone()
            provenance = source or {'type': '上传', 'name': Path(name).name}
            if found:
                row = dict(json.loads(found[0]))
                if provenance not in row['sources']:
                    row['sources'].append(provenance)
                    self.put(pid, row)
                return {**row, 'duplicate': True}
            folder = self.root / pid / '照片原件'
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / (digest + suffix)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('原件校验不一致')
            if not target.exists():
                target.write_bytes(payload)
            with Image.open(io.BytesIO(payload)) as im:
                dimensions = [im.width, im.height]
            row = {'id': digest, 'name': Path(name.replace('\\', '/')).name, 'path': str(target),
                   'dimensions': dimensions, 'quality': quality, 'sources': [provenance],
                   'fields': {}, 'suggestions': {}, 'ocr_text': '', 'warnings': [],
                   'state': 'pending', 'revision': 1, 'created_at': stamp(), 'saved_path': '', 'saved_root': ''}
            self.put(pid, row)
            return row

    def migrate(self, pid: str) -> None:
        with self.lock:
            settings = self.settings(pid)
            if settings.get('migration_done'):
                return
            folder = self.root / pid
            legacy = read_json(folder / '照片台账.json')
            if legacy and legacy.get('project_id') != pid:
                raise ValueError('旧照片台账项目不一致')
            entries = list(legacy.get('photos', []))
            people = read_json(folder / '来源证据' / '人员照片分类-2026-09-26.json')
            if people.get('project_id') == pid:
                entries.extend(people.get('records', []))
            for item in entries:
                relative = item.get('relative_path', item.get('archive_path', ''))
                if not relative:
                    continue
                file = (folder / relative).resolve()
                if not file.is_relative_to(folder.resolve()) or not file.is_file():
                    continue
                payload = file.read_bytes()
                expected = item.get('preview_sha256', item.get('sha256'))
                if hashlib.sha256(payload).hexdigest() != expected:
                    raise ValueError('旧照片台账文件校验不一致')
                row = self.receive(pid, file.name, payload, {'type': '既有归档', 'file': relative,
                    'chat_id': item.get('source_chat_id', ''), 'message_id': item.get('source_message_id', '')},
                    item.get('format_note', item.get('quality', '原图状态未核实')), automatic=False)
                if row.get('duplicate'):
                    continue
                captured = item.get('captured_at_claim', '')
                row['fields'] = {'kind': '人员' if item.get('category') == '人员/进场' else '车辆',
                    'movement': '进场', 'place': item.get('location_category', '现场'),
                    'captured_date': captured[:10], 'captured_time': captured[11:16],
                    'trip_date': item.get('arrival_date', captured[:10]), 'plate': item.get('vehicle_plate', ''),
                    'location': item.get('watermark_location', ''), 'watermark_project': item.get('watermark_project') or '',
                    'note': item.get('observation', ''), 'label': Path(item.get('user_photo_path', file.name)).stem,
                    'person_name': '', 'person_role': '', 'visible_count': str(item.get('visible_subject_count', ''))}
                destination = Path(item.get('user_photo_path', ''))
                output = Path(settings['output_root']).resolve()
                if destination.is_file() and destination.resolve().is_relative_to(output) and hashlib.sha256(destination.read_bytes()).hexdigest() == row['id']:
                    row.update(state='saved', saved_path=str(destination.resolve()), saved_root=str(output))
                row['reviewed'] = True
                self.put(pid, row)
            settings['migration_done'] = True
            with self.connect() as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (pid, json.dumps(settings, ensure_ascii=False)))

    def snapshot(self, pid: str) -> dict[str, Any]:
        self.migrate(pid)
        with self.connect() as db:
            photos = [dict(json.loads(r[0])) for r in db.execute('SELECT data FROM photos WHERE project=?', (pid,))]
            jobs = [dict(json.loads(r[0])) for r in db.execute('SELECT data FROM jobs WHERE project=? ORDER BY rowid DESC LIMIT 10', (pid,))]
        public = [{k: v for k, v in p.items() if k != 'path'} for p in photos]
        project = self.project(pid)
        for photo in public:
            photo['phase'] = phase_for(project, photo['fields'])
        public.sort(key=lambda p: (p['fields'].get('captured_date', ''), p['created_at']), reverse=True)
        reader = read_json(self.workspace / 'photo-reader.json')
        return {'project': self.project(pid), 'settings': self.settings(pid), 'photos': public, 'jobs': jobs,
                'reader_ready': bool(reader.get('python') and reader.get('helper_dir') and reader.get('audited_source'))}

    def file(self, pid: str, key: str) -> Path:
        row = self.get(pid, key)
        path = Path(row['path']).resolve()
        if not path.is_relative_to((self.root / pid / '照片原件').resolve()) or not path.is_file():
            raise ValueError('原件不可用')
        return path

    def analyze(self, pid: str, key: str) -> dict[str, Any]:
        row = self.get(pid, key)
        file = self.file(pid, key)
        with self.ocr_lock:
            text = '\n'.join(rows_of(read_receipt(file.read_bytes(), file.suffix)))
        suggestion = hints(text)
        warnings = ['OCR为候选值；保存前核对原图、项目及分类。姓名、岗位和完整出勤时长不自动推断。']
        settings = self.settings(pid)
        project = self.project(pid)
        decision: dict[str, Any] = {}
        phase = phase_for(project, suggestion)
        if settings.get('auto_classify') and not row.get('reviewed'):
            # Only textual facts go to Jev; local paths, image bytes and account IDs stay local.
            with self.connect() as db:
                known = [json.loads(r[0]) for r in db.execute('SELECT data FROM photos WHERE project=?', (pid,))]
            vehicles = [p['fields'] for p in known if p.get('reviewed') and p['fields'].get('plate') == suggestion.get('plate')]
            evidence = {'project': project['name'], 'project_location': settings.get('project_location', ''),
                'confirmed_project_names': sorted({p['fields'].get('watermark_project', '') for p in known
                    if p.get('reviewed') and p.get('saved_by') != 'automatic' and p['fields'].get('watermark_project')})[:20],
                'ocr_text': text[:10000], 'extracted': {k: v for k, v in suggestion.items() if k != 'kind'},
                'source_groups': list(dict.fromkeys(s.get('group_name', '') for s in row['sources'] if s.get('group_name'))),
                'known_vehicle_records': [{k: v.get(k, '') for k in ['plate', 'place', 'location', 'trip_date', 'movement']} for v in vehicles[:20]]}
            warnings = []
            suggestion.pop('kind', None)
            suggestion.pop('place', None)
            try:
                if not text.strip():
                    raise ValueError('OCR未提取到文字，请核对原图或补充分类信息。')
                decision = classify(evidence)
                for field in LABELS:
                    value = LABELS[field].get(accepted(decision, field))
                    if value:
                        suggestion[field] = value
                if accepted(decision, 'project') != 'belongs':
                    warnings.append('Jev未确认属于当前项目；可能是其他项目或文字依据不足。')
            except ValueError as exc:
                warnings.append(str(exc))
            if phase.get('movement'):
                suggestion['movement'] = phase['movement']
            else:
                warnings.append(phase['reason'])
            if suggestion.get('kind') == '车辆':
                if not suggestion.get('plate'):
                    warnings.append('没有唯一车牌，请核对。')
                if not suggestion.get('place'):
                    warnings.append('工厂或现场尚未确定。')
                if suggestion.get('place') == '现场':
                    suggestion['trip_date'] = suggestion.get('captured_date', '')
                elif suggestion.get('place') == '工厂':
                    day = suggestion.get('captured_date', '')
                    trips = {v.get('trip_date', '') for v in vehicles if v.get('movement') == suggestion.get('movement')
                             and day and v.get('trip_date', '') >= day
                             and (date.fromisoformat(v['trip_date']) - date.fromisoformat(day)).days <= 31}
                    if len(trips) == 1:
                        suggestion['trip_date'] = next(iter(trips))
                    else:
                        warnings.append('工厂照片对应的运输批次日期不唯一或未知，请填写到场日期。')
        if not suggestion.get('captured_date'):
            warnings.append('未识别到唯一拍摄日期，请填写水印日期。')
        if not suggestion.get('kind'):
            warnings.append('文字不足以确定照片类别，请补充分类。')
        with self.lock:
            current = self.get(pid, key)
            if current['revision'] != row['revision']:
                raise ValueError('识别期间照片已被修改，保留最新结果，请刷新。')
            if self.settings(pid) != settings or self.project(pid) != project:
                raise ValueError('识别期间项目设置已变化，未自动保存，请重新识别。')
            current.update(ocr_text=text, suggestions=suggestion, warnings=warnings,
                           recognized_at=stamp(), revision=current['revision'] + 1)
            # Recognizing never overwrites reviewed fields or changes saved folders.
            if not current.get('reviewed'):
                current['fields'] = {**current['fields'], **suggestion}
                current['phase'] = phase
                current['classification'] = decision
            self.put(pid, current)
            if settings.get('auto_classify') and not current.get('reviewed') and not warnings:
                current = self.save(pid, key, current['revision'], current['fields'], True, automatic=True)
        return current

    def process(self, pid: str, key: str) -> dict[str, Any]:
        """Failure after intake leaves a visible, retryable record and original bytes."""
        try:
            return self.analyze(pid, key)
        except Exception as exc:
            with self.lock:
                row = self.get(pid, key)
                if not row.get('reviewed'):
                    message = str(exc) if isinstance(exc, ValueError) else '识别或保存未完成，原照片已保留；请重试。'
                    row.update(warnings=[message[:500]], revision=row['revision'] + 1)
                    self.put(pid, row)
                return row

    def save(self, pid: str, key: str, revision: int, fields: dict[str, Any], confirmed: bool, *, automatic: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError('请核对照片属于当前项目')
        with self.lock:
            row = self.get(pid, key)
            if row['revision'] != revision:
                raise ValueError('照片已被更新，请刷新后再保存')
            if fields.get('kind') not in {'车辆', '人员', '其他'} or fields.get('movement') not in {'进场', '展期', '撤场'}:
                raise ValueError('请选择照片类别及阶段')
            cleaned = {k: str(fields.get(k, '')).strip()[:500] for k in ['kind', 'movement', 'place', 'captured_date',
                'captured_time', 'trip_date', 'plate', 'location', 'watermark_project', 'note', 'label', 'person_name', 'person_role', 'visible_count']}
            day = dated(cleaned['captured_date'])
            parts = [cleaned['kind'], cleaned['movement']]
            if cleaned['kind'] == '车辆':
                plate = component(cleaned['plate'].upper(), '车牌号')
                trip = dated(cleaned['trip_date'] or day)
                if cleaned['place'] not in {'工厂', '现场'}:
                    raise ValueError('请选择工厂或现场')
                cleaned['plate'], cleaned['trip_date'] = plate, trip
                parts.extend([plate + '-' + trip, cleaned['place']])
            else:
                parts.append(day)
            label = component(cleaned['label'] or cleaned['kind'] + '照片', '照片名称')
            output = Path(self.settings(pid)['output_root']).resolve()
            target = output.joinpath(*parts, f'{day}-{label}-{key[:10]}{Path(row["path"]).suffix}').resolve()
            if not target.is_relative_to(output):
                raise ValueError('保存路径超出项目目录')
            # An unchanged save retains a migrated human-readable filename.
            same_fields = row.get('reviewed') and all(str(row['fields'].get(k, '')) == v for k, v in cleaned.items())
            if same_fields and row['saved_path'] and row['saved_root'] == str(output):
                target = Path(row['saved_path']).resolve()
                if not target.is_relative_to(output):
                    raise ValueError('旧保存路径超出项目目录')
            source = self.file(pid, key)
            payload = source.read_bytes()
            if hashlib.sha256(payload).hexdigest() != key:
                raise ValueError('照片原件校验失败')
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != key:
                raise ValueError('目标文件已存在且内容不同；未覆盖')
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(payload)
            old = Path(row['saved_path']).resolve() if row['saved_path'] else None
            old_root = Path(row['saved_root']).resolve() if row['saved_root'] else None
            row.setdefault('history', []).append({'at': stamp(), 'fields': row['fields'], 'saved_path': row['saved_path']})
            row.update(fields=cleaned, state='saved', reviewed=True, saved_path=str(target), saved_root=str(output), revision=revision + 1)
            row.update(saved_by='automatic' if automatic else 'manual', phase=phase_for(self.project(pid), cleaned))
            self.put(pid, row)
            # Remove only our unchanged old classified copy, after the replacement is saved.
            if old and old != target and old_root and old.is_relative_to(old_root) and old.is_file() and hashlib.sha256(old.read_bytes()).hexdigest() == key:
                try:
                    old.unlink()
                except OSError:
                    row['warnings'] = [*row['warnings'], '新分类已保存，旧目录副本未能移除。']
                    self.put(pid, row)
            return row

    def update_job(self, job: dict[str, Any], **values: Any) -> None:
        job.update(values, updated_at=stamp())
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?)', (job['project_id'], job['id'], json.dumps(job, ensure_ascii=False)))

    def start(self, pid: str, action: str, body: dict[str, Any], *, job_id: str = '') -> dict[str, Any]:
        self.project(pid)
        if action not in {'folder', 'recognize', 'wechat'}:
            raise ValueError('照片操作无效')
        with self.lock:
            if job_id:
                existing = self.get_job(pid, job_id)
                if existing:
                    return existing
            with self.connect() as db:
                active = [json.loads(r[0]) for r in db.execute('SELECT data FROM jobs WHERE project=?', (pid,))]
            if any(j['state'] == 'running' for j in active):
                raise ValueError('本项目已有读取或识别任务，请等待完成')
            job = {'id': job_id or uuid.uuid4().hex, 'project_id': pid, 'action': action, 'state': 'running',
                   'message': '准备处理', 'done': 0, 'total': 0, 'added': 0, 'duplicates': 0, 'issues': [],
                   'photo_ids': [], 'added_ids': []}
            self.update_job(job)
            thread = threading.Thread(target=self._run, args=(job, body), daemon=True)
            thread.start()
            return dict(job)

    def get_job(self, pid: str, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute('SELECT data FROM jobs WHERE project=? AND id=?', (pid, job_id)).fetchone()
        return dict(json.loads(row[0])) if row else None

    def _run(self, job: dict[str, Any], body: dict[str, Any]) -> None:
        try:
            if job['action'] == 'folder':
                self.read_folder(job, str(body.get('path', '')))
            elif job['action'] == 'wechat':
                groups = body.get('group_ids', [body.get('group_id')])
                allowed = {g['id'] for g in self.settings(job['project_id'])['groups']}
                if not isinstance(groups, list) or not groups or any(not isinstance(g, str) or g not in allowed for g in groups):
                    raise ValueError('请选择此项目已配置的照片来源群')
                processed, total, failed_groups = 0, 0, 0
                for group_id in dict.fromkeys(groups):
                    job.update(done=0, total=0)
                    try:
                        self.read_wechat(job, {**body, 'group_id': group_id})
                    except (ValueError, OSError) as exc:
                        failed_groups += 1
                        job['issues'].append({'item': group_id, 'message': str(exc)[:300]})
                        if len(groups) == 1:
                            raise
                    processed += job['done']
                    total += job['total']
                job.update(done=processed, total=total)
                if failed_groups == len(set(groups)):
                    raise ValueError('所有来源群均未能完成读取，请查看未完成事项并重试。')
            else:
                ids = body.get('ids', [])
                if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or len(set(ids)) != len(ids):
                    raise ValueError('请选择1—500张不重复的照片')
                self.each(job, ids, lambda key: self.process(job['project_id'], key))
            self.update_job(job, state='partial' if job['issues'] else 'done', message='处理完成，有待核对项' if job['issues'] else '处理完成')
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else '处理未完成；已读取照片保留，可重试'
            self.update_job(job, state='failed', message=message[:500])

    def each(self, job: dict[str, Any], values: list[Any], process: Callable[[Any], Any]) -> None:
        self.update_job(job, total=len(values))
        for i, value in enumerate(values):
            try:
                result = process(value)
                if isinstance(result, dict) and result.get('id'):
                    if result['id'] not in job.setdefault('photo_ids', []):
                        job['photo_ids'].append(result['id'])
                    if not result.get('duplicate') and job.get('action') != 'recognize' and result['id'] not in job.setdefault('added_ids', []):
                        job['added_ids'].append(result['id'])
                if (isinstance(result, dict) and result.get('state') == 'pending' and result.get('warnings')
                        and self.settings(job['project_id']).get('auto_classify')):
                    job['issues'].append({'item': result.get('name', str(value)), 'message': '；'.join(result['warnings'])[:500]})
            except (ValueError, OSError) as exc:
                job['issues'].append({'item': str(value)[:200], 'message': str(exc)[:300]})
            self.update_job(job, done=i + 1, message=f'已处理 {i + 1}/{len(values)}')

    def read_folder(self, job: dict[str, Any], path: str) -> None:
        folder = Path(path).expanduser()
        if not path or not folder.is_absolute() or not folder.is_dir():
            raise ValueError('请填写存在的本地照片文件夹绝对路径')
        folder = folder.resolve()
        if folder == Path(folder.anchor):
            raise ValueError('请选择照片文件夹，不能读取整个磁盘')
        files = []
        for current, dirs, names in os.walk(folder, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink() and (Path(current) / d).resolve().is_relative_to(folder)]
            for name in names:
                file = Path(current) / name
                if file.suffix.lower() in SUFFIXES and not file.is_symlink() and file.resolve().is_relative_to(folder):
                    files.append(file)
                    if len(files) > 500:
                        raise ValueError('单次最多500张，请选择更小的子文件夹')
        def ingest(file: Path) -> dict[str, Any]:
            if file.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('单张照片不能超过20MB')
            row = self.receive(job['project_id'], file.name, file.read_bytes(), {'type': '文件夹', 'path': str(file), 'relative': file.relative_to(folder).as_posix()})
            job['duplicates' if row.get('duplicate') else 'added'] += 1
            return row
        self.each(job, sorted(files), ingest)

    def read_wechat(self, job: dict[str, Any], body: dict[str, Any]) -> None:
        settings = self.settings(job['project_id'])
        group = next((g for g in settings['groups'] if g['id'] == body.get('group_id')), None)
        if not group or not settings['account']:
            raise ValueError('请先配置此项目的微信账号及来源群')
        start, end = dated(body.get('start', '')), dated(body.get('end', ''))
        if not 0 <= (date.fromisoformat(end) - date.fromisoformat(start)).days < 31:
            raise ValueError('单次读取日期范围必须为1—31天')
        config = read_json(self.workspace / 'photo-reader.json')
        if not all(config.get(k) for k in ['python', 'helper_dir', 'audited_source', 'helper_hashes', 'media_sha256']):
            raise ValueError('本机微信照片读取器尚未配置；可先上传或读取文件夹')
        # Each source needs its own request/result/media directory within the run.
        folder = self.root / job['project_id'] / '读取批次' / job['id'] / group['id']
        folder.mkdir(parents=True)
        request = {**config, 'account': settings['account'], 'group_id': group['id'], 'start': start, 'end': end}
        request_file = folder / 'request.json'
        request_file.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        bridge = Path(__file__).with_name('photo_wechat_bridge.py')
        self.update_job(job, message='正在读取指定群的本机图片记录…')
        with self.reader_lock:
            result = subprocess.run([config['python'], '-X', 'utf8', str(bridge), str(request_file)],
                                    capture_output=True, timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        report = read_json(folder / 'result.json')
        if result.returncode != 0 or report.get('state') == 'failed':
            raise ValueError(report.get('error', '微信读取未完成，请确认本机微信登录状态后重试'))
        job['issues'].extend(report.get('issues', []))
        def ingest(item: dict[str, Any]) -> dict[str, Any]:
            path = (folder / item['file']).resolve()
            if not path.is_relative_to(folder.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('读取结果路径或校验不一致')
            row = self.receive(job['project_id'], path.name, path.read_bytes(),
                {'type': '微信群', 'group_name': group['name'], 'account': settings['account'], **item['source']}, item['quality'])
            job['duplicates' if row.get('duplicate') else 'added'] += 1
            return row
        self.each(job, report.get('photos', []), ingest)
