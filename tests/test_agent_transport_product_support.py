"""Native transport details cover service evidence without inventing products."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any

import pytest
import pymupdf

from invoice_print_layout.agent_evidence import material_facts
from invoice_print_layout.agent_product_rules import inspect_product_rules
from invoice_print_layout.agent_product_support import supporting_product_sources
from tests.test_agent_transport import bill, entry


def fixture(tmp_path: Path, kind: str = 'ride', *, extra: str = '', amount: str = '30.00',
            confirmed: bool = False, anonymous: bool = False
            ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    courier = kind == 'city'
    seller = '顺丰同城测试有限公司' if courier else '合成客运服务有限公司' if anonymous else '享道出行'
    invoice = entry(tmp_path, 'invoice', 'invoice', bill(seller, '30.00', courier=courier), '30.00', seller)
    text = ('运单起止日期：2030-01-01至2030-01-01\n共1笔运单，合计' + amount + '元\n'
            '序号\n订单类型\n下单时间\n发件信息\n收件信息\n订单号\n金额\n支付方式\n'
            '1\n帮我送\n01-01 周二\n上海市静安区\n起点甲\n上海市徐汇区\n终点乙\n'
            '8547695660999\n' + amount + '元\n在线支付\n页码：1 / 1' if courier else
            '腾讯出行服务—打车——行程单\n序号\n服务商\n车型\n上车时间\n城市\n起点\n终点\n金额\n(元)\n'
            '1\n享道出行\n经济型\n2030-01-01 12:00\n苏州市\n起点甲\n终点乙\n' + amount +
            '\n共1个行程，合计' + amount + '元\n页码：1 / 1') + extra
    support = entry(tmp_path, 'support', 'detail' if courier else 'trip', text, amount)
    entries = [invoice, support]
    for row in entries:
        row['project_id'] = 'synthetic-project'
    facts = [material_facts(row) for row in entries]
    goods = [{**row, 'material_id': invoice['id'], 'sha256': invoice['digest']} for row in facts[0]['goods']]
    if courier:
        # ContentTools retains this complete source-qualified service name
        # alongside its concise legacy display name.
        for good in goods:
            good['raw_name'] = '收派服务费'
    def ref(row: dict[str, Any]) -> dict[str, Any]:
        return {'key': 'q:' + row['id'], 'queue_id': row['id'], 'role': row['role'],
                'amount_cents': facts[entries.index(row)]['total_cents']}
    item = {'id': 'synthetic-draft', 'project_id': 'synthetic-project', 'stage': 'draft', 'verified': False,
            'amount_cents': 3000, 'material_match': {'recognition': {
                'amount_source': 'vouchers', 'amount_cents': 3000, 'voucher_total_cents': 3000,
                'amount_difference_cents': 0, 'payments': [], 'vouchers': [ref(invoice)], 'supports': [ref(support)]}},
            'agent_relation': {'source_context': {'kind': 'native_transport_relation', 'attachments': [
                {'material_id': row['id'], 'digest': row['digest']} for row in entries]}}}
    if confirmed or courier:
        item['agent_relation'] = {'relation_kind': 'user_confirmed', 'confirmation_id': 'synthetic-question',
            'source_fact': {'claim': 'same_expense', 'actor': 'user', 'action': 'confirm_relation',
                            'fact_id': 'synthetic-question', 'amount_cents': 3000,
                            'material_digests': [row['digest'] for row in entries]}}
    return item, facts, goods, entries


@pytest.mark.parametrize('kind,confirmed', [('ride', False), ('ride', True), ('city', True)])
def test_source_bound_complete_native_service_detail_is_supplementary(
        tmp_path: Path, kind: str, confirmed: bool) -> None:
    item, facts, goods, entries = fixture(tmp_path, kind, confirmed=confirmed)
    before = deepcopy((item, facts, goods, entries))
    plan = supporting_product_sources(item, facts, goods, {}, source_entries=entries)
    assert [source['id'] for source in plan['sources']] == ['invoice']
    assert plan['supplementary'][0]['material_id'] == 'support'
    assert plan['supplementary'][0]['sha256'] == entries[1]['digest']
    assert plan['supplementary'][0]['amount_cents'] == 3000
    assert plan['supplementary'][0]['quote'].strip() == entries[1]['text']
    checked = inspect_product_rules(goods, plan['sources'], plan['source_categories'])
    assert checked['status'] == 'exact' and checked['coverage_complete']
    assert checked['category'] == ('顺丰' if kind == 'city' else '打车')
    assert (item, facts, goods, entries) == before


@pytest.mark.parametrize('kind', ['ride', 'city'])
@pytest.mark.parametrize('extra', ['\n商品名称：鼠标蛋糕', '\n*服务*安装费', '\n未知仪器', '\n共1个行程，合计31.00元'])
def test_native_service_detail_never_hides_extra_source_text(tmp_path: Path, kind: str, extra: str) -> None:
    item, facts, goods, entries = fixture(tmp_path, kind, extra=extra)
    plan = supporting_product_sources(item, facts, goods, {}, source_entries=entries)
    assert not plan['supplementary'] and len(plan['sources']) == 2


@pytest.mark.parametrize('mutation', ['missing_entry', 'changed_bytes', 'source_scope', 'project',
    'source_truncated', 'goods_truncated', 'incomplete_source', 'source_issue', 'manual_conflict',
    'unknown_support_goods', 'unconfirmed_relation', 'fake_native_context', 'wrong_fact_scope',
    'wrong_fact_actor', 'wrong_confirmation', 'verified', 'wrong_recognition', 'source_revision'])
def test_native_service_source_identity_and_current_draft_guards(tmp_path: Path, mutation: str) -> None:
    item, facts, goods, entries = fixture(tmp_path, confirmed=True)
    if mutation == 'missing_entry': entries.pop()
    elif mutation == 'changed_bytes': Path(entries[1]['path']).write_bytes(b'changed source')
    elif mutation == 'source_scope': entries[1]['digest'] = 'a' * 64
    elif mutation == 'project': entries[1]['project_id'] = 'foreign-project'
    elif mutation == 'source_truncated': facts[1]['source_text_truncated'] = True
    elif mutation == 'goods_truncated': facts[1]['goods_text_truncated'] = True
    elif mutation == 'incomplete_source': facts[1]['complete'] = False
    elif mutation == 'source_issue': facts[1]['issues'] = ['原件额外页未读取']
    elif mutation == 'manual_conflict': entries[1]['field_conflicts'] = [{'field': 'amount'}]
    elif mutation == 'unknown_support_goods': facts[1]['goods'] = [{'name': '未知仪器'}]
    elif mutation == 'unconfirmed_relation': item['agent_relation'] = {}
    elif mutation == 'fake_native_context':
        item['agent_relation'] = {'source_context': {'kind': 'native_transport_relation', 'attachments': []}}
    elif mutation == 'wrong_fact_scope': item['agent_relation']['source_fact']['material_digests'] = ['b' * 64]
    elif mutation == 'wrong_fact_actor': item['agent_relation']['source_fact']['actor'] = 'model'
    elif mutation == 'wrong_confirmation': item['agent_relation']['confirmation_id'] = 'foreign-question'
    elif mutation == 'verified': item['verified'] = True
    elif mutation == 'source_revision': entries[1]['revision'] += 1
    else: item['material_match']['recognition']['amount_cents'] = 3100
    plan = supporting_product_sources(item, facts, goods, {}, source_entries=entries)
    assert not plan['supplementary'] and len(plan['sources']) == 2


def test_native_service_different_actual_amount_cannot_use_human_relation(tmp_path: Path) -> None:
    item, facts, goods, entries = fixture(tmp_path, amount='31.00', confirmed=True)
    assert not supporting_product_sources(item, facts, goods, {}, source_entries=entries)['supplementary']


@pytest.mark.parametrize('confirmed', [False, True])
def test_same_amount_anonymous_service_still_requires_exact_human_fact(tmp_path: Path, confirmed: bool) -> None:
    item, facts, goods, entries = fixture(tmp_path, confirmed=confirmed, anonymous=True)
    plan = supporting_product_sources(item, facts, goods, {}, source_entries=entries)
    assert bool(plan['supplementary']) is confirmed


def test_current_extra_original_page_is_not_hidden_by_cached_complete_facts(tmp_path: Path) -> None:
    item, facts, goods, entries = fixture(tmp_path, confirmed=True)
    path = Path(entries[1]['path'])
    with pymupdf.open(path) as pdf:
        page = pdf.new_page()
        page.insert_text((30, 30), '商品名称：未知仪器', fontname='china-s', fontsize=10)
        payload = pdf.tobytes()
    path.write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    entries[1]['digest'] = facts[1]['sha256'] = checksum
    item['agent_relation']['source_fact']['material_digests'] = [row['digest'] for row in entries]
    assert not supporting_product_sources(item, facts, goods, {}, source_entries=entries)['supplementary']


def test_extra_invoice_product_is_not_removed_with_the_transport_detail(tmp_path: Path) -> None:
    item, facts, goods, entries = fixture(tmp_path, confirmed=True)
    facts[0]['source_text'] += '\n商品名称：鼠标蛋糕'
    facts[0]['goods'].append({'name': '鼠标蛋糕', 'quote': '商品名称：鼠标蛋糕', 'method': 'native_text'})
    assert not supporting_product_sources(item, facts, goods, {}, source_entries=entries)['supplementary']
