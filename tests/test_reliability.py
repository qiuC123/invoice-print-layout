from pathlib import Path

import pytest

from invoice_print_layout.reliability import read_json, save_json


def test_json_state_is_saved_and_replaced(tmp_path: Path) -> None:
    path = tmp_path / 'state' / 'settings.json'
    assert read_json(path) == {}
    save_json(path, {'enabled': True})
    save_json(path, {'enabled': False})
    assert read_json(path) == {'enabled': False}
    assert not path.with_suffix('.tmp').exists()


def test_json_state_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    path.write_text('[]', encoding='utf-8')
    with pytest.raises(ValueError, match='状态文件格式无效'):
        read_json(path)
