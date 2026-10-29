"""Exact-original human facts must never become reusable defaults.

All records, original bytes and normal answer receipts live in pytest tmp_path.
No real material, OCR, model call or formal database is used.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any

import pytest

from tests.legacy_agent_fixture import LegacyQuestionFixture
from invoice_print_layout.agent_confirmed_facts import content_fact, relation_fact, restore, verified_content
from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from invoice_print_layout.agent_tools import InvoiceTools
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from tests.test_invoice_agent_combinations import entry, invoice_text


@pytest.fixture
def facts_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Jobs, InvoiceTools, str]:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail('Human-fact tests must not invoke OCR or a model')
    monkeypatch.setattr(GlmInvoiceAI, '_request', forbidden)
    monkeypatch.setattr('invoice_print_layout.intake_queue.read_receipt', forbidden)
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤/tasks.sqlite3').create_project('Synthetic human fact')['id']
    jobs = Jobs(store)
    return jobs, InvoiceTools(jobs), project


def pair(env: tuple[Jobs, InvoiceTools, str]) -> list[dict[str, Any]]:
    return [entry(env, 'invoice', '29.60', invoice_text('饮用水'), category='餐饮', shop='合成饮料销售方'),
            entry(env, 'payment', '29.60', '支付成功\n商品 美团订单', shop='平台聚合商户')]


def body_for(env: tuple[Jobs, InvoiceTools, str], rows: list[dict[str, Any]], *, content: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {'project_id': env[2], 'material_ids': [e['id'] for e in rows],
        'claim': 'material_content' if content else 'same_expense', 'amount_cents': 2960,
        'statement': '用户明确确认这两份原件属于同一笔饮用水费用。',
        'provenance': {'source': 'direct_user_reply', 'reference': 'test-only human answer',
                       'sha256': hashlib.sha256(b'synthetic human answer').hexdigest()}}
    if content:
        body.update(goods=['饮用水'], category='餐饮', content_material_id=rows[0]['id'],
                    statement='用户明确确认该原件商品只有饮用水，归餐饮。')
    return body


def job_for(env: tuple[Jobs, InvoiceTools, str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {'project_id': env[2], 'material_ids': [e['id'] for e in rows]}


def relation_row(rows: list[dict[str, Any]], amount: int = 2960) -> dict[str, Any]:
    return {'voucher_keys': ['q:' + rows[0]['id']], 'payment_ids': [rows[1]['id']],
            'support_ids': [e['id'] for e in rows[2:]], 'amount_cents': amount}


def queue_snapshot(jobs: Jobs) -> list[tuple[str, str, str]]:
    with jobs.store.connect() as db:
        return [(r['id'], r['digest'], r['data']) for r in db.execute('SELECT * FROM intake_queue ORDER BY rowid')]


def item_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {'id': 'test-expense', 'amount_cents': 2960, 'project_id': rows[0]['project_id'],
            'attachments': [{'id': 'attachment-' + str(n), 'digest': e['digest'], 'path': e['path'],
                             'role': e['role']} for n, e in enumerate(rows)]}


def content_sources(env: tuple[Jobs, InvoiceTools, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = pair(env)
    fact = restore(env[0], body_for(env, rows, content=True))
    rows[0]['confirmed_content'] = fact
    return rows, fact


def test_restore_is_idempotent_and_does_not_copy_history_into_queue_or_expenses(facts_env: Any) -> None:
    jobs, _, project = facts_env
    rows = pair(facts_env)
    before = queue_snapshot(jobs)
    source_hashes = {e['path']: hashlib.sha256(Path(e['path']).read_bytes()).hexdigest() for e in rows}
    first = restore(jobs, body_for(facts_env, rows))
    again = body_for(facts_env, rows)
    again['material_ids'].reverse()
    second = restore(jobs, again)
    assert first['fact_id'] == second['fact_id']
    assert first == second
    with jobs.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM invoice_agent_actions WHERE job_id=?', ('project-facts:' + project,)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM invoice_agent_jobs').fetchone()[0] == 0
    assert queue_snapshot(jobs) == before
    assert not jobs.store.list_items()
    assert source_hashes == {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in source_hashes}
    assert not {'category', 'role', 'text', 'recognition'} & first.keys()


@pytest.mark.parametrize('field,value', [('category', '餐饮'), ('goods', ['饮用水']), ('role', 'invoice'),
                                        ('text', '历史识别文字'), ('verified', True), ('recognition', {'amount': 2960})])
def test_relation_restore_rejects_category_role_recognition_and_verification_defaults(facts_env: Any, field: str, value: Any) -> None:
    rows = pair(facts_env)
    body = {**body_for(facts_env, rows), field: value}
    before = queue_snapshot(facts_env[0])
    with pytest.raises(ValueError):
        restore(facts_env[0], body)
    assert queue_snapshot(facts_env[0]) == before


@pytest.mark.parametrize('source', ['model', 'model_inference', 'script', 'mail_same_subject', ''])
def test_nonhuman_provenance_cannot_be_restored(facts_env: Any, source: str) -> None:
    rows = pair(facts_env)
    body = body_for(facts_env, rows)
    body['provenance']['source'] = source
    with pytest.raises(ValueError):
        restore(facts_env[0], body)


def test_relation_fact_reuses_exact_sha_amount_under_fresh_material_ids(facts_env: Any) -> None:
    rows = pair(facts_env)
    fact = restore(facts_env[0], body_for(facts_env, rows))
    fresh = [{**e, 'id': 'new-intake-' + e['id']} for e in rows]
    found = relation_fact(facts_env[0], job_for(facts_env, fresh), relation_row(fresh), fresh)
    assert found and found['fact_id'] == fact['fact_id']
    assert found['actor'] == 'user' and found['action'] == 'confirm_relation'


def test_relation_fact_is_not_reused_for_another_amount(facts_env: Any) -> None:
    rows = pair(facts_env)
    restore(facts_env[0], body_for(facts_env, rows))
    assert relation_fact(facts_env[0], job_for(facts_env, rows), relation_row(rows, 2961), rows) is None


@pytest.mark.parametrize('mutation', ['replace', 'add', 'foreign_project', 'actual_bytes'])
def test_relation_confirmation_cannot_expand_or_rebind_source_scope(facts_env: Any, mutation: str) -> None:
    rows = pair(facts_env)
    restore(facts_env[0], body_for(facts_env, rows))
    if mutation in {'replace', 'add'}:
        extra = entry(facts_env, 'payment', '29.60', '支付成功\n商品 未确认订单', shop='未确认收款方')
        rows = [rows[0], extra] if mutation == 'replace' else [*rows, extra]
    elif mutation == 'foreign_project':
        rows = [{**e, 'project_id': 'foreign-project'} for e in rows]
    else:
        Path(rows[0]['path']).write_bytes(b'changed actual original bytes')
    assert relation_fact(facts_env[0], job_for(facts_env, rows), relation_row(rows), rows) is None


@pytest.mark.parametrize('mutation', ['foreign_project', 'changed_bytes', 'duplicate_source', 'linked'])
def test_restore_rejects_sources_outside_current_originals(facts_env: Any, mutation: str) -> None:
    jobs, tools, _ = facts_env
    rows = pair(facts_env)
    body = body_for(facts_env, rows)
    if mutation == 'foreign_project':
        body['project_id'] = 'foreign-project'
    elif mutation == 'changed_bytes':
        Path(rows[0]['path']).write_bytes(b'changed actual source')
    elif mutation == 'duplicate_source':
        body['material_ids'] = [rows[0]['id'], rows[0]['id']]
    else:
        changed = {**rows[0], 'status': 'linked', 'item_id': 'existing-expense'}
        with jobs.store.connect() as db:
            tools.matching._write_queue(db, changed)
    with pytest.raises(ValueError):
        restore(jobs, body)
    with jobs.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM invoice_agent_actions').fetchone()[0] == 0


def waiting_relation(env: tuple[Jobs, InvoiceTools, str], rows: list[dict[str, Any]]) -> dict[str, Any]:
    jobs, tools, project = env
    job = jobs.create(project, [e['id'] for e in rows], 'fact-question-' + str(time.time_ns()))
    waiting = LegacyQuestionFixture(jobs).run(job['id'], tools)
    assert waiting['state'] == 'needs_input'
    assert waiting['question']['type'] == 'material_relation'
    return waiting


def test_normal_answer_receipt_can_be_reused_by_a_new_job_without_answer_replay(facts_env: Any) -> None:
    jobs, _, project = facts_env
    rows = pair(facts_env)
    waiting = waiting_relation(facts_env, rows)
    choice = next(c for c in waiting['question']['choices'] if c['action'] == 'confirm_relation')
    answered = jobs.answer(waiting['id'], choice['id'], waiting['question']['id'])
    assert answered['models'] == waiting['models'] and answered['limits'] == waiting['limits']
    assert len(answered['confirmations']) == 1 and answered['confirmations'][0]['actor'] == 'user'
    jobs.update(waiting['id'], state='cancelled')
    new_job = jobs.create(project, [e['id'] for e in rows], 'fact-reuse-' + str(time.time_ns()))
    fact = relation_fact(jobs, new_job, relation_row(rows), rows)
    assert fact and fact['action'] == 'confirm_relation'
    assert fact['provenance']['source'] == 'normal_question_answer'
    assert not new_job['confirmations']
    assert not jobs.store.list_items()


def test_later_normal_not_same_receipt_supersedes_older_positive_restore(facts_env: Any) -> None:
    jobs, _, project = facts_env
    rows = pair(facts_env)
    waiting = waiting_relation(facts_env, rows)
    restore(jobs, body_for(facts_env, rows))
    choice = next(c for c in waiting['question']['choices'] if c['action'] == 'not_same_expense')
    answered = jobs.answer(waiting['id'], choice['id'], waiting['question']['id'])
    assert answered['state'] == 'stopped'
    newer = jobs.create(project, [e['id'] for e in rows], 'fact-denial-' + str(time.time_ns()))
    fact = relation_fact(jobs, newer, relation_row(rows), rows)
    assert fact and fact['action'] == 'not_same_expense'
    assert not jobs.store.list_items()


def test_content_fact_can_be_loaded_in_larger_task_but_not_authorize_expanded_expense(facts_env: Any) -> None:
    rows, expected = content_sources(facts_env)
    found = content_fact(facts_env[0], job_for(facts_env, rows), rows[0])
    assert found and found['fact_id'] == expected['fact_id']
    extra = entry(facts_env, 'receipt', '1.00', '收据\n商品 未知商品', shop='未确认商家')
    expanded = [*rows, extra]
    # A batch may contain several independent groups. Loading this fact for
    # its own receipt is fine; applying it to a three-source expense is not.
    loaded = content_fact(facts_env[0], job_for(facts_env, expanded), rows[0])
    assert loaded and loaded['fact_id'] == expected['fact_id']
    assert verified_content(item_for(expanded), expanded) is None


def test_content_fact_does_not_accept_a_foreign_entry_with_same_digest(facts_env: Any) -> None:
    rows, _ = content_sources(facts_env)
    foreign = {**rows[0], 'id': 'foreign-material', 'project_id': 'foreign-project'}
    assert content_fact(facts_env[0], job_for(facts_env, rows), foreign) is None


def test_content_fact_cannot_use_changed_actual_source_bytes(facts_env: Any) -> None:
    rows, _ = content_sources(facts_env)
    Path(rows[0]['path']).write_bytes(b'tampered original')
    assert content_fact(facts_env[0], job_for(facts_env, rows), rows[0]) is None


def test_verified_content_accepts_only_full_expense_same_amount(facts_env: Any) -> None:
    rows, expected = content_sources(facts_env)
    item = item_for(rows)
    found = verified_content(item, rows)
    assert found and found['fact_id'] == expected['fact_id']
    assert verified_content({**item, 'amount_cents': 2961}, rows) is None


@pytest.mark.parametrize('field,value', [
    ('actor', 'model'), ('claim', 'same_expense'), ('claim', 'purpose'),
    ('action', 'not_same_expense'), ('fact_id', 'model-output'),
    ('goods', []), ('goods', ['']), ('goods', '饮用水'),
    ('category', '材料采购'),
    ('statement', ''), ('provenance', {}),
    ('provenance', {'source': 'model', 'reference': 'inferred', 'sha256': 'a' * 64}),
])
def test_verified_content_checks_human_content_contract(facts_env: Any, field: str, value: Any) -> None:
    rows, _ = content_sources(facts_env)
    rows[0]['confirmed_content'] = {**rows[0]['confirmed_content'], field: value}
    assert verified_content(item_for(rows), rows) is None


def test_verified_content_cannot_authorize_another_expense_project(facts_env: Any) -> None:
    rows, _ = content_sources(facts_env)
    assert verified_content({**item_for(rows), 'project_id': 'foreign-project'}, rows) is None


def test_content_reader_rejects_restored_journal_record_tampering(facts_env: Any) -> None:
    rows, fact = content_sources(facts_env)
    tampered = {**fact, 'category': '材料采购'}
    import json
    with facts_env[0].store.connect() as db:
        db.execute('UPDATE invoice_agent_actions SET data=? WHERE key=?',
                   (json.dumps(tampered, ensure_ascii=False), fact['fact_id']))
    assert content_fact(facts_env[0], job_for(facts_env, rows), rows[0]) is None


def test_verified_content_checks_attachment_digests_not_only_equal_count(facts_env: Any) -> None:
    rows, _ = content_sources(facts_env)
    extra = entry(facts_env, 'receipt', '1.00', '收据\n商品 未确认商品', shop='未确认商家')
    item = item_for([rows[0], extra])
    assert len(item['attachments']) == len(rows)
    assert verified_content(item, rows) is None


def test_verified_content_does_not_authorize_an_added_unknown_product_source(facts_env: Any) -> None:
    rows, _ = content_sources(facts_env)
    extra = entry(facts_env, 'receipt', '1.00', '收据\n商品 未知安装项目', shop='未确认商家')
    expanded = [*rows, extra]
    assert verified_content(item_for(expanded), expanded) is None


def test_verified_content_cannot_use_changed_actual_source_bytes(facts_env: Any) -> None:
    rows, _ = content_sources(facts_env)
    Path(rows[1]['path']).write_bytes(b'tampered payment original')
    assert verified_content(item_for(rows), rows) is None


def test_restore_content_requires_explicit_human_category_and_goods(facts_env: Any) -> None:
    rows = pair(facts_env)
    body = body_for(facts_env, rows, content=True)
    body.pop('category')
    with pytest.raises(ValueError):
        restore(facts_env[0], body)


def test_purpose_fact_records_installation_without_fabricating_category_or_goods(facts_env: Any) -> None:
    rows = pair(facts_env)
    payment = rows[1]
    body = body_for(facts_env, [payment])
    body.update(claim='material_purpose', statement='用户明确说明这是安装锁费用。')
    fact = restore(facts_env[0], body)
    assert fact['claim'] == 'material_purpose' and fact['actor'] == 'user'
    assert not {'category', 'goods', 'content_digest'} & fact.keys()
    payment['confirmed_content'] = fact
    assert verified_content(item_for([payment]), [payment]) is None
    assert content_fact(facts_env[0], job_for(facts_env, [payment]), payment) is None


@pytest.mark.parametrize('field,value', [('goods', ['安装锁']), ('category', '其他'),
                                        ('content_material_id', 'test-source')])
def test_purpose_fact_cannot_smuggle_a_category_or_full_goods_confirmation(facts_env: Any, field: str, value: Any) -> None:
    rows = pair(facts_env)
    body = body_for(facts_env, [rows[1]])
    body.update(claim='material_purpose', statement='用户明确说明这是安装锁费用。')
    body[field] = value
    with pytest.raises(ValueError):
        restore(facts_env[0], body)
