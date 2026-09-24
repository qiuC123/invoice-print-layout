from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import shutil
import json
import threading
from urllib.request import Request, urlopen
import zipfile
from xml.etree import ElementTree as ET

import pymupdf
import pytest
from PIL import Image

import invoice_print_layout.reimbursement as report
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.report_excel import TEMPLATE_NAME, create_excel, install_template
from invoice_print_layout.workbench_web import make_server
from invoice_print_layout.takeout import OcrLine
from tests.helpers import make_trip_pdf, make_invoice_pdf
from tests.test_courier import make_courier_pair


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExpenseStore:
    store = ExpenseStore(tmp_path / 'workspace')
    (store.root / TEMPLATE_NAME).write_bytes(b'synthetic template')
    monkeypatch.setattr(report, 'validate_template', lambda _: None)
    monkeypatch.setattr(report, 'spreadsheet_runtime', lambda: (Path('node'), Path('modules')))
    def excel(template: Path, items: list[dict[str, Any]], options: dict[str, str], output: Path) -> None:
        assert all(x['pages'] for x in items)
        output.write_bytes(b'synthetic excel')
    monkeypatch.setattr(report, 'create_excel', excel)
    return store


def matter(store: ExpenseStore, category: str, amount: str, paths: list[tuple[str, Path]]) -> dict[str, Any]:
    item = store.create({'title': category+'测试', 'category': category, 'amount': amount, 'expense_date': ''})
    for role, path in paths:
        store.add_attachment(item['id'], path.name, path.read_bytes(), role)
    return store.transition(item['id'], 'verify')


def test_report_ride_three_pages_and_courier_groups(store: ExpenseStore, tmp_path: Path) -> None:
    ride = matter(store, '打车', '123.45', [('trip',make_trip_pdf(tmp_path/'trip.pdf',pages=3)),
                                            ('invoice',make_invoice_pdf(tmp_path/'invoice.pdf'))])
    invoice, detail = make_courier_pair(tmp_path/'courier', '顺丰')
    courier = matter(store, '顺丰', '43', [('detail',detail),('invoice',invoice)])
    originals = {p:p.read_bytes() for p in [tmp_path/'trip.pdf',tmp_path/'invoice.pdf',invoice,detail]}
    before = store.list_items()
    result = report.create_report(store, [ride['id'],courier['id']], {'person':'Test'})
    assert set(result) == {'pdf','md','xlsx'}
    assert len({Path(x).stem for x in result.values()}) == 1
    folder = store.root / '导出'
    with pymupdf.open(folder/result['pdf']) as pdf:
        assert len(pdf) == 3
        assert 'ADVERTISEMENT' not in ''.join(p.get_text() for p in pdf)
        assert 'TRIP PAGE 3' in pdf[1].get_text()
        assert '123.45' in pdf[1].get_text()
        assert 'SF1234567890' in pdf[2].get_text()
        assert 'Download:1' in pdf[2].get_text()
    assert '166.45' in (folder/result['md']).read_text(encoding='utf-8')
    assert all(x['stage']=='submitted' and x['report_files']==result for x in store.list_items())
    assert all(x['stage']=='draft' for x in before)
    assert all(p.read_bytes() == content for p,content in originals.items())
    assert not list(folder.glob('.report-*'))


@pytest.mark.parametrize('stage', ['submitted','reimbursed','cancelled'])
def test_new_report_rejects_non_draft(store: ExpenseStore, tmp_path: Path, stage: str) -> None:
    x = matter(store, '酒店', '123.45', [('invoice',make_invoice_pdf(tmp_path/'invoice.pdf'))])
    if stage == 'reimbursed':
        store.transition(x['id'], 'submitted')
    store.transition(x['id'], stage)
    with pytest.raises(ValueError, match='未提交'):
        report.create_report(store,[x['id']])


def test_missing_material_or_review_cannot_report(store: ExpenseStore) -> None:
    x=store.create({'title':'Missing','category':'酒店','amount':'10'})
    with pytest.raises(ValueError,match='核对'):
        report.create_report(store,[x['id']])


def test_duplicate_source_or_order_is_rejected(store: ExpenseStore, tmp_path: Path) -> None:
    file=make_invoice_pdf(tmp_path/'invoice.pdf')
    a=matter(store,'酒店','123.45',[('invoice',file)])
    b=matter(store,'酒店','123.45',[('invoice',file)])
    with pytest.raises(ValueError,match='相同凭证'):
        report.create_report(store,[a['id'],b['id']])


def test_mismatch_or_excel_failure_publishes_nothing(store: ExpenseStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x=matter(store,'打车','100',[('trip',make_trip_pdf(tmp_path/'trip.pdf')),('invoice',make_invoice_pdf(tmp_path/'invoice.pdf'))])
    with pytest.raises(ValueError,match='金额'):
        report.create_report(store,[x['id']])
    x=store.update(x['id'],{**x,'amount':'123.45'})
    store.transition(x['id'],'verify')
    def fail(*args: Any) -> None:
        raise ValueError('synthetic excel failure')
    monkeypatch.setattr(report,'create_excel',fail)
    with pytest.raises(ValueError,match='synthetic'):
        report.create_report(store,[x['id']])
    assert list((store.root/'导出').iterdir()) == []


def test_takeout_uses_existing_composer(store: ExpenseStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import invoice_print_layout.receipt_intake as intake
    image=tmp_path/'order.png'
    Image.new('RGB',(600,1000),'white').save(image)
    rows=[OcrLine(t,10,y,500,y+30) for t,y in [('订单已送达',100),('闪购 测试餐厅',200),
          ('实付￥123.45',300),('订单号1000000000000000001',500)]]
    monkeypatch.setattr(intake,'read_receipt',lambda *args:rows)
    x=matter(store,'外卖','123.45',[('order',image),('invoice',make_invoice_pdf(tmp_path/'invoice.pdf'))])
    result=report.create_report(store,[x['id']])
    with pymupdf.open(store.root/'导出'/result['pdf']) as pdf:
        assert len(pdf)==1 and pdf[0].get_images() and '123.45' in pdf[0].get_text()


def test_material_payment_preserves_full_pages(store: ExpenseStore, tmp_path: Path) -> None:
    x=matter(store,'材料采购','123.45',[('purchase',make_trip_pdf(tmp_path/'receipt.pdf')),
                                       ('payment',make_invoice_pdf(tmp_path/'payment.pdf'))])
    result=report.create_report(store,[x['id']])
    with pymupdf.open(store.root/'导出'/result['pdf']) as pdf:
        assert len(pdf)==2 and 'ADVERTISEMENT' in pdf[0].get_text()
    assert '扣费记录替代' in (store.root/'导出'/result['md']).read_text(encoding='utf-8')


def test_report_submission_failure_removes_outputs(store: ExpenseStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    x=matter(store,'酒店','123.45',[('invoice',make_invoice_pdf(tmp_path/'invoice.pdf'))])
    before=store.list_items()
    def fail(*args: Any) -> None:
        raise ValueError('submission failed')
    monkeypatch.setattr(store,'submit_report',fail)
    with pytest.raises(ValueError,match='submission failed'):
        report.create_report(store,[x['id']])
    assert store.list_items()==before
    assert list((store.root/'导出').iterdir())==[]


def test_report_submission_is_atomic_on_changed_matter(store: ExpenseStore, tmp_path: Path) -> None:
    a=matter(store,'酒店','123.45',[('invoice',make_invoice_pdf(tmp_path/'a.pdf'))])
    b=matter(store,'酒店','123.45',[('invoice',make_invoice_pdf(tmp_path/'b.pdf'))])
    store.update(b['id'],{**b,'amount':'100'})
    with pytest.raises(ValueError,match='变化'):
        store.submit_report([a,b],{'pdf':'test.pdf','md':'test.md','xlsx':'test.xlsx'})
    assert store.get(a['id'])['stage']=='draft'
    assert store.get(a['id'])['events']==a['events']
    assert store.get(b['id'])['stage']=='draft'


def test_template_rejects_invalid_without_overwriting(tmp_path: Path) -> None:
    path=tmp_path/TEMPLATE_NAME
    path.write_bytes(b'original')
    with pytest.raises(ValueError):
        install_template(tmp_path,b'not xlsx')
    assert path.read_bytes()==b'original'


def test_report_http_and_three_downloads(store: ExpenseStore, tmp_path: Path) -> None:
    x=matter(store,'酒店','123.45',[('invoice',make_invoice_pdf(tmp_path/'invoice.pdf'))])
    server=make_server(store.workspace,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(url+'/api/state') as response:
            state=json.load(response)
        assert state['report_template_ready']
        body=json.dumps({'ids':[x['id']],'options':{'person':'Test','payment':'personal'}}).encode()
        with urlopen(Request(url+'/api/report',data=body,headers={'Content-Type':'application/json','X-Workbench-Token':state['token']})) as response:
            result=json.load(response)
        from urllib.parse import quote
        for name in result.values():
            with urlopen(url+'/export/'+quote(name)) as response:
                assert response.status==200 and len(response.read())>0
        assert store.get(x['id'])['stage']=='submitted'
    finally:
        server.shutdown();server.server_close();thread.join()


def test_company_excel_integration(tmp_path: Path) -> None:
    template=os.environ.get('INVOICE_TEST_REPORT_TEMPLATE')
    if not template:
        pytest.skip('Set INVOICE_TEST_REPORT_TEMPLATE for private-template integration')
    source=Path(template)
    original=source.read_bytes()
    output=tmp_path/'report.xlsx'
    items=[{'id':f'synthetic{i}', 'project':'测试项目', 'expense_date':'2026-09-21' if i else '',
            'category':'材料采购','title':'=literal' if i==0 else f'测试材料{i}', 'merchant':'测试商店',
            'amount_cents':1001,'alternative':i==0,'pages':str(i+1),'order_number':'1000000000000000001'} for i in range(7)]
    create_excel(source,items,{'person':'Test','payment':'personal','period':'2026-09'},output)
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(output) as z:
        main=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        def cell(ref: str) -> ET.Element:
            found=main.find(f'.//s:c[@r="{ref}"]',ns)
            assert found is not None
            return found
        total=cell('H35').find('s:v',ns)
        assert total is not None and round(float(total.text or '0')*100)==7007
        assert cell('H35').find('s:f',ns) is not None
        assert cell('H28').find('s:f',ns) is not None
        for row in range(8,36):
            for col in 'CDEFGIJKLM':
                entry=main.find(f'.//s:c[@r="{col}{row}"]',ns)
                assert entry is None or (entry.find('s:v',ns) is None and entry.find('s:f',ns) is None and entry.find('s:is',ns) is None)
        setup=main.find('s:pageSetup',ns)
        assert setup is not None and setup.get('fitToHeight') == '1'
        detail=ET.fromstring(z.read('xl/worksheets/sheet2.xml'))
        setup=detail.find('s:pageSetup',ns)
        assert setup is not None and setup.get('orientation') == 'landscape' and setup.get('fitToHeight') == '0'
        assert detail.find('.//s:c[@r="F2"]/s:f',ns) is None
        order=detail.find('.//s:c[@r="L2"]',ns)
        assert order is not None and order.get('t') in ('s','inlineStr','str')
        if order.get('t') == 's':
            strings=ET.fromstring(z.read('xl/sharedStrings.xml'))
            value=order.find('s:v',ns)
            assert value is not None
            assert ''.join(strings[int(value.text or '0')].itertext()) == '1000000000000000001'
        else:
            assert ''.join(order.itertext()) == '1000000000000000001'
    assert source.read_bytes()==original
