from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from PIL import Image
import pymupdf
import pytest

import invoice_print_layout.receipt_intake as intake
import invoice_print_layout.reimbursement as report
from invoice_print_layout.report_excel import TEMPLATE_NAME
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.transaction_evidence import extract_transaction_evidence, record_identifier_keys
from invoice_print_layout.workbench import ExpenseStore, expense_identifier_keys
from tests.helpers import make_invoice_pdf


NUMBER = '4006506215532850730'


def identifiers(kind: str = 'shopping', number: str = NUMBER) -> list[dict[str, str]]:
    text, role = ('淘宝闪购\n订单号：' + number, 'order') if kind == 'shopping' else (
        '支付宝\n淘宝闪购\n支付成功\n订单号：' + number, 'payment')
    return extract_transaction_evidence(text, role)['identifiers']


def values(**extra: Any) -> dict[str, Any]:
    return {'title': '合成测试材料', 'category': '其他', 'amount': '17.00',
            'material_basis': 'payment_only', 'expense_date': '', **extra}


def picture(color: str = 'white') -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (80, 120), color).save(stream, format='PNG')
    return stream.getvalue()


def lines(*text: str) -> list[OcrLine]:
    return [OcrLine(row, 5, index * 40, 580, index * 40 + 28) for index, row in enumerate(text)]


def test_transaction_metadata_survives_create_and_title_edit(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    metadata = {'shopping_platform': '淘宝闪购', 'shop_name': '测试便利店(演示店)',
                'payment_channel': '支付宝', 'transaction_identifiers': identifiers(),
                'transaction_evidence': {'shopping_platform': '感谢你对淘宝闪购的信任'}}
    item = store.create(values(**metadata))
    changed = store.update(item['id'], {'title': '仅修改标题'})
    for key, value in metadata.items():
        assert changed[key] == value
    with store.connect() as db:
        stored = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
    assert all(stored[key] == value for key, value in metadata.items())


@pytest.mark.parametrize('bad', [
    {'shopping_platform': []}, {'shop_name': 17}, {'payment_channel': None},
    {'shopping_platform': 'x' * 201}, {'transaction_identifiers': {}},
    {'transaction_identifiers': [{'kind': [], 'value': NUMBER}]},
    {'transaction_identifiers': [{'kind': 'invoice', 'value': NUMBER}]},
    {'transaction_identifiers': [{'kind': 'shopping_order', 'namespace': [], 'value': NUMBER}]},
    {'transaction_identifiers': [{'kind': 'shopping_order', 'namespace': '淘宝闪购', 'value': ''}]},
    {'transaction_identifiers': [{'kind': 'shopping_order', 'namespace': '淘宝闪购', 'value': 'not-an-order'}]},
    {'transaction_evidence': {'shop_name': []}},
])
def test_invalid_metadata_does_not_create_expense(tmp_path: Path, bad: dict[str, Any]) -> None:
    store = ExpenseStore(tmp_path)
    with pytest.raises(ValueError):
        store.create(values(**bad))
    assert store.list_items() == []


def test_local_receipt_separates_source_actors_and_typed_ids() -> None:
    parsed = intake.parse_receipt(lines('订单已送达', '感谢你对淘宝闪购的信任',
        '店铺：测试便利店(演示店)', '价格明细 实付款17.00', '支付方式：支付宝', '订单号：' + NUMBER))
    fields = parsed['fields']
    assert fields['shopping_platform'] == '淘宝闪购'
    assert fields['shop_name'] == '测试便利店(演示店)'
    assert fields['payment_channel'] == '支付宝'
    assert fields['merchant'] == fields['shop_name']
    assert fields['transaction_evidence']['shopping_platform']
    assert record_identifier_keys(fields) == record_identifier_keys({'transaction_identifiers': identifiers()})


def test_invalid_transaction_edit_is_atomic(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create(values(transaction_identifiers=identifiers()))
    with pytest.raises(ValueError, match='交易编号'):
        store.update(item['id'], {'transaction_identifiers': [{'kind': 'untyped_order', 'value': []}]})
    assert store.get(item['id']) == item


def test_same_digits_in_different_systems_do_not_block_save(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    existing = store.create(values(order_number=NUMBER, transaction_identifiers=identifiers('shopping')))
    saved = intake.save_receipt(store, 'payment.png', picture(),
        values(order_number=NUMBER, transaction_identifiers=identifiers('payment')), 'payment')
    assert saved['duplicates'] == []
    assert saved['item']['id'] != existing['id']
    assert len(store.list_items()) == 2


def test_same_typed_id_with_different_images_is_duplicate(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    first = intake.save_receipt(store, 'one.png', picture(),
        values(transaction_identifiers=identifiers()), 'payment')['item']
    result = intake.save_receipt(store, 'two.png', picture('red'),
        values(transaction_identifiers=identifiers()), 'payment')
    assert result['duplicates'][0]['id'] == first['id']
    assert result['duplicates'][0]['reason'] == '相同体系的交易编号'
    assert len(store.list_items()) == 1


def test_legacy_untyped_order_does_not_guess_scope(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    old = store.create(values(order_number=NUMBER, shopping_platform='淘宝闪购'))
    with store.connect() as db:
        assert expense_identifier_keys(db, old['id'], old) == set()
    assert intake.find_duplicates(store, 'different', NUMBER) == []
    result = intake.save_receipt(store, 'new.png', picture(),
        values(order_number=NUMBER, transaction_identifiers=identifiers()), 'payment')
    assert result['duplicates'] == [] and result['item']['id'] != old['id']


def test_historical_linked_source_can_derive_typed_key_without_write(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create(values(order_number=NUMBER))
    with store.connect() as db:
        db.execute('CREATE TABLE intake_queue(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        entry = {'id': 'old-source', 'item_id': item['id'], 'role': 'order',
                 'text': '淘宝闪购\n订单号：' + NUMBER, 'fields': {}}
        db.execute('INSERT INTO intake_queue VALUES (?,?)', (entry['id'], json.dumps(entry)))
        before = db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data']
        assert expense_identifier_keys(db, item['id'], item) == record_identifier_keys(entry)
    result = intake.find_duplicates(store, 'different', transaction_identifiers=identifiers())
    assert result[0]['id'] == item['id']
    with store.connect() as db:
        assert db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'] == before


def test_nullable_source_can_read_associated_original_without_crash(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create(values())
    with store.connect() as db:
        db.execute('CREATE TABLE intake_queue(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        entry = {'id': 'associated-source', 'item_id': item['id'], 'role': 'order',
                 'text': '淘宝闪购\n订单号：' + NUMBER}
        db.execute('INSERT INTO intake_queue VALUES (?,?)', (entry['id'], json.dumps(entry)))
        source = db.execute('SELECT source FROM expenses WHERE id=?', (item['id'],)).fetchone()['source']
        assert source is None
        assert expense_identifier_keys(db, item['id'], item, source=source) == record_identifier_keys(entry)


@pytest.mark.parametrize('released', ['undone_material_group', 'merged_into'])
def test_released_audit_shell_does_not_own_identifier(tmp_path: Path, released: str) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create(values(transaction_identifiers=identifiers()))
    with store.connect() as db:
        data = json.loads(db.execute('SELECT data FROM expenses WHERE id=?', (item['id'],)).fetchone()['data'])
        data.update(stage='cancelled', **{released: 'synthetic-group'})
        db.execute('UPDATE expenses SET data=? WHERE id=?', (json.dumps(data), item['id']))
    result = intake.save_receipt(store, 'again.png', picture(),
        values(transaction_identifiers=identifiers()), 'payment')
    assert result['duplicates'] == [] and result['item']['id'] != item['id']


@pytest.fixture
def reporting_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExpenseStore:
    store = ExpenseStore(tmp_path)
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))
    monkeypatch.setattr(report, 'create_excel', lambda _template, _items, _options, output: output.write_bytes(b'synthetic'))
    return store


def ready(store: ExpenseStore, color: str, **extra: Any) -> dict[str, Any]:
    item = store.create(values(**extra))
    store.add_attachment(item['id'], 'proof.png', picture(color), 'payment')
    return store.transition(item['id'], 'verify')


def test_report_blocks_same_typed_id_across_expenses_with_different_images(reporting_store: ExpenseStore) -> None:
    a = ready(reporting_store, 'white', transaction_identifiers=identifiers())
    b = ready(reporting_store, 'red', transaction_identifiers=identifiers())
    with pytest.raises(ValueError, match='同一体系'):
        report.create_report(reporting_store, [a['id'], b['id']], submit=False)
    assert not (reporting_store.root / '导出').exists()


@pytest.mark.parametrize('scoped', [False, True])
def test_report_allows_same_digits_from_unknown_or_different_systems(reporting_store: ExpenseStore, scoped: bool) -> None:
    a = ready(reporting_store, 'white', order_number=NUMBER,
              **({'transaction_identifiers': identifiers('shopping')} if scoped else {}))
    b = ready(reporting_store, 'red', order_number=NUMBER,
              **({'transaction_identifiers': identifiers('payment')} if scoped else {}))
    result = report.create_report(reporting_store, [a['id'], b['id']], submit=False)
    with pymupdf.open(reporting_store.root / '导出' / result['pdf']) as document:
        assert len(document) == 2
    assert all(item['stage'] == 'draft' for item in reporting_store.list_items())


@pytest.mark.parametrize('mismatch', ['payment', 'unknown', 'shopping'])
def test_takeout_compares_business_order_only_with_same_platform_scope(
        reporting_store: ExpenseStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str) -> None:
    order_path = tmp_path / 'order.png'
    Image.new('RGB', (600, 1000), 'white').save(order_path)
    observed = lines('订单已送达', '淘宝闪购', '闪购 测试餐厅', '实付￥123.45', '订单号' + NUMBER)
    monkeypatch.setattr(intake, 'read_receipt', lambda *args: observed)
    other = '4006506215532850731'
    metadata = {} if mismatch == 'unknown' else {'transaction_identifiers': identifiers(mismatch, other)}
    item = reporting_store.create(values(category='外卖', amount='123.45', material_basis='standard',
        order_number=other, **metadata))
    reporting_store.add_attachment(item['id'], order_path.name, order_path.read_bytes(), 'order')
    invoice = make_invoice_pdf(tmp_path / 'invoice.pdf')
    reporting_store.add_attachment(item['id'], invoice.name, invoice.read_bytes(), 'invoice')
    item = reporting_store.transition(item['id'], 'verify')
    target = tmp_path / 'composed'
    target.mkdir()
    if mismatch == 'shopping':
        with pytest.raises(ValueError, match='同一购物平台'):
            report.compose_matter(reporting_store, item, target)
    else:
        result, _ = report.compose_matter(reporting_store, item, target)
        with pymupdf.open(result) as document:
            assert len(document) == 1 and document[0].get_images()
