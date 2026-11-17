"""Synthetic-only acceptance for nonblocking organizing and legacy completion."""
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, AgentService
from invoice_print_layout.agent_transport import pair_status, project_transport
from invoice_print_layout.agent_review import leave_for_review
from tests.test_invoice_agent import environment, material, start
from tests.test_invoice_agent_content import draft
from tests.test_agent_transport import entry, bill, trip


def test_content_question_keeps_draft_and_processes_rest(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', amount, marker, text=
                       '收据\n商家名称：合成工具商店\n商品名称：' + goods + '\n实付：' + amount)
               for amount, marker, goods in [('17', 'unknown', '未知合成用品'), ('19', 'known', '胶带')]]
    entries += [material(tools, project, 'payment', amount, 'pay-' + amount) for amount in ['17', '19']]
    job = start(environment, entries)
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready', done
    assert len(done['item_ids']) == 2 and done['question'] is None
    assert done['message'] == '已生成 2 笔费用，0 份材料留在材料合成台'
    items = tools.store.get_many(done['item_ids'])
    flagged = [i for i in items if i.get('agent_review_reason')]
    assert len(flagged) == 1 and flagged[0]['pending_review']
    from invoice_print_layout.automatic_category import AutomaticCategory
    import threading
    assert not AutomaticCategory(tools.store, threading.Lock()).eligible(flagged[0], None)
    assert any('类别或商品明细待核对：' in m for m in flagged[0]['missing'])
    assert any(i['category'] == '材料采购' and not i.get('agent_review_reason') for i in items)
    assert AgentLoop(jobs).run(job['id'], tools) == done
    assert len(tools.store.list_items()) == 2


def test_relation_question_leaves_originals_and_completes_other_group(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'payment', '9', 'orphan'),
               material(tools, project, 'receipt', '17', 'known'),
               material(tools, project, 'payment', '17', 'known-payment')]
    before = {e['path']: Path(e['path']).read_bytes() for e in entries}
    done = AgentLoop(jobs).run(start(environment, entries)['id'], tools)
    assert done['state'] == 'ready' and len(done['item_ids']) == 1
    assert done['remaining_material_count'] == 1
    leftover = tools.intake.get(entries[0]['id'])
    assert not leftover.get('item_id') and leftover['agent_review_reason']
    assert tools.matching._queue_card(leftover)['agent_review_reason']
    assert all(Path(p).read_bytes() == payload for p, payload in before.items())


@pytest.mark.parametrize('content_question', [False, True])
def test_startup_finishes_legacy_waiting_once_without_new_expenses(environment: Any, content_question: bool) -> None:
    jobs, tools, project = environment
    if content_question:
        job, ident = draft(environment)
        with tools.store.connect() as db:
            item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
            item['agent_content'] = {'status': 'needs_input', 'reason': '合成商品待确认'}
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(item), ident))
        question = {'type': 'expense_content', 'text': '合成商品待确认', 'material_ids': job['material_ids']}
        jobs.update(job['id'], item_ids=[ident])
    else:
        rows = [material(tools, project, 'payment', '9', 'legacy-orphan')]
        job = start(environment, rows)
        question = {'type': 'material_relation', 'text': '合成对应关系不明', 'material_ids': job['material_ids']}
    jobs.stop(job['id'], 'needs_input', question['text'])
    jobs.update(job['id'], question=question)
    originals = {e['path']: Path(e['path']).read_bytes() for e in tools.intake.list()}
    item_count = len(tools.store.list_items())
    service = AgentService(tools.store)
    done = service.jobs.get(job['id'])
    assert done['state'] == 'ready' and done['question'] is None
    snapshot = copy.deepcopy(done)
    again = AgentService(tools.store)
    assert again.jobs.get(job['id']) == snapshot
    assert leave_for_review(jobs, job['id'], question, finish=True) == snapshot
    assert len(tools.store.list_items()) == item_count
    assert len(tools.intake.list()) == len(originals)
    assert all(Path(p).read_bytes() == payload for p, payload in originals.items())
    if content_question:
        assert tools.store.get(ident)['agent_review_reason'] == '合成商品待确认'
    else:
        assert tools.intake.get(job['material_ids'][0])['agent_review_reason'] == question['text']
    service.close(); again.close()


@pytest.mark.parametrize('seller', ['苏州市吉利优行电子科技有限公司', '吉利优行', '曹操出行'])
def test_caocao_passenger_invoice_matches_native_trip(tmp_path: Path, seller: str) -> None:
    text = bill(seller, '17.34').replace('旅客运输服务\n*交通运输服务*客运服务费', '*运输服务*出租汽车客运')
    invoice = entry(tmp_path, 'invoice', 'invoice', text, '17.34')
    support = entry(tmp_path, 'trip', 'trip', trip('曹操出行', '17.34'), '17.34')
    assert project_transport(invoice)['fields']['category'] == '打车'
    assert pair_status(invoice, support) == 'supported'


@pytest.mark.parametrize('product', ['出租汽车客运', '客运服务', '客运服务费'])
def test_native_ride_pair_classifies_duplicate_service_without_model(environment: Any, tmp_path: Path, product: str) -> None:
    jobs, tools, project = environment
    rows = native_ride_rows(environment, tmp_path, product)
    suggestions = tools.matching.suggest(project)
    assert suggestions['suggestion_count'] == 1, suggestions
    assert suggestions['suggestions'][0]['support_ids'] == [rows[1]['id']]
    done = AgentLoop(jobs).run(start(environment, rows, use_model=True)['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0, done
    assert len(done['item_ids']) == 1 and done['remaining_material_count'] == 0, done
    item = tools.store.get(done['item_ids'][0])
    assert item['category'] == '打车' and not item.get('agent_review_reason'), item['agent_content']
    assert item['agent_content']['status'] == 'ready'


def native_ride_rows(environment: Any, tmp_path: Path, product: str = '出租汽车客运') -> list[dict[str, Any]]:
    _, tools, project = environment
    text = bill('苏州市吉利优行电子科技有限公司', '17.34').replace(
        '旅客运输服务\n*交通运输服务*客运服务费', '*运输服务*' + product)
    native = [entry(tmp_path, 'native-invoice', 'invoice', text, '17.34'),
              entry(tmp_path, 'native-trip', 'trip', trip('曹操出行', '17.34'), '17.34')]
    rows = []
    for e in native:
        q = tools.intake.receive(Path(e['path']).name, Path(e['path']).read_bytes(), project_id=project)
        q.update(status='review', role=e['role'], text=e['text'], revision=1,
                 fields={**e['fields'], 'category':'打车', 'merchant': '苏州市吉利优行电子科技有限公司' if e['role']=='invoice' else '曹操出行'})
        with tools.store.connect() as db:
            tools.matching._write_queue(db, q)
        rows.append(q)
    return rows


@pytest.mark.parametrize('batch', [False, True])
def test_ride_suggestion_confirms_with_trip_and_replays_once(environment: Any, tmp_path: Path, batch: bool) -> None:
    _, tools, project = environment
    rows = native_ride_rows(environment, tmp_path)
    candidate = tools.matching.suggest(project)['suggestions'][0]
    fields = {'project_type':'transport', 'project_name':'打的(出租车、滴滴等)', 'amount':'17.34'}
    if batch:
        payload = {'suggestion_id':candidate['id'], 'snapshot_token':candidate['snapshot_token'],
                   'support_ids':candidate['support_ids'], 'fields':fields}
        result = tools.matching.create_batch(project, [payload], 'synthetic-ride-batch')
        again = tools.matching.create_batch(project, [payload], 'synthetic-ride-batch')
        assert [i['id'] for i in again['items']] == [i['id'] for i in result['items']]
        item = result['items'][0]
    else:
        result = tools.matching.create_expense(candidate['voucher_keys'], [], project,
                    support_ids=candidate['support_ids'], revisions=candidate['revisions'], fields=fields,
                    suggestion_id=candidate['id'], suggestion_token=candidate['snapshot_token'])
        item = result['item']
    assert len(tools.store.list_items()) == 1 and len(item['attachments']) == 2
    assert item['amount_cents'] == 1734 and item['category'] == '打车' and not item['verified']
    assert all(tools.intake.get(e['id'])['item_id'] == item['id'] for e in rows)


def test_ride_suggestion_cannot_omit_or_replace_trip(environment: Any, tmp_path: Path) -> None:
    _, tools, project = environment
    native_ride_rows(environment, tmp_path)
    candidate = tools.matching.suggest(project)['suggestions'][0]
    with pytest.raises(ValueError, match='材料选择'):
        tools.matching.create_expense(candidate['voucher_keys'], [], project,
            revisions=candidate['revisions'], fields={'project_type':'transport', 'project_name':'打的(出租车、滴滴等)', 'amount':'17.34'},
            suggestion_id=candidate['id'], suggestion_token=candidate['snapshot_token'])
    assert not tools.store.list_items()
