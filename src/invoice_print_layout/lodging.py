"""Owner-only lodging drafts and recoverable, explicit submissions."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Protocol

TZ = timezone(timedelta(hours=8))
FIELDS = {'日期': '起算日期', '晚数': '入住晚数', '单价': '每晚单价', '房号': '房号（选填）'}
HELP = ('住宿指令：\n住宿 新开 日期=2026-09-25；晚数=2；单价=188；房号=待定\n'
        '住宿 续住 记录=rec...；日期=2026-09-27；晚数=2；单价=188\n'
        '住宿 试算 晚数=2；单价=188\n住宿 修改 记录=rec...；房号=306\n'
        '住宿 查询 记录=rec...\n住宿 补充 日期=2026-09-25；晚数=2；单价=188\n'
        '住宿 提交 / 住宿 取消\n房号选填；日期必须明确。先预览，再提交。')


class LodgingError(Exception):
    pass


class LodgingBackend(Protocol):
    def get(self, record_id: str) -> dict[str, Any]: ...
    def find(self, marker: str) -> list[dict[str, Any]]: ...
    def create(self, fields: dict[str, Any], token: str) -> str: ...
    def update(self, record_id: str, fields: dict[str, Any]) -> None: ...


def scalar(value: Any) -> Any:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return ''.join(str(v.get('text', '')) for v in value)
    return value


def parse_fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in re.split(r'[；;\n]+', text.strip()):
        if not part.strip():
            continue
        match = re.fullmatch(r'\s*(日期|晚数|单价|房号|记录|酒店|项目|人员)\s*[=:：]\s*(.*?)\s*', part)
        if not match or not match[2]:
            raise LodgingError('字段格式不明确，请按“日期=2026-09-25；晚数=2；单价=188”填写。')
        if match[1] in result:
            raise LodgingError('同一字段出现多次，请只保留最终值。')
        if len(match[2]) > 200:
            raise LodgingError('单个字段过长。')
        result[match[1]] = match[2]
    return result


def checked(fields: dict[str, str], *, quote: bool = False) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {}
    for key, value in fields.items():
        if key == '日期':
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError
                day = date.fromisoformat(value)
                if not date(2000, 1, 2) <= day <= date(2100, 12, 1):
                    raise ValueError
                result[FIELDS[key]] = int(datetime.combine(day, time(), TZ).timestamp()*1000)
            except ValueError as exc:
                raise LodgingError('日期请使用明确的YYYY-MM-DD格式（2000—2100年）。') from exc
        elif key == '晚数':
            if not re.fullmatch(r'[1-9]\d{0,2}', value):
                raise LodgingError('晚数须为1—999的整数。')
            result[FIELDS[key]] = int(value)
        elif key == '单价':
            if not re.fullmatch(r'\d+(?:\.\d{1,2})?', value):
                raise LodgingError('单价须为正数，最多两位小数。')
            price = Decimal(value)
            if not 0 < price <= 1_000_000:
                raise LodgingError('单价超出范围。')
            result[FIELDS[key]] = float(price)
        elif key == '房号':
            result[FIELDS[key]] = '' if value in {'待定', '未知', '留空'} else value
        elif key == '记录' and not re.fullmatch(r'rec[a-zA-Z0-9]+', value):
            raise LodgingError('记录请填写完整的rec开头记录ID，不能只用房号定位。')
    required = ['晚数', '单价'] if quote else ['日期', '晚数', '单价']
    return result, [k for k in required if k not in fields]


def describe(fields: dict[str, Any]) -> str:
    start = fields.get('起算日期')
    nights, price = fields.get('入住晚数'), fields.get('每晚单价')
    lines = [f"房号：{scalar(fields.get('房号（选填）')) or '待定'}"]
    if start:
        day = datetime.fromtimestamp(float(start)/1000, TZ).date()
        lines.append(f'起算日期：{day}')
        if nights:
            lines.append(f'下次付费日期：{day + timedelta(days=int(nights))}')
    if nights and price:
        amount = Decimal(str(price))*Decimal(str(nights))
        lines.append(f'每晚{price}元 × {nights}晚 = {amount:.2f}元')
    return '\n'.join(lines)


class LodgingService:
    def __init__(self, root: Path, owner: str, backend: LodgingBackend,
                 classify: Callable[[str, str], str] | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path, self.owner, self.backend, self.classify = root/'lodging.sqlite3', owner, backend, classify
        self.lock = threading.Lock()
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS drafts(scope TEXT PRIMARY KEY,data TEXT);'
                'CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY,scope TEXT,text TEXT);'
                'CREATE TABLE IF NOT EXISTS writes(id TEXT PRIMARY KEY,data TEXT,phase TEXT,record_id TEXT);')

    def handle(self, sender: str, chat: str, message_id: str, text: str) -> str:
        if sender != self.owner:
            return '住宿第一版仅绑定人可用。'
        scope = hashlib.sha256((sender+'\0'+chat).encode()).hexdigest()
        with self.lock, sqlite3.connect(self.path) as db:
            cached = db.execute('SELECT scope,text FROM replies WHERE id=?', (message_id,)).fetchone()
            if cached:
                return str(cached[1]) if cached[0] == scope else '消息归属不一致。'
            row = db.execute('SELECT data FROM drafts WHERE scope=?', (scope,)).fetchone()
            draft = json.loads(row[0]) if row else None
            try:
                reply, draft = self._handle(db, scope, message_id, text, draft)
            except LodgingError as exc:
                reply = str(exc)
            if draft is None:
                db.execute('DELETE FROM drafts WHERE scope=?', (scope,))
            else:
                db.execute('INSERT OR REPLACE INTO drafts VALUES (?,?)', (scope, json.dumps(draft, ensure_ascii=False)))
            db.execute('INSERT INTO replies VALUES (?,?,?)', (message_id, scope, reply))
            return reply

    def _handle(self, db: sqlite3.Connection, scope: str, mid: str, text: str,
                draft: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
        body = re.sub(r'^住宿\s*', '', text.strip(), count=1)
        if body in {'', '帮助'}:
            return HELP, draft
        if body == '提交':
            if not draft or draft['kind'] == 'quote':
                return '没有待提交登记；试算不能直接提交，请先发“住宿 新开”或“住宿 续住”。', draft
            return self._submit(db, draft), draft
        if draft and db.execute('SELECT 1 FROM writes WHERE id=?', (draft['id'],)).fetchone():
            phase = db.execute('SELECT phase FROM writes WHERE id=?', (draft['id'],)).fetchone()[0]
            if phase != 'done':
                return '上笔提交结果待核对。请发“住宿 提交”回查；暂不能改动或取消这份草稿。', draft
            draft = None
        if body == '取消':
            return '已取消未提交草稿；已保存住宿未删除。', None
        if body == '状态':
            return (self._preview(draft) if draft else '没有住宿草稿。'), draft
        match = re.match(r'^(新开|续住|试算|修改|补充|查询)(?:\s+|$)(.*)$', body, re.S)
        if not match:
            if not self.classify:
                return '自由表达判断未启用，请使用固定字段指令。\n'+HELP, draft
            intent = self.classify(body, '有本人未提交草稿' if draft else '无草稿')
            names = {'create': '新开', 'extend': '续住', 'quote': '试算', 'amend': '修改',
                     'query': '查询', 'supplement': '补充', 'cancel': '取消'}
            if intent not in names:
                return '暂不能确定你的住宿要求，请选择新开、续住、试算、修改或查询。\n'+HELP, draft
            return f'理解为“{names[intent]}”。尚未改动记录，请用“住宿 {names[intent]}”及明确字段继续。\n'+HELP, draft
        action, raw = match.groups()
        values = parse_fields(raw)
        checked(values, quote=action == '试算')
        if action == '查询':
            if set(values) != {'记录'}:
                raise LodgingError('当前支持按记录ID查询：住宿 查询 记录=rec...')
            record = self.backend.get(values['记录'])
            return values['记录']+'\n'+describe(record['fields']), draft
        if action == '补充':
            if not draft:
                raise LodgingError('没有草稿，请先发住宿新开、续住或修改。')
            updated = dict(draft)
            updated['values'] = {**draft['values'], **values}
            if draft['kind'] == 'amend' and '记录' in values:
                if '记录' in draft['values'] and values['记录'] != draft['values']['记录']:
                    raise LodgingError('修改目标不能在补充中切换，请取消并重新发起修改。')
                if 'before' not in updated:
                    updated['before'] = self.backend.get(values['记录'])['fields']
            checked(updated['values'], quote=updated['kind'] == 'quote')
            return self._preview(updated), updated
        if draft:
            raise LodgingError('已有未提交草稿，请先“住宿 取消”或“住宿 提交”；试算可先取消草稿。')
        kind = {'新开': 'create', '续住': 'extend', '修改': 'amend', '试算': 'quote'}[action]
        if kind in {'create', 'quote'} and '记录' in values:
            raise LodgingError('新开/试算不接受已有记录ID。')
        draft = {'id': str(uuid.uuid5(uuid.NAMESPACE_URL, scope+mid)), 'kind': kind, 'values': values}
        if kind == 'amend' and '记录' in values:
            draft['before'] = self.backend.get(values['记录'])['fields']
        return self._preview(draft), draft

    def _preview(self, draft: dict[str, Any]) -> str:
        values = draft['values']
        fields, missing = checked(values, quote=draft['kind'] == 'quote')
        if draft['kind'] == 'amend':
            missing = [] if '记录' in values else ['记录']
            fields = {**draft.get('before', {}), **fields}
        elif draft['kind'] == 'extend' and '记录' not in values:
            missing.append('记录')
        metadata = '\n'.join(f'{k}：{v}' for k, v in values.items() if k in {'记录', '酒店', '项目', '人员'})
        changes = '\n'.join(f'本次填写 {k}={v}' for k, v in values.items() if k in FIELDS)
        return ('住宿预览（未保存）\n'+describe(fields)+'\n'+changes+'\n'+metadata+'\n'+
            ('待补：'+'、'.join(missing)+'。请发“住宿 补充 字段=值”。' if missing else
             '仅试算，不写表。' if draft['kind'] == 'quote' else '内容正确可发“住宿 提交”。'))

    def _submit(self, db: sqlite3.Connection, draft: dict[str, Any]) -> str:
        token = str(draft['id'])
        marker = 'lodging-task:'+token
        saved = db.execute('SELECT data,phase,record_id FROM writes WHERE id=?', (token,)).fetchone()
        if saved and saved[1] == 'done':
            return '已登记，未重复写入：'+str(saved[2])
        if saved:
            payload = json.loads(saved[0])
            matches = self.backend.find(marker)
            if len(matches) != 1:
                return '上次写入结果不确定，未再次写入。请维护者核对任务 '+token
            rid = str(matches[0]['record_id'])
        else:
            values = draft['values']
            fields, missing = checked(values)
            kind = draft['kind']
            target = values.get('记录', '')
            if kind in {'extend', 'amend'} and not target:
                missing.append('记录')
            if kind == 'amend':
                if not target:
                    raise LodgingError('修改需要明确记录ID。')
                before = self.backend.get(target)['fields']
                if 'before' not in draft:
                    raise LodgingError('修改草稿缺少原记录快照，请取消并重新发起修改。')
                if before != draft['before']:
                    raise LodgingError('原记录已变化，请取消草稿并重新查询、修改。')
                if before.get('已确认') or before.get('实付金额') or before.get('实际付款日期') or before.get('不办理'):
                    raise LodgingError('已确认、付款或不办理记录请先人工核对，当前不覆盖。')
                if not fields:
                    raise LodgingError('请提供至少一个要修改的日期、晚数、单价或房号。')
                full = {**before, **fields}
                missing = [k for k in ('起算日期', '入住晚数', '每晚单价') if not full.get(k)]
                old_note = str(scalar(before.get('备注', '')) or '')
            else:
                if kind == 'extend':
                    self.backend.get(target)  # Existence, not inference from room number.
                full = dict(fields)
                fields['业务类型'] = '续住' if kind == 'extend' else '新开'
                old_note = ''
            if missing:
                raise LodgingError('待补：'+'、'.join(missing)+'。未写入。')
            note = {k: v for k, v in values.items() if k in {'酒店', '项目', '人员', '记录'}}
            fields['备注'] = old_note+'\n'+marker+'\n'+json.dumps(note, ensure_ascii=False)
            payload = {'fields': fields, 'expected': full, 'kind': kind, 'target': target,
                       'before': draft.get('before') if kind == 'amend' else None}
            db.execute('INSERT INTO writes VALUES (?,?,?,NULL)', (token, json.dumps(payload, ensure_ascii=False), 'pending'))
            db.commit()  # Persist intent before any external mutation.
            try:
                if kind == 'amend':
                    self.backend.update(target, fields)
                    rid = target
                else:
                    rid = self.backend.create(fields, token)
            except Exception:
                return '写入结果待核对，未重试。请发“住宿 提交”回查。任务 '+token
        record = self.backend.get(rid)
        actual = record['fields']
        for name, value in payload['fields'].items():
            got = scalar(actual.get(name))
            if name in {'入住晚数', '每晚单价'}:
                try:
                    if Decimal(str(got)) == Decimal(str(value)):
                        continue
                except InvalidOperation:
                    pass
            if name == '房号（选填）' and value == '' and got is None:
                continue
            if got != value:
                raise LodgingError('回读字段不一致，未再次写入，请维护者核对：'+rid)
        expected = payload['expected']
        try:
            amount = Decimal(str(expected['每晚单价']))*Decimal(str(expected['入住晚数']))
            day = datetime.fromtimestamp(float(expected['起算日期'])/1000, TZ).date()+timedelta(days=int(expected['入住晚数']))
            if Decimal(str(scalar(actual.get('应付金额')))) != amount or scalar(actual.get('下次付费日期')) != day.isoformat():
                raise ValueError
        except (ValueError, InvalidOperation, TypeError) as exc:
            raise LodgingError('登记已写入，金额或到期日回读尚未一致，请发“住宿 提交”回查：'+rid) from exc
        db.execute('UPDATE writes SET phase=?,record_id=? WHERE id=?', ('done', rid, token))
        return '住宿登记已保存（未记付款）：'+rid+'\n'+describe(actual)
