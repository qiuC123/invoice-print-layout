"""Previously prepared materials receive one source-bound local completion pass."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop
from tests.test_invoice_agent import environment, proof, start


def prepared(env: Any, text: str, name: str) -> dict[str, Any]:
    _, tools, project = env
    entry = tools.intake.receive(name + '.pdf', proof(text), project_id=project)
    # New preparation always uses the real parser. Legacy cases below then
    # explicitly recreate the persisted old review shape they intend to test.
    return tools.intake.prepare_batch_entry(entry['id'])


def put(env: Any, entry: dict[str, Any]) -> None:
    with env[1].store.connect() as db:
        env[1].matching._write_queue(db, entry)


def pair(env: Any, *, payment: str = '付款金额：17.00\n当前状态：支付成功',
         purchase: str = '商品金额：17.00\n实付：17.00',
         legacy_review: bool = True) -> list[dict[str, Any]]:
    common = '\n商家名称：本地材料商店\n商家订单号：LOCALPREPARED735100'
    rows = [prepared(env, '收据\n商品名称：办公鼠标\n' + purchase + common, 'purchase'),
            prepared(env, '微信支付\n' + payment + common, 'payment')]
    if legacy_review:
        # A historical prepared payment could retain a payment-status result
        # while leaving its amount empty. Do not require today's improved
        # parser to reproduce that bug in order to test one-time recovery.
        old_payment = rows[1]
        old_payment['status'] = 'review'
        old_payment['fields']['amount'] = ''
        old_payment['fields']['payment_status'] = 'succeeded' if old_payment['role'] == 'payment' else 'uncertain'
        old_payment['issues'] = ['金额冲突']
        put(env, old_payment)
    return rows


def test_current_prepared_payment_has_amount_without_legacy_completion(environment: Any) -> None:
    jobs, tools, _ = environment
    rows = pair(environment, legacy_review=False)
    payment = rows[1]
    assert payment['status'] == 'review' and payment['role'] == 'payment'
    assert payment['fields']['amount'] == '17.00'
    assert payment['fields']['payment_status'] == 'succeeded'
    assert '金额冲突' not in payment['issues']
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 1700 and done['models'] == 0
    assert not any(event.get('tool') == 'analyze_materials' for event in done['events'])
    assert not tools.intake.get(payment['id']).get('agent_local_evidence')
    item = tools.store.get(done['item_ids'][0])
    assert len(item['attachments']) == 2 and not item['verified'] and item['stage'] == 'draft'


def test_legacy_prepared_payment_missing_amount_completes_to_unverified_preview(environment: Any) -> None:
    jobs, tools, _ = environment
    rows = pair(environment)
    assert rows[1]['status'] == 'review' and rows[1]['fields']['amount'] == ''
    assert rows[1]['issues'] == ['金额冲突']
    originals = {row['path']: hashlib.sha256(Path(row['path']).read_bytes()).hexdigest() for row in rows}
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0 and done['tools'] < 20
    assert done['preview']['total_cents'] == 1700 and len(done['item_ids']) == 1
    result = tools.store.get(done['item_ids'][0])
    assert result['amount_cents'] == 1700 and len(result['attachments']) == 2
    assert not result['verified'] and result['stage'] == 'draft'
    assert '鼠标' in result['title']
    recovered = tools.intake.get(rows[1]['id'])
    assert recovered['fields']['amount'] == '17.00'
    assert recovered['fields']['payment_status'] == 'succeeded'
    assert recovered['agent_local_evidence'][-1]['source_sha256'] == rows[1]['digest']
    assert sum(event.get('tool') == 'analyze_materials' and event['message'] == '本地识别材料'
               for event in done['events']) == 1
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == checksum
               for path, checksum in originals.items())


def test_prepared_purchase_unknown_amount_uses_conserved_components(environment: Any) -> None:
    jobs, tools, _ = environment
    rows = pair(environment, purchase='商品金额：20.00\n包装费：1.00\n配送费：0.00\n优惠：4.00\n实付：17.00')
    order = rows[0]
    order['fields']['amount'] = ''
    order['issues'] = ['金额冲突']
    put(environment, order)
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'ready' and done['preview']['total_cents'] == 1700 and done['models'] == 0
    recovered = tools.intake.get(order['id'])
    assert recovered['fields']['amount'] == '17.00'
    assert recovered['agent_amount_evidence'][-1]['previous_amount'] == ''


@pytest.mark.parametrize('payment', [
    '付款金额：17.00\n支付金额：19.00\n当前状态：支付成功',
    '付款金额：17.00\n当前状态：支付成功\n退款成功',
    '付款金额：17.00\n当前状态：等待付款',
])
def test_prepared_conflicting_or_unpaid_amounts_remain_incomplete_once(environment: Any, payment: str) -> None:
    jobs, tools, _ = environment
    rows = pair(environment, payment=payment)
    rows[1]['fields']['amount'] = ''
    rows[1]['issues'] = ['金额冲突']
    put(environment, rows[1])
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'needs_input' and not tools.store.list_items() and not done['preview']
    assert not tools.intake.get(rows[1]['id'])['fields'].get('amount')
    assert done['tools'] <= 4 and done['models'] == 0
    assert sum(event.get('tool') == 'analyze_materials' and event['message'] == '本地识别材料'
               for event in done['events']) <= 1


@pytest.mark.parametrize('kind', ['amount', 'manual_role', 'conflicts', 'legacy_manual'])
def test_prepared_manual_or_conflicting_fields_are_not_reparsed_or_overwritten(environment: Any, kind: str) -> None:
    jobs, tools, _ = environment
    rows = pair(environment)
    payment = rows[1]
    if kind == 'amount':
        payment['manual_fields'] = ['amount', 'payment_status']
        payment['fields'].update(amount='19.00', payment_status='uncertain')
    elif kind == 'manual_role':
        payment['manual_role'] = True
    elif kind == 'legacy_manual':
        payment['result'] = '人工补充材料信息，尚未关联费用'
    else:
        payment['field_conflicts'] = [{'field':'amount','local':'17.00','ai':'19.00'}]
    put(environment, payment)
    before = copy.deepcopy(tools.intake.get(payment['id']))
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'needs_input' and not tools.store.list_items()
    assert tools.intake.get(payment['id']) == before


def test_prepared_mismatching_purchase_components_do_not_become_a_guessed_total(environment: Any) -> None:
    jobs, tools, _ = environment
    rows = pair(environment, purchase='商品金额：20.00\n优惠：2.00\n实付：17.00')
    rows[0]['fields']['amount'] = ''
    rows[0]['issues'] = ['金额冲突']
    put(environment, rows[0])
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    assert done['state'] == 'needs_input' and not tools.store.list_items()
    assert not tools.intake.get(rows[0]['id'])['fields']['amount']
    assert done['tools'] <= 4


def test_prepared_revision_changed_after_start_is_rejected(environment: Any) -> None:
    jobs, tools, _ = environment
    rows = pair(environment)
    job = start(environment, rows)
    rows[1]['revision'] += 1
    rows[1]['manual_fields'] = ['amount']
    rows[1]['fields']['amount'] = '19.00'
    put(environment, rows[1])
    before = copy.deepcopy(tools.intake.get(rows[1]['id']))
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'failed' and not tools.store.list_items()
    assert tools.intake.get(rows[1]['id']) == before
