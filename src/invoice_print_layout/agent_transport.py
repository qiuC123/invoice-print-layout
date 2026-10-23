"""Native transport projections and source-bound relation candidates, without writes.

Document type/amount comes from the established PDF inspectors.  Platform
identity is separate: an anonymous same-city table or an unknown ride seller
can become a precise confirmation candidate, never an amount-only auto-match.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import pymupdf

from invoice_print_layout.agent_candidate_groups import known_cents, outside_competitor
from invoice_print_layout.courier import inspect_courier_pdf
from invoice_print_layout.documents import (
    _freight_evidence, compact_text, detect_provider, inspect_pdf, parse_invoice_number,
    PdfInspectionError,
)
from invoice_print_layout.models import DocumentKind, RideProvider
from invoice_print_layout.transaction_evidence import (
    extract_transaction_evidence, identifiers_for_record, identifier_key,
)


@dataclass(frozen=True)
class MailOrigin:
    """Server-built, attachment-bound provenance from the authoritative mail store.

    The caller must verify the persisted message row and file digest before
    constructing this value. JSON entry fields/mail_context/batch are ignored.
    """
    account_id: str
    mailbox: str
    uidvalidity: str
    uid: int
    message_id: str
    attachment_digest: str
    authority: str = 'mail_agent_messages'


def _same_trusted_mail(left: dict[str, Any], right: dict[str, Any],
                       origins: dict[str, MailOrigin] | None) -> bool:
    if origins is None:
        return False
    a, b = origins.get(str(left['id'])), origins.get(str(right['id']))
    if not isinstance(a, MailOrigin) or not isinstance(b, MailOrigin):
        return False
    for entry, origin in ((left, a), (right, b)):
        if (not all(isinstance(value, str) and value for value in (
                origin.account_id, origin.mailbox, origin.uidvalidity, origin.attachment_digest)) or
                not isinstance(origin.message_id, str) or not re.fullmatch(r'[0-9]{1,10}', origin.uidvalidity) or
                int(origin.uidvalidity) <= 0 or type(origin.uid) is not int or not 0 < origin.uid <= 2**32 - 1 or
                origin.authority not in {'mail_agent_messages', 'mail_imports.jsonl'} or
                origin.attachment_digest != entry.get('digest') or not source_current(entry)):
            return False
    return ((a.account_id, a.mailbox, a.uidvalidity, a.uid, a.message_id) ==
            (b.account_id, b.mailbox, b.uidvalidity, b.uid, b.message_id))


def mail_pair_evidence(left: dict[str, Any], right: dict[str, Any],
                       origins: dict[str, MailOrigin] | None) -> dict[str, Any]:
    """Retain the authority/attachment binding on a mail-supported proposal."""
    if not _same_trusted_mail(left, right, origins) or origins is None:
        return {}
    origin = origins[str(left['id'])]
    authorities = sorted({origins[str(e['id'])].authority for e in (left, right)})
    authority = authorities[0] if len(authorities) == 1 else 'mixed_mail_sources'
    return {'kind': 'trusted_same_mail', 'authority': authority, 'authorities': authorities,
            'account_id': origin.account_id, 'mailbox': origin.mailbox,
            'uidvalidity': origin.uidvalidity, 'uid': origin.uid, 'message_id': origin.message_id,
            'attachments': [{'material_id': str(e['id']), 'digest': str(e['digest'])} for e in (left, right)]}


def source_current(entry: dict[str, Any]) -> bool:
    """Keep legacy text callers, but never let cached text rescue changed bytes."""
    expected = str(entry.get('digest') or '')
    if not expected:
        return True
    try:
        return hashlib.sha256(Path(str(entry.get('path') or '')).read_bytes()).hexdigest() == expected
    except (ValueError, OSError):
        return False


def ride_service(text: str) -> bool:
    """Identify passenger service, not a carrier merely mentioned in a filename."""
    source = compact_text(text)
    return bool(_freight_evidence(text) is None and '铁路电子客票' not in source and
                (re.search(r'出租车|出租客运', source) or
                 '旅客运输服务' in source and re.search(r'[*＊][^*＊]+[*＊]客运服务费?', source) or
                 detect_provider(text) is not RideProvider.UNKNOWN and
                 re.search(r'[*＊][^*＊]+[*＊](?:客运服务|运输服务)', source)))


def _ride_identities(text: str) -> set[str]:
    # Keep the mature platform vocabulary. T3 is recognized only when printed
    # explicitly; no guessed 南京领行法人/brand alias is introduced here.
    source = compact_text(text).upper()
    found = {name for name, markers in {
        '享道': ('享道出行', '上汽享道', 'XIANGDAO'),
        '曹操': ('曹操出行', '吉利优行', 'CAOCAOTRAVEL', 'GEELYRIDE'),
        '滴滴': ('滴滴', 'DIDITRAVEL'),
        'T3': ('T3出行',),
    }.items() if any(marker in source for marker in markers)}
    return found


def _ride_identity(text: str) -> str:
    found = _ride_identities(text)
    return next(iter(found)) if len(found) == 1 else ''


def _pages_complete(texts: list[str]) -> bool:
    seen: list[tuple[int, int]] = []
    for text in texts:
        matches = re.findall(r'页码[:：]?(\d+)[/／](\d+)', compact_text(text))
        if len(matches) > 1:
            return False
        seen.extend((int(a), int(b)) for a, b in matches)
    return not seen or seen == [(i + 1, len(texts)) for i in range(len(texts))]


def transport_profile(entry: dict[str, Any]) -> dict[str, Any]:
    """Read an unchanged native original; never trust cached text as a new proof."""
    path = Path(str(entry.get('path') or ''))
    if path.suffix.lower() != '.pdf':
        return {}
    try:
        payload = path.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        expected = str(entry.get('digest') or '')
        if expected and actual != expected:
            return {}
        with pymupdf.open(stream=payload, filetype='pdf') as pdf:  # type: ignore[no-untyped-call]
            if pdf.needs_pass or not 0 < len(pdf) <= 10:
                return {}
            texts = [str(page.get_text()) for page in pdf]
        text = '\n'.join(texts)
        source = compact_text(text)
        if not source or not _pages_complete(texts):
            return {}
        base: dict[str, Any] = {'source_sha256': actual, 'text': text, 'page_count': len(texts)}
        # Courier's mature parser covers both SF-number tables and anonymous
        # same-city "帮我送" tables; it does not establish their relation.
        try:
            courier = inspect_courier_pdf(path)
        except (ValueError, OSError, RuntimeError):
            courier = None
        if courier is not None:
            if ('价税合计' in source) != courier.is_invoice:
                return {}
            if courier.is_invoice and not re.search(r'收派服务|快递(?:服务|运)?费|同城(?:配送|服务)费', source):
                # A carrier can also sell packaging or unrelated goods. Its
                # company name identifies the issuer, not the purchased service.
                return {}
            number = courier.invoice_number or ''
            if not number and '发票号码' in source:
                try:
                    number = parse_invoice_number(text)
                except PdfInspectionError:
                    return {}
            result = {**base, 'kind': 'courier_invoice' if courier.is_invoice else 'waybill',
                      'courier_kind': courier.kind, 'role': 'invoice' if courier.is_invoice else 'detail',
                      'amount_cents': int(courier.amount * 100), 'invoice_number': number,
                      'identity': '顺丰同城' if '顺丰同城' in source else '顺丰' if '顺丰' in source else ''}
        else:
            document = inspect_pdf(path)
            if document.kind is DocumentKind.INVOICE:
                if not ride_service(text):
                    return {}
                result = {**base, 'kind': 'ride_invoice', 'role': 'invoice',
                          'amount_cents': int(document.amount * 100),
                          'invoice_number': document.invoice_number or '', 'identity': _ride_identity(text),
                          'identity_ambiguous': len(_ride_identities(text)) > 1}
            else:
                from invoice_print_layout.ride_proofs import native_trip_pdf
                if not native_trip_pdf(path, actual):
                    return {}
                totals = {amount for amount in re.findall(
                    r'共(?:计)?\d+(?:笔|次|个|单)行程[，,;；。]?(?:合计|总计可开票金额)[¥￥]?(\d+(?:\.\d{1,2})?)元?', source)}
                if len(totals) > 1:
                    return {}
                result = {**base, 'kind': 'ride_trip', 'role': 'trip',
                          'amount_cents': int(document.amount * 100), 'invoice_number': '',
                          'identity': _ride_identity(text), 'identity_ambiguous': len(_ride_identities(text)) > 1}
        if result['amount_cents'] <= 0 or hashlib.sha256(path.read_bytes()).hexdigest() != actual:
            return {}
        return result
    except (ValueError, OSError, RuntimeError):
        return {}


def project_transport(entry: dict[str, Any]) -> dict[str, Any]:
    """Return role/fields/evidence for IntakeQueue's normal manual-field guard.

    Callers apply the projection through their existing analysis transaction;
    this function performs no queue, job, ledger, model or cache operations.
    """
    profile = transport_profile(entry)
    if not profile:
        return {}
    category = '打车' if profile['kind'].startswith('ride_') else '顺丰'
    fields: dict[str, Any] = {'category': category, 'amount': f"{profile['amount_cents'] / 100:.2f}"}
    if profile['invoice_number']:
        fields['invoice_number'] = profile['invoice_number']
    if profile['role'] in {'trip', 'detail'}:
        fields['title'] = ('网约车行程单' if profile['role'] == 'trip' else
                           '同城配送运单明细' if profile['courier_kind'] == '同城' else '顺丰运单明细')
    markers = ('行程单', '共', '运单起止日期', '运单明细', '客运服务', '收派服务',
               '价税合计', '小写', '发票号码', f"{profile['amount_cents'] / 100:.2f}")
    quotes = [row.strip() for row in profile['text'].splitlines() if any(word in row for word in markers)]
    return {'role': profile['role'], 'fields': fields, 'source_sha256': profile['source_sha256'],
            'evidence': {'method': 'native_transport', 'kind': profile['kind'],
                         'quotes': quotes[:12], 'platform_identity': profile['identity']}}


def identity_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """A relation answer cannot cure conflicting IDs in the same typed domain."""
    domains: list[dict[tuple[str, str], set[str]]] = []
    for entry in (left, right):
        values: dict[tuple[str, str], set[str]] = {}
        for ident in identifiers_for_record(entry):
            if key := identifier_key(ident):
                kind, namespace, value = json.loads(key)
                values.setdefault((kind, namespace), set()).add(value)
        domains.append(values)
    a, b = domains
    return any(a[domain] != b[domain] for domain in a.keys() & b.keys())


def _native_id_keys(entry: dict[str, Any], profile: dict[str, Any]) -> set[str]:
    """Read typed identifiers again from both originals, never injected metadata.

    A printed transport brand can scope an explicitly labelled merchant order.
    Generic 订单号 remains untyped; payment IDs retain their channel namespace.
    """
    keys = set()
    for ident in extract_transaction_evidence(profile['text'], str(entry.get('role') or ''))['identifiers']:
        if ident['kind'] == 'merchant_order' and not ident['namespace'] and profile['identity']:
            ident = {**ident, 'namespace': profile['identity']}
        if key := identifier_key(ident):
            keys.add(key)
    return keys


def pair_status(invoice: dict[str, Any], support: dict[str, Any],
                profiles: dict[str, dict[str, Any]] | None = None, *,
                trusted_mail_origins: dict[str, MailOrigin] | None = None) -> str:
    """Return supported/needs_fact/empty; equal amounts never establish identity."""
    a = profiles[invoice['id']] if profiles is not None else transport_profile(invoice)
    b = profiles[support['id']] if profiles is not None else transport_profile(support)
    if (not a or not b or known_cents(invoice) != a['amount_cents'] or
            known_cents(support) != b['amount_cents'] or a['amount_cents'] != b['amount_cents'] or
            identity_conflict(invoice, support) or a.get('identity_ambiguous') or b.get('identity_ambiguous')):
        return ''
    if any((entry.get('fields') or {}).get('invoice_number') and profile['invoice_number'] and
           str(entry['fields']['invoice_number']) != profile['invoice_number']
           for entry, profile in ((invoice, a), (support, b))):
        return ''
    if b['invoice_number'] and a['invoice_number'] != b['invoice_number']:
        return ''
    native = [_native_id_keys(entry, profile) for entry, profile in ((invoice, a), (support, b))]
    domains = [{tuple(json.loads(key)[:2]) for key in keys} for keys in native]
    for domain in domains[0] & domains[1]:
        values = [{json.loads(key)[2] for key in keys if tuple(json.loads(key)[:2]) == domain}
                  for keys in native]
        if values[0] != values[1]:
            return ''
    valid_kind = (a['kind'] == 'ride_invoice' and b['kind'] == 'ride_trip' or
                  a['kind'] == 'courier_invoice' and b['kind'] == 'waybill' and
                  a['courier_kind'] == b['courier_kind'])
    if not valid_kind or a['identity'] and b['identity'] and a['identity'] != b['identity']:
        return ''
    if b['invoice_number'] or native[0] & native[1]:
        return 'supported'
    if a['identity'] and b['identity']:
        return 'supported'
    return 'supported' if _same_trusted_mail(invoice, support, trusted_mail_origins) else 'needs_fact'


def pair_priority_evidence(invoice: dict[str, Any], support: dict[str, Any],
                           profiles: dict[str, dict[str, Any]], *,
                           trusted_mail_origins: dict[str, MailOrigin] | None = None) -> dict[str, Any]:
    """Rank only valid complete pairs; retain why the priority was established."""
    if pair_status(invoice, support, profiles, trusted_mail_origins=trusted_mail_origins) != 'supported':
        return {}
    shared = _native_id_keys(invoice, profiles[invoice['id']]) & _native_id_keys(support, profiles[support['id']])
    number = profiles[support['id']]['invoice_number']
    mail = mail_pair_evidence(invoice, support, trusted_mail_origins)
    rank = 3 if shared or number else 2 if mail else 1
    context = mail or {'kind': 'native_transport_relation',
                      'attachments': [{'material_id': str(e['id']), 'digest': str(e.get('digest') or '')}
                                      for e in (invoice, support)]}
    quotes = []
    for entry in (invoice, support):
        profile = profiles[entry['id']]
        markers = [value for value in [profile['identity'], number,
                   *(json.loads(key)[2] for key in shared)] if value]
        rows = [line.strip() for line in profile['text'].splitlines() if line.strip()]
        excerpts = ['\n'.join(rows[max(0, index - 1):index + 1]) for index, line in enumerate(rows)
                    if any(marker in compact_text(line) for marker in markers)]
        quotes.append({'material_id': str(entry['id']), 'quotes': list(dict.fromkeys(excerpts))})
    return {**context, 'priority_rank': rank,
            'priority_basis': 'shared_business_identity' if rank == 3 else
                              'trusted_same_mail' if rank == 2 else 'same_provider',
            'shared_business_ids': [json.loads(key) for key in sorted(shared)],
            'detail_invoice_number': number,
            'source_quotes': quotes}


def transport_missing_reason(invoice: dict[str, Any], context: list[dict[str, Any]],
                             profiles: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Describe existing support accurately; missing identity is not missing upload."""
    a = profiles.get(invoice['id']) or {}
    if a.get('kind') not in {'ride_invoice', 'courier_invoice'}:
        return None
    wanted = 'ride_trip' if a['kind'] == 'ride_invoice' else 'waybill'
    label = '行程单' if wanted == 'ride_trip' else '运单明细'
    support = [e for e in context if e['id'] != invoice['id'] and
               (profiles.get(e['id']) or {}).get('kind') == wanted]
    if not support:
        return None
    matching = [e for e in support if (profiles[e['id']]['amount_cents'] == a['amount_cents'])]
    reason = ''
    hard = False
    if not matching:
        amounts = '、'.join(sorted({f"{profiles[e['id']]['amount_cents'] / 100:.2f}" for e in support}))
        reason = f"已收到{label}，但其总额{amounts}元与发票{a['amount_cents'] / 100:.2f}元不符；请核对对应原件，不能改写票面金额配对"
        hard = True
    elif any(pair_status(invoice, e, profiles) == 'needs_fact' for e in matching):
        seller = str((invoice.get('fields') or {}).get('merchant') or '票面服务方')[:100]
        names = '、'.join(sorted({profiles[e['id']]['identity'] or '未标承运方' for e in matching}))
        reason = (f"已收到{a['amount_cents'] / 100:.2f}元发票和同额{label}；发票开票方为「{seller}」，"
                  f"{label}显示「{names}」，原件未显示共有订单或对应发票身份。请确认这两份原件的同笔对应关系，无需重复上传现有{label}")
    elif all(not pair_status(invoice, e, profiles) for e in matching):
        reason = f"已收到同额{label}，但服务方、对应发票号码或同域订单身份存在差异，不能仅凭同额确认；请检查对应原件"
        hard = True
    if not reason:
        return None
    return {'material_id': invoice['id'], 'reason': reason, 'requires_source': hard}


def transport_relation_candidates(entries: list[dict[str, Any]], states: dict[str, dict[str, Any]],
                                  facts: dict[str, dict[str, Any]], *,
                                  scope: set[str] | None = None,
                                  trusted_mail_origins: dict[str, MailOrigin] | None = None) -> list[dict[str, Any]]:
    """Expose complete transport pairs lacking identity as normal fact questions.

    The relation engine remains responsible for source snapshots, receipts and
    confirmation. No model may turn these needs_fact candidates into support.
    """
    from invoice_print_layout.agent_combinations import invoice_complete, selection
    eligible = [e for e in entries if (scope is None or e['id'] in scope) and e['id'] in states and
                not states[e['id']].get('name_conflict') and e['id'] in facts and
                facts[e['id']].get('complete') and not any(facts[e['id']].get(k) for k in
                ('issues', 'goods_issues', 'source_text_truncated', 'goods_text_truncated'))]
    if len(eligible) > 40:
        return []
    profiles = {e['id']: transport_profile(e) for e in eligible}
    usable = {e['id'] for e in eligible}
    found = []
    for invoice in eligible:
        if invoice.get('role') != 'invoice' or not invoice_complete(invoice):
            continue
        a = profiles[invoice['id']]
        if not a or a['kind'] not in {'ride_invoice', 'courier_invoice'}:
            continue
        for support in eligible:
            if (support.get('role') not in {'trip', 'trip_sheet', 'detail'} or
                    pair_status(invoice, support, profiles, trusted_mail_origins=trusted_mail_origins) != 'needs_fact'):
                continue
            category = '打车' if a['kind'] == 'ride_invoice' else '顺丰'
            identity = {'voucher_keys': ['q:' + invoice['id']], 'payment_ids': [],
                        'support_ids': [support['id']], 'category': category,
                        'material_basis': 'standard', 'amount_cents': a['amount_cents']}
            if outside_competitor(identity, entries, scope, usable):
                continue
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()[:24]
            found.append({**identity, 'id': key, 'strength': 'needs_fact', 'relation_kind': 'source_inference',
                          'anchors': ['已收到完整发票与行程/运单明细且总额一致，金额只计一次',
                                      '原件未显示足以证明两份材料同笔的服务方或共有订单身份；同额仅支持候选'],
                          'duplicate_views': []})
    if len(found) > 24:
        return []
    for row in found:
        row['competing'] = any(selection(row) & selection(other) and selection(row) != selection(other)
                               for other in found if other is not row)
    return found
