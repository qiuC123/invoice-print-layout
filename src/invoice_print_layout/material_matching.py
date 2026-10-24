"""Explicit invoice/payment grouping; amounts match before any ledger change."""
from __future__ import annotations

from collections import Counter
from bisect import insort
from contextlib import nullcontext
from itertools import combinations
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Protocol, cast
from urllib.parse import quote
import uuid

import pymupdf

from invoice_print_layout.documents import parse_invoice_number
from invoice_print_layout.intake_queue import (IntakeQueue, positioned_invoice_seller, payment_state_text,
                                               _readable_field_issue, _optional_order_issue, ORDER_NUMBER_NOTE)
from invoice_print_layout.matching_evidence import merchant_identity, matching_context
from invoice_print_layout.intake_usage import RAIL_PAYMENT_UNUSED_REASON, rail_payment_not_required
from invoice_print_layout.intake_archive import archive_capabilities
from invoice_print_layout.expense_projects import project_views, install_project_guards
from invoice_print_layout.report_types import infer_report_choice, material_report_suggestion, report_types, validate_report_choice
from invoice_print_layout.workbench import ExpenseStore, amount_cents, now, returned_material_state, expense_identifier_keys
from invoice_print_layout.transaction_evidence import (
    extract_transaction_evidence, identifiers_for_record, record_identifier_keys, identifier_key)
from invoice_print_layout.reimbursement_people import (common_person, person_fields, people_compatible,
                                                       validate_person)

SUPPORT_ROLES = {'other', 'order', 'trip', 'detail', 'trip_sheet'}
MERCHANT_CONFLICT = '商家与原生销售方不一致，需人工核对'
Candidate = tuple[tuple[str, ...], tuple[str, ...]]
TRANSACTION_FIELDS = ('shopping_platform', 'shop_name', 'payment_channel')


def _transaction_fields(record: dict[str, Any]) -> dict[str, str]:
    fields = record.get('fields') or record
    parsed = extract_transaction_evidence(str(record.get('text') or ''), str(record.get('role') or ''), fields)
    result = {key: str(fields.get(key) or parsed['fields'].get(key) or '') for key in TRANSACTION_FIELDS}
    manual = set(record.get('manual_fields') or [])
    sources = record.get('field_sources') or {}
    if ('merchant' in manual or sources.get('merchant') == 'manual') and not (
            'shop_name' in manual or sources.get('shop_name') == 'manual'):
        if merchant_identity(str(fields.get('merchant') or '')) != merchant_identity(result['shop_name']):
            result['shop_name'] = ''  # A corrected merchant must not retain an obsolete automatic shop.
    return result


def _transaction_metadata(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep identifiers of all members, without inventing one common shop."""
    result: dict[str, Any] = {}
    fields = [_transaction_fields(record) for record in records]
    for key in TRANSACTION_FIELDS:
        values = {row[key] for row in fields if row[key]}
        if values:
            normalized = {merchant_identity(value) for value in values}
            result[key] = sorted(values)[0] if len(normalized) == 1 else ''
    identifiers: dict[str, dict[str, str]] = {}
    for record in records:
        for value in identifiers_for_record(record):
            marker = identifier_key(value) or json.dumps(value, ensure_ascii=False, sort_keys=True)
            identifiers.setdefault(marker, value)
    if identifiers:
        result['transaction_identifiers'] = list(identifiers.values())
    return result


def _repeated_counted_identity(records: list[dict[str, Any]]) -> bool:
    seen: set[tuple[str, str]] = set()
    for record in records:
        role = str(record.get('role') or 'expense')
        bucket = ('payment' if role == 'payment' else 'voucher' if role in
                  {'order', 'purchase', 'receipt', 'expense'} else '')
        if not bucket:
            continue  # Distinct tax invoices may reference one shopping order.
        for key in record_identifier_keys(record):
            if (bucket, key) in seen:
                return True
            seen.add((bucket, key))
    return False


def _name_conflict_projection(entry: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Resolve only disproven name warnings, without changing stored evidence."""
    conflicts = [value for value in entry.get('field_conflicts', []) if isinstance(value, dict)]
    resolved: set[str] = set()
    actors = _transaction_fields(entry)
    role_names = {merchant_identity(value) for value in actors.values() if value}
    for field in ('merchant', 'shop_name'):
        observed = [value for value in conflicts if value.get('field') == field]
        if not observed:
            continue
        def agrees(value: dict[str, Any]) -> bool:
            local, ai = value.get('local_value'), value.get('ai_value')
            if not isinstance(local, str) or not isinstance(ai, str) or not local or not ai:
                return False
            left, right = merchant_identity(local), merchant_identity(ai)
            return left == right or (field == 'merchant' and bool(actors['shop_name']) and
                                     {left, right} <= role_names)
        if all(agrees(value) for value in observed):
            resolved.add(field)
    if not any(value.get('field') == 'merchant' for value in conflicts):
        shop = merchant_identity(actors['shop_name'])
        text = merchant_identity(str(entry.get('text') or ''))
        quotes = [(entry.get(key) or {}).get('merchant') for key in ('local_evidence', 'ai_evidence')]
        def confirms_shop(value: Any) -> bool:
            if not shop or not isinstance(value, str) or not value.strip():
                return False
            quote = merchant_identity(value)
            if quote not in text:
                return False
            if quote == shop:
                return True
            extracted = extract_transaction_evidence(value, str(entry.get('role') or ''), {'merchant': shop})
            return merchant_identity(extracted['fields']['shop_name']) == shop
        if all(confirms_shop(value) for value in quotes):
            resolved.add('merchant')
    def resolved_issue(issue: str) -> bool:
        return any(re.search(r'AI与原件字段冲突[:：]\s*' + field + r'(?:\b|$)', issue) or
                   ('AI与本地识别的' + ('商家名称' if field == 'merchant' else '实际店铺') + '不同') in issue
                   for field in resolved)
    return ([str(value) for value in entry.get('issues', []) if not resolved_issue(str(value))],
            [value for value in conflicts if value.get('field') not in resolved])


class AIStatus(Protocol):
    @property
    def enabled(self) -> bool: ...

    def public_status(self) -> dict[str, Any]: ...


def _decision_text(value: Any) -> str:
    """Retain matching evidence without account, contact or tax identity lines."""
    if not isinstance(value, str):
        return ''
    lines = [line for line in value.splitlines() if not re.search(
        r'税号|纳税人识别号|社会信用代码|身份证|地址|电话|手机号|开户行|银行账号|银行账户|购买方', line)]
    return '\n'.join(re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[联系方式已省略]', line) for line in lines)[:6000]


def _candidate_components(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Partition the complete candidate set by shared materials, never amount."""
    parents: dict[str, str] = {}

    def find(key: str) -> str:
        parents.setdefault(key, key)
        if parents[key] != key:
            parents[key] = find(parents[key])
        return parents[key]

    for vouchers, payments in candidates:
        keys = [*vouchers, *('q:' + key for key in payments)]
        first = find(keys[0])
        for key in keys[1:]:
            parents[find(key)] = first
    groups: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault(find(candidate[0][0]), []).append(candidate)
    return list(groups.values())


def _company_identity(value: Any, cents: int) -> str:
    """Accept complete company names, never short labels or filename guesses."""
    if not isinstance(value, str):
        return ''
    name = re.sub(r'\s+', '', value)
    annotation = re.search(r'开票[¥￥]?(\d+(?:\.\d{1,2})?)元?$', name)
    if annotation:
        if amount_cents(annotation[1]) != cents:
            return ''
        name = name[:annotation.start()]
    suffix = re.search(r'(?:股份有限公司|有限责任公司|有限公司)$', name)
    if suffix is None:
        return ''
    core = name[:suffix.start()]
    for prefix in ('南昌市', '南昌'):
        if core.startswith(prefix):
            core = core[len(prefix):]
            break
    if len(re.findall(r'[\u4e00-\u9fff]', core)) < 4:
        return ''
    if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9（）()·]+', name):
        return ''
    if any(platform in name for platform in ('支付宝', '微信支付', '财付通', '银联', '中国铁路', '银行')):
        return ''
    return name


def _same_company(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    # This explicitly supported local prefix can be absent from the payment
    # display name. Other regions and arbitrary substrings remain unresolved.
    for prefix in ('南昌市', '南昌'):
        if left.startswith(prefix) and left[len(prefix):] == right:
            return True
        if right.startswith(prefix) and right[len(prefix):] == left:
            return True
    return False


def _specific_merchant(value: Any, cents: int) -> str:
    """A concrete seller or shop, rather than a category or payment channel."""
    if not isinstance(value, str):
        return ''
    name = merchant_identity(value)
    if name in {'支付宝', '微信', '微信支付', '财付通', '银联', '支付平台', '携程', '携程旅行',
                '同程旅行', '飞猪', '去哪儿', '美团', '美团外卖', '淘宝', '淘宝闪购', '京东'}:
        return ''
    company = _company_identity(name, cents)
    if company:
        return company
    name = re.sub(r'[（(]个体工商户[）)]$', '', name)
    if name == '中国铁路':
        return name
    if (len(re.findall(r'[\u4e00-\u9fff]', name)) < 4 or len(name) > 160 or
            not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9（）()·-]+', name) or
            any(token in name for token in ('支付宝', '微信', '财付通', '银联', '银行', '支付平台',
                                            '酒店旅游', '酒店住宿', '住宿服务', '餐饮服务', '材料采购'))):
        return ''
    if name in {'酒店', '住宿', '餐饮', '其他', '携程', '携程旅行', '携程旅行网', '同程旅行', '飞猪', '美团', '淘宝', '京东'}:
        return ''
    base = re.sub(r'\([^()]{1,60}\)$', '', name)
    suffix = re.search(r'(?:股份有限公司|有限责任公司|有限公司|酒店|宾馆|旅馆|公寓|食堂|餐厅|餐饮店|快餐店|饭店|便利店|超市|小吃店|商店)$', base)
    if suffix is None or len(re.findall(r'[\u4e00-\u9fff]', base[:suffix.start()])) < 2:
        return ''
    if suffix[0].endswith('公司'):
        core = name[:suffix.start()]
        for prefix in ('南昌市', '南昌'):
            if core.startswith(prefix):
                core = core[len(prefix):]
                break
        if len(re.findall(r'[\u4e00-\u9fff]', core)) < 4:
            return ''
    if name[:suffix.start()] in {'餐饮', '酒店', '住宿', '快餐', '小吃', '便利', '旅游', '生活', '南昌', '南昌市'}:
        return ''
    return name


def _label_values(text: str, labels: tuple[str, ...]) -> list[str]:
    """Read labelled source descriptions; never search arbitrary generated notes."""
    rows = [re.sub(r'\s+', '', row) for row in text.splitlines() if row.strip()]
    pattern = re.compile(r'^(?:' + '|'.join(re.escape(label) for label in labels) + r')[:：]?(.*)$')
    values = []
    for index, row in enumerate(rows):
        match = pattern.fullmatch(row)
        if match:
            value = match[1] or (rows[index + 1] if index + 1 < len(rows) else '')
            if value and len(value) <= 160:
                values.append(value)
    return values


def _description_names_merchant(descriptions: list[str], name: str) -> bool:
    if not name:
        return False
    name = merchant_identity(name)
    variants = [name]
    for prefix in ('南昌市', '南昌'):
        if name.startswith(prefix):
            variants.append(name[len(prefix):])
    return any(re.search(r'(?:^|[，,、;/：:（(·|\-])' + re.escape(variant) +
                         r'(?=$|[，,、;/：:）)·|\-]|住宿|房费|入住|预订|订单)', merchant_identity(description))
               for variant in variants if len(variant) >= 4 for description in descriptions)


def _payment_status_text(text: str) -> str:
    """Standalone refund controls are not transaction refund states."""
    return re.sub(r'\s+', '', payment_state_text(text))


def _material_role(entry: dict[str, Any]) -> str:
    """Use completed platform purchase records as receipts, retaining source roles."""
    role = str(entry.get('role') or 'other')
    if (role not in {'order', 'purchase'} or entry.get('manual_role') or
            entry.get('result') == '人工补充材料信息，尚未关联费用' or
            entry.get('qr_status') in {'manual', 'converted'} or
            entry.get('kind') in {'qr', 'qr_claim', 'claim', 'invoice_qr'}):
        return role
    fields = entry.get('fields') or {}
    status = fields.get('payment_status', '')
    source = str(entry.get('text') or '')
    text = _payment_status_text(source)
    status_rows = [re.sub(r'\s+', '', row) for row in payment_state_text(source).splitlines()]
    # Actual transaction states take priority over a stale successful extraction.
    # Refund controls and return-policy advertising are not refund confirmations.
    if status not in {'', 'succeeded', 'unknown', 'uncertain'} or any(word in text for word in (
            '已退款', '退款成功', '退款完成', '退款中', '退款处理中', '已申请退款', '退款申请',
            '已退回', '已撤销', '交易关闭', '订单关闭', '订单已关闭', '交易失败', '支付失败', '付款失败',
            '已取消', '等待付款', '未付款', '未支付', '待支付', '待付款', '转账中')):
        return role
    # A paid but still pending shipment is not a completed purchase receipt.
    # Match state rows, rather than unrelated navigation or delivery promises.
    if any(re.match(r'^(?:(?:订单|配送|物流|当前)状态[:：]?)?'
                    r'(?:待配送|等待配送|待发货|等待发货|未发货|配送中|骑士正在配送|'
                    r'尚未送达|未送达|未签收|待取货|待收货)(?:[>＞，,。！!]|$)', row)
           for row in status_rows):
        return role
    completed = any(word in text for word in ('订单已送达', '订单已完成', '交易完成', '交易成功',
                                             '已付款', '已支付', '支付成功', '付款成功'))
    # Delivery apps also report the actual outcome with a relative duration or
    # a collection location. Require an explicit completed state at row start;
    # "预计提前...送达" and descriptions mentioning delivery are not evidence.
    delivered = any(re.match(
        r'^(?:(?:订单)?已(?:(?:提前|延迟)\d+(?:小时(?:\d+分(?:钟)?)?|分(?:钟)?))?送达|'
        r'已送至(?:指定暂存点|(?:您的)?收货地址|指定地点)|配送已完成|(?:订单|包裹)?已签收)'
        r'(?:[>＞，,。！!]|$)', row) for row in status_rows)
    completed = completed or delivered
    paid_total = re.search(r'(?:实付款|实付金额|实际付款|实付)[:：]?[¥￥]?\d+(?:\.\d{1,2})?', text)
    return 'receipt' if status == 'succeeded' or completed and paid_total else role


def _rules_relation(voucher: dict[str, Any], payment: dict[str, Any]) -> str:
    shop_left, shop_right = voucher.get('shop_name', ''), payment.get('shop_name', '')
    if shop_left and shop_right:
        return 'yes' if _same_company(merchant_identity(shop_left), merchant_identity(shop_right)) else 'no'
    left, right = voucher['identity'], payment['identity']
    supported = (_same_company(left, right) or
                 _description_names_merchant(payment['descriptions'], left) or
                 _description_names_merchant(voucher['descriptions'], right))
    if supported:
        return 'yes'
    return 'no' if left and right else 'unknown'


def _balanced_candidates(vouchers: list[dict[str, Any]], payments: list[dict[str, Any]]) -> tuple[
        dict[int, list[Candidate]], Counter[str], int]:
    voucher_cards = {card['key']: card for card in vouchers}
    payment_cards = {card['id']: card for card in payments}
    voucher_sums: dict[int, list[tuple[str, ...]]] = {}
    payment_sums: dict[int, list[tuple[str, ...]]] = {}
    for count in range(1, min(4, len(vouchers)) + 1):
        for group in combinations(vouchers, count):
            total = sum(card['amount_cents'] for card in group)
            voucher_sums.setdefault(total, []).append(tuple(card['key'] for card in group))
    for count in range(1, min(4, len(payments)) + 1):
        for group in combinations(payments, count):
            total = sum(card['amount_cents'] for card in group)
            if total in voucher_sums:
                payment_sums.setdefault(total, []).append(tuple(card['id'] for card in group))
    pair_count = sum(len(voucher_sums[total]) * len(groups) for total, groups in payment_sums.items())
    candidates: dict[int, list[Candidate]] = {}
    uses: Counter[str] = Counter()
    if pair_count > 50000:
        return candidates, uses, pair_count
    proper_cache: dict[tuple[int, ...], set[int]] = {}

    def proper_sums(amounts: tuple[int, ...]) -> set[int]:
        if amounts not in proper_cache:
            proper_cache[amounts] = {sum(group) for count in range(1, len(amounts))
                                    for group in combinations(amounts, count)}
        return proper_cache[amounts]

    for total, payment_groups in payment_sums.items():
        for voucher_keys in voucher_sums[total]:
            voucher_amounts = tuple(voucher_cards[key]['amount_cents'] for key in voucher_keys)
            for payment_keys in payment_groups:
                records = [*(voucher_cards[key] for key in voucher_keys), *(payment_cards[key] for key in payment_keys)]
                if len({record.get('reimbursement_person_id') for record in records if record.get('reimbursement_person_id')}) > 1:
                    continue
                payment_amounts = tuple(payment_cards[key]['amount_cents'] for key in payment_keys)
                # A union of independent balanced pairs is not a competing group.
                if (len(voucher_keys) > 1 and len(payment_keys) > 1 and
                        proper_sums(voucher_amounts) & proper_sums(payment_amounts)):
                    continue
                candidates.setdefault(total, []).append((voucher_keys, payment_keys))
                uses.update([*voucher_keys, *('q:' + key for key in payment_keys)])
    return candidates, uses, pair_count


def _data(value: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(value))


def _report_suggestion(item: dict[str, Any]) -> dict[str, str]:
    try:
        return infer_report_choice(item)
    except ValueError:
        return infer_report_choice({key: value for key, value in item.items() if key not in {'project_type', 'project_name'}})


def _identified_amount(entry: dict[str, Any]) -> int | None:
    fields = entry.get('fields') or {}
    try:
        if fields.get('amount') not in (None, ''):
            cents = amount_cents(fields['amount'])
        else:
            raw = fields.get('amount_cents', entry.get('amount_cents'))
            if isinstance(raw, bool) or raw is None or not str(raw).isdigit():
                return None
            cents = int(raw)
        return cents if cents > 0 else None
    except (ValueError, TypeError):
        return None


def _rail_invoice(entry: dict[str, Any]) -> bool:
    """Rail passenger invoices remain usable alone, never seek payment pairs."""
    return (entry.get('role') == 'invoice' and
            ((entry.get('fields') or {}).get('category') == '高铁' or
             bool(re.search(r'铁路电子客票|铁路客运(?:服务)?|12306(?:电子)?(?:发票|车票|客票)',
                            str(entry.get('text') or '')))))


class MaterialMatching:
    """Match original proofs once, retaining reversible payment associations."""

    def __init__(self, store: ExpenseStore, intake: IntakeQueue, ai: AIStatus | None = None):
        if intake.store.database != store.database:
            raise ValueError('收件与账本必须属于同一工作区')
        self.store, self.intake = store, intake
        self.ai = ai
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS material_matches (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS material_creation_batches '
                       '(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fingerprint TEXT NOT NULL, data TEXT NOT NULL)')
            install_project_guards(db)

    def _project(self, project_id: str) -> dict[str, Any]:
        project = next((p for p in project_views(self.store)
                        if project_id and p['id'] == project_id), None)
        if project is None:
            raise ValueError('请选择有效项目')
        return project

    @staticmethod
    def _payment_id(key: str) -> str:
        if not isinstance(key, str) or not key or key.startswith('e:'):
            raise ValueError('请选择有效付款收件')
        return key.removeprefix('q:')

    @staticmethod
    def _write_queue(db: sqlite3.Connection, entry: dict[str, Any]) -> None:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',
                   (json.dumps(entry, ensure_ascii=False), entry['id']))

    @staticmethod
    def _queue(db: sqlite3.Connection, key: str) -> dict[str, Any]:
        row = db.execute('SELECT data FROM intake_queue WHERE id=?', (key,)).fetchone()
        if row is None:
            raise ValueError('收件不存在')
        return _data(row['data'])

    def _attachment_role(self, entry: dict[str, Any], category: str) -> str:
        """Persist newly registered native trip proofs without changing undo snapshots."""
        role = _material_role(entry)
        if category != '打车' or role != 'other' or entry.get('manual_role'):
            return role
        from invoice_print_layout.ride_proofs import native_trip_pdf
        path = Path(entry['path']).resolve()
        return ('trip' if path.is_relative_to(self.store.workspace) and
                native_trip_pdf(path, str(entry.get('digest') or '')) else role)

    @staticmethod
    def _selected_person(db: sqlite3.Connection, project_id: str, records: list[dict[str, Any]],
                         fields: dict[str, Any]) -> dict[str, str]:
        explicit = fields.get('reimbursement_person_id')
        if 'reimbursement_person_id' in fields and not isinstance(explicit, str):
            raise ValueError('请选择有效费用归属人')
        if explicit:
            return validate_person(db, project_id, explicit)
        return common_person(records)

    def _available(self, entry: dict[str, Any], project_id: str) -> bool:
        return (entry.get('project_id', '') in {'', project_id} and entry.get('status') in {'received', 'review'}
                and not entry.get('item_id') and entry.get('qr_status') not in {'manual', 'converted'}
                and not rail_payment_not_required(entry))

    def _native_invoice_seller(self, entry: dict[str, Any]) -> str:
        """Read old invoice metadata without changing its queue or original."""
        if entry.get('role') != 'invoice' or Path(entry['name']).suffix.lower() != '.pdf':
            return ''
        try:
            path = Path(entry['path']).resolve()
            if not path.is_relative_to(self.store.workspace) or not path.is_file() or path.suffix.lower() != '.pdf':
                return ''
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != entry['digest']:
                return ''
            with pymupdf.open(stream=content, filetype='pdf') as document:  # type: ignore[no-untyped-call]
                sellers = {seller for page in document if (seller := positioned_invoice_seller(page))}
            return next(iter(sellers)) if len(sellers) == 1 else ''
        except (OSError, ValueError, RuntimeError):
            return ''

    def _queue_card(self, entry: dict[str, Any]) -> dict[str, Any]:
        image = Path(entry['name']).suffix.lower() != '.pdf'
        key = quote(entry['id'], safe='')
        role = 'other' if entry.get('qr_status') == 'manual' else _material_role(entry)
        known_fields = entry.get('fields') or {}
        visible_fields = {field: known_fields[field] for field in (
            'title', 'merchant', 'amount', 'amount_cents', 'category', 'expense_date', 'note',
            'order_number', 'invoice_number', 'goods', 'purchase_content', 'payment_note', 'transaction_description',
            'transaction_id', 'counterparty', 'payment_status')
            if field in known_fields}
        projected_issues, projected_conflicts = _name_conflict_projection(entry)
        raw_issues = [str(issue) for issue in projected_issues
                      if issue not in {'商品不明', '归属不明', '归属未定'}]
        issues = [_readable_field_issue(issue) for issue in raw_issues if not _optional_order_issue(issue)]
        notes = [ORDER_NUMBER_NOTE] if (any(_optional_order_issue(issue) for issue in raw_issues) or
                                      ORDER_NUMBER_NOTE in entry.get('ai_optional_notes', [])) else []
        field_conflicts = [{key: str(value[key])[:2000] for key in (
            'field', 'label', 'local_value', 'ai_value', 'local_quote', 'ai_quote', 'local_source')
            if isinstance(value.get(key), str)} for value in projected_conflicts
            if isinstance(value, dict) and value.get('field') in {'merchant', 'shop_name', 'amount', 'invoice_number', 'payment_status'}]
        visible_fields.update(_transaction_fields(entry))
        seller = self._native_invoice_seller(entry)
        if seller:
            if not known_fields.get('merchant'):
                visible_fields['merchant'] = seller
            elif (re.sub(r'\s+', '', str(known_fields['merchant'])) != re.sub(r'\s+', '', seller) and
                  not _same_company(_company_identity(known_fields['merchant'], _identified_amount(entry) or 0),
                                    _company_identity(seller, _identified_amount(entry) or 0))):
                issues.append(MERCHANT_CONFLICT)
        analysis = {**visible_fields, 'text': entry.get('text', ''), 'fields': visible_fields}
        if not analysis.get('title') or analysis['title'] == Path(entry['name']).stem:
            analysis['title'] = entry['name']
        suggestion = material_report_suggestion(analysis)
        report_choice = _report_suggestion(analysis if suggestion else {
            **visible_fields, 'title': visible_fields.get('title') or Path(entry['name']).stem})
        return {**person_fields(entry), 'key': 'q:' + entry['id'], 'id': entry['id'], 'name': entry['name'],
                'kind': 'qr_claim' if entry.get('qr_status') == 'manual' else role,
                'fields': visible_fields, 'qr_status': entry.get('qr_status', ''), 'qr_check': entry.get('qr_check'),
                'company_check': entry.get('company_check'),
                'claim_message': str(entry.get('result', '')) if entry.get('qr_status') == 'manual' else '',
                'ai_analysis': {name: value for name, value in (entry.get('ai_analysis') or {}).items()
                                if name in {'status', 'model', 'message', 'failure_code', 'failure_field'}
                                and isinstance(value, str)},
                'report_choice': report_choice, 'report_choice_suggestion': suggestion,
                'role': role, **({'source_role': entry['role']} if role == 'receipt' and role != entry.get('role') else {}),
                'amount_cents': _identified_amount(entry), 'project_id': entry['project_id'],
                'payment_not_required': _rail_invoice(entry),
                'unused_reason': (entry.get('unused_reason') or RAIL_PAYMENT_UNUSED_REASON
                                  if entry.get('status') == 'unused' or rail_payment_not_required(entry) else ''),
                'revision': entry['revision'], 'item_id': '',
                'issues': issues, 'recognition_notes': notes, 'field_conflicts': field_conflicts,
                'transaction_identifiers': identifiers_for_record(entry),
                **archive_capabilities(entry),
                'status': entry['status'], 'thumbnail': '/intake-file/' + key if image else
                '/intake-page/' + key + '?page=0', 'url': '/intake-file/' + key}

    @staticmethod
    def _expense_card(item: dict[str, Any]) -> dict[str, Any] | None:
        if item.get('synthesis_material_state') in {'archived', 'deleted'}:
            return None
        attachments = item['attachments']
        vouchers = [a for a in attachments if a['role'] in {'invoice', 'receipt'} and a['available']]
        if (item.get('deleted') or item['stage'] != 'draft' or item['verified'] or item.get('merged_into') or item.get('material_match') or
                not vouchers or any(a['role'] == 'payment' for a in attachments)):
            return None
        attachment = vouchers[0]
        key = quote(attachment['id'], safe='')
        image = Path(attachment['name']).suffix.lower() != '.pdf'
        from invoice_print_layout.synthesis_expenses import expense_archive_capabilities
        return {**person_fields(item), 'key': 'e:' + item['id'], 'id': item['id'], 'item_id': item['id'], 'name': item['title'],
                'kind': attachment['role'], 'role': attachment['role'], 'amount_cents': item['amount_cents'],
                'fields': {key: item[key] for key in ('title', 'merchant', 'category', 'expense_date',
                           'amount', 'amount_cents', 'order_number', 'note', *TRANSACTION_FIELDS) if key in item},
                'transaction_identifiers': identifiers_for_record(item),
                'project_id': item.get('project_id', ''), 'version': item['version'],
                'payment_not_required': item.get('category') == '高铁',
                'report_choice': item.get('report_choice') or _report_suggestion(item),
                'report_choice_suggestion': item.get('report_choice_suggestion', {}),
                'returned_to_synthesis': bool(item.get('returned_to_synthesis')),
                **expense_archive_capabilities(item),
                'thumbnail': '/file/' + key if image else '/attachment-page/' + key + '?page=0',
                'url': '/file/' + key,
                'issues': ['待配对：' + issue for issue in item['missing']], 'status': 'draft'}

    @staticmethod
    def _archived_expense_card(item: dict[str, Any]) -> dict[str, Any]:
        from invoice_print_layout.synthesis_expenses import expense_archive_capabilities
        attachment = next((row for row in item['attachments'] if row['available']), None)
        key = quote(attachment['id'], safe='') if attachment else ''
        image = bool(attachment and Path(attachment['name']).suffix.lower() != '.pdf')
        return {**person_fields(item), 'key': 'e:' + item['id'], 'id': item['id'], 'item_id': item['id'], 'name': item['title'],
                'kind': attachment['role'] if attachment else 'other',
                'role': attachment['role'] if attachment else 'other', 'amount_cents': item['amount_cents'],
                'fields': {field: item[field] for field in ('title', 'merchant', 'amount', 'category', 'expense_date') if field in item},
                'project_id': item.get('project_id', ''), 'version': item['version'],
                'synthesis_material_state': item['synthesis_material_state'], 'status': item['synthesis_material_state'],
                'thumbnail': ('/file/' + key if image else '/attachment-page/' + key + '?page=0') if attachment else '',
                'url': '/file/' + key if attachment else '', 'issues': [],
                **expense_archive_capabilities(item)}

    def _public_match(self, group: dict[str, Any], *, item: dict[str, Any] | None = None) -> dict[str, Any]:
        if item is None:
            item = self.store.get(group['item_id'])
        public = {key: group.get(key) for key in ('id', 'item_id', 'project_id', 'voucher_key', 'payment_ids',
                  'status', 'provisional', 'reason', 'payment_total_cents', 'payment_count', 'created_at', 'amount_only')}
        public.update(**person_fields(item), group_id=group['id'], title=item['title'], amount=item['amount'],
                      amount_cents=item['amount_cents'], expense_version=item['version'],
                      expected_version=item['version'], version=item['version'])
        keys = group['voucher_keys'] if 'voucher_keys' in group else [group['voucher_key']]
        public.update(voucher_keys=keys, voucher_count=len(keys),
                      voucher_total_cents=group.get('voucher_total_cents', group['payment_total_cents']),
                      vouchers=group.get('vouchers', []), schema_version=group.get('schema_version', 1),
                      support_ids=group.get('support_ids', []), support_count=len(group.get('support_ids', [])),
                      support_attachment_ids=group.get('support_attachment_ids', []),
                      mode=group.get('mode', 'matching'), recognition=group.get('recognition', {}),
                      alignment_status=group.get('alignment_status', 'balanced'), amount_pending=bool(item.get('amount_pending')),
                      can_supplement=group['status'] == 'provisional' and bool(group['provisional']) and
                      item['stage'] == 'draft' and (item['material_basis'] == 'invoice_payment' or
                      item.get('category') == '打车' and item['material_basis'] == 'standard') and
                      not item.get('deleted') and not item['verified'] and not item.get('merged_into'))
        return public

    def board(self, project_id: str) -> dict[str, Any]:
        if project_id:
            self._project(project_id)
        result: dict[str, Any] = {'vouchers': [], 'payments': [], 'other': [], 'unused': [],
                                 'archived': [], 'deleted': [], 'matches': [],
                                 'report_types': report_types(), 'supplement_candidates': []}
        with self.store.connect() as db:
            attached = {row['digest'] for row in db.execute('SELECT digest FROM attachments UNION SELECT digest FROM automatic_ride_files')}
            entries = [_data(row['data']) for row in db.execute('SELECT data FROM intake_queue ORDER BY rowid DESC')]
            groups = [_data(row['data']) for row in db.execute('SELECT data FROM material_matches ORDER BY rowid DESC')]
        for entry in entries:
            if ((project_id and entry.get('project_id', '') not in {'', project_id}) or entry.get('status') not in {'received', 'review', 'unused', 'archived', 'deleted'}
                    or entry.get('item_id') or entry.get('qr_status') == 'converted' or entry['digest'] in attached):
                continue
            card = self._queue_card(entry)
            if entry.get('status') == 'deleted':
                result['deleted'].append(card)
                continue
            if entry.get('status') == 'archived':
                result['archived'].append(card)
                continue
            if entry.get('status') == 'unused' or rail_payment_not_required(entry):
                result['unused'].append(card)
                result['archived'].append(card)
                continue
            target = ('vouchers' if card['role'] in {'invoice', 'receipt'} else
                      'payments' if card['role'] == 'payment' else 'other')
            result[target].append(card)
        items = self.store.list_items(include_deleted=True)
        by_id = {item['id']: item for item in items}
        for item in items:
            if item['deleted']:
                continue
            if (not project_id or item.get('project_id') == project_id) and item.get('synthesis_material_state') in {'archived', 'deleted'}:
                result[item['synthesis_material_state']].append(self._archived_expense_card(item))
                continue
            if (not project_id or item.get('project_id') == project_id) and (not item['complete'] or item.get('returned_to_synthesis')):
                expense_card = self._expense_card(item)
                if expense_card:
                    result['vouchers'].append(expense_card)
        for group in groups:
            if (project_id and group['project_id'] != project_id) or group['status'] == 'undone':
                continue
            matching_item = by_id.get(group['item_id'])
            if matching_item is None:
                matching_item = self.store.get(group['item_id'])
            if not matching_item.get('deleted'):
                result['matches'].append(self._public_match(group, item=matching_item))
        result['supplement_candidates'] = self._supplement_candidates(
            entries, groups, result['matches'], result['other'], items=by_id)
        if project_id:
            suggestions = self._suggestions_from_board(result, project_id)
            result.update(suggestions=suggestions['suggestions'], suggestion_count=suggestions['suggestion_count'],
                          suggestions_truncated=suggestions['suggestions_truncated'],
                          suggestion_conflicts=[{**conflict, 'project_id': project_id}
                                                for conflict in suggestions['conflicts']])
        else:
            suggestions_list: list[dict[str, Any]] = []
            suggestion_conflicts: list[dict[str, Any]] = []
            suggestion_count = 0
            known_projects = project_views(self.store)
            available_projects = {card['project_id'] for card in [*result['vouchers'], *result['payments']]}
            for project in known_projects:
                if project['id'] not in available_projects:
                    continue
                scan = self._suggestions_from_board(result, project['id'])
                suggestion_count += scan['suggestion_count']
                suggestions_list.extend(scan['suggestions'][:max(0, 100 - len(suggestions_list))])
                suggestion_conflicts.extend({**conflict, 'project_id': project['id'], 'project_name': project['name']}
                                            for conflict in scan['conflicts'])
            if suggestion_count > len(suggestions_list):
                suggestion_conflicts.append({'project_id': '', 'reason':
                    f'全部项目共有{suggestion_count}个候选，仅展示前100个；各项目竞争判断均包含完整材料池，请切换项目查看'})
            result.update(suggestions=suggestions_list, suggestion_count=suggestion_count,
                          suggestions_truncated=suggestion_count > len(suggestions_list),
                          suggestion_conflicts=suggestion_conflicts)
        return result

    def _supplement_candidates(self, entries: list[dict[str, Any]], groups: list[dict[str, Any]],
                               public: list[dict[str, Any]], cards: list[dict[str, Any]], *,
                               items: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Read-only exact-number suggestions; never infer from amount or name."""
        available = {card['id'] for card in cards}
        by_group = {group['id']: group for group in groups}
        by_number: dict[tuple[str, str], list[dict[str, Any]]] = {}
        candidates = []
        with self.store.connect() as db:
            for matching in public:
                if not matching['can_supplement']:
                    continue
                item = items.get(matching['item_id']) if items is not None else None
                if item is None:
                    item = self.store.get(matching['item_id'])
                roles = {attachment['role'] for attachment in item['attachments'] if attachment['available']}
                if (not roles.intersection({'invoice', 'receipt'}) or 'payment' not in roles or
                        item.get('material_match', {}).get('group_id') != matching['group_id']):
                    continue
                try:
                    self._assert_group_snapshot(db, by_group[matching['group_id']])
                    self._check_member_originals(self._member_state(db, item['id']))
                except ValueError:
                    continue
                for number in self._expense_numbers(db, item):
                    by_number.setdefault((item['project_id'], number), []).append(matching)
            for entry in entries:
                if (entry['id'] not in available or not entry.get('project_id') or
                        entry.get('role') not in SUPPORT_ROLES or entry.get('qr_status') in {'manual', 'converted'}):
                    continue
                path = Path(entry['path']).resolve()
                if (not path.is_relative_to(self.store.workspace) or not path.is_file() or
                        hashlib.sha256(path.read_bytes()).hexdigest() != entry['digest']):
                    continue
                number = self._creation_invoice_number({**entry, 'role': 'invoice'})
                options = by_number.get((entry['project_id'], number), []) if number else []
                if len(options) != 1:
                    continue
                matching = options[0]
                candidates.append({'key': 'q:' + entry['id'], 'support_id': entry['id'], 'support_ids': [entry['id']],
                                   'revision': entry['revision'], 'revisions': {entry['id']: entry['revision']},
                                   'group_id': matching['group_id'], 'item_id': matching['item_id'], 'title': matching['title'],
                                   'invoice_number': number, 'expected_version': matching['expected_version'],
                                   'expense_version': matching['expected_version'], 'can_supplement': True,
                                   'number_source': 'fields' if (entry.get('fields') or {}).get('invoice_number') else 'pdf_text',
                                   'reason': '发票号码一致；补充材料不计金额'})
        return candidates

    def _check_entry(self, db: sqlite3.Connection, entry: dict[str, Any], project_id: str,
                     revisions: dict[str, Any], roles: set[str]) -> None:
        if rail_payment_not_required(entry):
            raise ValueError(RAIL_PAYMENT_UNUSED_REASON)
        if not self._available(entry, project_id):
            raise ValueError('材料已关联、需领票或不属于当前项目')
        if revisions.get(entry['id'], revisions.get('q:' + entry['id'])) != entry['revision']:
            raise ValueError('收件版本已变化，请刷新后重试')
        if entry.get('role') not in roles and _material_role(entry) not in roles:
            raise ValueError('材料用途不符，请先明确票据或付款角色')
        path = Path(entry['path']).resolve()
        if not path.is_relative_to(self.store.workspace) or not path.is_file():
            raise ValueError('原始材料不可用')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['digest']:
            raise ValueError('原始材料内容已变化')
        if (db.execute('SELECT 1 FROM attachments WHERE digest=?', (entry['digest'],)).fetchone() or
                db.execute('SELECT 1 FROM automatic_ride_files WHERE digest=?', (entry['digest'],)).fetchone()):
            raise ValueError('材料已用于其他费用，不能重复关联')

    def _check_invoice_number(self, db: sqlite3.Connection, number: str) -> None:
        """Binary variants cannot register the same known invoice twice."""
        if not number:
            return
        active = {row['id']: _data(row['data']) for row in db.execute('SELECT id,data FROM expenses')}
        active = {key: item for key, item in active.items() if item['stage'] != 'cancelled' or item.get('deleted')}
        if any(str(item.get('invoice_number', '')).strip() == number or
               number in item.get('invoice_numbers', []) for item in active.values()):
            raise ValueError('此发票号码已登记费用，请补充到已有费用，不能重复记账')
        for row in db.execute('SELECT data FROM intake_queue'):
            entry = _data(row['data'])
            if (entry.get('item_id') in active and
                    str(entry.get('fields', {}).get('invoice_number', '')).strip() == number):
                raise ValueError('此发票号码已关联已有费用，请补充到原费用')
        for row in db.execute('SELECT data FROM material_matches'):
            group = _data(row['data'])
            if group.get('item_id') not in active:
                continue
            if any(str(entry.get('fields', {}).get('invoice_number', '')).strip() == number
                   for entry in group.get('queue_before', {}).values()):
                raise ValueError('此发票号码已匹配已有费用，请补充到原费用')
        # Old expenses did not retain number metadata. Read their existing
        # vector originals only; never OCR, re-download or alter them.
        for row in db.execute("SELECT item_id,path FROM attachments WHERE role='invoice'"):
            if row['item_id'] not in active:
                continue
            path = Path(row['path']).resolve()
            if path.suffix.lower() != '.pdf' or not path.is_relative_to(self.store.workspace) or not path.is_file():
                continue
            try:
                with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
                    if document.needs_pass:
                        continue
                    text = '\n'.join(document[index].get_text() for index in range(min(10, len(document))))
                existing_number = parse_invoice_number(text)
            except (ValueError, OSError, RuntimeError):
                continue
            if existing_number == number:
                raise ValueError('此发票号码已在原有费用票据中，请补充到已有费用，不能重复记账')

    def match(self, voucher_key: str | list[str], payment_ids: list[str], project_id: str,
              expected_version: str = '', revisions: dict[str, Any] | None = None,
              fields: dict[str, Any] | None = None, provisional: bool = True,
              reason: str = '', *, expected_versions: dict[str, str] | None = None,
              support_ids: list[str] | None = None,
              expected_materials: list[dict[str, Any]] | None = None,
              expected_pool: dict[str, Any] | None = None) -> dict[str, Any]:
        keys = [voucher_key] if isinstance(voucher_key, str) else voucher_key
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 100 or
                any(not isinstance(key, str) or not key.startswith(('q:', 'e:')) or len(key) <= 2 for key in keys)
                or len(keys) != len(set(keys))):
            raise ValueError('请选择1至100份不重复的票据或已有费用')
        versions = expected_versions or {}
        if not isinstance(versions, dict):
            raise ValueError('费用版本参数无效')
        if expected_materials is not None and (not isinstance(expected_materials, list) or
                len(expected_materials) > 52 or any(not isinstance(card, dict) for card in expected_materials)):
            raise ValueError('自动配对材料版本参数无效')
        if expected_pool is not None and not isinstance(expected_pool, dict):
            raise ValueError('自动配对材料池版本参数无效')
        support_keys = self._support_keys(support_ids)
        if len(keys) > 1:
            return self._match_multiple(keys, payment_ids, project_id, versions, revisions or {},
                                        fields or {}, provisional, reason, support_keys, expected_materials, expected_pool)
        key = keys[0]
        return self._match_single(key, payment_ids, project_id,
                                 versions.get(key[2:], versions.get(key, expected_version)),
                                 revisions, fields, provisional, reason, support_keys, expected_materials, expected_pool)

    def _support_keys(self, support_ids: list[str] | None) -> list[str]:
        if support_ids is None:
            return []
        if not isinstance(support_ids, list) or len(support_ids) > 100:
            raise ValueError('请选择至多100份补充材料')
        keys = [self._payment_id(key) for key in support_ids]
        if len(keys) != len(set(keys)):
            raise ValueError('补充材料不能重复选择')
        return keys

    def _supports(self, db: sqlite3.Connection, keys: list[str], project_id: str,
                  revisions: dict[str, Any]) -> list[dict[str, Any]]:
        entries = [self._queue(db, key) for key in keys]
        for entry in entries:
            self._check_entry(db, entry, project_id, revisions, SUPPORT_ROLES)
        if len({entry['digest'] for entry in entries}) != len(entries):
            raise ValueError('相同补充原件不能重复关联')
        return entries

    @staticmethod
    def _creation_invoice_number(entry: dict[str, Any]) -> str:
        number = str((entry.get('fields') or {}).get('invoice_number') or '').strip()
        if number or entry['role'] != 'invoice' or Path(entry['path']).suffix.lower() != '.pdf':
            return number
        try:
            with pymupdf.open(entry['path']) as document:  # type: ignore[no-untyped-call]
                if document.needs_pass:
                    return ''
                text = '\n'.join(document[index].get_text() for index in range(min(10, len(document))))
            return parse_invoice_number(text)
        except (ValueError, OSError, RuntimeError):
            return ''

    def _check_member_originals(self, state: dict[str, Any]) -> None:
        for attachment in state['attachments']:
            path = Path(attachment['path']).resolve()
            if not path.is_relative_to(self.store.workspace) or not path.is_file():
                raise ValueError('成员费用的原始材料不可用')
            if hashlib.sha256(path.read_bytes()).hexdigest() != attachment['digest']:
                raise ValueError('成员费用的原始材料内容已变化')

    @staticmethod
    def _check_transaction_selection(db: sqlite3.Connection, records: list[dict[str, Any]],
                                     excluded_items: set[str], *, count_amounts: bool = True) -> dict[str, Any]:
        """Same typed identity may support one expense, never fund it twice."""
        if count_amounts and _repeated_counted_identity(records):
            raise ValueError('所选材料含相同类型和平台的交易编号，不能重复计费；请保留一份或作为补充材料')
        metadata = _transaction_metadata(records)
        keys = record_identifier_keys(metadata)
        if keys:
            for row in db.execute('SELECT id,source,data FROM expenses'):
                if row['id'] in excluded_items:
                    continue
                if keys & expense_identifier_keys(db, row['id'], _data(row['data']), source=row['source']):
                    raise ValueError('同类型、同平台的交易编号已登记费用，不能重复记账；请查看原费用或补充材料')
        return metadata

    def create_expense(self, voucher_keys: list[str], payment_ids: list[str], project_id: str, *,
                       support_ids: list[str] | None = None, revisions: dict[str, Any] | None = None,
                       expected_versions: dict[str, str] | None = None,
                       fields: dict[str, Any] | None = None, suggestion_id: str = '',
                       suggestion_token: str = '', _db: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Register chosen proofs without asserting matching amounts or verifying."""
        project = self._project(project_id)
        if (not isinstance(voucher_keys, list) or len(voucher_keys) > 100 or
                any(not isinstance(key, str) or not key.startswith(('q:', 'e:')) or len(key) <= 2 for key in voucher_keys)
                or not isinstance(payment_ids, list) or len(payment_ids) > 100):
            raise ValueError('请选择有效的票据、付款或普通材料')
        support_keys = self._support_keys(support_ids)
        payment_keys = [self._payment_id(key) for key in payment_ids]
        selection = [*voucher_keys, *('q:' + key for key in payment_keys), *('q:' + key for key in support_keys)]
        if not selection or len(selection) != len(set(selection)):
            raise ValueError('请选择至少一份不重复的材料')
        revisions = {} if revisions is None else revisions
        versions = {} if expected_versions is None else expected_versions
        fields = {} if fields is None else fields
        if not isinstance(revisions, dict) or not isinstance(versions, dict) or not isinstance(fields, dict):
            raise ValueError('登记参数无效')
        choice = validate_report_choice(fields.get('project_type'), fields.get('project_name'))
        manual_amount = fields.get('amount')
        if isinstance(manual_amount, str):
            manual_amount = manual_amount.strip()
        if manual_amount in (None, ''):
            raise ValueError('请确认并填写金额')
        supplied_amount = amount_cents(manual_amount)
        expected_pool = None
        suggested_cards: list[dict[str, Any]] = []
        if not isinstance(suggestion_id, str) or not isinstance(suggestion_token, str):
            raise ValueError('候选版本参数无效')
        if suggestion_id or suggestion_token:
            board = self.board(project_id)
            suggestion = next((candidate for candidate in board['suggestions']
                               if candidate['id'] == suggestion_id), None)
            if suggestion is None or suggestion['snapshot_token'] != suggestion_token:
                raise ValueError('待合成候选或材料池已变化，请刷新后重新选择')
            if (set(voucher_keys) != set(suggestion['voucher_keys']) or
                    set(payment_keys) != set(suggestion['payment_ids'])):
                raise ValueError('材料选择已变化，请重新选择候选或按手工材料创建')
            expected_pool = self._rules_pool_snapshot(board, project_id)
            selected = set(voucher_keys).union('q:' + key for key in payment_keys)
            suggested_cards = [card for card in [*board['vouchers'], *board['payments']] if card['key'] in selected]
        group_id = uuid.uuid4().hex
        with self.store.connect() if _db is None else nullcontext(_db) as db:
            if _db is None:
                db.execute('BEGIN IMMEDIATE')
            if expected_pool is not None:
                if self._rules_current_pool_snapshot(db, project_id) != expected_pool:
                    raise ValueError('待合成材料池已变化，请刷新后重新选择')
                for card in suggested_cards:
                    self._rules_material_state(card, project_id, db=db, allow_name_review=True)
            members: dict[str, dict[str, Any]] = {}
            entries: list[dict[str, Any]] = []
            vouchers: list[dict[str, Any]] = []
            payment_records: list[dict[str, Any]] = []
            support_records: list[dict[str, Any]] = []
            numbers: set[str] = set()
            digests: set[str] = set()

            def remember_digest(digest: str) -> None:
                if digest in digests:
                    raise ValueError('同组原件摘要重复，不能重复登记')
                digests.add(digest)

            for key in voucher_keys:
                if key.startswith('e:'):
                    item = self.store.get(key[2:])
                    if item['version'] != versions.get(item['id'], versions.get(key)):
                        raise ValueError('成员费用版本已变化，请刷新后重新选择')
                    if (item.get('deleted') or item.get('synthesis_material_state') in {'archived', 'deleted'} or
                            item['stage'] != 'draft' or item['verified'] or item.get('merged_into') or item.get('material_match') or
                            item.get('project_id') != project_id or
                            not any(a['role'] in {'invoice', 'receipt'} and a['available'] for a in item['attachments']) or
                            any(a['role'] == 'payment' for a in item['attachments'])):
                        raise ValueError('已有费用必须属于当前项目、未核对且尚未配对付款')
                    state = self._member_state(db, item['id'])
                    self._check_member_originals(state)
                    member_numbers = self._expense_numbers(db, item)
                    if numbers & member_numbers:
                        raise ValueError('同组票据号码重复，不能重复登记')
                    numbers.update(member_numbers)
                    for attachment in state['attachments']:
                        remember_digest(attachment['digest'])
                    members[item['id']] = state
                    vouchers.append({'key': key, 'item_id': item['id'], 'role': 'invoice',
                                     'amount_cents': item['amount_cents'] if not item.get('amount_pending') and item['amount_cents'] > 0 else None,
                                     'invoice_numbers': sorted(member_numbers),
                                     **{field: item.get(field, '') for field in ('title', 'category', 'expense_date', 'merchant')}})
                else:
                    entry = self._queue(db, key[2:])
                    self._check_entry(db, entry, project_id, revisions, {'invoice', 'receipt'})
                    remember_digest(entry['digest'])
                    number = self._creation_invoice_number(entry)
                    if number:
                        if number in numbers:
                            raise ValueError('同组票据号码重复，不能重复登记')
                        self._check_invoice_number(db, number)
                        numbers.add(number)
                    entries.append(entry)
                    original = self._queue_card(entry)['fields']
                    vouchers.append({'key': key, 'queue_id': entry['id'], 'role': _material_role(entry),
                                     'amount_cents': _identified_amount(entry), 'invoice_numbers': [number] if number else [],
                                     'title': original.get('title') or Path(entry['name']).stem,
                                     **{field: original.get(field, '') for field in ('category', 'expense_date', 'merchant')}})
            for keys, allowed_roles, records in ((payment_keys, {'payment'}, payment_records),
                                                (support_keys, SUPPORT_ROLES | {'purchase'}, support_records)):
                for key in keys:
                    entry = self._queue(db, key)
                    self._check_entry(db, entry, project_id, revisions, allowed_roles)
                    remember_digest(entry['digest'])
                    entries.append(entry)
                    original = self._queue_card(entry)['fields']
                    records.append({'key': 'q:' + key, 'queue_id': key, 'role': entry['role'],
                                    'amount_cents': _identified_amount(entry),
                                    'title': original.get('title') or Path(entry['name']).stem,
                                    **{field: original.get(field, '') for field in ('category', 'expense_date', 'merchant')}})

            transaction_records = [*entries, *(state['data'] for state in members.values())]
            transaction_metadata = self._check_transaction_selection(db, transaction_records, set(members))
            for record in [*vouchers, *payment_records, *support_records]:
                source = next((entry for entry in transaction_records if entry['id'] ==
                               record.get('queue_id', record.get('item_id'))), None)
                if source:
                    record.update(_transaction_metadata([source]))

            owner = self._selected_person(db, project_id, [*entries, *(state['data'] for state in members.values())], fields)
            for record in [*entries, *(state['data'] for state in members.values()), *vouchers, *payment_records, *support_records]:
                record.update(owner)

            def identified_total(records: list[dict[str, Any]]) -> int | None:
                return sum(record['amount_cents'] for record in records) if all(record['amount_cents'] is not None for record in records) else None

            voucher_total, payment_total = identified_total(vouchers), identified_total(payment_records)
            suggested_amount = None
            amount_source = 'unknown'
            if vouchers and voucher_total is not None:
                suggested_amount, amount_source = voucher_total, 'vouchers'
            elif payment_records and payment_total is not None:
                suggested_amount, amount_source = payment_total, 'payments'
            elif not vouchers and not payment_records and len(support_records) == 1 and support_records[0]['amount_cents'] is not None:
                suggested_amount, amount_source = support_records[0]['amount_cents'], 'single_support'
            chosen_amount = supplied_amount if supplied_amount is not None else suggested_amount
            pending = chosen_amount is None
            primary = vouchers or payment_records or support_records
            suggested_title = primary[0]['title'] if len(primary) == 1 else f'合成费用 · {len(selection)}份材料'
            categories = {str(record['category']).strip() or '其他' for record in primary}
            suggested_category = next(iter(categories)) if len(categories) == 1 else '其他'
            dates = {record['expense_date'] for record in primary}
            merchants = {record['merchant'] for record in primary}
            if pending:
                alignment = 'unknown'
            elif vouchers and payment_records and voucher_total is not None and payment_total is not None and voucher_total != payment_total:
                alignment = 'mismatch'
            elif supplied_amount is not None and suggested_amount is not None and supplied_amount != suggested_amount:
                alignment = 'amount_override'
            elif vouchers and payment_records:
                alignment = 'balanced' if voucher_total is not None and payment_total is not None else 'unconfirmed'
            else:
                alignment = 'partial'
            recognition = {'title': suggested_title, 'category': suggested_category, 'amount_cents': suggested_amount,
                           'report_choice': infer_report_choice({'title': suggested_title, 'category': suggested_category}),
                           'amount_source': amount_source, 'manual_amount_cents': supplied_amount,
                           'voucher_total_cents': voucher_total, 'payment_total_cents': payment_total,
                           'difference_cents': payment_total - voucher_total if vouchers and payment_records and
                           voucher_total is not None and payment_total is not None else None,
                           'amount_difference_cents': supplied_amount - suggested_amount if supplied_amount is not None and
                           suggested_amount is not None else None, 'vouchers': vouchers,
                           'payments': payment_records, 'supports': support_records}
            description = '材料已登记；金额及对应关系待人工核对'
            reused = len(voucher_keys) == 1 and voucher_keys[0].startswith('e:')
            item_id = voucher_keys[0][2:] if reused else uuid.uuid4().hex[:10]
            base = dict(members[item_id]['data']) if reused else {}
            cents = 0 if chosen_amount is None else chosen_amount
            material_basis = 'standard' if choice['category'] in {'高铁', '打车'} else 'invoice_payment'
            values = {**base, **choice, **owner, 'project_id': project_id, 'project': project['name'],
                      **transaction_metadata,
                      'amount': f'{cents // 100}.{cents % 100:02d}', 'amount_pending': pending,
                      'material_basis': material_basis, 'expense_date': next(iter(dates)) if len(dates) == 1 else '',
                      'merchant': next(iter(merchants)) if len(merchants) == 1 else ''}
            stored = {**base, **self.store._validate(values)}
            stored.pop('returned_to_synthesis', None)
            grouping = {'group_id': group_id, 'provisional': True, 'reason': description, 'mode': 'registration',
                        'voucher_count': len(vouchers), 'voucher_total_cents': voucher_total,
                        'payment_count': len(payment_records), 'payment_total_cents': payment_total,
                        'support_ids': support_keys, 'support_count': len(support_keys), 'support_attachment_ids': [],
                        'alignment_status': alignment, 'recognition': recognition}
            stored.update(id=item_id, project_type=choice['project_type'], project_name=choice['project_name'],
                          created_at=base.get('created_at', now()), updated_at=now(), stage='draft', verified=False,
                          amount_pending=pending, review_requested=True, material_basis=material_basis, legacy=False,
                          material_match=grouping, material_members=vouchers, invoice_numbers=sorted(numbers))
            if not reused:
                db.execute('INSERT INTO expenses VALUES (?,?,?)', (item_id, 'material-create:' + group_id, json.dumps(stored, ensure_ascii=False)))
            queue_before = {entry['id']: dict(entry) for entry in entries}
            for row in db.execute('SELECT data FROM intake_queue'):
                entry = _data(row['data'])
                if entry.get('item_id') in members:
                    entry.update(owner)
                    queue_before[entry['id']] = entry
            moved: list[dict[str, Any]] = []
            support_attachments: list[str] = []
            for member_id, state in members.items():
                support_attachments.extend(a['id'] for a in state['attachments'] if a['role'] in SUPPORT_ROLES | {'purchase'})
                if reused:
                    continue
                moved.extend(state['attachments'])
                db.execute('UPDATE attachments SET item_id=? WHERE item_id=?', (item_id, member_id))
                merged = {**state['data'], 'stage': 'cancelled', 'merged_into': item_id, 'merged_by': group_id, 'updated_at': now()}
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(merged, ensure_ascii=False), member_id))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (member_id, now(), '材料登记合并到 ' + item_id + '；原金额保留，不重复计费'))
            added: list[str] = []
            linked_roles: dict[str, str] = {}
            for entry in entries:
                attachment_id = uuid.uuid4().hex
                role = self._attachment_role(entry, stored['category'])
                linked_roles[entry['id']] = role
                db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)', (attachment_id, item_id,
                           entry['name'], role, entry['path'], entry['digest']))
                added.append(attachment_id)
                if entry['id'] in support_keys:
                    support_attachments.append(attachment_id)
            grouping['support_attachment_ids'] = support_attachments
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            queue_after = {}
            for key, entry in queue_before.items():
                linked = {**entry, 'item_id': item_id, 'material_match_id': group_id, 'revision': entry['revision'] + 1,
                          'result': description}
                if key in {entry['id'] for entry in entries}:
                    linked.update(status='linked', project_id=project_id, issues=[],
                                  role=entry['role'] if _material_role(entry) != entry['role'] else linked_roles[key])
                self._write_queue(db, linked)
                queue_after[key] = linked
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), description))
            group = {'id': group_id, 'schema_version': 3, 'mode': 'registration', 'project_id': project_id, 'item_id': item_id,
                     'voucher_key': voucher_keys[0] if voucher_keys else '', 'voucher_keys': voucher_keys, 'vouchers': vouchers,
                     'voucher_total_cents': voucher_total, 'payment_ids': payment_keys, 'payment_total_cents': payment_total,
                     'payment_count': len(payment_records), 'payment_amounts': {record['queue_id']: record['amount_cents'] for record in payment_records},
                     'support_ids': support_keys, 'support_count': len(support_keys), 'support_attachment_ids': support_attachments,
                     'attachment_ids': added, 'moved_attachments': moved, 'member_before': members,
                     'member_after': {key: self._member_state(db, key) for key in members if key != item_id},
                     'queue_before': queue_before, 'queue_after': queue_after, 'aggregate_after': self._member_state(db, item_id),
                     'reused_item': reused, 'provisional': True, 'status': 'provisional', 'reason': description,
                     'alignment_status': alignment, 'recognition': recognition, 'created_at': now()}
            db.execute('INSERT INTO material_matches VALUES (?,?)', (group_id, json.dumps(group, ensure_ascii=False)))
        if _db is not None:
            # The outer batch commits before public views use independent read
            # connections; those cannot see these uncommitted expenses.
            return {'item_id': item_id, 'match_id': group_id, 'created': not reused}
        return {'item': self.store.get(item_id), 'match': self._public_match(group), 'created': not reused}

    def _batch_result(self, record: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
        with self.store.connect() as db:
            groups = [_data(db.execute('SELECT data FROM material_matches WHERE id=?', (key,)).fetchone()['data'])
                      for key in record['match_ids']]
        return {'items': [self.store.get(key) for key in record['item_ids']],
                'matches': [self._public_match(group) for group in groups],
                'created': len(record['item_ids']), 'new_expenses': record['new_expenses'],
                'replayed': replayed,
                'message': (f'已返回之前确认的{len(record["item_ids"])}组结果，没有重复创建。' if replayed else
                            f'已确认创建{len(record["item_ids"])}组，进入待核对；尚未核验或提交。')}

    def create_batch(self, project_id: str, groups: Any, idempotency_key: Any) -> dict[str, Any]:
        """Confirm independent suggestions atomically, rechecking their shared pool."""
        if (not isinstance(project_id, str) or not project_id or
                not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', idempotency_key) or
                not isinstance(groups, list) or not 1 <= len(groups) <= 100 or
                any(not isinstance(row, dict) for row in groups)):
            raise ValueError('请选择当前项目及待合成候选，并提供有效批量确认编号')
        self._project(project_id)
        allowed = {'suggestion_id', 'snapshot_token', 'voucher_keys', 'payment_ids',
                   'revisions', 'expected_versions', 'fields'}
        for row in groups:
            if (set(row) - allowed or not isinstance(row.get('suggestion_id'), str) or
                    not row['suggestion_id'] or not isinstance(row.get('snapshot_token'), str) or
                    not row['snapshot_token'] or not isinstance(row.get('fields'), dict)):
                raise ValueError('批量候选参数无效，请刷新后重新选择')
        fingerprint = hashlib.sha256(json.dumps({'project_id': project_id, 'groups': groups},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        # Resolve a completed retry before rescanning a pool whose members have
        # already moved into expenses. A key with changed choices is never reused.
        with self.store.connect() as db:
            prior = db.execute('SELECT project_id,fingerprint,data FROM material_creation_batches WHERE id=?',
                               (idempotency_key,)).fetchone()
        if prior:
            if prior['project_id'] != project_id or prior['fingerprint'] != fingerprint:
                raise ValueError('批量确认编号已用于另一组材料或内容，请刷新后重新确认')
            return self._batch_result(_data(prior['data']), replayed=True)
        replayed = False
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT project_id,fingerprint,data FROM material_creation_batches WHERE id=?',
                               (idempotency_key,)).fetchone()
            if prior:
                if prior['project_id'] != project_id or prior['fingerprint'] != fingerprint:
                    raise ValueError('批量确认编号已用于另一组材料或内容，请刷新后重新确认')
                record = _data(prior['data'])
                replayed = True
            else:
                board = self.board(project_id)
                available = {candidate['id']: candidate for candidate in board['suggestions']}
                expected_pool = self._rules_pool_snapshot(board, project_id)
                cards = {card['key']: card for card in [*board['vouchers'], *board['payments']]}
                selected: set[str] = set()
                rows = []
                for row in groups:
                    candidate = available.get(row['suggestion_id'])
                    if candidate is None or candidate['snapshot_token'] != row['snapshot_token']:
                        raise ValueError('待合成候选或材料池已变化，请刷新后重新选择')
                    for field in ('voucher_keys', 'payment_ids', 'revisions', 'expected_versions'):
                        if field in row and row[field] != candidate[field]:
                            raise ValueError('候选材料选择或版本已变化，请刷新后重新选择')
                    keys = {*candidate['voucher_keys'], *('q:' + key for key in candidate['payment_ids'])}
                    if selected.intersection(keys):
                        raise ValueError('所选候选共用材料，请只保留其中一种组合后再批量确认')
                    selected.update(keys)
                    # Every final choice is checked before any writes. The
                    # user's confirmation authorizes unresolved name candidates.
                    fields = row['fields']
                    validate_report_choice(fields.get('project_type'), fields.get('project_name'))
                    if fields.get('amount') in (None, ''):
                        raise ValueError('请为每组确认并填写金额')
                    amount_cents(fields['amount'])
                    rows.append((candidate, fields))
                # Inspect the complete competing pool once, before the first
                # group changes availability inside this same transaction.
                if self._rules_current_pool_snapshot(db, project_id) != expected_pool:
                    raise ValueError('待合成材料池已变化，请刷新后重新选择')
                for key in sorted(selected):
                    self._rules_material_state(cards[key], project_id, db=db, allow_name_review=True)
                refs = [self.create_expense(candidate['voucher_keys'], candidate['payment_ids'], project_id,
                            revisions=candidate['revisions'], expected_versions=candidate['expected_versions'],
                            fields=fields, _db=db) for candidate, fields in rows]
                record = {'item_ids': [result['item_id'] for result in refs],
                          'match_ids': [result['match_id'] for result in refs],
                          'new_expenses': sum(bool(result['created']) for result in refs), 'created_at': now()}
                db.execute('INSERT INTO material_creation_batches VALUES (?,?,?,?)',
                           (idempotency_key, project_id, fingerprint, json.dumps(record, ensure_ascii=False)))
        return self._batch_result(record, replayed=replayed)

    @staticmethod
    def _amount_choice(cards: list[dict[str, Any]]) -> dict[str, str]:
        choices = []
        for card in cards:
            choice = card.get('report_choice') or {}
            name = str(choice.get('project_name') or '').strip()
            if (not name or re.fullmatch(r'(?:普通)?(?:电子)?(?:发票|收据|付款|支付)(?:截图|凭证|文件)?\d*'
                    r'(?:[（(][^）)]*[）)])?', name) or
                    card['key'].startswith('q:') and name in {card['name'], Path(card['name']).stem}):
                continue
            try:
                choices.append(validate_report_choice(choice.get('project_type'), name))
            except ValueError:
                continue
        unique = {(choice['project_type'], choice['project_name']) for choice in choices}
        if len(unique) == 1:
            return choices[0]
        return validate_report_choice('other', '待核对费用')

    def amount_preview(self, project_id: Any, *, _db: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Read-only, prioritized amount matches; name relationships are irrelevant."""
        if not isinstance(project_id, str) or not project_id:
            raise ValueError('请先选择当前项目，再按金额配对')
        self._project(project_id)
        board = self.board(project_id)
        all_cards = [*board['vouchers'], *board['payments'], *board['other']]
        known_keys = {card['key'] for card in all_cards}
        # The ordinary board hides unavailable expense originals. Retain their
        # known amounts as blocked competitors in this explicit amount scan.
        for item in self.store.list_items():
            attachments = item['attachments']
            expense_vouchers = [row for row in attachments if row['role'] in {'invoice', 'receipt'}]
            key = 'e:' + item['id']
            if (key not in known_keys and item.get('project_id') == project_id and expense_vouchers and
                    not item.get('deleted') and item.get('synthesis_material_state') not in {'archived', 'deleted'} and
                    item['stage'] == 'draft' and not item['verified'] and not item.get('merged_into') and
                    not item.get('material_match') and not any(row['role'] == 'payment' for row in attachments) and
                    (not item['complete'] or item.get('returned_to_synthesis'))):
                all_cards.append({**person_fields(item), 'key': key, 'id': item['id'], 'item_id': item['id'], 'name': item['title'],
                                  'role': expense_vouchers[0]['role'], 'project_id': project_id, 'version': item['version'],
                                  'fields': {}, 'amount_cents': item['amount_cents'],
                                  'payment_not_required': item.get('category') == '高铁',
                                  'report_choice': item.get('report_choice', {})})
        population = {card['key']: {'version': card.get('version', card.get('revision')),
                      'role': card['role'], 'project_id': card['project_id'], 'amount_cents': card['amount_cents'], **person_fields(card)}
                      for card in all_cards}
        population.update({card['key']: {'version': card.get('version', card.get('revision')),
                           'role': card['role'], 'project_id': card['project_id'],
                           'amount_cents': card['amount_cents'], 'status': card['status'], **person_fields(card)}
                           for card in [*board['archived'], *board['deleted']]})
        skipped: list[dict[str, Any]] = []
        skipped.extend({'keys': [card['key']], 'reason': '材料已归档，不参与配对'} for card in board['archived'])
        skipped.extend({'keys': [card['key']], 'reason': '材料已删除，不参与配对'} for card in board['deleted'])
        vouchers: list[dict[str, Any]] = []
        payments: list[dict[str, Any]] = []
        blocked: dict[str, str] = {}
        by_number: dict[str, list[str]] = {}
        with nullcontext(_db) if _db is not None else self.store.connect() as db:
            for card in all_cards:
                reason = ''
                if card['project_id'] not in {'', project_id}:
                    reason = '材料项目待确认，请手工选择'
                elif card['role'] not in {'invoice', 'receipt', 'payment'}:
                    reason = '其他材料不按金额配对'
                elif card.get('payment_not_required'):
                    reason = '高铁发票无需付款截图'
                elif not card.get('amount_cents') or card['amount_cents'] <= 0:
                    reason = '金额未明确，请手工确认'
                elif card['role'] == 'payment' and self._rules_invalid_payment(card):
                    reason = '付款未成功或已退款'
                if reason:
                    skipped.append({'keys': [card['key']], 'reason': reason})
                    continue
                (payments if card['role'] == 'payment' else vouchers).append(card)
                try:
                    self._rules_material_state(card, project_id, db=db, amount_only=True)
                    if card['role'] != 'payment':
                        if card['key'].startswith('q:'):
                            number = self._creation_invoice_number(self._queue(db, card['id']))
                            if number:
                                self._check_invoice_number(db, number)
                            numbers = {number} if number else set()
                        else:
                            numbers = self._expense_numbers(db, self.store.get(card['id']))
                        for number in numbers:
                            by_number.setdefault(number, []).append(card['key'])
                except ValueError as exc:
                    blocked[card['key']] = str(exc)
            for keys in by_number.values():
                if len(keys) > 1:
                    blocked.update((key, '票号重复，请手工处理') for key in keys)
        limits = {'max_vouchers': 20, 'max_payments': 32, 'max_each_side': 4, 'max_candidates': 50000}
        selected: list[tuple[Candidate, str]] = []
        selected_keys: set[str] = set()
        if len(vouchers) > limits['max_vouchers'] or len(payments) > limits['max_payments']:
            skipped.append({'keys': [card['key'] for card in [*vouchers, *payments]],
                            'reason': '材料超过20份票据或32份付款，请分批手工配对'})
        else:
            # Unassigned evidence remains a possible competitor to a known
            # person's proofs; do not hide it by scanning people separately.
            owner_candidates, _, owner_pair_count = _balanced_candidates(vouchers, payments)
            for rows in owner_candidates.values():
                for owner_voucher_keys, owner_payment_keys in rows:
                    candidate_cards = [*(card for card in vouchers if card['key'] in owner_voucher_keys),
                                       *(card for card in payments if card['id'] in owner_payment_keys)]
                    if not people_compatible(candidate_cards):
                        blocked.update((card['key'], '费用归属人尚未全部指定，请手工确认归属') for card in candidate_cards)
            if owner_pair_count > limits['max_candidates']:
                blocked.update((card['key'], '候选组合超过50000组，请手工配对') for card in [*vouchers, *payments])
            voucher_buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
            payment_buckets: dict[tuple[str, int], list[dict[str, Any]]] = {}
            for card in vouchers:
                voucher_buckets.setdefault((str(card.get('reimbursement_person_id') or ''), card['amount_cents']), []).append(card)
            for card in payments:
                payment_buckets.setdefault((str(card.get('reimbursement_person_id') or ''), card['amount_cents']), []).append(card)
            # Clear one-to-one buckets take priority over large balanced unions.
            for bucket_key, bucket in sorted(voucher_buckets.items()):
                counterpart = payment_buckets.get(bucket_key, [])
                if len(bucket) == len(counterpart) == 1:
                    left, right = bucket[0], counterpart[0]
                    if left['key'] not in blocked and right['key'] not in blocked:
                        selected.append((((left['key'],), (right['id'],)), '唯一一票一付款'))
                        selected_keys.update((left['key'], right['key']))
            remaining_vouchers = [card for card in vouchers if card['key'] not in selected_keys]
            remaining_payments = [card for card in payments if card['key'] not in selected_keys]
            candidates, _, pair_count = _balanced_candidates(remaining_vouchers, remaining_payments)
            if pair_count > limits['max_candidates']:
                skipped.append({'keys': [card['key'] for card in [*remaining_vouchers, *remaining_payments]],
                                'reason': '候选组合超过50000组，请手工配对'})
            else:
                for component in _candidate_components([candidate for rows in candidates.values() for candidate in rows]):
                    component_keys = {key for left, right in component for key in [*left, *('q:' + key for key in right)]}
                    if len(component) == 1 and not component_keys.intersection(blocked):
                        selected.append((component[0], '唯一金额合计组合'))
                        selected_keys.update(component_keys)
                    else:
                        skipped.append({'keys': sorted(component_keys), 'reason': '金额组合有歧义，请手工选择' if len(component) > 1 else
                                        '材料信息待确认，请手工处理'})
            reported = {key for row in skipped for key in row['keys']}
            for card in [*vouchers, *payments]:
                if card['key'] not in selected_keys and card['key'] not in reported:
                    skipped.append({'keys': [card['key']], 'reason': blocked.get(card['key'],
                                    '未找到唯一金额组合；每组最多4份票据和4份付款')})
        cards = {card['key']: card for card in [*vouchers, *payments]}
        groups = []
        for (voucher_keys, payment_ids), reason in selected:
            voucher_cards = [cards[key] for key in voucher_keys]
            payment_cards = [cards['q:' + key] for key in payment_ids]
            choice = self._amount_choice(voucher_cards)
            if choice['project_name'] == '待核对费用':
                choice = self._amount_choice([*voucher_cards, *payment_cards])
            cents = sum(card['amount_cents'] for card in voucher_cards)
            identity = {'project_id': project_id, 'voucher_keys': list(voucher_keys), 'payment_ids': list(payment_ids)}
            groups.append({**identity, 'id': 'a:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24],
                **common_person([*voucher_cards, *payment_cards]),
                'revisions': {card['id']: card['revision'] for card in [*voucher_cards, *payment_cards] if card['key'].startswith('q:')},
                'expected_versions': {card['id']: card['version'] for card in voucher_cards if card['key'].startswith('e:')},
                'amount_cents': cents, 'amount': f'{cents // 100}.{cents % 100:02d}',
                'voucher_count': len(voucher_keys), 'payment_count': len(payment_ids),
                'report_choice': choice, 'title': choice['project_name'],
                'fields': {'project_type': choice['project_type'], 'project_name': choice['project_name'],
                           'amount': f'{cents // 100}.{cents % 100:02d}'}, 'reason': reason,
                'amount_only': True, 'needs_review': True})
        groups.sort(key=lambda row: (row['amount_cents'], row['id']))
        fingerprint = {'mode': 'amount_only_v1', 'project_id': project_id, 'population': population,
                       'groups': groups, 'skipped': skipped, 'limits': limits}
        snapshot_token = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {'project_id': project_id, 'snapshot_token': snapshot_token, 'groups': groups, 'skipped': skipped,
                'summary': {'groups': len(groups), 'materials': len(selected_keys),
                            'skipped_materials': len({key for row in skipped for key in row['keys']}),
                            'amount_cents': sum(row['amount_cents'] for row in groups)}, 'limits': limits,
                'message': '仅按金额选择唯一组合；商家和商品对应关系仍需核对，确认后进入待核对。',
                'requests': 0, 'engine': 'amount_only'}

    def amount_confirm(self, project_id: Any, snapshot_token: Any, idempotency_key: Any,
                       amount_only_confirmed: Any) -> dict[str, Any]:
        if (not isinstance(project_id, str) or not project_id or
                not isinstance(snapshot_token, str) or not re.fullmatch(r'[a-f0-9]{64}', snapshot_token) or
                not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', idempotency_key) or
                amount_only_confirmed is not True):
            raise ValueError('请明确确认按金额配对，并提供当前预览及有效确认编号')
        self._project(project_id)
        fingerprint = hashlib.sha256(json.dumps({'mode': 'amount_only_v1', 'project_id': project_id,
            'snapshot_token': snapshot_token}, sort_keys=True).encode()).hexdigest()
        replayed = False
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT project_id,fingerprint,data FROM material_creation_batches WHERE id=?',
                               (idempotency_key,)).fetchone()
            if prior:
                if prior['project_id'] != project_id or prior['fingerprint'] != fingerprint:
                    raise ValueError('确认编号已用于另一批材料，请重新预览并确认')
                record = _data(prior['data'])
                replayed = True
            else:
                preview = self.amount_preview(project_id, _db=db)
                if preview['snapshot_token'] != snapshot_token:
                    raise ValueError('按金额配对的材料池或预览已变化，请重新预览后确认')
                if not preview['groups']:
                    raise ValueError('没有可确认的唯一金额组合，请在合成台手工选择')
                # All candidates and originals were checked before any row moves.
                # Disjoint groups can now use the existing atomic creation path.
                refs = [self.create_expense(row['voucher_keys'], row['payment_ids'], project_id,
                            revisions=row['revisions'], expected_versions=row['expected_versions'], fields=row['fields'],
                            _db=db) for row in preview['groups']]
                for ref in refs:
                    item_id, group_id = ref['item_id'], ref['match_id']
                    self._check_member_originals(self._member_state(db, item_id))
                    stored = _data(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
                    reason = '用户确认仅按金额配对；商家及内容待核对，未核验或提交'
                    stored['material_match'].update(reason=reason, amount_only=True)
                    db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
                    db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, now(), reason))
                    group = _data(db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()['data'])
                    group.update(reason=reason, amount_only=True, aggregate_after=self._member_state(db, item_id))
                    db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group_id))
                record = {'mode': 'amount_only_v1', 'item_ids': [ref['item_id'] for ref in refs],
                          'match_ids': [ref['match_id'] for ref in refs], 'new_expenses': sum(ref['created'] for ref in refs)}
                db.execute('INSERT INTO material_creation_batches VALUES (?,?,?,?)',
                           (idempotency_key, project_id, fingerprint, json.dumps(record, ensure_ascii=False)))
        return self._batch_result(record, replayed=replayed)

    def _match_single(self, voucher_key: str, payment_ids: list[str], project_id: str,
                      expected_version: str = '', revisions: dict[str, Any] | None = None,
                      fields: dict[str, Any] | None = None, provisional: bool = True,
                      reason: str = '', support_ids: list[str] | None = None,
                      expected_materials: list[dict[str, Any]] | None = None,
                      expected_pool: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._project(project_id)
        if not isinstance(payment_ids, list) or not 1 <= len(payment_ids) <= 100:
            raise ValueError('请选择1至100份付款截图')
        ids = [self._payment_id(key) for key in payment_ids]
        if len(ids) != len(set(ids)):
            raise ValueError('付款截图不能重复选择')
        if not isinstance(voucher_key, str) or not voucher_key.startswith(('q:', 'e:')):
            raise ValueError('请选择票据或已有费用')
        revisions = revisions or {}
        fields = fields or {}
        if not isinstance(revisions, dict) or not isinstance(fields, dict) or not isinstance(provisional, bool):
            raise ValueError('匹配参数无效')
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ValueError('匹配说明过长')
        overrides = fields.get('payment_amounts', {})
        if not isinstance(overrides, dict):
            raise ValueError('请逐份确认付款金额')
        group_id = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for card in expected_materials or []:
                self._rules_material_state(card, project_id, db=db)
            if expected_pool is not None and self._rules_current_pool_snapshot(db, project_id) != expected_pool:
                raise ValueError('当前项目材料池已变化，请刷新后重新自动配对')
            payments = [self._queue(db, key) for key in ids]
            for payment in payments:
                self._check_entry(db, payment, project_id, revisions, {'payment'})
            supports = self._supports(db, support_ids or [], project_id, revisions)
            if len({entry['digest'] for entry in payments}) != len(payments):
                raise ValueError('相同付款原件不能重复计入')
            total = 0
            payment_amounts: dict[str, int] = {}
            for payment in payments:
                cents = _identified_amount(payment)
                override = overrides.get(payment['id'], overrides.get('q:' + payment['id']))
                if override not in (None, ''):
                    supplied = amount_cents(override)
                    if cents is not None and supplied != cents:
                        raise ValueError('人工付款金额与已识别金额不一致，请先修正材料识别')
                    cents = supplied
                if cents is None or cents <= 0:
                    raise ValueError('无法读取付款金额，请逐份人工明确正值金额')
                total += cents
                payment_amounts[payment['id']] = cents
            queue_voucher: dict[str, Any] | None = None
            previous: dict[str, Any] | None = None
            if voucher_key.startswith('e:'):
                item_id = voucher_key[2:]
                item = self.store.get(item_id)
                if item['version'] != expected_version:
                    raise ValueError('费用版本已变化，请刷新后重新选择')
                if item.get('project_id') != project_id or self._expense_card(item) is None:
                    raise ValueError('只接受当前项目缺付款且未核对的草稿费用')
                previous = _data(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
                stored = dict(previous)
                bill_cents = item['amount_cents']
                if fields.get('amount') not in (None, '') and amount_cents(fields['amount']) != bill_cents:
                    raise ValueError('不得覆盖已有费用金额')
            else:
                queue_voucher = self._queue(db, voucher_key[2:])
                self._check_entry(db, queue_voucher, project_id, revisions, {'invoice', 'receipt'})
                bill_cents = _identified_amount(queue_voucher)
                bill_supplied = fields.get('amount')
                if bill_supplied not in (None, ''):
                    manual = amount_cents(bill_supplied)
                    if bill_cents is not None and manual != bill_cents:
                        raise ValueError('票据金额与已识别金额不一致，请先修正材料识别')
                    bill_cents = manual
                if bill_cents is None or bill_cents <= 0:
                    raise ValueError('无法读取票据金额，请人工明确正值金额')
                values = {**queue_voucher.get('fields', {}), **fields, 'project_id': project_id,
                          'project': project['name'], 'amount': f'{bill_cents / 100:.2f}',
                          'material_basis': 'invoice_payment'}
                values.setdefault('title', Path(queue_voucher['name']).stem)
                values.setdefault('expense_date', '')
                invoice_number = str(values.get('invoice_number', '')).strip()
                self._check_invoice_number(db, invoice_number)
                stored = self.store._validate(values)
                if invoice_number:
                    stored['invoice_number'] = invoice_number
                item_id = uuid.uuid4().hex[:10]
                stored.update(id=item_id, created_at=now(), updated_at=now(), stage='draft', verified=False)
            if total != bill_cents:
                raise ValueError('付款合计与票据金额不一致，不能关联')
            entries = ([queue_voucher] if queue_voucher else []) + payments + supports
            owner = self._selected_person(db, project_id, [*entries, *([previous] if previous else [])], fields)
            stored.update(owner)
            if previous:
                previous.update(owner)
            for entry in entries:
                entry.update(owner)
            if len({entry['digest'] for entry in entries}) != len(entries):
                raise ValueError('票据和付款必须是独立原件')
            transaction_records = [*entries, *([previous] if previous else [])]
            transaction_previous = _transaction_metadata([previous] if previous else [queue_voucher] if queue_voucher else [])
            stored.update(self._check_transaction_selection(db, transaction_records, {item_id} if previous else set()))
            if previous is None:
                assert queue_voucher is not None
                if db.execute('SELECT 1 FROM expenses WHERE source=?', ('intake:' + queue_voucher['id'],)).fetchone():
                    raise ValueError('该票据已登记费用，不能重复记账')
                db.execute('INSERT INTO expenses VALUES (?,?,?)', (item_id, 'intake:' + queue_voucher['id'],
                           json.dumps(stored, ensure_ascii=False)))
            added: list[str] = []
            payment_attachments: list[str] = []
            support_attachments: list[str] = []
            before = {entry['id']: dict(entry) for entry in entries}
            if previous is not None:
                for source_row in db.execute('SELECT data FROM intake_queue').fetchall():
                    source_entry = _data(source_row['data'])
                    if source_entry.get('item_id') != item_id or source_entry['id'] in before:
                        continue
                    source_entry.update(owner)
                    before[source_entry['id']] = dict(source_entry)
                    source_entry.update(revision=source_entry['revision'] + 1, material_match_id=group_id)
                    self._write_queue(db, source_entry)
            for entry in entries:
                attachment_id = uuid.uuid4().hex
                db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)', (attachment_id, item_id,
                           entry['name'], _material_role(entry), entry['path'], entry['digest']))
                added.append(attachment_id)
                if entry['role'] == 'payment':
                    payment_attachments.append(attachment_id)
                if entry['id'] in (support_ids or []):
                    support_attachments.append(attachment_id)
                linked = {**entry, 'project_id': project_id, 'item_id': item_id, 'status': 'linked', 'revision': entry['revision'] + 1,
                          'result': '材料合成台已关联；金额对应，仍待核对', 'issues': [],
                          'material_match_id': group_id}
                self._write_queue(db, linked)
            description = reason.strip() or '金额已对应，商家关系待核对'
            grouping = {'group_id': group_id, 'provisional': provisional, 'reason': description,
                        'transaction_previous': {key: transaction_previous.get(key, [] if key == 'transaction_identifiers' else '')
                                                 for key in (*TRANSACTION_FIELDS, 'transaction_identifiers')},
                        'payment_total_cents': total, 'payment_count': len(payments),
                        'support_ids': support_ids or [], 'support_count': len(supports),
                        'support_attachment_ids': support_attachments}
            stored.update(material_basis='invoice_payment', material_match=grouping,
                          review_requested=True, verified=False, updated_at=now())
            stored.pop('returned_to_synthesis', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            group = {'id': group_id, 'project_id': project_id, 'item_id': item_id, 'voucher_key': voucher_key,
                     'payment_ids': ids, 'attachment_ids': added, 'payment_attachment_ids': payment_attachments,
                     'queue_before': before, 'bill_previous': previous, 'provisional': provisional,
                     'queue_after': {key: self._queue(db, key) for key in before},
                     'status': 'provisional' if provisional else 'confirmed', 'reason': description,
                     'payment_total_cents': total, 'payment_amounts': payment_amounts,
                     'payment_count': len(payments), 'created_at': now(), 'support_ids': support_ids or [],
                     'support_count': len(supports),
                     'support_attachment_ids': support_attachments}
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item_id, now(), '材料合成：付款合计已对应，未核验或提交；' + description))
            group.update(aggregate_after=self._member_state(db, item_id),
                         queue_after={key: self._queue(db, key) for key in before})
            db.execute('INSERT INTO material_matches VALUES (?,?)', (group_id, json.dumps(group, ensure_ascii=False)))
        return {'item': self.store.get(item_id), 'match': self._public_match(group)}

    @staticmethod
    def _member_state(db: sqlite3.Connection, item_id: str) -> dict[str, Any]:
        row = db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise ValueError('成员费用不存在')
        return {'data': _data(row['data']),
                'attachments': [dict(row) for row in db.execute('SELECT * FROM attachments WHERE item_id=? ORDER BY id', (item_id,))],
                'events': [dict(row) for row in db.execute('SELECT * FROM events WHERE item_id=? ORDER BY id', (item_id,))]}

    @staticmethod
    def preserve_edit_guard(db: sqlite3.Connection, item_id: str) -> None:
        """Older schema1 groups must retain the state before a manual edit."""
        state = MaterialMatching._member_state(db, item_id)
        group_id = state['data'].get('material_match', {}).get('group_id')
        if not group_id:
            return
        row = db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()
        if not row:
            raise ValueError('材料组合不存在，请刷新')
        group = _data(row['data'])
        if group.get('aggregate_after'):
            return
        if group['item_id'] != item_id or group['status'] == 'undone':
            raise ValueError('材料组合已变化，请刷新')
        group['aggregate_after'] = state
        group['queue_after'] = {key: MaterialMatching._queue(db, key) for key in group.get('queue_before', {})}
        db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group_id))

    @staticmethod
    def record_expense_action(db: sqlite3.Connection, item_id: str, before: dict[str, Any]) -> None:
        """Rebase deletion/restoration only when the prior group snapshot was intact."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_matches'").fetchone():
            return
        after = MaterialMatching._member_state(db, item_id)
        for row in db.execute('SELECT id,data FROM material_matches').fetchall():
            group = _data(row['data'])
            if group['item_id'] == item_id and group['status'] != 'undone' and group.get('aggregate_after') == before:
                group['aggregate_after'] = after
                db.execute('UPDATE material_matches SET data=? WHERE id=?',
                           (json.dumps(group, ensure_ascii=False), row['id']))

    @staticmethod
    def move_expense(db: sqlite3.Connection, item_id: str, project: dict[str, Any]) -> None:
        """Move one expense with its consumed members and undo ownership, atomically.

        Only ownership fields and this operation's events are rebased. Any older
        content change remains a mismatch, so undo cannot overwrite later edits.
        """
        state = MaterialMatching._member_state(db, item_id)
        if state['data'].get('reimbursement_person_id') and state['data'].get('project_id') != project['id']:
            raise ValueError('费用归属人属于原项目；请先改为未指定，迁移后再选择目标项目费用归属人')
        group_id = state['data'].get('material_match', {}).get('group_id')
        group_row = db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone() if group_id else None
        if group_id and not group_row:
            raise ValueError('材料组合不存在，请刷新后重试')
        group = _data(group_row['data']) if group_row else None
        if group and (group['item_id'] != item_id or group['status'] == 'undone'):
            raise ValueError('费用的材料组合已变化，请刷新后重试')
        timestamp = now()

        def ownership(data: dict[str, Any]) -> None:
            data.update(project_id=project['id'], project=project['name'], updated_at=timestamp,
                        verified=False, review_requested=data['stage'] == 'draft')

        def queue_ownership(entry: dict[str, Any]) -> None:
            entry['project_id'] = project['id']
            for key, value in (('project_id', project['id']), ('project', project['name'])):
                if key in entry.get('fields', {}):
                    entry['fields'][key] = value

        member_ids = list(group.get('member_before', {})) if group else []
        states = {key: MaterialMatching._member_state(db, key) for key in dict.fromkeys([item_id, *member_ids])}
        if group:
            for key in group.get('queue_before', {}):
                entry = MaterialMatching._queue(db, key)
                if entry.get('item_id') not in states or entry.get('material_match_id') != group_id:
                    raise ValueError('组合收件关联已变化，请刷新后重试')
        for key, previous in states.items():
            if key != item_id and previous['data'].get('merged_into') != item_id:
                raise ValueError('合并成员归属已变化，不能迁移原组合')
            data = dict(previous['data'])
            if returned_material_state(db, data):
                data['returned_to_synthesis'] = True
            ownership(data)
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), key))
            cursor = db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                                (key, timestamp, '迁移至项目：' + project['name'] + '；保留原件及金额，待核对'))
            event = dict(db.execute('SELECT * FROM events WHERE id=?', (cursor.lastrowid,)).fetchone())
            if group:
                for snapshot in [group.get('aggregate_after') if key == item_id else None,
                                 group.get('member_after', {}).get(key)]:
                    if snapshot:
                        ownership(snapshot['data'])
                        snapshot['events'].append(event)
                original = group.get('member_before', {}).get(key)
                if original:
                    ownership(original['data'])

        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intake_queue'").fetchone():
            linked = [_data(row['data']) for row in db.execute('SELECT data FROM intake_queue')]
            for entry in linked:
                if entry.get('item_id') not in states:
                    continue
                if group and entry['id'] in group.get('queue_before', {}) and entry.get('material_match_id') != group_id:
                    raise ValueError('组合收件关联已变化，请刷新后重试')
                queue_ownership(entry)
                entry['revision'] += 1
                MaterialMatching._write_queue(db, entry)
                if group:
                    expected = group.get('queue_after', {}).get(entry['id'])
                    if expected:
                        queue_ownership(expected)
                        expected['revision'] += 1
                    original = group.get('queue_before', {}).get(entry['id'])
                    if original:
                        queue_ownership(original)
        if group:
            if group.get('bill_previous'):
                ownership(group['bill_previous'])
            group.update(project_id=project['id'], moved_at=timestamp)
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group_id))

    def _expense_numbers(self, db: sqlite3.Connection, item: dict[str, Any]) -> set[str]:
        numbers = {str(number).strip() for number in item.get('invoice_numbers', []) if str(number).strip()}
        if item.get('invoice_number'):
            numbers.add(str(item['invoice_number']).strip())
        for row in db.execute('SELECT data FROM intake_queue'):
            entry = _data(row['data'])
            number = entry.get('fields', {}).get('invoice_number')
            if entry.get('item_id') == item['id'] and number:
                numbers.add(str(number).strip())
        for row in db.execute("SELECT path FROM attachments WHERE item_id=? AND role='invoice'", (item['id'],)):
            path = Path(row['path']).resolve()
            if path.suffix.lower() != '.pdf' or not path.is_relative_to(self.store.workspace) or not path.is_file():
                continue
            try:
                with pymupdf.open(path) as document:  # type: ignore[no-untyped-call]
                    if document.needs_pass:
                        continue
                    text = '\n'.join(document[index].get_text() for index in range(min(10, len(document))))
                number = parse_invoice_number(text)
                if number:
                    numbers.add(number)
            except (ValueError, OSError, RuntimeError):
                continue
        return numbers

    def _match_multiple(self, keys: list[str], payment_ids: list[str], project_id: str,
                        versions: dict[str, str], revisions: dict[str, Any], fields: dict[str, Any],
                        provisional: bool, reason: str, support_ids: list[str],
                        expected_materials: list[dict[str, Any]] | None = None,
                        expected_pool: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._project(project_id)
        if (not isinstance(payment_ids, list) or not 1 <= len(payment_ids) <= 100 or
                not isinstance(revisions, dict) or not isinstance(fields, dict) or
                not isinstance(provisional, bool) or not isinstance(reason, str) or len(reason) > 2000):
            raise ValueError('材料合成参数无效')
        payment_keys = [self._payment_id(key) for key in payment_ids]
        if len(payment_keys) != len(set(payment_keys)):
            raise ValueError('付款截图不能重复选择')
        payment_overrides = fields.get('payment_amounts', {})
        voucher_overrides = fields.get('voucher_amounts', {})
        if not isinstance(payment_overrides, dict) or not isinstance(voucher_overrides, dict):
            raise ValueError('请逐份明确原件金额')
        group_id, item_id = uuid.uuid4().hex, uuid.uuid4().hex[:10]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for card in expected_materials or []:
                self._rules_material_state(card, project_id, db=db)
            if expected_pool is not None and self._rules_current_pool_snapshot(db, project_id) != expected_pool:
                raise ValueError('当前项目材料池已变化，请刷新后重新自动配对')
            payments = [self._queue(db, key) for key in payment_keys]
            supports = self._supports(db, support_ids, project_id, revisions)
            payment_amounts: dict[str, int] = {}
            for payment in payments:
                self._check_entry(db, payment, project_id, revisions, {'payment'})
                cents = _identified_amount(payment)
                raw = payment_overrides.get(payment['id'], payment_overrides.get('q:' + payment['id']))
                if raw not in (None, ''):
                    manual = amount_cents(raw)
                    if cents is not None and manual != cents:
                        raise ValueError('人工付款金额与识别金额不一致')
                    cents = manual
                if cents is None or cents <= 0:
                    raise ValueError('请逐份人工明确付款正值金额')
                payment_amounts[payment['id']] = cents
            queue_vouchers: list[dict[str, Any]] = []
            members: dict[str, dict[str, Any]] = {}
            vouchers: list[dict[str, Any]] = []
            digests: set[str] = set()
            numbers: set[str] = set()
            for key in keys:
                if key.startswith('e:'):
                    member_id = key[2:]
                    current = self.store.get(member_id)
                    if current['version'] != versions.get(member_id, versions.get(key)):
                        raise ValueError('成员费用版本已变化，请刷新后重新选择')
                    if (current.get('project_id') != project_id or self._expense_card(current) is None or
                            current.get('material_match')):
                        raise ValueError('成员必须是当前项目未核对、未提交且缺付款的草稿')
                    state = self._member_state(db, member_id)
                    members[member_id] = state
                    info = {field: current.get(field, '') for field in ('title', 'category', 'expense_date', 'merchant')}
                    cents = current['amount_cents']
                    member_numbers = self._expense_numbers(db, current)
                    if numbers & member_numbers:
                        raise ValueError('同组票据号码重复，不能重复计费')
                    numbers.update(member_numbers)
                    for attachment in state['attachments']:
                        if attachment['digest'] in digests:
                            raise ValueError('同组原件摘要重复，不能重复计费')
                        digests.add(attachment['digest'])
                    vouchers.append({'key': key, 'item_id': member_id, 'amount_cents': cents,
                                     'invoice_numbers': sorted(member_numbers), **info})
                else:
                    entry = self._queue(db, key[2:])
                    self._check_entry(db, entry, project_id, revisions, {'invoice', 'receipt'})
                    if entry['digest'] in digests:
                        raise ValueError('同组原件摘要重复')
                    digests.add(entry['digest'])
                    cents = _identified_amount(entry)
                    raw = voucher_overrides.get(entry['id'], voucher_overrides.get(key))
                    if raw not in (None, ''):
                        manual = amount_cents(raw)
                        if cents is not None and manual != cents:
                            raise ValueError('人工票据金额与已识别金额不一致')
                        cents = manual
                    if cents is None or cents <= 0:
                        raise ValueError('请逐份人工明确票据正值金额')
                    original = entry.get('fields') or {}
                    number = str(original.get('invoice_number', '')).strip()
                    if number:
                        if number in numbers:
                            raise ValueError('同组票据号码重复')
                        self._check_invoice_number(db, number)
                        numbers.add(number)
                    queue_vouchers.append(entry)
                    vouchers.append({'key': key, 'queue_id': entry['id'], 'amount_cents': cents,
                                     'title': original.get('title') or Path(entry['name']).stem,
                                     'category': original.get('category', '其他'),
                                     'expense_date': original.get('expense_date', ''),
                                     'merchant': original.get('merchant', ''),
                                     'invoice_numbers': [number] if number else []})
            for entry in [*payments, *supports]:
                if entry['digest'] in digests:
                    raise ValueError('同组票据和付款原件重复')
                digests.add(entry['digest'])
            voucher_total = sum(voucher['amount_cents'] for voucher in vouchers)
            payment_total = sum(payment_amounts.values())
            if voucher_total != payment_total:
                raise ValueError('票据合计与付款合计不一致，不能合成')
            if fields.get('amount') not in (None, '') and amount_cents(fields['amount']) != voucher_total:
                raise ValueError('人工合计与票据合计不一致')
            categories = {voucher['category'] for voucher in vouchers}
            dates = {voucher['expense_date'] for voucher in vouchers}
            category = next(iter(categories)) if len(categories) == 1 else '其他'
            common_date = next(iter(dates)) if len(dates) == 1 else ''
            description = reason.strip() or '多票与付款合计已对应，商家关系待核对'
            owner = self._selected_person(db, project_id, [*queue_vouchers, *payments, *supports,
                                          *(state['data'] for state in members.values())], fields)
            for record in [*queue_vouchers, *payments, *supports, *vouchers, *(state['data'] for state in members.values())]:
                record.update(owner)
            stored = self.store._validate({**fields, **owner, 'title': fields.get('title') or f'合成费用 · {len(keys)}张票据',
                     **self._check_transaction_selection(db, [*queue_vouchers, *payments, *supports,
                            *(state['data'] for state in members.values())], set(members)),
                     'category': fields.get('category') or category, 'amount': f'{voucher_total / 100:.2f}',
                     'project_id': project_id, 'project': project['name'], 'material_basis': 'invoice_payment',
                     'expense_date': fields.get('expense_date', common_date),
                     'note': fields.get('note') or '多张票据合并计费；每票原始名称、类别、日期和金额保留在分项证据中。'})
            grouping = {'group_id': group_id, 'provisional': provisional, 'reason': description,
                        'voucher_count': len(keys), 'voucher_total_cents': voucher_total,
                        'payment_count': len(payments), 'payment_total_cents': payment_total,
                        'support_ids': support_ids, 'support_count': len(supports), 'support_attachment_ids': []}
            stored.update(id=item_id, created_at=now(), updated_at=now(), stage='draft', verified=False,
                          review_requested=True, material_match=grouping, material_members=vouchers,
                          invoice_numbers=sorted(numbers))
            db.execute('INSERT INTO expenses VALUES (?,?,?)', (item_id, 'material-match:' + group_id,
                       json.dumps(stored, ensure_ascii=False)))
            entries = queue_vouchers + payments + supports
            queue_before = {entry['id']: dict(entry) for entry in entries}
            for row in db.execute('SELECT data FROM intake_queue'):
                entry = _data(row['data'])
                if entry.get('item_id') in members:
                    entry.update(owner)
                    queue_before[entry['id']] = entry
            moved: list[dict[str, Any]] = []
            for member_id, state in members.items():
                moved.extend(state['attachments'])
                db.execute('UPDATE attachments SET item_id=? WHERE item_id=?', (item_id, member_id))
                merged = {**state['data'], 'stage': 'cancelled', 'merged_into': item_id,
                          'merged_by': group_id, 'updated_at': now()}
                db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(merged, ensure_ascii=False), member_id))
                db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                           (member_id, now(), '材料合成：原费用合并到 ' + item_id + '；原金额保留，不重复计费'))
            added: list[str] = []
            support_attachments: list[str] = []
            for entry in entries:
                attachment_id = uuid.uuid4().hex
                db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)', (attachment_id, item_id,
                           entry['name'], _material_role(entry), entry['path'], entry['digest']))
                added.append(attachment_id)
                if entry['id'] in support_ids:
                    support_attachments.append(attachment_id)
            stored['material_match']['support_attachment_ids'] = support_attachments
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            queue_after: dict[str, dict[str, Any]] = {}
            for key, entry in queue_before.items():
                linked = {**entry, 'item_id': item_id, 'revision': entry['revision'] + 1,
                          'material_match_id': group_id, 'result': '材料合成已关联；未核验或提交'}
                if key in {entry['id'] for entry in entries}:
                    linked.update(status='linked', project_id=project_id, issues=[])
                self._write_queue(db, linked)
                queue_after[key] = linked
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item_id, now(), '多对多材料合成：票据与付款合计一致，仅一笔合计；未核验或提交'))
            group = {'id': group_id, 'schema_version': 2, 'project_id': project_id, 'item_id': item_id,
                     'voucher_key': keys[0], 'voucher_keys': keys, 'vouchers': vouchers,
                     'voucher_total_cents': voucher_total, 'payment_ids': payment_keys,
                     'payment_amounts': payment_amounts, 'payment_total_cents': payment_total,
                     'payment_count': len(payments), 'attachment_ids': added,
                     'support_ids': support_ids, 'support_count': len(supports), 'support_attachment_ids': support_attachments,
                     'moved_attachments': moved, 'member_before': members,
                     'member_after': {key: self._member_state(db, key) for key in members},
                     'queue_before': queue_before, 'queue_after': queue_after,
                     'aggregate_after': self._member_state(db, item_id), 'provisional': provisional,
                     'status': 'provisional' if provisional else 'confirmed', 'reason': description,
                     'created_at': now()}
            db.execute('INSERT INTO material_matches VALUES (?,?)', (group_id, json.dumps(group, ensure_ascii=False)))
        return {'item': self.store.get(item_id), 'match': self._public_match(group)}

    @staticmethod
    def _assert_group_snapshot(db: sqlite3.Connection, group: dict[str, Any]) -> None:
        if group.get('aggregate_after') is not None and MaterialMatching._member_state(db, group['item_id']) != group['aggregate_after']:
            raise ValueError('合成费用后续内容已变化，不能覆盖后续编辑')
        for key, expected in group.get('member_after', {}).items():
            if MaterialMatching._member_state(db, key) != expected:
                raise ValueError('成员费用后续内容已变化，整组不能撤回')
        for key, expected in group.get('queue_after', {}).items():
            if MaterialMatching._queue(db, key) != expected:
                raise ValueError('成员收件后续内容已变化，整组不能撤回')

    def supplement(self, group_id: str, support_ids: list[str], *, expected_version: str,
                   revisions: dict[str, Any]) -> dict[str, Any]:
        support_keys = self._support_keys(support_ids)
        if not support_keys or not isinstance(revisions, dict):
            raise ValueError('请选择补充材料并提供收件版本')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()
            if row is None:
                raise ValueError('匹配记录不存在')
            group = _data(row['data'])
            item_id, project_id = group['item_id'], group['project_id']
            self._project(project_id)
            item = self.store.get(item_id)
            if item['version'] != expected_version:
                raise ValueError('费用版本已变化，请刷新后重试')
            if (group['status'] != 'provisional' or not group['provisional'] or item['stage'] != 'draft' or
                    item.get('deleted') or item['verified'] or item.get('merged_into') or item.get('project_id') != project_id):
                raise ValueError('仅可向当前未核对的暂存草稿组补件')
            if (item.get('material_match', {}).get('group_id') != group_id or
                    item['material_basis'] != 'invoice_payment' and not (
                        item.get('category') == '打车' and item['material_basis'] == 'standard')):
                raise ValueError('费用的匹配关系已变化')
            self._assert_group_snapshot(db, group)
            # Older schema1 groups had no snapshots. Check their known linked
            # originals before adding a snapshot; do not silently adopt a moved proof.
            if not group.get('queue_after'):
                for key in group['queue_before']:
                    current = self._queue(db, key)
                    if current.get('item_id') != item_id or current.get('material_match_id') != group_id:
                        raise ValueError('原组材料的关联已变化')
            supports = self._supports(db, support_keys, project_id, revisions)
            common_person([item, *supports])
            transaction_metadata = self._check_transaction_selection(
                db, [item, *supports], {item_id}, count_amounts=False)
            added: list[str] = []
            for entry in supports:
                attachment_id = uuid.uuid4().hex
                role = self._attachment_role(entry, item['category'])
                db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)', (attachment_id, item_id,
                           entry['name'], role, entry['path'], entry['digest']))
                added.append(attachment_id)
                group['queue_before'][entry['id']] = dict(entry)
                linked = {**entry, 'project_id': project_id, 'item_id': item_id, 'status': 'linked',
                          'revision': entry['revision'] + 1, 'result': '补充材料已关联；不计金额，未核验或提交',
                          'issues': [], 'material_match_id': group_id,
                          'role': entry['role'] if _material_role(entry) != entry['role'] else role}
                self._write_queue(db, linked)
            group['support_ids'] = [*group.get('support_ids', []), *support_keys]
            group['support_count'] = len(group['support_ids'])
            group['support_attachment_ids'] = [*group.get('support_attachment_ids', []), *added]
            group['attachment_ids'].extend(added)
            stored = _data(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            stored.update(transaction_metadata)
            stored['material_match'].update(support_ids=group['support_ids'], support_count=group['support_count'],
                                             support_attachment_ids=group['support_attachment_ids'])
            stored['updated_at'] = now()
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item_id, now(), f'关联{len(supports)}份补充材料；费用金额不变，未核验或提交'))
            group.update(aggregate_after=self._member_state(db, item_id),
                         queue_after={key: self._queue(db, key) for key in group['queue_before']}, supplemented_at=now())
            db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group_id))
        return {'item': self.store.get(item_id), 'match': self._public_match(group)}

    def _undo_multiple(self, db: sqlite3.Connection, group: dict[str, Any], item: dict[str, Any]) -> None:
        item_id = item['id']
        for attachment in group['moved_attachments']:
            db.execute('UPDATE attachments SET item_id=? WHERE id=?', (attachment['item_id'], attachment['id']))
        for attachment_id in group['attachment_ids']:
            db.execute('DELETE FROM attachments WHERE id=? AND item_id=?', (attachment_id, item_id))
        for key, state in group['member_before'].items():
            restored_member = {**state['data'], 'returned_to_synthesis': True}
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(restored_member, ensure_ascii=False), key))
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (key, now(), '撤回多对多合成，恢复原费用及附件归属'))
        for key, previous in group['queue_before'].items():
            restored = {**previous, 'revision': group['queue_after'][key]['revision'] + 1,
                        'result': '已撤回合成，恢复原费用关联' if previous.get('item_id') else '已撤回合成，等待重新匹配'}
            if not previous.get('item_id'):
                restored.update(status='review', item_id='')
            self._write_queue(db, restored)
        stored = _data(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
        stored.pop('material_match', None)
        stored.pop('review_requested', None)
        stored.update(stage='cancelled', verified=False, updated_at=now(), undone_material_group=group['id'])
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
        group.update(status='undone', undone_at=now())
        db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group['id']))
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                   (item_id, now(), '撤回多对多合成；合成费用取消，原费用及所有原件保留'))

    def _undo_registration(self, db: sqlite3.Connection, group: dict[str, Any], item: dict[str, Any]) -> None:
        if not group['reused_item']:
            self._undo_multiple(db, group, item)
            return
        item_id = item['id']
        for attachment_id in group['attachment_ids']:
            db.execute('DELETE FROM attachments WHERE id=? AND item_id=?', (attachment_id, item_id))
        original = {**group['member_before'][item_id]['data'], 'returned_to_synthesis': True}
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(original, ensure_ascii=False), item_id))
        for key, previous in group['queue_before'].items():
            restored = {**previous, 'revision': group['queue_after'][key]['revision'] + 1,
                        'result': '撤回材料登记，恢复原费用与材料归属'}
            if not previous.get('item_id'):
                restored.update(status='review', item_id='')
            self._write_queue(db, restored)
        group.update(status='undone', undone_at=now())
        db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group['id']))
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                   (item_id, now(), '撤回材料登记；恢复原费用金额、类别及原件归属'))

    def undo(self, group_id: str, expected_version: str) -> dict[str, Any]:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM material_matches WHERE id=?', (group_id,)).fetchone()
            if row is None:
                raise ValueError('匹配记录不存在')
            group = _data(row['data'])
            item_id = group['item_id']
            item = self.store.get(item_id)
            if item['version'] != expected_version:
                raise ValueError('费用版本已变化，请刷新后重试')
            if (group['status'] != 'provisional' or not group['provisional'] or
                    item.get('deleted') or item['stage'] != 'draft' or item['verified']):
                raise ValueError('已确认、核对或提交的匹配不能撤回，请先明确退回')
            if item.get('material_match', {}).get('group_id') != group_id:
                raise ValueError('费用的匹配关系已变化')
            self._assert_group_snapshot(db, group)
            if group.get('schema_version') == 3:
                self._undo_registration(db, group, item)
            elif group.get('schema_version') == 2:
                self._undo_multiple(db, group, item)
            else:
                self._undo_single(db, group, item_id)
        return {'item': self.store.get(item_id), 'match': self._public_match(group)}

    def _undo_single(self, db: sqlite3.Connection, group: dict[str, Any], item_id: str) -> None:
        released_ids = [*group['payment_ids'], *group.get('support_ids', [])]
        for key in released_ids:
            current = self._queue(db, key)
            if (current.get('item_id') != item_id or current.get('material_match_id') != group['id'] or
                    current.get('status') in {'archived', 'deleted', 'unused'}):
                raise ValueError('付款材料的关联已变化')
        for key in [*group['payment_attachment_ids'], *group.get('support_attachment_ids', [])]:
            db.execute('DELETE FROM attachments WHERE id=? AND item_id=?', (key, item_id))
        for key in released_ids:
            current = self._queue(db, key)
            restored = {**group['queue_before'][key], 'status': 'review', 'item_id': '',
                        'revision': current['revision'] + 1, 'result': '已撤回付款关联，等待重新匹配'}
            restored.pop('material_match_id', None)
            self._write_queue(db, restored)
        stored = _data(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
        stored.pop('material_match', None)
        stored.pop('review_requested', None)
        if group.get('bill_previous') is not None:
            stored['material_basis'] = group['bill_previous'].get('material_basis', 'standard')
        if isinstance(group.get('transaction_previous'), dict):
            stored.update(group['transaction_previous'])
        stored.update(verified=False, returned_to_synthesis=True, updated_at=now())
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(stored, ensure_ascii=False), item_id))
        group.update(status='undone', undone_at=now())
        db.execute('UPDATE material_matches SET data=? WHERE id=?', (json.dumps(group, ensure_ascii=False), group['id']))
        db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                   (item_id, now(), '撤回材料匹配；保留票据费用和所有原件，付款等待重新匹配'))

    def _rules_material_state(self, card: dict[str, Any], project_id: str, *,
                              db: sqlite3.Connection | None = None, amount_only: bool = False,
                              allow_name_review: bool = False) -> dict[str, Any]:
        """Validate source evidence without requiring optional AI classification."""
        with nullcontext(db) if db is not None else self.store.connect() as connection:
            if card['key'].startswith('q:'):
                entry = self._queue(connection, card['id'])
                self._check_entry(connection, entry, project_id, {card['id']: card['revision']}, {card['role']})
                if entry.get('project_id') != project_id and not (amount_only and not entry.get('project_id')):
                    raise ValueError('材料归属未明确，等待人工选择项目')
                issues = [str(issue) for issue in [*_name_conflict_projection(entry)[0], *card.get('issues', [])]]
                if card['role'] == 'payment' and any('付款状态待确认' in issue for issue in issues):
                    raise ValueError('付款成功状态仍待核对，等待人工处理')
                name_conflicts = (MERCHANT_CONFLICT, '商家冲突', '收款身份待核对', '销售方名称待核对',
                                  'AI与原件字段冲突：merchant', 'AI与本地识别的商家名称不同',
                                  'AI与本地识别的实际店铺不同')
                name_conflict = any(any(core in issue for core in name_conflicts) for issue in issues)
                core_conflicts: tuple[str, ...] = ('金额冲突', '多个付款金额', '金额待确认', MERCHANT_CONFLICT,
                                  '商家冲突', '收款身份待核对', '销售方名称待核对',
                                  '发票号码冲突', 'AI与原件字段冲突：invoice_number',
                                  'AI与原件字段冲突：amount', 'AI与原件字段冲突：merchant',
                                  'AI与原件字段冲突：payment_status', 'AI与原件用途冲突', '材料用途待确认',
                                  'AI与本地识别的金额不同', 'AI与本地识别的发票号码不同',
                                  'AI与本地识别的付款状态不同', 'AI与本地识别的商家名称不同',
                                  'AI与本地识别的实际店铺不同')
                if amount_only or allow_name_review:
                    core_conflicts = tuple(issue for issue in core_conflicts if issue not in name_conflicts)
                found = next((issue for issue in issues if any(core in issue for core in core_conflicts)), '')
                if found:
                    raise ValueError('金额或商家核心信息待核对：' + _readable_field_issue(found))
                text = _decision_text(entry.get('text'))
                manual = set(entry.get('manual_fields') or [])
                sources = entry.get('field_sources') or {}
                if card['role'] == 'payment':
                    status = (entry.get('fields') or {}).get('payment_status', '')
                    compact = _payment_status_text(str(entry.get('text') or ''))
                    failed = any(word in compact for word in ('退款', '退回', '已撤销', '交易关闭', '交易失败',
                                 '支付失败', '付款失败', '已取消', '等待对方收款', '待对方收款', '等待付款', '未支付', '未付款',
                                 '待支付', '待付款', '转账中'))
                    succeeded = any(word in compact for word in ('支付成功', '付款成功', '交易成功',
                                      '对方已收钱', '已收钱', '转账成功', '支付完成'))
                    if failed or status not in {'', 'succeeded'} or not (status == 'succeeded' or succeeded):
                        raise ValueError('付款成功状态未明确或含退款信息，等待人工处理')
                if amount_only:
                    return {'id': card['key'], 'role': card['role'], 'amount_cents': card['amount_cents']}
                observed = str(card['fields'].get('merchant') or '')
                trusted = ('merchant' in manual or sources.get('merchant') == 'manual' or
                           bool(observed and merchant_identity(observed) in merchant_identity(text)))
                if not trusted and observed and card['role'] == 'invoice':
                    native = self._native_invoice_seller(entry)
                    trusted = bool(native and (re.sub(r'\s+', '', native) == re.sub(r'\s+', '', observed) or
                                   _same_company(_company_identity(native, card['amount_cents']),
                                                 _company_identity(observed, card['amount_cents']))))
                order = str(card['fields'].get('order_number') or '')
                order_trusted = ('order_number' in manual or sources.get('order_number') == 'manual' or
                                 order in _label_values(text, ('订单号', '订单编号')))
                transaction_record = entry
            else:
                item = self.store.get(card['id'])
                if item['version'] != card['version']:
                    raise ValueError('成员费用版本已变化，请刷新后重新选择')
                if item.get('project_id') != project_id or self._expense_card(item) is None:
                    raise ValueError('费用已关联、已核对或不属于当前项目')
                self._check_member_originals(self._member_state(connection, item['id']))
                text = ''
                observed = str(item.get('merchant') or '')
                trusted = True
                order = str(item.get('order_number') or '')
                order_trusted = True
                name_conflict = any('商家' in str(issue) and '冲突' in str(issue) for issue in card.get('issues', []))
                transaction_record = item
        description_labels: tuple[str, ...] = ('商品说明', '商品名称', '商品', '酒店名称', '入住酒店', '住宿酒店', '购买内容')
        if card['role'] == 'payment':
            description_labels += ('付款备注', '交易备注', '交易说明', '备注')
        descriptions = _label_values(text, description_labels)
        transaction_fields = _transaction_fields(transaction_record)
        manual_merchant = ('merchant' in (transaction_record.get('manual_fields') or []) or
                           (transaction_record.get('field_sources') or {}).get('merchant') == 'manual')
        if manual_merchant and not transaction_fields['shop_name']:
            # Keep the original description on the material, but do not use an
            # obsolete shop description to overrule an explicit correction.
            descriptions = []
        return {'id': card['key'], 'role': card['role'], 'amount_cents': card['amount_cents'],
                **transaction_fields,
                'transaction_identifiers': identifiers_for_record(transaction_record),
                'identity': _specific_merchant(observed, card['amount_cents']) if trusted else '',
                'descriptions': descriptions, 'order_number': order if order_trusted else '',
                'name_conflict': name_conflict}

    def _rules_invalid_payment(self, card: dict[str, Any]) -> bool:
        """Only established ineligible payments may leave the competing pool."""
        try:
            with self.store.connect() as db:
                entry = self._queue(db, card['id'])
                self._check_entry(db, entry, card['project_id'], {card['id']: card['revision']}, {'payment'})
            status = (entry.get('fields') or {}).get('payment_status', '')
            text = re.sub(r'\s+', '', str(entry.get('text') or ''))
            return status in {'failed', 'refunded', 'unpaid', 'cancelled', 'canceled'} or any(
                word in text for word in ('已退款', '退款成功', '已全额退款', '全额退回', '已退回', '已撤销',
                                         '交易关闭', '交易失败', '支付失败', '付款失败', '已取消', '等待对方收款',
                                         '待对方收款', '等待付款', '未支付', '未付款', '待支付', '待付款', '转账中'))
        except ValueError:
            return False

    @staticmethod
    def _rules_pool_snapshot(board: dict[str, Any], project_id: str) -> dict[str, Any]:
        return {card['key']: card['version'] if card['key'].startswith('e:') else card['revision']
                for card in [*board['vouchers'], *board['payments']]
                if card['project_id'] == project_id and card['amount_cents'] and
                not card.get('payment_not_required') and
                (card['key'].startswith('e:') or card['status'] in {'received', 'review'})}

    def _rules_current_pool_snapshot(self, db: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        attached = {row['digest'] for row in db.execute('SELECT digest FROM attachments')}
        result: dict[str, Any] = {}
        for row in db.execute('SELECT data FROM intake_queue'):
            entry = _data(row['data'])
            if (entry.get('project_id') == project_id and entry.get('status') in {'received', 'review'} and
                    not entry.get('item_id') and entry.get('qr_status') not in {'manual', 'converted'} and
                    entry['digest'] not in attached and _material_role(entry) in {'invoice', 'receipt', 'payment'} and
                    _identified_amount(entry) and not rail_payment_not_required(entry) and not _rail_invoice(entry)):
                result['q:' + entry['id']] = entry['revision']
        for item in self.store.list_items():
            if item.get('project_id') == project_id and (not item['complete'] or item.get('returned_to_synthesis')):
                card = self._expense_card(item)
                if card and not card.get('payment_not_required'):
                    result[card['key']] = card['version']
        return result

    def _suggestions_from_board(self, board: dict[str, Any], project_id: str) -> dict[str, Any]:
        """Find balanced choices without linking materials or creating expenses.

        Every amount competitor participates in ambiguity assessment, including
        unknown or different merchant names. Source failures never become
        recommended members or disappear to make another choice look unique.
        """
        vouchers = [card for card in board['vouchers'] if card['project_id'] == project_id and card['amount_cents'] and
                    not card.get('payment_not_required') and
                    (card['key'].startswith('e:') or card['status'] in {'received', 'review'})]
        payments = [card for card in board['payments'] if card['project_id'] == project_id and card['amount_cents'] and
                    card['status'] in {'received', 'review'}]
        conflicts: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            'matched': 0, 'suggested': 0, 'unmatched': len(vouchers), 'items': [], 'suggestions': [],
            'suggestion_count': 0, 'suggestions_truncated': False, 'conflicts': conflicts,
            'engine': 'rules', 'requests': 0,
            'ai': {'requests': 0, 'reviewed': 0, 'failed': 0, 'used_for_matching': False,
                   'message': 'GLM不参与配对；候选由本地规则核算，确认创建后进入待核对。'},
            'issues': ['系统候选进入待合成；选择材料并确认创建后进入待核对。',
                       '每组最多4份票据和4份付款；更大组合请在合成台人工选择。'],
        }
        if len(vouchers) > 20 or len(payments) > 32:
            conflicts.append({'reason': '材料池超过20份票据或32份付款，请分批人工匹配；未截断后猜测唯一'})
            return result
        eligible_payments = []
        for card in payments:
            if self._rules_invalid_payment(card):
                conflicts.append({'payment_ids': [card['id']], 'reason': '付款含退款或非成功信息，不推荐合成'})
            else:
                eligible_payments.append(card)
        states: dict[str, dict[str, Any]] = {}
        blocked: dict[str, str] = {}
        for card in [*vouchers, *eligible_payments]:
            try:
                states[card['key']] = self._rules_material_state(card, project_id, allow_name_review=True)
            except ValueError as exc:
                blocked[card['key']] = str(exc)
                conflicts.append({'voucher_keys' if card in vouchers else 'payment_ids':
                                  [card['key'] if card in vouchers else card['id']], 'reason': str(exc)})
        # Read owners once. Duplicate materials remain in the full competing
        # pool, but a spent transaction is never offered as another expense.
        owners: dict[str, set[str]] = {}
        with self.store.connect() as db:
            for row in db.execute('SELECT id,source,data FROM expenses'):
                for key in expense_identifier_keys(db, row['id'], _data(row['data']), source=row['source']):
                    owners.setdefault(key, set()).add(row['id'])
        for card in [*vouchers, *eligible_payments]:
            owned_elsewhere = set().union(*(owners.get(key, set()) for key in
                                          record_identifier_keys(states.get(card['key'], {}))))
            if card['key'].startswith('e:'):
                owned_elsewhere.discard(card['id'])
            if owned_elsewhere:
                reason = '同类型、同平台的交易编号已登记费用，请查看原费用或补充材料'
                blocked[card['key']] = reason
                conflicts.append({'voucher_keys' if card in vouchers else 'payment_ids':
                                  [card['key'] if card in vouchers else card['id']], 'reason': reason})
        candidates, _, pair_count = _balanced_candidates(vouchers, eligible_payments)
        if pair_count > 50000:
            conflicts.append({'reason': '候选组合超过50000组，请分批人工匹配；未截断后猜测唯一'})
            return result
        voucher_cards = {card['key']: card for card in vouchers}
        payment_cards = {card['id']: card for card in eligible_payments}
        pool = self._rules_pool_snapshot(board, project_id)
        ranked: list[tuple[tuple[int, int], str, dict[str, Any]]] = []
        for component in _candidate_components([candidate for groups in candidates.values() for candidate in groups]):
            competing_keys = {key for candidate in component for key in [*candidate[0], *('q:' + p for p in candidate[1])]}
            blocked_competitors = sorted(competing_keys.intersection(blocked))
            for voucher_group, payment_group in component:
                keys = [*voucher_group, *('q:' + key for key in payment_group)]
                candidate_cards = [*(voucher_cards[key] for key in voucher_group), *(payment_cards[key] for key in payment_group)]
                if not people_compatible(candidate_cards):
                    conflicts.append({'voucher_keys': list(voucher_group), 'payment_ids': list(payment_group),
                                      'reason': '费用归属人尚未全部指定，请手工确认归属后再合成'})
                    continue
                if any(key in blocked for key in keys):
                    continue
                if _repeated_counted_identity([states[key] for key in keys]):
                    conflicts.append({'voucher_keys': list(voucher_group), 'payment_ids': list(payment_group),
                                      'reason': '所选组合重复包含同一交易编号，不能将同笔付款或购买重复计费'})
                    continue
                relation = [_rules_relation(states[v], states['q:' + p]) for v in voucher_group for p in payment_group]
                name_conflict = any(states[key]['name_conflict'] for key in keys)
                name_review = name_conflict or not all(value == 'yes' for value in relation)
                context = matching_context([states[key] for key in voucher_group],
                                           [states['q:' + key] for key in payment_group], relation)
                warnings = []
                if name_review:
                    warnings.append('商家名称识别不一致，请对照原件确认' if name_conflict else
                                    '商家名称或商品对应关系尚未明确，请核对是否属于同一费用')
                if context['platform'] == 'different':
                    warnings.append('购物平台不同，请对照店铺、商品说明与原件确认')
                if len(component) > 1:
                    warnings.append(f'这些材料存在{len(component)}种金额相符组合，请人工选择；不是唯一配对')
                if blocked_competitors:
                    warnings.append('竞争材料含核心信息待核对的材料，不能排除后认定唯一')
                identity = {'project_id': project_id, 'voucher_keys': sorted(voucher_group),
                            'payment_ids': sorted(payment_group)}
                candidate_id = 's:' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
                snapshot = {'candidate': identity, 'pool': pool,
                            'evidence': {key: states[key] for key in sorted(keys)},
                            'cards': [voucher_cards[key] for key in sorted(voucher_group)] +
                                     [payment_cards[key] for key in sorted(payment_group)]}
                token = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                cards = [*(voucher_cards[key] for key in voucher_group), *(payment_cards[key] for key in payment_group)]
                evidence = [{'key': card['key'], 'role': card['role'], 'amount_cents': card['amount_cents'],
                             'merchant': states[card['key']]['identity'],
                             **{field: states[card['key']][field] for field in TRANSACTION_FIELDS},
                             'transaction_identifiers': states[card['key']]['transaction_identifiers'],
                             'descriptions': states[card['key']]['descriptions']} for card in cards]
                revisions = {card['id']: card['revision'] for card in cards if card['key'].startswith('q:')}
                versions = {card['id']: card['version'] for card in cards if card['key'].startswith('e:')}
                result['suggestion_count'] += 1
                # Rank supporting evidence, while every competing combination
                # above still participates in ambiguity and confirmation checks.
                suggestion = {
                        **common_person(cards),
                        **identity, 'id': candidate_id, 'status': 'suggested', 'snapshot_token': token,
                        'amount_cents': sum(voucher_cards[key]['amount_cents'] for key in voucher_group),
                        'title': voucher_cards[voucher_group[0]]['fields'].get('title') or voucher_cards[voucher_group[0]]['name'],
                        'voucher_count': len(voucher_group), 'payment_count': len(payment_group),
                        'reason': '；'.join(context['checks'][:3]), 'matching_context': context,
                        'warnings': warnings, 'evidence': evidence, 'needs_name_review': name_review,
                        'ambiguous': len(component) > 1, 'competing_count': len(component) - 1,
                        'revisions': revisions, 'expected_versions': versions,
                    }
                support = int(context['shop'] == 'matched') * 4 + int(context['platform'] == 'matched') * 2
                rank = (-support, int(name_review))
                insort(ranked, (rank, candidate_id, suggestion))
                if len(ranked) > 100:
                    ranked.pop()
        result['suggestions'] = [suggestion for _, _, suggestion in ranked]
        # A concurrent intake/edit must not yield an apparently current choice.
        with self.store.connect() as db:
            if self._rules_current_pool_snapshot(db, project_id) != pool:
                conflicts.append({'reason': '材料池扫描期间已变化，请刷新待合成候选'})
                result.update(suggestions=[], suggestion_count=0)
        result['suggested'] = result['suggestion_count']
        result['suggestions_truncated'] = result['suggestion_count'] > len(result['suggestions'])
        if result['suggestions_truncated']:
            conflicts.append({'reason': f'共有{result["suggestion_count"]}个候选，仅展示前100个；竞争判断包含全部组合，请分批人工选择'})
        return result

    def suggest(self, project_id: str) -> dict[str, Any]:
        """Public read-only scan, including ambiguous names and competing sums."""
        self._project(project_id)
        board = self.board(project_id)
        return {'matched': 0, 'suggested': board['suggestion_count'], 'items': [],
                'unmatched': len(board['vouchers']), 'suggestions': board['suggestions'],
                'suggestion_count': board['suggestion_count'], 'suggestions_truncated': board['suggestions_truncated'],
                'conflicts': board['suggestion_conflicts'], 'engine': 'rules', 'requests': 0,
                'ai': {'requests': 0, 'used_for_matching': False,
                       'message': 'GLM不参与配对；确认创建后进入待核对。'},
                'issues': ['系统候选进入待合成；选择材料并确认创建后进入待核对。',
                           '每组最多4份票据和4份付款；更大组合请在合成台人工选择。']}

    def auto_match(self, project_id: str) -> dict[str, Any]:
        """Legacy internal mutation API; production HTTP uses suggest instead."""
        self._project(project_id)
        board = self.board(project_id)
        expected_pool = self._rules_pool_snapshot(board, project_id)
        payments = [card for card in board['payments'] if card['project_id'] == project_id
                    and card['status'] in {'received', 'review'} and card['amount_cents']]
        vouchers = [card for card in board['vouchers'] if card['project_id'] == project_id and card['amount_cents'] and
                    not card.get('payment_not_required') and
                    (card['key'].startswith('e:') or card['status'] in {'received', 'review'})]
        conflicts: list[dict[str, Any]] = []
        try:
            status = self.ai.public_status() if self.ai is not None else {'enabled': False, 'available': False}
        except (ValueError, OSError, RuntimeError):
            status = {'enabled': self.ai is not None, 'available': False}
        ai_status = {**status, 'requests': 0, 'reviewed': 0, 'failed': 0, 'used_for_matching': False,
                     'message': 'GLM不参与配对；金额及对应关系由本地规则判断。'}
        result: dict[str, Any] = {'matched': 0, 'unmatched': len(vouchers), 'conflicts': conflicts, 'items': [],
                                 'engine': 'rules', 'requests': 0, 'ai': ai_status,
                                 'issues': ['本地规则核算金额与原件对应关系；明确组合进入待核对，有歧义保留人工。',
                                            '每组最多4份票据和4份付款；更大组合请人工选择。']}
        if len(payments) > 32 or len(vouchers) > 20:
            conflicts.append({'reason': '材料池超过20份票据或32份付款，请分批人工匹配；未截断后自动猜测'})
            return result
        eligible_payments = []
        states: dict[str, dict[str, Any]] = {}
        blocked: dict[str, str] = {}
        for card in payments:
            if self._rules_invalid_payment(card):
                conflicts.append({'payment_ids': [card['id']], 'reason': '付款成功状态未明确或含退款信息，等待人工处理'})
            else:
                eligible_payments.append(card)
        for card in [*vouchers, *eligible_payments]:
            try:
                states[card['key']] = self._rules_material_state(card, project_id)
            except ValueError as exc:
                blocked[card['key']] = str(exc)
                conflicts.append({'voucher_keys' if card in vouchers else 'payment_ids':
                                  [card['key'] if card in vouchers else card['id']], 'reason': str(exc)})
        candidates, _, pair_count = _balanced_candidates(vouchers, eligible_payments)
        if pair_count > 50000:
            conflicts.append({'reason': '候选组合超过50000组，请分批人工匹配；未截断后自动猜测'})
            return result
        voucher_cards = {card['key']: card for card in vouchers}
        payment_cards = {card['id']: card for card in eligible_payments}
        remaining = [candidate for groups in candidates.values() for candidate in groups]
        while remaining:
            next_candidates: list[Candidate] = []
            for component in _candidate_components(remaining):
                voucher_keys = sorted({key for candidate in component for key in candidate[0]})
                payment_keys = sorted({key for candidate in component for key in candidate[1]})
                component_cards = [*(voucher_cards[key] for key in voucher_keys),
                                   *(payment_cards[key] for key in payment_keys)]

                def conflict(reason: str) -> None:
                    conflicts.append({'voucher_keys': voucher_keys, 'payment_ids': payment_keys, 'reason': reason})

                blocked_keys = set(voucher_keys).union('q:' + key for key in payment_keys).intersection(blocked)
                if blocked_keys:
                    conflict('竞争组合含核心信息待核对的材料，未排除后猜测唯一；' +
                             '；'.join(dict.fromkeys(blocked[key] for key in sorted(blocked_keys))))
                    continue
                try:
                    for card in component_cards:
                        states[card['key']] = self._rules_material_state(card, project_id)
                except ValueError as exc:
                    conflict(str(exc))
                    continue
                supported: list[Candidate] = []
                plausible: list[Candidate] = []
                for candidate in component:
                    selected_cards = [*(voucher_cards[key] for key in candidate[0]), *(payment_cards[key] for key in candidate[1])]
                    if not people_compatible(selected_cards):
                        plausible.append(candidate)
                        continue
                    relations = [_rules_relation(states[v], states['q:' + p])
                                 for v in candidate[0] for p in candidate[1]]
                    if all(relation == 'yes' for relation in relations):
                        supported.append(candidate)
                        plausible.append(candidate)
                    elif any(relation == 'unknown' for relation in relations):
                        plausible.append(candidate)
                uses: Counter[str] = Counter()
                for voucher_group, payment_group in plausible:
                    uses.update([*voucher_group, *('q:' + key for key in payment_group)])
                selected = next((candidate for candidate in supported if
                                 all(uses[key] == 1 for key in (*candidate[0], *('q:' + key for key in candidate[1])))), None)
                if selected is None:
                    if supported:
                        conflict('同商家或未知关系存在多个票据付款组合，等待人工选择')
                    else:
                        conflict('金额相同但商家或原文商品对应关系未明确，等待人工选择')
                    continue
                selected_vouchers, selected_payments = selected
                revisions = {key: payment_cards[key]['revision'] for key in selected_payments}
                versions: dict[str, str] = {}
                for key in selected_vouchers:
                    card = voucher_cards[key]
                    if key.startswith('q:'):
                        revisions[card['id']] = card['revision']
                    else:
                        versions[card['id']] = card['version']
                fields: dict[str, Any] = {}
                if len(selected_vouchers) == 1 and selected_vouchers[0].startswith('q:'):
                    name = voucher_cards[selected_vouchers[0]]['fields'].get('merchant')
                    if name:
                        fields['merchant'] = name
                try:
                    for card in component_cards:
                        self._rules_material_state(card, project_id)
                    matched = self.match(list(selected_vouchers), list(selected_payments), project_id,
                                         expected_versions=versions, revisions=revisions, fields=fields,
                                         provisional=True, reason='本地规则根据商家或原文商品及金额暂配，仍待人工核对',
                                         expected_materials=component_cards, expected_pool=expected_pool)
                except ValueError as exc:
                    conflict(str(exc))
                    continue
                result['matched'] += 1
                result['unmatched'] -= len(selected_vouchers)
                result['items'].append(matched)
                for key in [*selected_vouchers, *('q:' + key for key in selected_payments)]:
                    expected_pool.pop(key, None)
                next_candidates.extend(candidate for candidate in component
                                       if not set(candidate[0]).intersection(selected_vouchers)
                                       and not set(candidate[1]).intersection(selected_payments))
            remaining = next_candidates
        return result
