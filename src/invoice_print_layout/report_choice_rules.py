"""Small local naming rules based on transaction descriptions, never merchants.

These are suggestions for human confirmation, not saved classifications. Keep
aliases explicit so device names and instructions to print invoices cannot match.
"""
from __future__ import annotations

import re
from typing import Any


PRINTING_ALIASES = ('打印制作', '打印服务', '打印费', '复印服务', '复印费',
                    '图文快印', '数码快印', '印刷服务', '印刷费', '打印制作费')
PRINTING_SMALL_ITEMS = ('胶带', '笔', '铅笔', '签字笔', '订书钉', '订书针',
                        '文件夹', '文件袋', '回形针', '纸张', '复印纸')


def printing_basket(names: list[str]) -> bool:
    """Exact printing plus small stationery on one receipt; unknown goods abstain."""
    services = (*PRINTING_ALIASES, '打印', '复印')
    patterns = '|'.join(re.escape(name) for name in (*services, *PRINTING_SMALL_ITEMS))
    cleaned = [re.sub(r'^[*＊][^*＊]+[*＊]', '', name).strip() for name in names]
    matches = [re.fullmatch('(' + patterns + r')[\s\d.元份个只包盒张页支卷×x]*', name) for name in cleaned]
    return bool(matches) and all(matches) and any(match and match[1] in services for match in matches)


DRINKING_WATER_ALIASES = ('饮用水', '矿泉水', '纯净水', '瓶装水', '桶装水')
_DESCRIPTION_KEYS = ('goods', 'purchase_content', 'payment_note', 'transaction_description')
_DESCRIPTION_LABELS = ('商品说明', '商品名称', '购买内容', '交易说明', '付款备注', '支付备注')
_GENERIC_NAME = re.compile(r'(?:普通)?(?:电子)?(?:发票|收据|付款|支付)(?:截图|凭证|文件)?\d*'
                           r'(?:[（(][^）)]*[）)])?')


def _has_confirmed_name(item: dict[str, Any]) -> bool:
    if item.get('project_name'):
        return True
    name = str(item.get('display_name') or item.get('title') or '').strip().split(' · ')[-1]
    return bool(name and not _GENERIC_NAME.fullmatch(name)
                and not re.search(r'\.(?:pdf|png|jpe?g|webp)$', name, re.IGNORECASE))


def _descriptions(source: dict[str, Any]) -> list[str]:
    descriptions = [str(source[key]).strip() for key in _DESCRIPTION_KEYS
                    if isinstance(source.get(key), str) and source[key].strip()]
    fields = source.get('fields')
    if isinstance(fields, dict):
        descriptions.extend(str(fields[key]).strip() for key in _DESCRIPTION_KEYS
                            if isinstance(fields.get(key), str) and fields[key].strip())
    raw = source.get('text')
    if isinstance(raw, str):
        # Chinese invoices commonly print "*tax category*transaction item".
        descriptions.extend(re.findall(r'\*[^\r\n*]{1,40}\*([^\r\n]{1,160})', raw))
        label = '|'.join(_DESCRIPTION_LABELS)
        descriptions.extend(re.findall(r'(?:' + label + r')\s*[:：]?\s*([^\r\n]{1,160})', raw))
    return descriptions


def printing_report_suggestion(item: dict[str, Any]) -> dict[str, str]:
    """Return an evidence-based printing suggestion without replacing named work."""
    if _has_confirmed_name(item):
        return {}
    if item.get('project_type') not in (None, '', 'other', '其他'):
        return {}
    if item.get('category') not in (None, '', '其他') or item.get('report_section') not in (None, '', 'other'):
        return {}
    sources = [item]
    related = item.get('recognition_sources')
    if isinstance(related, list):
        sources.extend(source for source in related if isinstance(source, dict))
    matches: list[str] = []
    for source in sources:
        descriptions = _descriptions(source)
        if not descriptions:
            continue
        if not printing_basket([name for description in descriptions
                                for name in re.split(r'[、,，;；+＋\n]+', description)]):
            return {}
        matches.extend(descriptions)
    if not matches:
        return {}
    return {'project_type': 'printing', 'project_name': '打印', 'source': 'local_rule',
            'reason': '材料明细包含打印、复印或快印服务', 'evidence': matches[0]}


def material_report_suggestion(item: dict[str, Any]) -> dict[str, str]:
    """Suggest known service/material names from saved optional descriptions."""
    printing = printing_report_suggestion(item)
    if printing:
        return printing
    if _has_confirmed_name(item) or item.get('project_type') not in (None, '', 'other', 'living', '其他', '生活费'):
        return {}
    if item.get('category') not in (None, '', '其他', '外卖', '餐饮'):
        return {}
    if item.get('report_section') not in (None, '', 'other', 'takeout', 'dining'):
        return {}
    sources = [item]
    related = item.get('recognition_sources')
    if isinstance(related, list):
        sources.extend(source for source in related if isinstance(source, dict))
    for source in sources:
        for description in _descriptions(source):
            compact = re.sub(r'\s+', '', description)
            if not any(alias in compact for alias in DRINKING_WATER_ALIASES):
                continue
            if re.search(r'饮水机|净水器|过滤器|空瓶|(?:矿泉水|饮用水)瓶(?:身|盖|标签|制造|生产)|'
                         r'(?:不含|不包含|不包括|不是|无需|未含).{0,12}(?:饮用水|矿泉水|纯净水|瓶装水|桶装水)', compact):
                continue
            return {'project_type': 'living', 'project_name': '现场饮用水、夜宵', 'source': 'local_rule',
                    'reason': '材料明细明确包含饮用水', 'evidence': description.strip()[:160]}
    return {}
