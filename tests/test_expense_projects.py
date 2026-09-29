from pathlib import Path

import pytest

from invoice_print_layout.expense_projects import ExpenseProjects
from invoice_print_layout.workbench import ExpenseStore


def test_unique_migration_and_move_preserve_expenses(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    old = store.create({'title': '材料', 'project': '甲', 'amount': '14'})
    projects = ExpenseProjects(store)
    first = projects.registry.create_project('甲')
    second = projects.registry.create_project('乙')
    assert projects.migrate() == {'matched': 1, 'pending': 0}
    assert projects.migrate()['matched'] == 0
    current = store.get(old['id'])
    assert current['project_id'] == first['id']
    projects.move([old['id']], second['id'], {old['id']: current['version']})
    moved = store.get(old['id'])
    assert moved['project_id'] == second['id']
    assert moved['amount_cents'] == 1400
    assert moved['attachments'] == old['attachments']
    with pytest.raises(ValueError, match='已变化'):
        projects.move([old['id']], first['id'], {old['id']: current['version']})


def test_stale_edit_and_unknown_project(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': '原事项', 'amount': '239', 'project': '未知'})
    assert ExpenseProjects(store).migrate() == {'matched': 0, 'pending': 1}
    store.update(item['id'], {**item, 'title': '已修改'})
    with pytest.raises(ValueError, match='已变化'):
        store.update(item['id'], {**item, 'expected_version': item['version']})
    assert store.get(item['id'])['title'] == '已修改'
    with pytest.raises(ValueError, match='已变化'):
        store.transition(item['id'], 'cancelled', expected_version=item['version'])
