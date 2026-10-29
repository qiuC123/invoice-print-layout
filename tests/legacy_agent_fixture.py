"""Create pre-upgrade waiting records for legacy answer/retry API contracts.

Normal organizing tests must use AgentLoop. These fixtures stop at the old
question boundary solely to exercise persisted questions from older versions.
"""
from typing import Any

from invoice_print_layout.agent_jobs import AgentLoop


class LegacyQuestionFixture(AgentLoop):
    def run(self, key: str, tools: Any, planner: Any = None) -> dict[str, Any]:
        execute = tools.execute

        class WaitingRecordCreated(RuntimeError):
            pass

        def record(name: str, args: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
            result = execute(name, args, job)
            if name == 'ask_user':
                current = self.jobs.get(key)
                self.jobs.update(key, question=result['question'], last_results=[
                    *current['last_results'][-7:], {'tool': name, 'result': result}])
                self.jobs.stop(key, 'needs_input', result['question']['text'])
                raise WaitingRecordCreated
            return result  # type: ignore[no-any-return]

        tools.execute = record
        try:
            return super().run(key, tools, planner)
        except WaitingRecordCreated:
            return self.jobs.get(key)
        finally:
            tools.execute = execute


def review_question(job: dict[str, Any]) -> dict[str, Any]:
    """The observation remains auditable even though the task no longer asks it."""
    assert job['state'] == 'ready' and job['question'] is None
    return next(step['result']['question'] for step in reversed(job['last_results'])
                if step['tool'] == 'ask_user')  # type: ignore[no-any-return]
