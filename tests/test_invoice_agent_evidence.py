"""Synthetic local role/layout evidence; no model or business-state writes."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from invoice_print_layout.agent_evidence import material_facts
import invoice_print_layout.agent_evidence as evidence
from invoice_print_layout.takeout import OcrLine


def invoice(tmp_path: Path, *, swapped: bool = False, missing: bool = False,
            split_headers: bool = False, discount: bool = False,
            detached_tax_slash: bool = False, unit: str = '组', quantity: str = '2') -> dict[str, Any]:
    path = tmp_path / 'synthetic.pdf'
    with pymupdf.open() as document:
        page = document.new_page(width=650, height=450)
        def draw(x: float, y: float, text: str) -> None:
            page.insert_text((x, y), text, fontname='china-s', fontsize=8)
        draw(20, 30, '电子发票（普通发票）')
        draw(20, 50, '发票号码：12345678901234567890')
        for label, name, x in (('购买方', '合成买方公司', 330 if swapped else 20),
                               ('销售方', '合成卖方公司', 20 if swapped else 330)):
            if not missing or label != '销售方':
                for index, character in enumerate(label + '信息'):
                    draw(x, 90 + index * 9, character)
            draw(x + 18, 90, '名称：')
            draw(x + 43, 90, name)
            if label == '购买方':
                draw(x + 18, 118, '纳税人识别号：91320000MA1234567X')
        for x, label in ((20, '项目名称'), (155, '规格型号'), (225, '单位'), (285, '数量'),
                          (355, '单价'), (425, '金额'), (485, '税率/征收率'), (565, '税额')):
            if label == '税率/征收率' and detached_tax_slash:
                draw(x, 205, '税率征收率')
                draw(x + 12, 205, '/')
                continue
            if split_headers and label in {'单位', '数量', '单价', '金额', '税额'}:
                for index, character in enumerate(label):
                    draw(x + index * 15, 205, character)
            else:
                draw(x, 205, label)
        for x, text in ((20, '*软饮料*饮用水'), (225, unit), (303, quantity), (355, '11.325'),
                         (425, '22.65'), (485, '13%'), (565, '2.95')):
            draw(x, 225, text)
        if discount:
            draw(20, 245, '*软饮料*饮用水')
            draw(425, 245, '-3.54')
            draw(565, 245, '-0.46')
        draw(20, 310, '合计')
        draw(20, 330, '价税合计')
        draw(400, 330, '（小写）')
        draw(470, 330, '¥22.00')
        draw(20, 365, '地址：合成测试街道')
        draw(20, 380, '纳税人识别号：123456789012345678')
        document.save(path)
    return {'id': 'synthetic', 'digest': hashlib.sha256(path.read_bytes()).hexdigest(), 'revision': 3,
            'path': str(path), 'role': 'invoice', 'text': '',
            # Incorrect incoming name must never determine the PDF actor role.
            'fields': {'merchant': '合成买方公司', 'amount': '22.00'}}


@pytest.mark.parametrize('swapped', [False, True])
def test_explicit_roles_win_column_order_and_incoming_merchant(tmp_path: Path, swapped: bool) -> None:
    entry = invoice(tmp_path, swapped=swapped)
    before, payload = copy.deepcopy(entry), Path(entry['path']).read_bytes()
    result = material_facts(entry)
    assert result['actors']['seller']['value'] == '合成卖方公司'
    assert result['actors']['buyer']['value'] == '合成买方公司'
    assert result['actors']['seller']['method'] == 'native_geometry'
    assert result['actors']['seller']['page'] == 1 and result['actors']['seller']['bbox']
    assert result['actors']['buyer']['tax_id_fingerprint'].startswith('sha256:')
    assert result['complete'] and result['total_cents'] == 2200
    assert '合成测试街道' not in result['text'] and '123456789012345678' not in result['text']
    assert entry == before and Path(entry['path']).read_bytes() == payload
    assert result['id'] == entry['id'] and result['sha256'] == entry['digest'] and result['revision'] == 3


def test_missing_explicit_seller_role_is_not_guessed_from_two_names(tmp_path: Path) -> None:
    result = material_facts(invoice(tmp_path, missing=True))
    assert result['actors']['seller'] is None
    assert result['actors']['buyer']['value'] == '合成买方公司'


@pytest.mark.parametrize('split_headers', [False, True])
def test_positioned_quantity_unit_and_negative_discount(tmp_path: Path, split_headers: bool) -> None:
    result = material_facts(invoice(tmp_path, split_headers=split_headers, discount=True))
    assert len(result['goods']) == 2
    bought, discounted = result['goods']
    assert bought['name'] == '饮用水' and bought['quantity'] == '2' and bought['unit'] == '组'
    assert bought['is_discount'] is False
    assert discounted['name'] == '饮用水' and discounted['is_discount'] is True
    assert 'quantity' not in discounted
    assert bought['method'] == discounted['method'] == 'native_geometry'


def test_overlapping_detached_tax_header_slash_does_not_mix_tax_rate_with_amount(tmp_path: Path) -> None:
    result = material_facts(invoice(tmp_path, split_headers=True, discount=True, detached_tax_slash=True))
    assert result['goods'][0]['is_discount'] is False
    assert result['goods'][1]['is_discount'] is True


def test_explicit_native_day_quantity_does_not_become_inferred_nights(tmp_path: Path) -> None:
    result = material_facts(invoice(tmp_path, split_headers=True, unit='天', quantity='7'))
    assert result['goods'][0]['unit'] == '天' and result['goods'][0]['quantity'] == '7'
    assert '晚' not in str(result['goods'])


def test_order_ads_contacts_and_precise_time_are_excluded() -> None:
    entry = {'id': 'order', 'digest': 'synthetic-digest', 'revision': 1, 'role': 'order',
             'text': ('广告口罩 实付款￥999\n订单已送达\n闪购合成便利店\n商品明细\n'
                      '合成鼠标USB笔记本台式\n电脑商务办公 ￥16\nx1\n打包费 ￥1\n'
                      '价格明细 实付17\n支付时间 2026-01-01 12:34:56\n'
                      '收货地址：合成街道\n联系人：合成人员\n13800000000\n'
                      '为你推荐\n矿泉水 ￥20\n'),
             'fields': {'merchant': '合成便利店', 'amount': '17'}}
    result = material_facts(entry)
    assert [item['name'] for item in result['goods']] == ['鼠标']
    assert '矿泉水' not in result['text'] and '口罩' not in result['text']
    assert '合成街道' not in result['text'] and '合成人员' not in result['text']
    assert '13800000000' not in result['text'] and '12:34:56' not in result['text']


@pytest.mark.parametrize('recommendation,anchor', [
    ('为你推荐', '闪购合成工具店'), ('商品推荐', '商品明细'),
    ('猜你喜欢', '购买明细'), ('推荐商品', '店铺名称：合成工具店'),
])
def test_top_recommendation_does_not_hide_later_explicit_purchase_area(recommendation: str, anchor: str) -> None:
    result = material_facts({'role': 'purchase', 'text':
                             recommendation + '\n奶茶 ￥99\n订单已送达\n' + anchor +
                             '\n鼠标 ￥16\nx1\n价格明细 实付17\n为你推荐\n商品明细\n矿泉水 ￥30',
                             'fields': {'merchant': '合成工具店', 'amount': '17'}})
    assert [item['name'] for item in result['goods']] == ['鼠标']
    assert '订单已送达' in result['text'] and anchor in result['text']
    assert '奶茶' not in result['text'] and '矿泉水' not in result['text']


def test_payment_description_and_collector_are_different_actor_facts() -> None:
    result = material_facts({'id': 'payment', 'role': 'payment', 'text':
                             '支付宝\n交易成功\n收款方：合成旅行有限公司\n商品说明：合成佳缘宾馆\n-120.00',
                             'fields': {'amount': '120.00', 'payment_status': 'succeeded'}})
    assert result['actors']['collector']['value'] == '合成旅行有限公司'
    assert result['descriptions'][0]['value'] == '合成佳缘宾馆'
    assert result['actors']['seller'] is None and result['actors']['buyer'] is None
    assert result['actors']['payment_channel']['value'] == '支付宝'
    assert result['actors']['shop'] is None


@pytest.mark.parametrize('role', ['payment', 'receipt', 'order', 'trip'])
def test_noninvoice_native_pdf_keeps_labelled_description_and_image_text_equivalence(
        tmp_path: Path, role: str) -> None:
    source = ('支付宝\n交易成功\n收款方 合成旅行有限公司\n商品说明 车站春雨宾馆\n'
              '付款金额 120.00\n支付时间 2026年01月01日12:34:56\n'
              '地址：合成联系街道\n税号：91320000MA1234567X')
    path = tmp_path / 'synthetic-payment.pdf'
    with pymupdf.open() as document:
        page = document.new_page()
        for index, line in enumerate(source.splitlines()):
            page.insert_text((20, 30 + index * 24), line, fontname='china-s', fontsize=9)
        document.save(path)
    image_text = {'id': 'image-text', 'role': role, 'text': source,
                  'fields': {'amount': '120.00', 'payment_status': 'succeeded'}}
    pdf = {**image_text, 'id': 'pdf', 'path': str(path), 'text': ''}
    extracted, image = material_facts(pdf), material_facts(image_text)
    assert extracted['descriptions'][0]['value'] == image['descriptions'][0]['value'] == '车站春雨宾馆'
    assert '商品说明 车站春雨宾馆' in extracted['descriptions'][0]['quote']
    assert extracted['descriptions'][0]['method'] == 'native_label'
    if role == 'payment':
        assert extracted['actors']['collector']['value'] == image['actors']['collector']['value'] == '合成旅行有限公司'
    for result in (extracted, image):
        assert '12:34:56' not in result['text'] and '合成联系街道' not in result['text']
        assert '91320000MA1234567X' not in result['text']


def test_payment_acquirer_is_never_actual_shop() -> None:
    result = material_facts({'role': 'payment', 'text':
                             '美团\n美团平台商户\n支付成功\n商品说明：美团订单\n收单机构 合成支付技术有限公司\n-29.60',
                             'fields': {'merchant': '收单机构合成支付技术有限公司', 'amount': '29.60',
                                        'shop_name': '收单机构合成支付技术有限公司'}})
    assert result['actors']['shop'] is None and result['actors']['seller'] is None
    assert result['descriptions'][0]['value'] == '美团订单'


def test_flash_shop_heading_and_basket_count_without_invented_products() -> None:
    result = material_facts({'role': 'order', 'text':
                             '订单已送达\n闪购合成茶饮店(合成门店) 蜂鸟准时达\n共6件\n价格明细 实付58.30',
                             'fields': {'merchant': '合成茶饮店(合成门店)', 'amount': '58.30'}})
    assert result['actors']['shop']['value'] == '合成茶饮店(合成门店)'
    assert not result['goods'] and result['descriptions'][0]['value'] == '共6件'


def test_image_explicit_role_boxes_and_private_buyer_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'synthetic.jpg'
    path.write_bytes(b'synthetic-local-ocr')
    calls: list[str] = []
    def ocr(payload: bytes, suffix: str) -> list[OcrLine]:
        calls.append(suffix)
        return [OcrLine('购买方信息', 20, 90, 35, 160),
                OcrLine('名称：合成买方公司', 50, 90, 250, 110),
                OcrLine('纳税人识别号：91320000MA1234567X', 50, 125, 275, 145),
                OcrLine('销售方信息', 330, 90, 345, 160),
                OcrLine('名称：合成卖方公司', 360, 90, 560, 110)]
    monkeypatch.setattr(evidence, 'read_receipt', ocr)
    entry = {'id': 'image', 'path': str(path), 'digest': hashlib.sha256(path.read_bytes()).hexdigest(),
             'role': 'invoice', 'text': '名称：合成买方公司 名称：合成卖方公司\n购买方信息 销售方信息',
             'fields': {'amount': '22'}}
    result = material_facts(entry)
    assert result['actors']['buyer']['value'] == '合成买方公司'
    assert result['actors']['seller']['value'] == '合成卖方公司'
    assert result['actors']['seller']['method'] == 'local_ocr_geometry'
    native = material_facts(invoice(tmp_path))
    assert result['actors']['buyer']['tax_id_fingerprint'] == native['actors']['buyer']['tax_id_fingerprint']
    assert '91320000MA1234567X' not in str(result)
    material_facts(entry)
    assert calls == ['.jpg']


def test_ambiguous_labelled_seller_has_no_actor() -> None:
    result = material_facts({'id': 'ambiguous', 'role': 'invoice', 'text':
                             '销售方：合成一公司\n销售方：合成二公司\n', 'fields': {'amount': '10'}})
    assert result['actors']['seller'] is None and not result['complete']


def test_unreadable_pdf_is_incomplete_instead_of_trusting_cached_fields(tmp_path: Path) -> None:
    path = tmp_path / 'broken.pdf'
    path.write_bytes(b'invalid')
    result = material_facts({'id': 'broken', 'path': str(path), 'role': 'invoice',
                             'text': '销售方：合成公司', 'fields': {'amount': '10'}})
    assert not result['complete'] and result['issues']


def test_description_budget_is_bounded_and_input_remains_unchanged() -> None:
    entry = {'id': 'long', 'role': 'payment', 'text': '商品说明：' + '合成物品' * 1500,
             'fields': {'amount': '10'}}
    before = copy.deepcopy(entry)
    result = material_facts(entry)
    assert len(result['text']) <= 3000 and entry == before
