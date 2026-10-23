"""Evidence-bounded Agent adapters for the workbench's material requirements.

This module proposes drafts, never verification. All proposals are rechecked by
InvoiceTools and registered through MaterialMatching's transaction/digest guards.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pymupdf

from invoice_print_layout.documents import (compact_text, parse_amount,
    parse_invoice_number, _parse_positioned_invoice_amount, PdfInspectionError)
from invoice_print_layout.matching_evidence import merchant_identity
from invoice_print_layout.material_matching import _rules_relation, _same_company
from invoice_print_layout.models import DocumentKind
from invoice_print_layout.transaction_evidence import identifiers_for_record, identifier_key
from invoice_print_layout.workbench import amount_cents, evaluate
from invoice_print_layout.agent_candidate_groups import (EnumerationBudget, amount_groups,
    failed_payment, known_cents, outside_competitor)
from invoice_print_layout.agent_transport import (MailOrigin, identity_conflict, pair_priority_evidence, pair_status,
    project_transport, ride_service, source_current, transport_missing_reason, transport_profile)


def local_structured_facts(entry: dict[str, Any]) -> dict[str, Any]:
    """Recover only explicit totals in structured native rail/waybill PDFs."""
    transport = project_transport(entry)
    if transport:
        return {'role': transport['role'], 'fields': transport['fields']}
    if Path(str(entry.get('path') or '')).suffix.lower() != '.pdf':
        return {}
    try:
        with pymupdf.open(entry['path']) as pdf:  # type: ignore[no-untyped-call]
            native_text = '\n'.join(page.get_text() for page in pdf)
            text = compact_text(native_text)
    except (OSError, RuntimeError, ValueError):
        return {}
    try:
        number = parse_invoice_number(native_text)
    except PdfInspectionError:
        # Waybill detail tables may share a row with their invoice date. Keep
        # the explicit column heading and a complete standalone number.
        rows = [row.strip() for row in native_text.splitlines() if row.strip()]
        numbers = {m[1] for i, row in enumerate(rows[:-1]) if row == '发票号码'
                   and (m := re.fullmatch(r'(?:\d{4}[/.-]\d{1,2}[/.-]\d{1,2}\s+)?(\d{20})', rows[i + 1]))}
        if len(numbers) != 1:
            return {}
        number = next(iter(numbers))
    if not re.fullmatch(r'\d{20}', number):
        return {}
    if ('电子发票(铁路电子客票)' in text and '电子客票号' in text and
            re.search(r'(?:票价|退票费)[:：]', text)):
        values = {amount_cents(v) for v in re.findall(r'[¥￥](\d+\.\d{2})(?![\d.])', text)}
        if len(values) == 1 and next(iter(values)) > 0:
            cents = next(iter(values))
            return {'role': 'invoice', 'fields': {'amount': f'{cents / 100:.2f}',
                    'category': '高铁', 'merchant': '中国铁路', 'invoice_number': number,
                    'title': '铁路退票费' if '退票费' in text else '铁路车票'}}
    if '运单明细' in text and re.search(r'SF\d{10,}', text):
        totals = {amount_cents(v) for v in re.findall(
            r'发票总金额[（(]元[)）][:：]?(\d+(?:\.\d{1,2})?)(?![\d.])', text)}
        if len(totals) == 1 and next(iter(totals)) > 0:
            return {'role': 'detail', 'fields': {'amount': f'{next(iter(totals)) / 100:.2f}',
                    'category': '顺丰', 'invoice_number': number}}
    return {}


def source_category(entry: dict[str, Any]) -> str:
    """Category follows source service/content, never a filename or project title."""
    text = compact_text(str(entry.get('text') or ''))
    role = entry.get('role')
    if '铁路电子客票' in text and '电子客票号' in text:
        return '高铁'
    if role == 'invoice' and re.search(r'[*＊][^*＊]+[*＊]住宿服务', text):
        return '酒店'
    if role in {'invoice', 'trip', 'trip_sheet'} and (
            '行程单' in text or '出租车' in text or '出租客运' in text or
            role == 'invoice' and ride_service(str(entry.get('text') or ''))):
        return '打车'
    if (role == 'detail' and ('运单明细' in text or all(
            word in text for word in ('运单起止日期', '订单号', '发件信息', '收件信息'))) or
            role == 'invoice' and '收派服务' in text and '顺丰' in text):
        return '顺丰'
    # Existing local category rules may identify goods; they do not assert links.
    category = str((entry.get('fields') or {}).get('category') or '')
    return category if category in {'材料采购', '餐饮', '外卖'} else ''


def invoice_complete(entry: dict[str, Any]) -> bool:
    source = str(entry.get('text') or '')
    text = compact_text(source)
    fields = entry.get('fields') or {}
    if entry.get('role') != 'invoice' or not fields.get('amount'):
        return False
    try:
        number = parse_invoice_number(source)
        if fields.get('invoice_number') and fields['invoice_number'] != number:
            return False
        expected = amount_cents(fields['amount'])
        if '铁路电子客票' in text:
            facts = local_structured_facts(entry)
            return bool(facts and amount_cents(facts['fields']['amount']) == expected)
        if '价税合计' not in text:
            return False
        try:
            return amount_cents(parse_amount(source, DocumentKind.INVOICE)) == expected
        except PdfInspectionError:
            pass
        if Path(str(entry.get('path') or '')).suffix.lower() != '.pdf':
            return False
        with pymupdf.open(entry['path']) as pdf:  # type: ignore[no-untyped-call]
            totals = {amount_cents(_parse_positioned_invoice_amount(page)) for page in pdf
                      if '价税合计' in compact_text(page.get_text())}
        return totals == {expected} and expected > 0
    except (ValueError, OSError, RuntimeError):
        return False


def _identities(entry: dict[str, Any]) -> set[str]:
    return {k for i in identifiers_for_record(entry) if (k := identifier_key(i))}


def source_relation(left: dict[str, Any], right: dict[str, Any],
                    left_state: dict[str, Any], right_state: dict[str, Any]) -> bool:
    """Use existing seller/description rules plus exact typed transaction IDs."""
    if left_state.get('name_conflict') or right_state.get('name_conflict'):
        return False
    left_ids, right_ids = _identities(left), _identities(right)
    left_domains = {tuple(json.loads(k)[:2]) for k in left_ids}
    right_domains = {tuple(json.loads(k)[:2]) for k in right_ids}
    for domain in left_domains & right_domains:
        lvalues = {tuple(json.loads(k)) for k in left_ids if tuple(json.loads(k)[:2]) == domain}
        rvalues = {tuple(json.loads(k)) for k in right_ids if tuple(json.loads(k)[:2]) == domain}
        if lvalues != rvalues:
            return False
    if left_ids & right_ids:
        return True
    if _rules_relation(left_state, right_state) == 'yes':
        return True
    # Explicit transfer recipients can be embedded after 转给 instead of in a
    # merchant field. Reuse the same company-equivalence rule, not fuzzy names.
    for invoice, payment, state in ((left, right, left_state), (right, left, right_state)):
        if invoice.get('role') != 'invoice' or payment.get('role') != 'payment':
            continue
        text = compact_text(str(payment.get('text') or ''))
        match = re.search(r'转给([\u4e00-\u9fffA-Za-z0-9（）()·]+?(?:股份有限公司|有限责任公司|有限公司))', text)
        if match and _same_company(merchant_identity(state.get('identity') or ''), merchant_identity(match[1])):
            return True
    return False


def extended_candidates(entries: list[dict[str, Any]], states: dict[str, dict[str, Any]],
                        *, scope: set[str] | None = None,
                        trusted_mail_origins: dict[str, MailOrigin] | None = None) -> list[dict[str, Any]]:
    """Enumerate bounded legal paths; shared members remain competing candidates."""
    scoped = [e for e in entries if scope is None or e['id'] in scope]
    eligible = [e for e in scoped if e['id'] in states and not states[e['id']].get('name_conflict')
                and known_cents(e) is not None and not failed_payment(e)]
    invoices = [e for e in eligible if invoice_complete(e)]
    payments = [e for e in eligible if e.get('role') == 'payment']
    supports = [e for e in eligible if e.get('role') in {'purchase', 'order', 'trip', 'trip_sheet', 'detail'}]
    profiles = {e['id']: transport_profile(e) for e in eligible
                if e.get('role') in {'trip', 'trip_sheet', 'detail'} or source_category(e) in {'打车', '顺丰'}}
    results: list[dict[str, Any]] = []
    budget = EnumerationBudget()

    def cents(e: dict[str, Any]) -> int:
        return amount_cents((e.get('fields') or {}).get('amount', '0'))

    def add(v: list[dict[str, Any]], p: list[dict[str, Any]], s: list[dict[str, Any]],
            category: str, basis: str, proof: str) -> None:
        if not budget.spend():
            return
        records = [*v, *p, *s]
        total = sum(cents(e) for e in v)
        roles = {str(e['role']) for e in records}
        if not total or not evaluate(category, roles, material_basis=basis)['complete']:
            return
        if p and sum(cents(e) for e in p) != total:
            return
        owners = {e.get('reimbursement_person_id') for e in records if e.get('reimbursement_person_id')}
        if len(owners) > 1:
            return
        result = {'voucher_keys': sorted('q:' + e['id'] for e in v),
                        'payment_ids': sorted(e['id'] for e in p), 'support_ids': sorted(e['id'] for e in s),
                        'category': category, 'material_basis': basis, 'amount_cents': total, 'proof': proof}
        if category in {'打车', '顺丰'} and len(v) == len(s) == 1:
            result['source_context'] = pair_priority_evidence(v[0], s[0], profiles,
                                                              trusted_mail_origins=trusted_mail_origins)
        results.append(result)

    for invoice in invoices:
        if budget.exhausted:
            return []
        category = source_category(invoice)
        if category in {'打车', '顺丰'} and not source_current(invoice):
            continue
        if category in {'高铁', '酒店'}:
            # Each complete native service invoice stands alone. Optional
            # payments attach only when the ordinary seller relation proves it.
            related = [p for p in payments if source_relation(invoice, p, states[invoice['id']], states[p['id']])]
            groups = amount_groups(related, cents(invoice), cents, budget)
            if groups is not None and len(groups) == 1:
                add([invoice], groups[0], [], category, 'standard', '完整服务发票及同商家全额付款')
            else:
                add([invoice], [], [], category, 'standard', '按既有材料要求使用完整铁路或住宿发票')
        if category == '打车':
            for support in supports:
                if support['role'] not in {'trip', 'trip_sheet'} or cents(support) != cents(invoice):
                    continue
                if not source_current(support) or identity_conflict(invoice, support):
                    continue
                a, b = profiles.get(invoice['id']), profiles.get(support['id'])
                if a and b and pair_status(invoice, support, profiles, trusted_mail_origins=trusted_mail_origins) == 'supported':
                    proof = ('同一可信邮件内完整发票及行程的唯一对应合计' if not a['identity'] or not b['identity'] else
                             '同一出行服务方的完整发票及行程合计')
                    add([invoice], [], [support], category, 'standard', proof)
        if category == '顺丰':
            for support in supports:
                if support['role'] != 'detail':
                    continue
                if not source_current(support) or identity_conflict(invoice, support):
                    continue
                a, b = profiles.get(invoice['id']), profiles.get(support['id'])
                if a and b and pair_status(invoice, support, profiles, trusted_mail_origins=trusted_mail_origins) == 'supported':
                    proof = ('完整运单与发票的服务方或发票身份及总额相符' if b['invoice_number'] or b['identity'] else
                             '同一可信邮件内完整发票及同城运单的唯一对应合计')
                    add([invoice], [], [support], category, 'standard', proof)
        for support in supports:
            if support['role'] not in {'purchase', 'order'} or cents(support) != cents(invoice):
                continue
            if source_relation(invoice, support, states[invoice['id']], states[support['id']]):
                kind = source_category(support)
                if kind in {'材料采购', '餐饮', '外卖'}:
                    add([invoice], [], [support], kind, 'standard', '发票与明细的商家或同域交易身份及全额对应')
        # Standard invoice/payment paths can have missing merchant projections,
        # but only a grounded source relation can fill that gap.
        related = [p for p in payments if source_relation(invoice, p, states[invoice['id']], states[p['id']])]
        if category not in {'高铁', '酒店', '打车', '顺丰'}:
            for group in amount_groups(related, cents(invoice), cents, budget) or []:
                add([invoice], group, [], '其他', 'invoice_payment', '发票与成功付款的明确商家及全额对应')
    # Multi-invoice groups require a single supporting order with grounded
    # seller/typed-ID links to every invoice. Amount alone never creates a link.
    for support in supports:
        if budget.exhausted:
            return []
        if support['role'] not in {'purchase', 'order'}:
            continue
        related = [v for v in invoices if source_relation(v, support, states[v['id']], states[support['id']])]
        kind = source_category(support)
        if kind in {'材料采购', '餐饮', '外卖'}:
            for group in amount_groups(related, cents(support), cents, budget, minimum=2) or []:
                add(group, [], [support], kind, 'standard', '同域交易或商家对应的多张发票合计与明细相符')
    # Prefer mutually strongest source-bound transport pairs before ambiguity.
    # Equal ranks remain competing; an unscoped orphan provider-only proof must
    # not silently disappear merely because another pair has mail provenance.
    transport = [r for r in results if (r.get('source_context') or {}).get('priority_rank')]
    if transport:
        best: dict[str, int] = {}
        for row in transport:
            rank = int(row['source_context']['priority_rank'])
            for key in selection(row):
                best[key] = max(best.get(key, 0), rank)
        strongest = [r for r in transport if all(
            r['source_context']['priority_rank'] == best[key] for key in selection(r))]
        covered = set().union(*(selection(r) for r in strongest))
        unscoped = [r for r in transport if r['source_context']['priority_rank'] == 1 and any(
            key not in covered and not isinstance((trusted_mail_origins or {}).get(key), MailOrigin)
            for key in selection(r))]
        strongest = [r for r in strongest if r['source_context']['priority_rank'] != 2 or not any(
            selection(r) & selection(other) for other in unscoped)]
        results = [r for r in results if r not in transport] + strongest

    # Do not resolve equal-amount/provider competition by enumeration order.
    usable = {e['id'] for e in eligible}
    # A known amount alone cannot make an incomplete/native-conflicting support
    # invisible to the existing whole-project outside-competitor audit.
    usable -= {key for key, profile in profiles.items() if not profile or
               profile.get('identity_ambiguous') or
               known_cents(next(e for e in eligible if e['id'] == key)) != profile['amount_cents']}
    def blocked_competitor(row: dict[str, Any]) -> bool:
        return outside_competitor(row, entries, scope, usable,
                                  payment_context=row['category'] == '酒店' and
                                  any(e.get('role') == 'payment' for e in scoped),
                                  all_payment_alternatives=True, budget=budget)
    if budget.exhausted:
        return []
    accepted = [r for r in results if not blocked_competitor(r) and not any(
        r is not other and selection(r) & selection(other) and selection(r) != selection(other) for other in results)]
    return [] if budget.exhausted else accepted


def selection(row: dict[str, Any]) -> set[str]:
    return {k.removeprefix('q:') for k in row['voucher_keys']} | set(row['payment_ids']) | set(row.get('support_ids', []))


def missing_questions(entries: list[dict[str, Any]], *,
                      context: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """State the missing fact; do not reword every problem as an amount issue."""
    context = entries if context is None else context
    roles = {str(e['role']) for e in context}
    invoices = [e for e in context if e['role'] == 'invoice']
    transport = {e['id']: transport_profile(e) for e in context}
    from invoice_print_layout.agent_content import goods_from_text
    price_fragments = [e for e in context if e['role'] == 'other' and
                       re.search(r'价格明细|实付款|总优惠', compact_text(str(e.get('text') or ''))) and
                       not goods_from_text(str(e.get('text') or ''), e.get('fields') or {})]
    fragment_reason = ('当前购买截图只显示金额和收货等片段，未包含可确认的商品名称及店铺；'
                       '请补充含商品区、店铺和订单状态的完整购买截图' +
                       ('，已有付款记录无需重复上传' if 'payment' in roles else ''))
    reasons: list[dict[str, Any]] = []
    for e in entries:
        role, category = e['role'], source_category(e)
        text = compact_text(str(e.get('text') or ''))
        reason = ''
        requires_source = False
        transport_issue = transport_missing_reason(e, context, transport) if role == 'invoice' else None
        if transport_issue:
            reasons.append(transport_issue)
            continue
        if (e in price_fragments or role == 'payment' and price_fragments and
                not roles.intersection({'invoice', 'receipt', 'purchase', 'order'})):
            reason = fragment_reason
            requires_source = True
        elif role == 'invoice' and not invoice_complete(e):
            reason = '这份发票缺少可确认的完整价税合计或票面信息，请补充完整发票，不能用截图片段猜金额'
            requires_source = True
        elif category == '打车' and role == 'invoice' and not roles.intersection({'trip', 'trip_sheet'}):
            reason = '打车发票还缺对应行程单，请补充完整行程单'
            requires_source = True
        elif role in {'trip', 'trip_sheet'} and 'invoice' not in roles:
            reason = '行程单还缺对应正式发票，请补充发票'
            requires_source = True
        elif category == '顺丰' and role == 'invoice' and 'detail' not in roles:
            reason = '快递发票还缺对应完整运单明细；请补充含运单或订单身份、承运方及总额的原件'
            requires_source = True
        elif role == 'detail' and 'invoice' not in roles:
            reason = '运单明细还缺对应正式发票，请补充发票'
            requires_source = True
        elif role in {'purchase', 'receipt', 'order'} and not roles.intersection({'payment', 'invoice'}):
            reason = '购买材料还缺对应付款截图或正式发票，请补充其中一种'
            requires_source = True
        elif role == 'payment' and invoices:
            displayed = str((e.get('fields') or {}).get('goods') or
                            (e.get('fields') or {}).get('merchant') or '').strip()
            parties = list(dict.fromkeys(str((v.get('fields') or {}).get('merchant') or '').strip()
                                        for v in invoices))
            parties = [p for p in parties if p]
            detail = ('付款记录显示「' + displayed[:100] + '」，' if displayed else '')
            detail += ('现有发票开票方为「' + '、'.join(p[:100] for p in parties[:3]) + '」；' if parties else '')
            reason = ('已收到本任务的发票，' + detail +
                      '付款与现有发票的商家、住宿名称或订单对应关系尚未确认；'
                      '请补充能证明对应关系的订单，或在人工工作台明确材料归属，无需重复上传已有发票')
        elif role == 'payment' and not roles.intersection({'invoice', 'receipt', 'purchase', 'order'}):
            observed = (e.get('local_payment_observations') or {}).get('fields') or {}
            purpose = str(observed.get('note') or (e.get('fields') or {}).get('goods') or '').strip()
            reason = (('付款原文已列用途「' + purpose[:120] + '」，尚缺对应购买或服务凭证；请补充凭证或在人工工作台处理')
                      if purpose else '只有付款记录，尚缺购买内容及购买凭证或正式发票；请补充材料和具体用途')
            requires_source = True
        elif role == 'invoice' and re.search(r'[*＊][^*＊]+[*＊](?:超市服务|日用百货)', text):
            reason = '发票仅写通用商品类别，无法确定具体购买内容；请补充购买明细及商家与付款的对应依据'
            requires_source = True
        if reason:
            reasons.append({'material_id': e['id'], 'reason': reason, 'requires_source': requires_source})
    if not reasons and entries:
        reasons.append({'material_id': entries[0]['id'],
                        'reason': '材料的商家、交易身份或合计仍无法唯一对应，请补充能证明同一笔费用的订单明细或对应关系'})
    return reasons
