"""Rejected read-only investigations remain observations within the durable budget."""
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from tests.test_invoice_agent import Scripted, call, environment, material, start


@pytest.fixture
def recovery_environment(environment: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    # These exercise the loop's planner/rejection contract. Clear missing-source
    # cases now ask directly; their domain shortcut has a separate regression.
    jobs, tools, project = environment
    monkeypatch.setattr(tools, 'next_action', lambda _job: None)
    return jobs, tools, project


@pytest.mark.parametrize('tool,args', [
    ('read_material', {'id': 'outside-current-job'}),
    ('inspect_materials', {'unexpected': 'not accepted'}),
    ('extract_goods', {'item_id': 'not-created-by-job'}),
    ('classify_expense', {'item_id': 'not-created-by-job'}),
    ('name_expense', {'item_id': 'not-created-by-job'}),
])
def test_rejected_investigation_can_reach_real_handoff(recovery_environment: Any, tool: str, args: dict[str, Any]) -> None:
    jobs, tools, project = recovery_environment
    entry = material(tools, project, 'receipt', '17', 'unpaired-for-handoff')
    job = start(recovery_environment, [entry], use_model=True)
    planner = Scripted({'tool': tool, 'arguments': args}, call('ask_user'))
    result = AgentLoop(jobs).run(job['id'], tools, planner)
    assert result['state'] == 'needs_input' and result['question']['text']
    assert result['models'] == 2 and result['tools'] <= 5
    assert any(step['result'].get('tool_error') for step in result['last_results'])
    assert not tools.store.list_items()
    assert Jobs(tools.store).get(job['id'])['models'] == 2


def test_repeated_rejected_read_is_bounded_without_reset(recovery_environment: Any) -> None:
    jobs, tools, project = recovery_environment
    entry = material(tools, project, 'receipt', '17', 'unpaired-repeat-read')
    job = start(recovery_environment, [entry], use_model=True)
    result = AgentLoop(jobs).run(job['id'], tools, Scripted(call('read_material', id='outside-job')))
    assert result['state'] == 'stopped' and '新证据' in result['message']
    assert result['models'] == 3 and result['tools'] < result['limits']['tools']
    assert not tools.store.list_items()
    restored = Jobs(tools.store).get(job['id'])
    assert restored['models'] == result['models'] and restored['elapsed'] == result['elapsed']


def test_exhausted_model_budget_keeps_specific_question_without_another_model(recovery_environment: Any) -> None:
    jobs, tools, project = recovery_environment
    entry = material(tools, project, 'receipt', '17', 'budget-handoff')
    job = start(recovery_environment, [entry], use_model=True,
                limits={'models': 1, 'tools': 8, 'seconds': 30, 'stalls': 2})
    planner = Scripted(call('read_material', id=entry['id']))
    result = AgentLoop(jobs).run(job['id'], tools, planner)
    assert result['state'] == 'stopped' and result['models'] == planner.calls == 1
    assert result['tools'] <= result['limits']['tools']
    assert result['question']['budget_exhausted'] and result['question']['choices'] == []
    assert '付款' in result['question']['text'] and '人工工作台' in result['message']
    assert not tools.store.list_items()
    with pytest.raises(ValueError):
        jobs.answer(job['id'], 'cannot-reset-the-budget')
