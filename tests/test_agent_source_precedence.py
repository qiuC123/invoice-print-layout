"""New provenance never silently overturns the operator's exact-source denial."""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from invoice_print_layout.agent_jobs import AgentLoop
from invoice_print_layout.agent_transport import MailOrigin
from tests.test_invoice_agent_relations import relation_env  # noqa: F401
from tests.test_agent_transport import entry, bill, trip, projected


def pair(env: Any, tmp_path: Path) -> list[dict[str, Any]]:
    _, tools, project = env
    invoice = entry(tmp_path, 'taxi', 'invoice', bill('示例承运主体有限公司', '17.78'), '17.78')
    itinerary = entry(tmp_path, 'trip', 'trip', trip('T3出行', '17.78'), '17.78')
    result = []
    for source in [invoice, itinerary]:
        received = tools.intake.receive('neutral.pdf', Path(source['path']).read_bytes(), project_id=project)
        source = projected({**source, 'id': received['id'], 'path': received['path'], 'project_id': project})
        received.update(role=source['role'], text=source['text'], fields=source['fields'],
                        status='review', revision=1, issues=[])
        with tools.store.connect() as db:
            tools.matching._write_queue(db, received)
        result.append(received)
    return result


def test_normal_denial_blocks_later_trusted_same_mail_automatic_proposal(
        relation_env: Any, tmp_path: Path, monkeypatch: Any) -> None:
    jobs, tools, project = relation_env
    rows = pair(relation_env, tmp_path)
    job = jobs.create(project, [e['id'] for e in rows], 'denial-' + str(time.time_ns()))
    waiting = AgentLoop(jobs).run(job['id'], tools)
    assert waiting['state'] == 'needs_input' and waiting['question']['type'] == 'material_relation'
    decline = next(c for c in waiting['question']['choices'] if c['action'] == 'not_same_expense')
    jobs.answer(job['id'], decline['id'], waiting['question']['id'])
    origins = {e['id']: MailOrigin('a' * 64, 'INBOX', '2030001', 42, '<source@example.invalid>', e['digest'])
               for e in rows}
    monkeypatch.setattr('invoice_print_layout.agent_mail_origins.load_trusted_mail_origins', lambda *_: origins)
    again = jobs.create(project, [e['id'] for e in rows], 'after-mail-' + str(time.time_ns()))
    # The transport parser does recognize this new authority as a unique pair;
    # the common proposal gate must still honor the later human denial.
    from invoice_print_layout.agent_combinations import extended_candidates
    assert extended_candidates(rows, {e['id']: {'name_conflict': False} for e in rows},
                               scope=set(again['material_ids']), trusted_mail_origins=origins)
    scan = tools._scan(again)
    assert not scan['proposals'] and not scan['choices'] and not scan['relations']['candidates']
    done = AgentLoop(jobs).run(again['id'], tools)
    assert done['state'] == 'needs_input' and not done['item_ids'] and not tools.store.list_items()


def test_current_answer_receipt_does_not_change_its_own_source_snapshot(relation_env: Any) -> None:
    from tests.test_invoice_agent_relations import weak_pair
    jobs, tools, project = relation_env
    rows = weak_pair(relation_env)
    job = jobs.create(project, [e['id'] for e in rows], 'snapshot-' + str(time.time_ns()))
    waiting = AgentLoop(jobs).run(job['id'], tools)
    before = tools.fingerprint(waiting)
    choice = next(c for c in waiting['question']['choices'] if c['action'] == 'confirm_relation')
    queued = jobs.answer(job['id'], choice['id'], waiting['question']['id'])
    assert tools.fingerprint(queued) == before
    # A different task must observe the newly recorded historical fact.
    assert tools.fingerprint({**queued, 'id': 'another-task'}) != before
