"""Batch QR replacement and local payment OCR use synthetic receipts only."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pymupdf
import pytest
from PIL import Image

from invoice_print_layout import intake_queue as intake_module, invoice_claim, mail_search
from invoice_print_layout.intake_queue import IntakeQueue, positioned_invoice_seller
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.workbench import ExpenseStore

NUMBER = '12345678901234567890'
TOKEN = '2_' + NUMBER + '_2026100614304900ABCDEF'
SHANGHAI = 'https://dppt.shanghai.chinatax.gov.cn:8443/v/' + TOKEN
JIANGXI = 'https://dppt.jiangxi.chinatax.gov.cn:8443/v/' + TOKEN


def image_bytes(color: str = 'white') -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (240, 480), color).save(stream, format='PNG')
    return stream.getvalue()


def invoice_bytes(number: str = NUMBER) -> bytes:
    with pymupdf.open() as document:
        page = document.new_page()
        for index, text in enumerate(['电子发票（普通发票）', '发票号码：' + number, '价税合计（小写）：¥14.00']):
            page.insert_text((20, 40 + index * 35), text, fontname='china-s', fontsize=12)
        return document.tobytes()


def detached_invoice_bytes(*, positioned: bool = False) -> bytes:
    """Tax-style stream order has labels first, then independent field values."""
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=420)
        labels = [(20, 30, '电子发票（普通发票）'), (400, 55, '发票号码：'),
                  (400, 82, '开票日期：'), (30, 140, '名称：'), (320, 140, '名称：'),
                  (30, 285, '合计'), (30, 310, '价税合计（大写）'), (385, 310, '（小写）'), (30, 355, '备注')]
        for x, y, value in labels:
            page.insert_text((x, y), value, fontname='china-s', fontsize=9)
        # Keep ticket/date adjacent in extraction order to reproduce the former
        # whitespace-compaction number-boundary rejection.
        page.insert_text((465, 55), NUMBER, fontsize=7)
        page.insert_text((465, 82), '2026年10月06日', fontname='china-s', fontsize=7)
        page.insert_text((30, 160), '测试购买方', fontname='china-s', fontsize=9)
        page.insert_text((320, 160), '测试销售方', fontname='china-s', fontsize=9)
        for x, y, value in [(310, 285, '13.86'), (420, 285, '0.14'), (450, 310, '14.00')]:
            page.insert_text((x, y), value if positioned else '¥' + value, fontsize=9)
        return document.tobytes()


def seller_column_invoice_bytes(*, split_name: bool = False, horizontal: bool = False,
                                defect: str = '') -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=420)
        for x, y, value in [(20, 30, '电子发票（普通发票）'), (330, 55, '发票号码：'),
                            (390, 55, NUMBER), (385, 310, '（小写）'), (450, 310, '¥14.00')]:
            page.insert_text((x, y), value, fontname='china-s', fontsize=9)
        for index, character in enumerate('购买方信息'):
            page.insert_text((20, 100 + index * 10), character, fontname='china-s', fontsize=9)
        page.insert_text((32, 100), '名称：', fontname='china-s', fontsize=9)
        page.insert_text((60, 100), '测试购买公司', fontname='china-s', fontsize=9)
        if defect != 'missing-marker':
            for index, character in enumerate('销售方信息'):
                page.insert_text((300 + index * 10 if horizontal else 300,
                                  100 if horizontal else 100 + index * 10),
                                 character, fontname='china-s', fontsize=9)
        name_x = 350 if horizontal else 317
        if defect != 'missing-name':
            if split_name:
                for index, character in enumerate('名称：'):
                    page.insert_text((name_x + index * 10, 100), character, fontname='china-s', fontsize=9)
            else:
                page.insert_text((name_x, 100), '名称：', fontname='china-s', fontsize=9)
        seller_x = name_x + 28
        seller_y = 170 if defect == 'different-baseline' else 100
        if split_name:
            page.insert_text((seller_x, seller_y), '测试销售', fontname='china-s', fontsize=9)
            page.insert_text((seller_x + 36, seller_y), '公司', fontname='china-s', fontsize=9)
        else:
            page.insert_text((seller_x, seller_y), '测试销售公司', fontname='china-s', fontsize=9)
        if defect == 'duplicate-name':
            page.insert_text((380, 110), '名称：', fontname='china-s', fontsize=9)
            page.insert_text((408, 110), '另一个销售公司', fontname='china-s', fontsize=9)
        if defect == 'overlap-values':
            page.insert_text((seller_x + 8, seller_y), '另一个销售公司', fontname='china-s', fontsize=9)
        if defect == 'duplicate-marker':
            for index, character in enumerate('销售方信息'):
                page.insert_text((450, 100 + index * 10), character, fontname='china-s', fontsize=9)
            page.insert_text((467, 100), '名称：', fontname='china-s', fontsize=9)
            page.insert_text((495, 100), '另一个销售公司', fontname='china-s', fontsize=9)
        return document.tobytes()


def tax_responses(monkeypatch: pytest.MonkeyPatch, *, pdf: bytes | None = None,
                  first: bytes | None = None) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    def request(endpoint: str, values: dict[str, Any], *, get: bool = False,
                base: str = invoice_claim.BASE) -> bytes:
        calls.append((base, endpoint))
        if endpoint == 'queryFpjcxxb':
            assert values == {'Cs': TOKEN}
            return first if first is not None else json.dumps({
                'code': '1', 'fphm': NUMBER, 'kprq': '2026-10-06 14:30:49', 'jym': 'ABCD'}).encode()
        if endpoint == 'exportDzfpwjEwm':
            assert get and values['Fphm'] == NUMBER and values['Wjgs'] == 'PDF'
            return pdf if pdf is not None else invoice_bytes()
        return b'{"code":"1"}'
    monkeypatch.setattr(invoice_claim, 'request', request)
    return calls


def fail_network(*_: Any, **__: Any) -> bytes:
    raise AssertionError('No network request is allowed in this synthetic case')


def ocr(monkeypatch: pytest.MonkeyPatch, text: list[str]) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: '')
    lines = [OcrLine(value, 0, index * 30, 400, index * 30 + 20) for index, value in enumerate(text)]
    monkeypatch.setattr(intake_module, 'read_receipt', lambda *_: lines)


@pytest.mark.parametrize('url,base', [(SHANGHAI, invoice_claim.BASE),
    (JIANGXI, 'https://dppt.jiangxi.chinatax.gov.cn:8443')])
def test_supported_tax_claim_is_exactly_bounded_and_retains_original_pdf(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, base: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    content = invoice_bytes()
    calls = tax_responses(monkeypatch, pdf=content)
    result = invoice_claim.claim(queue, url)
    assert result['status'] == 'received'
    assert calls == [(base, endpoint) for endpoint in
                     ('queryFpjcxxb', 'queryFpmxNum', 'saveDzfpxzjlxx', 'exportDzfpwjEwm')]
    assert Path(result['entry']['path']).read_bytes() == content
    assert not queue.store.list_items()


@pytest.mark.parametrize('url', [
    'https://unknown.example/v/' + TOKEN,
    'https://dppt.jiangxi.chinatax.gov.cn.evil.example/v/' + TOKEN,
    'http://dppt.jiangxi.chinatax.gov.cn/v/' + TOKEN,
    'https://dppt.jiangxi.chinatax.gov.cn:443/v/' + TOKEN,
    'https://dppt.jiangxi.chinatax.gov.cn/v/' + TOKEN + '?next=https://evil.example',
    'https://dppt.jiangxi.chinatax.gov.cn/v/' + TOKEN + '#anchor',
    'https://dppt.jiangxi.chinatax.gov.cn/unknown/' + TOKEN,
    'https://127.0.0.1/v/' + TOKEN,
])
def test_unknown_or_modified_claim_urls_do_not_make_network_calls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    monkeypatch.setattr(invoice_claim, 'request', fail_network)
    assert invoice_claim.claim(queue, url)['status'] == 'manual'
    assert queue.list() == []


def test_adapter_rejects_arbitrary_base_and_endpoint_before_opening_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(invoice_claim.urllib.request, 'build_opener', fail_network)
    for endpoint, base in [('unknown', invoice_claim.BASE), ('queryFpjcxxb', 'https://evil.example')]:
        with pytest.raises(ValueError, match='不支持'):
            invoice_claim.request(endpoint, {}, base=base)


@pytest.mark.parametrize('failure', ['expired', 'login', 'malformed', 'wrong_invoice', 'non_invoice', 'wrong_metadata', 'password'])
def test_expired_login_or_mismatched_claims_are_not_saved_as_invoices(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    first = b'{"code":"0"}' if failure == 'expired' else None
    if failure == 'wrong_metadata':
        first = json.dumps({'code': '1', 'fphm': '98765432109876543210',
                            'kprq': '2026-10-06 14:30:49', 'jym': 'ABCD'}).encode()
    content = invoice_bytes()
    if failure == 'login':
        content = b'<html>Please log in</html>'
    elif failure == 'malformed':
        content = b'%PDF-not-a-real-document'
    elif failure == 'wrong_invoice':
        content = invoice_bytes('98765432109876543210')
    elif failure == 'non_invoice':
        with pymupdf.open() as document:
            document.new_page().insert_text((20, 30), 'Download failed. ' + NUMBER)
            content = document.tobytes()
    elif failure == 'password':
        with pymupdf.open(stream=content, filetype='pdf') as document:
            content = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw='synthetic-password')
    tax_responses(monkeypatch, pdf=content, first=first)
    result = invoice_claim.claim(queue, JIANGXI)
    assert result['status'] == 'manual' and queue.list() == [] and queue.store.list_items() == []


@pytest.mark.parametrize('endpoint', ['queryFpjcxxb', 'queryFpmxNum', 'saveDzfpxzjlxx'])
def test_non_object_protocol_responses_stay_manual(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    tax_responses(monkeypatch)
    original = invoice_claim.request
    def malformed(name: str, values: dict[str, Any], **kwargs: Any) -> bytes:
        return b'[]' if name == endpoint else original(name, values, **kwargs)
    monkeypatch.setattr(invoice_claim, 'request', malformed)
    assert invoice_claim.claim(queue, JIANGXI)['status'] == 'manual'
    assert queue.list() == []


def test_redirect_requires_manual_action_and_does_not_follow_other_hosts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    class Opener:
        def open(self, request: Any, timeout: int) -> None:
            opened.append(request.full_url)
            raise HTTPError(request.full_url, 302, 'Redirect', {'Location': 'https://evil.example'}, None)
    def build(handler: Any) -> Opener:
        assert isinstance(handler, invoice_claim._NoRedirect)
        return Opener()
    monkeypatch.setattr(invoice_claim.urllib.request, 'build_opener', build)
    queue = IntakeQueue(ExpenseStore(tmp_path))
    assert invoice_claim.claim(queue, JIANGXI)['status'] == 'manual'
    assert len(opened) == 1 and opened[0].startswith('https://dppt.jiangxi.chinatax.gov.cn:8443/')
    assert queue.list() == []


def test_batch_qr_replacement_preserves_source_relationship_and_is_idempotent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')
    source = queue.receive('qr.png', image_bytes(), project_id=project['id'], batch='synthetic-batch')
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: JIANGXI)
    calls = tax_responses(monkeypatch)
    result = queue.prepare_batch_entry(source['id'])
    converted = queue.get(source['id'])
    assert converted['status'] == 'converted' and converted['qr_status'] == 'converted'
    assert converted['role'] == 'other' and converted['fields'] == {} and converted['replacement_id'] == result['id']
    assert Path(converted['path']).read_bytes() == image_bytes()
    assert result['source_entry_id'] == source['id'] and result['source_entry_ids'] == [source['id']]
    assert result['batch'] == source['batch'] and result['project_id'] == project['id']
    assert result['name'].endswith('.pdf') and result['role'] == 'invoice'
    assert result['fields']['invoice_number'] == NUMBER and len(queue.list()) == 2
    before = queue.list()
    monkeypatch.setattr(invoice_claim, 'request', fail_network)
    assert queue.prepare_batch_entry(source['id']) == result
    assert queue.list() == before and len(calls) == 4 and queue.store.list_items() == []


def test_two_qr_images_of_one_invoice_reuse_pdf_with_two_source_records(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: SHANGHAI)
    tax_responses(monkeypatch, pdf=invoice_bytes())
    first = queue.receive('qr-one.png', image_bytes('red'))
    second = queue.receive('qr-two.png', image_bytes('blue'))
    result_one = queue.prepare_batch_entry(first['id'])
    result_two = queue.prepare_batch_entry(second['id'])
    assert result_one['id'] == result_two['id']
    assert result_two['source_entry_id'] == first['id']
    assert result_two['source_entry_ids'] == [first['id'], second['id']]
    assert len(queue.list()) == 3 and all(queue.get(source['id'])['status'] == 'converted' for source in (first, second))
    assert not queue.store.list_items()


def test_claim_duplicate_in_another_project_is_manual_without_changing_ownership(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    projects = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3')
    first = projects.create_project('Synthetic first project')
    second = projects.create_project('Synthetic second project')
    content = invoice_bytes()
    prior = queue.receive('invoice.pdf', content, project_id=first['id'])
    source = queue.receive('qr.png', image_bytes(), project_id=second['id'])
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: JIANGXI)
    tax_responses(monkeypatch, pdf=content)
    result = queue.prepare_batch_entry(source['id'])
    assert result['qr_status'] == 'manual' and result['replacement_id'] == prior['id']
    assert '其他项目' in result['result'] and result['role'] == 'other' and result['fields'] == {}
    assert queue.get(prior['id']) == prior and not queue.store.list_items()


@pytest.mark.parametrize('value', ['https://unknown.example/download', 'weixin://wxpay/payment',
                                  'unknown-qr-payload', 'https://[invalid'])
def test_unknown_decoded_qr_is_kept_manual_without_ocr_or_network(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('qr.png', image_bytes())
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: value)
    monkeypatch.setattr(invoice_claim, 'request', fail_network)
    monkeypatch.setattr(intake_module, 'read_receipt', fail_network)
    result = queue.prepare_batch_entry(source['id'])
    assert result['status'] == 'review' and result['qr_status'] == 'manual'
    assert result['role'] == 'other' and result['fields'] == {} and result['issues'] == ['领票待处理']
    assert len(queue.list()) == 1 and queue.store.list_items() == []


def test_real_image_qr_decoder_routes_a_supported_tax_image_to_pdf(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cv2
    pixels = cv2.QRCodeEncoder_create().encode(JIANGXI)
    pixels = cv2.resize(pixels, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    pixels = cv2.copyMakeBorder(pixels, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    success, encoded = cv2.imencode('.png', pixels)
    assert success
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('official-qr.png', bytes(encoded))
    assert mail_search.qr_value(Path(source['path']).read_bytes()) == JIANGXI
    tax_responses(monkeypatch)
    result = queue.prepare_batch_entry(source['id'])
    assert result['source_entry_id'] == source['id'] and result['name'].endswith('.pdf')
    assert queue.get(source['id'])['status'] == 'converted'


def test_expired_qr_stays_manual_even_if_later_decoder_cannot_read_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('expired-qr.png', image_bytes())
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: JIANGXI)
    tax_responses(monkeypatch, first=b'{"code":"0"}')
    first = queue.prepare_batch_entry(source['id'])
    assert first['qr_status'] == 'manual' and len(queue.list()) == 1
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: '')
    monkeypatch.setattr(intake_module, 'read_receipt', fail_network)
    assert queue.prepare_batch_entry(source['id']) == first
    assert queue.store.list_items() == []


@pytest.mark.parametrize('text,amount', [
    (['微信转账', '付款方甲', '-595.00', '对方已收钱'], '595.00'),
    (['微信支付', '店铺甲', '-940.00', '当前状态：支付成功'], '940.00'),
    (['支付宝', '付款金额：73.00', '支付成功', '收款方：店铺甲'], '73.00'),
    (['支付宝', '店铺甲', '¥14.00', '交易成功'], '14.00'),
])
def test_local_payment_ocr_reads_money_lines_and_success_without_using_filename(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: list[str], amount: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('payment-999999.00.png', image_bytes())
    ocr(monkeypatch, text)
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'payment' and result['fields']['amount'] == amount
    assert result['fields']['payment_status'] == 'succeeded'
    assert result['status'] == 'review' and queue.store.list_items() == []


@pytest.mark.parametrize('status', ['退款成功', '支付失败', '交易关闭', '等待对方收款', '已退回'])
def test_refund_failed_or_pending_payment_is_not_an_automatic_payment_candidate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('payment.png', image_bytes())
    ocr(monkeypatch, ['微信支付', '店铺甲', '-595.00', '对方已收钱', status])
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'other' and result['fields']['amount'] == ''
    assert result['fields']['payment_status'] == 'uncertain' and '付款状态待确认' in result['issues']
    assert queue.store.list_items() == []


@pytest.mark.parametrize('text', [['费用清单', '-595.00'], ['清单', '595.00'],
    ['微信支付', '-595.00', '-345.00', '支付成功']])
def test_unproved_or_ambiguous_money_lines_are_left_for_manual_review(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: list[str]) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('proof.png', image_bytes())
    ocr(monkeypatch, text)
    result = queue.prepare_batch_entry(source['id'])
    assert result['fields']['amount'] == '' and '金额冲突' in result['issues']
    assert queue.store.list_items() == []


def test_paper_receipt_does_not_require_goods_classification_during_intake(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('receipt.png', image_bytes())
    ocr(monkeypatch, ['某某公司', '收据', '合计14.00', '商品名称难以识别'])
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'receipt' and result['fields']['amount'] == '14.00'
    assert '商品不明' not in result['issues'] and result['status'] == 'review'
    assert queue.store.list_items() == []


@pytest.mark.parametrize('positioned', [False, True])
def test_batch_pdf_reads_detached_tax_invoice_number_and_total_without_guessing_seller(
        tmp_path: Path, positioned: bool) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    content = detached_invoice_bytes(positioned=positioned)
    source = queue.receive('synthetic-tax-invoice.pdf', content)
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'invoice' and result['fields']['invoice_number'] == NUMBER
    assert result['fields']['amount'] == '14.00'
    # Two detached "name" values lack an explicit seller relationship in text.
    assert result['fields']['merchant'] == '' and result['fields']['category'] == '其他'
    assert result['fields']['expense_date'] == '' and '商品不明' not in result['issues']
    assert Path(result['path']).read_bytes() == content and queue.store.list_items() == []


@pytest.mark.parametrize('split_name,horizontal', [(False, False), (True, False), (True, True)])
def test_native_seller_column_distinguishes_buyer_and_supports_split_pdf_words(
        tmp_path: Path, split_name: bool, horizontal: bool) -> None:
    content = seller_column_invoice_bytes(split_name=split_name, horizontal=horizontal)
    with pymupdf.open(stream=content, filetype='pdf') as document:
        assert positioned_invoice_seller(document[0]) == '测试销售公司'
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('synthetic-seller-column.pdf', content)
    result = queue.prepare_batch_entry(source['id'])
    assert result['fields']['merchant'] == '测试销售公司'
    assert result['fields']['title'] == '测试销售公司 · 发票'
    assert result['fields']['amount'] == '14.00' and result['fields']['invoice_number'] == NUMBER
    assert result['fields']['expense_date'] == '' and result['fields']['category'] == '其他'
    assert '商品不明' not in result['issues'] and not queue.store.list_items()
    assert Path(result['path']).read_bytes() == content


@pytest.mark.parametrize('defect', ['missing-marker', 'missing-name', 'different-baseline',
                                  'duplicate-name', 'overlap-values', 'duplicate-marker'])
def test_native_seller_requires_unique_labelled_name_baseline(defect: str) -> None:
    with pymupdf.open(stream=seller_column_invoice_bytes(defect=defect), filetype='pdf') as document:
        assert positioned_invoice_seller(document[0]) == ''


def test_batch_pdf_preserves_explicit_single_line_seller(tmp_path: Path) -> None:
    with pymupdf.open() as document:
        page = document.new_page()
        for index, value in enumerate(['电子发票（普通发票）', '发票号码：' + NUMBER,
                                       'TOTAL WITH TAX:14.00', 'SELLER: Synthetic Seller']):
            page.insert_text((20, 40 + index * 30), value, fontname='china-s', fontsize=10)
        content = document.tobytes()
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('synthetic-explicit-seller.pdf', content)
    result = queue.prepare_batch_entry(source['id'])
    assert result['fields']['merchant'] == 'Synthetic Seller'
    assert result['fields']['amount'] == '14.00'


@pytest.mark.parametrize('positioned', [True, False])
def test_batch_pdf_uses_printed_total_and_never_takes_the_last_tax_amount(
        tmp_path: Path, positioned: bool) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=420)
        for x, y, value in [(20, 30, '电子发票（普通发票）'), (20, 55, '发票号码：' + NUMBER),
                            (30, 310, '价税合计（大写）'), (385, 310, '（小写）' if positioned else '金额'),
                            (30, 285, '税额')]:
            page.insert_text((x, y), value, fontname='china-s', fontsize=9)
        page.insert_text((450, 310), '¥40.50', fontsize=9)
        # PDF stream order ends with the tax although the printed total is lower
        # on the page. Choosing the last currency value incorrectly yields 2.29.
        page.insert_text((420, 285), '¥2.29', fontsize=9)
        content = document.tobytes()
    source = queue.receive('synthetic-tax-last.pdf', content, project_id=project)
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'invoice' and result['fields']['invoice_number'] == NUMBER
    assert result['fields']['amount'] == ('40.50' if positioned else '')
    if not positioned:
        assert '发票价税合计待核对' in result['issues'] and '金额冲突' in result['issues']
        assert MaterialMatching(queue.store, queue).auto_match(project)['matched'] == 0
    assert Path(result['path']).read_bytes() == content and queue.store.list_items() == []


@pytest.mark.parametrize('totals,amount', [(['TOTAL WITH TAX:40.50'], '40.50'),
                                        (['价税合计：¥40.50'], '40.50'),
                                        (['TOTAL WITH TAX:40.50', 'TOTAL WITH TAX:2.29'], '')])
def test_batch_pdf_without_small_total_position_accepts_only_unique_directly_labelled_total(
        tmp_path: Path, totals: list[str], amount: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    with pymupdf.open() as document:
        page = document.new_page()
        for index, value in enumerate(['电子发票（普通发票）', '发票号码：' + NUMBER, *totals, '税额：¥2.29']):
            page.insert_text((20, 40 + index * 35), value, fontname='china-s', fontsize=10)
        content = document.tobytes()
    source = queue.receive('synthetic-direct-total.pdf', content)
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'invoice' and result['fields']['amount'] == amount
    assert result['fields']['invoice_number'] == NUMBER and Path(result['path']).read_bytes() == content
    if not amount:
        assert '发票价税合计待核对' in result['issues']


def test_waybill_with_invoice_number_is_not_reclassified_as_invoice(tmp_path: Path) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    with pymupdf.open() as document:
        page = document.new_page()
        for index, value in enumerate(['运单明细', '发票号码：' + NUMBER,
                                       '提示：电子发票可在原入口领取', '合计14.00']):
            page.insert_text((20, 40 + index * 30), value, fontname='china-s', fontsize=12)
        content = document.tobytes()
    source = queue.receive('waybill.pdf', content)
    result = queue.prepare_batch_entry(source['id'])
    assert result['role'] == 'detail' and 'invoice_number' not in result['fields']
    assert result['status'] == 'review' and queue.store.list_items() == []


def test_ready_code_three_can_generate_a_pdf_with_detached_number_and_date(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    source = queue.receive('qr.png', image_bytes())
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: JIANGXI)
    content = detached_invoice_bytes()
    tax_responses(monkeypatch, pdf=content)
    original = invoice_claim.request
    def ungenerated(endpoint: str, values: dict[str, Any], **kwargs: Any) -> bytes:
        return (b'{"code":"3","tdyslxDm":null,"message":""}' if endpoint == 'queryFpmxNum'
                else original(endpoint, values, **kwargs))
    monkeypatch.setattr(invoice_claim, 'request', ungenerated)
    result = queue.prepare_batch_entry(source['id'])
    assert queue.get(source['id'])['status'] == 'converted'
    assert result['fields']['invoice_number'] == NUMBER and result['fields']['amount'] == '14.00'
    assert result['role'] == 'invoice' and Path(result['path']).read_bytes() == content


def test_second_payment_ocr_failure_clears_old_amount_and_cannot_be_auto_matched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    bill = queue.receive('tax.pdf', detached_invoice_bytes(), project_id=project)
    queue.prepare_batch_entry(bill['id'])
    payment = queue.receive('payment.png', image_bytes(), project_id=project)
    ocr(monkeypatch, ['微信支付', '店铺甲', '-14.00', '支付成功'])
    first = queue.prepare_batch_entry(payment['id'])
    assert first['role'] == 'payment' and first['fields']['amount'] == '14.00'
    def fail_ocr(*_: Any) -> Any:
        raise RuntimeError('Synthetic OCR failure')
    monkeypatch.setattr(intake_module, 'read_receipt', fail_ocr)
    failed = queue.prepare_batch_entry(payment['id'])
    assert failed['role'] == 'other' and failed['fields'] == {} and failed['text'] == ''
    assert failed['previous_analysis']['fields']['amount'] == '14.00'
    matcher = MaterialMatching(queue.store, queue)
    assert matcher.board(project)['payments'] == []
    assert matcher.auto_match(project)['matched'] == 0 and queue.store.list_items() == []


@pytest.mark.parametrize('kind', ['image', 'pdf'])
def test_bulk_preparation_does_not_organize_or_change_existing_verified_items(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    registry = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3')
    old_project = registry.create_project('Existing verified project')['id']
    new_project = registry.create_project('Current upload project')['id']
    old = queue.store.create({'title': 'Already verified', 'project_id': old_project,
                              'category': '酒店', 'amount': '14', 'order_number': '123456789012'})
    queue.store.add_attachment(old['id'], 'old.pdf', invoice_bytes(), 'invoice')
    old = queue.store.transition(old['id'], 'verify')
    # Legacy organization would follow the order to another project's verified
    # expense. Batch preparation must only update its new intake entry.
    monkeypatch.setattr(queue, 'organize', lambda *_: pytest.fail('Batch preparation must not organize older expenses'))
    monkeypatch.setattr(queue, 'analyze', lambda *_: pytest.fail('Bulk PDF must not analyze the whole intake folder'))
    if kind == 'image':
        source = queue.receive('receipt.png', image_bytes(), project_id=new_project)
        ocr(monkeypatch, ['收据', '合计14.00', '订单号123456789012'])
    else:
        source = queue.receive('invoice.pdf', detached_invoice_bytes(), project_id=new_project)
    result = queue.prepare_batch_entry(source['id'])
    assert result['status'] == 'review' and not result['item_id']
    assert queue.store.get(old['id']) == old and len(queue.store.list_items()) == 1
