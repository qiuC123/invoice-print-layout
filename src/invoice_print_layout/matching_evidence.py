"""Shop identity and platform/amount matching context."""
from __future__ import annotations

from datetime import datetime
import re
import unicodedata
from typing import Any


def merchant_identity(value: str) -> str:
    """Normalize typography and a flash-shopping badge, retaining branch names."""
    name = re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).strip(':：·')
    return re.sub(r'^闪购[:：·]?', '', name)


_DATE_TIME = re.compile(
    r'(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?[ T]?'
    r'(\d{1,2})[:时](\d{2})(?:[:分](\d{2})(?:\.(\d{1,6}))?秒?)?'
    r'(?![\d.:])')


def labelled_transaction_time(text: str, role: str) -> dict[str, Any]:
    """Read full local date/time only beside a transaction label, never a clock."""
    priorities = ({'支付时间': 0, '付款时间': 0, '扣款时间': 0, '交易时间': 1} if role == 'payment' else
                  {'下单时间': 0, '订单创建时间': 0, '消费时间': 1,
                   '支付时间': 2, '付款时间': 2, '交易时间': 2})
    rows = [unicodedata.normalize('NFKC', row).strip() for row in text.splitlines() if row.strip()]
    found: dict[int, dict[str, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        for label, priority in priorities.items():
            match = re.match(r'^' + re.escape(label) + r'\s*[:：]?\s*(.*)$', row)
            if not match:
                continue
            value = match[1] or (rows[index + 1] if index + 1 < len(rows) else '')
            compact = re.sub(r'\s+', '', value)
            for observed in _DATE_TIME.finditer(compact):
                # Do not silently discard an explicit timezone and compare it
                # with a local screenshot time from an unknown timezone.
                if re.match(r'(?:Z|UTC|GMT|[+-]\d{2}:?\d{2})', compact[observed.end():], re.I):
                    continue
                year, month, day, hour, minute, second, fraction = observed.groups()
                try:
                    moment = datetime(int(year), int(month), int(day), int(hour), int(minute),
                                      int(second or 0), int((fraction or '').ljust(6, '0') or 0))
                except ValueError:
                    continue
                precision = 'fraction' if fraction else 'second' if second else 'minute'
                stamp = moment.isoformat(timespec='microseconds' if fraction else 'seconds')
                found.setdefault(priority, {})[stamp] = {
                    'value': stamp, 'label': label, 'quote': row + ('\n' + value if not match[1] else ''),
                    'precision': precision, 'ambiguous': False,
                }
            break
    if not found:
        return {'value': '', 'label': '', 'quote': '', 'precision': '', 'ambiguous': False}
    best = found[min(found)]
    if len(best) != 1:
        return {'value': '', 'label': '', 'quote': '', 'precision': '', 'ambiguous': True}
    return next(iter(best.values()))


def matching_context(vouchers: list[dict[str, Any]], payments: list[dict[str, Any]],
                     relations: list[str]) -> dict[str, Any]:
    """Describe supporting evidence without discarding competing combinations."""
    shop = 'matched' if relations and all(value == 'yes' for value in relations) else (
        'different' if any(value == 'no' for value in relations) else 'unknown')
    left = {str(record.get('order_number') or '') for record in vouchers} - {''}
    right = {str(record.get('order_number') or '') for record in payments} - {''}
    numbers = 'missing' if not left or not right else 'same' if left == right else 'different'
    left_platforms = {record.get('shopping_platform') for record in vouchers if record.get('shopping_platform')}
    right_platforms = {record.get('shopping_platform') for record in payments if record.get('shopping_platform')}
    platform = ('unknown' if not left_platforms or not right_platforms else
                'matched' if len(left_platforms) == len(right_platforms) == 1 and left_platforms == right_platforms else 'different')
    checks = ['金额合计一致']
    checks.append({'matched': '店铺或原文商品说明对应', 'different': '店铺名称不同，需对照商品和原件确认',
                   'unknown': '店铺对应关系需人工确认'}[shop])
    if platform != 'unknown':
        checks.append('购物平台一致：' + str(next(iter(left_platforms))) if platform == 'matched' else
                      '购物平台不同，请结合实际店铺与原件确认')
    else:
        checks.append('结合店铺与金额核对')
    checks.append('订单、支付及商家编号分别用于区分和防重，不要求不同体系的编号相同')
    return {'platform': platform, 'shop': shop, 'order_numbers': numbers, 'checks': checks}
