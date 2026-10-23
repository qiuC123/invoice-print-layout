"""Measurements survive ordinary loop/cache/pause/recovery paths without policy changes."""
from __future__ import annotations

import copy
import json
import time
from typing import Any

import pytest

from invoice_print_layout import agent_jobs, agent_metrics
from invoice_print_layout.agent_jobs import AgentLoop, AgentService, Jobs
from tests.test_invoice_agent import environment, material, start, Scripted, call


def test_real_tools_have_durable_content_free_timings(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'timed-order'),
               material(tools, project, 'payment', '17', 'timed-payment')]
    before = {e['id']: copy.deepcopy(e) for e in entries}
    done = AgentLoop(jobs).run(start(environment, entries)['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0
    timings = done['timings']
    assert timings['active_seconds'] == done['elapsed'] > 0
    assert timings['stage_times_are_inclusive'] is True
    assert timings['stages']['tool:build_preview']['count'] == 1
    assert sum(s['count'] for s in timings['stages'].values() if s['stage'] == 'tool') == done['tools']
    assert all(s['seconds'] >= 0 and s['cache_misses'] == s['count'] for s in timings['stages'].values()
               if s['stage'] == 'tool')
    assert Jobs(tools.store).get(done['id']) == done
    assert jobs.list(project)[0] == done
    assert AgentLoop(jobs).run(done['id'], tools) == done
    metric_text = json.dumps(timings, ensure_ascii=False)
    assert all(e['id'] not in metric_text and e['path'] not in metric_text and e['text'] not in metric_text
               for e in before.values())


def test_cached_repetition_is_measured_but_does_not_execute_again(environment: Any,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'timed-unmatched')
    # Force a repeated planner observation for the cache/timing contract.  The
    # optimized script now asks missing-fact questions before invoking a model.
    monkeypatch.setattr(tools, 'next_action', lambda job: None)
    done = AgentLoop(jobs).run(start(environment, [entry], use_model=True)['id'], tools,
                               Scripted(call('read_material', id=entry['id'])))
    assert done['state'] == 'stopped' and done['models'] == 3
    read = done['timings']['stages']['tool:read_material']
    assert read['count'] == 3 and read['cache_misses'] == 1 and read['cache_hits'] == 2
    planned = done['timings']['stages']['model_plan']
    assert planned['count'] == planned['model_calls'] == 3
    assert done['timings']['stages']['model_context']['count'] == 3


def test_user_wait_excluded_from_active_total_on_answer_and_resume(environment: Any,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, tools, project = environment
    order = material(tools, project, 'receipt', '17', 'timed-choice-order')
    payments = [material(tools, project, 'payment', '17', 'timed-choice-' + str(n)) for n in range(2)]
    waiting = AgentLoop(jobs).run(start(environment, [order, *payments])['id'], tools)
    assert waiting['state'] == 'needs_input'
    elapsed = waiting['elapsed']
    old_limits = copy.deepcopy(waiting['limits'])
    real_time = agent_jobs.time.time
    offset = real_time() + 600
    monkeypatch.setattr(agent_jobs.time, 'time', lambda: offset)
    after_wait = Jobs(tools.store).get(waiting['id'])
    assert after_wait['timings']['active_seconds'] == elapsed
    assert after_wait['timings']['current_user_wait_seconds'] >= 600
    choice = after_wait['question']['choices'][0]
    jobs.answer(waiting['id'], choice['id'])
    resumed = AgentLoop(jobs).run(waiting['id'], tools)
    # The unused competing payment still needs evidence; only the chosen pair
    # reaches preview, preserving the original partial-preview policy.
    assert resumed['state'] == 'needs_input' and resumed['preview']['total_cents'] == 1700
    assert resumed['limits'] == old_limits
    assert resumed['timings']['user_wait_seconds'] >= 600
    assert resumed['timings']['active_seconds'] == resumed['elapsed'] == elapsed
    assert len(resumed['confirmations']) == 1


def test_recovery_preserves_observations_and_original_budget(environment: Any) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'timed-recovery')
    job = start(environment, [entry])
    jobs.update(job['id'], state='running', run_started=agent_jobs.time.time() - .2, tools=1)
    with jobs.timed(job['id'], 'tool', tool='read_material'):
        tools.execute('read_material', {'id': entry['id']}, jobs.get(job['id']))
    observed = jobs.get(job['id'])['timings']['stages']
    service = AgentService(tools.store)
    try:
        recovered = service.jobs.get(job['id'])
        assert recovered['state'] == 'queued'
        assert recovered['tools'] == 1 and recovered['elapsed'] >= .2
        assert recovered['timings']['stages'] == observed
        assert recovered['timings']['active_seconds'] == recovered['elapsed']
    finally:
        service.close()


def test_old_jobs_without_timings_read_and_resume(environment: Any) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'timed-old-order'),
               material(tools, project, 'payment', '17', 'timed-old-payment')]
    job = start(environment, entries)
    job.pop('timings')
    with tools.store.connect() as db:
        db.execute('UPDATE invoice_agent_jobs SET data=? WHERE id=?', (json.dumps(job), job['id']))
    assert jobs.get(job['id'])['timings']['records'] == 0
    done = AgentLoop(Jobs(tools.store)).run(job['id'], tools)
    assert done['state'] == 'ready' and done['timings']['stages']['tool:build_preview']['count'] == 1


def test_metric_failure_cannot_change_result_or_receipts(environment: Any,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, tools, project = environment
    entries = [material(tools, project, 'receipt', '17', 'timed-failure-order'),
               material(tools, project, 'payment', '17', 'timed-failure-payment')]
    def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError('synthetic metrics unavailable')
    monkeypatch.setattr(jobs, '_record_timing', broken)
    done = AgentLoop(jobs).run(start(environment, entries)['id'], tools)
    assert done['state'] == 'ready' and done['models'] == 0 and len(done['item_ids']) == 1
    assert len(tools.store.list_items()) == 1 and done['preview']['total_cents'] == 1700
    assert done['timings']['records'] == 0


def test_unavailable_metrics_clock_still_runs_tool(environment: Any,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, tools, project = environment
    job = start(environment, [material(tools, project, 'receipt', '17', 'timed-clock')])
    def broken_clock() -> float:
        raise OSError('synthetic unavailable monotonic clock')
    monkeypatch.setattr(agent_jobs.time, 'perf_counter', broken_clock)
    with jobs.timed(job['id'], 'facts_parse'):
        observed = 'business result retained'
    assert observed == 'business result retained'
    assert jobs.get(job['id'])['timings']['records'] == 0


def test_nested_timings_report_inclusive_stages_without_adding_to_active_total(environment: Any) -> None:
    jobs, tools, project = environment
    job = start(environment, [material(tools, project, 'receipt', '17', 'timed-nested')], use_model=True)
    jobs.update(job['id'], state='running', run_started=agent_jobs.time.time())
    with jobs.timed(job['id'], 'tool', tool='extract_goods'):
        with jobs.timed(job['id'], 'goods_model', models_before=0):
            jobs.reserve_model(job['id'])
    done = jobs.stop(job['id'], 'needs_input', 'synthetic pause')
    metrics = done['timings']
    assert metrics['stages']['goods_model']['model_calls'] == metrics['stages']['tool:extract_goods']['model_calls'] == 1
    assert metrics['stages']['tool:extract_goods']['seconds'] >= metrics['stages']['goods_model']['seconds']
    assert metrics['active_seconds'] == done['elapsed']


def test_rejected_and_raised_tools_keep_original_failure_policy(environment: Any,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'timed-error')
    monkeypatch.setattr(tools, 'next_action', lambda job: None)
    rejected = AgentLoop(jobs).run(start(environment, [entry], use_model=True)['id'], tools,
                                   Scripted(call('read_material', id='outside')))
    assert rejected['state'] == 'stopped'
    assert rejected['timings']['stages']['tool:read_material']['errors'] == 1
    again = material(tools, project, 'receipt', '17', 'timed-error-again')
    failed = AgentLoop(jobs).run(start(environment, [again], use_model=True)['id'], tools,
                                Scripted(call('create_draft', proposal_id='guessed')))
    assert failed['state'] == 'failed'
    assert failed['timings']['stages']['tool:create_draft']['errors'] == 1


def test_bounded_history_retains_aggregate_and_sequences() -> None:
    job: dict[str, Any] = {'timings': agent_metrics.initial()}
    for _ in range(agent_metrics.MAX_STEPS + 17):
        agent_metrics.record(job, stage='facts_parse', seconds=.01, outcome='ok')
    metrics = job['timings']
    assert len(metrics['steps']) == agent_metrics.MAX_STEPS
    assert metrics['records'] == metrics['stages']['facts_parse']['count'] == agent_metrics.MAX_STEPS + 17
    assert metrics['steps'][0]['sequence'] == 18


def test_nested_business_write_defers_metrics_without_waiting_or_losing_samples(environment: Any) -> None:
    jobs, tools, project = environment
    job = start(environment, [material(tools, project, 'receipt', '17', 'timed-write-lock')])
    jobs.cache(job['id'], 'synthetic-receipt', {'preserved': True})
    with tools.store.connect() as db:
        db.execute('CREATE TABLE timing_business_sentinel (value INTEGER NOT NULL)')
        db.execute('INSERT INTO timing_business_sentinel VALUES (42)')
    began = time.monotonic()
    with jobs.timed(job['id'], 'tool', tool='create_draft'):
        with tools.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE timing_business_sentinel SET value=43')
            with jobs.timed(job['id'], 'candidate_scan'):
                with jobs.timed(job['id'], 'facts_parse'):
                    assert db.execute('SELECT value FROM timing_business_sentinel').fetchone()[0] == 43
            # The second connection must return immediately under this actual
            # write lock.  Only this test's business transaction owns the row.
            assert len(jobs._timing_pending) == 2
            assert jobs.get(job['id'])['timings']['records'] == 0
            assert db.execute('SELECT value FROM timing_business_sentinel').fetchone()[0] == 43
    assert time.monotonic() - began < 1
    measured = jobs.get(job['id'])
    assert measured['timings']['records'] == 3
    assert set(measured['timings']['stages']) == {'facts_parse', 'candidate_scan', 'tool:create_draft'}
    assert measured['state'] == job['state'] == 'queued'
    assert measured['tools'] == measured['models'] == 0 and measured['elapsed'] == 0
    assert not jobs._timing_pending
    assert jobs.cached(job['id'], 'synthetic-receipt') == {'preserved': True}
    assert jobs.get(job['id'])['timings']['records'] == 3
    with tools.store.connect() as db:
        assert db.execute('SELECT value FROM timing_business_sentinel').fetchone()[0] == 43


def test_next_read_flushes_completed_timing_after_business_transaction(environment: Any) -> None:
    jobs, tools, project = environment
    job = start(environment, [material(tools, project, 'receipt', '17', 'timed-next-read')])
    began = time.monotonic()
    with tools.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        with jobs.timed(job['id'], 'facts_parse'):
            pass
        assert len(jobs._timing_pending) == 1
    assert jobs.list(project)[0]['timings']['records'] == 1
    assert time.monotonic() - began < 1
    assert jobs.get(job['id'])['timings']['records'] == 1
