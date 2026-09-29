from pathlib import Path

import pytest

from invoice_print_layout.expense_preferences import Preferences
from invoice_print_layout.report_snapshot import snapshot
from invoice_print_layout.workbench import ExpenseStore


def test_exact_goods_and_purpose_conflict_and_disable(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': '便利店购买', 'amount': '14.50'})
    prefs = Preferences(store)
    rule = prefs.save({'kind': 'category', 'scope': 'exact_content', 'match': '饭团', 'purpose': '途中用餐', 'value': '餐饮'})
    assert prefs.apply(item, goods='口罩', purpose='途中用餐')['changes'] == {}
    assert prefs.apply(item, goods='饭团', purpose='途中用餐')['changes'] == {'category': '餐饮'}
    prefs.save({**rule, 'id': '', 'value': '材料采购'})
    assert prefs.apply(item, goods='饭团', purpose='途中用餐')['conflicts'] == ['category']
    prefs.save({**rule, 'enabled': False})
    assert prefs.apply(item, goods='饭团', purpose='途中用餐')['changes'] == {'category': '材料采购'}
    with pytest.raises(ValueError, match='仅限本笔'):
        prefs.save({'kind': 'material_basis', 'scope': 'exact_content', 'match': '罚款', 'purpose': '罚款', 'value': 'payment_only'})


def test_receipt_default_is_explicit_business_preference(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': '线下收据', 'amount': '14'})
    prefs = Preferences(store)
    assert prefs.apply(item, receipt=True)['changes'] == {}
    prefs.save({'kind': 'category', 'scope': 'receipt_default', 'match': '线下收据无明确商品', 'value': '材料采购'})
    assert prefs.apply(item, receipt=True)['changes'] == {'category': '材料采购'}
    assert prefs.apply(item, goods='饭团', receipt=True)['changes'] == {}
    assert prefs.apply(item, receipt=False)['changes'] == {}


def test_raincoat_display_group_does_not_merge_ledger(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    a = store.create({'title': '商家 · 雨衣（规格一）', 'category': '材料采购', 'project': '甲', 'amount': '88.70'})
    b = store.create({'title': '雨衣33件', 'category': '材料采购', 'project': '甲', 'amount': '28.70'})
    c = store.create({'title': '餐费', 'category': '餐饮', 'project': '甲', 'amount': '49.50'})
    result = snapshot([a, b, c])
    rain = next(row for row in result['rows'] if row['row'] == 24)
    assert rain['amount_cents'] == 11740
    assert rain['name'] == '雨衣采购'
    assert len(rain['items']) == 2
    assert next(row for row in result['rows'] if row['row'] == 19)['amount_cents'] == 4950
    assert len(store.list_items()) == 3
    changed = store.update(b['id'], {**b, 'project': '乙'})
    assert len(snapshot([a, changed])['rows']) == 2
