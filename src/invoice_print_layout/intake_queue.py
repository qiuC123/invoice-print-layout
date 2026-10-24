"""Durable local intake; association is atomic and never implies verification."""
from __future__ import annotations

import hashlib
import builtins
import io
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, cast
import uuid

import pymupdf
from PIL import Image

from invoice_print_layout.glm_invoice_ai import (GlmInvoiceAI, GlmAIError, FAILURE_MESSAGES, FIELD_LIMITS,
                                                OPTIONAL_EXTRACTION_FIELDS, labelled_invoice_numbers,
                                                optional_material_issue)
from invoice_print_layout.receipt_intake import (validate_image, parse_receipt, read_receipt,
                                               rows_of, labelled_counterparty)
from invoice_print_layout.documents import parse_invoice_number
from invoice_print_layout.intake_usage import rail_payment_not_required, RAIL_PAYMENT_UNUSED_REASON
from invoice_print_layout.intake_archive import IntakeArchive, INACTIVE_STATUSES
from invoice_print_layout.transaction_evidence import extract_transaction_evidence, record_identifier_keys, identifiers_for_record
from invoice_print_layout.workbench import ExpenseStore, MAX_BYTES, ROLES, now, amount_cents


FIELD_LABELS = {'amount': '金额', 'merchant': '商家名称', 'expense_date': '消费/付款日期',
                'invoice_number': '发票号码', 'order_number': '订单号', 'payment_status': '付款状态',
                'category': '费用类别', 'goods': '购买内容', 'title': '费用名称', 'note': '备注',
                'shopping_platform': '购物平台', 'shop_name': '实际店铺', 'payment_channel': '支付渠道'}
ORDER_NUMBER_NOTE = '订单号用于区分记录和避免重复；不同体系的编号无需一致，配对结合平台、店铺和金额。'
CONFLICT_FIELDS = frozenset({'amount', 'merchant', 'shop_name', 'invoice_number', 'payment_status'})


def _readable_field_issue(issue: str) -> str:
    """Translate established diagnostic field keys without altering proof text."""
    for field, label in FIELD_LABELS.items():
        if issue == 'AI与原件字段冲突：' + field:
            return 'AI与本地识别的' + label + '不同，请对照原件确认。'
        for prefix in ('AI字段未能在提取出的文字中核对：', '已忽略未能核对原文的可选字段：'):
            if issue == prefix + field:
                return prefix + label
    return issue


def _optional_order_issue(issue: str) -> bool:
    """Different order/payment identifier namespaces alone do not block a pair."""
    if not any(marker in issue for marker in ('订单号', '交易单号', '交易号', '流水号', '支付编号')):
        return False
    # Retain actual mixed transactions and unrelated core-proof uncertainty.
    # A payment order and merchant order may both occur on a normal single bill.
    if (any(marker in issue for marker in ('多笔', '多个', '多份', '多张', '两笔', '两个订单',
            '混有', '歧义', '无法区分', '不能区分', '退款', '付款失败', '未付款', '未支付',
            '付款状态', '支付状态', '发票号码', '收款身份', '材料用途', '凭证用途')) or
            re.search(r'(?:金额|商家|店铺|销售方).{0,8}(?:冲突|不一致|不同|不符)', issue)):
        return False
    return any(marker in issue for marker in ('缺', '未显示', '未提供', '未见', '未识别', '未能识别',
               '无法识别', '不同', '不一致', '不相同', '不符', '无法建立', '无法对应', '唯一对应', '不能建立'))


def positioned_invoice_seller(page: pymupdf.Page) -> str:
    """Read a tax PDF's labelled seller name without assuming right means seller.

    Native PDF stream order commonly separates all labels from their values.
    Require a unique explicit seller column and its name label, then read only
    the name's baseline. Missing or ambiguous geometry remains blank.
    """
    raw_words = page.get_text('words')  # type: ignore[no-untyped-call]
    words = [(pymupdf.Rect(*word[:4]), str(word[4]).strip())  # type: ignore[no-untyped-call]
             for word in raw_words if len(word) >= 5 and str(word[4]).strip()]
    compact = [(bounds, re.sub(r'\s+', '', value)) for bounds, value in words]
    markers = [bounds for bounds in page.search_for('销售方')
               if bounds.width > bounds.height * 2]
    # Tax forms print the column title vertically, often as five one-character
    # PDF words. Also accept the same explicit title split across a text row.
    for start, value in compact:
        if value != '销':
            continue
        for vertical in (True, False):
            sequence = [start]
            previous = start
            for character in '售方':
                choices = []
                for bounds, text in compact:
                    if text != character:
                        continue
                    if vertical:
                        aligned = abs((bounds.x0 + bounds.x1 - previous.x0 - previous.x1) / 2) <= max(previous.width, bounds.width) * .6
                        gap = bounds.y0 - previous.y1
                        limit = max(previous.height, bounds.height) * 1.6
                    else:
                        aligned = abs((bounds.y0 + bounds.y1 - previous.y0 - previous.y1) / 2) <= max(previous.height, bounds.height) * .5
                        gap = bounds.x0 - previous.x1
                        limit = max(previous.width, bounds.width) * 1.6
                    if aligned and -1 <= gap <= limit:
                        choices.append(bounds)
                if len(choices) != 1:
                    break
                previous = choices[0]
                sequence.append(previous)
            if len(sequence) == 3:
                combined = pymupdf.Rect(min(r.x0 for r in sequence), min(r.y0 for r in sequence),  # type: ignore[no-untyped-call]
                                        max(r.x1 for r in sequence), max(r.y1 for r in sequence))
                if combined not in markers:
                    markers.append(combined)
    labels = page.search_for('名称')
    names = [bounds for marker in markers for bounds in labels
             if marker.x1 - 2 <= bounds.x0 <= marker.x1 + page.rect.width * .2
             and bounds.y1 >= marker.y0 - 4 and bounds.y0 <= marker.y1 + 4]
    if len(names) != 1:
        return ''
    anchor = names[0]
    right = min((r.x0 for r in labels if r.x0 > anchor.x1 and
                 r.y1 >= anchor.y0 - 2 and r.y0 <= anchor.y1 + 2), default=page.rect.x1)
    candidates = []
    for bounds, value in words:
        if bounds.height > anchor.height * 1.8 or abs(
                (bounds.y0 + bounds.y1 - anchor.y0 - anchor.y1) / 2) > max(anchor.height, bounds.height) * .4:
            continue
        # MuPDF may join a label and value into one word, even when separate PDF
        # drawing calls supplied them. The labelled substring's native geometry
        # still distinguishes it from the buyer's identical name label.
        if bounds.x0 <= anchor.x0 + 1 and bounds.x1 > anchor.x1 and '名称' in value:
            value = re.split(r'名称\s*[:：]?', value, maxsplit=1)[1].strip()
            bounds = pymupdf.Rect(anchor.x1, bounds.y0, bounds.x1, bounds.y1)  # type: ignore[no-untyped-call]
        value = value.lstrip(':：').strip()
        if value and bounds.x0 >= anchor.x1 - 5 and bounds.x1 <= right + 1 and bounds not in markers:
            candidates.append((bounds, value))
    candidates.sort(key=lambda entry: entry[0].x0)
    if not candidates or candidates[0][0].x0 - anchor.x1 > max(anchor.height * 6, 40) or any(
            b.x0 < a.x1 - 2 or b.x0 - a.x1 > max(a.height, b.height) * 1.6
            for (a, _), (b, _) in zip(candidates, candidates[1:])):
        return ''
    name = ' '.join(value for _, value in candidates)
    name = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', name)
    if len(name) > 200 or not re.search(r'[A-Za-z\u4e00-\u9fff]', name) or re.search(
            r'[:：]|名称|税号|识别号|开户|账号|地址|电话|发票号码|开票日期', name):
        return ''
    return name


def payment_state_text(text: str) -> str:
    """An available refund action is not an actual refunded/pending status."""
    # Strip only a standalone control, preserving status-bearing phrases such
    # as 已申请退款 / 当前状态：退款申请 and all other original wording.
    controls = re.compile(r'[\t ]*(?:可申请退款|申请退款|退款申请)[\t ]*[›>↗]?[\t ]*')
    state_labels = {'当前状态', '支付状态', '付款状态', '交易状态', '退款状态'}
    result = []
    previous = ''
    for line in text.splitlines(keepends=True):
        visible = line.rstrip('\r\n')
        compact = re.sub(r'\s+', '', visible).rstrip(':：')
        if controls.fullmatch(visible) and previous not in state_labels:
            result.append(line[len(visible):])
        else:
            result.append(line)
        if compact:
            previous = compact
    return ''.join(result)


class _BatchClaimReceiver:
    """Check the QR parent's version atomically when downloaded PDF is queued."""
    def __init__(self, queue: IntakeQueue, source: dict[str, Any], url: str):
        self.queue, self.source = queue, {**source, 'claim_url': url}

    def receive(self, name: str, payload: bytes, *, source: str = 'upload', project_id: str = '',
                batch: str = '') -> dict[str, Any]:
        return self.queue.receive(name, payload, source=source, project_id=project_id, batch=batch,
                                  expected_source=self.source)


class IntakeQueue:
    def __init__(self, store: ExpenseStore, ai: GlmInvoiceAI | None = None):
        self.store = store
        self.ai = ai if ai is not None else GlmInvoiceAI(store.root)
        self.folder = store.root / '统一收件'
        self.folder.mkdir(exist_ok=True)
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS intake_queue (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')
            from invoice_print_layout.expense_projects import install_project_guards
            install_project_guards(db)

    def list(self) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            return [self._visible_entry(json.loads(row['data']))
                    for row in db.execute('SELECT data FROM intake_queue ORDER BY rowid DESC')]

    @staticmethod
    def _visible_entry(data: dict[str, Any]) -> dict[str, Any]:
        # Hide this retired diagnostic in old rows without rewriting their
        # ledger state or requiring all original materials to be read again.
        if '商品不明' in data.get('issues', []):
            data['issues'] = [issue for issue in data['issues'] if issue != '商品不明']
        return data

    def get(self, key: str) -> dict[str, Any]:
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM intake_queue WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('收件不存在')
            return self._visible_entry(json.loads(row['data']))

    def archive_action(self, key: str, revision: Any, project_id: Any, action: str) -> dict[str, Any]:
        result = IntakeArchive(self.store).transition(key, revision, project_id, action)
        result['entry'] = self._visible_entry(result['entry'])
        return result

    def receive(self, name: str, payload: bytes, *, source: str = 'upload', project_id: str = '', batch: str = '',
                expected_source: dict[str, Any] | None = None) -> dict[str, Any]:
        name = Path(name.replace('\\', '/')).name
        if not payload or len(payload) > MAX_BYTES:
            raise ValueError('附件须为20MB以内文件')
        suffix = Path(name).suffix.lower()
        if suffix == '.pdf':
            with pymupdf.open(stream=payload, filetype='pdf') as pdf:  # type: ignore[no-untyped-call]
                if pdf.needs_pass or not pdf.page_count:
                    raise ValueError('PDF加密或没有页面')
        else:
            validate_image(name, payload)
        if project_id:
            from invoice_print_layout.expense_projects import ExpenseProjects
            if not any(p['id'] == project_id for p in ExpenseProjects(self.store).list()):
                raise ValueError('项目不存在')
        digest = hashlib.sha256(payload).hexdigest()
        target = self.folder / (digest + suffix)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            from invoice_print_layout.expense_projects import assert_project_active, assert_material_not_permanently_deleted
            assert_project_active(db, project_id)
            assert_material_not_permanently_deleted(db, digest)
            if expected_source is not None:
                parent = db.execute('SELECT data FROM intake_queue WHERE id=?', (expected_source['id'],)).fetchone()
                current_source = json.loads(parent['data']) if parent else {}
                if (current_source.get('revision') != expected_source['revision'] or
                        current_source.get('digest') != expected_source['digest'] or
                        current_source.get('project_id') != expected_source.get('project_id') or
                        current_source.get('status') not in {'received', 'review'} or
                        current_source.get('item_id')):
                    raise ValueError('二维码材料已变化，请刷新合成台后重试')
            previous = db.execute('SELECT data FROM intake_queue WHERE digest=?', (digest,)).fetchone()
            if previous:
                prior = json.loads(previous['data'])
                if expected_source is not None:
                    self._record_batch_claim(db, expected_source, prior)
                return {**prior, 'duplicate': True}
            attached = db.execute('SELECT item_id FROM attachments WHERE digest=?', (digest,)).fetchone()
            if not target.exists():
                temp = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                try:
                    temp.write_bytes(payload)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
            data: dict[str, Any] = {'id': uuid.uuid4().hex, 'digest': digest, 'name': name, 'path': str(target),
                    'source': source, 'batch': batch, 'project_id': project_id, 'received_at': now(),
                    'status': 'duplicate' if attached else 'received', 'revision': 0,
                    'item_id': attached['item_id'] if attached else '', 'fields': {}, 'role': 'other',
                    'issues': [], 'text': '', 'result': '重复文件' if attached else '已保存，等待识别'}
            if expected_source is not None:
                from invoice_print_layout.reimbursement_people import person_fields
                data.update(person_fields(expected_source))
            db.execute('INSERT INTO intake_queue VALUES (?,?,?)', (data['id'], digest, json.dumps(data, ensure_ascii=False)))
            if expected_source is not None:
                self._record_batch_claim(db, expected_source, data)
        return data

    @staticmethod
    def _record_batch_claim(db: sqlite3.Connection, source: dict[str, Any], target: dict[str, Any]) -> None:
        # Creation and replacement linkage share receive's immediate transaction.
        # A parent cannot be archived in the gap between these two writes.
        if target['status'] in INACTIVE_STATUSES:
            raise ValueError('二维码对应发票已归档或删除，请在归档区处理')
        if target.get('project_id') and source.get('project_id') and target['project_id'] != source['project_id']:
            return  # Existing cross-project handling leaves the carrier manual.
        from invoice_print_layout.reimbursement_people import common_person
        common_person([source, target])
        source_ids = list(dict.fromkeys([*target.get('source_entry_ids', []), source['id']]))
        target.update(source_entry_id=target.get('source_entry_id') or source['id'], source_entry_ids=source_ids,
                      revision=target['revision'] + 1)
        if not target.get('batch'):
            target['batch'] = source.get('batch', '')
        if not target.get('project_id') and not target.get('item_id'):
            target['project_id'] = source.get('project_id', '')
        current = {**source, 'status': 'converted', 'qr_status': 'converted', 'replacement_id': target['id'],
                   'role': 'other', 'fields': {}, 'issues': [], 'revision': source['revision'] + 1,
                   'result': '二维码已替换为原始发票PDF；来源图保留，不作为另一份票据。'}
        for entry in (current, target):
            db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                       (json.dumps(entry, ensure_ascii=False), entry['id']))

    def analyze(self, key: str) -> dict[str, Any]:
        # AI intake is deliberately the single-proof batch path. The legacy
        # organizer scans other files and can create expenses before matching.
        if self.ai.enabled or any(entry.get('reimbursement_person_id') for entry in self.list()):
            return self.prepare_batch_entry(key)
        data = self.get(key)
        if data['status'] in {'linked', 'duplicate'} | INACTIVE_STATUSES:
            return data
        original_fields = dict(data.get('fields') or {})
        manual = self._manual_keys(data)
        path = Path(data['path'])
        if path.suffix == '.pdf':
            from invoice_print_layout.automatic_rides import organize_rides
            organize_rides(self.store, folder=self.folder)
            self.reconcile()
            data = self.get(key)
            if data['status'] in {'linked', 'duplicate'} | INACTIVE_STATUSES:
                return data
        try:
            if path.suffix == '.pdf':
                with pymupdf.open(path) as pdf:  # type: ignore[no-untyped-call]
                    text = '\n'.join(page.get_text() for page in list(pdf)[:10])
                from invoice_print_layout.takeout import OcrLine
                lines = [OcrLine(line, 0, i * 20, 500, i * 20 + 15) for i, line in enumerate(text.splitlines())]
                parsed = parse_receipt(lines)
                compact = re.sub(r'\s+', '', text)
                number = re.search(r'发票号码[:：]?(\d{8,30})', compact)
                if number:
                    parsed['role'] = 'invoice'
                    parsed['fields']['invoice_number'] = number.group(1)
                amount = re.search(r'(?:小写|价税合计)[)）:：]*[¥￥]?(\d+\.\d{2})', compact)
                if amount:
                    parsed['fields']['amount'] = amount.group(1)
                if '铁路电子客票' in compact or '铁路客运' in compact:
                    parsed['fields']['category'] = '高铁'
                    parsed['fields']['title'] = '铁路车票'
                elif '住宿' in compact or '客房费' in compact:
                    parsed['fields']['category'] = '酒店'
                    parsed['fields']['title'] = '酒店住宿'
            else:
                lines = read_receipt(path.read_bytes(), path.suffix)
                parsed = parse_receipt(lines)
                text = '\n'.join(line.text for line in lines)
            fields = parsed['fields']
            issues = []
            if not fields.get('amount'):
                issues.append('金额冲突')
            if not text.strip():
                issues.append('手写难辨')
            if not data['project_id']:
                issues.append('归属不明')
            data.update(fields=fields, role=parsed['role'], text=text[:24000], issues=issues,
                        status='review', result='待确认' if issues else '识别完成')
            for field in manual:
                if field in original_fields:
                    data['fields'][field] = original_fields[field]
            source = 'native_pdf' if path.suffix.lower() == '.pdf' else 'local_ocr'
            data['field_sources'] = {field: 'manual' if field in manual else source
                                     for field, value in data['fields'].items() if value}
            self._project_transaction_evidence(data, manual, source)
        except (ValueError, OSError, RuntimeError) as exc:
            data.update(status='review', issues=['手写难辨'], result='识别未完成：' + str(exc)[:200])
        if rail_payment_not_required(data):
            data.update(status='unused', unused_reason=RAIL_PAYMENT_UNUSED_REASON,
                        result=RAIL_PAYMENT_UNUSED_REASON)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(key)
            if current['revision'] != data['revision'] or current['status'] in INACTIVE_STATUSES:
                raise ValueError('收件已变化，请刷新')
            if data['status'] == 'unused' and hashlib.sha256(path.read_bytes()).hexdigest() != data['digest']:
                raise ValueError('材料原件已变化，请重新上传')
            data['revision'] += 1
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
        return self.organize(key)

    def _save_batch_qr_manual(self, data: dict[str, Any], url: str, message: str,
                              *, replacement_id: str = '') -> dict[str, Any]:
        """Keep a QR carrier visible without treating it as a payment or invoice."""
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(url)
            safe_url = url if (parsed.scheme in {'http', 'https'} and parsed.hostname and
                               not parsed.username and not parsed.password) else ''
        except ValueError:
            safe_url = ''
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(data['id'])
            if current['revision'] != data['revision'] or current['status'] in INACTIVE_STATUSES:
                raise ValueError('收件已变化，请刷新后重试')
            current.update(status='review', qr_status='manual', claim_url=safe_url,
                           role='other', fields={}, issues=['领票待处理'], result=message,
                           revision=current['revision'] + 1)
            if replacement_id:
                current['replacement_id'] = replacement_id
            db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                       (json.dumps(current, ensure_ascii=False), current['id']))
        return current

    def analyze_local(self, key: str) -> dict[str, Any]:
        """Read one local proof without claiming QR URLs or organizing other files.

        Use an independent disabled AI adapter so desktop configuration and
        simultaneous requests cannot enable network extraction on this path.
        """
        from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
        local = IntakeQueue(self.store, ai=LocalOnlyInvoiceAI())
        data = local.get(key)
        if data['status'] in {'linked', 'duplicate', 'converted'} | INACTIVE_STATUSES:
            return data
        return (local._analyze_batch_pdf(data) if Path(data['path']).suffix.lower() == '.pdf'
                else local._analyze_batch_image(data))

    def prepare_batch_entry(self, key: str, *, force_ai: bool = False) -> dict[str, Any]:
        """Prepare an uploaded proof locally; replace supported tax QR images once.

        Converted source QR entries remain archived with their replacement id,
        while callers receive the effective PDF entry. Unknown, expired and
        login-bound QR carriers remain manual and are never auto-organized.
        Ordinary proofs use local OCR/PDF analysis of this entry only.
        """
        from invoice_print_layout.invoice_claim import claim
        from invoice_print_layout.mail_search import qr_value
        if type(force_ai) is not bool:
            raise ValueError('AI辅助识别选项须为布尔值')
        data = self.get(key)
        if data['status'] in {'linked', 'duplicate'} | INACTIVE_STATUSES:
            return data
        if data.get('qr_status') == 'converted' and data.get('replacement_id'):
            replacement = self.get(data['replacement_id'])
            return (replacement if not force_ai or replacement['status'] in {'linked', 'duplicate'} else
                    self.prepare_batch_entry(replacement['id'], force_ai=force_ai))
        path = Path(data['path'])
        value = qr_value(path.read_bytes()) if path.suffix.lower() != '.pdf' else ''
        if not value:
            # A previously decoded QR must not become a fictitious proof merely
            # because a later decoder attempt fails.
            if data.get('qr_status') == 'manual':
                return data
            return (self._analyze_batch_image(data, force_ai=force_ai) if path.suffix.lower() != '.pdf'
                    else self._analyze_batch_pdf(data, force_ai=force_ai))
        try:
            result = claim(cast(IntakeQueue, _BatchClaimReceiver(self, data, value)), value, data.get('project_id', ''))
        except (ValueError, OSError, RuntimeError) as exc:
            return self._save_batch_qr_manual(data, value, '自动领票未完成：' + str(exc)[:200])
        if result['status'] != 'received':
            return self._save_batch_qr_manual(data, value, result['message'])
        replacement = result['entry']
        current_source = self.get(key)
        if current_source.get('qr_status') == 'converted' and current_source.get('replacement_id') == replacement['id']:
            target = self.get(replacement['id'])
            return (target if target['status'] in {'linked', 'duplicate'} | INACTIVE_STATUSES
                    else self._analyze_batch_pdf(target, force_ai=force_ai))
        if (replacement.get('project_id') and data.get('project_id') and
                replacement['project_id'] != data['project_id']):
            return self._save_batch_qr_manual(data, value, '对应发票已属于其他项目，请人工核对归属。',
                                              replacement_id=replacement['id'])
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(key)
            if (current['revision'] != data['revision'] or current['digest'] != data['digest'] or
                    current.get('project_id') != data.get('project_id') or current.get('item_id') != data.get('item_id') or
                    current['status'] in {'linked', 'duplicate', 'converted'} | INACTIVE_STATUSES):
                raise ValueError('收件已变化，请刷新后重试')
            target = self.get(replacement['id'])
            if target['revision'] != replacement['revision'] or target['digest'] != replacement['digest']:
                raise ValueError('二维码对应发票已变化，请刷新后重试')
            if target['status'] in INACTIVE_STATUSES:
                raise ValueError('二维码对应发票已归档或删除，请在归档区处理')
            source_ids = list(dict.fromkeys([*target.get('source_entry_ids', []), key]))
            target.update(source_entry_id=target.get('source_entry_id') or key, source_entry_ids=source_ids,
                          revision=target['revision'] + 1)
            if not target.get('batch'):
                target['batch'] = data.get('batch', '')
            if not target.get('project_id') and not target.get('item_id'):
                target['project_id'] = data.get('project_id', '')
            current.update(status='converted', qr_status='converted', replacement_id=target['id'],
                           claim_url=value, role='other', fields={}, issues=[], revision=current['revision'] + 1,
                           result='二维码已替换为原始发票PDF；来源图保留，不作为另一份票据。')
            for entry in (current, target):
                db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                           (json.dumps(entry, ensure_ascii=False), entry['id']))
        return (self.get(target['id']) if target['status'] in {'linked', 'duplicate'}
                else self._analyze_batch_pdf(self.get(target['id']), force_ai=force_ai))

    def _analyze_batch_pdf(self, data: dict[str, Any], *, force_ai: bool = False) -> dict[str, Any]:
        """Read one PDF with mature invoice parsers, without organizing older inbox files."""
        original = dict(data)
        data.pop('qr_check', None)
        data.pop('company_check', None)
        images: list[bytes] | None = None
        complete = True
        from invoice_print_layout.documents import (classify_text, compact_text,
                parse_amount, parse_invoice_number, _parse_positioned_invoice_amount, PdfInspectionError)
        from invoice_print_layout.models import DocumentKind
        from invoice_print_layout.takeout import OcrLine
        try:
            with pymupdf.open(Path(data['path'])) as document:  # type: ignore[no-untyped-call]
                if document.needs_pass or not len(document):
                    raise ValueError('PDF加密或没有页面')
                complete = len(document) <= 10
                pages = list(document)[:10]
                page_texts = [page.get_text() for page in pages]
                text = '\n'.join(page_texts)
                lines: list[OcrLine] = []
                offset = 0.0
                for page in pages:
                    lines.extend(OcrLine(str(word[4]), int(round(word[0])), int(round(word[1] + offset)),
                                         int(round(word[2])), int(round(word[3] + offset))) for word in page.get_text('words'))
                    offset += float(page.rect.height) + 30
                parsed = parse_receipt(lines)
                fields = parsed['fields']
                data['local_evidence'] = dict(parsed.get('evidence', {}))
                issues = [warning for warning in parsed.get('warnings', []) if warning in
                          {'收款身份待核对', '销售方名称待核对'}]
                def invoice_header(value: str) -> bool:
                    rows = [compact_text(row).upper() for row in value.splitlines() if row.strip()][:12]
                    return any(re.fullmatch(r'(?:电子发票(?:[（(].*[）)])?|增值税(?:电子)?(?:普通|专用)发票|'
                                            r'ELECTRONICINVOICE|铁路电子客票)', row) for row in rows)
                headers = [number for number, value in enumerate(page_texts) if invoice_header(value)]
                if headers:
                    parsed['role'] = 'invoice'
                    invoice_text = page_texts[headers[0]]
                    fields['amount'] = ''
                    fields.pop('invoice_number', None)
                    if len(headers) > 1:
                        issues.append('同文件含多张发票，请拆分核对')
                    else:
                        try:
                            try:
                                # Tax PDFs often extract independent values after all
                                # labels; the last currency value can be the tax, not
                                # the total. Use the printed small-total position first.
                                amount = _parse_positioned_invoice_amount(pages[headers[0]])
                            except PdfInspectionError:
                                # Only directly labelled values on a text row are
                                # safe without coordinates. Never use currency-last.
                                labelled = {
                                    parse_amount('TOTAL WITH TAX:' + match[1], DocumentKind.INVOICE)
                                    for row in invoice_text.splitlines()
                                    for match in re.finditer(
                                        r'(?:[（(]?小写[）)]?|价税合计|TOTALWITHTAX)[:：]?[¥￥]?'
                                        r'([0-9]+(?:\.[0-9]{1,2})?)(?![0-9.])',
                                        compact_text(row), flags=re.IGNORECASE)
                                }
                                if len(labelled) != 1:
                                    raise PdfInspectionError('无法确认唯一的发票价税合计')
                                amount = labelled.pop()
                            fields['amount'] = f'{amount:.2f}'
                        except PdfInspectionError:
                            issues.append('发票价税合计待核对')
                        if len(labelled_invoice_numbers(invoice_text)) > 1:
                            issues.append('发票号码冲突，请拆分材料或对照原件确认。')
                        else:
                            try:
                                fields['invoice_number'] = parse_invoice_number(invoice_text)
                            except PdfInspectionError:
                                issues.append('发票号码待核对')
                    sellers = {seller[1].strip() for row in invoice_text.splitlines()
                        if (seller := re.fullmatch(r'\s*(?:销售方名称|销方名称|销售方|SELLER)\s*[:：]\s*(.{1,200})',
                                                 row, flags=re.IGNORECASE))}
                    if len(sellers) == 1:
                        fields['merchant'] = sellers.pop()
                    elif len(sellers) > 1:
                        fields['merchant'] = ''
                        issues.append('销售方名称待核对')
                    if not fields.get('merchant') and len(headers) == 1:
                        fields['merchant'] = positioned_invoice_seller(pages[headers[0]])
                    fields['title'] = (fields['merchant'] + ' · 发票') if fields.get('merchant') else '电子发票（费用类别待核对）'
                    if '铁路电子客票' in compact_text(invoice_text):
                        fields.update(category='高铁', title='铁路车票')
                    if len(headers) == 1:
                        from invoice_print_layout.tax_categories import classify_tax, human_category
                        if (original.get('field_sources', {}).get('category') in
                                {'manual', 'human', 'experience', 'preference', 'category_experience', 'expense_preferences'}
                                and original.get('fields', {}).get('category')):
                            fields['category'] = original['fields']['category']
                        if (fields.get('category') in {'', '其他', None} and 'category' not in self._manual_keys(original) and
                                original.get('field_sources', {}).get('category') not in
                                {'manual', 'human', 'experience', 'preference', 'category_experience', 'expense_preferences'}):
                            learned, has_human = human_category(self.store, original, fields, invoice_text)
                            rule = classify_tax(invoice_text)
                            if has_human:
                                if learned:
                                    fields['category'] = learned
                                    data['local_evidence']['category'] = {'source': 'human_experience', 'category': learned}
                            elif rule['category']:
                                fields['category'] = rule['category']
                                fields['title'] = rule['category'] + ' · ' + (fields.get('merchant') or '发票')
                                data['local_evidence']['category'] = rule
                            elif rule['tax_classes']:
                                data['local_evidence']['category'] = rule
                        from invoice_print_layout.invoice_checks import issue_date, read_tax_qr, compare_qr
                        from invoice_print_layout.company_profile import check_pdf
                        data['company_check'] = check_pdf(self.store.root, pages[headers[0]])
                        if data['company_check'] and data['company_check']['status'] == 'mismatch':
                            issues.append('发票抬头/税号与公司信息不一致')
                        fields['issue_date'] = issue_date(pages[headers[0]])
                        try:
                            data['qr_check'] = compare_qr(read_tax_qr(pages[headers[0]]), fields)
                        except (ValueError, OSError, RuntimeError):
                            data['qr_check'] = compare_qr(None, fields)
                        if data['qr_check']['mismatches']:
                            labels = {'invoice_number': '号码', 'amount': '金额', 'issue_date': '日期'}
                            issues.append('二维码与正文不一致：' + '、'.join(labels[key] for key in data['qr_check']['mismatches']))
                else:
                    # An invoice number copied into a waybill is not an invoice.
                    if parsed['role'] == 'invoice':
                        parsed['role'] = 'other'
                    fields.pop('invoice_number', None)
                    try:
                        kind = classify_text(text)
                    except PdfInspectionError:
                        kind = None
                    if kind is DocumentKind.TRIP:
                        parsed['role'] = 'trip'
                        fields.update(category='打车', title='行程单（对应发票待核对）')
                        fields['amount'] = ''
                        try:
                            fields['amount'] = f'{parse_amount(text, DocumentKind.TRIP):.2f}'
                        except PdfInspectionError:
                            issues.append('行程合计待核对')
                    elif any(marker in text for marker in ('运单明细', '运费明细')):
                        parsed['role'] = 'detail'
                if not fields.get('amount'):
                    issues.append('金额冲突')
                if not text.strip():
                    issues.append('手写难辨')
                if not data['project_id']:
                    issues.append('归属不明')
                data.pop('amount_cents', None)
                data.update(fields=fields, role=parsed['role'], text=text[:24000], issues=issues,
                            status='review', result='待确认' if issues else '识别完成')
                self._complete_local_evidence(data, text.splitlines())
                if len(text) > 24000:
                    complete = False
                if self.ai.enabled and not text.strip():
                    # Do not silently treat the first four scans as a complete
                    # multi-page invoice. No page of such a file is uploaded.
                    complete = len(document) <= 4
                    if complete:
                        images = []
                        for page in document:
                            scale = min(1.5, 2000 / max(page.rect.width, page.rect.height))
                            rendered = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes('png')  # type: ignore[no-untyped-call]
                            images.append(self._bounded_vision_png(rendered))
        except (ValueError, OSError, RuntimeError) as exc:
            data.update(previous_analysis={'role': data.get('role', 'other'), 'fields': data.get('fields', {}),
                                          'text': data.get('text', '')},
                        role='other', fields={}, text='', status='review', issues=['手写难辨'],
                        result='识别未完成：' + str(exc)[:200])
            data.pop('amount_cents', None)
        return self._finish_batch_analysis(data, original, images=images, complete=complete,
                                           native_source='native_pdf', force_ai=force_ai)

    def _analyze_batch_image(self, data: dict[str, Any], *, force_ai: bool = False) -> dict[str, Any]:
        """Read payment/receipt details locally without promoting unsuccessful payments."""
        original = dict(data)
        path = Path(data['path'])
        complete = True
        try:
            lines = read_receipt(path.read_bytes(), path.suffix)
            parsed = parse_receipt(lines)
            visible_text = '\n'.join(rows_of(lines))
            rows = [re.sub(r'\s+', '', row).replace('￥', '¥').replace('−', '-') for row in rows_of(lines)]
            text = '\n'.join(rows)
            complete = len(visible_text) <= 24000
            fields = parsed['fields']
            data['local_evidence'] = dict(parsed.get('evidence', {}))
            extra_issues = [warning for warning in parsed.get('warnings', []) if warning in
                            {'收款身份待核对', '销售方名称待核对'}]
            payment = any(marker in text for marker in ('微信支付', '支付宝', '支付成功', '付款成功', '交易成功',
                          '对方已收钱', '转账', '交易单号', '支付方式', '付款详情', '付款金额', '支付金额'))
            receipt = '收据' in text or '收款凭证' in text
            invoice = '发票号码' in text and any(marker in text for marker in ('电子发票', '增值税'))
            if invoice:
                numbers = labelled_invoice_numbers(text)
                if len(numbers) == 1:
                    fields['invoice_number'] = next(iter(numbers))
                elif len(numbers) > 1:
                    fields.pop('invoice_number', None)
                    extra_issues.append('发票号码冲突，请拆分材料或对照原件确认。')
            state_text = re.sub(r'\s+', '', payment_state_text(visible_text))
            failed = any(marker in state_text for marker in ('退款', '退回', '已撤销', '交易关闭', '交易失败', '支付失败',
                         '付款失败', '已取消', '等待对方收款', '待对方收款', '等待付款', '未付款', '未支付', '待支付', '待付款', '转账中'))
            if payment and failed and not invoice:
                parsed['role'] = 'other'
                fields.update(amount='', payment_status='uncertain')
                extra_issues.append('付款状态待确认')
            elif receipt and not invoice:
                parsed['role'] = 'receipt'
                if fields.get('title') in {'', '待核对购买凭证'}:
                    fields['title'] = '收据（购买内容待核对）'
            elif payment and not invoice:
                succeeded = any(marker in text for marker in ('支付成功', '付款成功', '交易成功', '对方已收钱',
                                     '已收钱', '转账成功', '支付完成'))
                fields['payment_status'] = 'succeeded' if succeeded and not failed else 'uncertain'
                parsed['role'] = 'payment' if succeeded and not failed else 'other'
                fields['amount'] = ''
                if failed or not succeeded:
                    extra_issues.append('付款状态待确认')
                else:
                    amounts: list[tuple[int, str, int]] = []
                    for index, row in enumerate(rows):
                        labelled = re.fullmatch(r'(?:付款金额|支付金额|转账金额|交易金额|实付款|实付金额)[:：]?¥?-?(\d+(?:\.\d{1,2})?)元?', row)
                        bare = re.fullmatch(r'¥?(-?)(\d+\.\d{1,2})(?:元)?', row)
                        if labelled:
                            amounts.append((3, labelled[1], index))
                        elif bare:
                            amounts.append((2 if bare[1] == '-' else 1, bare[2], index))
                    if amounts:
                        best = max(amount[0] for amount in amounts)
                        candidates = {f'{amount_cents(number) / 100:.2f}' for priority, number, _ in amounts if priority == best}
                        if len(candidates) == 1:
                            fields['amount'] = candidates.pop()
                        elif self.ai.enabled:
                            extra_issues.append('多个付款金额待核对')
                    merchant, merchant_quote, merchant_conflict = labelled_counterparty(rows)
                    if merchant:
                        fields['merchant'] = merchant
                        data['local_evidence']['merchant'] = merchant_quote
                    if merchant_conflict:
                        fields['merchant'] = ''
                        extra_issues.append('收款身份待核对')
                    if not merchant_conflict and not fields.get('merchant') and fields.get('amount'):
                        amount_index = next(index for priority, _, index in amounts if priority == best)
                        if amount_index > 0:
                            preceding = rows[amount_index - 1]
                            labels = {'微信支付', '支付宝', '转账', '付款详情', '账单详情', '交易详情', '支付成功',
                                      '付款成功', '交易成功', '对方已收钱', '二维码付款'}
                            if preceding not in labels and re.fullmatch(r'[^\d:：¥]{1,160}', preceding):
                                fields['merchant'] = preceding
                fields['title'] = (fields.get('merchant') + ' · 付款截图') if fields.get('merchant') else '付款截图（收款方待核对）'
            issues = list(extra_issues)
            if not fields.get('amount'):
                issues.append('金额冲突')
            if not text.strip():
                issues.append('手写难辨')
            if not data['project_id']:
                issues.append('归属不明')
            data.update(fields=fields, role=parsed['role'], text=visible_text[:24000], issues=issues,
                        status='review', result='付款状态待确认' if extra_issues else '待确认' if issues else '识别完成')
            self._complete_local_evidence(data, rows_of(lines))
        except (ValueError, OSError, RuntimeError) as exc:
            data.update(previous_analysis={'role': data.get('role', 'other'), 'fields': data.get('fields', {}),
                                          'text': data.get('text', '')},
                        role='other', fields={}, text='', status='review', issues=['手写难辨'],
                        result='识别未完成：' + str(exc)[:200])
            data.pop('amount_cents', None)
        return self._finish_batch_analysis(data, original, complete=complete,
                                           native_source='local_ocr', force_ai=force_ai)

    @staticmethod
    def _project_transaction_evidence(data: dict[str, Any], manual: set[str], source: str) -> None:
        """Keep platform, actual shop and payment channel as separate source roles."""
        fields = data.setdefault('fields', {})
        source_role = data.get('transaction_source_role') or data.get('role') or 'other'
        extracted = extract_transaction_evidence(str(data.get('text') or ''),
                                                 str(source_role), fields)
        quotes = data.setdefault('ai_evidence' if source == 'glm-5.3-flash' else 'local_evidence', {})
        sources = data.setdefault('field_sources', {})
        transaction_quotes: dict[str, str] = {}
        for key in ('shopping_platform', 'shop_name', 'payment_channel'):
            value, quote = extracted['fields'].get(key, ''), extracted['evidence'].get(key, '')
            if key in manual:
                sources[key] = 'manual'
                # Never attach a local quote to an independently entered value.
                if value and fields.get(key) == value and quote:
                    transaction_quotes[key] = quote
                continue
            if value and quote:
                fields[key], quotes[key], sources[key] = value, quote, source
                transaction_quotes[key] = quote
            else:
                fields.pop(key, None)
                quotes.pop(key, None)
                sources.pop(key, None)
        # Backwards-compatible merchant storage is left intact when it names a
        # platform/recipient. A missing merchant may use an explicit actual shop.
        if 'merchant' not in manual and not fields.get('merchant') and fields.get('shop_name'):
            fields['merchant'] = fields['shop_name']
            quotes['merchant'] = quotes.get('shop_name', '')
            sources['merchant'] = source
        data['transaction_evidence'] = transaction_quotes
        data['transaction_identifiers'] = extracted['identifiers']
        data['transaction_identifier_evidence'] = [
            {**identifier, 'source': source, 'source_sha256': data['digest']}
            for identifier in extracted['identifiers']]

    @staticmethod
    def _complete_local_evidence(data: dict[str, Any], rows: builtins.list[str]) -> None:
        """Attach visible quotes to values already selected by local rules."""
        evidence = data.setdefault('local_evidence', {})
        source = re.sub(r'\s+', '', str(data.get('text') or ''))
        for key in ('amount', 'merchant', 'invoice_number', 'payment_status'):
            value = str(data.get('fields', {}).get(key) or '')
            if not value:
                continue
            existing = evidence.get(key)
            if isinstance(existing, str) and existing and re.sub(r'\s+', '', existing) in source:
                continue
            if key == 'payment_status':
                quote = next((row for row in rows if any(marker in re.sub(r'\s+', '', row) for marker in
                    ('支付成功', '付款成功', '交易成功', '对方已收钱', '已收钱', '转账成功', '支付完成'))), '')
            elif key == 'amount':
                target = amount_cents(value)
                quote = ''
                for row in rows:
                    numbers = re.finditer(r'(?<![\d.])(-?\d+(?:\.\d{1,2})?)(?![\d.])', row)
                    def same_amount(number: str) -> bool:
                        try:
                            return amount_cents(number.lstrip('-')) == target
                        except ValueError:
                            return False
                    if any(same_amount(match[1]) for match in numbers):
                        quote = row
                        break
            else:
                from invoice_print_layout.matching_evidence import merchant_identity
                normalized_value = merchant_identity(value) if key == 'merchant' else re.sub(r'\s+', '', value)
                pattern = re.escape(normalized_value)
                if key in {'amount', 'invoice_number'}:
                    pattern = r'(?<![\d.])' + pattern + r'(?![\d.])'
                quote = next((row for row in rows if re.search(pattern,
                    merchant_identity(row) if key == 'merchant' else re.sub(r'\s+', '', row))), '')
            if quote:
                evidence[key] = quote

    @staticmethod
    def _local_core_ready(data: dict[str, Any], manual: set[str]) -> bool:
        """Category/date guesses do not control eligibility of explicit proofs."""
        fields = data.get('fields', {})
        role = data.get('role')
        if role not in {'invoice', 'receipt', 'payment', 'order', 'purchase'}:
            return False
        if role in {'order', 'purchase'}:
            text = re.sub(r'\s+', '', payment_state_text(str(data.get('text') or '')))
            if (fields.get('payment_status') not in {'', None, 'succeeded', 'unknown', 'uncertain'} or
                    any(marker in text for marker in ('已退款', '退款成功', '退款处理中', '退款申请',
                        '已撤销', '交易关闭', '订单关闭', '交易失败', '支付失败', '付款失败', '已取消',
                        '等待付款', '未付款', '未支付', '待支付', '待付款', '转账中')) or
                    not any(marker in text for marker in ('订单已送达', '订单已完成', '交易完成',
                        '交易成功', '已付款', '已支付', '支付成功', '付款成功')) or
                    not re.search(r'(?:实付款|实付金额|实际付款|实付)[:：]?[¥￥]?\d+(?:\.\d{1,2})?', text)):
                return False
        try:
            if amount_cents(fields.get('amount', '')) <= 0:
                return False
        except ValueError:
            return False
        merchant = re.sub(r'\s+', '', str(fields.get('merchant') or ''))
        if (not merchant or merchant in {'微信支付', '支付宝', '付款', '收款方', '未知商家'} or
                '待核对' in merchant):
            return False
        critical = {'amount', 'merchant'}
        if role == 'invoice':
            if not re.fullmatch(r'\d{8,30}', str(fields.get('invoice_number') or '')):
                return False
            critical.add('invoice_number')
        if role == 'payment':
            if fields.get('payment_status') != 'succeeded':
                return False
            critical.add('payment_status')
        blockers = {'同文件含多张发票，请拆分核对', '发票价税合计待核对', '发票号码待核对',
                    '发票号码冲突，请拆分材料或对照原件确认。',
                    '多个付款金额待核对', '付款状态待确认', '多页材料未完整识别', '金额冲突',
                    '商家冲突', '销售方名称待核对', '收款身份待核对'}
        if any(issue in blockers for issue in data.get('issues', [])):
            return False
        evidence = data.get('local_evidence', {})
        source = re.sub(r'\s+', '', str(data.get('text') or ''))
        return all(key in manual or (isinstance(evidence.get(key), str) and bool(evidence[key]) and
                   re.sub(r'\s+', '', evidence[key]) in source) for key in critical)

    @staticmethod
    def _bounded_vision_png(payload: bytes) -> bytes:
        """Bound a temporary visual derivative; retain original file bytes."""
        with Image.open(io.BytesIO(payload)) as source:
            image = source.convert('RGB')
        image.thumbnail((4000, 4000), Image.Resampling.LANCZOS)
        while True:
            output = io.BytesIO()
            image.save(output, format='PNG')
            if len(output.getvalue()) <= 5 * 1024 * 1024:
                return output.getvalue()
            image = image.resize((max(1, int(image.width * .75)), max(1, int(image.height * .75))),
                                 Image.Resampling.LANCZOS)

    @staticmethod
    def _manual_keys(data: dict[str, Any]) -> set[str]:
        values = data.get('manual_fields')
        if isinstance(values, list):
            return {key for key in values if isinstance(key, str)}
        if data.get('result') == '人工补充材料信息，尚未关联费用':
            return set(data.get('fields', {}))
        return set()

    def _finish_batch_analysis(self, data: dict[str, Any], original: dict[str, Any], *,
                               images: builtins.list[bytes] | None = None, complete: bool = True,
                               native_source: str, force_ai: bool = False) -> dict[str, Any]:
        """Merge suggestions into unknown machine fields and atomically save one proof."""
        manual = self._manual_keys(original)
        if native_source == 'local_ocr':
            data.pop('company_check', None)
        if native_source == 'local_ocr' and data.get('role') == 'invoice':
            from invoice_print_layout.company_profile import check_screenshot
            data['company_check'] = check_screenshot(self.store.root, data.get('text', ''))
            if data['company_check'] and data['company_check']['status'] == 'mismatch':
                data['issues'] = [*data.get('issues', []), '发票抬头/税号与公司信息不一致']
        if original.get('result') == '人工补充材料信息，尚未关联费用':
            # Migrate the old manual marker before replacing the result text.
            data['manual_fields'] = sorted(manual)
            data['manual_role'] = True
        for key in manual:
            if key in original.get('fields', {}):
                data.setdefault('fields', {})[key] = original['fields'][key]
        if original.get('manual_role') or original.get('result') == '人工补充材料信息，尚未关联费用':
            data['role'] = original.get('role', 'other')
        data['field_sources'] = {key: 'manual' if key in manual else native_source
                                for key, value in data.get('fields', {}).items() if value}
        # Every retry has fresh local fields; old AI conflicts must not outlive
        # their extraction or appear against manually corrected values.
        data.pop('field_conflicts', None)
        self._complete_local_special_fields(data, original, native_source)
        self._project_transaction_evidence(data, manual, native_source)
        category_origin = data.get('local_evidence', {}).get('category', {})
        if isinstance(category_origin, dict) and category_origin.get('source') and 'category' not in manual:
            data['field_sources']['category'] = category_origin['source']
        old_category_source = original.get('field_sources', {}).get('category')
        if (old_category_source in {'human', 'experience', 'preference', 'category_experience', 'expense_preferences'}
                and data.get('fields', {}).get('category') == original.get('fields', {}).get('category')):
            data['field_sources']['category'] = old_category_source
        data['issues'] = [issue for issue in data.get('issues', []) if not optional_material_issue(issue)]
        local_ready = complete and self._local_core_ready(data, manual)
        local_unused = complete and rail_payment_not_required(data)
        if local_ready and not local_unused and (not force_ai or not self.ai.enabled):
            # A successful local retry supersedes old AI failure metadata even
            # when cloud assistance has since been disabled. Fresh offline
            # proofs retain their existing no-AI-analysis contract.
            data['issues'] = [issue for issue in data.get('issues', []) if issue not in
                              {'AI识别未完成', '文字提取未完成', '手写难辨'}]
            for key in ('ai_evidence', 'ai_text', 'ai_optional_notes'):
                data.pop(key, None)
            if self.ai.enabled or 'ai_analysis' in original:
                data['ai_analysis'] = {'status': 'not_needed', 'model': 'glm-5.3-flash',
                    'message': '已通过本地文字提取确定关键字段，等待配对核对；未调用AI。',
                    'source_sha256': original['digest'], 'revision': original['revision'] + 1,
                    'input_sha256': hashlib.sha256(json.dumps({'digest': original['digest'],
                        'text': data.get('text', '')}, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()}
                data['result'] = data['ai_analysis']['message']
        elif self.ai.enabled and not local_unused:
            # Build the visual derivative only when local core extraction is
            # genuinely incomplete (or the user explicitly requests recheck).
            # Native text PDFs retain their text-first path; scanned PDFs have
            # already supplied bounded pages above.
            if complete and images is None and native_source == 'local_ocr':
                try:
                    images = [self._bounded_vision_png(Path(data['path']).read_bytes())]
                except (ValueError, OSError, Image.DecompressionBombError):
                    data['issues'] = list(dict.fromkeys([*data.get('issues', []), '图片无法转录']))
            self._apply_ai_analysis(data, original, manual, images=images, complete=complete,
                                    native_source=native_source, local_ready=local_ready)
        if complete and rail_payment_not_required(data):
            data.update(status='unused', unused_reason=RAIL_PAYMENT_UNUSED_REASON,
                        result=RAIL_PAYMENT_UNUSED_REASON)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(data['id'])
            if (current['revision'] != original['revision'] or
                    current.get('project_id') != original.get('project_id') or
                    current.get('item_id') != original.get('item_id') or
                    current['status'] in {'linked', 'duplicate', 'converted'} | INACTIVE_STATUSES or
                    current['digest'] != original['digest']):
                raise ValueError('收件已变化，请刷新')
            if (self.ai.enabled or data['status'] == 'unused' or
                    (local_ready and 'ai_analysis' in original)) and hashlib.sha256(Path(data['path']).read_bytes()).hexdigest() != original['digest']:
                raise ValueError('材料原件已变化，请重新上传')
            data['revision'] += 1
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), data['id']))
        return data

    def _complete_local_special_fields(self, data: dict[str, Any], original: dict[str, Any], source: str) -> None:
        """Reuse native transport and labelled payment parsers in the normal intake.

        These projections read existing bytes/text only. Manual fields and
        conflicting identified values remain authoritative and visible.
        """
        from invoice_print_layout.agent_transport import project_transport
        from invoice_print_layout.local_payment_fields import extract_local_payment
        manual = self._manual_keys(original)
        fields = data.setdefault('fields', {})
        transport = project_transport(data)
        if transport and not original.get('manual_role'):
            changes = {k: v for k, v in transport['fields'].items() if k not in manual and (
                not fields.get(k) or k in {'category', 'title'} and fields[k] in {'其他', '待核对购买凭证'})}
            if (not fields.get('amount') or not transport['fields'].get('amount') or
                    amount_cents(fields['amount']) == amount_cents(transport['fields']['amount'])):
                fields.update(changes)
                data['role'] = transport['role']
                data['native_transport_evidence'] = transport['evidence']
                if fields.get('amount'):
                    data['issues'] = [i for i in data.get('issues', []) if i != '金额冲突']
        payment_text = str(data.get('text') or '')
        payment = extract_local_payment(payment_text, current_fields=fields, manual_fields=manual,
                                        state_text=payment_state_text(payment_text))
        if data.get('role') in {'payment', 'other'}:
            # A shopping receipt mentioning an amount paid is not a payment screenshot.
            if payment['role']:
                prior_evidence = dict(data.get('local_evidence', {}))
                prior_sources = dict(data.get('field_sources', {}))
                fields.update(payment['fields'])
                if not original.get('manual_role') and data.get('role') != 'invoice':
                    data['role'] = payment['role']
                if data.get('role') != 'payment' or fields.get('payment_status') != 'succeeded':
                    if 'amount' not in manual:
                        fields['amount'] = ''
                    if not original.get('manual_role'):
                        data['role'] = 'other'
                data.setdefault('local_evidence', {}).update(payment['evidence'])
                data['local_payment_observations'] = {'fields': payment['observed_fields'],
                                                       'evidence': payment['evidence']}
                critical_conflicts = {k: v for k, v in payment['conflicts'].items()
                                      if k in {'amount', 'merchant', 'payment_status'}}
                if critical_conflicts:
                    conflicts = data.get('field_conflicts')
                    if not isinstance(conflicts, list):
                        conflicts = []
                        data['field_conflicts'] = conflicts
                    for key, conflict in critical_conflicts.items():
                        record = {'field': key, 'label': FIELD_LABELS.get(key, key),
                                  'local_value': conflict['existing'], 'ai_value': conflict['observed'],
                                  'local_quote': str(prior_evidence.get(key) or '')[:2000],
                                  'ai_quote': conflict['quote'][:2000],
                                  'local_source': str(prior_sources.get(key) or source),
                                  'observed_source': 'labelled_payment'}
                        if record not in conflicts:
                            conflicts.append(record)
                issues = list(data.get('issues', []))
                for key, issue in [('amount', '金额冲突'), ('merchant', '收款身份待核对'),
                                   ('payment_status', '付款状态待确认')]:
                    if payment['fields'].get(key) and key not in critical_conflicts and (
                            key != 'payment_status' or payment['fields'][key] == 'succeeded'):
                        issues = [value for value in issues if value != issue]
                data['issues'] = list(dict.fromkeys([*issues, *payment['issues']]))
        data.setdefault('field_sources', {}).update({k: 'manual' if k in manual else source
            for k, v in fields.items() if v})

    def mark_rail_payments_unused(self, ids: builtins.list[str],
                                 revisions: dict[str, int]) -> dict[str, Any]:
        """Atomically archive selected redundant rail payments, retaining proofs."""
        if (not isinstance(ids, list) or not ids or any(not isinstance(key, str) or not key for key in ids) or
                len(set(ids)) != len(ids) or not isinstance(revisions, dict) or set(revisions) != set(ids) or
                any(type(value) is not int or value < 0 for value in revisions.values())):
            raise ValueError('请选择材料并提供对应收件版本')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            entries = []
            project_id = ''
            for key in ids:
                row = db.execute('SELECT digest,data FROM intake_queue WHERE id=?', (key,)).fetchone()
                if not row:
                    raise ValueError('收件不存在')
                entry = json.loads(row['data'])
                if entry['revision'] != revisions[key]:
                    raise ValueError('收件已变化，请刷新')
                if entry.get('item_id') or entry['status'] in {'linked', 'duplicate', 'converted'}:
                    raise ValueError('材料已关联或已转换，不能标为无需使用')
                if entry['status'] not in {'received', 'review', 'unused'} or not rail_payment_not_required(entry):
                    raise ValueError('材料不是已识别的铁路客运支付截图')
                current_project = str(entry.get('project_id') or '')
                if not current_project or (project_id and project_id != current_project):
                    raise ValueError('材料须属于同一个项目')
                project_id = current_project
                attached = db.execute('SELECT 1 FROM attachments WHERE digest=? UNION SELECT 1 FROM automatic_ride_files WHERE digest=?',
                                      (entry['digest'], entry['digest'])).fetchone()
                if attached:
                    raise ValueError('材料已关联，不能标为无需使用')
                try:
                    digest = hashlib.sha256(Path(entry['path']).read_bytes()).hexdigest()
                except OSError as exc:
                    raise ValueError('材料原件不可读取，请检查原件') from exc
                if digest != entry['digest'] or digest != row['digest']:
                    raise ValueError('材料原件已变化，请重新上传')
                entries.append(entry)
            marked = 0
            for entry in entries:
                if entry['status'] == 'unused':
                    continue
                entry.update(status='unused', unused_reason=RAIL_PAYMENT_UNUSED_REASON,
                             result=RAIL_PAYMENT_UNUSED_REASON, revision=entry['revision'] + 1)
                db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                           (json.dumps(entry, ensure_ascii=False), entry['id']))
                marked += 1
        return {'marked': marked, 'unchanged': len(entries) - marked,
                'entries': [self._visible_entry(dict(entry)) for entry in entries]}

    def _apply_ai_analysis(self, data: dict[str, Any], original: dict[str, Any], manual: set[str], *,
                           images: builtins.list[bytes] | None, complete: bool, native_source: str,
                           local_ready: bool = False) -> None:
        """An extraction failure never revives guessed money or mutates a ledger."""
        fields = data.setdefault('fields', {})
        native_text = str(data.get('text') or '')
        local_identifiers = identifiers_for_record(data)
        sources = {key: 'manual' if key in manual else native_source for key, value in fields.items() if value}
        data['field_sources'] = sources
        input_sha = hashlib.sha256(json.dumps({'digest': original['digest'], 'text': native_text,
            'images': [hashlib.sha256(image).hexdigest() for image in images or []]},
            ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        analysis: dict[str, Any] = {'status': 'review', 'model': 'glm-5.3-flash', 'message': '',
            'input_sha256': input_sha, 'source_sha256': original['digest'],
            'revision': original['revision'] + 1}
        data['ai_analysis'] = analysis
        data.pop('ai_evidence', None)
        data.pop('ai_text', None)
        data.pop('ai_optional_notes', None)
        data.pop('field_conflicts', None)
        if not complete:
            analysis['message'] = '材料超过完整识别范围（扫描4页、文本10页或24000字），请拆分后识别。'
            data['issues'] = list(dict.fromkeys([*data.get('issues', []), '多页材料未完整识别']))
            data['result'] = analysis['message']
            return
        if not native_text.strip() and not images:
            analysis['message'] = '未能提取材料文字，请在合成台手动处理。'
            data['issues'] = list(dict.fromkeys([*data.get('issues', []), '文字提取未完成']))
            return
        if hashlib.sha256(Path(data['path']).read_bytes()).hexdigest() != original['digest']:
            raise ValueError('材料原件已变化，请重新上传')
        try:
            extracted = self.ai.extract(native_text, images=images)
        except GlmAIError as error:
            code = getattr(error, 'failure_code', 'internal_error')
            if not isinstance(code, str) or code not in FAILURE_MESSAGES:
                code = 'internal_error'
            field = getattr(error, 'failure_field', '')
            if not isinstance(field, str) or field not in FIELD_LIMITS:
                field = ''
            detail = (FIELD_LABELS[field] + '未能在提取出的文字中核对。' if code == 'evidence_invalid' and field
                      else FAILURE_MESSAGES[code])
            analysis.update(status='failed', failure_code=code, failure_field=field,
                            message=detail + '请重新识别或手工处理。')
            data['issues'] = (list(data.get('issues', [])) if local_ready else
                              list(dict.fromkeys([*data.get('issues', []), 'AI识别未完成'])))
            data['result'] = analysis['message']
            return
        except (ValueError, OSError, RuntimeError):
            # The client already sanitizes errors. Keep queue messages generic
            # so neither an OCR exception nor an API body can reveal a key.
            analysis.update(status='failed', failure_code='internal_error', failure_field='',
                            message='识别过程未完成，请重新识别或手工处理。')
            data['issues'] = (list(data.get('issues', [])) if local_ready else
                              list(dict.fromkeys([*data.get('issues', []), 'AI识别未完成'])))
            data['result'] = analysis['message']
            return
        ai_text = str(extracted.get('text') or '').strip()
        data['ai_text'] = ai_text[:24000]
        if not native_text.strip():
            data['text'] = ai_text[:24000]
        evidence = extracted.get('evidence', {})
        data['ai_evidence'] = evidence
        issues = [_readable_field_issue(issue) for issue in extracted.get('issues', [])
                  if not optional_material_issue(issue)]
        field_warnings = extracted.get('field_warnings', [])
        if isinstance(field_warnings, list):
            field_warnings = [_readable_field_issue(warning) for warning in field_warnings if isinstance(warning, str)]
        else:
            field_warnings = []
        if any(_optional_order_issue(issue) for issue in issues):
            field_warnings.append(ORDER_NUMBER_NOTE)
            issues = [issue for issue in issues if not _optional_order_issue(issue)]
        local_number = str(fields.get('invoice_number') or '')
        local_quote = data.get('local_evidence', {}).get('invoice_number')
        locally_confirmed = (data['role'] == 'invoice' and re.fullmatch(r'\d{8,30}', local_number) and
            isinstance(local_quote, str) and bool(local_quote) and
            local_number in re.sub(r'\s+', '', local_quote) and
            re.sub(r'\s+', '', local_quote) in re.sub(r'\s+', '', native_text) and
            not any(marker in str(issue) for issue in data.get('issues', []) for marker in
                    ('同文件含多张发票', '发票号码待核对', '发票号码冲突')))
        if locally_confirmed and '发票号码尚未确认' in issues:
            # Some PDF streams separate labels and values geometrically. The
            # established local parser has already confirmed this number; a
            # rejected redundant model field must not clear that source proof.
            issues = [issue for issue in issues if issue not in {'发票号码尚未确认',
                      '未能确认正式发票，请领取原票或手动确认材料用途。'}]
            field_warnings.append('发票号码沿用本地已读取的号码，未采用AI重复返回的号码。')
        if field_warnings:
            data['ai_optional_notes'] = list(dict.fromkeys(field_warnings))
        proposed = extracted.get('fields', {})
        # A platform, a shop and a payment channel can all be named on one
        # original. Comparing unlike roles as merchant identities is invalid.
        from invoice_print_layout.matching_evidence import merchant_identity
        role_contexts = [extract_transaction_evidence(native_text, str(data.get('role') or 'other'), fields),
                         extract_transaction_evidence(ai_text, str(extracted.get('role') or 'other'), proposed)]

        def merchant_role(value: str) -> str:
            normalized = merchant_identity(value)
            for field in ('shopping_platform', 'payment_channel', 'shop_name'):
                if any(merchant_identity(str(context['fields'].get(field) or '')) == normalized
                       for context in role_contexts if context['fields'].get(field)):
                    return field
            return ''
        unknown = {'', '待核对购买凭证', '其他', '电子发票（费用类别待核对）', 'uncertain', 'unknown'}
        protected_amount = any(issue in data.get('issues', []) for issue in
            ('同文件含多张发票，请拆分核对', '发票价税合计待核对', '多个付款金额待核对')) and bool(native_text.strip())
        for key, value in proposed.items():
            if key in manual or not value:
                continue
            prior = str(fields.get(key) or '')
            if key == 'invoice_number' and any('发票号码冲突' in str(issue) for issue in data.get('issues', [])):
                continue
            if protected_amount and key in {'amount', 'invoice_number'}:
                continue
            if key == 'goods' and prior not in unknown and data.get('local_evidence', {}).get('goods'):
                # Keep labelled source wording; optional model paraphrases must
                # not become new identities for matching or replace the quote.
                continue
            # Descriptive headings are generated summaries, not identities.
            # Keep a usable native title/category without making a different
            # paraphrase look like a conflict in the underlying transaction.
            if prior not in unknown and key not in {'note', 'goods'}:
                if key in {'merchant', 'shop_name'}:
                    identity_prior, identity_proposed = merchant_identity(prior), merchant_identity(str(value))
                else:
                    identity_prior, identity_proposed = prior.strip(), str(value).strip()
                if key == 'merchant' and identity_prior != identity_proposed:
                    left_role, right_role = merchant_role(prior), merchant_role(str(value))
                    if left_role and right_role and left_role != right_role:
                        continue
                if key == 'order_number' and identity_prior != identity_proposed:
                    # Identifiers from separate business/payment namespaces are
                    # useful duplicate keys, never an equality requirement.
                    field_warnings.append(ORDER_NUMBER_NOTE)
                elif (key not in OPTIONAL_EXTRACTION_FIELDS or key == 'shop_name') and identity_prior != identity_proposed:
                    if key in CONFLICT_FIELDS:
                        label = FIELD_LABELS.get(key, '材料字段')
                        issues.append('AI与本地识别的' + label + '不同，请对照原件确认。')
                        data.setdefault('field_conflicts', []).append({
                            'field': key, 'label': label, 'local_value': prior, 'ai_value': str(value),
                            'local_quote': str(data.get('local_evidence', {}).get(key) or '')[:2000],
                            'ai_quote': str(evidence.get(key) or '')[:2000], 'local_source': native_source})
                continue
            if not evidence.get(key):
                if key in OPTIONAL_EXTRACTION_FIELDS:
                    field_warnings.append('已忽略未能核对原文的可选字段：' + FIELD_LABELS.get(key, '材料字段'))
                    continue
                issues.append('AI字段未能在提取出的文字中核对：' + FIELD_LABELS.get(key, '材料字段'))
                continue
            fields[key] = value
            sources[key] = 'glm-5.3-flash'
            if key in {'shopping_platform', 'shop_name', 'payment_channel'}:
                data.setdefault('transaction_evidence', {})[key] = evidence[key]
        ai_role = extracted.get('role', 'other')
        manual_role = original.get('manual_role') or original.get('result') == '人工补充材料信息，尚未关联费用'
        if not manual_role:
            if data['role'] in {'other', 'purchase'}:
                data['role'] = ai_role
            elif ai_role != data['role'] and ai_role != 'other':
                issues.append('AI与原件用途冲突')
        if not native_text.strip() and ai_text:
            self._project_transaction_evidence(data, manual, 'glm-5.3-flash')
        if ai_text:
            # A visual fallback can complement a partial OCR transcript. Read
            # labelled IDs from its validated transcription independently;
            # never invent a kind or namespace from the bare order_number.
            ai_identifiers = extract_transaction_evidence(ai_text, str(ai_role), proposed)['identifiers']
            data['transaction_identifiers'] = identifiers_for_record({
                'transaction_identifiers': [*local_identifiers, *ai_identifiers]})
            data['transaction_identifier_evidence'] = [
                *({**identifier, 'source': native_source, 'source_sha256': original['digest']}
                  for identifier in local_identifiers),
                *({**identifier, 'source': 'glm-5.3-flash', 'source_sha256': original['digest']}
                  for identifier in ai_identifiers)]
            if 'transaction_identifiers' in fields:
                fields['transaction_identifiers'] = data['transaction_identifiers']
        # Re-check the entire visible transcription, including a vision fallback.
        # A model cannot turn a refunded, pending, or failed transfer into proof.
        combined = re.sub(r'\s+', '', payment_state_text(native_text + '\n' + ai_text))
        failed = any(marker in combined for marker in ('退款', '退回', '已撤销', '交易关闭', '交易失败',
            '支付失败', '付款失败', '已取消', '等待对方收款', '待对方收款', '等待付款', '未付款', '未支付', '待支付', '待付款', '转账中'))
        if fields.get('payment_status') in {'failed', 'refunded', 'unpaid'} or (
                failed and (data['role'] == 'payment' or '付款状态待确认' in data.get('issues', []))):
            issues.append('付款状态待确认')
            if not manual_role:
                data['role'] = 'other'
            if 'amount' not in manual:
                fields['amount'] = ''
        if data['role'] == 'payment' and fields.get('payment_status') != 'succeeded':
            issues.append('付款状态待确认')
        if not fields.get('amount'):
            issues.append('金额冲突')
        if not data.get('text', '').strip():
            issues.append('文字提取未完成')
        if data['role'] == 'other':
            issues.append('材料用途待确认')
        if not data.get('project_id'):
            issues.append('归属不明')
        # Only generic local unknowns can be resolved by evidenced AI fields.
        previous_issues = [issue for issue in data.get('issues', []) if issue not in
                           {'金额冲突', '商品不明', '手写难辨', '归属不明'}]
        if any(_optional_order_issue(issue) for issue in previous_issues):
            field_warnings.append(ORDER_NUMBER_NOTE)
        issues.extend(issue for issue in previous_issues if not _optional_order_issue(issue))
        if field_warnings:
            data['ai_optional_notes'] = list(dict.fromkeys(field_warnings))
        if data['role'] == 'invoice':
            optional = [issue for issue in issues if self._optional_invoice_issue(issue)]
            if optional:
                data['ai_optional_notes'] = list(dict.fromkeys([*field_warnings, *optional]))
                issues = [issue for issue in issues if not self._optional_invoice_issue(issue)]
        data['issues'] = list(dict.fromkeys(issues))
        message = ('存在不确定信息，请手动核对。' if data['issues'] else '文字与字段已识别，等待配对核对。')
        if field_warnings:
            message = ' '.join(field_warnings) + ' ' + message
        analysis.update(status='review' if data['issues'] else 'ok', message=message)
        data['result'] = analysis['message']

    @staticmethod
    def _optional_invoice_issue(issue: str) -> bool:
        """An invoice need not independently prove its payment or expense date."""
        # Mixed messages involving an actual ambiguity retain their blocker.
        if any(marker in issue for marker in ('金额', '票号', '发票号码', '商家', '销售方',
                '退款', '付款失败', '未付款', '未支付', '用途', '冲突', '多个', '不一致', '歧义')):
            return False
        return (any(marker in issue for marker in ('消费日期', '消费时间', '支出日期', '支付状态', '付款状态')) and
                any(marker in issue for marker in ('缺少', '缺失', '未提供', '未显示', '未标明', '未见',
                    '无法确认', '未能确定', '未能识别', '只有开票日期', '仅有开票日期')))

    def organize(self, key: str) -> dict[str, Any]:
        data = self.get(key)
        if data['status'] != 'review':
            return data
        fields = data['fields']
        # Hidden expenses still own their invoice/order identities and originals.
        items = self.store.list_items(include_deleted=True)
        identifier_keys = record_identifier_keys(data)
        exact = [x for x in items if identifier_keys & record_identifier_keys(x)]
        # Older expense snapshots may predate typed ID storage, while their
        # linked material still retains the labelled original transaction.
        linked_owners = {entry['item_id'] for entry in self.list()
                         if entry.get('item_id') and entry['id'] != key and
                         identifier_keys & record_identifier_keys(entry)}
        exact = [x for x in items if x in exact or x['id'] in linked_owners]
        invoice = fields.get('invoice_number')
        if invoice:
            related = [q for q in self.list() if q['id'] != key and q.get('fields', {}).get('invoice_number') == invoice and q.get('item_id')]
            ids = {q['item_id'] for q in related} | {item['id'] for item in exact}
            exact = [x for x in items if x['id'] in ids or str(x.get('invoice_number', '')) == str(invoice)
                     or str(invoice) in x.get('invoice_numbers', [])]
        candidates = [x for x in items if fields.get('merchant') and x['merchant'] == fields['merchant']
                      and x['amount'] == fields.get('amount') and (x['stage'] != 'cancelled' or x.get('deleted'))]
        with self.store.connect() as db:
            deleted_ids = self._deleted_owners(db, fields, data['digest'], record=data)
        deleted = [item for item in items if item['id'] in deleted_ids]
        if deleted:
            data.update(issues=list(dict.fromkeys(data['issues'] + ['疑似重复', '对应事项已删除'])),
                        candidates=[item['id'] for item in deleted], result='材料对应已删除事项，请先恢复后再补充材料')
            self._save_organize_result(data)
            return data
        equal_amount = bool(len(exact) == 1 and fields.get('amount') not in (None, '') and
                            exact[0]['amount_cents'] == amount_cents(fields['amount']))
        from invoice_print_layout.reimbursement_people import people_compatible
        if (len(exact) == 1 and people_compatible([data, exact[0]]) and not exact[0].get('deleted') and exact[0]['stage'] == 'draft' and equal_amount
                and (not data['project_id'] or data['project_id'] == exact[0].get('project_id'))):
            return self.resolve(key, data['revision'], fields, data['role'], exact[0]['id'], exact[0]['version'])
        if exact or candidates:
            data['issues'] = list(dict.fromkeys(data['issues'] + ['疑似重复']))
            if exact and not equal_amount:
                data['issues'] = list(dict.fromkeys(data['issues'] + ['金额冲突']))
            data['candidates'] = [x['id'] for x in exact or candidates]
            self._save_organize_result(data)
            return data
        # Standalone invoices / orders only; receipts and payments need explicit grouping.
        # Retiring a classification hint must not make the legacy intake path
        # create a new expense before the user selects its report category.
        if (not data['issues'] and fields.get('category') not in (None, '', '其他') and
                (identifier_keys or fields.get('invoice_number')) and data['role'] in {'invoice', 'order', 'purchase'}):
            return self.resolve(key, data['revision'], fields, data['role'])
        return data

    def _save_organize_result(self, data: dict[str, Any]) -> None:
        # Legacy annotations are not allowed to overwrite a concurrently
        # archived material after the review row was read outside this lock.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self.get(data['id'])
            if current['revision'] != data['revision'] or current['status'] != 'review':
                raise ValueError('收件已变化，请刷新')
            db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), data['id']))

    @staticmethod
    def _deleted_owners(db: sqlite3.Connection, fields: dict[str, Any], digest: str, *,
                        record: dict[str, Any] | None = None) -> builtins.list[str]:
        """Known identities remain reserved when an expense is hidden."""
        deleted = {row['id']: json.loads(row['data']) for row in db.execute('SELECT id,data FROM expenses')
                   if json.loads(row['data']).get('deleted')}
        owners = {row['item_id'] for row in db.execute('SELECT item_id FROM attachments WHERE digest=?', (digest,))
                  if row['item_id'] in deleted}
        invoice = str(fields.get('invoice_number') or '').strip()
        identifier_keys = record_identifier_keys(record if record is not None else fields)
        for item_id, item in deleted.items():
            if ((identifier_keys & record_identifier_keys(item)) or
                    (invoice and (str(item.get('invoice_number') or '').strip() == invoice or invoice in item.get('invoice_numbers', [])))):
                owners.add(item_id)
        for row in db.execute('SELECT data FROM intake_queue'):
            entry = json.loads(row['data'])
            prior = entry.get('fields') or {}
            if entry.get('item_id') in deleted and (
                    (invoice and str(prior.get('invoice_number') or '').strip() == invoice) or
                    (identifier_keys & record_identifier_keys(entry))):
                owners.add(entry['item_id'])
        if invoice:
            for row in db.execute("SELECT item_id,path FROM attachments WHERE role='invoice'"):
                if row['item_id'] not in deleted or Path(row['path']).suffix.lower() != '.pdf':
                    continue
                try:
                    with pymupdf.open(row['path']) as document:  # type: ignore[no-untyped-call]
                        if document.needs_pass:
                            continue
                        text = '\n'.join(document[index].get_text() for index in range(min(10, len(document))))
                    if parse_invoice_number(text) == invoice:
                        owners.add(row['item_id'])
                except (ValueError, OSError, RuntimeError):
                    continue
        return sorted(owners)

    def resolve(self, key: str, revision: int, fields: dict[str, Any], role: str,
                item_id: str = '', expected_version: str = '') -> dict[str, Any]:
        if role not in ROLES or role == 'package':
            raise ValueError('材料用途无效')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            data = self.get(key)
            if data['status'] in {'linked', 'duplicate'}:
                return data
            if data['status'] in {'archived', 'deleted', 'converted'}:
                raise ValueError('材料已归档、删除或已转换，不能关联费用')
            if data['status'] == 'unused' or rail_payment_not_required(data):
                raise ValueError('铁路支付截图无需使用，不能关联费用')
            if data['revision'] != revision:
                raise ValueError('收件已变化，请刷新')
            if (self._deleted_owners(db, data.get('fields') or {}, data['digest'], record=data) or
                    self._deleted_owners(db, fields, data['digest'], record={**data, 'fields': fields})):
                raise ValueError('材料对应已删除事项，请先恢复该事项，不能重新记账或补件')
            incoming_keys = record_identifier_keys(data)
            from invoice_print_layout.recognition_corrections import record
            record(db, key, data.get('fields', {}), fields, data.get('field_sources', {}))
            owners = {row['id'] for row in db.execute('SELECT id,data FROM expenses')
                      if incoming_keys & record_identifier_keys(json.loads(row['data']))}
            owners.update(entry.get('item_id') for row in db.execute('SELECT data FROM intake_queue')
                          if (entry := json.loads(row['data'])).get('item_id') and
                          incoming_keys & record_identifier_keys(entry))
            if owners - ({item_id} if item_id else set()):
                raise ValueError('同类型、同平台的订单或支付编号已关联费用，请补充到原费用，不能重复记账')
            if item_id:
                item = self.store.get(item_id)
                if item.get('returned_to_synthesis'):
                    raise ValueError('材料已撤回，请先在合成台重新确认创建')
                if item.get('deleted'):
                    raise ValueError('所属费用已删除，请先恢复后再补充材料')
                if item['version'] != expected_version or item['stage'] != 'draft':
                    raise ValueError('所属费用已变化或不是草稿，请刷新')
                from invoice_print_layout.reimbursement_people import common_person
                common_person([data, item])
                stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
                result = '补充材料'
            else:
                if fields.get('amount') in ('', None):
                    raise ValueError('请确认支出金额')
                if fields.get('title') in (None, '', '待核对购买凭证'):
                    raise ValueError('请确认具体购买内容')
                from invoice_print_layout.reimbursement_people import person_fields, PERSON_FIELDS
                clean = {key: value for key, value in fields.items() if key not in PERSON_FIELDS}
                stored = self.store._validate({**data.get('fields', {}), **clean, **person_fields(data),
                                                'transaction_identifiers': identifiers_for_record(data),
                                                'project_id': fields.get('project_id', data['project_id']),
                                                'expense_date': fields.get('expense_date', '')})
                item_id = uuid.uuid4().hex[:10]
                stored.update(id=item_id, created_at=now(), stage='draft', verified=False)
                db.execute('INSERT INTO expenses VALUES (?,?,?)', (item_id, 'intake:' + key, json.dumps(stored, ensure_ascii=False)))
                result = '新增费用'
            stored['transaction_identifiers'] = identifiers_for_record({
                **stored, 'transaction_identifiers': [*identifiers_for_record(stored), *identifiers_for_record(data)]})
            db.execute('INSERT OR IGNORE INTO attachments VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, item_id, data['name'], role, data['path'], data['digest']))
            stored.update(verified=False, updated_at=now())
            stored.pop('review_requested', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), '统一收件：' + result))
            data.update(item_id=item_id, status='linked', revision=revision + 1, issues=[], result=result, role=role)
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
        return data

    def import_mail(self, project_id: str = '') -> dict[str, Any]:
        from invoice_print_layout.storage import read_history
        counts: dict[str, Any] = {'received': 0, 'duplicate': 0}
        failures: list[dict[str, str]] = []
        contexts = {entry['attachment_hash']: entry for entry in read_history(self.store.workspace / 'mail_imports.jsonl')
                    if isinstance(entry.get('attachment_hash'), str)}
        folder = self.store.root / '邮件收件'
        for path in sorted(folder.iterdir() if folder.exists() else []):
            if not path.is_file() or path.suffix.lower() not in {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}:
                continue
            try:
                result = self.receive(path.name, path.read_bytes(), source='mail', project_id=project_id)
            except (ValueError, OSError, RuntimeError):
                failures.append({'name': path.name,
                                 'reason': '材料格式无效、过大或无法读取，原件保留在邮件收件；请检查或更换原文件。'})
                continue
            if not result.get('duplicate'):
                context = contexts.get(result['digest'], {})
                visible_context = {key: context[key] for key in ('message_uid', 'subject', 'order_number', 'kind', 'source_kind')
                                   if isinstance(context.get(key), str) and context[key]}
                if visible_context:
                    # Keep mail provenance separate from OCR fields; an order in
                    # the notification is not evidence of a printed invoice field.
                    with self.store.connect() as db:
                        db.execute('BEGIN IMMEDIATE')
                        current = self.get(result['id'])
                        if current['status'] not in INACTIVE_STATUSES:
                            current['mail_context'] = visible_context
                            current['revision'] += 1
                            db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                                       (json.dumps(current, ensure_ascii=False), result['id']))
            counts['duplicate' if result.get('duplicate') or result['status'] == 'duplicate' else 'received'] += 1
        if failures:
            counts.update(failed=len(failures), failures=failures)
        return counts

    def reconcile(self) -> None:
        """Reflect pairing done by the existing ride organizer in the durable queue."""
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT data FROM intake_queue').fetchall():
                data = json.loads(row['data'])
                if data['status'] in {'linked', 'duplicate', 'converted'} | INACTIVE_STATUSES:
                    continue
                attached = db.execute('SELECT item_id FROM attachments WHERE digest=? UNION SELECT item_id FROM automatic_ride_files WHERE digest=?', (data['digest'], data['digest'])).fetchone()
                if attached:
                    stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (attached['item_id'],)).fetchone()['data'])
                    if stored.get('deleted'):
                        continue
                    if data['project_id']:
                        from invoice_print_layout.expense_projects import ExpenseProjects
                        project = next((p for p in ExpenseProjects(self.store).list() if p['id'] == data['project_id']), None)
                        if project and stored['stage'] == 'draft' and stored['project'] == '待分配项目':
                            stored.update(project_id=project['id'], project=project['name'], verified=False, updated_at=now())
                            stored.pop('review_requested', None)
                            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), attached['item_id']))
                    data.update(status='linked', item_id=attached['item_id'], issues=[], result='打车配套材料已整理', revision=data['revision'] + 1)
                    db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), data['id']))
