"""Conservative local evidence for shopping, payment and identifier namespaces.

These fields describe different actors.  A payment platform is not a shop, and
business orders are not payment orders merely because both are labelled 订单号.
Only explicit source text can establish an actor or an identifier namespace.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any
import unicodedata


_PLATFORMS = ('淘宝闪购', '美团外卖', '美团', '饿了么', '京东秒送', '京东',
              '淘宝', '天猫', '拼多多', '抖音商城', '携程', '同程旅行', '飞猪')
_CHANNELS = ('支付宝', '微信支付', '银联', '云闪付')
_PLATFORM_LABELS = ('购物平台', '购买平台', '订单平台', '交易平台', '平台')
_SHOP_LABELS = ('商家名称', '商户名称', '店铺名称', '门店名称', '收款商家',
                '交易商户', '商家', '商户', '店铺', '门店', '商品说明')
_CHANNEL_LABELS = ('支付渠道', '付款渠道', '支付方式', '付款方式', '支付平台', '付款平台')
_IDENTIFIER_LABELS: dict[str, str] = {
    '支付宝交易号': 'payment_transaction', '微信支付交易号': 'payment_transaction',
    '微信支付订单号': 'payment_order', '支付订单号': 'payment_order',
    '商家订单号': 'merchant_order', '商户订单号': 'merchant_order',
    '商家单号': 'merchant_order', '商户单号': 'merchant_order',
    '交易流水号': 'payment_transaction', '支付流水号': 'payment_transaction',
    '交易单号': 'payment_transaction', '交易号': 'payment_transaction',
    '支付单号': 'payment_order', '订单编号': 'order', '订单号': 'order',
}
_KINDS = frozenset({'shopping_order', 'payment_order', 'merchant_order',
                    'payment_transaction', 'untyped_order'})


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).strip()


def _labelled_values(text: str, labels: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Read an exact field label and its same-row or next-row value."""
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    values: list[tuple[str, str, str]] = []
    for index, raw in enumerate(rows):
        row = unicodedata.normalize('NFKC', raw)
        for label in sorted(labels, key=len, reverse=True):
            if row == label:
                if index + 1 < len(rows):
                    values.append((label, rows[index + 1], raw + '\n' + rows[index + 1]))
                break
            observed = re.fullmatch(re.escape(label) + r'(?:\s*:\s*|\s+)(.+)', row)
            if observed:
                values.append((label, observed[1].strip(), raw))
                break
            # OCR sometimes joins a numeric field label directly to its value.
            if label in _IDENTIFIER_LABELS:
                joined = re.fullmatch(re.escape(label) + r'(?=\d)(.+)', row)
                if joined:
                    values.append((label, joined[1].strip(), raw))
                    break
    return values


def _unique(candidates: dict[str, str]) -> tuple[str, str]:
    return next(iter(candidates.items())) if len(candidates) == 1 else ('', '')


def _shopping_platform(text: str) -> tuple[str, str]:
    candidates: dict[str, str] = {}
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    for raw in rows:
        if _compact(raw) in _PLATFORMS:
            candidates.setdefault(_compact(raw), raw)
    for _label, value, quote in _labelled_values(text, _PLATFORM_LABELS):
        if _compact(value) in _PLATFORMS:
            candidates.setdefault(_compact(value), quote)
    # The service acknowledgement names the platform even if the logo is not
    # legible.  It says nothing about the actual shop or payment channel.
    for index, row in enumerate(rows):
        quote = '\n'.join(rows[index:index + 2])
        compact = _compact(quote)
        for platform in _PLATFORMS:
            if re.search(r'感谢(?:你|您)?对' + re.escape(platform) + r'的信任', compact):
                candidates.setdefault(platform, quote)
    return _unique(candidates)


def _payment_channel(text: str) -> tuple[str, str]:
    candidates: dict[str, str] = {}
    for raw in text.splitlines():
        compact = _compact(raw)
        for channel in _CHANNELS:
            if compact in (channel, channel + '账单', channel + '账单详情', channel + '交易详情'):
                candidates.setdefault(channel, raw.strip())
    for _label, value, quote in _labelled_values(text, _CHANNEL_LABELS):
        compact = _compact(value)
        if compact == '微信':
            candidates.setdefault('微信支付', quote)
        for channel in _CHANNELS:
            if (compact == channel or
                    re.fullmatch(re.escape(channel) + r'(?:[(-].+|余额|余额宝|花呗)', compact)):
                candidates.setdefault(channel, quote)
    # A channel-specific transaction label is also explicit channel evidence.
    for label, _value, quote in _labelled_values(text, tuple(_IDENTIFIER_LABELS)):
        if label.startswith('支付宝'):
            candidates.setdefault('支付宝', quote)
        elif label.startswith('微信支付'):
            candidates.setdefault('微信支付', quote)
    return _unique(candidates)


def _shop_value(value: str) -> str:
    value = re.sub(r'^(?:淘宝闪购|闪购)[\s:：·|]*', '', value.strip())
    compact = _compact(value).strip(':：·')
    if (len(compact) < 2 or len(compact) > 100 or compact in (
            *_PLATFORMS, *_CHANNELS, '微信', '携程旅行网', '付款平台', '支付平台',
            '酒店旅游', '酒店住宿', '住宿服务', '餐饮服务', '餐饮', '酒店', '住宿',
            '材料采购', '其他') or '银行' in compact or
            any(marker in compact for marker in ('不是', '并非', '不属于', '仅供参考', '示例',
                                                  '联系电话', '订单号', '发票号')) or
            re.search(r'[￥¥]|\d{4}[-/]\d{1,2}[-/]|(?:实付|合计|总价|开票)\d', compact)):
        return ''
    return compact


def _shop_shaped(value: str) -> bool:
    return bool(re.search(r'(?:店|超市|商行|餐厅|饭馆|酒楼|小吃|公司)(?:\([^()]{1,60}\))?$', value))


def _shop_name(text: str, fields: Mapping[str, Any] | None,
               platform: str) -> tuple[str, str]:
    candidates: dict[str, str] = {}
    observed_values: list[tuple[str, str]] = []
    for label, value, quote in _labelled_values(text, _SHOP_LABELS):
        cleaned = _shop_value(value)
        if cleaned and (label != '商品说明' or _shop_shaped(cleaned)):
            candidates.setdefault(cleaned, quote)
            observed_values.append((cleaned, quote))
    # A shop-shaped heading is useful only when a shopping platform is explicit;
    # arbitrary company names elsewhere in a bill are not inferred as the shop.
    if platform:
        for row in text.splitlines():
            cleaned = _shop_value(row)
            labelled = any(re.match(r'^' + re.escape(label) + r'(?:\s|[:：])', row.strip())
                           for label in (*_SHOP_LABELS, *_PLATFORM_LABELS, *_CHANNEL_LABELS))
            if cleaned and _shop_shaped(cleaned) and not re.search(r'[:：]', row) and not labelled:
                candidates.setdefault(cleaned, row.strip())
                observed_values.append((cleaned, row.strip()))
    # Existing OCR fields may retain a heading even when its platform is absent.
    # Require its complete value in the original row, never a substring of a
    # longer shop/branch name or an ungrounded LLM candidate.
    for key in ('shop_name', 'merchant', 'goods'):
        candidate = _shop_value(str((fields or {}).get(key) or ''))
        if not candidate or (key == 'goods' and not _shop_shaped(candidate)):
            continue
        for raw in text.splitlines():
            cleaned = _shop_value(raw)
            if cleaned == candidate:
                candidates.setdefault(candidate, raw.strip())
        for observed, quote in observed_values:
            if observed == candidate:
                candidates.setdefault(candidate, quote)
    return _unique(candidates)


def _normalise_identifier(value: str) -> str:
    value = re.sub(r'(?:[|｜丨│]?\s*复制)$', '', value.strip())
    compact = _compact(value)
    if (not re.fullmatch(r'[A-Za-z0-9-]{6,64}', compact) or
            len(re.findall(r'\d', compact)) < 4 or
            re.fullmatch(r'1[3-9]\d{9}', compact) or
            re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}', compact) or
            not compact.strip('0')):
        return ''
    return compact


def identifier_key(identifier: Mapping[str, Any]) -> str:
    """Return an exact namespace-scoped key, or no hard key when uncertain."""
    kind = str(identifier.get('kind') or '')
    namespace = _compact(str(identifier.get('namespace') or ''))
    value = _normalise_identifier(str(identifier.get('value') or ''))
    if (kind not in _KINDS or kind == 'untyped_order' or not namespace or not value or
            namespace.lower() in {'unknown', '未知', '未识别', '未指定', '平台', '店铺', '商家', '支付渠道'}):
        return ''
    return json.dumps([kind, namespace, value], ensure_ascii=False, separators=(',', ':'))


def extract_transaction_evidence(text: str, role: str,
                                 fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Extract explicit actors and typed IDs; different ID types never conflict."""
    platform, platform_quote = _shopping_platform(text)
    channel, channel_quote = _payment_channel(text)
    shop, shop_quote = _shop_name(text, fields, platform)
    role = role.lower()
    order_kind = ('payment_order' if role == 'payment' else 'shopping_order'
                  if role in {'order', 'purchase', 'receipt', 'voucher'} or
                  re.search(r'订单已送达|订单详情|下单时间|订单创建时间', text)
                  else 'untyped_order')
    identifiers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for label, raw, quote in _labelled_values(text, tuple(_IDENTIFIER_LABELS)):
        value = _normalise_identifier(raw)
        kind = _IDENTIFIER_LABELS[label]
        if kind == 'order':
            kind = order_kind
        if not value or (kind, value) in seen:
            continue
        seen.add((kind, value))
        namespace = (platform if kind == 'shopping_order' else
                     channel if kind in {'payment_order', 'payment_transaction'} else
                     platform or shop if kind == 'merchant_order' else '')
        identifiers.append({'kind': kind, 'namespace': namespace, 'value': value,
                            'label': label, 'quote': quote})
    # Preserve conflicting observations for review, without asserting either is
    # the unique transaction ID from which a hard duplicate can be concluded.
    for identifier in identifiers:
        if len({item['value'] for item in identifiers if item['kind'] == identifier['kind']}) > 1:
            identifier['namespace'] = ''
    return {
        'fields': {'shopping_platform': platform, 'shop_name': shop, 'payment_channel': channel},
        'evidence': {'shopping_platform': platform_quote, 'shop_name': shop_quote,
                     'payment_channel': channel_quote},
        'identifiers': identifiers,
    }


def identifiers_for_record(record: Mapping[str, Any], role: str = '') -> list[dict[str, str]]:
    """Read flat saved metadata and labelled text without inventing ID scopes."""
    observed_fields = record.get('fields')
    fields = observed_fields if isinstance(observed_fields, Mapping) else {}
    metadata: list[Any] = []
    for value in (record.get('transaction_identifiers'), fields.get('transaction_identifiers')):
        if isinstance(value, list):
            metadata.extend(value)
    text = record.get('text')
    source = text if isinstance(text, str) else ''
    original_role = record.get('transaction_source_role')
    source_role = role or (original_role if isinstance(original_role, str) else
                          record.get('role') if isinstance(record.get('role'), str) else '')
    metadata.extend(extract_transaction_evidence(source, str(source_role or ''), fields)['identifiers'])
    result: list[dict[str, str]] = []
    indexed: dict[str, dict[str, str]] = {}
    for observed in metadata:
        if not isinstance(observed, Mapping):
            continue
        kind = observed.get('kind')
        raw_value = observed.get('value')
        if not isinstance(kind, str) or kind not in _KINDS or not isinstance(raw_value, str):
            continue
        value = _normalise_identifier(raw_value)
        if not value:
            continue
        item: dict[str, str] = {}
        for name in ('kind', 'namespace', 'label', 'quote'):
            content = observed.get(name)
            item[name] = content if isinstance(content, str) else ''
        item['value'] = value
        # A labelled plain order of unknown role must never inherit an invented
        # scope merely because saved metadata contains a namespace string.
        if kind == 'untyped_order':
            item['namespace'] = ''
        key = identifier_key(item) or json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in indexed:
            if not indexed[key]['quote'] and item['quote']:
                indexed[key]['quote'] = item['quote']
            if not indexed[key]['label'] and item['label']:
                indexed[key]['label'] = item['label']
            continue
        indexed[key] = item
        result.append(item)
    # A stored expense can legitimately aggregate several payment attachments.
    # Each already-scoped ID remains useful for duplicate checks; uncertainty
    # within one original was handled by extract_transaction_evidence above.
    return result


def record_identifier_keys(record: Mapping[str, Any], role: str = '') -> set[str]:
    """Get only exact typed, explicitly scoped duplicate keys for a record."""
    return {key for item in identifiers_for_record(record, role) if (key := identifier_key(item))}
