"""Exact product decisions grounded in the complete current source, never substrings.

The caller supplies ``material_facts(entry)`` from the normal checked-bytes
adapter.  This module reads those facts and the existing extracted records; it
does not mutate goods, infer quantities, change thresholds or call a model.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any
import unicodedata


PRODUCT_RULES_VERSION = 'whole-product-rules-v5'
EXACT_PRODUCT_CATEGORIES = {'亚克力导视牌': '材料采购', '透明亚克力导视牌': '材料采购',
                            '办公鼠标': '材料采购', '娃哈哈饮用水596ml*12瓶': '餐饮'}
_LABEL = re.compile(r'^(?:商品名称|商品说明|购买内容|货物或应税劳务名称|商品明细|'
                    r'购买明细|商品清单|商品列表|商品信息|项目明细|项目名称)\s*[:：]\s*(.*)$')
_SECTION = re.compile(r'^(?:商品明细|购买明细|商品清单|商品列表|商品信息|货物或应税劳务名称|项目明细|项目名称)[:：]?$')
_TAX = re.compile(r'^[*＊][^*＊]+[*＊]')
_BOUNDARY = re.compile(r'^(?:合计|价税合计|总计|总价|实付|实际付款|已提取总金额|付款金额|支付金额|'
                       r'支付时间|订单号|订单编号|商家订单号|交易号|发票号码|发票代码|开票日期|'
                       r'销售方|购买方|商家名称|店铺名称|收款方|收货|收件|地址|电话|手机|联系人|'
                       r'配送费|打包费|包装费|运费|价格明细|备注|开票人|商品金额)')
_METADATA = re.compile(r'^(?:数量|单位|单价|售价|原价|现价|参考价|税额|税率|金额)\s*[:：]?'
                       r'\s*(?:[¥￥]?[\d.+%×xX\s-]*(?:元|件|个|只|份|组|瓶|盒|杯|包|卷|块)?)?$')
_NUMERIC = re.compile(r'^[\d\s.+%¥￥×xX-]+(?:元|件|个|只|份|组|瓶|盒|杯|包|卷|块)?$')
_DECORATION = re.compile(r'^(?:加入购物车|\d+天.*(?:退|换)|已免运费|共\s*\d+\s*(?:件|份|组|杯))$')
_PARTS = re.compile(r'[、,，;；+＋\n\r]+')
_FOOD_END = re.compile(r'(?:蛋糕|面包|饼干|午餐|晚餐|早餐|盒饭|餐食|餐饮服务|奶茶|饮用水|矿泉水|纯净水|饮料)'
                       r'(?:\d+(?:\.\d+)?(?:份|个|盒|杯|瓶|组|包))?$')
_SERVICE_END = re.compile(r'(?:安装|租赁|设计|制作|维修|施工|调试|运输|加工|维护|装配|出租|租用)'
                          r'(?:费用?|服务)$')
_INLINE_PRICE = re.compile(r'(?:实付)?[¥￥]\s*\d+(?:\.\d+)?')
_CONTEXT_LABEL = re.compile(r'^(?:承运方|承运单位|承运公司)\s*[:：]')
_PRODUCT_ROLES = {'receipt', 'order', 'purchase', 'invoice', 'detail', 'trip'}
_ANCILLARY = {'派送服务', '物流网络配送费', '配送费', '打包费', '包装费'}


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def _parts(value: str) -> list[str]:
    return [part.strip() for part in _PARTS.split(value) if part.strip()]


def _tax_product(value: str) -> str:
    # Three detached numeric table columns establish a table suffix. A lone
    # number may instead be a product model/specification and stays untouched.
    columns = re.fullmatch(r'(.+?)\s+((?:[+-]?\d+(?:\.\d+)?%?\s+){2,}'
                           r'[+-]?\d+(?:\.\d+)?%?)', value.strip())
    return columns[1] if columns else value


def _ancillary_tax_end(rows: Sequence[str], index: int) -> int | None:
    tax = _TAX.match(rows[index])
    if tax is None:
        return None
    name = _compact(_tax_product(rows[index][tax.end():]))
    if name in _ANCILLARY:
        return index + 1
    if name == '物流网络':
        for position in range(index + 1, len(rows)):
            following = _compact(rows[position])
            if following == '配送费':
                return position + 1
            if following not in {'次', '无', '件', '单位', '数量', '单价', '金额', '税额'} and not (
                    _NUMERIC.fullmatch(rows[position]) or _METADATA.fullmatch(rows[position])):
                break
    return None


def _labelled_products(text: str) -> list[str]:
    """Keep every labelled section and its unlabelled continuation, in order.

    Numeric columns and explicit price fields are not names.  An unknown text
    row inside a goods section remains a product candidate instead of vanishing
    merely because it contains no word from our small rule vocabulary.
    """
    from invoice_print_layout.agent_content import _metadata_only_goods
    products: list[str] = []
    active = False
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    specific_label = False
    tax_section = False
    position = 0
    while position < len(rows):
        row = rows[position]
        labelled = _LABEL.fullmatch(row)
        tax = _TAX.match(row)
        if labelled:
            active = True
            tax_section = False
            parts = [part for part in _parts(labelled[1]) if not _metadata_only_goods(part)]
            products.extend(parts)
            specific_label = specific_label or any(_compact(part) not in {'日用百货', '商品', '商品不明', '其他'}
                                                  for part in parts)
        elif _SECTION.fullmatch(_compact(row)):
            active = True
        elif tax:
            active = True
            tax_section = True
            # A bare generic tax description can summarize an already explicit
            # purchase label. A valued/quantified generic row remains unknown.
            following = rows[position + 1] if position + 1 < len(rows) else ''
            if (specific_label and _compact(row) == '*日用杂品*日用百货' and
                    (not following or _BOUNDARY.match(_compact(following)))):
                position += 1
                continue
            ancillary_end = _ancillary_tax_end(rows, position) if products else None
            if ancillary_end is not None:
                position = ancillary_end
                continue
            products.extend(_parts(_tax_product(row[tax.end():])))
        elif tax_section and _compact(row) in {'合', '计'}:
            active = False
        elif _CONTEXT_LABEL.match(row):
            pass  # Carrier identity is context, while a following product stays visible.
        elif row == '订单已送达':
            pass  # Exact order status, without hiding the next product row.
        elif _BOUNDARY.match(_compact(row)):
            active = False
        elif active and not (_METADATA.fullmatch(row) or _NUMERIC.fullmatch(row) or
                             _DECORATION.fullmatch(row)):
            products.extend(_parts(row))
        position += 1
    return products


def _source_products(source: Mapping[str, Any], text: str) -> list[str]:
    # Native table geometry already supplies complete wrapped names without
    # numeric columns.  It is preferable to reconstructing table stream order.
    native = [row for row in source.get('goods', []) if isinstance(row, Mapping) and
              row.get('method') == 'native_geometry' and row.get('name') and row.get('quote')]
    if native and len(native) == len(source.get('goods', [])):
        text_compact = _compact(text)
        if all((_compact(str(row['name'])) in text_compact and
                _compact(str(row['name'])) in _compact(str(row['quote']))) or
               native_wrapped_product(row, text) == str(row['name']) for row in native):
            names = [part for row in native for part in _parts(str(row['name']))]
            specific = [name for name in names if _compact(name) not in _ANCILLARY]
            return (specific or names) + _native_uncovered_products(text, names, source=source)
    names = _labelled_products(text)
    if not names and _courier_detail_product(source, text):
        return ['顺丰快递费']
    if not names and str(source.get('role') or '') in {'purchase', 'order'}:
        names = _shopping_products(source, text)
    # Join only a whole explicitly enumerated product across adjacent lines.
    # This never joins two arbitrary unknown products or strips their qualifier.
    joined: list[str] = []
    for name in names:
        if joined and _compact(joined[-1] + name) in EXACT_PRODUCT_CATEGORIES:
            joined[-1] += name
        else:
            joined.append(name)
    return joined


def native_wrapped_product(row: Mapping[str, Any], text: str) -> str:
    """Prove a name-column wrap despite intervening numeric table columns.

    The geometry reader already owns the column association. Independently
    reconstruct its quote's name fragments and require every quoted token in
    this same full source; unknown fragments and shortened raw names fail.
    """
    if row.get('method') != 'native_geometry':
        return ''
    name = str(row.get('name') or '')
    raw = str(row.get('raw_name') or '')
    quote_rows = [value.strip() for value in str(row.get('quote') or '').splitlines() if value.strip()]
    if not name or not _TAX.match(raw) or not quote_rows or not _TAX.match(quote_rows[0]):
        return ''
    if _compact(_TAX.sub('', raw)) != _compact(name):
        return ''
    fragments: list[str] = []
    source = _compact(text)
    for value in quote_rows:
        if any(_compact(token) not in source for token in value.split()):
            return ''
        plain = _TAX.sub('', value)
        columns = re.split(r'\s+(?=(?:[+-]?\d|无(?:\s|$)|次(?:\s|$)|天(?:\s|$)|'
                           r'个(?:\s|$)|件(?:\s|$)|只(?:\s|$)|份(?:\s|$)|组(?:\s|$)|'
                           r'瓶(?:\s|$)|盒(?:\s|$)|杯(?:\s|$)|包(?:\s|$)|卷(?:\s|$)|块(?:\s|$)))',
                           plain, maxsplit=1)
        if len(columns) == 2 and any(not (_NUMERIC.fullmatch(token) or token in
                {'无', '次', '天', '个', '件', '只', '份', '组', '瓶', '盒', '杯', '包', '卷', '块'} or
                re.fullmatch(r'\d+(?:\.\d+)?(?:ml|mL|ML)[*×xX]\d+(?:瓶|盒|包|组)?', token) and
                _compact(name).count(_compact(token)) == 1)
                for token in columns[1].split()):
            return ''
        fragments.append(columns[0])
    return name if _compact(''.join(fragments)) == _compact(name) else ''


def _courier_detail_product(source: Mapping[str, Any], text: str) -> bool:
    """Read the complete native SF waybill table, including its exact footer.

    This is a document schema, not a blanket exception for dedicated tickets.
    An extra row, product label, unsupported layout or unequal total stays held.
    """
    if source.get('role') != 'detail':
        return False
    goods = source.get('goods') or []
    if (len(goods) != 1 or not isinstance(goods[0], Mapping) or
            goods[0].get('name') != '顺丰快递费' or goods[0].get('method') != 'native_text'):
        return False
    rows = [_compact(row).replace('—', '-') for row in text.splitlines() if row.strip()]
    if rows and re.fullmatch(r'\d+-\d+', rows[0]):
        rows = rows[1:]
    header = ['顺丰电子发票-运单明细', '#', '运单号码', '寄件时间', '寄方', '寄件地',
              '收方', '收件地', '运单金额(元)']
    if rows[:len(header)] != header or rows.count('开票时间') != 1:
        return False
    split = rows.index('开票时间')
    table, footer = rows[len(header):split], rows[split:]
    if (not table or len(table) % 8 or len(table) > 160 or len(footer) != 7 or
            footer[:2] != ['开票时间', '发票号码'] or footer[3] != '公司名称' or
            footer[5] != '发票总金额(元)' or not re.fullmatch(
                r'\d{4}/\d{2}/\d{2}(?:\d{15,30}|\[编号省略\])', footer[2]) or
            not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9()]{2,100}(?:公司|企业|单位|学校)', footer[4])):
        return False
    from decimal import Decimal, InvalidOperation
    total = Decimal(0)
    for position in range(0, len(table), 8):
        index, waybill, date, sender, origin, recipient, destination, money = table[position:position + 8]
        if (index != str(position // 8 + 1) or not re.fullmatch(r'SF\d{8,24}|\[编号省略\]', waybill) or
                not re.fullmatch(r'\d{4}/\d{2}/\d{2}', date) or
                not all(re.fullmatch(r'[\u4e00-\u9fffA-Za-z·]{1,40}', value) for value in (sender, recipient)) or
                not all(re.fullmatch(r'[\u4e00-\u9fff]{1,30}(?:市|区|县|州)', value) for value in (origin, destination)) or
                not re.fullmatch(r'\d+\.\d{2}', money)):
            return False
        total += Decimal(money)
    try:
        return bool(re.fullmatch(r'\d+\.\d{2}', footer[6]) and total > 0 and
                    total == Decimal(footer[6]) and int(total * 100) == source.get('total_cents'))
    except InvalidOperation:
        return False


def _native_metadata_lines(source: Mapping[str, Any], text: str) -> set[int]:
    """Exclude only one exact row backed by a current native labelled field."""
    metadata = source.get('native_metadata')
    digest = str(source.get('sha256') or source.get('digest') or '')
    if (source.get('role') != 'invoice' or not re.fullmatch(r'[0-9a-f]{64}', digest) or
            not isinstance(metadata, list)):
        return set()
    labels = {'invoice_total_words': r'价税合计\(大写\)[:：]?', 'issuer': r'开票人[:：]?'}
    rows = [_compact(row) for row in text.splitlines()]
    excluded = set()
    for field in metadata:
        if not isinstance(field, Mapping):
            continue
        kind = field.get('kind')
        if not isinstance(kind, str) or kind not in labels or sum(isinstance(row, Mapping) and row.get('kind') == kind
                                     for row in metadata) != 1:
            continue
        value = _compact(str(field.get('value') or ''))
        quote = _compact(str(field.get('quote') or ''))
        box = field.get('bbox')
        if (field.get('method') != 'native_geometry' or field.get('sha256') != digest or
                type(field.get('page')) is not int or not 1 <= field['page'] <= 10 or not value or
                not isinstance(field.get('value'), str) or not isinstance(field.get('quote'), str) or
                not isinstance(box, (list, tuple)) or len(box) != 4 or
                any(type(v) not in {int, float} or not math.isfinite(v) for v in box) or
                box[0] >= box[2] or box[1] >= box[3] or
                re.fullmatch(labels[kind] + re.escape(value), quote) is None or
                rows.count(value) != 1):
            continue
        excluded.add(rows.index(value))
    return excluded


def _native_uncovered_products(text: str, names: Sequence[str], *,
                               source: Mapping[str, Any] | None = None) -> list[str]:
    """A table fact must not hide another explicit label or tax-row suffix."""
    extras: list[str] = []
    active = False
    metadata_lines = _native_metadata_lines(source, text) if source is not None else set()
    for index, raw in enumerate(text.splitlines()):
        if index in metadata_lines:
            continue
        row = raw.strip()
        labelled = _LABEL.fullmatch(row)
        tax = _TAX.match(row)
        candidates: list[str] = []
        if labelled:
            candidates = _parts(labelled[1])
        elif tax:
            active = True
            candidates = _parts(row[tax.end():])
        elif active:
            if ((_BOUNDARY.match(_compact(row)) and _compact(row) not in _ANCILLARY) or
                    _compact(row).startswith('下载次数')):
                active = False
            elif not (_METADATA.fullmatch(row) or _NUMERIC.fullmatch(row) or
                      re.fullmatch(r'天|无|次|个|件|只|份|组|瓶|盒|单位|数量|单价|金额|税额', _compact(row))):
                candidates = _parts(row)
        for candidate in candidates:
            # Drop only separated numeric table columns, never word suffixes.
            title = re.split(r'\s+(?:-?\d|(?:天|个|件|只|份|组|瓶|盒|杯|包|卷|块)(?:\s|$))',
                             candidate, maxsplit=1)[0]
            compact = _compact(title)
            if compact and not any(compact in _compact(name) for name in names):
                extras.append(title)
    return extras


def _shopping_products(source: Mapping[str, Any], text: str) -> list[str]:
    """Read a bounded real shopping block, including unquoted continuations.

    Existing fact quotes group adjacent wrapped title lines. A new unknown row
    is retained as its own candidate; only a proven split word such as 台式/电脑
    may extend a preceding quote. Merchant/header/footer text stays outside.
    """
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    actors = source.get('actors') or {}
    shop = actors.get('shop') or {} if isinstance(actors, Mapping) else {}
    shop_quote = _compact(str(shop.get('quote') or '')) if isinstance(shop, Mapping) else ''
    anchors = [index for index, row in enumerate(rows) if
               re.match(r'^(?:淘宝)?闪购', _compact(row)) or shop_quote and _compact(row) == shop_quote]
    if len(anchors) != 1:
        return []
    quotes = [_compact(str(row.get('quote') or '')) for row in source.get('goods', [])
              if isinstance(row, Mapping) and row.get('quote')]
    groups: list[list[str]] = []
    owners: list[int | None] = []
    for row in rows[anchors[0] + 1:]:
        if _BOUNDARY.match(_compact(row)):
            break
        if _DECORATION.fullmatch(row) or _NUMERIC.fullmatch(row) or _METADATA.fullmatch(row):
            continue
        clean = _INLINE_PRICE.sub('', row).replace('加入购物车', '').strip()
        if not clean:
            continue
        matches = [index for index, quote in enumerate(quotes) if _compact(row) in quote]
        owner = matches[0] if matches else None
        if owner is not None and owner in owners:
            groups[owners.index(owner)].append(clean)
        elif (groups and owner is None and
              (re.search(r'(?:台式|笔记本)$', _compact(groups[-1][-1])) and _compact(clean).startswith('电脑') or
               _compact(''.join(groups[-1]) + clean) in EXACT_PRODUCT_CATEGORIES)):
            groups[-1].append(clean)
        else:
            groups.append([clean])
            owners.append(owner)
    return ['\n'.join(group) for group in groups]


def _join_grounded_wraps(names: Sequence[str], records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Reuse a validated text completion's exact contiguous modifier span."""
    from invoice_print_layout.agent_goods_ai import _modifier_fragment
    result: list[str] = []
    position = 0
    while position < len(names):
        joined = False
        if _modifier_fragment(names[position]):
            for end in range(position + 2, len(names) + 1):
                quote = '\n'.join(names[position:end])
                record = next((record for record in records if record.get('method') == 'glm_text_grounded' and
                               str(record.get('quote') or '').strip() == quote and
                               _compact(str(record.get('raw_name') or record.get('name') or '')) == _compact(quote)), None)
                if record is not None:
                    result.append(str(record.get('raw_name') or record['name']))
                    position = end
                    joined = True
                    break
        if not joined:
            result.append(names[position])
            position += 1
    return result


def _related(record: Mapping[str, Any], product: str) -> bool:
    """Associate a source product with a record, without deciding its category.

    Short old names can still locate a complete source product (mouse cake,
    installation etc.). Only the *complete* product is then classified.
    """
    complete = _compact(product)
    name = _compact(str(record.get('name') or ''))
    raw = _compact(str(record.get('raw_name') or ''))
    quote = _compact(str(record.get('quote') or ''))
    if not complete or not name:
        return False
    return complete == name or complete == raw or bool((name in complete or raw and raw in complete) and
                                                     (not raw or raw in complete or complete in raw or complete in quote))


def _all_covered(records: Sequence[Mapping[str, Any]], products: Sequence[str]) -> bool:
    # Each actual source row needs its own record. A familiar name must not
    # account for a missing neighbouring item or for two independent duplicates.
    owners: dict[int, int] = {}

    def assign(index: int, seen: set[int]) -> bool:
        for position, record in enumerate(records):
            if position in seen or not _related(record, products[index]):
                continue
            seen.add(position)
            previous = owners.get(position)
            if previous is None or assign(previous, seen):
                owners[position] = index
                return True
        return False

    return (all(assign(index, set()) for index in range(len(products))) and
            all(any(_related(record, product) for product in products) for record in records))


def inspect_supplementary_invoice(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Describe one complete native generic invoice; never grant its admission.

    The input is a current checked-bytes ``material_facts`` result. A caller
    must separately prove its existing draft relation and equal-amount complete
    purchase details before treating this non-specific invoice as supplemental.
    This function does not skip any source in ``inspect_product_rules`` and
    cannot assign an expense category or invent a concrete purchased product.
    """
    if (source.get('role') != 'invoice' or source.get('complete') is not True or
            source.get('goods_complete') is not True or source.get('source_text_truncated') is not False or
            source.get('goods_text_truncated') is not False or source.get('issues') != [] or
            source.get('goods_issues') != []):
        return None
    ident = str(source.get('material_id') or source.get('id') or '')
    sha256 = str(source.get('sha256') or source.get('digest') or '')
    goods = source.get('goods')
    if not ident or not re.fullmatch(r'[0-9a-f]{64}', sha256) or not isinstance(goods, list) or len(goods) != 1:
        return None
    row = goods[0]
    expected_raw = '*日用杂品*日用百货'
    if (not isinstance(row, Mapping) or row.get('method') != 'native_geometry' or
            _compact(str(row.get('name') or '')) != '日用百货' or
            _compact(str(row.get('raw_name') or '')) != expected_raw or row.get('is_discount')):
        return None
    total, amount, tax_amount = source.get('total_cents'), row.get('amount_cents'), row.get('tax_cents')
    if (type(total) is not int or type(amount) is not int or type(tax_amount) is not int or
            total <= 0 or amount <= 0 or tax_amount < 0 or amount + tax_amount != total):
        return None
    text = str(source.get('source_text') or '')
    quote = str(row.get('quote') or '')
    normalized_quote = unicodedata.normalize('NFKC', quote).strip()
    if (not text or not re.fullmatch(re.escape(expected_raw) +
                                    r'(?:\s+[+-]?\d+(?:\.\d+)?%?)+', normalized_quote)):
        return None
    rows = [part.strip() for part in text.splitlines() if part.strip()]
    tax_rows = [part for part in rows if _TAX.match(part)]
    if (len(tax_rows) != 1 or len(re.findall(r'[*＊][^*＊\r\n]+[*＊]', text)) != 1 or
            _compact(_tax_product(tax_rows[0])) != expected_raw):
        return None
    # Ordinary invoice parsing stops at totals/footer. Supplemental admission
    # is stricter: unexplained text after that point cannot disappear merely
    # because it follows a known footer, which could hide another product.
    tail = rows[rows.index(tax_rows[0]) + 1:]
    for position, part in enumerate(tail):
        compact = _compact(part)
        if (_NUMERIC.fullmatch(part) or _METADATA.fullmatch(part) or
                compact in {'单位', '数量', '单价', '金额', '税额', '税率', '税率/征收率',
                            '次', '无', '个', '件', '只', '份', '组', '瓶', '盒', '包',
                            '合', '计', '合计', '价税合计', '价税合计(大写)', '价税合计(小写)',
                            '(大写)', '(小写)', '备注'}):
            continue
        if re.fullmatch(r'下载次数[:：]\d+', compact) and position == len(tail) - 1:
            continue
        if re.fullmatch(r'(?:合计|价税合计(?:\(小写\))?|小写)[:：]?[¥￥]?\d+(?:\.\d+)?', compact):
            continue
        if re.fullmatch(r'[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]+', compact):
            continue
        return None
    for part in rows:
        labelled = _LABEL.fullmatch(part)
        if labelled and labelled[1].strip():
            return None
        if (_SECTION.fullmatch(_compact(part)) and _compact(part).rstrip(':：') not in
                {'项目名称', '货物或应税劳务名称'}):
            return None
        if re.match(r'^备\s*注\s*[:：]\s*\S', part):
            return None
    # The tail has been checked more strictly above. Ordinary table parsing
    # may expose its detached 合/计 or amount words as uncovered fragments.
    if any(part not in tail for part in _native_uncovered_products(text, ['日用百货'])):
        return None
    return {'kind': 'supplementary_generic_invoice', 'non_specific': True,
            'material_id': ident, 'sha256': sha256, 'quote': quote,
            'amount_cents': total, 'item_amount_cents': amount, 'tax_cents': tax_amount,
            'reason': '当前完整发票仅列通用日用杂品税目，不提供具体商品事实；需另证与完整采购明细的关联'}


def _native_dining_invoice(source: Mapping[str, Any]) -> dict[str, Any] | None:
    """Qualify one explicit dining tax row, not an arbitrary service keyword."""
    if (source.get('role') != 'invoice' or source.get('complete') is not True or
            source.get('goods_complete') is not True or source.get('source_text_truncated') is not False or
            source.get('goods_text_truncated') is not False or source.get('issues') != [] or
            source.get('goods_issues') != []):
        return None
    ident = str(source.get('material_id') or source.get('id') or '')
    sha256 = str(source.get('sha256') or source.get('digest') or '')
    goods = source.get('goods')
    if not ident or not re.fullmatch(r'[0-9a-f]{64}', sha256) or not isinstance(goods, list) or len(goods) != 1:
        return None
    row = goods[0]
    raw = '*生产生活服务*餐饮服务'
    if (not isinstance(row, Mapping) or row.get('method') != 'native_geometry' or row.get('is_discount') or
            _compact(str(row.get('name') or '')) != '餐饮服务' or
            _compact(str(row.get('raw_name') or '')) != raw):
        return None
    total, amount, tax = source.get('total_cents'), row.get('amount_cents'), row.get('tax_cents')
    if (type(total) is not int or type(amount) is not int or type(tax) is not int or
            total <= 0 or amount <= 0 or tax < 0 or amount + tax != total):
        return None
    text, quote = str(source.get('source_text') or ''), str(row.get('quote') or '')
    normalized = unicodedata.normalize('NFKC', quote).strip()
    if not re.fullmatch(re.escape(raw) + r'(?:\s+\d+(?:\.\d+)?%?)+', normalized):
        return None
    columns = normalized[len(raw):].split()
    if (len(columns) < 3 or columns[-3] != f'{amount / 100:.2f}' or
            not re.fullmatch(r'\d+(?:\.\d+)?%', columns[-2]) or columns[-1] != f'{tax / 100:.2f}'):
        return None
    rows = [part.strip() for part in text.splitlines() if part.strip()]
    tax_rows = [part for part in rows if _TAX.match(part)]
    if (len(tax_rows) != 1 or len(re.findall(r'[*＊][^*＊\r\n]+[*＊]', text)) != 1 or
            _compact(_tax_product(tax_rows[0])) != raw):
        return None
    tokens = {_compact(token).lstrip('¥￥') for part in rows for token in part.split()}
    if any(column not in tokens for column in columns):
        return None
    # A footer cannot hide an extra service/product outside the geometry row.
    for part in rows[rows.index(tax_rows[0]) + 1:]:
        compact = _compact(part)
        if (_NUMERIC.fullmatch(part) or _METADATA.fullmatch(part) or compact in
                {'合', '计', '合计', '价税合计', '价税合计(大写)', '价税合计(小写)', '(大写)', '(小写)'} or
                re.fullmatch(r'(?:合计|价税合计(?:\(小写\))?|小写)[:：]?[¥￥]?\d+(?:\.\d+)?', compact) or
                re.fullmatch(r'[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]+', compact)):
            continue
        return None
    return {'kind': 'native_dining_service', 'material_id': ident, 'sha256': sha256,
            'category': '餐饮', 'quote': quote, 'amount_cents': total,
            'item_amount_cents': amount, 'tax_cents': tax}


def inspect_product_rules(goods: Sequence[Mapping[str, Any]], sources: Sequence[Mapping[str, Any]],
                          categories: Mapping[str, str]) -> dict[str, Any]:
    """Return exact/model/needs_input and the complete goods text for classification.

    ``sources`` is the unmodified result list of ``material_facts(entry,
    cache_root=store.root)``; its ``id`` and ``sha256`` bind records to current
    originals. ``material_id``/``digest`` aliases are accepted for callers that
    retain the intake field names. Payment-only sources contribute no products.
    Existing user/preference decisions remain the caller's responsibility.
    """
    products: list[dict[str, str]] = []
    coverage_complete = False
    service_proofs: dict[str, dict[str, Any]] = {}

    def result(status: str, reason: str, category: str | None = None) -> dict[str, Any]:
        forbidden = ['材料采购'] if any(_FOOD_END.search(_compact(row['name'])) or
                                     _SERVICE_END.search(_compact(row['name'])) for row in products) else []
        return {'status': status, 'category': category, 'reason': reason,
                'goods_text': '\n'.join(row['name'] for row in products), 'products': products,
                'forbidden_categories': forbidden,
                'qualified_service_sources': list(service_proofs.values()),
                'coverage_complete': coverage_complete,
                'rules_version': PRODUCT_RULES_VERSION}

    if not goods:
        return result('needs_input', '未提取到完整商品，请在人工工作台补充商品信息')
    by_id: dict[str, Mapping[str, Any]] = {}
    for source in sources:
        ident = str(source.get('material_id') or source.get('id') or '')
        if ident in by_id:
            return result('needs_input', '同一材料出现多份原文，需重新读取当前原件')
        by_id[ident] = source
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for good in goods:
        ident = str(good.get('material_id') or '')
        matched_source = by_id.get(ident)
        if not ident or matched_source is None or not good.get('sha256') or str(good['sha256']) != str(
                matched_source.get('sha256') or matched_source.get('digest') or ''):
            return result('needs_input', '商品与当前材料来源不一致，请重新读取原件')
        grouped.setdefault(ident, []).append(good)
    for ident, source in by_id.items():
        if str(source.get('role') or '') == 'payment':
            if ident in grouped:
                return result('needs_input', '付款凭证不能替代完整购买商品明细')
            continue
        if str(source.get('role') or '') not in _PRODUCT_ROLES and ident not in grouped:
            continue
        text = str(source.get('source_text') or '')
        if (not text or source.get('complete') is False or source.get('issues') or
                source.get('source_text_truncated') or source.get('goods_text_truncated') or
                not source.get('goods_complete') or source.get('goods_issues')):
            return result('needs_input', '商品原文缺失、截断或尚未完整提取，请补充完整原件')
        names = _source_products(source, text)
        if not names:
            return result('needs_input', '原件未找到完整商品边界，请在人工工作台核对商品明细')
        records = grouped.get(ident, [])
        names = _join_grounded_wraps(names, records)
        if not _all_covered(records, names):
            return result('needs_input', '完整原文含有尚未对应到商品记录的内容，请核对是否漏提商品或限定文字')
        if proof := _native_dining_invoice(source):
            service_proofs[ident] = proof
        products.extend({'name': name, 'material_id': ident,
                         'sha256': str(source.get('sha256') or source.get('digest') or '')} for name in names)
    if not products:
        return result('needs_input', '没有可核对的完整购买商品原文')
    coverage_complete = True
    if len('\n'.join(row['name'] for row in products)) > 3000:
        return result('needs_input', '完整商品文字超出分类范围，请在人工工作台核对并拆分费用')
    exact_categories = {_compact(name): category for name, category in
                        {**categories, **EXACT_PRODUCT_CATEGORIES}.items()}
    resolved = ['餐饮' if _compact(row['name']) == '餐饮服务' and row['material_id'] in service_proofs else
                exact_categories.get(_compact(row['name'])) for row in products]
    known = {category for category in resolved if category is not None}
    material = any(category == '材料采购' for category in resolved)
    mixed_food = material and any(_FOOD_END.search(_compact(row['name'])) for row in products)
    mixed_service = material and any(category is None and _SERVICE_END.search(_compact(row['name']))
                                     for row, category in zip(products, resolved))
    if len(known) > 1 or mixed_food or mixed_service:
        return result('needs_input', '完整商品中存在食品、实物或服务等不同类别，请在人工工作台确认是否拆分费用')
    if None not in resolved and len(known) == 1:
        return result('exact', '完整原文的全部商品精确命中同一类别规则', next(iter(known)))
    return result('model', '商品原文完整，含未精确命中的商品或服务，需结合完整内容判断')
