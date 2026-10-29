from pathlib import Path
from decimal import Decimal

import pymupdf
import pytest

from invoice_print_layout.courier import inspect_courier_pdf, compose_courier_package

NUMBER = "26000000000000000001"


def make_courier_pair(folder: Path, kind: str, number: str = NUMBER, pages: int = 1) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    invoice, detail = folder / 'invoice.pdf', folder / 'detail.pdf'
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=397)
        lines = ['电子发票', '顺丰速运有限公司' if kind == '顺丰' else '顺丰同城物流有限公司',
                 '发票号码：' + number, '开票日期：2026年09月18日', '价税合计']
        for i, line in enumerate(lines):
            page.insert_text((20, 25 + i * 25), line, fontname='china-s', fontsize=10)
        page.insert_text((300, 250), '（小写）', fontname='china-s', fontsize=10)
        # Currency visually overlaps the closing full-width bracket's glyph box,
        # as in the SF source PDF; locate the words, not the bracket edge.
        page.insert_text((336, 250), '¥43.00', fontsize=10)
        # Tax extracted LAST must never be mistaken for invoice total.
        page.insert_text((480, 190), '¥2.43', fontsize=10)
        page.add_freetext_annot(pymupdf.Rect(500, 290, 590, 312), 'Download:1', fontsize=9)
        doc.save(invoice)
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=595, height=842)
            lines = (['顺丰电子发票 — 运单明细', '发票号码：' + number, '发票总金额(元)43.00']
                     if kind == '顺丰' else ['运单起止日期：2026-09-01至2026-09-01', '共1笔运单，合计43.00元', '订单号 发件信息 收件信息'])
            lines += [f'运单记录第{i + 1}页', f'SF123456789{i} 43.00']
            for j, line in enumerate(lines):
                page.insert_text((20, 45 + j * 28), line, fontname='china-s', fontsize=10)
            page.insert_text((500, 815), f'{i+1}/{pages}', fontsize=10)
        doc.save(detail)
    return [invoice, detail]


@pytest.mark.parametrize('kind', ['顺丰', '同城'])
def test_amount_and_vector_package_preserve_originals(tmp_path: Path, kind: str) -> None:
    files = make_courier_pair(tmp_path / 'source', kind)
    before = [file.read_bytes() for file in files]
    invoice, detail = [inspect_courier_pdf(file, kind) for file in files]
    assert invoice.amount == detail.amount == Decimal('43.00')
    assert invoice.is_invoice and not detail.is_invoice
    output = tmp_path / 'print.pdf'
    compose_courier_package(detail.path, invoice.path, output, invoice.amount)
    with pymupdf.open(output) as pdf:
        assert len(pdf) == 1 and abs(pdf[0].rect.height - 841.89) < 1
        assert 'SF1234567890' in pdf[0].get_text()
        assert 'Download:1' in pdf[0].get_text()
    assert [file.read_bytes() for file in files] == before


def test_three_detail_pages_pair_last_with_invoice(tmp_path: Path) -> None:
    invoice, detail = make_courier_pair(tmp_path / 'source', '顺丰', pages=3)
    output = tmp_path / 'print.pdf'
    compose_courier_package(detail, invoice, output, Decimal('43.00'))
    with pymupdf.open(output) as pdf:
        assert len(pdf) == 2
        assert 'SF1234567890' in pdf[0].get_text() and 'SF1234567891' in pdf[0].get_text()
        assert 'SF1234567892' in pdf[1].get_text() and NUMBER in pdf[1].get_text()


def test_wrong_kind_is_rejected(tmp_path: Path) -> None:
    files = make_courier_pair(tmp_path / 'source', '同城')
    for file in files:
        with pytest.raises(ValueError, match='类型'):
            inspect_courier_pdf(file, '顺丰')
