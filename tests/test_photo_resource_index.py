import pytest

from invoice_print_layout.photo_wechat_bridge import unique_resource_id


def record(identifier: bytes) -> bytes:
    return b'\x12\x22\x0a\x20' + identifier


def test_identical_duplicate_rows_resolve_to_one_resource() -> None:
    packed = record(b'a' * 32)
    assert unique_resource_id([packed, packed]) == 'a' * 32


@pytest.mark.parametrize('values', [
    [], [b''], [b'unrecognized'],
    [record(b'a' * 32), record(b'b' * 32)],
    [record(b'a' * 32), record(b'a' * 32) + b'different-metadata'],
    [record(b'a' * 32) + record(b'b' * 32)],
])
def test_missing_or_conflicting_resource_index_is_not_guessed(values: list[bytes]) -> None:
    assert unique_resource_id(values) is None
