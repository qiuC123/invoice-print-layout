"""Source-bound payment fields from text that has already been read locally.

This module performs no OCR, model call, or mutation.  A payee is an identity,
not evidence of the purchased product or of an installation/repair service.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import TypedDict
import unicodedata


class LocalPaymentFields(TypedDict):
    role: str
    fields: dict[str, str]
    observed_fields: dict[str, str]
    evidence: dict[str, str]
    issues: list[str]
    conflicts: dict[str, dict[str, str]]


class _Row(TypedDict):
    raw: str
    text: str
    start: int
    end: int


_PAYEE_LABELS = ('收款方全称', '收款商户全称', '收款方名称', '商户全称', '商户名称',
                 '商家全称', '商家名称', '收款商户', '收款人', '收款方', '收款单位')
_FIELD_START = re.compile(r'^(?:收款|付款|交易|转账|支付|金额|币种|银行|开户|账号|账户|'
                          r'商户|商家|订单|附言|备注|用途|状态|当前状态|电子回单|开票人)')
_PURE_LABELS = frozenset((*_PAYEE_LABELS, '二维码收款', '二维码付款', '收款二维码',
                         '微信转账', '微信支付', '支付宝', '付款详情', '账单详情', '交易详情',
                         '收款方备注', '收款方服务', '收款方名片', '回收款方名片',
                         '收款人签字', '收款人签章', '收款方盖章', '收款银行', '付款银行',
                         '收款账户', '付款账户', '收款账号', '付款账号', '付款人', '付款方',
                         '客户', '未知商家'))
_AMOUNT_LABEL = re.compile(r'^(?:转账金额|付款金额|支付金额|交易金额|汇款金额|发生金额|'
                           r'实付金额|实付款|金额(?:\(小写\))?|小写金额|金额小写|'
                           r'人民币\(小写\)|小写)(?:\(元\))?[:：]?')
_NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?'
_STATE_LABEL = re.compile(r'^(?:当前状态|交易状态|支付状态|转账状态|付款状态|交易结果|'
                          r'转账结果|处理状态|退款状态|状态)[:：]?')
_SUCCESS = frozenset(('支付成功', '付款成功', '交易成功', '对方已收钱', '转账成功',
                      '支付完成', '转账完成', '交易完成', '已入账', '成功'))
_NEGATIVE: dict[str, frozenset[str]] = {
    'failed': frozenset(('交易失败', '支付失败', '付款失败', '转账失败', '已撤销', '已取消',
                         '交易关闭', '转账已撤销', '失败')),
    'refunded': frozenset(('退款成功', '已退款', '已退回', '对方已退还', '转账已退还')),
    'unpaid': frozenset(('等待付款', '未付款', '未支付', '待支付', '待付款', '尚未付款')),
    'uncertain': frozenset(('处理中', '转账中', '付款处理中', '转账处理中', '交易处理中',
                            '等待对方收款', '待对方收款', '待收款', '待处理', '受理成功',
                            '已受理', '已提交', '转账申请已提交', '已发起转账', '转账已发起',
                            '转账发起成功', '退款处理中', '退款申请中', '已申请退款')),
}
_FINANCIAL_DIGITS = dict(zip('零壹贰叁肆伍陆柒捌玖', range(10)))


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).replace('￥', '¥').replace('−', '-')


def is_label_only_payee(value: str) -> bool:
    """Reject a form/control label, never infer an identity from it."""
    compact = _compact(value).strip(':：·-')
    return (not compact or compact in _PURE_LABELS or len(compact) > 160 or
            not re.search(r'[A-Za-z\u4e00-\u9fff]', compact) or
            bool(re.search(r'盖章|签字|开票人|收款人[:：]|收款方[:：]|待核对|待确认', compact)) or
            bool(re.match(r'^(?:备注|服务|名片|账号|账户|银行|名称|全称|专用章|\(盖章\))[:：]?', compact)))


def _rows(text: str) -> list[_Row]:
    output: list[_Row] = []
    start = 0
    for line in text.splitlines(keepends=True):
        raw = line.rstrip('\r\n')
        if raw.strip():
            output.append({'raw': raw, 'text': _compact(raw), 'start': start, 'end': start + len(raw)})
        start += len(line)
    return output


def _quote(text: str, rows: list[_Row], first: int, last: int) -> str:
    return text[rows[first]['start']:rows[last]['end']]


def _payee_value(value: str, *, title: bool = False) -> str:
    value = _compact(value).strip(':：·-')
    if title:
        value = re.sub(r'的(?:转账|付款|二维码付款)$', '', value)
        # A transfer nickname can contain a phone number or billing reminder.
        # Preserve the quoted title but keep the explicit company/name itself.
        value = re.sub(r'1[3-9]\d{9}$', '', value)
        value = re.sub(r'(?<=(?:有限公司))(?:开票|备注|附言).+$', '', value)
    if is_label_only_payee(value) or re.search(r'[¥￥]|(?:开票|合计|总计)\d|[:：]', value):
        return ''
    return value


def is_payment_payee_metadata(row: str) -> bool:
    """Recognize recipient controls/notes, not an actual recipient identity."""
    compact = _compact(row)
    return (compact.startswith('收款方备注') or
            bool(re.fullmatch(r'(?:回)?收款方(?:服务|名片)[:：]?[›>↗]?', compact)))


def payment_title_payee(row: str) -> str:
    """Return only a recipient explicitly named in a payment/transfer title."""
    compact = _compact(row)
    title = re.fullmatch(r'(?:(?:扫)?二维码(?:付款|支付)|转账|付款)[-—–:：]?(?:转给|给)(.+)', compact)
    if title is None:
        title = re.fullmatch(r'给(.+)的(?:转账|付款|二维码付款)', compact)
    return _payee_value(title[1], title=True) if title is not None else ''


def _payee(text: str, rows: list[_Row]) -> tuple[str, str, bool]:
    candidates: dict[int, dict[str, str]] = {}
    for index, row in enumerate(rows):
        compact = row['text']
        if is_payment_payee_metadata(row['raw']):
            continue
        for label in _PAYEE_LABELS:
            if not compact.startswith(label):
                continue
            value = compact[len(label):].lstrip(':：')
            last = index
            if not value and index + 1 < len(rows) and not _FIELD_START.match(rows[index + 1]['text']):
                last += 1
                value = rows[last]['text']
            if value.endswith(('有限', '有限责任')) and index + 1 < len(rows) and rows[index + 1]['text'] == '公司':
                value += '公司'
                last = index + 1
            value = _payee_value(value)
            if value and not _FIELD_START.match(value):
                priority = 3 if label not in {'收款方', '收款人', '收款单位'} else 2
                candidates.setdefault(priority, {})[value] = _quote(text, rows, index, last)
            break
        title_value = payment_title_payee(row['raw'])
        if title_value:
            candidates.setdefault(1, {})[title_value] = row['raw']
        # An issued bank receipt also prints its payer-to-payee heading.
        heading = re.fullmatch(r'[^:：]{1,80}向(.+?)(?:转账|汇款)', compact)
        if heading is not None:
            value = _payee_value(heading[1])
            if value:
                candidates.setdefault(1, {})[value] = row['raw']
    if not candidates:
        return '', '', False
    best = candidates[max(candidates)]
    if len(best) != 1:
        return '', '', True
    value, quote = next(iter(best.items()))
    # An explicit full company and a truncated bank heading may agree. An
    # unrelated explicit title is not silently ignored at lower priority.
    for choices in candidates.values():
        if any(other != value and not value.startswith(other) and not other.startswith(value)
               for other in choices):
            return '', '', True
    return value, quote, False


def _cents(value: str) -> int | None:
    try:
        amount = Decimal(value.replace(',', '').lstrip('-'))
        if amount <= 0 or amount > Decimal('999999999') or amount != amount.quantize(Decimal('.01')):
            return None
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        return None


def _financial_cents(value: str) -> int | None:
    match = re.fullmatch(r'(?:人民币)?([零壹贰叁肆伍陆柒捌玖拾佰仟万亿]+)[元圆](整|[零壹贰叁肆伍陆柒捌玖角分]+)', value)
    if match is None:
        return None
    total = section = 0
    number: int | None = None
    last_small, last_big = 10000, 1000000000000
    for char in match[1]:
        if char in _FINANCIAL_DIGITS:
            if number not in {None, 0}:
                return None
            number = _FINANCIAL_DIGITS[char]
        elif char in '拾佰仟':
            unit = {'拾': 10, '佰': 100, '仟': 1000}[char]
            if unit >= last_small or number == 0 or (number is None and char != '拾'):
                return None
            section += (number or 1) * unit
            number, last_small = None, unit
        else:
            unit = 10000 if char == '万' else 100000000
            section += number or 0
            if unit >= last_big or not section:
                return None
            total += section * unit
            section, number, last_small, last_big = 0, None, 10000, unit
    fraction = match[2]
    if fraction == '整':
        pennies = 0
    else:
        decimal_match = re.fullmatch(r'(?:([零壹贰叁肆伍陆柒捌玖])角)?(?:零?([零壹贰叁肆伍陆柒捌玖])分)?', fraction)
        if decimal_match is None or not any(decimal_match.groups()):
            return None
        pennies = _FINANCIAL_DIGITS.get(decimal_match[1] or '', 0) * 10 + _FINANCIAL_DIGITS.get(decimal_match[2] or '', 0)
    cents = (total + section + (number or 0)) * 100 + pennies
    return cents if 0 < cents <= 99999999900 else None


def _amount(rows: list[_Row]) -> tuple[str, str, bool]:
    candidates: dict[int, dict[int, str]] = {}
    uppercase: dict[int, str] = {}
    for row in rows:
        compact = row['text']
        labelled = _AMOUNT_LABEL.match(compact)
        value = compact[labelled.end():] if labelled else compact
        direct = re.fullmatch(r'(?:人民币|CNY)?[¥]?-?(' + _NUMBER + r')(?:元)?', value)
        if direct is not None and (labelled or re.fullmatch(r'[¥]-?' + _NUMBER + r'(?:元)?|-?\d+\.\d{1,2}(?:元)?', value)):
            cents = _cents(direct[1])
            if cents is not None:
                candidates.setdefault(3 if labelled else 2 if value.startswith(('-', '¥')) else 1, {})[cents] = row['raw']
        chinese = re.fullmatch(r'(?:金额\(大写\)|大写金额|金额大写|大写|人民币大写)[:：]?(.+)', compact)
        if chinese:
            cents = _financial_cents(chinese[1])
            if cents is not None:
                uppercase[cents] = row['raw']
    if not candidates:
        if len(uppercase) == 1:
            cents, quote = next(iter(uppercase.items()))
            return f'{cents / 100:.2f}', quote, False
        return '', '', len(uppercase) > 1
    selected = candidates[max(candidates)]
    # Repeated same-value totals are harmless; a second explicit value is not.
    all_values = {value for group in candidates.values() for value in group}
    if len(selected) != 1 or len(all_values) > 1 or len(uppercase) > 1:
        return '', '', True
    cents, quote = next(iter(selected.items()))
    if uppercase and set(uppercase) != {cents}:
        return '', '', True
    return f'{cents / 100:.2f}', quote, False


def _state(rows: list[_Row]) -> tuple[str, str]:
    states: dict[str, str] = {}
    previous_label = False
    retained: list[_Row] = []
    for row in rows:
        compact = row['text']
        match = _STATE_LABEL.match(compact)
        value = compact[match.end():] if match else compact
        # A refund action is not a completed refund or a current status.
        if (not match and not previous_label and
                re.fullmatch(r'(?:申请退款|退款申请|可申请退款)[›>↗]?', value)):
            continue
        retained.append(row)
        if value in _SUCCESS and (value != '成功' or match is not None or previous_label):
            states['succeeded'] = row['raw']
        for status, phrases in _NEGATIVE.items():
            if value in phrases or (match is not None and value in {'退款申请', '申请退款'} and status == 'uncertain'):
                states[status] = row['raw']
        previous_label = bool(match and not value)
    # Success can be historical. Retain any actual refund/failure/pending
    # wording anywhere in the visible status text, including amount/application
    # rows which are not one of the short status values above.
    blocked = next((row['raw'] for row in retained if any(marker in row['text'] for marker in (
        '退款', '退回', '已撤销', '交易关闭', '交易失败', '支付失败', '付款失败', '已取消',
        '等待对方收款', '待对方收款', '等待付款', '未付款', '未支付', '待支付', '待付款',
        '转账中', '处理中', '受理成功', '已受理', '已发起', '转账申请', '转账已发起', '待处理'))), '')
    if blocked and ('succeeded' in states or not states):
        return 'uncertain', blocked
    if len(states) > 1:
        return 'uncertain', next(quote for status, quote in states.items() if status != 'succeeded')
    return next(iter(states.items())) if states else ('uncertain', '')


def _dated_transaction(rows: list[_Row]) -> bool:
    for row in rows:
        if not re.match(r'^(?:交易时间|交易日期|转账时间|记账时间|记账日期)[:：]?', row['text']):
            continue
        match = re.search(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})', row['text'])
        if match:
            try:
                date(*(int(part) for part in match.groups()))
                return True
            except ValueError:
                pass
    return False


def _has_labelled_party(rows: list[_Row], labels: tuple[str, ...]) -> bool:
    for index, row in enumerate(rows):
        match = re.fullmatch(r'(?:' + '|'.join(map(re.escape, labels)) + r')[:：]?(.*)', row['text'])
        if match is None:
            continue
        value = match[1]
        if not value and index + 1 < len(rows):
            value = rows[index + 1]['text']
        if value and not _FIELD_START.match(value) and _payee_value(value):
            return True
    return False


def extract_local_payment(text: str | Sequence[str], *,
                          current_fields: Mapping[str, object] | None = None,
                          manual_fields: Collection[str] = (),
                          state_text: str | None = None) -> LocalPaymentFields:
    """Return exact-quote observations and safe additions to existing fields.

    ``observed_fields`` retains facts even when a manual/existing value blocks
    their application. ``fields`` never overwrites a manual field or a genuine
    non-empty conflict. Callers must retain ``issues`` and ``conflicts``.
    """
    source = text if isinstance(text, str) else '\n'.join(text)
    rows = _rows(source)
    result: LocalPaymentFields = {'role': '', 'fields': {}, 'observed_fields': {},
                                 'evidence': {}, 'issues': [], 'conflicts': {}}
    compact = _compact(source)
    if ('发票号码' in compact and any(marker in compact for marker in ('电子发票', '增值税'))) or not any(
            marker in compact for marker in ('电子回单', '转账', '付款', '支付成功', '对方已收钱', '交易成功', '微信支付', '支付宝')):
        return result
    merchant, merchant_quote, merchant_conflict = _payee(source, rows)
    amount, amount_quote, amount_conflict = _amount(rows)
    status, status_quote = _state(_rows(state_text) if state_text is not None else rows)
    # A complete issued bank receipt is itself execution evidence. A heading,
    # an application/acceptance slip, or an uncompleted transfer is insufficient.
    issued = ('电子回单' in compact and '电子回单专用章' in compact and
              _has_labelled_party(rows, ('付款人', '付款单位')) and
              _has_labelled_party(rows, ('收款人', '收款单位')) and
              any(_AMOUNT_LABEL.match(row['text']) for row in rows) and
              _dated_transaction(rows) and bool(merchant and amount) and
              not any(phrase in compact for phrase in ('回单申请', '回单样例', '回单示例', '仅供参考', '预览回单',
                  '申请转账', '转账申请', '已发起', '转账已发起', '发起转账', '受理', '处理中',
                  '转账中', '待收款', '待付款', '未付款', '失败', '撤销', '退回', '退款', '已取消', '待处理')))
    if status == 'uncertain' and not status_quote and issued:
        status, status_quote = 'succeeded', source
    result['role'] = 'payment' if status == 'succeeded' else 'other'
    observed = result['observed_fields']
    evidence = result['evidence']
    if merchant:
        observed['merchant'], evidence['merchant'] = merchant, merchant_quote
    if amount:
        observed['amount'], evidence['amount'] = amount, amount_quote
    observed['payment_status'] = status
    if status_quote:
        evidence['payment_status'] = status_quote
    for index, row in enumerate(rows):
        match = re.match(r'^(?:转账附言|转账备注|交易附言|付款备注|附言|用途)[:：]?(.*)', row['text'])
        if match is not None:
            raw_match = re.match(r'^\s*(?:转账附言|转账备注|交易附言|付款备注|附言|用途)\s*[:：]?\s*(.*)', row['raw'])
            value, last = (raw_match[1].strip() if raw_match is not None else match[1]), index
            if not value and index + 1 < len(rows) and not _FIELD_START.match(rows[index + 1]['text']):
                last = index + 1
                value = rows[last]['raw'].strip()
            if value:
                observed['note'], evidence['note'] = value, _quote(source, rows, index, last)
                break
    if amount_conflict:
        result['issues'].append('多个付款金额待核对')
    if merchant_conflict:
        result['issues'].append('收款身份待核对')
    if status != 'succeeded':
        result['issues'].append('付款状态待确认')
    current = current_fields or {}
    for field, value in observed.items():
        if field in manual_fields:
            continue
        # An observed number remains inspectable, but is not a billable amount
        # for an uncompleted, failed, or refunded payment.
        if field == 'amount' and (status != 'succeeded' or
                str(current.get('payment_status') or '') not in {'', 'unknown', 'uncertain', 'succeeded'}):
            continue
        previous = str(current.get(field) or '')
        same = _compact(previous) == _compact(value)
        if field == 'amount' and previous:
            same = _cents(previous) == _cents(value)
        replaceable = not previous or same or (field == 'merchant' and is_label_only_payee(previous)) or (
            field == 'payment_status' and previous in {'unknown', 'uncertain'})
        if replaceable:
            result['fields'][field] = value
        else:
            result['conflicts'][field] = {'existing': previous, 'observed': value, 'quote': evidence.get(field, '')}
            if field in {'amount', 'merchant', 'payment_status'}:
                issue = {'amount': '金额冲突', 'merchant': '收款身份待核对', 'payment_status': '付款状态待确认'}[field]
                if issue not in result['issues']:
                    result['issues'].append(issue)
    assert all(quote and quote in source for quote in evidence.values())
    return result
