"""Request-local reuse must still reject concurrent ledger/source changes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_material_matching import setup, auto_expense, auto_queued, proof
from tests.test_invoice_render_duplicates import material, save


@pytest.mark.parametrize('change', ['', 'ledger', 'source'])
def test_board_reuses_expenses_but_rechecks_changes(setup: Any, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    matcher, project, _ = setup
    item = auto_expense(matcher, project, 'Existing invoice')
    auto_queued(matcher, project, 'Payment', 'payment', '14')
    original_list = matcher.store.list_items
    reads = 0

    def list_items(**kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        return original_list(**kwargs)

    monkeypatch.setattr(matcher.store, 'list_items', list_items)
    original_state = matcher._rules_material_state
    changed = False

    def material_state(*args: Any, **kwargs: Any) -> Any:
        nonlocal changed
        result = original_state(*args, **kwargs)
        if change and not changed:
            changed = True
            with matcher.store.connect() as db:
                if change == 'ledger':
                    row = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()
                    data = json.loads(row['data'])
                    data['verified'] = True
                    db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
                else:
                    path = db.execute('SELECT path FROM attachments WHERE item_id=?', (item['id'],)).fetchone()[0]
                    Path(path).unlink()
        return result

    monkeypatch.setattr(matcher, '_rules_material_state', material_state)
    board = matcher.board(project)
    if change:
        assert reads == 2 and not board['suggestions']
        assert any('扫描期间已变化' in conflict['reason'] for conflict in board['suggestion_conflicts'])
    else:
        assert reads == 1 and len(board['suggestions']) == 1


def test_seller_cache_keeps_live_digest_validation(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    entry = auto_queued(matcher, project, 'Invoice', 'invoice', '14')
    calls = 0

    def seller(page: Any) -> str:
        nonlocal calls
        calls += 1
        return 'Synthetic seller'

    monkeypatch.setattr('invoice_print_layout.material_matching.positioned_invoice_seller', seller)
    assert matcher._native_invoice_seller(entry) == 'Synthetic seller'
    assert matcher._native_invoice_seller(entry) == 'Synthetic seller'
    assert calls == 1
    Path(entry['path']).write_bytes(proof('Replaced original'))
    assert matcher._native_invoice_seller(entry) == '' and calls == 1
    Path(entry['path']).unlink()
    assert matcher._native_invoice_seller(entry) == ''


def test_expense_reuse_is_checked_inside_final_pool_scan(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    matcher, project, _ = setup
    item = auto_expense(matcher, project, 'Final scan invoice')
    auto_queued(matcher, project, 'Final scan payment', 'payment', '14')
    assert matcher.board(project)['suggestions']
    original = matcher._rules_current_pool_snapshot

    def snapshot(*args: Any, **kwargs: Any) -> Any:
        with matcher.store.connect() as db:
            row = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()
            data = json.loads(row['data'])
            data['verified'] = True
            db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
        return original(*args, **kwargs)

    monkeypatch.setattr(matcher, '_rules_current_pool_snapshot', snapshot)
    board = matcher.board(project)
    assert not board['suggestions']
    assert any('扫描期间已变化' in row['reason'] for row in board['suggestion_conflicts'])


def test_render_duplicate_never_enters_board_or_creation(setup: Any) -> None:
    matcher, project, _ = setup
    pdf = material(matcher.intake, 'x.pdf', number='12345678', project_id=project,
                   fields={'invoice_number': '12345678', 'amount': '249.50'})
    image = material(matcher.intake, 'x.pdf.png', project_id=project)
    board = matcher.board(project)
    assert [card['id'] for card in board['vouchers']] == [pdf['id']]
    assert board['vouchers'][0]['amount_cents'] == 24950
    assert all(not board[key] for key in ('payments', 'other', 'unused', 'archived', 'deleted', 'matches'))
    duplicate = matcher.intake.get(image['id'])
    assert duplicate['duplicate_of'] == pdf['id']
    assert not matcher._available(duplicate, project)
    with matcher.store.connect() as db:
        assert 'q:' + image['id'] not in matcher._rules_current_pool_snapshot(db, project)
    save(matcher.intake, pdf, status='linked', item_id='synthetic-existing-expense')
    assert all(not matcher.board(project)[key] for key in ('vouchers', 'payments', 'other'))
    assert Path(image['path']).is_file()


def test_suggestion_cache_checks_sources_owners_and_never_shares_mutable_results(
        setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_platform_transaction_matching import material as transaction, ORDER
    matcher, project, _ = setup
    voucher = transaction(matcher, project, 'order')
    transaction(matcher, project, 'payment')
    original = matcher._suggestions_from_board
    scans = 0

    def scan(*args: Any, **kwargs: Any) -> Any:
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(matcher, '_suggestions_from_board', scan)
    first = matcher.board(project)
    assert len(first['suggestions']) == 1
    first['suggestions'][0]['warnings'].append('caller mutation')
    second = matcher.board(project)
    assert scans == 1 and 'caller mutation' not in second['suggestions'][0]['warnings']
    matcher.store.create({'title': 'Another owner', 'project_id': project, 'amount': '17', 'category': '其他',
                          'transaction_identifiers': [{'kind': 'shopping_order', 'namespace': '淘宝闪购',
                                                       'value': ORDER, 'label': '订单号', 'quote': ORDER}]})
    assert not matcher.board(project)['suggestions'] and scans == 2
    Path(voucher['path']).write_bytes(proof('Changed source with unchanged ledger revision'))
    assert not matcher.board(project)['suggestions'] and scans == 3
