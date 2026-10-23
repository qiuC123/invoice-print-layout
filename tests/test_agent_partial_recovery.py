"""Real legacy partial hotel drafts accept facts without a second expense."""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, Jobs, digest, encoded
from invoice_print_layout.agent_partial_recovery import PartialRecovery
from invoice_print_layout.agent_tools import InvoiceTools
from tests.test_invoice_agent import environment
from tests.test_invoice_agent_combinations import entry, invoice_text


@pytest.fixture
def legacy_partial(environment: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    jobs, tools, project = environment
    invoice = entry(environment, 'invoice', '1353.00', invoice_text('住宿服务'), shop='合成佳缘宾馆')
    payments = [entry(environment, 'payment', amount, '支付成功\n商品说明 合成佳缘宾馆',
                      shop='合成平台收款方') for amount in ['124.00', '154.00', '154.00', '921.00']]
    rows = [invoice, *payments]
    job = jobs.create(project, [e['id'] for e in rows], 'legacy-partial-hotel')
    job = jobs.update(job['id'], state='running', run_started=time.time())
    proposal = next(p for p in tools._scan(job)['proposals'] if not p['payment_ids'])
    valid = tools.execute('validate_group', {k: proposal.get(k, []) for k in
                          ['voucher_keys', 'payment_ids', 'support_ids']}, job)
    job = jobs.update(job['id'], last_results=[{'tool': 'validate_group', 'result': valid}])
    created = tools.execute('create_draft', {'proposal_id': proposal['id']}, job)
    job = jobs.update(job['id'], item_ids=[created['item_id']])
    for tool in ['extract_goods', 'classify_expense', 'name_expense']:
        arguments = {'item_id': created['item_id']}
        result = tools.execute(tool, arguments, job)
        job = jobs.update(job['id'], seen_results=[*job['seen_results'], digest([tool, arguments, result])])
    job = jobs.update(job['id'], models=1, tools=11, elapsed=46.0,
                      question={'text': '旧版未确认对应关系', 'choices': [],
                                'snapshot': tools.fingerprint(job), 'material_ids': [p['id'] for p in payments]})
    job = jobs.stop(job['id'], 'needs_input', '旧版单票已有草稿，付款尚待确认')
    monkeypatch.setattr(tools.relations.ai, '_request', lambda *a, **k: pytest.fail('No new model after a fact answer'))
    return jobs, tools, job, rows, created['item_id']


def approve(env: tuple[Any, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
    jobs, tools, job, _, _ = env
    waiting = jobs.refresh_question(job['id'], job['updated_at'], tools.refresh_question)
    question = waiting['question']
    choice = next(c for c in question['choices'] if c['action'] == 'confirm_relation')
    return jobs.answer(job['id'], choice['id'], question['id']), waiting


def row_counts(tools: InvoiceTools) -> tuple[int, int, int]:
    with tools.store.connect() as db:
        return tuple(db.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in
                     ['expenses', 'attachments', 'invoice_agent_effects'])  # type: ignore[return-value]


def test_actual_legacy_content_changes_can_confirm_and_finish_same_job(legacy_partial: Any) -> None:
    jobs, tools, original, rows, item_id = legacy_partial
    item_before = tools.store.get(item_id)
    assert len(item_before['attachments']) == 1
    resumed, waiting = approve(legacy_partial)
    assert waiting['state'] == 'needs_input' and not waiting['confirmations']
    assert len(waiting['question']['material_refs']) == 5
    assert {r['digest'] for r in waiting['question']['material_refs']} == {r['digest'] for r in rows}
    assert resumed['models'] == original['models'] == 1
    assert resumed['tools'] == original['tools'] == 11
    assert resumed['elapsed'] >= original['elapsed']
    done = AgentLoop(jobs).run(original['id'], tools)
    assert done['id'] == original['id'] and done['state'] == 'ready'
    assert done['models'] == 1 and 11 < done['tools'] <= done['limits']['tools']
    assert done['elapsed'] >= original['elapsed'] and done['elapsed'] < done['limits']['seconds']
    assert done['preview']['total_cents'] == 135300 and done['item_ids'] == [item_id]
    item = tools.store.get(item_id)
    assert len(tools.store.list_items()) == 1 and item['amount_cents'] == 135300
    assert item['category'] == item_before['category'] == '酒店'
    assert item['title'] == item_before['title'] and not item['verified'] and item['stage'] == 'draft'
    assert sorted(a['role'] for a in item['attachments']) == ['invoice', *(['payment'] * 4)]
    with tools.store.connect() as db:
        assert {r['digest'] for r in db.execute('SELECT digest FROM attachments WHERE item_id=?', (item_id,))} == {r['digest'] for r in rows}
    assert item['material_match']['payment_total_cents'] == 135300
    assert item['material_match']['payment_count'] == 4 and item['material_match']['alignment_status'] == 'balanced'
    assert len(done['confirmations']) == 1 and done['confirmations'][0]['actor'] == 'user'
    assert item['agent_relation']['confirmation_id'] == done['confirmations'][0]['question_id']
    assert item['agent_content']['status'] == 'ready'
    tools.content._load(done, item_id)
    assert tools.content.next_action(done, item_id) is None
    assert all(tools.intake.get(r['id'])['item_id'] == item_id for r in rows)
    # A lost result/restart replay reuses its committed effect, including both
    # equal-amount proofs; no second attachment or fee and no new model.
    before = row_counts(tools)
    result = PartialRecovery(tools).complete(resumed)
    assert result['item_id'] == item_id and row_counts(tools) == before
    restarted = Jobs(tools.store)
    assert restarted.get(original['id'])['state'] == 'ready'
    assert restarted.get(original['id'])['models'] == 1
    with pytest.raises(ValueError):
        jobs.answer(original['id'], waiting['question']['choices'][0]['id'], waiting['question']['id'])
    assert row_counts(tools) == before


@pytest.mark.parametrize('mutation', ['note', 'title', 'event', 'group', 'duplicate_transaction', 'competitor'])
def test_refresh_rejects_human_edits_and_ambiguous_or_duplicate_proofs(legacy_partial: Any, mutation: str) -> None:
    jobs, tools, job, rows, item_id = legacy_partial
    with tools.store.connect() as db:
        if mutation in {'note', 'title'}:
            item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            item[mutation] = '人工已修改'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), item_id))
        elif mutation == 'event':
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)', (item_id, 'manual', '人工后续事件'))
        elif mutation == 'group':
            group = json.loads(db.execute('SELECT data FROM material_matches').fetchone()['data'])
            group['support_ids'] = ['some-other-material']
            db.execute('UPDATE material_matches SET data=?', (encoded(group),))
        elif mutation == 'duplicate_transaction':
            for payment in rows[2:4]:
                current = tools.intake.get(payment['id'])
                current['fields']['transaction_identifiers'] = [{'kind': 'payment_transaction', 'namespace': 'wechat',
                    'value': 'SAME-TRANSACTION-123456', 'label': '交易单号', 'quote': '交易单号 SAME-TRANSACTION-123456'}]
                current['text'] += '\n交易单号 SAME-TRANSACTION-123456'
                tools.matching._write_queue(db, current)
    if mutation == 'competitor':
        entry((jobs, tools, job['project_id']), 'payment', '154.00', '支付成功\n商品说明 合成佳缘宾馆', shop='合成平台收款方')
    before = row_counts(tools)
    with pytest.raises(ValueError):
        jobs.refresh_question(job['id'], job['updated_at'], tools.refresh_question)
    assert jobs.get(job['id'])['question'] == job['question']
    assert jobs.get(job['id'])['models'] == 1 and row_counts(tools) == before


@pytest.mark.parametrize('mutation', ['expense', 'payment', 'group', 'source_bytes'])
def test_changes_after_answer_fail_without_partial_attachment_writes(legacy_partial: Any, mutation: str) -> None:
    jobs, tools, _, rows, item_id = legacy_partial
    resumed, _ = approve(legacy_partial)
    running = jobs.update(resumed['id'], state='running', run_started=time.time())
    with tools.store.connect() as db:
        if mutation == 'expense':
            item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            item['note'] = '人工更正'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), item_id))
        elif mutation == 'payment':
            changed = tools.intake.get(rows[1]['id'])
            changed['revision'] += 1
            tools.matching._write_queue(db, changed)
        elif mutation == 'group':
            group = json.loads(db.execute('SELECT data FROM material_matches').fetchone()['data'])
            group['reason'] = '后续修改'
            db.execute('UPDATE material_matches SET data=?', (encoded(group),))
    if mutation == 'source_bytes':
        from pathlib import Path
        Path(rows[1]['path']).write_bytes(b'changed after the human answer')
    before = row_counts(tools)
    with pytest.raises((ValueError, RuntimeError)):
        PartialRecovery(tools).complete(running)
    assert row_counts(tools) == before and len(tools.store.get(item_id)['attachments']) == 1
    assert jobs.get(resumed['id'])['models'] == 1 and jobs.get(resumed['id'])['tools'] == 11


def test_complete_requires_persisted_normal_answer_not_in_memory_approval(legacy_partial: Any) -> None:
    jobs, tools, job, _, _ = legacy_partial
    waiting = jobs.refresh_question(job['id'], job['updated_at'], tools.refresh_question)
    chosen = waiting['question']['choices'][0]
    fabricated = {**waiting, 'approval': {'choice': chosen['id'], 'question_id': waiting['question']['id'],
                    'snapshot': waiting['question']['snapshot'], 'choice_data': chosen}}
    with pytest.raises(ValueError, match='正式问题'):
        PartialRecovery(tools).complete(fabricated)
    assert row_counts(tools) == (1, 1, 1)


@pytest.mark.parametrize('human_edit', [False, True])
def test_committed_completion_with_new_sources_can_explicitly_resume_old_stall_once(legacy_partial: Any, human_edit: bool) -> None:
    jobs, tools, _, _, item_id = legacy_partial
    resumed, _ = approve(legacy_partial)
    running = jobs.update(resumed['id'], state='running', run_started=time.time())
    completed = PartialRecovery(tools).complete(running)
    running = jobs.update(running['id'], approval=None, material_revisions=completed['revisions'], tools=14)
    for name in ['extract_goods', 'classify_expense']:
        tools.execute(name, {'item_id': item_id}, running)
    stopped = jobs.stop(running['id'], 'stopped', '旧版进度误判')
    stopped = jobs.update(stopped['id'], stalls=stopped['limits']['stalls'])
    if human_edit:
        with tools.store.connect() as db:
            item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item_id,)).fetchone()['data'])
            item['note'] = '人工修改后禁止恢复'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), item_id))
        with pytest.raises(ValueError):
            jobs.retry(stopped['id'])
        assert jobs.get(stopped['id'])['tools'] == 14
        return
    retry = jobs.retry(stopped['id'])
    assert retry['models'] == 1 and retry['tools'] == 14 and retry['elapsed'] == stopped['elapsed']
    # The exception is limited to one durable continuation, not a fresh loop.
    jobs.stop(retry['id'], 'stopped', '再次无进展')
    jobs.update(retry['id'], stalls=retry['limits']['stalls'])
    with pytest.raises(ValueError, match='新增依据'):
        jobs.retry(retry['id'])
