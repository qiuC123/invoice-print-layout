"""Pending materials need no project warning; project boundaries still apply."""
from __future__ import annotations

from typing import Any

import pytest

from tests.test_material_matching import queued, setup, snapshots


@pytest.mark.parametrize('role', ['invoice', 'payment'])
@pytest.mark.parametrize('assigned', [False, True])
def test_board_hides_legacy_project_hints_without_assigning_or_rewriting(
        setup: Any, role: str, assigned: bool) -> None:
    matcher, project, _ = setup
    entry = queued(matcher, project if assigned else '', 'Synthetic pending material', role, '50',
                   issues=['商品不明', '归属不明', '归属未定', '金额待确认'])
    before = snapshots(matcher)
    board = matcher.board(project)
    card = next(value for value in board['vouchers' if role == 'invoice' else 'payments']
                if value['id'] == entry['id'])
    assert card['issues'] == ['金额待确认']
    assert card['project_id'] == (project if assigned else '')
    assert snapshots(matcher) == before
    with matcher.store.connect() as db:
        assert matcher._queue(db, entry['id'])['issues'] == entry['issues']
