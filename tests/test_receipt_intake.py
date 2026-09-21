from __future__ import annotations

import base64
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

import invoice_print_layout.receipt_intake as intake
from invoice_print_layout.takeout import OcrLine, TakeoutError
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.workbench_web import make_server


def lines(*texts: str) -> list[OcrLine]:
    return [OcrLine(text, 0, i*40, 500, i*40+25) for i, text in enumerate(texts)]


def picture(color: str = 'white') -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (40, 80), color).save(stream, format='PNG')
    return stream.getvalue()


def fields(**changes: Any) -> dict[str, Any]:
    return {'title': 'Synthetic purchase', 'category': '材料采购', 'amount': '24.60',
            'expense_date': '', **changes}


def test_goods_category_and_total_over_item_price() -> None:
    result = intake.parse_receipt(lines('14:40', '闪购 测试超市（演示店）',
        '【50只/盒】独立包装口罩 实付￥23.6', 'x2', '打包费 ￥1',
        '配送费 ￥0', '价格明细 总优惠￥6 实付款￥24.6', '总价￥30.6'))
    assert result['fields']['category'] == '材料采购'
    assert result['role'] == 'purchase'
    assert result['fields']['amount'] == '24.60'
    assert result['fields']['expense_date'] == ''
    assert result['fields']['order_number'] == ''
    assert result['fields']['merchant'] == '测试超市（演示店）'


def test_advertisements_ignored_and_split_boxes_joined() -> None:
    rows = lines('广告口罩 实付款￥999', '闪购 测试咖啡（演示店） 蜂鸟准时达',
                 '共6件', '价格明细 总优惠￥11.2 实付￥58.3')
    rows.extend([OcrLine('订单号', 0, 200, 120, 226), OcrLine('1234567890123456789 复制', 300, 202, 800, 228)])
    result = intake.parse_receipt(rows)
    assert result['fields']['category'] == '外卖'
    assert result['fields']['merchant'] == '测试咖啡（演示店）'
    assert result['fields']['amount'] == '58.30'
    assert result['fields']['order_number'] == '1234567890123456789'
    assert result['role'] == 'order'


def test_unknown_and_conflicting_fields_not_invented() -> None:
    result = intake.parse_receipt(lines('联系人 13800000000', '实付10.00', '实付12.00',
        '开票日期2026年09月21日', '下单日期2026-02-30'))
    assert result['fields']['amount'] == ''
    assert result['fields']['expense_date'] == ''
    assert result['fields']['order_number'] == ''
    assert result['fields']['category'] == '其他'
    assert intake.parse_receipt(lines('下单时间2026-09-18 20:00'))['fields']['expense_date'] == '2026-09-18'
    assert intake.parse_receipt(lines('下单时间2026-09-18', '支付时间2026-09-19'))['fields']['expense_date'] == ''


@pytest.mark.parametrize('text,role', [('微信支付 支付成功 实付24.6','payment'),
    ('电子发票 发票号码12345678901234567890 （小写）￥24.60','invoice')])
def test_payment_and_invoice_not_purchase_details(text: str, role: str) -> None:
    assert intake.parse_receipt(lines(text))['role'] == role


@pytest.mark.parametrize('name,payload', [('fake.png', b'\x89PNG\r\n\x1a\n'), ('wrong.jpg', picture()),
    ('source.pdf', b'%PDF-1.7'), ('large.png', b'x'*(20*1024*1024+1))], ids=['corrupt', 'wrong-format', 'pdf', 'oversize'])
def test_invalid_images_rejected(name: str, payload: bytes) -> None:
    with pytest.raises(ValueError):
        intake.validate_image(name, payload)


def test_preview_failure_falls_back_without_creating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    def unreadable(*args: Any) -> list[OcrLine]:
        raise TakeoutError('no text')
    monkeypatch.setattr(intake, 'read_receipt', unreadable)
    result = intake.recognize(store, 'receipt.png', picture())
    assert not store.list_items()
    assert list(store.files.iterdir()) == []
    assert result['fields']['amount'] == ''
    assert '未读出文字' in result['warnings'][0]


def test_save_atomic_idempotent_and_retains_review_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    payload = picture()
    saved = intake.save_receipt(store, 'receipt.png', payload, fields(), 'purchase')['item']
    assert not saved['verified'] and saved['stage'] == 'draft'
    assert saved['expense_date'] == '' and saved['amount'] == '24.60'
    assert saved['missing'] == ['发票或微信／支付宝扣费记录']
    actual, _ = store.attachment_path(saved['attachments'][0]['id'])
    assert actual.read_bytes() == payload
    duplicate = intake.save_receipt(store, 'renamed.png', payload, fields(), 'purchase')
    assert duplicate['duplicates'][0]['id'] == saved['id']
    assert len(store.list_items()) == 1
    monkeypatch.setattr(intake, 'read_receipt', lambda *args: pytest.fail('Duplicate must not rerun OCR'))
    assert intake.recognize(store, 'again.png', payload)['duplicates'][0]['reason'] == '相同文件'
    assert len(store.get(saved['id'])['events']) == 1


def test_order_duplicate_any_stage_but_not_same_amount(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    existing = store.create(fields(order_number='1234567890123456789'))
    store.transition(existing['id'], 'cancelled')
    duplicate = intake.save_receipt(store, 'receipt.png', picture(), fields(order_number='1234567890123456789'), 'purchase')
    assert duplicate['duplicates'][0]['stage'] == 'cancelled'
    assert list(store.files.iterdir()) == []
    saved = intake.save_receipt(store, 'receipt.png', picture(), fields(order_number='9876543210987654321'), 'purchase')
    assert saved['item']['id'] != existing['id']


def test_concurrent_saves_do_not_duplicate(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: intake.save_receipt(store, 'receipt.png', picture(), fields(), 'purchase'), range(2)))
    assert len(store.list_items()) == 1
    assert sum('item' in result for result in results) == 1


def test_failed_save_has_no_empty_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ExpenseStore(tmp_path)
    with pytest.raises(ValueError):
        intake.save_receipt(store, 'receipt.png', picture(), fields(amount=''), 'purchase')
    def failed(*args: Any) -> None:
        raise OSError('disk unavailable')
    monkeypatch.setattr(intake.os, 'replace', failed)
    with pytest.raises(OSError):
        intake.save_receipt(store, 'receipt.png', picture(), fields(), 'purchase')
    assert not store.list_items() and not list(store.files.iterdir())


def test_http_preview_save_and_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake, 'read_receipt', lambda *args: lines('闪购测试超市', '口罩', '实付款24.60'))
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(url+'/api/state') as response:
            token = json.load(response)['token']
        def post(route: str, body: dict[str, Any], auth: str = token) -> Any:
            request = Request(url+'/api/'+route, data=json.dumps(body).encode(),
                              headers={'Content-Type': 'application/json', 'X-Workbench-Token': auth})
            with urlopen(request) as response:
                return json.load(response)
        body = {'name': 'receipt.png', 'data': base64.b64encode(picture()).decode()}
        with pytest.raises(HTTPError) as error:
            post('recognize', body, '')
        assert error.value.code == 403
        result = post('recognize', body)
        assert result['fields']['amount'] == '24.60'
        assert not ExpenseStore(tmp_path).list_items()
        body.update(fields=result['fields'], role=result['role'])
        saved = post('receipt', body)
        assert saved['item']['amount'] == '24.60'
        assert post('receipt', body)['duplicates']
        for route in ('/', '/receipt.js'):
            with urlopen(url+route) as response:
                assert '上传凭证并识别' in response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
