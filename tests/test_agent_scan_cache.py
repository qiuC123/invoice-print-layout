"""Caching removes repeated work without hiding changed source bytes or peers."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import digest
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.agent_tools import ModelPlanner
from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.project_registry import ProjectRegistry
from tests.test_invoice_agent import environment, material, proof


def legacy_fingerprint(tools: Any, job: dict[str, Any]) -> str:
    rows = [e for e in tools.intake.list() if e['project_id'] == job['project_id']]
    pool = [{k: e.get(k) for k in ('id', 'revision', 'status', 'item_id', 'digest')} for e in rows]
    with tools.store.connect() as db:
        expenses = [dict(r) for r in db.execute('SELECT id,data FROM expenses')
                    if json.loads(r['data']).get('project_id') == job['project_id']]
        attachments = [dict(r) for r in db.execute('SELECT id,item_id,role,digest FROM attachments')
                       if r['item_id'] in {e['id'] for e in expenses}]
    template = tools.store.root / '报销模板.xlsx'
    template_hash = hashlib.sha256(template.read_bytes()).hexdigest() if template.is_file() else ''
    return digest([tools.build_id, sorted(pool, key=lambda e: e['id']),
                   sorted(expenses, key=lambda e: e['id']), sorted(attachments, key=lambda e: e['id']), template_hash])


def test_project_sql_fingerprint_preserves_all_previous_dependency_fields(environment: Any) -> None:
    _, tools, project = environment
    peer = ProjectRegistry(tools.store.workspace / '后勤/tasks.sqlite3').create_project('Other synthetic')['id']
    current = material(tools, project, 'receipt', '17', 'current')
    material(tools, peer, 'payment', '19', 'foreign')
    for ident, owner in [('own-expense', project), ('peer-expense', peer)]:
        with tools.store.connect() as db:
            db.execute('INSERT INTO expenses(id,data) VALUES (?,?)',
                       (ident, json.dumps({'id': ident, 'project_id': owner, 'title': 'Synthetic'})))
    job = {'project_id': project}
    before = tools.fingerprint(job)
    assert before == legacy_fingerprint(tools, job)
    # A material outside this task is still part of the ambiguity snapshot.
    competitor = material(tools, project, 'payment', '17', 'competitor')
    assert tools.fingerprint(job) != before
    assert tools.fingerprint(job) == legacy_fingerprint(tools, job)
    with tools.store.connect() as db:
        data = tools.intake.get(competitor['id'])
        data['status'] = 'archived'
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data), competitor['id']))
    assert tools.fingerprint(job) == legacy_fingerprint(tools, job)
    assert tools.intake.get(current['id'])['id'] == current['id']


def test_queue_projection_cache_keeps_rules_and_deep_copies(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tools, project = environment
    entry = material(tools, project, 'invoice', '17', 'cache-original')
    calls = []
    def seller(self: Any, row: dict[str, Any]) -> str:
        calls.append(row['id'])
        return '合成工具商店'
    monkeypatch.setattr(MaterialMatching, '_native_invoice_seller', seller)
    first = tools.matching._queue_card(entry)
    first['fields']['merchant'] = 'caller-only modification'
    second = tools.matching._queue_card(entry)
    assert second['fields']['merchant'] == '合成工具商店'
    assert len(calls) == 1 and tools.matching.card_cache_hits == 1
    changed = copy.deepcopy(entry)
    changed['fields']['title'] = 'changed human description'
    assert tools.matching._queue_card(changed)['fields']['title'] == 'changed human description'
    assert len(calls) == 2


def test_queue_projection_never_reuses_cache_after_original_replacement(environment: Any,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _, tools, project = environment
    entry = material(tools, project, 'invoice', '17', 'cache-replacement')
    calls = []
    def seller(self: Any, row: dict[str, Any]) -> str:
        calls.append(row['id'])
        return '合成工具商店'
    monkeypatch.setattr(MaterialMatching, '_native_invoice_seller', seller)
    tools.matching._queue_card(entry)
    Path(entry['path']).write_bytes(proof('different original, same stored revision'))
    tools.matching._queue_card(entry)
    assert len(calls) == 2
    assert tools.matching.card_cache_hits == 0


def test_queue_projection_cache_is_bounded(environment: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _, tools, project = environment
    entry = material(tools, project, 'invoice', '17', 'bounded-cache')
    monkeypatch.setattr(MaterialMatching, '_native_invoice_seller', lambda self, row: '合成工具商店')
    for i in range(140):
        changed = copy.deepcopy(entry)
        changed['revision'] = i
        tools.matching._queue_card(changed)
    assert len(tools.matching._card_cache) == 128


def test_oversized_planner_context_requests_review_without_provider_call(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    planner = ModelPlanner(tmp_path)
    monkeypatch.setattr(planner.ai, 'public_status', lambda: {'available': True})
    def no_request(*args: Any, **kwargs: Any) -> None:
        pytest.fail('Oversized material context must not reach the provider')
    monkeypatch.setattr(planner.ai, '_request', no_request)
    assert planner.choose({'materials': 'x' * 60001}) == {'tool': 'ask_user', 'arguments': {}}


def test_same_price_typed_order_components_are_scripted_without_model(environment: Any) -> None:
    jobs, tools, project = environment
    entries = []
    for i in range(4):
        order = f'TESTORDER{i:08d}'
        for role in ['receipt', 'payment']:
            source = ('收据\n商品名称：鼠标\n实付：17.00' if role == 'receipt' else
                      '微信支付\n付款金额：17.00\n当前状态：支付成功')
            source += '\n商家名称：合成工具商店\n商家订单号：' + order
            entries.append(material(tools, project, role, '17.00', f'group-{i}-{role}', text=source))
    job = jobs.create(project, [entry['id'] for entry in entries], 'four-typed-orders')
    result = AgentLoop(jobs).run(job['id'], tools)
    assert result['state'] == 'ready'
    assert result['models'] == 0 and len(result['item_ids']) == 4
    assert sum(tools.store.get(ident)['amount_cents'] for ident in result['item_ids']) == 6800
    for ident in result['item_ids']:
        originals = tools.store.get(ident)['attachments']
        assert len(originals) == 2


def test_explicitly_missing_original_requests_source_without_model(environment: Any) -> None:
    jobs, tools, project = environment
    payment = material(tools, project, 'payment', '17', 'payment-without-purchase')
    job = jobs.create(project, [payment['id']], 'missing-source', use_model=True)
    result = AgentLoop(jobs).run(job['id'], tools, ModelPlanner(tools.store.root))
    assert result['state'] == 'needs_input' and result['models'] == 0
    assert not tools.store.list_items()
    assert '购买' in result['question']['text']
