"""Read-only, source-bound product evidence roles within an already created draft.

An auxiliary receipt is never discarded.  This adapter only keeps its amount
and relation proof separate from the complete source that describes the goods.
The caller has already authenticated the draft and read/hash-checked its facts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
from typing import Any
import unicodedata

from invoice_print_layout.agent_product_rules import inspect_product_rules, inspect_supplementary_invoice


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def _amount(value: Any) -> bool:
    return type(value) is int and value > 0


def _table_money(value: str) -> int | None:
    if not re.fullmatch(r'\d+\.\d{2}元?', value):
        return None
    try:
        return int(Decimal(value.removesuffix('元')) * 100)
    except InvalidOperation:
        return None


def _transport_table(profile: Mapping[str, Any]) -> bool:
    """Cover the complete native single-page table, not its role or title alone."""
    if profile.get('page_count') != 1:
        return False
    rows = [_compact(row) for row in str(profile.get('text') or '').splitlines() if row.strip()]
    if any(_PRODUCT_AREA.search(row) or re.match(r'^(?:商品|购买内容|货物|安装|租赁|维修)[:：]', row)
           for row in rows):
        return False
    ride = profile.get('kind') == 'ride_trip'
    city = profile.get('kind') == 'waybill' and profile.get('courier_kind') == '同城'
    if not (ride or city):
        return False
    headers = (['序号', '服务商', '车型', '上车时间', '城市', '起点', '终点', '金额'] if ride else
               ['序号', '订单类型', '下单时间', '发件信息', '收件信息', '订单号', '金额', '支付方式'])
    starts = [i for i in range(len(rows)) if rows[i:i + len(headers)] == headers]
    if len(starts) != 1:
        return False
    summary = (r'共(?:计)?(\d+)(?:个|单|次|笔)行程[,，]?(?:合计|总计可开票金额)(\d+\.\d{2})元' if ride else
               r'共(\d+)笔运单[,，]?合计(\d+\.\d{2})元')
    totals = [m for row in rows if (m := re.fullmatch(summary, row))]
    if len(totals) != 1 or not 1 <= int(totals[0][1]) <= 40 or _table_money(totals[0][2]) != profile.get('amount_cents'):
        return False
    count, start = int(totals[0][1]), starts[0]

    def metadata(row: str) -> bool:
        if re.fullmatch(summary, row) or re.fullmatch(r'页码[:：]?1[/／]1', row):
            return True
        if ride:
            return bool(row == 'AMAPITINERARY' or
                        re.fullmatch(r'(?:高德地图|腾讯出行服务)[—-]+打车[—-]+行程单', row) or
                        re.fullmatch(r'(?:申请时间|申请日期)[:：]\d{4}-\d{2}-\d{2}(?:\d{2}:\d{2}:\d{2})?', row) or
                        re.fullmatch(r'行程时间[:：]\d{4}-\d{2}-\d{2}\d{2}:\d{2}至\d{4}-\d{2}-\d{2}\d{2}:\d{2}', row) or
                        re.fullmatch(r'(?:行程人手机号|手机号)[:：]\d{11}', row) or row in {
                            '1.行程单信息仅作为乘客出行记录参考,实际报销金额请以电子发票金额为准。',
                            '2.行程单上合计总金额,可能包含优惠券金额和高速费、路桥费、停车费等出行中附加产生金额。'})
        return bool(row == '顺丰同城运单明细' or
                    re.fullmatch(r'运单起止日期[:：]\d{4}-\d{2}-\d{2}至\d{4}-\d{2}-\d{2}', row))

    if not all(metadata(row) for row in rows[:start]):
        return False
    cursor = start + len(headers)
    if ride and cursor < len(rows) and rows[cursor] == '(元)':
        cursor += 1
    amounts = []
    for ordinal in range(1, count + 1):
        if cursor >= len(rows) or rows[cursor] != str(ordinal):
            return False
        cursor += 1
        end = next((i for i in range(cursor, len(rows)) if
                    (ordinal < count and rows[i] == str(ordinal + 1)) or metadata(rows[i])), len(rows))
        values = rows[cursor:end]
        if ride:
            if (len(values) < 7 or not re.fullmatch(r'(?:T3出行|享道出行|滴滴(?:出行)?|曹操出行)', values[0]) or
                    not values[1].endswith('型') or
                    not re.fullmatch(r'(?:\d{4}-)?\d{2}-\d{2}\d{2}:\d{2}', values[2]) or
                    not re.fullmatch(r'.{1,15}市', values[3])):
                return False
            amount = _table_money(values[-1])
        else:
            if (len(values) < 8 or values[0] != '帮我送' or
                    not re.fullmatch(r'\d{2}-\d{2}周[一二三四五六日天]', values[1]) or
                    not re.fullmatch(r'\d{10,24}', values[-3]) or values[-1] != '在线支付'):
                return False
            amount = _table_money(values[-2])
        if amount is None or amount <= 0:
            return False
        amounts.append(amount)
        cursor = end
    return sum(amounts) == profile['amount_cents'] and all(metadata(row) for row in rows[cursor:])


def _transport_support(item: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
                       goods: Sequence[Mapping[str, Any]], categories: Mapping[str, str],
                       entries: Sequence[Mapping[str, Any]] | None) -> dict[str, Any] | None:
    """An authenticated service detail is supplementary, never invented goods."""
    if (entries is None or len(entries) != 2 or len(facts) != 2 or len(goods) != 1 or
            item.get('stage') != 'draft' or item.get('verified') or item.get('deleted') or
            item.get('merged_into') or item.get('amount_pending') or not _amount(item.get('amount_cents')) or
            not isinstance(item.get('project_id'), str) or not item.get('project_id')):
        return None
    by_id = {e.get('id'): e for e in entries}
    if len(by_id) != 2 or {f.get('id') for f in facts} != set(by_id):
        return None
    for fact in facts:
        entry = by_id[fact['id']]
        if (not _clean_source(fact, products=False) or entry.get('digest') != fact.get('sha256') or
                entry.get('project_id') != item.get('project_id') or entry.get('role') != fact.get('role') or
                entry.get('revision') != fact.get('revision') or
                fact.get('total_cents') != item['amount_cents'] or entry.get('field_conflicts')):
            return None
    invoices = [f for f in facts if f.get('role') == 'invoice']
    supports = [f for f in facts if f.get('role') in {'trip', 'detail'}]
    if len(invoices) != 1 or len(supports) != 1:
        return None
    # Dedicated trip service labels repeat the invoice's passenger service.
    if supports[0].get('goods') and not (supports[0].get('role') == 'trip' and all(
            g.get('name') in {'网约车客运', '出租汽车客运'} for g in supports[0]['goods'])):
        return None
    invoice, support = invoices[0], supports[0]
    if goods[0].get('material_id') != invoice['id'] or goods[0].get('sha256') != invoice['sha256']:
        return None
    recognition = (item.get('material_match') or {}).get('recognition') or {}
    if (recognition.get('amount_source') != 'vouchers' or recognition.get('payments') or
            any(recognition.get(k) != item['amount_cents'] for k in ('amount_cents', 'voucher_total_cents')) or
            recognition.get('amount_difference_cents') != 0):
        return None
    for key, source in (('vouchers', invoice), ('supports', support)):
        rows = recognition.get(key)
        if (not isinstance(rows, list) or len(rows) != 1 or rows[0].get('queue_id') != source['id'] or
                rows[0].get('key') != 'q:' + source['id'] or rows[0].get('role') != source['role'] or
                rows[0].get('amount_cents') != item['amount_cents']):
            return None
    from invoice_print_layout.agent_transport import pair_status, transport_profile
    original_invoice, original_support = dict(by_id[invoice['id']]), dict(by_id[support['id']])
    invoice_profile, support_profile = transport_profile(original_invoice), transport_profile(original_support)
    if (not invoice_profile or not support_profile or
            invoice_profile.get('kind') not in {'ride_invoice', 'courier_invoice'} or
            invoice_profile.get('source_sha256') != invoice['sha256'] or
            support_profile.get('source_sha256') != support['sha256'] or
            invoice_profile.get('amount_cents') != item['amount_cents'] or
            support_profile.get('amount_cents') != item['amount_cents'] or
            not _transport_table(support_profile)):
        return None
    status = pair_status(original_invoice, original_support)
    if status not in {'supported', 'needs_fact'}:
        return None
    relation = item.get('agent_relation') or {}
    digests = {f['sha256'] for f in facts}
    source_fact = relation.get('source_fact') or {}
    context = relation.get('source_context') or {}
    confirmed = (relation.get('relation_kind') == 'user_confirmed' and
                 relation.get('confirmation_id') == source_fact.get('fact_id') and
                 isinstance(source_fact.get('fact_id'), str) and bool(source_fact['fact_id']) and
                 source_fact.get('actor') == 'user' and source_fact.get('action') == 'confirm_relation' and
                 source_fact.get('claim') == 'same_expense' and source_fact.get('amount_cents') == item['amount_cents'] and
                 set(source_fact.get('material_digests', [])) == digests)
    native = (status == 'supported' and context.get('kind') == 'native_transport_relation' and
              {(a.get('material_id'), a.get('digest')) for a in context.get('attachments', [])} ==
              {(f['id'], f['sha256']) for f in facts})
    if not (confirmed or native):
        return None
    category = '打车' if invoice_profile['kind'] == 'ride_invoice' else '顺丰'
    aliases = ({'客运服务费': category, '客运服务': category} if category == '打车' else
               {'收派服务': category, '收派服务费': category})
    checked = inspect_product_rules(goods, [invoice], {**categories, **aliases})
    if checked['status'] != 'exact' or checked['category'] != category or not checked['coverage_complete']:
        return None
    return {'sources': deepcopy([invoice]), 'source_categories': aliases, 'supplementary': [
        {'kind': 'supplementary_native_transport_detail', 'material_id': support['id'],
         'sha256': support['sha256'], 'amount_cents': support['total_cents'],
         'quote': support_profile['text'], 'reason': '完整原生服务明细与已认证发票关系、表格行数及金额守恒；按发票只计一次'}]}


def _clean_source(fact: Mapping[str, Any], *, products: bool = True) -> bool:
    return bool(fact.get('complete') is True and fact.get('source_text') and
                fact.get('issues') == [] and fact.get('goods_issues') == [] and
                fact.get('source_text_truncated') is False and fact.get('goods_text_truncated') is False and
                (not products or fact.get('goods_complete') is True) and
                _amount(fact.get('total_cents')))


def _quote_sources(fact: Mapping[str, Any]) -> list[str]:
    values = [str(fact.get(key) or '') for key in ('source_text', 'text')]
    actors = fact.get('actors') or {}
    groups = [fact.get('goods') or [], fact.get('descriptions') or [],
              list(actors.values()) if isinstance(actors, Mapping) else []]
    for group in groups:
        for row in group:
            if isinstance(row, Mapping):
                values.extend(str(row.get(key) or '') for key in ('name', 'value', 'quote'))
    return values


def _quotes_total(quote: str, total: int) -> bool:
    """A shared amount anchor must quote this total, not merely carry its tag."""
    pattern = (r'(?:[¥￥]\s*|(?:实付款?|实际付款|总金额|付款金额|支付金额|合计)\s*[:：]?\s*'
               r'(?:[¥￥]\s*)?)(\d+)(?:\.(\d{1,2}))?(?![\d.])')
    return any(int(match[1]) * 100 + int((match[2] or '').ljust(2, '0') or '0') == total
               for match in re.finditer(pattern, quote))


def _relation_context(item: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
                      goods: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]] | None:
    relation = item.get('agent_relation') or {}
    match = item.get('material_match') or {}
    recognition = match.get('recognition') or {}
    total = item.get('amount_cents')
    if (item.get('stage') != 'draft' or item.get('verified') or item.get('deleted') or item.get('merged_into') or
            item.get('amount_pending') or not _amount(total) or
            relation.get('relation_kind') not in {'source_inference', 'model_inference', 'user_confirmed'} or
            relation.get('relation_kind') == 'user_confirmed' and not relation.get('confirmation_id') or
            recognition.get('amount_source') != 'vouchers' or recognition.get('amount_cents') != total or
            recognition.get('voucher_total_cents') != total or match.get('voucher_total_cents') != total or
            recognition.get('amount_difference_cents') != 0 or recognition.get('payments')):
        return None
    by_id: dict[str, Mapping[str, Any]] = {}
    for fact in facts:
        ident = fact.get('id')
        if (not isinstance(ident, str) or not ident or ident in by_id or
                not isinstance(fact.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', fact['sha256']) or
                not _clean_source(fact, products=False)):
            return None
        by_id[ident] = fact
    scopes: list[set[str]] = []
    for name in ('vouchers', 'supports'):
        rows = recognition.get(name)
        if not isinstance(rows, list) or not rows:
            return None
        selected: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                return None
            ident = row.get('queue_id')
            if not isinstance(ident, str) or ident not in by_id or ident in selected:
                return None
            fact = by_id[ident]
            if (row.get('key') != 'q:' + ident or row.get('role') != fact.get('role') or
                    row.get('amount_cents') != fact.get('total_cents') or
                    name == 'vouchers' and fact.get('role') != 'invoice' or
                    name == 'supports' and (fact.get('role') not in {'purchase', 'order'} or
                                            fact.get('total_cents') != total)):
                return None
            selected.add(ident)
        scopes.append(selected)
    vouchers, supports = scopes
    if (vouchers & supports or vouchers | supports != set(by_id) or
            sum(by_id[key]['total_cents'] for key in vouchers) != total or
            set(match.get('support_ids') or []) != supports):
        return None
    if len(supports) > 1 and not any(
            isinstance(group, list) and set(group) == supports for group in relation.get('duplicate_views', [])):
        return None
    for good in goods:
        ident = good.get('material_id')
        if ident not in by_id or good.get('sha256') != by_id[ident]['sha256']:
            return None
    evidence = relation.get('relation_evidence')
    if not isinstance(evidence, list):
        return None
    quoted: set[str] = set()
    amount_quoted: set[str] = set()
    total_quoted: set[str] = set()
    for row in evidence:
        if not isinstance(row, Mapping):
            return None
        ident, quote = row.get('material_id'), row.get('quote')
        if (ident not in by_id or not isinstance(quote, str) or not 1 <= len(quote) <= 500 or
                row.get('supports') not in {'goods', 'amount', 'merchant'} or
                not any(_compact(quote) in _compact(value) for value in _quote_sources(by_id[ident]))):
            return None
        quoted.add(ident)
        if row['supports'] == 'amount':
            amount_quoted.add(ident)
            if _quotes_total(quote, total):
                total_quoted.add(ident)
    # An already established duplicate view does not need a model to choose
    # the same quote label twice.  All originals still have valid individual
    # citations, source bindings and matching totals; only a real paid-total
    # quote from this exact group can supply its amount anchor.
    if (len(supports) > 1 and supports & total_quoted and all(
            type(by_id[key].get('revision')) is int and by_id[key]['revision'] >= 0 for key in supports)):
        amount_quoted |= supports
    # An independent order still needs its own amount citation. Neither an
    # invoice quote nor an unrelated group's quote grants that permission.
    if quoted != set(by_id) or not supports <= amount_quoted:
        return None
    return vouchers, supports


_PRODUCT_AREA = re.compile(r'^(?:商品名称|商品说明|购买内容|商品明细|购买明细|商品清单|商品列表|'
                           r'商品信息|项目明细|货物或应税劳务名称|项目名称)(?:\s*[:：]|\s*$)|^[*＊]', re.M)


def _summary_order(fact: Mapping[str, Any], goods: Sequence[Mapping[str, Any]]) -> str:
    """Only a shop/count/price summary, never an unparsed purchase section."""
    if fact.get('role') != 'order' or fact.get('goods') or any(
            good.get('material_id') == fact['id'] for good in goods):
        return ''
    text = str(fact.get('source_text') or '')
    if _PRODUCT_AREA.search(text):
        return ''
    rows = [row.strip() for row in text.splitlines() if row.strip()]
    anchors = [index for index, row in enumerate(rows) if re.match(r'^(?:淘宝)?闪购', _compact(row))]
    if len(anchors) != 1:
        return ''
    start = anchors[0]
    end = next((index for index in range(start + 1, len(rows)) if
                re.match(r'^(?:价格明细|实际付款|实付款?|总优惠)', _compact(rows[index]))), None)
    if end is None or end == start + 1:
        return ''
    if not all(re.fullmatch(r'共\s*\d+\s*(?:件|份|组|杯)', row) for row in rows[start + 1:end]):
        return ''
    if not re.fullmatch(r'(?:(?:价格明细|总优惠|实际付款|实付款?|总价)|[\d.¥￥元\s：:])+', rows[end]):
        return ''
    for row in rows[end + 1:]:
        compact = _compact(row)
        if not (re.fullmatch(r'发票[:：]?.*公司[>›]?', compact) or
                re.fullmatch(r'订单(?:号|编号)[:：]?(?:\d+|\[编号省略\])(?:[|｜]?复制)?', compact) or
                compact == '订单信息' or
                re.fullmatch(r'安心权益\d+项服务保障中查看全部[>›]?', compact) or
                compact == '食无忧去申请>号码保护'):
            return ''
    return '\n'.join(rows[start:end + 1])


def _delivery_invoice(fact: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = fact.get('goods') or []
    if not _clean_source(fact) or fact.get('role') != 'invoice' or len(rows) != 1:
        return None
    row = rows[0]
    if (not isinstance(row, Mapping) or row.get('method') != 'native_geometry' or
            row.get('is_discount') or
            _compact(str(row.get('name') or '')) != '物流网络配送费' or
            _compact(str(row.get('raw_name') or '')) != '*生产生活服务*物流网络配送费' or
            not _amount(row.get('amount_cents')) or type(row.get('tax_cents')) is not int or
            row['tax_cents'] < 0 or row['amount_cents'] + row['tax_cents'] != fact['total_cents']):
        return None
    text = str(fact.get('source_text') or '')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    taxes = [index for index, line in enumerate(lines) if re.match(r'^[*＊]', line)]
    if len(taxes) != 1:
        return None
    start = taxes[0]
    if not re.fullmatch(r'[*＊]生产生活服务[*＊]物流网络(?:\s+(?:次|[\d.%+\-]+))*', lines[start]):
        return None
    end = next((index for index in range(start + 1, len(lines)) if _compact(lines[index]) == '配送费'), None)
    if end is None or any(not re.fullmatch(r'次|无|[\d.%+\-\s]+', line)
                          for line in lines[start + 1:end]):
        return None
    if _compact(str(row.get('quote') or '')) != _compact('\n'.join(lines[start:end + 1])):
        return None
    # The positioned title crosses numeric table columns. Prove that exact
    # original span here, without replacing source_text with an invented row.
    for index, line in enumerate(lines):
        if index < start and _PRODUCT_AREA.search(line) and _compact(line) not in {
                '项目名称', '货物或应税劳务名称'}:
            return None
        if index <= end:
            continue
        compact = _compact(line)
        if not (compact in {'合', '计', '合计', '价税合计（大写）', '价税合计(大写)',
                            '（小写）', '(小写)', '备', '注', '备注'} or
                re.fullmatch(r'[¥￥]?[\d.]+', compact) or
                re.fullmatch(r'[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]+', compact) or
                index == len(lines) - 1 and re.fullmatch(r'下载次数[:：]\d+', compact)):
            return None
    return {'kind': 'supplementary_delivery_invoice', 'material_id': fact['id'], 'sha256': fact['sha256'],
            'quote': row['quote'], 'amount_cents': fact['total_cents'],
            'reason': '已关联的完整独立网络配送税票；金额计入本笔，商品分类由具体商品发票提供'}


def _complete_simple_source(fact: Mapping[str, Any], product_quote: str) -> str:
    """Check every line of a simple explicit order/invoice, including its tail.

    This is intentionally separate from the summary and native-table readers.
    It returns an actual total quote, never a model-generated amount citation.
    """
    if not _clean_source(fact):
        return ''
    product_rows = [row.strip() for row in product_quote.splitlines() if row.strip()]
    if len(product_rows) != 1:
        return ''
    seen_product = 0
    totals: list[str] = []
    lines = iter(str(fact['source_text']).splitlines())
    for raw in lines:
        row = raw.strip()
        compact = _compact(row)
        if not row:
            continue
        if row == product_rows[0]:
            seen_product += 1
            continue
        if fact['role'] == 'invoice' and compact == '价税合计':
            # Native PDF geometry can put the literal small-total label and
            # its amount on the next line. Only that exact adjacent pair is
            # one metadata field; keep the original lines in its citation.
            following = next(lines, '').strip()
            if not re.fullmatch(r'\(小写\)[¥￥]?(\d+)(?:\.(\d{1,2}))?', _compact(following)):
                return ''
            row += '\n' + following
            compact = _compact(row)
        label = r'(?:实付(?:款)?|实际付款)' if fact['role'] == 'order' else r'价税合计\(小写\)'
        total = re.fullmatch(label + r'[:：]?[¥￥]?(\d+)(?:\.(\d{1,2}))?', compact)
        if total:
            cents = int(total[1]) * 100 + int((total[2] or '').ljust(2, '0') or '0')
            if cents != fact['total_cents']:
                return ''
            totals.append(row)
            continue
        if compact in {'电子发票(普通发票)', '订单详情', '收据', '订单已送达'}:
            continue
        if fact['role'] == 'invoice' and re.fullmatch(
                r'开票日期[:：]\d{4}年(?:0?[1-9]|1[0-2])月(?:0?[1-9]|[12]\d|3[01])日', compact):
            continue
        if re.fullmatch(r'共\d+(?:件|份|组|杯)', compact):
            continue
        if re.fullmatch(r'(?:商家名称|销售方名称|销售方)[:：]\S+', compact):
            continue
        # Existing local preparation/trace identifiers carry no product text.
        # Only an entirely numeric or already-redacted identifier is allowed.
        if re.fullmatch(r'(?:发票号码|发票代码|订单号|订单编号|商家订单号|交易号|'
                        r'测试材料|材料标识|材料编号)[:：](?:\d+|\[编号省略\])', compact):
            continue
        return ''
    return totals[0] if seen_product == 1 and totals else ''


def _concrete_food_split(item: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
                         goods: Sequence[Mapping[str, Any]],
                         categories: Mapping[str, str]) -> dict[str, Any] | None:
    """Keep a concrete order as goods while admitting its one delivery tax bill."""
    orders = [fact for fact in facts if fact.get('role') == 'order']
    invoices = [fact for fact in facts if fact.get('role') == 'invoice']
    if len(facts) != 3 or len(orders) != 1 or len(invoices) != 2 or any(
            not _clean_source(fact) or type(fact.get('revision')) is not int or fact['revision'] < 0
            for fact in facts):
        return None
    order = orders[0]
    product_invoices = [fact for fact in invoices if any(g.get('material_id') == fact['id'] for g in goods)]
    if len(product_invoices) != 1:
        return None
    product = product_invoices[0]
    delivery = next(fact for fact in invoices if fact['id'] != product['id'])
    if any(g.get('material_id') not in {order['id'], product['id']} for g in goods):
        return None
    order_goods = [good for good in goods if good.get('material_id') == order['id']]
    product_goods = [good for good in goods if good.get('material_id') == product['id']]
    order_check = inspect_product_rules(order_goods, [order], categories)
    product_check = inspect_product_rules(product_goods, [product], categories)
    if (not order_goods or not product_goods or order_check['status'] != 'exact' or
            product_check['status'] != 'exact' or not order_check['coverage_complete'] or
            not product_check['coverage_complete'] or order_check['category'] not in {'餐饮', '外卖'} or
            order_check['category'] != product_check['category'] or
            len(order_check['products']) != 1 or len(product_check['products']) != 1 or
            _compact(order_check['products'][0]['name']) != _compact(product_check['products'][0]['name'])):
        return None
    quotes: list[str] = []
    for fact, name in ((order, order_check['products'][0]['name']),
                       (product, product_check['products'][0]['name']), (delivery, '物流网络配送费')):
        rows = fact.get('goods') or []
        if (len(rows) != 1 or not isinstance(rows[0], Mapping) or rows[0].get('method') != 'native_text' or
                _compact(str(rows[0].get('name') or '')) != _compact(name)):
            return None
        quote = str(rows[0].get('quote') or '')
        if fact['role'] == 'invoice' and not re.fullmatch(
                r'[*＊][^*＊\r\n]+[*＊]' + re.escape(str(name)), quote.strip()):
            return None
        if fact is delivery and _compact(quote) not in {
                '*服务*物流网络配送费', '*生产生活服务*物流网络配送费'}:
            return None
        total_quote = _complete_simple_source(fact, quote)
        if not total_quote:
            return None
        quotes.append(total_quote)
    # Supplying a source-checked local amount citation is specific to this
    # fully checked concrete split. Existing generic/summary requirements are
    # unchanged, and no original relation evidence or human field is modified.
    inspected_item = deepcopy(dict(item))
    relation = inspected_item.get('agent_relation')
    if not isinstance(relation, dict) or not isinstance(relation.get('relation_evidence'), list):
        return None
    relation['relation_evidence'].append({'material_id': order['id'], 'quote': quotes[0], 'supports': 'amount'})
    context = _relation_context(inspected_item, facts, goods)
    if context is None or context != ({product['id'], delivery['id']}, {order['id']}):
        return None
    return {'sources': deepcopy([order, product]), 'context_category': '外卖', 'supplementary': [
        {'kind': 'supplementary_delivery_invoice', 'material_id': delivery['id'], 'sha256': delivery['sha256'],
         'quote': delivery['goods'][0]['quote'], 'amount_cents': delivery['total_cents'],
         'reason': '已关联的具体食品订单与商品、配送分票金额守恒；配送票原文完整且只列网络配送费'},
        {'kind': 'local_concrete_order_amount', 'material_id': order['id'], 'sha256': order['sha256'],
         'quote': quotes[0], 'amount_cents': order['total_cents'],
         'reason': '从同一完整具体商品订单读取实付原文，补充可复核金额引用；订单仍保留为商品来源'}]}


def supporting_product_sources(item: Mapping[str, Any], facts: Sequence[Mapping[str, Any]],
                               goods: Sequence[Mapping[str, Any]],
                               categories: Mapping[str, str], *,
                               source_entries: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Separate two strictly proven auxiliary roles; uncertain sources stay in.

    This never creates a relation, changes an amount, grants a human fact,
    verifies an expense or mutates any caller record.
    """
    unchanged = {'sources': deepcopy(list(facts)), 'supplementary': []}
    transport = _transport_support(item, facts, goods, categories, source_entries)
    if transport is not None:
        return transport
    concrete = _concrete_food_split(item, facts, goods, categories)
    if concrete is not None:
        return concrete
    context = _relation_context(item, facts, goods)
    if context is None:
        return unchanged
    vouchers, supports = context
    by_id = {str(fact['id']): fact for fact in facts}
    if len(vouchers) == 1:
        voucher = by_id[next(iter(vouchers))]
        proof = inspect_supplementary_invoice(voucher)
        purchase_facts = [fact for fact in facts if fact['id'] in supports]
        purchase_goods = [good for good in goods if good.get('material_id') in supports]
        checked = inspect_product_rules(purchase_goods, purchase_facts, categories)
        if (proof and voucher['total_cents'] == item['amount_cents'] and purchase_goods and
                len(purchase_goods) == len(goods) and
                all(categories.get(str(good.get('name') or '')) == '材料采购' for good in purchase_goods) and
                checked.get('coverage_complete') and checked['status'] in {'exact', 'model'} and
                '材料采购' not in checked.get('forbidden_categories', [])):
            return {'sources': deepcopy(purchase_facts), 'supplementary': [proof]}
    if len(vouchers) != 2 or len(supports) != 1:
        return unchanged
    order = by_id[next(iter(supports))]
    order_quote = _summary_order(order, goods)
    if not order_quote:
        return unchanged
    deliveries = [(key, _delivery_invoice(by_id[key])) for key in vouchers]
    delivery = [(key, proof) for key, proof in deliveries if proof]
    if len(delivery) != 1:
        return unchanged
    delivery_id, delivery_proof = delivery[0]
    product = by_id[next(iter(vouchers - {delivery_id}))]
    product_goods = [good for good in goods if good.get('material_id') == product['id']]
    checked = inspect_product_rules(product_goods, [product], categories)
    if (not _clean_source(product) or checked['status'] != 'exact' or not checked['coverage_complete'] or
            checked.get('category') not in {'餐饮', '外卖'} or not product_goods or
            any(good.get('material_id') != product['id'] for good in goods)):
        return unchanged
    audit = {'kind': 'supplementary_order_summary', 'material_id': order['id'], 'sha256': order['sha256'],
             'quote': order_quote, 'amount_cents': order['total_cents'],
             'reason': '已关联的完整订单仅展示店铺、件数与实付；两份发票合计覆盖实付，具体商品由商品发票提供'}
    return {'sources': deepcopy([product]), 'context_category': '外卖',
            'supplementary': [audit, delivery_proof]}
