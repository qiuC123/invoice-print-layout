"""A bounded human correction completes a fresh, locally analyzed group."""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.agent_confirmed_facts import restore
from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.workbench import ExpenseStore
from tests.test_agent_confirmed_facts import facts_env  # noqa: F401


RECEIPT_TEXT = ('收据\n品名 单位 金额\n数量 单价 十万千百十元角分\n打印+年\n合计人民币（大写）：难辨\n'
                '收款方：示例广告店\n收款人（盖章）：——')


def native_source(text: str) -> bytes:
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for n, line in enumerate(text.splitlines()):
            page.insert_text((30, 40 + n * 22), line, fontname='china-s', fontsize=11)
        return pdf.tobytes()


def fresh_group(env: Any) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    jobs, tools, project = env
    payloads = [native_source(RECEIPT_TEXT), *[native_source(
        '微信支付\n扫二维码付款-给示例广告店\n付款金额 ' + amount +
        '\n当前状态 支付成功\n转账单号 ' + number)
        for amount, number in [('11.00', '100010730120300101000000000001'),
                               ('3.00', '100010730120300101000000000002')]]]
    rows = [tools.intake.receive(f'neutral-{n}.pdf', payload, project_id=project)
            for n, payload in enumerate(payloads)]
    assert len({row['digest'] for row in rows}) == 3
    assert all(row['status'] == 'received' and row['fields'] == {} and row['text'] == '' for row in rows)
    statement = '用户确认这三份原件同一笔，合计14元；收据实际内容是打印和胶带，分类材料采购。'
    fact = restore(jobs, {'project_id': project, 'material_ids': [e['id'] for e in rows],
        'claim': 'material_content', 'statement': statement, 'amount_cents': 1400,
        'goods': ['打印', '胶带'], 'category': '材料采购', 'content_material_id': rows[0]['id'],
        'provenance': {'source': 'direct_user_reply', 'reference': 'synthetic explicit user correction',
                       'sha256': hashlib.sha256(statement.encode()).hexdigest()}})
    hashes = {e['path']: hashlib.sha256(Path(e['path']).read_bytes()).hexdigest() for e in rows}
    return rows, fact, hashes


def preview_stub(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    def preview(store: ExpenseStore, ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
        calls.append(ids)
        return {'total_cents': sum(store.get(i)['amount_cents'] for i in ids), 'page_count': 1,
                'pdf': 'synthetic-preview.pdf', 'xlsx': 'synthetic-preview.xlsx', 'preview_pages': []}
    monkeypatch.setattr('invoice_print_layout.report_print_preview.create_report_print_preview', preview)
    return calls


def diagnostic(done: dict[str, Any]) -> dict[str, Any]:
    return {'state': done['state'], 'message': done['message'], 'question': done.get('question'),
            'steps': [{'tool': r['tool'], 'status': r['result'].get('content_status'),
                       'reason': r['result'].get('reason'), 'tool_error': r['result'].get('tool_error')}
                      for r in done['last_results']]}


def test_human_confirmed_incomplete_receipt_three_originals_continue_through_normal_analysis_to_preview(
        facts_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, tools, project = facts_env
    rows, fact, hashes = fresh_group(facts_env)
    calls = preview_stub(monkeypatch)
    job = jobs.create(project, [e['id'] for e in rows], 'confirmed-flow-' + str(time.time_ns()))
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready', diagnostic(done)
    assert done['question'] is None and not done['confirmations'] and done['models'] == 0
    steps = [r['tool'] for r in done['last_results']]
    assert any(event.get('tool') == 'analyze_materials' for event in done['events'])
    assert 'ask_user' not in steps
    assert all(step in steps for step in ['create_draft', 'extract_goods', 'classify_expense', 'name_expense', 'build_preview'])
    assert done['preview']['total_cents'] == 1400 and len(calls) == 1
    items = tools.store.list_items()
    assert len(items) == 1
    item = items[0]
    assert item['amount_cents'] == 1400 and item['category'] == '材料采购'
    assert item['stage'] == 'draft' and item['verified'] is False
    assert len(item['attachments']) == 3
    with tools.store.connect() as db:
        attached_digests = {a['digest'] for a in db.execute('SELECT digest FROM attachments WHERE item_id=?', (item['id'],))}
    assert attached_digests == {e['digest'] for e in rows}
    assert '打印' in item['title'] and '胶带' in item['title']
    receipt = tools.intake.get(rows[0]['id'])
    assert receipt['fields']['amount'] == '14.00'
    assert receipt['fields']['goods'] == '打印、胶带'
    assert receipt['confirmed_content']['fact_id'] == fact['fact_id']
    assert '打印+年' in receipt['text'] and '14.00' not in receipt['text'] and '胶带' not in receipt['text']
    assert hashes == {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in hashes}
    # Polling/running the already final task cannot create another expense,
    # repeat the preview, or spend a fresh tool/model budget.
    again = AgentLoop(jobs).run(job['id'], tools)
    assert again['state'] == 'ready' and again['tools'] == done['tools'] and again['models'] == done['models']
    assert len(tools.store.list_items()) == 1 and len(calls) == 1


@pytest.mark.parametrize('mutation', ['actual_source_bytes', 'different_manual_amount'])
def test_confirmed_receipt_does_not_apply_after_actual_source_or_manual_amount_change(
        facts_env: Any, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    jobs, tools, project = facts_env
    rows, _, _ = fresh_group(facts_env)
    calls = preview_stub(monkeypatch)
    if mutation == 'actual_source_bytes':
        path = Path(rows[0]['path'])
        path.write_bytes(path.read_bytes() + b'\n% original changed after human confirmation\n')
    else:
        receipt = {**rows[0], 'fields': {'amount': '15.00'}, 'manual_fields': ['amount'],
                   'revision': rows[0]['revision'] + 1}
        with tools.store.connect() as db:
            tools.matching._write_queue(db, receipt)
    job = jobs.create(project, [e['id'] for e in rows], 'blocked-confirmed-flow-' + str(time.time_ns()))
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] != 'ready', diagnostic(done)
    assert not calls and not tools.store.list_items()
    receipt = tools.intake.get(rows[0]['id'])
    assert not receipt.get('confirmed_content')
    if mutation == 'different_manual_amount':
        assert receipt['fields']['amount'] == '15.00' and 'amount' in receipt['manual_fields']
