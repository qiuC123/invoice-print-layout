from pathlib import Path

import pytest

from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_migration import audit, backup, restore
from tests.test_intake_review import image_bytes


def test_backup_restore_preserves_amount_links_and_hashes(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    store = ExpenseStore(root)
    item = store.create({'title': 'test', 'amount': '239'})
    store.add_attachment(item['id'], 'payment.png', image_bytes(), 'payment')
    before = audit(root)
    destination = tmp_path / 'backup'
    backup(root, destination)
    store.update(item['id'], {**store.get(item['id']), 'amount': '240'})
    restore(root, destination)
    assert audit(root) == before
    with pytest.raises(ValueError, match='已存在'):
        backup(root, destination)
    source = next((destination / 'originals').iterdir())
    source.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='校验失败'):
        restore(root, destination)
    assert audit(root) == before
