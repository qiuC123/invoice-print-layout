"""Source-backed intake exclusions shared by intake and synthesis pools."""
from __future__ import annotations

import re
from typing import Any

from invoice_print_layout.receipt_intake import labelled_goods


RAIL_PAYMENT_UNUSED_REASON = '高铁报销只需12306发票，支付截图无需使用；原件已保留。'


def _rail_goods(value: str) -> bool:
    compact = re.sub(r'\s+', '', value).strip(':：·，,。')
    # "Railway" alone is not a passenger-ticket identity: logistics, retail,
    # membership and other non-ticket purchases remain available for synthesis.
    if any(word in compact for word in ('物流', '货运', '货物', '快递', '运费', '商城', '会员', '餐饮')):
        return False
    return bool(re.fullmatch(
        r'(?:12306(?:消费|车票|购票|订票|客票|火车票|高铁票)|'
        r'(?:中国)?铁路(?:客运(?:服务)?|电子客票|客票|车票)|高铁票|火车票)'
        r'(?:费|费用|付款|支付|订单|消费|服务费)?', compact, re.I))


def rail_payment_not_required(entry: dict[str, Any]) -> bool:
    """Identify passenger-ticket payments from visible or confirmed goods.

    Filename, money, a generic merchant and machine category never suffice.
    Invoices (including refund-fee invoices) and associated proofs are untouched.
    """
    if (entry.get('role') != 'payment' or entry.get('item_id') or
            entry.get('status') in {'linked', 'duplicate', 'converted'}):
        return False
    text = str(entry.get('text') or '')
    fields = entry.get('fields')
    if isinstance(fields, dict) and fields.get('payment_status') in {'refunded', 'failed', 'unpaid'}:
        return False
    # A stored legacy "payment" role may predate the current status parser.
    # Only explicit refund/failure state wording blocks, never a refund CTA.
    compact = re.sub(r'\s+', '', text)
    if any(marker in compact for marker in ('退款成功', '已退款', '交易失败', '支付失败', '交易关闭', '已撤销')):
        return False
    goods, _ = labelled_goods(text.splitlines())
    if goods and _rail_goods(goods):
        return True
    if not isinstance(fields, dict):
        return False
    value = str(fields.get('goods') or '')
    if not _rail_goods(value):
        return False
    manual = entry.get('manual_fields', [])
    sources = entry.get('field_sources', {})
    if ((isinstance(manual, list) and 'goods' in manual) or
            (isinstance(sources, dict) and sources.get('goods') == 'manual')):
        return True
    normalized = re.sub(r'\s+', '', value)
    for key in ('local_evidence', 'ai_evidence'):
        evidence = entry.get(key, {})
        quote = evidence.get('goods') if isinstance(evidence, dict) else None
        if isinstance(quote, str) and quote.strip():
            quote_compact = re.sub(r'\s+', '', quote)
            if normalized in quote_compact and quote_compact in compact:
                return True
    return False
