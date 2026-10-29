"""F12 regressions use disposable stores and invented original documents only."""
import copy
import json
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.agent_jobs import AgentLoop, AgentService, encoded
from invoice_print_layout.agent_review import leave_for_review
from invoice_print_layout.glm_invoice_ai import GlmAIError
from tests.test_invoice_agent import environment, material, start
from tests.test_invoice_agent_content import draft
from tests.test_organize_nonblocking import native_ride_rows


REASON = '原件未找到完整商品边界，请在人工工作台核对商品明细'
LEGACY_REASON = REASON + '；已组合费用和原件保留，可在人工工作台继续核对'


def save_queue(tools: Any, row: dict[str, Any], **changes: Any) -> None:
    row.update(changes)
    with tools.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (encoded(row), row['id']))


def flag_expense(tools: Any, ident: str) -> None:
    with tools.store.connect() as db:
        item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        item.update(agent_review_reason=REASON, review_requested=True)
        item['agent_content'].update(status='needs_input', reason=REASON, product_rule_revision='old')
        item['agent_content']['base'] = tools.content._base(item)
        db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))


@pytest.mark.parametrize('content', [True, False])
def test_waiting_migration_only_marks_question_targets(environment: Any, content: bool) -> None:
    jobs, tools, project = environment
    job, ident = draft(environment)
    untouched = material(tools, project, 'payment', '99', 'never-processed')
    target = material(tools, project, 'payment', '98', 'question-target')
    question = {'type': 'expense_content' if content else 'material_relation', 'text': LEGACY_REASON,
                'material_ids': job['material_ids'] if content else [target['id']]}
    with tools.store.connect() as db:
        item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
        item['agent_content'] = {'status': 'needs_input', 'reason': REASON}
        db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
    jobs.update(job['id'], material_ids=[*job['material_ids'], target['id'], untouched['id']], item_ids=[ident])
    jobs.stop(job['id'], 'needs_input', 'legacy')
    jobs.update(job['id'], question=question)
    service = AgentService(tools.store)
    done = service.jobs.get(job['id'])
    assert done['state'] == 'ready'
    assert not tools.intake.get(untouched['id']).get('agent_review_reason')
    assert bool(tools.intake.get(target['id']).get('agent_review_reason')) is (not content)
    assert bool(tools.store.get(ident).get('agent_review_reason')) is content
    assert done['deferred_materials'] == ([] if content else [target['id']])
    service.close()


def test_new_job_clears_only_selected_reasons_and_replay_preserves_new_reason(environment: Any) -> None:
    jobs, tools, project = environment
    rows = [material(tools, project, 'payment', str(n), 'new-job-' + str(n)) for n in (9, 10)]
    for row in rows:
        save_queue(tools, row, agent_review_reason=LEGACY_REASON)
    with pytest.raises(ValueError):
        jobs.create(project, [rows[0]['id'], 'missing'], 'invalid-request')
    assert tools.intake.get(rows[0]['id'])['agent_review_reason'] == LEGACY_REASON
    job = jobs.create(project, [rows[0]['id']], 'new-review-request')
    cleared = tools.intake.get(rows[0]['id'])
    assert not cleared.get('agent_review_reason') and cleared['revision'] == rows[0]['revision']
    assert tools.intake.get(rows[1]['id'])['agent_review_reason'] == LEGACY_REASON
    done = AgentLoop(jobs).run(job['id'], tools)
    assert done['state'] == 'ready'
    updated = tools.intake.get(rows[0]['id'])
    assert updated['agent_review_reason'] != LEGACY_REASON
    jobs.create(project, [rows[0]['id']], 'new-review-request')
    assert tools.intake.get(rows[0]['id']) == updated


def test_content_question_does_not_mark_another_pending_expense(environment: Any) -> None:
    jobs, tools, project = environment
    rows = [material(tools, project, role, str(n), f'target-{n}-{role}')
            for n in (17, 19) for role in ('receipt', 'payment')]
    done = AgentLoop(jobs).run(start(environment, rows)['id'], tools)
    for ident in done['item_ids']:
        with tools.store.connect() as db:
            item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
            item['agent_content']['status'] = 'needs_input'
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
    jobs.stop(done['id'], 'needs_input', 'legacy')
    question = {'type': 'expense_content', 'material_ids': [e['id'] for e in rows[:2]], 'text': REASON}
    leave_for_review(jobs, done['id'], question, finish=True)
    target = tools.intake.get(rows[0]['id'])['item_id']
    assert tools.store.get(target).get('agent_review_reason')
    assert not tools.store.get(next(i for i in done['item_ids'] if i != target)).get('agent_review_reason')


@pytest.mark.parametrize('history', ['last_results', 'cache'])
@pytest.mark.parametrize('job_state', ['ready', 'failed'])
def test_startup_repairs_old_content_spill_and_transport_once(environment: Any, tmp_path: Path, history: str,
                                                            job_state: str) -> None:
    jobs, tools, project = environment
    ride = native_ride_rows(environment, tmp_path)
    done = AgentLoop(jobs).run(start(environment, ride)['id'], tools)
    ident = done['item_ids'][0]
    flag_expense(tools, ident)
    rows = [material(tools, project, 'payment', str(n), 'spill-' + str(n)) for n in range(41, 45)]
    for row in rows:
        save_queue(tools, row, agent_review_reason=LEGACY_REASON)
    # A later independent reason must survive the historical cleanup.
    save_queue(tools, rows[-1], agent_review_reason='人工补充的关系疑问')
    question = {'type': 'expense_content', 'text': LEGACY_REASON, 'material_ids': [e['id'] for e in ride]}
    jobs.update(done['id'], state=job_state, material_ids=[*done['material_ids'], *[e['id'] for e in rows]],
                deferred_content=[ident], deferred_materials=[e['id'] for e in rows],
                last_results=[{'tool': 'ask_user', 'result': {'question': question}}] if history == 'last_results' else [])
    if history == 'cache':
        jobs.cache(done['id'], 'legacy-cached-question', {'question': question})
    original = {row['path']: Path(row['path']).read_bytes() for row in [*ride, *rows]}
    snapshots = []
    for _ in range(2):
        service = AgentService(tools.store)
        assert not tools.store.get(ident).get('agent_review_reason')
        assert tools.store.get(ident)['agent_content']['status'] == 'ready'
        assert all(not tools.intake.get(row['id']).get('agent_review_reason') for row in rows[:-1])
        assert tools.intake.get(rows[-1]['id'])['agent_review_reason'] == '人工补充的关系疑问'
        snapshots.append(encoded([service.jobs.get(done['id']), tools.store.get(ident), tools.intake.list()]))
        assert len(tools.store.list_items()) == 1 and len(tools.intake.list()) == 6
        assert all(Path(p).read_bytes() == data for p, data in original.items())
        service.close()
    assert snapshots[0] == snapshots[1]
    assert AgentLoop(jobs).run(done['id'], tools)['state'] == job_state
    retry = start(environment, rows[:-1])
    assert AgentLoop(jobs).run(retry['id'], tools)['state'] == 'ready'
    assert len(tools.store.list_items()) == 1


@pytest.mark.parametrize('mutation', ['human_edit', 'changed_source', 'verified', 'wrong_question'])
def test_startup_keeps_unproved_or_changed_records(environment: Any, tmp_path: Path, mutation: str) -> None:
    jobs, tools, project = environment
    ride = native_ride_rows(environment, tmp_path)
    done = AgentLoop(jobs).run(start(environment, ride)['id'], tools)
    ident = done['item_ids'][0]
    flag_expense(tools, ident)
    orphan = material(tools, project, 'payment', '9', 'keep-reason')
    save_queue(tools, orphan, agent_review_reason=LEGACY_REASON)
    jobs.update(done['id'], deferred_content=[ident], deferred_materials=[orphan['id']],
                material_ids=[*done['material_ids'], orphan['id']], last_results=[])
    if mutation == 'changed_source':
        with tools.store.connect() as db:
            path = db.execute('SELECT path FROM attachments WHERE item_id=?', (ident,)).fetchone()['path']
        Path(path).write_bytes(b'changed synthetic bytes')
    elif mutation in {'human_edit', 'verified'}:
        with tools.store.connect() as db:
            item = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (ident,)).fetchone()['data'])
            item.update({'title': '人工编辑'} if mutation == 'human_edit' else {'verified': True})
            db.execute('UPDATE expenses SET data=? WHERE id=?', (encoded(item), ident))
    else:
        jobs.cache(done['id'], 'unrelated-question', {'question': {'type': 'material_relation',
                   'text': LEGACY_REASON, 'material_ids': [orphan['id']]}})
    service = AgentService(tools.store)
    assert tools.intake.get(orphan['id'])['agent_review_reason'] == LEGACY_REASON
    if mutation != 'wrong_question':
        assert tools.store.get(ident)['agent_review_reason'] == REASON
    service.close()


def passenger_rows(environment: Any, tmp_path: Path, *, extra: str = '', malformed: bool = False,
                   tax_row: bool = True) -> list[dict[str, Any]]:
    rows = native_ride_rows(environment, tmp_path)
    _, tools, _ = environment
    # Entire identity and both places are fictional; the ID is deliberately impossible.
    text = rows[0]['text'].replace('电子发票（普通发票）', '电子发票（旅客运输服务）')
    path = tmp_path / 'passenger-original.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=900, height=600)
        for n, line in enumerate(text.splitlines()[:-1]):
            if not tax_row and line.startswith('*'):
                continue
            page.insert_text((25, 25 + n * 20), line, fontname='china-s', fontsize=10)
        labels = ['出行人', '有效身份证件号', '出行日期', '出发地', '到达地', '等级', '交通工具类型']
        values = ['虚构旅客甲', '000000000000000000', '2030-01-01', '虚构起点市', '虚构终点市', '无', '出租汽车']
        for x, label, value in zip([25, 145, 315, 425, 535, 645, 735], labels, values):
            page.insert_text((x, 190), label, fontname='china-s', fontsize=10)
            page.insert_text((x, 215), '无法识别' if malformed and label == '出行日期' else value,
                             fontname='china-s', fontsize=10)
        if extra:
            page.insert_text((25, 240), extra, fontname='china-s', fontsize=10)
        page.insert_text((25, 275), '价税合计', fontname='china-s', fontsize=10)
        page.insert_text((300, 275), '（小写）', fontname='china-s', fontsize=10)
        page.insert_text((336, 275), '¥17.34', fontsize=10)
        payload = pdf.tobytes()
        native_text = page.get_text()
    row = tools.intake.receive(path.name, payload, project_id=rows[0]['project_id'])
    save_queue(tools, row, status='review', role='invoice', revision=1, fields=rows[0]['fields'], text=native_text)
    tools.intake.archive_action(rows[0]['id'], rows[0]['revision'], rows[0]['project_id'], 'archive')
    return [row, rows[1]]


@pytest.mark.parametrize('tax_row', [True, False])
def test_passenger_invoice_pdf_is_local_and_private(environment: Any, tmp_path: Path, tax_row: bool) -> None:
    from invoice_print_layout.agent_evidence import material_facts
    jobs, tools, _ = environment
    rows = passenger_rows(environment, tmp_path, tax_row=tax_row)
    facts = material_facts(rows[0])
    assert facts['complete'] and not facts['issues'], facts
    done = AgentLoop(jobs).run(start(environment, rows, use_model=True)['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0, done
    assert len(done['item_ids']) == 1 and done['remaining_material_count'] == 0
    item = tools.store.get(done['item_ids'][0])
    assert item['category'] == '打车' and not item.get('agent_review_reason'), item['agent_content']
    visible = encoded([facts, done, item])
    assert '虚构旅客甲' not in visible and '000000000000000000' not in visible
    if not tax_row:
        from invoice_print_layout.agent_product_rules import inspect_product_rules
        current = {**facts, 'source_text': facts['source_text'] + '\n未知独立商品'}
        goods = [{**g, 'material_id': rows[0]['id'], 'sha256': rows[0]['digest']} for g in facts['goods']]
        assert inspect_product_rules(goods, [current], {'客运服务': '打车'})['status'] == 'needs_input'


@pytest.mark.parametrize('extra,malformed', [('未知仪器', False), ('商品名称：办公鼠标', False), ('', True)])
def test_passenger_schema_cannot_hide_unknown_rows(environment: Any, tmp_path: Path, extra: str, malformed: bool) -> None:
    from invoice_print_layout.agent_evidence import material_facts
    rows = passenger_rows(environment, tmp_path, extra=extra, malformed=malformed)
    facts = material_facts(rows[0])
    assert not facts['complete'] and facts['issues']
    assert '虚构旅客甲' not in encoded(facts) and '000000000000000000' not in encoded(facts)


@pytest.mark.parametrize('failure_code', ['network_error', 'config_unavailable'])
def test_mid_job_model_failure_continues_local_and_keeps_materials(environment: Any, monkeypatch: Any,
                                                                  failure_code: str) -> None:
    jobs, tools, project = environment
    rows = [material(tools, project, role, str(n), f'mid-{n}-{role}') for n in (17, 19) for role in ('receipt', 'payment')]
    rows.append(material(tools, project, 'payment', '9', 'orphan-after-failure'))
    before = {e['path']: Path(e['path']).read_bytes() for e in rows}
    original = tools.next_action
    attempted = []
    def next_action(job: dict[str, Any]) -> Any:
        if len(job['item_ids']) == 1 and job['use_model'] and not attempted:
            return None
        return original(job)
    class OfflinePlanner:
        def choose(self, state: dict[str, Any]) -> Any:
            attempted.append(True)
            raise GlmAIError('', failure_code=failure_code)
    monkeypatch.setattr(tools, 'next_action', next_action)
    done = AgentLoop(jobs).run(start(environment, rows, use_model=True)['id'], tools, OfflinePlanner())
    assert done['state'] == 'ready' and done['model_unavailable'], done
    assert len(done['item_ids']) == 2 and done['models'] == len(attempted) == 1
    assert tools.intake.get(rows[-1]['id'])['agent_review_reason'] == '模型暂时不可用，已留在材料合成台'
    assert '已生成 2 笔费用，1 份材料留在材料合成台' in done['message']
    assert all(Path(p).read_bytes() == data for p, data in before.items())
    snapshot = copy.deepcopy(done)
    assert AgentLoop(jobs).run(done['id'], tools, OfflinePlanner()) == snapshot
    assert len(tools.store.list_items()) == 2 and len(tools.intake.list()) == len(rows)


@pytest.mark.parametrize('provider', ['organizer', 'classifier'])
@pytest.mark.parametrize('failure', ['connection', 'timeout', 'gateway'])
def test_transport_failures_have_safe_fallback_code(monkeypatch: Any, provider: str, failure: str) -> None:
    import urllib.error
    from invoice_print_layout import expense_classifier as classifier
    from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
    errors = {'connection': urllib.error.URLError('synthetic-private-detail'),
              'timeout': TimeoutError('synthetic-private-detail'),
              'gateway': urllib.error.HTTPError('http://synthetic.invalid', 502, 'synthetic-private-detail', {}, None)}
    class OfflineOpener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise errors[failure]
    monkeypatch.setattr('urllib.request.build_opener', lambda *a, **k: OfflineOpener())
    monkeypatch.setattr(classifier, 'api_key', lambda: 'synthetic-key')
    ai = object.__new__(GlmInvoiceAI)
    ai._key = 'synthetic-key'
    with pytest.raises(GlmAIError) as error:
        if provider == 'organizer':
            ai._network('http://synthetic.invalid', {}, timeout=1)
        else:
            classifier.ask_jev({'goods': '合成用品', 'purpose': ''})
    assert error.value.failure_code == 'network_error'
    assert 'synthetic-private-detail' not in str(error.value)


def test_goods_completion_failure_notifies_loop_and_does_not_retry(environment: Any, monkeypatch: Any) -> None:
    from invoice_print_layout import agent_evidence
    from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI
    jobs, tools, project = environment
    rows = [material(tools, project, role, '17', 'completion-' + role,
                     **({'text': '收据\n商家名称：合成工具商店\n商品清单\n胶带\n实付：17'} if role == 'receipt' else {}))
            for role in ('receipt', 'payment')]
    original = agent_evidence.material_facts
    def incomplete(entry: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        facts = original(entry, **kwargs)
        if entry['role'] == 'receipt':
            facts = {**facts, 'goods': [], 'goods_complete': False}
        return facts
    monkeypatch.setattr(agent_evidence, 'material_facts', incomplete)
    monkeypatch.setattr(GlmInvoiceAI, 'public_status', lambda _: {'available': True})
    calls = []
    def offline(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        raise GlmAIError('', failure_code='network_error')
    monkeypatch.setattr(GlmInvoiceAI, '_request', offline)
    done = AgentLoop(jobs).run(start(environment, rows, use_model=True)['id'], tools)
    assert done['state'] == 'ready' and done['model_unavailable'] and len(calls) == done['models'] == 1, done
    assert len(tools.intake.list()) == len(rows)
    assert len(tools.store.list_items()) == len(done['item_ids']) == 1
