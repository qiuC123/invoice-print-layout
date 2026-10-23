"""The synthesis desk collects mail materials without creating fees, with or without AI."""
import json
from typing import Any

import pytest

from invoice_print_layout import workbench_web
from invoice_print_layout.intake_queue import IntakeQueue
from tests.test_glm_pipeline_http import FakeAI, web
from tests.helpers import make_invoice_pdf


@pytest.mark.parametrize('enabled', [True, False])
def test_material_only_mail_preserves_other_projects_and_uses_prepare(
        web: Any, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    _, post, project, entry, store = web
    monkeypatch.setattr(FakeAI, 'enabled', enabled)
    queue = IntakeQueue(store)
    other = queue.receive('other.pdf', make_invoice_pdf(store.root/'other.pdf', amount='63.00').read_bytes())
    for row in (entry, other):
        with store.connect() as db:
            data = queue.get(row['id'])
            data['source'] = 'mail'
            db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data), row['id']))
    untouched = queue.get(other['id'])
    calls: list[tuple[str, Any]] = []

    def sync(_store: Any, *, organize: bool = True) -> dict[str, Any]:
        assert organize is False
        calls.append(('sync', organize))
        return {'message': 'Synthetic material collection', 'downloaded': 1}

    def imported(_queue: Any, pid: str) -> list[Any]:
        calls.append(('import', pid))
        return []

    def prepared(_queue: Any, key: str) -> dict[str, Any]:
        calls.append(('prepare', key))
        return _queue.get(key)

    monkeypatch.setattr(workbench_web, 'sync_mail', sync)
    monkeypatch.setattr(IntakeQueue, 'import_mail', imported)
    monkeypatch.setattr(IntakeQueue, 'prepare_batch_entry', prepared)
    monkeypatch.setattr(IntakeQueue, 'analyze', lambda *_: pytest.fail('Legacy fee creation was invoked'))
    result = post('mail', {'project_id': project, 'material_only': True})
    assert result['downloaded'] == 1
    assert calls == [('sync', False), ('import', project), ('prepare', entry['id'])]
    assert not store.list_items()
    assert queue.get(other['id']) == untouched
