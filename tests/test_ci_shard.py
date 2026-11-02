"""Shards must split a large parametrized file without losing or duplicating cases."""
from types import SimpleNamespace

import pytest

from tests.ci_shard import pytest_collection_modifyitems


def config(deselected):
    return SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda *, items: deselected.extend(items)))


def test_complete_balanced_partition_of_a_single_large_file(monkeypatch):
    original = [f'tests/large.py::test_fee[{number}]' for number in range(513)]
    groups = []
    monkeypatch.setenv('INVOICE_PYTEST_SHARD_COUNT', '16')
    for index in range(16):
        monkeypatch.setenv('INVOICE_PYTEST_SHARD_INDEX', str(index))
        selected, deselected = original.copy(), []
        pytest_collection_modifyitems(config(deselected), selected)
        assert set(selected).isdisjoint(deselected)
        assert set(selected + deselected) == set(original)
        groups.append(selected)
    flattened = [item for group in groups for item in group]
    assert len(flattened) == len(set(flattened)) == len(original)
    assert set(flattened) == set(original)
    assert max(map(len, groups)) - min(map(len, groups)) <= 1


def test_local_collection_is_unchanged(monkeypatch):
    monkeypatch.delenv('INVOICE_PYTEST_SHARD_INDEX', raising=False)
    monkeypatch.delenv('INVOICE_PYTEST_SHARD_COUNT', raising=False)
    items, deselected = ['first', 'second'], []
    pytest_collection_modifyitems(config(deselected), items)
    assert items == ['first', 'second'] and not deselected


@pytest.mark.parametrize('index,count', [(None, '3'), ('0', None), ('-1', '4'), ('4', '4'),
                                        ('0', '0'), ('x', '4'), ('0', 'x'), ('0', '-1')])
def test_invalid_shard_fails_closed(monkeypatch, index, count):
    for key, value in [('INVOICE_PYTEST_SHARD_INDEX', index), ('INVOICE_PYTEST_SHARD_COUNT', count)]:
        monkeypatch.delenv(key, raising=False) if value is None else monkeypatch.setenv(key, value)
    with pytest.raises(pytest.UsageError):
        pytest_collection_modifyitems(config([]), ['case'])


def test_empty_shard_fails_closed(monkeypatch):
    monkeypatch.setenv('INVOICE_PYTEST_SHARD_INDEX', '1')
    monkeypatch.setenv('INVOICE_PYTEST_SHARD_COUNT', '2')
    with pytest.raises(pytest.UsageError, match='no test cases'):
        pytest_collection_modifyitems(config([]), ['case'])
