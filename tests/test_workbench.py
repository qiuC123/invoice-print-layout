from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import json
import sqlite3
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
import pymupdf

from invoice_print_layout.workbench import ExpenseStore, evaluate, amount_cents
from invoice_print_layout.workbench_web import make_server, sync_mail
from invoice_print_layout.workbench_export import export_items
from invoice_print_layout.storage import append_history, ensure_workspace
from tests.helpers import make_invoice_pdf, make_trip_pdf


@pytest.mark.parametrize('roles,complete', [({'invoice'},False),({'payment'},False),({'purchase'},False),
    ({'purchase','invoice'},True),({'purchase','payment'},True),({'purchase','payment','invoice'},True)])
def test_material_rules(roles: set[str], complete: bool) -> None:
    result = evaluate('材料采购', roles)
    assert result['complete'] == complete
    assert result['alternative'] == ('payment' in roles and 'invoice' not in roles)


@pytest.mark.parametrize('category', ['酒店','高铁'])
def test_hotel_rail_require_invoice(category: str) -> None:
    assert evaluate(category, {'invoice'})['complete']
    assert not evaluate(category, {'payment','order','purchase'})['complete']


def test_dining_persists_without_becoming_delivery_or_verified(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': '店内餐饮', 'category': '餐饮', 'amount': '15.00'})
    saved = ExpenseStore(tmp_path).get(item['id'])
    assert saved['category'] == '餐饮'
    assert saved['stage'] == 'draft' and not saved['verified']
    assert not evaluate('餐饮', {'payment'})['complete']
    assert not evaluate('餐饮', {'invoice'})['complete']
    assert evaluate('餐饮', {'invoice', 'purchase'})['complete']
    assert evaluate('餐饮', {'invoice', 'order'})['complete']
    assert not evaluate('外卖', {'invoice', 'purchase'})['complete']


@pytest.mark.parametrize('category', ['餐饮', '材料采购'])
@pytest.mark.parametrize('voucher', ['invoice', 'receipt'])
def test_voucher_payment_requires_both_materials(category: str, voucher: str) -> None:
    result = evaluate(category, {voucher}, material_basis='invoice_payment')
    assert result['missing'] == ['付款截图'] and not result['complete']
    assert evaluate(category, {voucher, 'payment'}, material_basis='invoice_payment')['complete']
    assert evaluate(category, {'purchase', 'payment'}, material_basis='invoice_payment')['missing'] == ['发票或收据']
    assert evaluate(category, {'order', 'payment'}, material_basis='invoice_payment')['missing'] == ['发票或收据']
    assert evaluate(category, {'invoice', 'order'}, material_basis='invoice_payment')['missing'] == ['付款截图']


def test_voucher_payment_changes_require_review_and_real_files(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Receipt and payment', 'category': '餐饮', 'amount': '15.00',
                         'material_basis': 'invoice_payment', 'invoice_state': 'requested'})
    receipt = store.add_attachment(item['id'], 'receipt.pdf', make_invoice_pdf(tmp_path/'receipt.pdf').read_bytes(), 'receipt')
    assert receipt['missing'] == ['付款截图']
    item = store.add_attachment(item['id'], 'payment.pdf', make_trip_pdf(tmp_path/'payment.pdf').read_bytes(), 'payment')
    assert item['complete'] and not item['verified'] and not item['ready']
    reopened = ExpenseStore(tmp_path).get(item['id'])
    assert reopened['material_basis'] == 'invoice_payment' and reopened['complete']
    store.transition(item['id'], 'verify')
    updated = store.update(item['id'], {**store.get(item['id']), 'note': 'Payment checked again'})
    assert updated['complete'] and not updated['verified']
    with store.connect() as db:
        payment_path = Path(db.execute("SELECT path FROM attachments WHERE item_id=? AND role='payment'", (item['id'],)).fetchone()['path'])
    payment_path.unlink()
    assert store.get(item['id'])['missing'] == ['付款截图']


@pytest.mark.parametrize('amount', ['NaN','Infinity','-1','1.001','bad'])
def test_invalid_amount(amount: str) -> None:
    with pytest.raises(ValueError):
        amount_cents(amount)


def test_unknown_expense_date_stays_empty(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title': 'Unknown purchase date', 'category': '外卖',
                         'amount': '58.30', 'expense_date': ''})
    assert item['expense_date'] == ''
    assert ExpenseStore(tmp_path).get(item['id'])['expense_date'] == ''
    assert store.update(item['id'], {**item, 'note': 'Date pending'})['expense_date'] == ''
    changed = store.update(item['id'], {**item, 'expense_date': '2026-09-18'})
    assert changed['expense_date'] == '2026-09-18'
    with pytest.raises(ValueError):
        store.update(item['id'], {**item, 'expense_date': '2026-02-30'})
    assert store.get(item['id'])['expense_date'] == '2026-09-18'


def test_late_material_persists_and_workflow_requires_review(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title':'材料采购测试','category':'材料采购','amount':'128.50','followup_date':date.today().isoformat()})
    assert item['overdue'] and not item['complete']
    first = make_trip_pdf(tmp_path/'receipt.pdf').read_bytes()
    second = make_invoice_pdf(tmp_path/'payment.pdf').read_bytes()
    store.add_attachment(item['id'],'receipt.pdf',first,'purchase')
    store = ExpenseStore(tmp_path)
    with pytest.raises(ValueError):
        store.transition(item['id'],'submitted')
    result = store.add_attachment(item['id'],'payment.pdf',second,'payment')
    assert result['complete'] and result['alternative'] and not result['verified']
    store.add_attachment(item['id'],'same.pdf',second,'payment')
    assert len(store.get(item['id'])['attachments']) == 2
    store.transition(item['id'],'verify')
    with pytest.raises(ValueError):
        store.transition(item['id'],'reimbursed')
    store.transition(item['id'],'submitted')
    with pytest.raises(ValueError):
        store.add_attachment(item['id'],'invoice.pdf',second,'invoice')
    assert store.transition(item['id'],'reimbursed')['stage'] == 'reimbursed'
    store.transition(item['id'],'draft')
    invoice = store.add_attachment(item['id'],'invoice.pdf',second,'invoice')
    assert not invoice['alternative'] and not invoice['verified']


def test_role_reclassification_invalidates_verification_and_missing_file(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    item = store.create({'title':'酒店','category':'酒店','amount':'500'})
    item = store.add_attachment(item['id'],'invoice.pdf',make_invoice_pdf(tmp_path/'source.pdf').read_bytes(),'invoice')
    store.transition(item['id'],'verify')
    key = item['attachments'][0]['id']
    store.set_role(key,'payment')
    assert not store.get(item['id'])['complete'] and not store.get(item['id'])['verified']
    store.set_role(key,'invoice')
    path,_ = store.attachment_path(key)
    path.unlink()
    assert not store.get(item['id'])['complete']


def test_import_history_once_without_reimbursement(tmp_path: Path) -> None:
    paths = ensure_workspace(tmp_path)
    source = make_invoice_pdf(paths.completed/'old.pdf')
    append_history(paths.history, {'output':str(source),'archive':str(paths.archived), 'amount':'68.70',
                                 'category':'打车','invoice_date':'2026-09-01','provider':'滴滴'})
    store = ExpenseStore(tmp_path)
    assert store.import_history()['added'] == 1
    assert store.import_history()['added'] == 0
    item = store.list_items()[0]
    assert item['complete'] and not item['verified'] and item['stage'] == 'draft'
    assert source.exists()
    changed = store.update(item['id'],{**item,'category':'材料采购'})
    assert not changed['complete']


def test_export_single_bundle_preserves_originals_and_status(tmp_path: Path) -> None:
    store = ExpenseStore(tmp_path)
    invoice = make_invoice_pdf(tmp_path/'invoice.pdf')
    receipt = make_trip_pdf(tmp_path/'receipt.pdf')
    hotel = store.create({'title':'酒店测试','category':'酒店','amount':'500'})
    materials = store.create({'title':'采购测试','category':'材料采购','amount':'68.70'})
    store.add_attachment(hotel['id'],'invoice.pdf',invoice.read_bytes(),'invoice')
    store.add_attachment(materials['id'],'receipt.pdf',receipt.read_bytes(),'purchase')
    store.add_attachment(materials['id'],'payment.pdf',invoice.read_bytes(),'payment')
    with pytest.raises(ValueError):
        export_items(store,[hotel['id'],materials['id']])
    for item in (hotel,materials):
        store.transition(item['id'],'verify')
    result = export_items(store,[hotel['id'],materials['id']])
    with pymupdf.open(store.root/'导出'/result['pdf']) as pdf:
        assert len(pdf) == 3
        assert all(abs(page.rect.height-841.89)<1 for page in pdf)
    markdown = (store.root/'导出'/result['md']).read_text(encoding='utf-8')
    assert '568.70' in markdown and '无发票替代' in markdown
    assert store.get(hotel['id'])['stage'] == 'draft' and invoice.exists() and receipt.exists()


def test_http_requires_loopback_host_token_and_origin(tmp_path: Path) -> None:
    server = make_server(tmp_path,0)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(url+'/api/state') as response:
            token=json.load(response)['token']
        with pytest.raises(HTTPError):
            urlopen(Request(url+'/api/create',data=b'{}',headers={'Content-Type':'application/json'}))
        for headers in [{'Host':'evil.example'}, {'Origin':'https://evil.example'}]:
            with pytest.raises(HTTPError):
                urlopen(Request(url+'/api/state',headers=headers))
        data=json.dumps({'title':'网络创建测试','category':'高铁','amount':'200'}).encode()
        with urlopen(Request(url+'/api/create',data=data,headers={'Content-Type':'application/json','X-Workbench-Token':token})) as response:
            item=json.load(response)
        assert item['missing']==['12306发票']
        with urlopen(url+'/') as response:
            assert '发票助手' in response.read().decode()
        from invoice_print_layout.logistics import LogisticsStore
        project = LogisticsStore(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic photos')
        with urlopen(url + '/api/photos?project=' + project['id']) as response:
            assert 'briefs' not in json.load(response)
        with pytest.raises(HTTPError) as removed:
            urlopen(url + '/photo-review/' + project['id'] + '/key/0')
        assert removed.value.code == 404
        for action in ('brief-settings', 'brief-preview', 'brief-read'):
            body = json.dumps({'project_id': project['id']}).encode()
            with pytest.raises(HTTPError) as removed:
                urlopen(Request(url + '/api/photos/' + action, data=body,
                                headers={'Content-Type': 'application/json', 'X-Workbench-Token': token}))
            assert removed.value.code == 400
        with sqlite3.connect(tmp_path / '后勤' / 'tasks.sqlite3') as db:
            assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'photo_brief%'").fetchall()
        exported=tmp_path/'工作台'/'导出'
        exported.mkdir(exist_ok=True)
        (exported/'中文.md').write_text('测试清单',encoding='utf-8')
        with urlopen(url+'/export/%E4%B8%AD%E6%96%87.md') as response:
            assert response.read().decode()=='测试清单'
    finally:
        server.shutdown();server.server_close();thread.join()




def test_mail_cursor_advanced_only_after_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import invoice_print_layout.mail163 as mail
    from invoice_print_layout.reliability import save_json,read_json
    store=ExpenseStore(tmp_path)
    cursor=store.root/'mail-sync.json'
    earlier=(date.today()-timedelta(days=3)).isoformat()
    save_json(cursor,{'date':earlier})
    monkeypatch.setattr(mail,'read_mail_settings',lambda _:mail.Mail163Settings('test@163.com'))
    monkeypatch.setattr(mail,'read_auth_code',lambda _:'fake')
    def failed(*args: object, **kwargs: object) -> object:
        assert kwargs['days']==4 and kwargs['all_invoice_types'] is True
        raise mail.MailImportError('temporary')
    monkeypatch.setattr(mail,'import_pdf_attachments',failed)
    with pytest.raises(mail.MailImportError):
        sync_mail(store)
    assert read_json(cursor)['date']==earlier
    monkeypatch.setattr(mail,'import_pdf_attachments',lambda *args,**kwargs:mail.MailImportSummary(downloaded=1))
    sync_mail(store)
    assert read_json(cursor)['date']==date.today().isoformat()


def test_legacy_missing_fields_imported_for_manual_review(tmp_path: Path) -> None:
    paths=ensure_workspace(tmp_path)
    source=make_invoice_pdf(paths.completed/'2026-09-01_滴滴_68.70元_Test.pdf')
    append_history(paths.history,{'output':str(source),'archive':str(paths.archived)})
    store=ExpenseStore(tmp_path)
    assert store.import_history()['added']==1
    item=store.list_items()[0]
    assert item['amount']=='68.70' and item['stage']=='draft' and not item['verified']
