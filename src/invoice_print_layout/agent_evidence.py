"""Read-only, locally grounded material facts for bounded Agent investigation.

This module describes originals.  It never decides that two materials belong to
one transaction and never updates intake fields, expenses or merchant aliases.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import re
from pathlib import Path
import time
from typing import Any
import unicodedata

import pymupdf

from invoice_print_layout.documents import compact_text
from invoice_print_layout.agent_material_cache import encoded, source_facts
from invoice_print_layout.receipt_intake import labelled_counterparty, labelled_goods, read_receipt
from invoice_print_layout.transaction_evidence import extract_transaction_evidence
from invoice_print_layout.workbench import amount_cents


_RECOMMENDATION = re.compile(r'^(?:猜你喜欢|为你推荐|猜你想买|看了又看|相关推荐|推荐商品|相似商品|商品推荐)')
_PURCHASE_ANCHOR = re.compile(r'^(?:商品明细|购买明细|(?:淘宝)?闪购|(?:店铺|门店|商家)(?:名称)?[:：])')
_CONTACT = re.compile(r'地址|电话|手机|联系人|收货人|收件人|寄件人|送至|送到|开票人|'
                      r'纳税人识别号|统一社会信用代码|税号|开户|银行账号|身份证|电子邮箱|https?://|@', re.I)
_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')
PARSER_VERSION = 'material-facts-v5'


def _clean(value: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


@dataclass(frozen=True)
class _Word:
    text: str
    left: float
    top: float
    right: float
    bottom: float

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.bottom - self.top


def _words(page: Any) -> list[_Word]:
    return [_Word(str(word[4]).strip(), *(float(value) for value in word[:4]))
            for word in page.get_text('words') if len(word) >= 5 and str(word[4]).strip()]


def _union(words: list[_Word]) -> list[float]:
    return [round(min(word.left for word in words), 3), round(min(word.top for word in words), 3),
            round(max(word.right for word in words), 3), round(max(word.bottom for word in words), 3)]


def _passenger_page(page: Any, words: list[_Word]) -> tuple[str, list[_Word], bool, str]:
    """Remove only a complete native passenger table; never retain passenger PII."""
    text = str(page.get_text())
    if '电子发票(旅客运输服务)' not in _clean(text):
        return text, words, False, ''
    patterns = {'出行人': r'[\u4e00-\u9fffA-Za-z·*＊]{1,40}',
                '有效身份证件号': r'[A-Za-z0-9*＊-]{4,30}',
                '出行日期': r'\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?',
                '出发地': r'[\u4e00-\u9fffA-Za-z·]{1,40}',
                '到达地': r'[\u4e00-\u9fffA-Za-z·]{1,40}',
                '等级': r'无|-|经济舱|商务舱|头等舱|一等座|二等座|商务座|普通',
                '交通工具类型': r'出租汽车|出租车|网约车|汽车|飞机|火车|轮船|其他'}
    headers = sorted([w for w in words if _clean(w.text) in patterns], key=lambda w: w.left)
    valid = (len(headers) == len(patterns) and len({_clean(w.text) for w in headers}) == len(patterns) and
             max(w.cy for w in headers) - min(w.cy for w in headers) < max(w.height for w in headers))
    if not headers:
        return '', [], True, ''
    top = max(w.bottom for w in headers)
    bottom = min((w.top for w in words if w.top > top and re.match(r'^(?:合计|价税合计|备注)', _clean(w.text))),
                 default=float(page.rect.height))
    table = [w for w in words if top < w.cy < bottom]
    cells: list[list[_Word]] = []
    for index, header in enumerate(headers):
        left = (headers[index - 1].right + header.left) / 2 if index else 0
        right = (header.right + headers[index + 1].left) / 2 if index + 1 < len(headers) else float(page.rect.width)
        column = [w for w in table if left <= w.cx < right]
        cells.append(column)
        valid = valid and bool(column) and all(re.fullmatch(patterns[_clean(header.text)], _clean(w.text)) for w in column)
    valid = valid and len({len(column) for column in cells}) == 1 and sum(map(len, cells)) == len(table)
    if not valid:
        return '', [], True, ''
    excluded = [*headers, *table]
    # Span geometry preserves unrelated product lines, including unknown extra rows.
    rows = []
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines', []):
            spans = [span['text'] for span in line['spans'] if not any(
                span['bbox'][0] <= w.cx <= span['bbox'][2] and span['bbox'][1] <= w.cy <= span['bbox'][3]
                for w in excluded)]
            if spans:
                rows.append(''.join(spans))
    vehicles = cells[next(i for i, header in enumerate(headers) if _clean(header.text) == '交通工具类型')]
    service = '客运服务' if all(_clean(w.text) in {'出租汽车', '出租车', '网约车'} for w in vehicles) else ''
    return '\n'.join(rows), [w for w in words if w not in excluded], False, service


def _rows(words: list[_Word]) -> list[list[_Word]]:
    result: list[list[_Word]] = []
    for word in sorted(words, key=lambda value: (value.cy, value.left)):
        if result and abs(word.cy - result[-1][0].cy) <= max(1.5, min(word.height, result[-1][0].height) * .55):
            result[-1].append(word)
        else:
            result.append([word])
    return [sorted(row, key=lambda value: value.left) for row in result]


def _marker_words(words: list[_Word], label: str) -> list[_Word]:
    """Explicit role titles, including separate horizontal/vertical characters."""
    markers = [word for word in words if _clean(word.text) in {label, label + '信息'}]
    for start in words:
        if _clean(start.text) != label[0]:
            continue
        for vertical in (True, False):
            sequence = [start]
            for character in label[1:]:
                previous = sequence[-1]
                choices = []
                for word in words:
                    if _clean(word.text) != character:
                        continue
                    if vertical:
                        aligned = abs(word.cx - previous.cx) <= max(word.right - word.left, previous.right - previous.left) * .6
                        gap = word.top - previous.bottom
                    else:
                        aligned = abs(word.cy - previous.cy) <= max(word.height, previous.height) * .5
                        gap = word.left - previous.right
                    if aligned and -1 <= gap <= max(word.height, previous.height) * 1.6:
                        choices.append(word)
                if len(choices) != 1:
                    break
                sequence.append(choices[0])
            if len(sequence) == len(label):
                bounds = _union(sequence)
                marker = _Word(label, *bounds)
                if marker not in markers:
                    markers.append(marker)
    return markers


def _actor(page: Any, words: list[_Word], label: str, page_number: int,
           method: str = 'native_geometry') -> dict[str, Any] | None:
    markers = _marker_words(words, label)
    # search_for finds the native substring even when MuPDF joins 名称和值.
    anchors = [_Word('名称', *[float(value) for value in bounds]) for bounds in page.search_for('名称')]
    names = [anchor for marker in markers for anchor in anchors
             if marker.right - 2 <= anchor.left <= marker.right + float(page.rect.width) * .2
             and anchor.bottom >= marker.top - 4 and anchor.top <= marker.bottom + 4]
    if len(names) != 1:
        return None
    anchor = names[0]
    right = min((other.left for other in anchors if other.left > anchor.right and
                 other.bottom >= anchor.top - 2 and other.top <= anchor.bottom + 2), default=float(page.rect.x1))
    # The next column's vertical role heading lies before its name label.  It
    # must not be appended to this column's company name, including swapped
    # left/right tax forms.
    other_markers = _marker_words(words, '购买方' if label == '销售方' else '销售方')
    right = min([right, *[marker.left for marker in other_markers if marker.left > anchor.right and
                         marker.bottom >= anchor.top - 2 and marker.top <= anchor.bottom + 2]])
    selected: list[_Word] = []
    parts: list[str] = []
    for word in sorted(words, key=lambda value: value.left):
        if word.height > anchor.height * 1.8 or abs(word.cy - anchor.cy) > max(word.height, anchor.height) * .4:
            continue
        value = word.text
        left = word.left
        if left <= anchor.left + 1 and word.right > anchor.right and '名称' in value:
            value = re.split(r'名称\s*[:：]?', value, maxsplit=1)[1]
            left = anchor.right
        value = value.lstrip(':：').strip()
        if value and left >= anchor.right - 5 and word.right <= right + 1 and word not in markers:
            selected.append(_Word(value, left, word.top, word.right, word.bottom))
            parts.append(value)
    if (not selected or selected[0].left - anchor.right > max(anchor.height * 6, 40) or
            any(b.left < a.right - 2 or b.left - a.right > max(a.height, b.height) * 1.6
                for a, b in zip(selected, selected[1:]))):
        return None
    quote = ' '.join(parts)
    value = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', quote)
    if (len(value) > 200 or not re.search(r'[A-Za-z\u4e00-\u9fff]', value) or
            re.search(r'[:：]|名称|税号|识别号|开户|账号|地址|电话|发票号码|开票日期', value)):
        return None
    return {'value': value, 'quote': quote, 'method': method,
            'page': page_number, 'bbox': _union(selected)}


def _buyer_fingerprint(words: list[_Word], buyer: dict[str, Any] | None) -> str:
    """Compare explicitly labelled buyer tax IDs locally, never expose the ID."""
    if not buyer:
        return ''
    markers = _marker_words(words, '购买方')
    if len(markers) != 1:
        return ''
    marker = markers[0]
    right = min((other.left for other in _marker_words(words, '销售方') if other.left > marker.right),
                default=max(word.right for word in words) + 1)
    region = [word for word in words if marker.left <= word.left < right and
              marker.top - 4 <= word.cy <= marker.bottom + max(marker.height, 25)]
    labels = [word for word in region if re.search(r'识别号|信用代码|税号', word.text)]
    ids: set[str] = set()
    for label in labels:
        for word in region:
            if abs(word.cy - label.cy) > max(word.height, label.height) * .6:
                continue
            value = _clean(word.text).upper()
            matches = re.findall(r'(?<![A-Z0-9])[A-Z0-9]{15,20}(?![A-Z0-9])', value)
            if word.left >= label.left:
                ids.update(matches)
    if len(ids) != 1:
        return ''
    return 'sha256:' + hashlib.sha256(next(iter(ids)).encode('ascii')).hexdigest()


def _native_metadata(words: list[_Word], page_number: int, source_sha: str) -> list[dict[str, Any]]:
    """Bind detached native footer values to their actual printed labels.

    Stream order alone cannot identify these values. Unknown tail words stay
    outside this list and still require the product-coverage check.
    """
    result: list[dict[str, Any]] = []
    specs = (
        ('invoice_total_words', r'价税合计[（(]大写[）)]', r'[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]+'),
        ('issuer', r'开票人[:：]?', r'[\u4e00-\u9fff·]{2,20}'),
    )
    for kind, label_pattern, value_pattern in specs:
        labels = [word for word in words if re.fullmatch(label_pattern, _clean(word.text))]
        if len(labels) != 1:
            continue
        label = labels[0]
        values = [word for word in words if word is not label and
                  label.right - 2 <= word.left <= label.right + max(label.height, 8) * 30 and
                  abs(word.cy - label.cy) <= max(word.height, label.height) * .6 and
                  re.fullmatch(value_pattern, _clean(word.text))]
        if len(values) != 1:
            continue
        value = values[0]
        result.append({'kind': kind, 'value': _clean(value.text),
                       'quote': label.text + '\n' + value.text, 'page': page_number,
                       'bbox': _union([label, value]), 'method': 'native_geometry', 'sha256': source_sha})
    return result


@dataclass(frozen=True)
class _PageSize:
    width: float
    x1: float


class _OcrPage:
    """Adapt already local OCR boxes to the same explicit-role geometry reader."""
    def __init__(self, words: list[_Word]):
        self.words = words
        width = max(word.right for word in words)
        self.rect = _PageSize(width, width)

    def search_for(self, value: str) -> list[tuple[float, float, float, float]]:
        result = []
        for word in self.words:
            for match in re.finditer(re.escape(value), word.text):
                unit = (word.right - word.left) / len(word.text)
                result.append((word.left + match.start() * unit, word.top,
                               word.left + match.end() * unit, word.bottom))
        return result


def _image_roles(payload: bytes, suffix: str) -> dict[str, Any]:
    # material_facts has already validated these exact bytes. The shared cache
    # covers both roles and goods; do not reopen a path after its hash check.
    lines = read_receipt(payload, suffix)
    words = [_Word(line.text, float(line.left), float(line.top), float(line.right), float(line.bottom))
             for line in lines]
    page = _OcrPage(words)
    facts: dict[str, Any] = {actor: _actor(page, words, label, 1, 'local_ocr_geometry')
                             for actor, label in (('seller', '销售方'), ('buyer', '购买方'))}
    fingerprint = _buyer_fingerprint(words, facts['buyer'])
    if fingerprint:
        facts['buyer']['tax_id_fingerprint'] = fingerprint
    return facts


def _header_label(row: list[_Word], label: str) -> _Word | None:
    matches = []
    for index, word in enumerate(row):
        selected: list[_Word] = []
        text = ''
        for following in row[index:index + len(label)]:
            if selected and following.left - selected[-1].right > max(following.height, selected[-1].height) * 2.5:
                break
            selected.append(following)
            text += _clean(following.text).strip(':：')
            if text == label:
                matches.append(_Word(label, *_union(selected)))
                break
            if not label.startswith(text):
                break
    return matches[0] if len(matches) == 1 else None


def _native_goods(words: list[_Word], page_number: int) -> list[dict[str, Any]]:
    rows = _rows(words)
    headers = [(index, row, _header_label(row, '项目名称') or _header_label(row, '货物或应税劳务名称'))
               for index, row in enumerate(rows)]
    headers = [item for item in headers if item[2] is not None]
    if len(headers) != 1:
        return []
    start, header, name_label = headers[0]
    assert name_label is not None
    labels = {name: label for name in ('规格型号', '单位', '数量', '单价', '金额', '税率/征收率', '税额')
              if (label := _header_label(header, name)) is not None}
    if '税率/征收率' not in labels:
        tax_rates = [word for word in header if _clean(word.text).replace('/', '') == '税率征收率']
        if len(tax_rates) == 1:
            labels['税率/征收率'] = tax_rates[0]
    # A name column requires a real neighbouring header, not a guessed x ratio.
    right_labels = [label.left for label in labels.values() if label.left > name_label.right]
    if not right_labels:
        return []
    name_right = min(right_labels) - .5
    ordered = sorted([name_label, *labels.values()], key=lambda value: value.cx)

    def values(row: list[_Word], column: str) -> list[_Word]:
        label = labels.get(column)
        if label is None:
            return []
        index = ordered.index(label)
        left = (ordered[index - 1].cx + label.cx) / 2 if index else float('-inf')
        right = (ordered[index + 1].cx + label.cx) / 2 if index + 1 < len(ordered) else float('inf')
        return [word for word in row if left < word.cx < right]

    groups: list[list[list[_Word]]] = []
    for row in rows[start + 1:]:
        text = _clean(''.join(word.text for word in row))
        if text.startswith(('合计', '价税合计', '备注', '开票人')):
            break
        names = [word for word in row if word.left < name_right and word.right <= name_right + 2]
        if names and _clean(names[0].text).startswith(('*', '＊')):
            groups.append([row])
        elif groups and names:
            groups[-1].append(row)
    result: list[dict[str, Any]] = []
    for group in groups:
        names = [word for row in group for word in row if word.left < name_right and word.right <= name_right + 2]
        raw_name = ''.join(word.text for word in names)
        name = re.sub(r'^[*＊][^*＊]+[*＊]', '', raw_name).strip()
        if not name or _CONTACT.search(name):
            continue
        quote = '\n'.join(' '.join(word.text for word in row) for row in group)
        first = group[0]
        item: dict[str, Any] = {'name': name, 'raw_name': raw_name, 'quote': quote, 'method': 'native_geometry',
                                'page': page_number, 'bbox': _union([word for row in group for word in row])}
        quantity = values(first, '数量')
        if len(quantity) == 1 and _NUMBER.fullmatch(_clean(quantity[0].text)):
            item['quantity'] = _clean(quantity[0].text)
        units = values(first, '单位')
        if len(units) == 1 and re.fullmatch(r'[\u4e00-\u9fffA-Za-z]{1,8}', units[0].text):
            item['unit'] = units[0].text
        amounts = values(first, '金额')
        if len(amounts) == 1 and _NUMBER.fullmatch(_clean(amounts[0].text)):
            item['is_discount'] = _clean(amounts[0].text).startswith('-')
            try:
                value = _clean(amounts[0].text)
                item['amount_cents'] = amount_cents(value.lstrip('-')) * (-1 if value.startswith('-') else 1)
            except ValueError:
                pass
        taxes = values(first, '税额')
        if len(taxes) == 1 and _NUMBER.fullmatch(_clean(taxes[0].text)):
            try:
                value = _clean(taxes[0].text)
                item['tax_cents'] = amount_cents(value.lstrip('-')) * (-1 if value.startswith('-') else 1)
            except ValueError:
                pass
        result.append(item)
    return result


def _safe_text(text: str, *, limit: int = 3000) -> str:
    rows: list[str] = []
    skip_value = False
    source_rows = text.splitlines()
    first_purchase = next((index for index, raw in enumerate(source_rows)
                           if _PURCHASE_ANCHOR.search(_clean(raw))), None)
    skipping_top_ad = False
    for index, raw in enumerate(source_rows):
        row = raw.strip()
        compact = _clean(row)
        if _RECOMMENDATION.search(compact):
            # Some shopping apps put a promotional panel above the real shop.
            # Ignore that panel until its later explicit purchase anchor.  A
            # recommendation after the purchase area still ends usable content.
            if first_purchase is not None and index < first_purchase:
                skipping_top_ad = True
                continue
            break
        if skipping_top_ad:
            if index == first_purchase:
                skipping_top_ad = False
            elif compact in {'订单已送达', '订单已完成', '交易成功', '订单详情'}:
                rows.append(row)
                continue
            else:
                continue
        if compact.startswith(('广告', '推广')):
            if first_purchase is not None and index < first_purchase:
                skipping_top_ad = True
            continue
        if _CONTACT.search(row):
            skip_value = bool(re.fullmatch(r'(?:地址|电话|手机|联系人|收货人|收件人|寄件人|开票人|'
                                          r'纳税人识别号|统一社会信用代码|税号|开户行|银行账号|身份证|电子邮箱)[:：]?', compact))
            continue
        if skip_value:
            skip_value = False
            continue
        if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', row):
            continue
        row = re.sub(r'(?<!\d)\d{1,2}[:：]\d{2}(?:[:：]\d{2})?(?!\d)', '[时间省略]', row)
        # Detached native tax numbers and unlabeled contact identifiers are not
        # needed for semantic goods/merchant review. Typed IDs remain elsewhere.
        row = re.sub(r'(?<![A-Za-z0-9])[A-Za-z0-9]{15,}(?![A-Za-z0-9])', '[编号省略]', row)
        if row:
            rows.append(row)
    return '\n'.join(rows)[:limit]


def _text_fact(value: str, quote: str, method: str = 'intake_text') -> dict[str, Any] | None:
    if not value or not quote:
        return None
    return {'value': value, 'quote': quote[:500], 'method': method, 'page': None, 'bbox': None}


def _labelled_actor(text: str, role: str) -> dict[str, Any] | None:
    labels = {'seller': r'销售方名称|销方名称|销售方|SELLER',
              'buyer': r'购买方名称|购方名称|购买方|BUYER'}
    observed: dict[str, str] = {}
    for row in text.splitlines():
        match = re.fullmatch(r'\s*(?:' + labels[role] + r')\s*[:：]\s*(.{2,200})', row, re.I)
        if match and not _CONTACT.search(match[1]):
            observed[match[1].strip()] = row.strip()
    if len(observed) != 1:
        return None
    value, quote = next(iter(observed.items()))
    return _text_fact(value, quote)


def _material_facts(entry: dict[str, Any], payload: bytes | None,
                    source_issue: str = '') -> dict[str, Any]:
    """Compute facts from validated bytes; keep cache policy outside parsing."""
    # Lazy imports keep the existing combination/content modules free to consume
    # this independent read adapter without introducing an import cycle.
    from invoice_print_layout.agent_combinations import invoice_complete
    from invoice_print_layout.agent_content import goods_from_text
    from invoice_print_layout.intake_queue import positioned_invoice_seller

    role = str(entry.get('role') or 'other')
    fields = dict(entry.get('fields') or {})
    source_text = str(entry.get('text') or '')
    text = _safe_text(source_text)
    issues: list[str] = []
    actors: dict[str, dict[str, Any] | None] = dict.fromkeys(
        ('seller', 'buyer', 'shop', 'platform', 'collector', 'payment_channel'))
    goods: list[dict[str, Any]] = []
    native_metadata: list[dict[str, Any]] = []
    native = False
    path = Path(str(entry.get('path') or ''))
    if source_issue:
        # Do not let stale intake text or fields rescue replaced/missing bytes.
        return {'id': entry.get('id'), 'sha256': entry.get('digest'), 'revision': entry.get('revision'),
                'role': role, 'complete': False, 'total_cents': None, 'actors': actors,
                'goods': [], 'descriptions': [], 'text': '', 'source_text': '',
                'goods_text': '', 'goods_issues': [], 'goods_complete': False,
                'source_text_truncated': False, 'goods_text_truncated': False,
                'parser_version': PARSER_VERSION, 'issues': [source_issue]}
    if path.suffix.lower() == '.pdf':
        try:
            with pymupdf.open(stream=payload, filetype='pdf') as document:  # type: ignore[no-untyped-call]
                if document.needs_pass or len(document) > 10:
                    raise ValueError('PDF页数或加密状态需要人工检查')
                collected: dict[str, list[dict[str, Any]]] = {'seller': [], 'buyer': []}
                source_pages = []
                passenger_goods = []
                for index, page in enumerate(document):
                    words = _words(page)
                    native = native or bool(words)
                    if role == 'invoice':
                        page_text, words, passenger_issue, service = _passenger_page(page, words)
                        source_pages.append(page_text)
                        if passenger_issue:
                            issues.append('客运栏目未能完整核验，请在人工工作台核对原件')
                        if service:
                            passenger_goods.append({'name': service, 'quote': '电子发票（旅客运输服务）',
                                                    'method': 'native_passenger_service', 'page': index + 1})
                    else:
                        source_pages.append(str(page.get_text()))
                    for actor, label in (('seller', '销售方'), ('buyer', '购买方')):
                        fact = _actor(page, words, label, index + 1)
                        if actor == 'seller' and fact:
                            seller = positioned_invoice_seller(page)
                            if seller and _clean(str(fact['value'])) != _clean(seller):
                                fact = None
                        if fact:
                            if actor == 'buyer' and (fingerprint := _buyer_fingerprint(words, fact)):
                                fact['tax_id_fingerprint'] = fingerprint
                            collected[actor].append(fact)
                    goods.extend(_native_goods(words, index + 1))
                    if role == 'invoice':
                        native_metadata.extend(_native_metadata(words, index + 1, str(entry.get('digest') or '')))
                for actor, facts in collected.items():
                    if len({_clean(str(fact['value'])) for fact in facts}) == 1:
                        actors[actor] = facts[0]
                    elif facts:
                        issues.append('原件' + ('销售方' if actor == 'seller' else '购买方') + '不唯一')
                if native:
                    source = '\n'.join(source_pages)
                    source_text = source
                    if not goods:
                        goods = [{**item, 'method': 'native_text'} for item in goods_from_text(_safe_text(source), fields)]
                    if not goods:
                        goods = passenger_goods
                    for actor in ('seller', 'buyer'):
                        if actors[actor] is None:
                            fact = _labelled_actor(source, actor)
                            if fact:
                                fact['method'] = 'native_label'
                                actors[actor] = fact
                    if role == 'invoice':
                        # Tax forms detach buyer/seller and contact labels from
                        # values in stream order, so use positioned fragments.
                        text = _safe_text('\n'.join([
                            *[('销售方' if actor == 'seller' else '购买方') + '：' + str(fact['quote'])
                              for actor, fact in actors.items() if fact],
                            *[str(item['quote']) for item in goods],
                        ]))
                    else:
                        # Payment/receipt PDFs need their explicit product and
                        # collector labels, just as image intake text does.
                        text = _safe_text(source)
        except (OSError, RuntimeError, ValueError) as exc:
            issues.append('原件PDF未能完整读取：' + str(exc)[:100])
    elif (role == 'invoice' and path.suffix.lower() in {'.jpg', '.jpeg', '.png', '.webp'} and payload is not None and
          '销售方' in _clean(source_text) and '购买方' in _clean(source_text)):
        # Intake already provides usable text for ordinary screenshots.  Only
        # this ambiguous side-by-side tax-form role needs its local OCR boxes.
        try:
            actors.update(copy.deepcopy(_image_roles(payload, path.suffix.lower())))
        except (OSError, RuntimeError, ValueError):
            issues.append('图片买卖双方版面未能确认')
    if not native:
        actors['seller'] = actors['seller'] or _labelled_actor(text, 'seller')
        actors['buyer'] = actors['buyer'] or _labelled_actor(text, 'buyer')
    transaction = extract_transaction_evidence(text, role, fields)
    for actor, key in (('shop', 'shop_name'), ('platform', 'shopping_platform'), ('payment_channel', 'payment_channel')):
        actors[actor] = _text_fact(str(transaction['fields'].get(key) or ''),
                                   str(transaction['evidence'].get(key) or ''))
    if role in {'order', 'purchase'} and not actors['shop']:
        shops: dict[str, str] = {}
        for row in text.splitlines():
            match = re.match(r'^\s*(?:淘宝闪购|闪购)\s*(.+?)(?:\s+蜂鸟|\s+联系商家|\s+进入店铺|\s*[>›]|$)', row)
            if match and re.search(r'店|超市|商行|餐厅|饭馆', match[1]):
                shops[match[1].strip()] = row.strip()
        if len(shops) == 1:
            shop_name, quote = next(iter(shops.items()))
            actors['shop'] = _text_fact(shop_name, quote)
    if role == 'payment':
        collector, quote, conflict = labelled_counterparty(text.splitlines())
        actors['collector'] = _text_fact(collector, quote)
        if conflict:
            issues.append('收款方不唯一')
        shop = actors['shop']
        if shop and (re.search(r'收单机构|支付机构|银行', str(shop['quote'])) or
                     collector and _clean(str(shop['value'])) == _clean(collector)):
            actors['shop'] = None
    if not goods:
        goods = [{**item, 'method': 'intake_text'} for item in goods_from_text(text, fields)]
    goods_issues = ['商品行超过20条，尚未完整提取'] if len(goods) > 20 else []
    goods = goods[:20]
    descriptions: list[dict[str, Any]] = []
    description, description_quote = labelled_goods(text.splitlines())
    description_fact = _text_fact(description, description_quote, 'native_label' if native else 'intake_text')
    if description_fact:
        descriptions.append(description_fact)
    for item in goods:
        fact = _text_fact(str(item['name']), str(item['quote']), str(item['method']))
        if fact:
            fact['page'], fact['bbox'] = item.get('page'), item.get('bbox')
            if _clean(str(fact['value'])) not in {_clean(str(existing['value'])) for existing in descriptions}:
                descriptions.append(fact)
    for row in text.splitlines():
        if re.fullmatch(r'共\s*\d+\s*(?:件|份|杯|组)', row):
            fact = _text_fact(_clean(row), row)
            if fact:
                descriptions.append(fact)
    total: int | None
    try:
        total = amount_cents(fields.get('amount') or '')
        if total <= 0:
            total = None
    except (ValueError, TypeError):
        total = None
    if total is not None:
        # Label as the existing intake total; do not fabricate a native quote.
        text = (text + '\n已提取总金额：' + f'{total / 100:.2f}').strip()[:3000]
    complete = invoice_complete({**entry, 'text': source_text}) if role == 'invoice' else bool(text and total is not None and role != 'other')
    if issues:
        complete = False
    source_context = _safe_text(source_text, limit=12001)
    goods_context = '\n'.join(str(item['quote']) for item in goods)
    return {'id': entry.get('id'), 'sha256': entry.get('digest'), 'revision': entry.get('revision'),
            'role': role, 'complete': complete, 'total_cents': total, 'actors': actors,
            'goods': goods, 'native_metadata': native_metadata,
            'descriptions': descriptions, 'text': text, 'issues': issues,
            'source_text': source_context[:12000], 'source_text_truncated': len(source_context) > 12000,
            'goods_text': goods_context[:12000], 'goods_text_truncated': len(goods_context) > 12000,
            'goods_complete': bool(goods) and not goods_issues,
            'goods_issues': goods_issues, 'parser_version': PARSER_VERSION}


def material_facts(entry: dict[str, Any], *, cache_root: Path | None = None,
                   cache_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Describe current evidence, with an optional workspace-local parse cache.

    Every call reads/hashes the original before a cache lookup. A valid intake
    digest is required to accept real source bytes; legacy text-only callers
    remain supported. The cache avoids repeated local parsing/OCR, and never
    mutates intake fields or treats prior facts as proof of unchanged originals.
    ``cache_info`` collects non-business timing/hit metadata when requested.
    """
    started = time.perf_counter()
    from invoice_print_layout.agent_content import GOODS_RULES_VERSION
    snapshot = copy.deepcopy(entry)
    path_value = str(snapshot.get('path') or '')
    path = Path(path_value) if path_value else None
    expected = str(snapshot.get('digest') or '')
    actual = ''
    payload: bytes | None = None
    issue = ''
    hashing = time.perf_counter()
    if path is not None:
        try:
            payload = path.read_bytes()
            actual = hashlib.sha256(payload).hexdigest()
            if expected and (not re.fullmatch(r'[a-fA-F0-9]{64}', expected) or actual != expected.lower()):
                issue = '原件哈希与收件不一致，请重新检查材料'
        except OSError:
            issue = '原件未能完整读取，请重新检查材料'
    elif re.fullmatch(r'[a-fA-F0-9]{64}', expected):
        issue = '材料缺少可核验的原件路径'
    if cache_info is not None:
        cache_info.update(source_hash_seconds=time.perf_counter() - hashing,
                          source_bytes=len(payload) if payload is not None else 0)
    if issue:
        result = _material_facts(snapshot, None, issue)
        if cache_info is not None:
            cache_info.update(hit=False, storage='invalid_source', facts_seconds=time.perf_counter() - started)
        return result
    # The complete entry snapshot deliberately includes manual/conflict/status
    # metadata, not just revision. A changed field with a stale revision must not
    # reuse yesterday's machine result. Paths prevent cross-source provenance.
    identity = [PARSER_VERSION, GOODS_RULES_VERSION, str(pymupdf.VersionBind),
                str(path.resolve()) if path is not None else '', actual, snapshot]
    key = hashlib.sha256(encoded(identity).encode('utf-8')).hexdigest()
    result = source_facts(key, lambda: _material_facts(snapshot, payload), root=cache_root, info=cache_info)
    if cache_info is not None:
        cache_info['facts_seconds'] = time.perf_counter() - started
    return result
