"""Partition collected test cases, including large parametrized files, only when CI requests it."""
import os

import pytest


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    raw_index = os.environ.get('INVOICE_PYTEST_SHARD_INDEX')
    raw_count = os.environ.get('INVOICE_PYTEST_SHARD_COUNT')
    if raw_index is None and raw_count is None:
        return
    try:
        index, count = int(raw_index), int(raw_count)
    except (TypeError, ValueError):
        raise pytest.UsageError('Set both CI shard index and count to integers') from None
    if not 0 <= index < count:
        raise pytest.UsageError('CI shard index must be within [0, count)')
    selected = items[index::count]
    if not selected:
        raise pytest.UsageError('CI shard selected no test cases')
    config.hook.pytest_deselected(items=[item for number, item in enumerate(items) if number % count != index])
    items[:] = selected
