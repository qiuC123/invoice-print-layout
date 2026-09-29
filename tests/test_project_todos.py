import pytest

from invoice_print_layout.logistics import LogisticsStore
from invoice_print_layout.project_todos import ProjectTodos


def test_checklist_persistence_isolation_and_conflicts(tmp_path):
    store = LogisticsStore(tmp_path / 'tasks.sqlite3')
    first = store.create_project('第一项目')['id']
    second = store.create_project('第二项目')['id']
    todos = ProjectTodos(store)
    item = todos.save(first, {'title': ' 收集照片 ', 'due_date': '2026-09-27', 'note': '补充工厂装货说明'})
    assert item['title'] == '收集照片' and item['status'] == 'pending'
    assert ProjectTodos(LogisticsStore(store.path)).list(first) == [item]
    assert todos.list(second) == []
    with pytest.raises(ValueError, match='本项目没有'):
        todos.save(second, {**item, 'status': 'done'})
    done = todos.save(first, {**item, 'status': 'done'})
    assert done['revision'] == 2 and done['status'] == 'done'
    with pytest.raises(ValueError, match='已更新'):
        todos.save(first, {**item, 'title': '过期修改'})
    reopened = todos.save(first, {**done, 'status': 'pending', 'title': '补齐照片'})
    assert reopened['status'] == 'pending' and reopened['revision'] == 3
    assert store.snapshot()['tasks'] == store.snapshot()['outbox'] == []
    assert todos.list(first) == [reopened]
    with pytest.raises(ValueError):
        todos.list('missing')


@pytest.mark.parametrize('fields', [
    {'title': ''}, {'title': ['bad']}, {'title': '字' * 201},
    {'due_date': '2026-02-30'}, {'due_date': '20260927'}, {'due_date': []},
    {'status': 'sent'}, {'status': []}, {'note': '字' * 4001},
])
def test_invalid_checklist_inputs_leave_no_record(tmp_path, fields):
    store = LogisticsStore(tmp_path / 'tasks.sqlite3')
    project = store.create_project('项目')['id']
    todos = ProjectTodos(store)
    with pytest.raises(ValueError):
        todos.save(project, {'title': '有效标题', **fields})
    assert todos.list(project) == []


def test_delete_restore_scope_revision_and_old_editor(tmp_path):
    store = LogisticsStore(tmp_path / 'tasks.sqlite3')
    first, second = [store.create_project(name)['id'] for name in ['甲', '乙']]
    todos = ProjectTodos(store)
    item = todos.save(first, {'title': '明天买水'})
    with pytest.raises(ValueError):
        todos.remove(second, item['id'], item['revision'])
    with pytest.raises(ValueError):
        todos.remove(first, item['id'], 0)
    deleted = todos.remove(first, item['id'], item['revision'])
    assert deleted['deleted_at'] and todos.list(first) == []
    assert ProjectTodos(LogisticsStore(store.path)).list(first) == []
    with pytest.raises(ValueError):
        todos.save(first, item)
    with pytest.raises(ValueError):
        todos.remove(first, item['id'], item['revision'], restore=True)
    restored = todos.remove(first, item['id'], deleted['revision'], restore=True)
    assert not restored['deleted_at'] and restored['title'] == item['title']
    assert todos.list(first)[0]['id'] == item['id']
