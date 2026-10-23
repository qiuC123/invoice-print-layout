"""A completed worker cannot overwrite a newly accepted same-job continuation."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, AgentService, Jobs
from invoice_print_layout.agent_tools import InvoiceTools
from invoice_print_layout.workbench import ExpenseStore
from tests.test_invoice_agent import environment, material, start
from tests.test_invoice_agent_confirmations import persisted_confirmation_state, question


def confirmation_barrier_worker(workspace: str, key: str) -> None:
    """Pause a real process after publishing a valid question but before exit."""
    root = Path(workspace)
    store = ExpenseStore(root)
    jobs = Jobs(store)
    job = jobs.get(key)
    if not job.get('confirmations'):
        jobs.update(key, state='running', run_started=time.time())
        jobs.stop(key, 'needs_input', job['question']['text'])
        (root / 'old-worker-paused.json').write_text(json.dumps({'pid': os.getpid(), 'id': key}), encoding='utf-8')
        deadline = time.monotonic() + 8
        while not (root / 'release-old-worker').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Test barrier was not released')
            time.sleep(.01)
        return
    (root / 'continuation-worker.json').write_text(json.dumps({
        'pid': os.getpid(), 'id': key, 'queue_revision': job.get('queue_revision'),
        'models': job['models'], 'tools': job['tools'], 'elapsed': job['elapsed'],
        'limits': job['limits'], 'confirmations': job['confirmations']}), encoding='utf-8')
    # This is a supervisor concurrency test, not the separate real-file report
    # acceptance. Keep the same synthetic report renderer as environment while
    # executing the real matching, confirmation, write and content tools.
    from invoice_print_layout import report_print_preview
    def preview(store: ExpenseStore, ids: list[str], options: dict[str, Any]) -> dict[str, Any]:
        items = store.get_many(ids)
        return {'pdf': 'synthetic-supervisor-preview.pdf', 'xlsx': 'synthetic-supervisor-preview.xlsx',
                'total_cents': sum(i['amount_cents'] for i in items), 'page_count': 2,
                'preview_pages': [], 'ordered_item_ids': ids}
    report_print_preview.create_report_print_preview = preview
    AgentLoop(jobs).run(key, InvoiceTools(jobs))


@pytest.mark.parametrize('legacy', [False, True])
def test_answer_while_previous_spawn_worker_exits_continues_same_job(environment: Any, legacy: bool) -> None:
    jobs, tools, _ = environment
    waiting, q = question(environment)
    jobs.update(waiting['id'], state='queued')
    if legacy:
        with tools.store.connect() as db:
            row = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (waiting['id'],)).fetchone()
            old = json.loads(row['data'])
            old.pop('queue_revision')
            db.execute('UPDATE invoice_agent_jobs SET data=? WHERE id=?', (json.dumps(old), waiting['id']))
    original_bytes = {e['path']: hashlib.sha256(Path(e['path']).read_bytes()).hexdigest()
                      for e in [tools.intake.get(i) for i in waiting['material_ids']]}
    service = AgentService(tools.store, worker=confirmation_barrier_worker)
    root = tools.store.workspace
    try:
        service.kick()
        deadline = time.monotonic() + 8
        while not (root / 'old-worker-paused.json').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert (root / 'old-worker-paused.json').exists()
        old_worker = json.loads((root / 'old-worker-paused.json').read_text('utf-8'))
        assert any(p.pid == old_worker['pid'] and p.is_alive() for p in multiprocessing.active_children())
        checkpoint = jobs.get(waiting['id'])
        assert checkpoint['state'] == 'needs_input'
        accepted = jobs.answer(waiting['id'], 'same-expense', q['id'])
        assert accepted['id'] == waiting['id'] and accepted['state'] == 'queued'
        assert accepted['queue_revision'] == checkpoint.get('queue_revision', 0) + 1 == 1
        assert all(accepted[k] == checkpoint[k] for k in ('models', 'tools', 'elapsed', 'limits'))
        receipt = accepted['confirmations'][0]
        assert receipt['actor'] == 'user' and receipt['question_id'] == q['id']
        # Reproduce the exact race: kick sees the old thread still alive, then
        # that old process exits while the accepted job is already queued.
        service.kick()
        (root / 'release-old-worker').touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            done = jobs.get(waiting['id'])
            if done['state'] in {'ready', 'failed', 'stopped', 'cancelled'}:
                break
            time.sleep(.02)
        assert done['state'] == 'ready', done['message']
        continuation = json.loads((root / 'continuation-worker.json').read_text('utf-8'))
        assert continuation['id'] == done['id'] == waiting['id']
        assert continuation['pid'] != old_worker['pid'] and continuation['queue_revision'] == 1
        assert all(continuation[k] == accepted[k] for k in ('models', 'tools', 'elapsed', 'limits', 'confirmations'))
        assert done['confirmations'] == [receipt] and done['models'] == accepted['models'] == 1
        assert done['tools'] > accepted['tools'] and done['elapsed'] >= accepted['elapsed']
        assert done['limits'] == waiting['limits']
        assert done['preview']['total_cents'] == 1700
        assert len(jobs.list(done['project_id'])) == len(tools.store.list_items()) == 1
        expense = tools.store.list_items()[0]
        assert expense['amount_cents'] == 1700 and not expense['verified'] and expense['stage'] == 'draft'
        assert len(expense['attachments']) == 2
        assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value for path, value in original_bytes.items())
    finally:
        (root / 'release-old-worker').touch()
        service.close()


def test_old_dispatch_stop_is_atomic_noop_after_answer(environment: Any) -> None:
    jobs, _, _ = environment
    waiting, q = question(environment)
    accepted = jobs.answer(waiting['id'], 'same-expense', q['id'])
    before = persisted_confirmation_state(jobs, waiting['id'])
    for target in ('failed', 'stopped', 'queued'):
        observed = jobs.stop_dispatch(waiting['id'], 0, target, 'old process outcome')
        assert observed['state'] == 'queued' and observed['queue_revision'] == 1
        assert all(observed[k] == accepted[k] for k in ('models', 'tools', 'elapsed', 'limits', 'confirmations', 'approval'))
        assert persisted_confirmation_state(jobs, waiting['id']) == before


@pytest.mark.parametrize('terminal', ['cancelled', 'stopped', 'ready', 'needs_input'])
def test_supervisor_failure_cannot_overwrite_terminal_checkpoint(environment: Any, terminal: str) -> None:
    jobs, _, _ = environment
    waiting, _ = question(environment)
    state = jobs.update(waiting['id'], state=terminal)
    before = persisted_confirmation_state(jobs, waiting['id'])
    observed = jobs.stop_dispatch(waiting['id'], 0, 'failed', 'late worker failure')
    assert observed['state'] == state['state'] == terminal
    assert persisted_confirmation_state(jobs, waiting['id']) == before


def test_same_dispatch_crash_still_fails_and_preserves_cumulative_budget(environment: Any) -> None:
    jobs, tools, _ = environment
    job = start(environment, [material(tools, environment[2], 'receipt', '17', 'dispatch-crash')])
    jobs.update(job['id'], state='running', models=2, tools=5, elapsed=3, run_started=time.time() - .2)
    failed = jobs.stop_dispatch(job['id'], 0, 'failed', 'owned worker crashed')
    assert failed['state'] == 'failed' and failed['models'] == 2 and failed['tools'] == 5
    assert failed['elapsed'] >= 3.2 and failed['run_started'] is None
    assert failed['limits'] == job['limits']


def test_retry_and_mail_supplement_each_advance_only_the_queue_generation(environment: Any) -> None:
    jobs, tools, project = environment
    first = material(tools, project, 'receipt', '17', 'generation-order')
    second = material(tools, project, 'payment', '17', 'generation-payment')
    job = start(environment, [first])
    jobs.update(job['id'], state='failed', models=2, tools=5, elapsed=3.2)
    retried = jobs.retry(job['id'])
    assert retried['queue_revision'] == 1
    assert (retried['models'], retried['tools'], retried['elapsed']) == (2, 5, 3.2)
    jobs.update(job['id'], state='needs_input')
    appended = jobs.append_materials(job['id'], [second['id']], 'mail_generation_1234')
    assert appended['queue_revision'] == 2 and appended['id'] == retried['id']
    assert (appended['models'], appended['tools'], appended['elapsed']) == (2, 5, 3.2)
    assert jobs.append_materials(job['id'], [second['id']], 'mail_generation_1234')['queue_revision'] == 2
    assert jobs.stop_dispatch(job['id'], 1, 'failed', 'stale process')['state'] == 'queued'


def quick_dispatch_worker(workspace: str, key: str) -> None:
    """A no-tool worker isolates scheduler wakeup from invoice parsing."""
    root = Path(workspace)
    jobs = Jobs(ExpenseStore(root))
    jobs.update(key, state='running', run_started=time.time())
    (root / 'quick-dispatch.json').write_text(json.dumps({'id': key, 'pid': os.getpid()}), encoding='utf-8')
    jobs.stop(key, 'ready', 'synthetic scheduler checkpoint')


@pytest.mark.parametrize('boundary', ['before_idle_check', 'after_idle_check'])
def test_empty_queue_exit_and_kick_cannot_lose_enqueue(environment: Any, monkeypatch: pytest.MonkeyPatch,
                                                      boundary: str) -> None:
    jobs, tools, project = environment
    entry = material(tools, project, 'receipt', '17', 'idle-handoff-' + boundary)
    service = AgentService(tools.store, worker=quick_dispatch_worker)
    entered, release = threading.Event(), threading.Event()
    original_idle = service._idle_or_resume
    observed: dict[str, Any] = {}

    def pause() -> None:
        observed['thread'] = threading.current_thread()
        entered.set()
        assert release.wait(8), 'Test did not release the idle boundary'

    def idle_boundary() -> bool:
        if not observed:
            if boundary == 'before_idle_check':
                pause()
            resume = original_idle()
            if boundary == 'after_idle_check' and not resume:
                pause()
            return resume
        return original_idle()

    monkeypatch.setattr(service, '_idle_or_resume', idle_boundary)
    try:
        service.kick()  # No Agent jobs yet: pause after the first empty read.
        assert entered.wait(8)
        old_thread = observed['thread']
        assert old_thread.is_alive()
        job = start(environment, [entry])
        service.kick()
        if boundary == 'before_idle_check':
            assert service._thread is old_thread  # kick did not create a second consumer.
        else:
            assert service._thread is not old_thread and service._thread is not None
        release.set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            done = jobs.get(job['id'])
            if done['state'] in {'ready', 'failed', 'stopped'}:
                break
            time.sleep(.02)
        assert done['state'] == 'ready'
        assert json.loads((tools.store.workspace / 'quick-dispatch.json').read_text('utf-8'))['id'] == job['id']
        assert done['id'] == job['id'] and done['models'] == done['tools'] == 0
        assert done['limits'] == job['limits'] and len(jobs.list(project)) == 1
        assert not tools.store.list_items()
    finally:
        release.set()
        service.close()
        if observed.get('thread'):
            observed['thread'].join(2)
            assert not observed['thread'].is_alive()
