"""Grounded goods, existing category decisions and concrete report names for Agent drafts."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from typing import Any

from invoice_print_layout import expense_classifier as classifier
from invoice_print_layout.agent_expense_naming import concise_expense_name
from invoice_print_layout.agent_product_rules import (PRODUCT_RULES_VERSION, inspect_product_rules,
                                                     native_wrapped_product)
from invoice_print_layout.agent_jobs import Jobs, ModelBudgetExceeded, digest, encoded
from invoice_print_layout.glm_invoice_ai import GlmAIError
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.report_types import infer_report_choice, validate_report_choice
from invoice_print_layout.workbench import now

GOODS_RULES_VERSION = 'goods-v9-native-ride-pairs'
PLACEHOLDERS = {'', '待核对购买凭证', '待核对费用', '日用百货', '超市服务', '商品', '商品不明', '其他'}
EXTRA_GOODS = ('鼠标垫', '鼠标', '键盘', '显示器', '笔记本电脑', '台式电脑', '电脑', '打印机', '测试耗材',
               '办公耗材', '工具', '雨衣', '矿泉水', '饮用水', '纯净水', '奶茶', '茶饮料', '盒饭')
STOP = re.compile(r'^(?:价格明细|收货|收件|订单号|订单编号|发票|查发票|支付|付款|实付|总价|总优惠|'
                  r'优惠|配送费|打包费|包装费|运费|商品金额|合计|更多|售后)')
DECORATION = re.compile(r'加入购物车|\d+天.*(?:退|换)|^[xX×]\s*\d+$|^已免运费$|'
                        r'^共\s*\d+\s*(?:件|个|只|份|组|瓶|盒|杯)$')
PRICE = re.compile(r'(?:￥|¥)\s*\d+(?:\.\d+)?')
ACCESSORIES = ('鼠标垫', '鼠标', '键盘', '显示器', '打印机')
ANCILLARY = {'派送服务', '物流网络配送费', '配送费', '打包费', '包装费'}
CONTENT_CATEGORIES = {
    **{word: classifier.CATEGORY[key] for key, words in classifier.RULES.items() for word in words},
    **{word: '材料采购' for word in EXTRA_GOODS if word not in
       {'矿泉水', '饮用水', '纯净水', '奶茶', '茶饮料', '盒饭'}},
    **{word: '餐饮' for word in ('矿泉水', '饮用水', '纯净水', '奶茶', '茶饮料', '咖啡饮品')},
    **{word: '餐饮' for word in ('早餐', '午餐', '晚餐', '餐食')},
    '铁路退票费': '高铁', '打印制作费': '其他',
}


def _compact(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def _metadata_only_goods(value: str) -> bool:
    """A standalone price field is not a purchased item; model numbers are."""
    plain = _compact(value)
    label = (r'(?:商品金额|金额|付款金额|支付金额|实付金额|应付金额|总金额|总价|单价|'
             r'价格|售价|原价|现价|参考价|成交价|实付|应付|合计|总计|税额|税率)')
    return bool(re.fullmatch(label + r'[:：]?', plain) or re.fullmatch(
        r'(?:(?:人民币|CNY|RMB|[¥￥])\d+(?:\.\d+)?(?:元)?|\d+(?:\.\d+)?元)', plain, re.I) or re.fullmatch(
        label + r'[:：]?(?:人民币|[¥￥])?[+-]?\d+(?:\.\d+)?(?:元|%|RMB|CNY)?'
        r'(?:\((?:含税|已含税)\))?', plain, re.I))


def _product_parts(value: str) -> list[str]:
    """Keep coordinated products distinct without splitting device compatibility."""
    parts = [part.strip() for part in re.split(r'[、,，;；+＋]+', value) if part.strip()]
    result: list[str] = []
    for part in parts:
        coordinated = re.split(r'以及|及|和|与', part)
        # A mouse compatible with two computers still describes one purchase.
        if (any(noun in part for noun in ACCESSORIES) and
                all(re.search(r'电脑|笔记本|台式|Mac|PC', piece, re.I) for piece in coordinated) and
                re.search(r'(?:适用(?:于)?|兼容|支持|连接|USB|蓝牙|有线|无线).*'
                          r'(?:电脑|笔记本|台式)', part, re.I)):
            result.append(part)
            continue
        # Boundary conjunctions and short pieces may belong to a product name
        # (for example 和田玉); retain that original rather than invent items.
        if len(coordinated) > 1 and all(len(_compact(piece)) >= 2 for piece in coordinated):
            result.extend(piece.strip() for piece in coordinated)
        else:
            result.append(part)
    return result


def _invoice_items(rows: list[str]) -> list[tuple[str, str]]:
    """Native PDF columns may put a short item suffix after numeric columns."""
    result = []
    for index, row in enumerate(rows):
        if not row.startswith(('*', '＊')):
            continue
        value = re.sub(r'^[*＊][^*＊]+[*＊]', '', row)
        quotes = [row]
        for following in rows[index + 1:index + 13]:
            if (following.startswith(('*', '＊')) or classifier.PRIVATE_LABEL.search(following) or
                    re.match(r'^(?:合|计|价税|备|注|下载|发票|开票|出行人|有效身份证|出行日期)', following)):
                break
            if re.fullmatch(r'[\d\W]+|(?:无|次|个|件|只|份|组|瓶|盒|单位|数量|单价|金额|税额)', following):
                continue
            # Long rows are another table field, not a wrapped title suffix.
            if re.fullmatch(r'[\u4e00-\u9fff]{1,6}', following):
                value += following
                quotes.append(following)
            break
        result.append((value, '\n'.join(quotes)))
    return result


def _purchase_items(rows: list[str], start: int) -> list[tuple[str, str]]:
    """Keep adjacent title lines together; separate independently priced items."""
    result: list[tuple[str, str]] = []
    block: list[str] = []
    nouns = (*EXTRA_GOODS, *(word for words in classifier.RULES.values() for word in words))
    def flush() -> None:
        if block:
            quote = '\n'.join(block)
            title = ''.join(part for part in block if not re.fullmatch(
                r'\d+(?:\.\d+)?\s*(?:件|个|只|份|组|瓶|盒|杯|包|卷|块)', part))
            result.append((title, quote))
            block.clear()
    for row in rows[start:]:
        if STOP.search(row):
            break
        count = re.fullmatch(r'[xX×]\s*\d+(?:\.\d+)?', row)
        if count:
            flush()
            if result and not re.search(r'(?:^|\n)[xX×]\s*\d+(?:\.\d+)?$', result[-1][1]):
                title, quote = result[-1]
                result[-1] = (title, quote + '\n' + row)
            continue
        if DECORATION.search(row) or classifier.PRIVATE_LABEL.search(row):
            flush()
            continue
        if block and PRICE.search(row) and any(PRICE.search(part) for part in block):
            existing = {word for word in nouns if word in ''.join(block)}
            descriptive = bool(re.search(r'[\u4e00-\u9fff]{2,}', PRICE.sub('', row)))
            price_label = bool(re.match(r'^(?:原价|现价|折扣价|售价|券后|活动价|参考价)', row))
            separate = existing and (row.startswith(nouns) or descriptive and not price_label) and not any(
                word in row for word in existing)
            # Shopping titles often show an inline discounted price before a
            # wrapped continuation carrying the list price.
            # A split word proves continuation even when an OCR thumbnail
            # repeats the short noun between title lines and prices differ.
            word_wrap = (PRICE.sub('', row).lstrip().startswith('携式') and any(
                re.search(r'雨衣便(?:\s*实付)?\s*$', PRICE.sub('', part)) and
                all(_compact(following) == '雨衣' for following in block[index + 1:])
                for index, part in enumerate(block)))
            if not word_wrap and (separate or not any('实付' in part for part in block)):
                flush()
        block.append(row)
    flush()
    return result


def _compatible_computer(value: str, start: int) -> bool:
    """A target device in an accessory title is not an extra purchased device."""
    if not any(word in value for word in ACCESSORIES):
        return False
    prefix = value[:start]
    return bool(re.search(r'(?:适用(?:于)?|兼容|支持|连接)[^、，,;；]{0,16}$', prefix) or
                re.search(r'(?:USB|蓝牙|有线|无线)(?:(?:笔记本|台式)){0,2}$', prefix, re.I))


def _service_name(value: str, text: str) -> str:
    """Normalize explicit service wording, with a carrier/transport identity check."""
    if len(_product_parts(value)) > 1:
        return ''
    if '住宿服务' in value:
        return '住宿费'
    if re.search(r'打印制作|打印服务|复印服务|印刷服务|打印费|复印费|印刷费', value) and not re.search(r'打印机|复印机', value):
        return '打印制作费'
    if re.search(r'收派服务|快递服务', value) and '顺丰' in text:
        return '顺丰快递费'
    if re.search(r'客运服|出租汽车客运|网约车客运', value) and re.search(r'出租车|出租汽车|网约车', text):
        return '出租汽车客运'
    return ''


def _content_categories(goods: list[dict[str, str]]) -> set[str | None]:
    # These are the canonical names saved by extraction, not a new parse of
    # originals. Unknown neighbouring products therefore remain unknown.
    return {CONTENT_CATEGORIES.get(row['name']) for row in goods}


def _known_content_decision(goods: list[dict[str, str]]) -> dict[str, Any] | None:
    """Extend existing exact-content rules only for explicit, normalized items."""
    categories = _content_categories(goods)
    if None in categories or len(categories) != 1:
        return None
    return {'category': next(iter(categories)), 'source': 'rules',
            'reason': '原件明确商品或服务：' + '、'.join(row['name'] for row in goods)}


def goods_from_text(text: str, fields: dict[str, Any]) -> list[dict[str, str]]:
    """Keep only labelled goods or a bounded purchase section, excluding merchant/address/payment."""
    rows = [r.strip() for r in text.splitlines() if r.strip()]
    candidates: list[tuple[str, str]] = _invoice_items(rows)
    for row in rows:
        labelled = re.match(r'^(?:商品名称|商品说明|购买内容|货物或应税劳务名称|商品明细|购买明细)\s*[:：]\s*(.+)', row)
        if labelled:
            candidates.append((labelled[1], row))
    raw_goods = str(fields.get('goods') or '').strip()
    compact = _compact(text)
    merchant = _compact(str(fields.get('shop_name') or fields.get('merchant') or ''))
    if raw_goods and _compact(raw_goods) in compact and _compact(raw_goods) != merchant:
        candidates.append((raw_goods, raw_goods))
    # Special tickets have no *tax category* item row. Only their explicit type
    # establishes the service; a merchant, filename or previous category cannot.
    if '铁路电子客票' in compact and '电子发票' in compact:
        label = '铁路退票费' if '退票费' in compact else '高铁车票'
        quote = next(row for row in rows if '铁路电子客票' in row)
        if label == '铁路退票费':
            quote = next(row for row in rows if '退票费' in row) + '\n' + quote
        candidates.append((label, quote))
    if re.search(r'(?:滴滴出行|曹操出行|享道出行|网约车|出租车|打车).{0,6}行程单', compact):
        candidates.append(('网约车客运', next(row for row in rows if '行程单' in row)))
    if '顺丰电子发票' in compact and '运单明细' in compact:
        candidates.append(('顺丰快递费', next(row for row in rows if '顺丰电子发票' in row)))
    # A recognized transport document is not a merchant shopping section.
    if not candidates:
        # An explicit product section outranks a preceding merchant/header.
        # Metadata between the shop and the section is not a product boundary.
        start = next((i + 1 for i, row in enumerate(rows) if row.rstrip(':：') in
                      {'商品明细', '购买明细', '商品清单', '商品列表', '商品信息'}), None)
        if start is None:
            start = next((i + 1 for i, row in enumerate(rows) if merchant and merchant in _compact(row)
                          and not classifier.PRIVATE_LABEL.search(row)), None)
        if start is not None:
            candidates.extend(_purchase_items(rows, start))
    nouns = sorted({*EXTRA_GOODS, *(w for words in classifier.RULES.values() for w in words)}, key=len, reverse=True)
    result: list[dict[str, str]] = []
    # Each explicitly separated product is resolved independently. A familiar
    # noun in one part must not make an unfamiliar neighbouring product vanish.
    separated = [(part, quote) for value, quote in candidates for part in _product_parts(value)]
    for value, quote in separated:
        if _metadata_only_goods(value):
            continue
        value = classifier.clean(value)
        value = _compact(PRICE.sub('', value))
        if (not value or STOP.search(value) or DECORATION.search(value) or
                _compact(value) == merchant or _compact(value) in PLACEHOLDERS):
            continue
        if re.search(r'忽略.{0,12}(规则|指令)|system\s*prompt|输出.{0,12}(类别|分类)|不要.{0,10}(分类|判断)', value, re.I):
            result.append({'name': value[:180], 'quote': quote[:500]})
            continue
        service = _service_name(value, compact)
        hits: list[str] = [service] if service else []
        if not service:
            spans: list[tuple[int, int]] = []
            for noun in nouns:
                for match in re.finditer(re.escape(noun), value):
                    if any(start <= match.start() and match.end() <= end for start, end in spans):
                        continue
                    spans.append(match.span())
                    if noun in {'电脑', '笔记本电脑', '台式电脑'} and _compatible_computer(value, match.start()):
                        continue
                    if noun not in hits:
                        hits.append(noun)
        if not hits:
            # A price, quantity or wrapping continuation is not a second product.
            if re.fullmatch(r'[\d\W]+|[xX×]\s*\d+|.*(?:商务办公|加入购物车|B\d+-\d+-\d+).*', value):
                continue
            hits = [value[:80]] if len(value) >= 2 and value not in PLACEHOLDERS else []
        for name in hits:
            if name not in {g['name'] for g in result}:
                record = {'name': name, 'quote': quote[:500]}
                quantity = re.search(r'(?:^|\n)(\d+(?:\.\d+)?)\s*(件|个|只|份|组|瓶|盒|杯|包|卷|块)$', quote)
                if quantity:
                    record.update(quantity=quantity[1], unit=quantity[2])
                else:
                    count = re.search(r'(?:^|\n)([xX×]\s*(\d+(?:\.\d+)?))$', quote)
                    if count:
                        record.update(quantity=count[2], quantity_quote=count[1])
                result.append(record)
    # Preserve one overflow sentinel. The shared fact adapter exposes at most
    # 20 rows and records the overflow instead of treating a clipped basket as
    # complete goods evidence.
    return result[:21]


class ContentTools:
    def __init__(self, jobs: Jobs, intake: IntakeQueue):
        self.jobs, self.store, self.intake = jobs, jobs.store, intake

    def _load(self, job: dict[str, Any], ident: Any) -> tuple[str, dict[str, Any], list[dict[str, Any]], str]:
        if not isinstance(ident, str):
            raise ValueError('费用编号无效')
        with self.store.connect() as db:
            effects = [json.loads(r['data']) for r in db.execute(
                'SELECT data FROM invoice_agent_effects WHERE job_id=?', (job['id'],))]
            if ident not in {r.get('item_id') for r in effects}:
                raise ValueError('工具只能处理当前任务创建的费用')
            row = db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise ValueError('费用不存在')
            raw = str(row['data'])
            item = json.loads(raw)
            attachments = [dict(r) for r in db.execute(
                'SELECT path,role,digest FROM attachments WHERE item_id=? ORDER BY id', (ident,))]
        if item.get('project_id') != job['project_id'] or item.get('stage') != 'draft' or item.get('verified'):
            raise ValueError('费用已变更或核对，不能继续自动修改')
        sources = [self.intake.get(key) for key in job['material_ids']]
        sources = [e for e in sources if e.get('item_id') == ident]
        if not sources or {e['digest'] for e in sources} != {a['digest'] for a in attachments}:
            raise ValueError('费用原件关联已变化')
        for attachment in attachments:
            if hashlib.sha256(Path(attachment['path']).read_bytes()).hexdigest() != attachment['digest']:
                raise ValueError('费用原件已经变化')
        binding = self._source_binding(attachments, sources)
        state = item.get('agent_content') or {}
        created: dict[str, Any] = next((e for e in effects if e.get('item_id') == ident and e.get('content_base')), {})
        if not state and created and created['content_base'] != self._base(item):
            raise ValueError('费用已人工修改，请在人工工作台继续')
        if state and (state.get('job_id') != job['id'] or state.get('binding') != binding or
                      state.get('base') != self._base(item)):
            raise ValueError('费用或材料已人工修改，请在人工工作台继续')
        return raw, item, sources, binding

    def _source_binding(self, attachments: list[dict[str, Any]], sources: list[dict[str, Any]], *,
                        db: sqlite3.Connection | None = None) -> str:
        """Bind actual sources and category context, including an author's transaction."""
        with self.store.connect() if db is None else nullcontext(db) as connection:
            tables = {r['name'] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            preferences = [{k: v for k, v in json.loads(r['data']).items() if k != 'last_used'} for r in
                           connection.execute('SELECT data FROM expense_preferences ORDER BY id')] if 'expense_preferences' in tables else []
            experiences = [list(r) for r in connection.execute('SELECT * FROM category_experience ORDER BY evidence_key')] if 'category_experience' in tables else []
            disabled = [list(r) for r in connection.execute('SELECT * FROM category_experience_disabled ORDER BY evidence_key')] if 'category_experience_disabled' in tables else []
        return digest([attachments, [{k: e.get(k) for k in
            ('id', 'digest', 'revision', 'role', 'fields', 'text', 'manual_fields', 'confirmed_content')} for e in sources],
            preferences, experiences, disabled])

    @staticmethod
    def _base(item: dict[str, Any]) -> str:
        return digest({k: v for k, v in item.items() if k not in {'agent_content', 'updated_at'}})

    def _save(self, job: dict[str, Any], raw: str, item: dict[str, Any], state: dict[str, Any],
              binding: str, updates: dict[str, Any] | None = None) -> None:
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            live = json.loads(db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (job['id'],)).fetchone()['data'])
            if (live['state'] != 'running' or
                    live['elapsed'] + time.time() - live['run_started'] >= live['limits']['seconds']):
                raise ValueError('任务已停止或超时')
            current, _, _, current_binding = self._load(job, item['id'])
            if current != raw or current_binding != binding:
                raise ValueError('判断期间费用或原件已变化')
            item.update(updates or {})
            state.update(job_id=job['id'], binding=binding, base=self._base(item))
            item.update(agent_content=state, updated_at=now())
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), item['id']))

    def next_action(self, job: dict[str, Any], ident: str) -> dict[str, Any] | None:
        _, item, _, binding = self._load(job, ident)
        state = item.get('agent_content') or {}
        tool = {'extracted': 'classify_expense', 'classified': 'name_expense'}.get(str(state.get('status')), 'extract_goods')
        if state.get('status') == 'ready':
            return None
        if state.get('status') == 'needs_input':
            if self._rule_resume_authorized(job, item, binding):
                return {'tool': 'classify_expense', 'arguments': {'item_id': ident}}
            return {'tool': 'ask_user', 'arguments': {}}
        return {'tool': tool, 'arguments': {'item_id': ident}}

    def _inspect_products(self, state: dict[str, Any], sources: list[dict[str, Any]],
                          item: dict[str, Any] | None = None) -> dict[str, Any]:
        from invoice_print_layout.agent_evidence import material_facts
        facts = [material_facts(entry, cache_root=self.store.root) for entry in sources]
        from invoice_print_layout.agent_confirmed_facts import verified_content
        confirmed = verified_content(item or {}, sources)
        if confirmed:
            return self._confirmed_product_check(confirmed, facts, sources)
        if state.get('confirmed_content'):
            return {'status': 'needs_input', 'category': None, 'coverage_complete': False,
                    'goods_text': '', 'products': [], 'forbidden_categories': [],
                    'reason': '已确认商品事实与当前完整原件集合不一致，请在人工工作台核对',
                    'rules_version': PRODUCT_RULES_VERSION}
        completed = {row['material_id'] for row in state.get('extractions', []) if
                     row.get('required') and row.get('complete') and row.get('source') in {'glm', 'cache'}}
        for fact in facts:
            # A source-grounded completion can fill a local parser gap. It
            # cannot cure truncated text, source errors or missing originals;
            # the product checker still independently covers the full text.
            if (fact['id'] in completed and fact.get('complete') is not False and not fact.get('issues') and
                    not fact.get('goods_issues') and
                    not fact.get('source_text_truncated') and not fact.get('goods_text_truncated')):
                fact['goods_complete'] = True
        categories = dict(CONTENT_CATEGORIES)
        # Exact service aliases remain source-qualified, rather than granting
        # an entire non-material category an exemption from product coverage.
        aliases = {'住宿服务', '打印制作', '打印服务', '复印服务', '印刷服务',
                   '收派服务', '收派服务费', '快递服务', '客运服务', '出租汽车客运'}
        for full in aliases:
            relevant = [fact for fact in facts if fact.get('role') != 'payment' and
                        (full in _compact(str(fact.get('source_text') or '')) or any(
                            native_wrapped_product(row, str(fact.get('source_text') or '')) == full
                            for row in fact.get('goods', [])))]
            qualified: list[str | None] = []
            for fact in relevant:
                text = _compact(str(fact.get('source_text') or ''))
                service = _service_name(full, text)
                qualified.append(CONTENT_CATEGORIES.get(service) if service and any(
                    g.get('name') == service or _service_name(str(g.get('name') or ''), text) == service
                    for g in fact.get('goods', [])) else None)
            # A carrier identity on one attachment cannot qualify another.
            if qualified and None not in qualified and len(set(qualified)) == 1:
                categories[full] = str(qualified[0])
        checked_goods = [dict(good) for good in state.get('goods', [])]
        for good in checked_goods:
            fact = next((f for f in facts if f['id'] == good.get('material_id')), {})
            text = _compact(str(fact.get('source_text') or ''))
            quote = _compact(str(good.get('quote') or ''))
            candidates = [full for full in aliases if full in categories and full in quote and
                          _service_name(full, text) == good.get('name')]
            if len(candidates) == 1:
                # Inspection-only association; keep the saved native/raw
                # record intact and require the complete product to match.
                good['raw_name'] = candidates[0]
        from invoice_print_layout.agent_product_support import supporting_product_sources
        support = supporting_product_sources(item or {}, facts, checked_goods, categories, source_entries=sources)
        checked = inspect_product_rules(checked_goods, support['sources'],
                                        {**categories, **support.get('source_categories', {})})
        if support['supplementary'] and checked.get('coverage_complete'):
            checked['supplementary_sources'] = support['supplementary']
            if support.get('context_category') and checked.get('category') in {'餐饮', '外卖'}:
                checked['context_category'] = support['context_category']
                checked['context_reason'] = '完整食品订单与独立配送发票已通过来源和金额校验，按外卖场景分类'
        for product in checked.get('products', []):
            fact = next((f for f in facts if f['id'] == product['material_id']), {})
            quote = self._product_quote(product['name'], str(fact.get('source_text') or ''))
            if quote:
                product['source_quote'] = quote
        if checked['status'] == 'needs_input':
            # A trip is a dedicated ticket, not a second invoice goods table.
            from invoice_print_layout.agent_transport import pair_status
            invoices = [e for e in sources if e.get('role') == 'invoice']
            trips = [e for e in sources if e.get('role') == 'trip']
            if len(sources) == 2 and len(invoices) == len(trips) == 1 and pair_status(invoices[0], trips[0]) == 'supported':
                parts = []
                for fact in facts:
                    goods = [g for g in checked_goods if g['material_id'] == fact['id']]
                    part = inspect_product_rules(goods, [fact], {**categories, '客运服务': '打车', '客运服务费': '打车', '出租汽车客运': '打车'})
                    if part['status'] == 'needs_input':
                        part = self._special_ticket_check({'goods': goods}, [fact]) or part
                    parts.append(part)
                if all(p['status'] == 'exact' and p['category'] == '打车' and p['coverage_complete'] for p in parts):
                    return {**parts[0], 'products': [g for p in parts for g in p['products']],
                            'goods_text': '\n'.join(dict.fromkeys(g['name'] for p in parts for g in p['products'])),
                            'reason': '同一出行服务方的完整发票与行程单，客运商品按同笔费用核对'}
            special = self._special_ticket_check(state, facts)
            if special:
                return special
        return checked

    @staticmethod
    def _confirmed_product_check(confirmed: dict[str, Any], facts: list[dict[str, Any]],
                                 sources: list[dict[str, Any]]) -> dict[str, Any]:
        """A scoped human statement is separate from machine/source extraction."""
        from invoice_print_layout.agent_product_rules import _source_products
        names = confirmed['goods']
        allowed = {_compact(name) for name in names}
        # This small display alias does not authorize a different service or
        # an accessory merely containing the same noun.
        if '打印' in allowed:
            allowed |= {'打印制作费', '打印服务'}
        failure = ''
        for fact in facts:
            if (fact.get('source_text_truncated') or fact.get('goods_text_truncated') or
                    any('原件' in str(issue) for issue in fact.get('issues', []))):
                failure = '原件未能完整读取，已确认商品事实不能替代缺失或截断的原件'
                break
            if fact.get('role') != 'payment':
                native_names = _source_products(fact, str(fact.get('source_text') or ''))
                if any(_compact(name) not in allowed for name in native_names):
                    failure = '当前原件明确列出确认范围以外的商品或服务，请在人工工作台核对'
                    break
        entry = next(source for source in sources if source['digest'] == confirmed['content_digest'])
        products = [{'name': name, 'material_id': entry['id'], 'sha256': entry['digest'],
                     'method': 'user_confirmation', 'source_quote': confirmed['statement'],
                     'fact_id': confirmed['fact_id']} for name in names]
        return {'status': 'needs_input' if failure else 'human_confirmed',
                'category': None if failure else confirmed['category'],
                'coverage_complete': not failure, 'coverage_basis': 'exact_source_set_user_confirmation',
                'goods_text': '\n'.join(names), 'products': products, 'forbidden_categories': [],
                'confirmed_content': confirmed,
                'reason': failure or '复用同一完整原件集合的明确人工商品及分类事实',
                'rules_version': PRODUCT_RULES_VERSION}

    @staticmethod
    def _product_quote(name: str, text: str) -> str:
        """Return an actual short source span, including a wrapped suffix."""
        rows = [row.strip() for row in text.splitlines() if row.strip()]
        candidates = ['\n'.join(rows[start:end]) for start in range(len(rows))
                      for end in range(start + 1, min(len(rows), start + 10) + 1)
                      if _compact(name) in _compact(''.join(rows[start:end]))]
        return min(candidates, key=len) if candidates else ''

    @staticmethod
    def _special_ticket_check(state: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Dedicated tickets have explicit document identities, not a goods table."""
        goods = state.get('goods', [])
        products: list[dict[str, str]] = []
        for fact in facts:
            if fact.get('role') == 'payment':
                continue
            text = str(fact.get('source_text') or '')
            compact = _compact(text)
            if (not text or fact.get('complete') is False or fact.get('issues') or
                    not fact.get('goods_complete') or fact.get('goods_issues') or
                    fact.get('source_text_truncated') or fact.get('goods_text_truncated') or
                    re.search(r'^(?:商品名称|商品说明|购买内容|商品明细|购买明细|商品清单|商品列表|'
                              r'商品信息|项目明细|货物或应税劳务名称|项目名称)(?:\s*[:：]|\s*$)|^[*＊]', text, re.M)):
                return None
            expected: set[str] = set()
            if '铁路电子客票' in compact and '电子发票' in compact:
                expected = {'铁路退票费' if '退票费' in compact else '高铁车票'}
            elif re.search(r'(?:滴滴出行|曹操出行|享道出行|网约车|出租车|打车).{0,6}行程单', compact):
                expected = {'网约车客运', '出租汽车客运'}
            elif '顺丰电子发票' in compact and '运单明细' in compact:
                expected = {'顺丰快递费'}
            current = [g for g in goods if g.get('material_id') == fact['id']]
            if (not expected or not current or any(g.get('name') not in expected or
                    g.get('sha256') != fact['sha256'] for g in current)):
                return None
            products.extend({'name': g['name'], 'material_id': fact['id'], 'sha256': fact['sha256']} for g in current)
        decision = _known_content_decision(goods)
        if not products or not decision or len(products) != len(goods):
            return None
        return {'status': 'exact', 'category': decision['category'], 'reason': '完整专用票据明确服务类型',
                'goods_text': '\n'.join(p['name'] for p in products), 'products': products,
                'coverage_complete': True, 'forbidden_categories': [], 'rules_version': PRODUCT_RULES_VERSION}

    def rule_reassessment_plan(self, job: dict[str, Any], ident: str) -> dict[str, Any]:
        """Prove a new deterministic rule can resolve an unchanged old draft.

        Source identity remains independent of rule revisions. No old approval,
        cached action, model budget or material revision is fabricated here.
        """
        raw, item, sources, binding = self._load(job, ident)
        state = item.get('agent_content') or {}
        if (state.get('status') != 'needs_input' or not state.get('goods') or
                state.get('decision', {}).get('category') or
                state.get('product_rule_revision') == PRODUCT_RULES_VERSION or
                not state.get('extractions') or any(not e.get('complete') for e in state['extractions']) or
                item.get('deleted') or item.get('merged_into') or item.get('amount_pending') or
                not isinstance(item.get('amount_cents'), int) or item['amount_cents'] <= 0):
            raise ValueError('当前商品尚无可用的新规则依据，请保留人工核对')
        protected = set().union(*(self.intake._manual_keys(e) for e in sources))
        if protected & {'category', 'project_type', 'project_name', 'report_section'}:
            raise ValueError('已有人工分类，请在人工工作台继续')
        checked = self._inspect_products(state, sources, item)
        if checked['status'] != 'exact' or not checked.get('category'):
            raise ValueError(checked['reason'])
        return {'item_id': ident, 'binding': binding, 'base': self._base(item),
                'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'rules_revision': PRODUCT_RULES_VERSION,
                'proof': digest(checked), 'material_refs': [
                    {'id': e['id'], 'revision': e['revision'], 'digest': e['digest']}
                    for e in sorted(sources, key=lambda e: e['id'])]}

    def _rule_resume_authorized(self, job: dict[str, Any], item: dict[str, Any], binding: str) -> bool:
        plan = job.get('product_rule_resume') or {}
        state = item.get('agent_content') or {}
        return bool(plan.get('item_id') == item['id'] and plan.get('binding') == binding and
                    plan.get('base') == self._base(item) and plan.get('rules_revision') == PRODUCT_RULES_VERSION and
                    state.get('status') == 'needs_input' and not state.get('decision', {}).get('category') and
                    state.get('product_rule_revision') != PRODUCT_RULES_VERSION)

    @staticmethod
    def _restore_semantic_names(state: dict[str, Any], checked: dict[str, Any]) -> None:
        """Keep a lost food/service qualifier without discarding the old record."""
        if not checked.get('coverage_complete') or not (
                '材料采购' in checked.get('forbidden_categories', []) or checked.get('status') == 'exact'):
            return
        for good in state['goods']:
            products = [p for p in checked['products'] if p['material_id'] == good['material_id'] and
                        p['sha256'] == good['sha256'] and _compact(good['name']) in _compact(p['name'])]
            quoted = [p for p in products if _compact(p['name']) in _compact(good.get('quote', ''))]
            products = quoted or products
            names = {p['name'] for p in products}
            if len(names) == 1:
                full = next(iter(names))
                if full != good['name']:
                    good.setdefault('recognized_name', good['name'])
                    good['name'] = full
                    quote = next((p.get('source_quote') for p in products if p['name'] == full and p.get('source_quote')), '')
                    if quote and _compact(full) not in _compact(good.get('quote', '')):
                        good.setdefault('recognized_quote', good.get('quote', ''))
                        good['quote'] = quote

    def pending(self, job: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
        pending = []
        for ident in ids:
            _, item, sources, _ = self._load(job, ident)
            state = item.get('agent_content') or {}
            if state.get('status') != 'ready':
                pending.append({'item_id': ident, 'reason': state.get('reason') or '购买内容和费用类别尚待确认',
                                'material_ids': [e['id'] for e in sources]})
        return pending

    def summary(self, job: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
        result = []
        for ident in ids:
            _, item, _, _ = self._load(job, ident)
            state = item.get('agent_content') or {}
            result.append({'id': ident, 'goods': '、'.join(g['name'] for g in state.get('goods', [])),
                           'category': item['category'], 'title': item['title'],
                           'relation_kind': (item.get('agent_relation') or {}).get('relation_kind', ''),
                           'amount_cents': item['amount_cents'], 'source': state.get('decision', {}).get('source', '')})
        return result

    def execute(self, name: str, job: dict[str, Any], ident: Any) -> dict[str, Any]:
        raw, item, sources, binding = self._load(job, ident)
        state = item.get('agent_content') or {}
        protected = set().union(*(self.intake._manual_keys(e) for e in sources))
        if state.get('status') == 'ready':
            return {'content_status': 'ready', 'goods': [g['name'] for g in state['goods']],
                    'category': item['category'], 'title': item['title'], 'source_binding': binding}
        if name == 'extract_goods':
            # An authorized payment completion has a real new source binding,
            # but no extraction result for those sources yet.
            if state and state.get('status') != 'pending':
                return {'content_status': state['status'], 'source_binding': binding}
            from invoice_print_layout.agent_evidence import material_facts
            from invoice_print_layout.agent_goods_ai import supplement_goods
            from invoice_print_layout.agent_confirmed_facts import verified_content
            confirmed = verified_content(item, sources)
            if confirmed:
                native_goods = [{**good, 'material_id': entry['id'], 'sha256': entry['digest']}
                                for entry in sources for good in material_facts(
                                    entry, cache_root=self.store.root).get('goods', [])]
                origin = next(entry for entry in sources if entry['digest'] == confirmed['content_digest'])
                confirmed_goods = [{'name': value, 'raw_name': value, 'quote': confirmed['statement'],
                                    'method': 'user_confirmation', 'fact_id': confirmed['fact_id'],
                                    'provenance': confirmed['provenance'],
                                    'material_digests': list(confirmed['material_digests']),
                                    'material_id': origin['id'], 'sha256': origin['digest']}
                                   for value in confirmed['goods']]
                checked = self._inspect_products({'goods': confirmed_goods}, sources, item)
                state = {'status': 'extracted' if checked['status'] == 'human_confirmed' else 'needs_input',
                         'goods': confirmed_goods, 'native_goods': native_goods, 'extractions': [],
                         'confirmed_content': confirmed, 'reason': '' if checked['status'] == 'human_confirmed'
                         else checked['reason']}
                self._save(job, raw, item, state, binding)
                return {'content_status': state['status'], 'goods': confirmed['goods'],
                        'source_binding': binding, 'reason': state['reason']}
            goods: list[dict[str, Any]] = []
            unresolved: list[str] = []
            extractions: list[dict[str, Any]] = []
            for entry in sources:
                text = str(entry.get('text') or '')
                if (entry['role'] not in {'receipt', 'order', 'purchase', 'invoice', 'detail', 'trip'} and
                        not ('铁路电子客票' in _compact(text) and '电子发票' in _compact(text))):
                    continue
                facts = material_facts(entry, cache_root=self.store.root)
                original_goods = [dict(g) for g in facts.get('goods', []) if g.get('name') and
                                  _compact(str(g['name'])) not in PLACEHOLDERS]
                # One fact adapter supplies matching and expense content. Keep
                # full native/positioned names, quantities and source boxes.
                completion = supplement_goods(self.jobs, job, entry, facts, original_goods)
                if completion['required'] and not completion['complete']:
                    unresolved.append(completion['reason'])
                extractions.append({'material_id': entry['id'], **{k: completion[k] for k in
                                    ('required', 'complete', 'source', 'reason')}})
                for good in completion['goods']:
                    # Source records remain intact; short established service
                    # names are a display normalization of that same record.
                    service = _service_name(str(good['name']), _compact(str(facts.get('source_text') or
                                                                           facts.get('text') or '')))
                    normalized = [{'name': service}] if service else goods_from_text('商品名称：' + str(good['name']), {})
                    for short in normalized or [{'name': str(good['name'])}]:
                        goods.append({**good, 'raw_name': good['name'], 'name': short['name'],
                                      'material_id': entry['id'], 'sha256': entry['digest']})
            # Do not collapse different source rows merely because they carry
            # the same short name. Naming may deduplicate, evidence may not.
            goods = list({digest(g): g for g in goods}.values())
            if any(g['name'] not in ANCILLARY for g in goods):
                goods = [g for g in goods if g['name'] not in ANCILLARY]
            state = {'status': 'extracted' if goods and not unresolved else 'needs_input', 'goods': goods,
                     'extractions': extractions,
                     'reason': '；'.join(dict.fromkeys(unresolved)) if unresolved else
                     ('' if goods else '未提取到明确购买内容，请在人工工作台补充商品名称')}
        elif name == 'classify_expense':
            resuming_rules = self._rule_resume_authorized(job, item, binding)
            if state.get('status') != 'extracted' and not resuming_rules:
                raise ValueError('必须先提取原件商品')
            checked = self._inspect_products(state, sources, item)
            if resuming_rules and (checked['status'] != 'exact' or digest(checked) != job['product_rule_resume']['proof']):
                raise ValueError('商品原文或规则判断已变化，请重新检查')
            state['product_check'] = checked
            state['product_rule_revision'] = PRODUCT_RULES_VERSION
            self._restore_semantic_names(state, checked)
            evidence = {'goods': '\n'.join(g['name'] for g in state['goods']), 'purpose': ''}
            if checked.get('goods_text'):
                evidence['goods'] = checked['goods_text']
            from invoice_print_layout.expense_preferences import Preferences
            preferences = Preferences(self.store).apply(item, goods=evidence['goods'], purpose=evidence['purpose'])
            state['preferences'] = preferences

            def model_call(value: dict[str, str]) -> dict[str, Any]:
                if not job['use_model']:
                    raise ValueError('当前任务仅使用本地分类')
                classifier.api_key()  # Check configuration before reserving an actual provider call.
                with self.jobs.timed(job['id'], 'classification_model',
                                     models_before=self.jobs.get(job['id'])['models']) as timing:
                    self.jobs.reserve_model(job['id'])
                    self.jobs.event(job['id'], 'Jev正在按商品内容判断费用类别')
                    timing['cache'] = 'miss'
                    return classifier.ask_jev(value)

            known_categories = _content_categories(state['goods']) - {None}
            if (checked['status'] == 'human_confirmed' and not
                    protected & {'category', 'project_type', 'project_name', 'report_section'}):
                decision = {'category': checked['category'], 'source': 'human', 'reason': checked['reason'],
                            'fact_id': checked['confirmed_content']['fact_id']}
            elif preferences['conflicts']:
                decision = {'category': None, 'source': 'experience', 'reason': '已有人工经验冲突，请确认费用分类'}
            elif protected & {'category', 'project_type', 'project_name', 'report_section'}:
                decision = {'category': item['category'], 'source': 'human', 'reason': '保留材料上的人工分类'}
            elif preferences['changes'].get('category'):
                decision = {'category': preferences['changes']['category'], 'source': 'experience', 'reason': '复用已有人工分类经验'}
            elif checked['status'] == 'needs_input':
                decision = {'category': None, 'source': 'local', 'reason': checked['reason']}
            elif len(known_categories) > 1:
                decision = {'category': None, 'source': 'local',
                            'reason': '发现不同类别的商品或服务，请在人工工作台确认是否拆分费用'}
            else:
                # Reuse the classifier's exact-content experience/cache before a model call.
                from invoice_print_layout.automatic_category import digest as category_digest
                normal = {k: re.sub(r'\s+', '', v).casefold() for k, v in evidence.items()}
                key = category_digest([normal, classifier.MODEL, classifier.CRITERIA, classifier.RULES])
                with self.store.connect() as db:
                    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='category_experience'").fetchone()
                    experience = db.execute('SELECT * FROM category_experience WHERE evidence_key=?', (key,)).fetchone() if has_table else None
                    disabled = db.execute("SELECT 1 FROM sqlite_master WHERE name='category_experience_disabled'").fetchone()
                    if disabled and db.execute('SELECT 1 FROM category_experience_disabled WHERE evidence_key=?', (key,)).fetchone():
                        experience = None
                if experience and experience['origin'] == 'human':
                    decision = {'category': None if experience['conflict'] else experience['category'],
                                'source': 'experience', 'reason': '复用相同商品内容的分类记录'}
                elif checked['status'] in {'exact', 'established'}:
                    decision = {'category': checked['category'], 'source': 'rules', 'reason': checked['reason']}
                elif experience:
                    # An automatic result is a cache, not a user's explicit
                    # classification. Full-source restrictions still apply.
                    decision = {'category': None if experience['conflict'] else experience['category'],
                                'source': 'cache', 'reason': '复用相同商品内容的自动分类缓存'}
                else:
                    try:
                        decision = classifier.decide(evidence, ask_model=model_call)
                    except ModelBudgetExceeded:
                        raise
                    except GlmAIError:
                        raise
                    except ValueError:
                        decision = {'category': None, 'source': 'local', 'reason': '已有商品，但分类尚未确定，请在人工工作台确认'}
            category = decision.get('category')
            if (decision.get('source') not in {'human', 'experience'} and category in {'餐饮', '外卖'} and
                    checked.get('coverage_complete') and checked.get('context_category') == '外卖'):
                decision = {**decision, 'category': '外卖', 'source': 'rules',
                            'goods_category': category, 'reason': checked['context_reason']}
                category = '外卖'
            if decision.get('source') not in {'human', 'experience'} and category in checked.get('forbidden_categories', []):
                category = None
                decision.update(category=None, reason='完整商品原文明示食品或服务，不能按材料采购放行，请核对费用类别')
            if category not in classifier.CATEGORY.values():
                category = None
            state.update(status='classified' if category else 'needs_input', decision=decision,
                         reason='' if category else decision.get('reason', '请确认费用分类'))
        elif name == 'name_expense':
            if state.get('status') != 'classified' or not state.get('decision', {}).get('category'):
                raise ValueError('必须先完成有依据的费用分类')
            names = list(dict.fromkeys(g['name'] for g in state['goods']))
            category = state['decision']['category']
            preferred = state.get('preferences', {}).get('changes', {})
            if protected & {'title', 'display_name', 'project_name', 'project_type', 'report_section', 'report_group'}:
                choice = validate_report_choice(item.get('project_type'), item.get('project_name'))
            else:
                title = preferred.get('display_name') or concise_expense_name(names, category)
                if len(title) > 200:
                    raise ValueError('报销名称过长，请在人工工作台填写200字以内的名称；完整商品信息保留')
                choice = infer_report_choice({'title': title, 'category': category,
                                             **({'report_section': preferred['report_section']} if preferred.get('report_section') else {})})
                # The company template puts courier services in the editable
                # Other rows, but that must not erase the proven carrier class.
                if category == '顺丰' and choice['project_type'] == 'other':
                    choice = validate_report_choice('other', title)
                    choice.update(category='顺丰', report_section='sf')
                # A template's drinks keyword must not erase an already proved
                # delivery scene. Keep the concrete name in the detail fields.
                if category == '外卖' and choice['category'] == '餐饮':
                    choice = validate_report_choice('living', '员工统一购餐费(附清单)')
                # Keep the fixed company row separate from the concrete service
                # name. In particular, a refund fee is not an ordinary new fare.
                choice.update(title=title, display_name=title)
                if preferred.get('report_group'):
                    choice['report_group'] = preferred['report_group']
            if choice['category'] != category or choice['title'] in PLACEHOLDERS:
                raise ValueError('分类、报销名称或人工选择不一致，请在人工工作台确认')
            state.update(status='ready', reason='商品、费用类别和报销名称已准备')
            self._save(job, raw, item, state, binding, choice)
            return {'content_status': 'ready', 'goods': names, 'category': category, 'title': choice['title'],
                    'source_binding': binding}
        else:
            raise ValueError('商品工具不存在')
        self._save(job, raw, item, state, binding)
        return {'content_status': state['status'], 'goods': [g['name'] for g in state['goods']], 'source_binding': binding,
                'reason': state.get('reason', ''), 'decision': state.get('decision', {})}
