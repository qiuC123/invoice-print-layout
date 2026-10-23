"""Persistent expense matters, independent of transient print batches."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager, nullcontext
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from invoice_print_layout.storage import ensure_workspace, read_history
from invoice_print_layout.memory import _record_from_entry
from invoice_print_layout.reliability import save_json
from invoice_print_layout.missing_materials import (
    acknowledgement_valid, can_acknowledge_missing, expense_scope, source_snapshot,
)

CATEGORIES = ('材料采购', '酒店', '高铁', '外卖', '餐饮', '打车', '顺丰', '其他')
ROLES = {'purchase': '购买明细', 'invoice': '发票', 'receipt': '收据', 'payment': '微信／支付宝扣费记录',
         'order': '订单记录', 'trip': '行程单', 'detail': '运单明细', 'other': '待分类材料', 'package': '历史打印包'}
STAGES = {'draft': '待提交', 'submitted': '已提交', 'reimbursed': '已报销', 'cancelled': '已取消'}
MAX_BYTES = 20 * 1024 * 1024


def now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def returned_material_state(db: sqlite3.Connection, item: dict[str, Any]) -> bool:
    """Recognize old withdrawal receipts without rewriting historical rows."""
    if item.get('returned_to_synthesis') is True:
        return True
    if (item.get('stage') != 'draft' or item.get('verified') or item.get('deleted') or
            item.get('merged_into') or item.get('material_match')):
        return False
    event = db.execute('SELECT message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 1', (item['id'],)).fetchone()
    prefixes = ('撤回材料匹配；', '撤回材料登记；', '撤回多对多合成，恢复原费用及附件归属')
    if not event or not str(event['message']).startswith(prefixes):
        return False
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='material_matches'").fetchone():
        return False
    for row in db.execute('SELECT data FROM material_matches ORDER BY rowid DESC'):
        group = json.loads(row['data'])
        if group.get('item_id') != item['id'] and item['id'] not in group.get('member_before', {}):
            continue
        return bool(group.get('status') == 'undone' and not (
            item.get('updated_at') and group.get('undone_at') and item['updated_at'] > group['undone_at']))
    return False


def expense_identifier_keys(db: sqlite3.Connection, item_id: str, data: dict[str, Any], *,
                            source: str | None = '') -> set[str]:
    """Read typed transaction identities without migrating historical ledgers."""
    from invoice_print_layout.transaction_evidence import record_identifier_keys
    if data.get('undone_material_group') or data.get('merged_into'):
        return set()
    keys = record_identifier_keys(data)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
        return keys
    original_id = source.removeprefix('intake:') if source and source.startswith('intake:') else ''
    for row in db.execute('SELECT data FROM intake_queue'):
        entry = json.loads(row['data'])
        if entry.get('item_id') == item_id or original_id and entry.get('id') == original_id:
            keys.update(record_identifier_keys(entry))
    return keys


def amount_cents(value: object) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal('999999999') or number != number.quantize(Decimal('.01')):
            raise ValueError('金额必须为非负数，最多两位小数')
        return int(number * 100)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError('金额无效') from exc


def evaluate(category: str, roles: set[str], *, legacy: bool = False, material_basis: str = 'standard',
             amount_pending: bool = False) -> dict[str, Any]:
    if not isinstance(amount_pending, bool):
        raise ValueError('金额待确认标记必须为布尔值')
    result = _evaluate_materials(category, roles, legacy=legacy, material_basis=material_basis)
    if amount_pending:
        result['missing'].append('金额待确认')
        result['complete'] = False
    return result


def _evaluate_materials(category: str, roles: set[str], *, legacy: bool, material_basis: str) -> dict[str, Any]:
    missing: list[str] = []
    if material_basis == 'payment_only':
        return {'missing': [] if 'payment' in roles else ['支付凭证'],
                'complete': 'payment' in roles, 'alternative': True,
                'basis': '此事项已确认仅需支付凭证；打印按A4纸2×2排列'}
    if category == '打车' and not legacy:
        missing.extend(ROLES[role] for role in ('invoice', 'trip') if role not in roles)
        return {'missing': missing, 'complete': not missing, 'alternative': False,
                'basis': '打车须有正式发票和行程单，不要求付款截图；金额及对应行程需人工核对'}
    if category == '顺丰' and not legacy:
        missing.extend(ROLES[role] for role in ('invoice', 'detail') if role not in roles)
        return {'missing': missing, 'complete': not missing, 'alternative': False,
                'basis': '快递须有正式发票和运单明细，不要求付款截图；金额及对应运单需人工核对'}
    if material_basis == 'invoice_payment':
        if not roles.intersection({'invoice', 'receipt'}):
            missing.append('发票或收据')
        if 'payment' not in roles:
            missing.append('付款截图')
        return {'missing': missing, 'complete': not missing, 'alternative': False,
                'basis': '发票与收据任选一种，另附付款截图；金额与对应关系需人工核对'}
    if legacy and 'package' in roles:
        return {'missing': [], 'complete': True, 'alternative': False, 'basis': '历史成功打印包；报销状态未确认'}
    if category == '材料采购':
        if 'purchase' not in roles:
            missing.append('购买明细／收据')
        if not roles.intersection({'invoice', 'payment'}):
            missing.append('发票或微信／支付宝扣费记录')
    elif category in {'酒店', '高铁'}:
        if 'invoice' not in roles:
            missing.append('发票' if category == '酒店' else '12306发票')
    elif category == '餐饮':
        if 'invoice' not in roles:
            missing.append('发票')
        if not roles.intersection({'order', 'purchase'}):
            missing.append('订单记录或购买明细／收据')
    elif category in {'打车', '外卖', '顺丰'}:
        second = {'打车': 'trip', '外卖': 'order', '顺丰': 'detail'}[category]
        missing.extend(ROLES[role] for role in ('invoice', second) if role not in roles)
    else:
        missing.append('确认此类别的材料要求（请选择具体类别）')
    return {'missing': missing, 'complete': not missing,
            'alternative': category == '材料采购' and 'invoice' not in roles and 'payment' in roles,
            'basis': '材料存在性检查；金额、内容和真伪需人工核对'}


class ExpenseStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / '工作台'
        self.files = self.root / '附件'
        self.files.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'expenses.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS expenses (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, item_id TEXT REFERENCES expenses(id), name TEXT NOT NULL,
                    role TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL,
                    UNIQUE(item_id,digest,role));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, item_id TEXT, at TEXT, message TEXT);
                CREATE TABLE IF NOT EXISTS selections (owner TEXT PRIMARY KEY, item_id TEXT);
                CREATE TABLE IF NOT EXISTS automatic_ride_files (
                    digest TEXT PRIMARY KEY, item_id TEXT REFERENCES expenses(id), material_key TEXT NOT NULL);
            ''')
            from invoice_print_layout.expense_projects import install_project_guards
            install_project_guards(db)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, values: dict[str, Any], source: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = self._validate(values)
            data.update(id=uuid.uuid4().hex[:10], created_at=now(), updated_at=now(), stage='draft', verified=False)
            if source:
                previous = db.execute('SELECT id FROM expenses WHERE source=?', (source,)).fetchone()
                if previous:
                    return self.get(previous['id'])
            db.execute('INSERT INTO expenses VALUES (?,?,?)', (data['id'], source, json.dumps(data, ensure_ascii=False)))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (data['id'], now(), '登记事项'))
        return self.get(data['id'])

    def _validate(self, values: dict[str, Any]) -> dict[str, Any]:
        raw_category = values.get('category')
        if raw_category is not None and not isinstance(raw_category, str):
            raise ValueError('费用类别必须为文字')
        category = (raw_category or '').strip() or '其他'
        if len(category) > 200:
            raise ValueError('费用类别文字过长')
        result: dict[str, Any] = {'category': category}
        for key, default in [('title', ''), ('project', '待分配项目'), ('merchant', ''), ('note', ''), ('order_number', '')]:
            result[key] = str(values.get(key, default)).strip()
            if len(result[key]) > (2000 if key == 'note' else 200):
                raise ValueError('文字过长')
        # Keep the shopping platform, real shop and payment channel separate.
        # Optional transaction identity is retained when unrelated fields change.
        for key in ('shopping_platform', 'shop_name', 'payment_channel'):
            value = values.get(key, '')
            if not isinstance(value, str) or len(value) > 200:
                raise ValueError('交易平台、店铺和支付渠道必须为200字以内的文字')
            result[key] = value.strip()
        raw_identifiers = values.get('transaction_identifiers', [])
        kinds = {'shopping_order', 'payment_order', 'merchant_order', 'payment_transaction', 'untyped_order'}
        if not isinstance(raw_identifiers, list) or len(raw_identifiers) > 40:
            raise ValueError('交易编号列表无效')
        identifiers: list[dict[str, str]] = []
        from invoice_print_layout.transaction_evidence import identifiers_for_record
        for identifier in raw_identifiers:
            if (not isinstance(identifier, dict) or not isinstance(identifier.get('kind'), str) or
                    identifier.get('kind') not in kinds):
                raise ValueError('交易编号类型无效')
            cleaned: dict[str, str] = {}
            for key in ('kind', 'namespace', 'value', 'label', 'quote'):
                value = identifier.get(key, '')
                if not isinstance(value, str) or len(value) > (2000 if key == 'quote' else 200):
                    raise ValueError('交易编号内容无效')
                cleaned[key] = value.strip()
            if not cleaned['value']:
                raise ValueError('交易编号不能为空')
            normalised = identifiers_for_record({'transaction_identifiers': [cleaned]})
            if not normalised:
                raise ValueError('交易编号格式无效')
            cleaned = normalised[0]
            if cleaned not in identifiers:
                identifiers.append(cleaned)
        result['transaction_identifiers'] = identifiers
        raw_evidence = values.get('transaction_evidence', {})
        if not isinstance(raw_evidence, dict) or len(raw_evidence) > 40:
            raise ValueError('交易识别依据无效')
        if any(not isinstance(key, str) or len(key) > 200 or not isinstance(value, str) or len(value) > 2000
               for key, value in raw_evidence.items()):
            raise ValueError('交易识别依据必须为文字')
        result['transaction_evidence'] = dict(raw_evidence)
        if not result['title']:
            raise ValueError('请填写事项名称')
        result['project'] = result['project'] or '待分配项目'
        pending = values.get('amount_pending', False)
        if not isinstance(pending, bool):
            raise ValueError('金额待确认标记必须为布尔值')
        raw_amount = values.get('amount', '0')
        empty_amount = raw_amount is None or (isinstance(raw_amount, str) and not raw_amount.strip())
        result['amount_cents'] = 0 if empty_amount else amount_cents(raw_amount)
        result['amount_pending'] = pending or empty_amount
        if result['amount_pending'] and result['amount_cents'] != 0:
            raise ValueError('金额待确认时只能保存0占位；请明确金额后再保存')
        expense_date = str(values.get('expense_date', date.today().isoformat())).strip()
        result['expense_date'] = date.fromisoformat(expense_date).isoformat() if expense_date else ''
        followup = str(values.get('followup_date', '')).strip()
        result['followup_date'] = date.fromisoformat(followup).isoformat() if followup else ''
        invoice_state = values.get('invoice_state', 'not_requested')
        if invoice_state not in {'not_requested', 'requested', 'unavailable'}:
            raise ValueError('开票状态无效')
        result['invoice_state'] = invoice_state
        material_basis = values.get('material_basis', 'standard')
        if material_basis not in {'standard', 'payment_only', 'invoice_payment'}:
            raise ValueError('材料要求无效')
        result['material_basis'] = material_basis
        for field in ('display_name', 'report_section', 'report_group'):
            result[field] = str(values.get(field, '')).strip()
            if len(result[field]) > 200:
                raise ValueError('报表设置文字过长')
        from invoice_print_layout.report_snapshot import REPORT_SECTIONS
        if result['report_section'] and result['report_section'] not in REPORT_SECTIONS:
            raise ValueError('报表栏目无效')
        from invoice_print_layout.expense_projects import project_views, assert_project_name_not_deleted
        projects = project_views(self)
        project_id = str(values.get('project_id', ''))
        matches = [p for p in projects if p['id'] == project_id] if project_id else [p for p in projects if p['name'] == result['project']]
        if not project_id:
            # Old history records can retain a directory name after the invoice
            # display name changes. Only an exact active canonical identity may
            # bind that alias; unrelated unknown labels remain unassigned.
            from invoice_print_layout.project_registry import ProjectRegistry
            canonical = [row for row in ProjectRegistry(self.workspace / '后勤' / 'tasks.sqlite3').list()
                         if row['name'] == result['project']]
            if len(canonical) == 1:
                known = {row['id'] for row in matches} | {
                    row['id'] for row in projects if row['id'] == canonical[0]['id']}
                if len(known) > 1:
                    raise ValueError('项目名称对应多个项目，请选择具体项目')
                matches = [row for row in projects if row['id'] in known]
        if project_id and len(matches) != 1:
            raise ValueError('项目不存在，请刷新后重试')
        if not project_id and not matches:
            with self.connect() as db:
                assert_project_name_not_deleted(db, result['project'])
        result['project_id'] = matches[0]['id'] if len(matches) == 1 else ''
        if len(matches) == 1:
            result['project'] = matches[0]['name']
        if 'reimbursement_person_id' in values:
            from invoice_print_layout.reimbursement_people import validate_person
            with self.connect() as db:
                result.update(validate_person(db, result['project_id'], values['reimbursement_person_id']))
        return result

    def get(self, item_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT id,source,data FROM expenses WHERE id=?', (item_id,)).fetchone()
            if row is None:
                raise ValueError('事项不存在')
            return self._read_item(db, row)

    def _read_item(self, db: sqlite3.Connection, row: sqlite3.Row, *,
                   projects: list[dict[str, Any]] | None = None,
                   entries: list[dict[str, Any]] | None = None,
                   confirmation_sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(row['data'])
        stored = dict(data)
        item_id = str(row['id'])
        data['attachments'] = [dict(a) for a in db.execute('SELECT id,name,role,path FROM attachments WHERE item_id=?', (item_id,))]
        source_attachments = [dict(attachment) for attachment in data['attachments']]
        attachment_digests = {a['id']: a['digest'] for a in db.execute(
            'SELECT id,digest FROM attachments WHERE item_id=?', (item_id,))}
        data['events'] = [dict(e) for e in db.execute('SELECT at,message FROM events WHERE item_id=? ORDER BY id DESC LIMIT 30', (item_id,))]
        # Old role-edit events did not name the attachment. Preserve all
        # still-unclassified proofs on such a matter rather than overriding
        # the user's choice from native text, even after the event list's
        # 30-row display window has moved past the edit.
        manual_other = bool(db.execute('SELECT 1 FROM events WHERE item_id=? AND message=? LIMIT 1',
                                      (item_id, '材料用途改为：' + ROLES['other'])).fetchone())
        returned = returned_material_state(db, data)
        if entries is None:
            entries = ([json.loads(entry['data']) for entry in db.execute('SELECT data FROM intake_queue')]
                       if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone() else [])
        source_id = str(row['source'] or '').removeprefix('intake:') if str(row['source'] or '').startswith('intake:') else ''
        saved_materials = [entry for entry in entries
                           if entry.get('item_id') == item_id or entry.get('id') == source_id]
        data['version'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        data['returned_to_synthesis'] = returned
        # Display attribution without rewriting old records or their versions.
        from invoice_print_layout.reimbursement_people import current_person_label
        if data.get('reimbursement_person_id') or 'reimbursement_person_label' in data:
            data['reimbursement_person_label'] = current_person_label(
                db, str(data.get('project_id') or ''), str(data.get('reimbursement_person_id') or ''),
                str(data.get('reimbursement_person_label') or ''))
        # The invoice display name is independent of the historical stored
        # amount/verification/version and the logistics directory's name.
        from invoice_print_layout.expense_projects import project_views
        if projects is None:
            projects = project_views(self, db=db)
        project = next((row for row in projects if row['id'] == data.get('project_id')), None)
        if not project and not data.get('project_id'):
            from invoice_print_layout.project_registry import ProjectRegistry
            canonical = next((row for row in ProjectRegistry(self.workspace / '后勤' / 'tasks.sqlite3').list()
                              if row['name'] == data.get('project')), None)
            owners = [row for row in projects if row['id'] == canonical['id']] if canonical else [
                row for row in projects if row['name'] == data.get('project')]
            project = owners[0] if len(owners) == 1 else None
        if project:
            data['project'] = project['name']
        # Resolve proven historical choices after hashing the persisted view,
        # but before material rules, missing-proof checks and print policy.
        raw_matching = stored.get('material_match')
        matching = raw_matching if isinstance(raw_matching, dict) else {}
        raw_recognition = matching.get('recognition')
        recognition = raw_recognition if isinstance(raw_recognition, dict) else {}
        raw_choice = recognition.get('report_choice')
        original_choice = raw_choice if isinstance(raw_choice, dict) else {}
        from invoice_print_layout.report_types import (
            explicit_manual_report_choice, legacy_auto_category_report_choice,
            legacy_manual_category_report_choice,
        )
        history = [dict(event) for event in db.execute(
            'SELECT id,at,message FROM events WHERE item_id=? ORDER BY id', (item_id,))]
        registration = None
        if (matching.get('mode') == 'registration' and matching.get('group_id') and
                db.execute("SELECT 1 FROM sqlite_master WHERE name='material_matches'").fetchone()):
            group_row = db.execute('SELECT data FROM material_matches WHERE id=?', (matching['group_id'],)).fetchone()
            if group_row:
                try:
                    registration = json.loads(group_row['data'])
                except (ValueError, TypeError):
                    pass
        manual_choice = explicit_manual_report_choice(stored, history, registration)
        if manual_choice:
            data['manual_report_choice'] = manual_choice
        compatible_choice = legacy_manual_category_report_choice(stored, history, registration)
        if (not compatible_choice and not manual_choice and matching.get('mode') == 'registration' and
                original_choice.get('project_type') == 'other' and stored.get('category') not in (None, '', '其他')):
            compatible_choice = legacy_auto_category_report_choice(stored, history)
        if compatible_choice:
            data['stored_report_choice'] = {field: stored.get(field, '') for field in
                ('project_type', 'project_name', 'category', 'report_section', 'display_name', 'title', 'report_group')}
            data.update(compatible_choice)
        from invoice_print_layout.ride_proofs import invoice_trip_required, native_trip_pdf
        from invoice_print_layout.courier_proofs import invoice_waybill_required, native_waybill_pdf
        # This compatibility field changes the effective material rule only;
        # the saved category, template name and every source role stay intact.
        data['courier_waybill_required'] = invoice_waybill_required({**data, 'courier_waybill_required': False})
        manual_digests = {str(entry.get('digest') or '') for entry in saved_materials
                          if entry.get('manual_role') or entry.get('result') == '人工补充材料信息，尚未关联费用'}
        if invoice_trip_required(data) and not manual_other:
            for attachment in data['attachments']:
                path = Path(attachment['path']).resolve()
                if (attachment['role'] == 'other' and path.is_relative_to(self.workspace) and
                        attachment_digests[attachment['id']] not in manual_digests and
                        native_trip_pdf(path, attachment_digests[attachment['id']])):
                    attachment['role'] = 'trip'
        if invoice_waybill_required(data) and not manual_other:
            for attachment in data['attachments']:
                path = Path(attachment['path']).resolve()
                if (attachment['role'] == 'other' and path.is_relative_to(self.workspace) and
                        attachment_digests[attachment['id']] not in manual_digests and
                        native_waybill_pdf(path, attachment_digests[attachment['id']])):
                    attachment['role'] = 'detail'
        roles = {a['role'] for a in data['attachments'] if Path(a['path']).is_file()}
        for a in data['attachments']:
            a['available'] = Path(a.pop('path')).is_file()
        data.setdefault('material_basis', 'standard')
        data.setdefault('amount_pending', False)
        data.setdefault('deleted', False)
        data.update(evaluate('顺丰' if data['courier_waybill_required'] else data['category'], roles, legacy=bool(data.get('legacy')),
                             material_basis=data['material_basis'], amount_pending=data['amount_pending']))
        data['amount'] = '' if data['amount_pending'] else f"{data['amount_cents'] / 100:.2f}"
        data['overdue'] = bool(data['followup_date'] and data['followup_date'] <= date.today().isoformat()
                               and data['stage'] == 'draft' and not data['complete'])
        data['missing_acknowledgement_allowed'] = can_acknowledge_missing(data)
        snapshot_required = bool(stored.get('missing_material_acknowledgement') or
                                 data['missing_acknowledgement_allowed'])
        acknowledged_sources = source_snapshot(source_attachments, self.workspace) if snapshot_required else []
        if confirmation_sources is not None:
            confirmation_sources.extend(acknowledged_sources)
        if snapshot_required:
            # Bind both first and repeated missing-proof confirmations to live
            # sources. Complete ordinary/legacy rows retain their old versions.
            data['version'] = hashlib.sha256(json.dumps(
                [data['version'], acknowledged_sources], sort_keys=True).encode()).hexdigest()
        data['missing_acknowledged'] = acknowledgement_valid(stored, data, acknowledged_sources)
        stale_acknowledgement = bool(stored.get('missing_material_acknowledgement') and
                                     stored.get('verified') and not data['missing_acknowledged'])
        if stale_acknowledgement:
            # External source loss/replacement must invalidate the effective
            # review without rewriting the ledger during a read.
            data['verified'] = False
        data['ready'] = ((data['complete'] or data['missing_acknowledged']) and data['verified']
                         and not data['deleted'] and not returned)
        data.setdefault('review_requested', False)
        data['pending_review'] = (not data['deleted'] and not returned and data['stage'] == 'draft' and not data['verified']
                                  and (data['complete'] or data['review_requested'] or
                                       bool(stored.get('missing_material_acknowledgement'))))
        from invoice_print_layout.report_types import infer_report_choice, material_report_suggestion
        analysis = dict(data)
        analysis['recognition_sources'] = [
            {'text': entry.get('text', ''), 'fields': entry.get('fields') or {}}
            for entry in saved_materials]
        try:
            data['report_choice'] = infer_report_choice(analysis)
        except ValueError:
            data['report_choice'] = {}
        data['report_choice_suggestion'] = material_report_suggestion(analysis)
        data['waiting_days'] = (date.today() - date.fromisoformat(data['created_at'][:10])).days
        return data

    def list_items(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute('SELECT id,source,data FROM expenses ORDER BY rowid DESC').fetchall()
            items = self._read_items(db, rows)
        return items if include_deleted else [item for item in items if not item['deleted']]

    def get_many(self, item_ids: list[str]) -> list[dict[str, Any]]:
        """Read current requested views in input order, sharing only this call's context."""
        if not item_ids:
            return []
        keys = list(dict.fromkeys(item_ids))
        with self.connect() as db:
            rows = db.execute('SELECT id,source,data FROM expenses WHERE id IN (' +
                              ','.join('?' for _ in keys) + ')', keys).fetchall()
            by_id = {str(row['id']): row for row in rows}
            if any(key not in by_id for key in keys):
                raise ValueError('事项不存在')
            return self._read_items(db, [by_id[key] for key in item_ids])

    def _read_items(self, db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        from invoice_print_layout.expense_projects import project_views
        if not rows:
            return []
        projects = project_views(self, db=db)
        entries = ([json.loads(entry['data']) for entry in db.execute('SELECT data FROM intake_queue')]
                   if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone() else [])
        return [self._read_item(db, row, projects=projects, entries=entries) for row in rows]

    def remove(self, item_id: str, expected_version: str, *, restore: bool = False) -> dict[str, Any]:
        """Hide a draft reversibly, retaining its expense identity and every original."""
        if not isinstance(item_id, str) or not item_id or not isinstance(expected_version, str) or not expected_version:
            raise ValueError('请选择有效事项并提供当前版本')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(item_id)
            if current['version'] != expected_version:
                raise ValueError('事项或材料已变化，请刷新后重新选择')
            if current['stage'] not in {'draft', 'cancelled'} or current.get('merged_into'):
                raise ValueError('仅可删除或恢复草稿及已取消事项；合并成员请在合并去向处理')
            if current['deleted'] == (not restore):
                return current
            from invoice_print_layout.material_matching import MaterialMatching
            before = MaterialMatching._member_state(db, item_id)
            data = dict(before['data'])
            if current.get('returned_to_synthesis'):
                data['returned_to_synthesis'] = True
            if restore:
                data.pop('deleted', None)
                data.pop('deleted_at', None)
                data.update(verified=False, review_requested=data['stage'] == 'draft')
                label = '恢复已删除事项；保留原状态及原件，金额和材料待核对'
            else:
                data.update(deleted=True, deleted_at=now())
                label = '删除事项；移出费用列表及报销统计，保留原件，可恢复'
            data['updated_at'] = now()
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), label))
            MaterialMatching.record_expense_action(db, item_id, before)
        return self.get(item_id)

    def request_review(self, item_ids: list[str], versions: dict[str, str]) -> dict[str, Any]:
        """Move selected drafts to review without claiming their material is complete."""
        if (not isinstance(item_ids, list) or not item_ids or
                any(not isinstance(key, str) or not key for key in item_ids) or
                len(item_ids) != len(set(item_ids)) or not isinstance(versions, dict)):
            raise ValueError('请勾选不重复的有效事项')
        changed = 0
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            items = [self.get(key) for key in item_ids]
            if any(item['deleted'] or item['stage'] != 'draft' or item['verified'] or item.get('returned_to_synthesis') for item in items):
                raise ValueError('只能将未核对的草稿移入待核对')
            if len({item.get('project_id') or item['project'] for item in items}) != 1:
                raise ValueError('请选择同一项目的事项')
            if any(versions.get(item['id']) != item['version'] for item in items):
                raise ValueError('事项或材料已变化，请刷新后重新选择')
            for item in items:
                if item['pending_review']:
                    continue
                row = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()
                data = json.loads(row['data'])
                data.update(review_requested=True, verified=False, updated_at=now())
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item['id']))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           (item['id'], now(), '确认进入待核对；缺失材料仍保留，未核验或提交'))
                changed += 1
        return {'items': [self.get(key) for key in item_ids], 'changed': changed}

    def update(self, item_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('事项不存在')
            old = json.loads(row['data'])
            if self.get(item_id).get('returned_to_synthesis'):
                raise ValueError('已撤回材料，请先在合成台重新确认创建')
            if old.get('deleted'):
                raise ValueError('事项已删除，请先恢复再编辑')
            if 'expected_version' in values and values['expected_version'] != self.get(item_id)['version']:
                raise ValueError('事项或材料已变化，请刷新后重新保存')
            if 'amount_pending' in values and not isinstance(values['amount_pending'], bool):
                raise ValueError('金额待确认标记必须为布尔值')
            merged = {**old, 'amount': '' if old.get('amount_pending', False) else f"{old['amount_cents'] / 100:.2f}",
                      **values}
            if 'amount' in values and values['amount'] is not None and str(values['amount']).strip():
                # An explicitly entered amount is the confirmation. Name/category
                # edits without this field cannot confirm an unknown zero placeholder.
                merged['amount_pending'] = False
            choice = {}
            if 'project_type' in values or 'project_name' in values:
                from invoice_print_layout.report_types import validate_report_choice
                choice = validate_report_choice(values.get('project_type'), values.get('project_name'))
                merged.update(choice)
            changes = {**self._validate(merged), **choice}
            data = {**old, **changes, 'updated_at': now()}
            if any(old.get(key, False if key == 'amount_pending' else None) != value for key, value in changes.items()):
                if old['stage'] != 'draft':
                    raise ValueError('请先退回待提交，再修改已提交的事项')
                data['verified'] = False
                data.pop('review_requested', None)
            if old['category'] != data['category']:
                data['legacy'] = False
            from invoice_print_layout.material_matching import MaterialMatching
            MaterialMatching.preserve_edit_guard(db, item_id)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), '更新事项；内容变更后需重新核对'))
        current = self.get(item_id)
        if old['category'] != current['category']:
            from invoice_print_layout.automatic_category import AutomaticCategory
            AutomaticCategory(self, nullcontext()).feedback(old, current)
        return current

    def edit_review_fields(self, item_id: Any, expected_version: Any, project_type: Any,
                           project_name: Any, amount: Any) -> dict[str, Any]:
        from invoice_print_layout.report_types import validate_report_choice
        if not isinstance(item_id, str) or not item_id or not isinstance(expected_version, str) or not expected_version:
            raise ValueError('请选择费用并提供当前版本')
        choice = validate_report_choice(project_type, project_name)
        if amount is None or isinstance(amount, str) and not amount.strip():
            raise ValueError('请确认并填写金额')
        cents = amount_cents(amount)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = self.get(item_id)
            if item['version'] != expected_version:
                raise ValueError('费用已变化，请刷新后重新编辑')
            if item['stage'] != 'draft' or item['deleted'] or item.get('merged_into') or item.get('returned_to_synthesis'):
                raise ValueError('仅可编辑未删除、未合并且已确认合成的草稿费用')
            stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            if stored['category'] != choice['category']:
                stored['legacy'] = False
            from invoice_print_layout.material_matching import MaterialMatching
            MaterialMatching.preserve_edit_guard(db, item_id)
            stored.update(choice, amount_cents=cents, amount_pending=False, verified=False,
                          review_requested=True, updated_at=now())
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item_id, now(), '编辑报销项目类型、项目名称及金额；保留原件和关联，待重新核对'))
        return self.get(item_id)

    def transition(self, item_id: str, action: str, *, expected_version: str | None = None,
                   acknowledge_missing: bool = False, expected_missing: list[str] | None = None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,source,data FROM expenses WHERE id=?', (item_id,)).fetchone()
            if row is None:
                raise ValueError('事项不存在')
            confirmation_sources: list[dict[str, Any]] = []
            current = self._read_item(db, row, confirmation_sources=confirmation_sources)
            if current['deleted']:
                raise ValueError('事项已删除，请先恢复再操作')
            if current.get('returned_to_synthesis') and action != 'cancelled':
                raise ValueError('材料已撤回，请先在合成台重新确认创建')
            if expected_version is not None and current['version'] != expected_version:
                raise ValueError('事项或材料已变化，请重新核对')
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            if current.get('returned_to_synthesis'):
                data['returned_to_synthesis'] = True
            if action == 'verify':
                if not isinstance(acknowledge_missing, bool):
                    raise ValueError('缺失材料确认标记必须为布尔值')
                if data['stage'] != 'draft':
                    raise ValueError('请先补齐材料，并在待提交状态下核对')
                if not current['complete']:
                    if (not acknowledge_missing or not can_acknowledge_missing(current)):
                        raise ValueError('请先补齐材料；金额与材料要求已明确后，可确认当前缺项再继续核对')
                    if not isinstance(expected_version, str) or not expected_version:
                        raise ValueError('确认缺项时请提供当前事项版本')
                    if (not isinstance(expected_missing, list) or
                            any(not isinstance(gap, str) for gap in expected_missing) or
                            expected_missing != current['missing']):
                        raise ValueError('缺失材料已变化，请刷新后重新确认实际缺项')
                    attachments = [dict(attachment) for attachment in db.execute(
                        'SELECT id,role,path FROM attachments WHERE item_id=?', (item_id,))]
                    if source_snapshot(attachments, self.workspace) != confirmation_sources:
                        raise ValueError('确认期间材料来源已变化，请刷新后重新核对')
                    data['missing_material_acknowledgement'] = {
                        'missing': list(current['missing']), 'version': expected_version, 'at': now(),
                        'expense_scope': expense_scope(data),
                        'sources': confirmation_sources,
                    }
                else:
                    data.pop('missing_material_acknowledgement', None)
                data['verified'] = True
                label = '人工确认材料内容及金额'
                if not current['complete']:
                    label += '；已确认仍缺：' + '、'.join(current['missing'])
            elif action == 'return_materials':
                if data['stage'] != 'draft' or current['verified'] or current['complete']:
                    raise ValueError('只能退回尚缺材料、未核对的草稿；已暂配付款请先撤回匹配')
                data['verified'] = False
                data.pop('missing_material_acknowledgement', None)
                label = '人工退回待补材料；保留全部原件'
            elif action in STAGES:
                if data.get('merged_into'):
                    raise ValueError('此记录已合并，请在合并去向处理，不能恢复重复费用')
                if action in {'submitted', 'reimbursed'} and not current['ready']:
                    raise ValueError('请先补齐材料并核对完成')
                if action == 'reimbursed' and data['stage'] != 'submitted':
                    raise ValueError('请先标记已提交，再确认报销到账')
                data['stage'] = action
                label = '状态变更：' + STAGES[action]
            else:
                raise ValueError('不支持的状态操作')
            data['updated_at'] = now()
            data.pop('review_requested', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), label))
        return self.get(item_id)

    def submit_report(self, snapshots: list[dict[str, Any]], files: dict[str, str]) -> None:
        """Commit all report matters together; reject edits made during rendering."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for expected in snapshots:
                current = self.get(expected['id'])
                if current['stage'] != 'draft' or not current['ready']:
                    raise ValueError('制作期间事项状态已变化，请刷新后重试')
                row = db.execute('SELECT data FROM expenses WHERE id=?', (expected['id'],)).fetchone()
                data = json.loads(row['data'])
                # A read-only compatibility view can show a corrected template
                # row. Compare its preserved stored fields against the ledger;
                # never materialize that projection as part of submission.
                expected_stored = dict(expected)
                stored_choice = expected.get('stored_report_choice')
                if isinstance(stored_choice, dict):
                    expected_stored.update(stored_choice)
                if (current['version'] != expected['version'] or
                        any(expected_stored.get(key) != value for key, value in data.items()) or
                        current.get('stored_report_choice') != stored_choice or
                        isinstance(stored_choice, dict) and
                        any(current.get(key) != expected.get(key) for key in stored_choice) or
                        current['attachments'] != expected['attachments']):
                    raise ValueError('制作期间事项或材料已变化，请刷新后重试')
                data.update(stage='submitted', updated_at=now(), report_files=files)
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), data['id']))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           (data['id'], now(), '报销包制作成功，自动标记已提交：' + files['pdf']))

    def add_attachment(self, item_id: str, filename: str, payload: bytes, role: str = 'other') -> dict[str, Any]:
        if role not in ROLES or role == 'package':
            raise ValueError('附件用途无效')
        filename = Path(filename.replace('\\', '/')).name
        suffix = Path(filename).suffix.lower()
        if not payload or len(payload) > MAX_BYTES:
            raise ValueError('附件须为1字节至20MB')
        valid = ((suffix == '.pdf' and payload.startswith(b'%PDF-')) or
                 (suffix == '.png' and payload.startswith(b'\x89PNG\r\n\x1a\n')) or
                 (suffix in {'.jpg', '.jpeg'} and payload.startswith(b'\xff\xd8\xff')) or
                 (suffix == '.webp' and payload[:4] == b'RIFF' and payload[8:12] == b'WEBP'))
        if not valid:
            raise ValueError('请上传原始PDF或PNG/JPG/WebP图片，不支持ZIP和可执行文件')
        digest = hashlib.sha256(payload).hexdigest()
        target = self.files / (digest + suffix)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from invoice_print_layout.intake_archive import reject_inactive_digest
            reject_inactive_digest(db, digest)
            current = self.get(item_id)
            if current.get('returned_to_synthesis'):
                raise ValueError('材料已撤回，请先在合成台重新确认创建后追加材料')
            if current['deleted'] or current['stage'] != 'draft':
                raise ValueError('请先退回待提交，再追加材料')
            if not target.exists():
                temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                temporary.write_bytes(payload)
                os.replace(temporary, target)
            db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, item_id, filename, role, str(target), digest))
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            data.update(verified=False, updated_at=now())
            data.pop('review_requested', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), '收到材料：' + filename))
        return self.get(item_id)

    def set_role(self, attachment_id: str, role: str) -> None:
        if role not in ROLES or role == 'package':
            raise ValueError('附件用途无效')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT item_id,role FROM attachments WHERE id=?', (attachment_id,)).fetchone()
            if not row or row['role'] == 'package':
                raise ValueError('此附件不能修改用途')
            if self.get(row['item_id']).get('returned_to_synthesis'):
                raise ValueError('材料已撤回，请在合成台重新确认材料用途')
            data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (row['item_id'],)).fetchone()['data'])
            if data.get('deleted') or data['stage'] != 'draft':
                raise ValueError('请先退回待提交，再修改材料用途')
            db.execute('UPDATE attachments SET role=? WHERE id=?', (role, attachment_id))
            data.update(verified=False, updated_at=now())
            data.pop('review_requested', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), row['item_id']))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (row['item_id'], now(), '材料用途改为：' + ROLES[role]))

    def attachment_path(self, key: str) -> tuple[Path, str]:
        with self.connect() as db:
            row = db.execute('SELECT path,name FROM attachments WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('附件不存在')
        path = Path(row['path']).resolve()
        if not path.is_relative_to(self.workspace) or not path.is_file():
            raise ValueError('附件不可用')
        return path, row['name']

    def select(self, owner: str, item_id: str | None) -> None:
        if item_id:
            self.get(item_id)
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO selections VALUES (?,?)', (owner, item_id))

    def selected(self, owner: str) -> str | None:
        with self.connect() as db:
            row = db.execute('SELECT item_id FROM selections WHERE owner=?', (owner,)).fetchone()
        return str(row['item_id']) if row and row['item_id'] else None

    def import_history(self) -> dict[str, int]:
        """Reference successful originals/print packages; never infer reimbursement."""
        from invoice_print_layout.intake_archive import reject_inactive_digest
        added = skipped = 0
        for record in read_history(ensure_workspace(self.workspace).history):
            output = Path(str(record.get('output', ''))).resolve()
            if not output.is_relative_to(self.workspace) or not output.is_file():
                skipped += 1
                continue
            source = 'history:' + str(output)
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT data FROM expenses WHERE source=?', (source,)).fetchone()
                if existing and json.loads(existing['data']).get('history_imported'):
                    continue
                # Existing versions used legacy as the completion flag.
                if existing and json.loads(existing['data']).get('legacy'):
                    migrated = json.loads(existing['data'])
                    migrated['history_imported'] = True
                    db.execute('UPDATE expenses SET data=? WHERE source=?', (json.dumps(migrated, ensure_ascii=False), source))
                    continue
                normalized = _record_from_entry(record)
                category = normalized.category or '其他'
                category = {'咖啡': '外卖', '快递': '顺丰', '同城配送': '顺丰'}.get(category, category)
                if category not in CATEGORIES:
                    category = '其他'
                files = [(output, 'package')]
                archive = Path(str(record.get('archive', ''))).resolve()
                if archive.is_relative_to(self.workspace) and archive.is_dir():
                    for file in archive.iterdir():
                        if file.is_file() and file.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}:
                            role = ('invoice' if '发票' in file.name else 'trip' if '行程' in file.name
                                    else 'detail' if '运单' in file.name else 'other')
                            files.append((file, role))
                originals = [(file, role, hashlib.sha256(file.read_bytes()).hexdigest()) for file, role in files]
                try:
                    for _, _, digest in originals:
                        reject_inactive_digest(db, digest)
                    validated = self._validate({'title': output.stem, 'amount': normalized.amount, 'category': category,
                                               'expense_date': normalized.invoice_date, 'merchant': normalized.provider or '',
                                               'order_number': record.get('order_number', ''),
                                               'note': '历史打印记录导入；这里日期为开票日期，请核对消费日期。未推断报销状态。'})
                except ValueError:
                    skipped += 1
                    continue
                if existing:
                    data = json.loads(existing['data'])
                    data.update(legacy=True, history_imported=True)
                    db.execute('UPDATE expenses SET data=? WHERE source=?', (json.dumps(data, ensure_ascii=False), source))
                else:
                    data = validated
                    data.update(id=uuid.uuid4().hex[:10], created_at=now(), stage='draft', verified=False,
                                legacy=True, history_imported=True)
                    db.execute('INSERT INTO expenses VALUES (?,?,?)', (data['id'], source, json.dumps(data, ensure_ascii=False)))
                    db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (data['id'], now(), '登记事项'))
                for file, role, digest in originals:
                    db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                               (uuid.uuid4().hex, data['id'], file.name, role, str(file), digest))
            added += 1
        result = {'added': added, 'skipped': skipped}
        save_json(self.root / 'history-import.json', result)
        return result
