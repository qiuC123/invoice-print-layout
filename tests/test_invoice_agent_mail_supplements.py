"""New mail evidence resumes the same bounded task without stale approvals."""
from typing import Any

import pytest

from tests.test_invoice_agent import environment, material, start


def test_mail_supplement_preserves_budget_and_is_idempotent(environment: Any) -> None:
    jobs, tools, project = environment
    first = material(tools, project, 'receipt', '17', 'mail-first')
    second = material(tools, project, 'payment', '17', 'mail-second')
    job = start(environment, [first], use_model=True)
    jobs.update(job['id'], state='needs_input', models=2, tools=9, elapsed=3.2,
                question={'id': 'old-question'}, approval={'choice': 'old'})
    result = jobs.append_materials(job['id'], [second['id']], 'mail_new_message_1234')
    assert result['id'] == job['id'] and result['state'] == 'queued'
    assert result['material_ids'] == [first['id'], second['id']]
    assert (result['models'], result['tools'], result['elapsed']) == (2, 9, 3.2)
    assert result['question'] is None and result['approval'] is None
    assert jobs.append_materials(job['id'], [second['id']], 'mail_new_message_1234') == result
    with pytest.raises(ValueError, match='另一组'):
        jobs.append_materials(job['id'], [first['id']], 'mail_new_message_1234')
    assert not tools.store.list_items()


@pytest.mark.parametrize('change', [
    {'state': 'running'}, {'state': 'ready'}, {'item_ids': ['already-created']},
    {'models': 6}, {'tools': 48}, {'elapsed': 180.0},
])
def test_mail_supplement_cannot_restart_finished_or_exhausted_task(environment: Any, change: Any) -> None:
    jobs, tools, project = environment
    first = material(tools, project, 'receipt', '17', 'mail-old')
    second = material(tools, project, 'payment', '17', 'mail-new')
    job = start(environment, [first])
    before = jobs.update(job['id'], state='needs_input', **change) if 'state' not in change else jobs.update(job['id'], **change)
    with tools.store.connect() as db:
        durable_before = db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (job['id'],)).fetchone()['data']
        actions_before = [tuple(row) for row in db.execute(
            'SELECT * FROM invoice_agent_actions WHERE job_id=? ORDER BY key', (job['id'],))]
    with pytest.raises(ValueError):
        jobs.append_materials(job['id'], [second['id']], 'mail_fail_message_1234')
    after = jobs.get(job['id'])
    assert {key: value for key, value in after.items() if key != 'timings'} == {
        key: value for key, value in before.items() if key != 'timings'}
    with tools.store.connect() as db:
        assert db.execute('SELECT data FROM invoice_agent_jobs WHERE id=?', (job['id'],)).fetchone()['data'] == durable_before
        assert [tuple(row) for row in db.execute(
            'SELECT * FROM invoice_agent_actions WHERE job_id=? ORDER BY key', (job['id'],))] == actions_before


def test_mail_supplement_rejects_tampered_old_original(environment: Any) -> None:
    from pathlib import Path
    jobs, tools, project = environment
    first = material(tools, project, 'receipt', '17', 'tamper-old')
    second = material(tools, project, 'payment', '17', 'tamper-new')
    job = start(environment, [first])
    jobs.update(job['id'], state='needs_input')
    Path(first['path']).write_bytes(b'changed-after-question')
    with pytest.raises(ValueError, match='原件已变化'):
        jobs.append_materials(job['id'], [second['id']], 'mail_tamper_message_1234')
    assert jobs.get(job['id'])['material_ids'] == [first['id']]
