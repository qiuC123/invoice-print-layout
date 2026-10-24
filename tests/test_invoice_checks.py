import io
from pathlib import Path
import pymupdf
import qrcode
import pytest

from invoice_print_layout.invoice_checks import parse_tax_qr, read_tax_qr, compare_qr, issue_date
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.workbench import ExpenseStore

NUMBER = '12345678901234567890'
PAYLOAD = f'01,32,,{NUMBER},14.00,20260930,,'


def pdf_bytes(payload=PAYLOAD, second=False):
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=420)
        for i, text in enumerate(['电子发票（普通发票）', '发票号码：'+NUMBER,
                                  '价税合计：14.00', '开票日期：2026年09月30日']):
            page.insert_text((180, 40+i*30), text, fontname='china-s', fontsize=10)
        if payload:
            stream=io.BytesIO();qrcode.make(payload).save(stream,format='PNG')
            page.insert_image(pymupdf.Rect(15, 15, 155, 155),stream=stream.getvalue())
            if second:
                page.insert_image(pymupdf.Rect(410, 250, 550, 390),stream=stream.getvalue())
        return doc.tobytes()


@pytest.mark.parametrize('change,status', [({},'match'),({'invoice_number':'12345678901234567000'},'mismatch'),
    ({'amount':'15.00'},'mismatch'),({'issue_date':'2026-09-29'},'mismatch'),({'issue_date':''},'partial')])
def test_qr_comparison(change,status):
    fields={'invoice_number':NUMBER,'amount':'14.00','issue_date':'2026-09-30',**change}
    assert compare_qr(parse_tax_qr(PAYLOAD),fields)['status']==status


def test_legacy_qr_net_amount_is_not_a_false_mismatch():
    qr=parse_tax_qr(PAYLOAD.replace('01,32,','01,10,'))
    result=compare_qr(qr,{'invoice_number':NUMBER,'amount':'15.00','issue_date':'2026-09-30'})
    assert result['status']=='partial' and result['not_compared']==['amount']


@pytest.mark.parametrize('value',['https://tax.example/',PAYLOAD.replace('20260930','20260230'),PAYLOAD.replace('14.00','-14.00')])
def test_invalid_tax_qr_fails_closed(value):
    assert parse_tax_qr(value) is None


@pytest.mark.parametrize('date', ['20260930', '2026-09-30'])
@pytest.mark.parametrize('spaced', [False, True])
def test_tax_qr_date_and_field_whitespace_are_normalized(date, spaced):
    payload = PAYLOAD.replace('20260930', date)
    if spaced:
        payload = ','.join(' \t' + part + ' \n' for part in payload.split(','))
    qr = parse_tax_qr(payload)
    assert qr == {'type': '32', 'code': '', 'invoice_number': NUMBER,
                  'amount': '14.00', 'issue_date': '2026-09-30'}
    assert compare_qr(qr, {'invoice_number': NUMBER, 'amount': '14.00',
                           'issue_date': '2026-09-30'})['status'] == 'match'


@pytest.mark.parametrize('date', ['2026-02-30', '2026-9-30', '2026/09/30', '2026-0930'])
def test_tax_qr_extended_date_still_rejects_invalid_or_unsupported_forms(date):
    assert parse_tax_qr(PAYLOAD.replace('20260930', date)) is None


@pytest.mark.parametrize('payload,second,readable',[(PAYLOAD,False,True),('',False,False),(PAYLOAD,True,False)])
def test_real_qr_decode_and_multi_code_rejection(payload,second,readable):
    with pymupdf.open(stream=pdf_bytes(payload,second),filetype='pdf') as doc:
        assert bool(read_tax_qr(doc[0])) is readable
        assert issue_date(doc[0])=='2026-09-30'


def test_normal_intake_stores_check_and_keeps_issue_date_separate(tmp_path: Path):
    queue=IntakeQueue(ExpenseStore(tmp_path),ai=LocalOnlyInvoiceAI())
    entry=queue.receive('synthetic.pdf',pdf_bytes())
    result=queue.prepare_batch_entry(entry['id'])
    assert result['qr_check']['status']=='match'
    assert result['fields']['issue_date']=='2026-09-30'
    assert result['fields'].get('expense_date') != '2026-09-30'
    assert queue.store.list_items()==[]
