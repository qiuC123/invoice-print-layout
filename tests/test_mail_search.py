from datetime import date
from email.message import EmailMessage
from pathlib import Path
import base64
import io
import json
import zipfile

import pytest
from PIL import Image

from invoice_print_layout import mail_search, mail163
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf
from tests.test_mail163 import FakeImap


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = ExpenseStore(tmp_path / 'workspace')
    item = store.create({'title': '采购事项', 'amount': '68.70', 'category': '材料采购',
                         'project': '甲', 'merchant': '测试商店', 'order_number': '100000000000000001'})
    monkeypatch.setattr(mail163, 'read_mail_settings', lambda _: mail163.Mail163Settings('a@163.com'))
    monkeypatch.setattr(mail163, 'read_auth_code', lambda _: 'test')
    pdf = make_invoice_pdf(tmp_path / 'test.pdf').read_bytes()
    return store, item, pdf


def mail(html='', pdf=None, subject='电子发票通知'):
    m = EmailMessage(); m['Subject'] = subject; m['From'] = 'test@example.com'
    m.set_content('测试商店 68.70 订单号100000000000000001')
    if html: m.add_alternative(html, subtype='html')
    if pdf: m.add_attachment(pdf, maintype='application', subtype='pdf', filename='发票.pdf')
    return m.as_bytes()


def run(setup, messages, downloader=lambda _: b'', client=None):
    store, item, _ = setup
    client = client or FakeImap(messages)
    result = mail_search.search_invoices(store, item['id'], '2026-01-01', date.today().isoformat(),
        imap_factory=lambda *a, **k: client, downloader=downloader)
    return result, client


def test_discovery_is_read_only_and_association_is_explicit_and_idempotent(setup):
    store, item, pdf = setup
    before = store.list_items()
    result, client = run(setup, {b'1': mail(pdf=pdf)})
    c = result['candidates'][0]
    assert c['status'] == 'downloaded' and '订单号一致' in c['reasons']
    assert store.list_items() == before
    assert client.readonly and client.logged_out
    assert all('BODY.PEEK' in q for q in client.fetch_queries)
    args = (store, item['id'], result['search_id'], c['id'])
    assert not mail_search.associate(*args)['duplicate']
    after = store.get(item['id'])
    assert after['stage'] == 'draft' and not after['verified']
    assert len(after['attachments']) == 1 and after['attachments'][0]['role'] == 'invoice'
    assert mail_search.associate(*args)['duplicate']
    assert store.get(item['id']) == after


def test_taobao_fapiao_download_and_zip_dedup(setup):
    _, _, pdf = setup
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('../../invoice.pdf', pdf)
    calls = []
    def download(url):
        calls.append(url)
        return archive.getvalue() if 'aliyuncs' in url else pdf
    html = ('<a href="https://bucket.aliyuncs.com/invoice.zip?secret=x">票</a>'
            '<a href="https://www.fapiao.com/dzfp-web/pdf/download?secret=y">PDF</a>')
    result, _ = run(setup, {b'1': mail(html)}, download)
    assert len(calls) == 2 and len(result['candidates']) == 1
    path, name = mail_search.candidate_file(setup[0], result['search_id'], result['candidates'][0]['id'])
    assert path.parent.name == result['search_id'] and name == 'invoice.pdf'
    assert path.read_bytes() == pdf


def test_failed_link_is_not_an_invoice_and_no_arbitrary_download(setup):
    html = ('<a href="https://bucket.aliyuncs.com/i.pdf">发票</a>'
            '<a href="http://127.0.0.1/private">发票</a><img src="https://example.com/tracker">')
    calls = []
    def download(url):calls.append(url);return b'<html>login</html>'
    result, _ = run(setup, {b'1': mail(html)}, download)
    assert len(calls) == 1
    c = result['candidates'][0]
    assert c['status'] == 'needs_action' and not c['file']
    with pytest.raises(ValueError, match='尚未取得'):
        mail_search.associate(setup[0], setup[1]['id'], result['search_id'], c['id'])


def test_qr_only_mail_exposes_local_image_not_invoice(setup, monkeypatch):
    out = io.BytesIO(); Image.new('RGB', (20,20), 'white').save(out, format='PNG')
    encoded = base64.b64encode(out.getvalue()).decode()
    monkeypatch.setattr(mail_search, 'qr_value', lambda _: 'https://www.fapiao.com/claim?t=test')
    result, _ = run(setup, {b'1': mail(f'<img src="data:image/png;base64,{encoded}">')})
    c = result['candidates'][0]
    assert c['status'] == 'needs_action' and c['preview'] and c['link'].startswith('https:')
    assert setup[0].get(setup[1]['id'])['attachments'] == []


@pytest.mark.parametrize('url', ['http://bucket.aliyuncs.com/a.pdf', 'https://www.fapiao.com.evil/a.pdf',
    'https://127.0.0.1/a.pdf', 'https://a@bucket.aliyuncs.com/a.pdf', 'https://bucket.aliyuncs.com:8000/a.pdf',
    'file:///etc/passwd', 'https://www.fapiao.com/login', 'https://evil.com/a.pdf'])
def test_download_allowlist(url):
    assert not mail_search.downloadable(url)


def test_candidate_tamper_wrong_item_and_changed_matter_rejected(setup):
    store, item, pdf = setup
    result, _ = run(setup, {b'1': mail(pdf=pdf)})
    c = result['candidates'][0]
    other = store.create({'title': '另一事项', 'amount': '68.70'})
    with pytest.raises(ValueError, match='不属于'):
        mail_search.associate(store, other['id'], result['search_id'], c['id'])
    store.update(item['id'], {**item, 'amount': '99.00'})
    with pytest.raises(ValueError, match='事项已修改'):
        mail_search.associate(store, item['id'], result['search_id'], c['id'])
    path,_ = mail_search.candidate_file(store, result['search_id'], c['id'])
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='已变化'):
        mail_search.candidate_file(store, result['search_id'], c['id'])


def test_already_used_by_other_expense_is_blocked(setup):
    store, item, pdf = setup
    other = store.create({'title': '旧事项', 'amount': '68.70'})
    store.add_attachment(other['id'], 'invoice.pdf', pdf, 'invoice')
    result, _ = run(setup, {b'1': mail(pdf=pdf)})
    with pytest.raises(ValueError, match='其他事项'):
        mail_search.associate(store, item['id'], result['search_id'], result['candidates'][0]['id'])


def test_range_and_partial_failure_are_visible(setup):
    class Broken(FakeImap):
        def uid(self, command, *args):
            if command == 'FETCH': return 'NO', []
            assert 'BEFORE' in args
            return super().uid(command,*args)
    result, _ = run(setup, {}, client=Broken({b'1': b''}))
    assert result['warnings'] and result['candidates'] == []
    with pytest.raises(ValueError, match='日期范围'):
        mail_search.search_invoices(setup[0],setup[1]['id'],'2020-01-01','2026-01-01')


def test_duplicate_mails_and_no_direct_file_fallback(setup):
    result, _ = run(setup, {b'1': mail(pdf=setup[2]), b'2': mail(pdf=setup[2]),
                          b'3': mail('<p>微信扫码领取发票</p>')})
    assert sum(c['status']=='downloaded' for c in result['candidates']) == 1
    assert any('原邮件' in c['detail'] for c in result['candidates'])


def test_too_many_zip_entries_rejected(setup):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for i in range(31):z.writestr(str(i)+'.pdf', setup[2])
    with pytest.raises(ValueError, match='过多'):
        mail_search.files_from(out.getvalue(),'a.zip')


def test_redirects_revalidated_and_errors_do_not_leak_signed_urls(monkeypatch):
    from urllib.error import HTTPError
    class Response(io.BytesIO):
        headers = {}
    class Opener:
        def __init__(self, location):self.calls=[];self.location=location
        def open(self, request, timeout):
            self.calls.append(request.full_url)
            if len(self.calls)==1:
                raise HTTPError(request.full_url,302,'redirect',{'Location':self.location},None)
            return Response(b'%PDF-test')
    url='https://www.fapiao.com/dzfp-web/pdf/download?secret=PRIVATE'
    opener=Opener('https://www.fapiao.com/DownLoad/downloadController/download?t=PRIVATE')
    monkeypatch.setattr(mail_search.urllib.request,'build_opener',lambda *a:opener)
    assert mail_search.download(url)==b'%PDF-test' and len(opener.calls)==2
    opener=Opener('http://127.0.0.1/internal?secret=PRIVATE')
    with pytest.raises(ValueError) as e:mail_search.download(url)
    assert len(opener.calls)==1 and 'PRIVATE' not in str(e.value)


def test_qr_local_decode():
    cv=pytest.importorskip('cv2')
    code=cv.QRCodeEncoder_create().encode('https://www.fapiao.com/claim?id=synthetic')
    ok,payload=cv.imencode('.png',code)
    assert ok and mail_search.qr_value(payload.tobytes())=='https://www.fapiao.com/claim?id=synthetic'


def test_trip_attachment_keeps_explicit_material_role(setup):
    m=EmailMessage();m['Subject']='电子发票及行程单';m.set_content('附件')
    m.add_attachment(setup[2],maintype='application',subtype='pdf',filename='行程报销单.pdf')
    result,_=run(setup,{b'1':m.as_bytes()});c=result['candidates'][0]
    assert c['suggested_role']=='trip'
    mail_search.associate(setup[0],setup[1]['id'],result['search_id'],c['id'],'trip')
    assert setup[0].get(setup[1]['id'])['attachments'][0]['role']=='trip'


def test_http_search_preview_and_confirm(setup, monkeypatch):
    import threading
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError
    from invoice_print_layout.workbench_web import make_server
    store,item,pdf=setup
    monkeypatch.setattr(mail_search.imaplib,'IMAP4_SSL',lambda *a,**k:FakeImap({b'1':mail(pdf=pdf)}))
    server=make_server(store.workspace,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base+'/api/state') as r:
            state=json.load(r);token=state['token'];assert state['mail_search_ready']
        body={'id':item['id'],'start':'2026-01-01','end':date.today().isoformat()}
        with pytest.raises(HTTPError) as e:
            urlopen(Request(base+'/api/mail/search',data=json.dumps(body).encode()))
        assert e.value.code==403
        def post(endpoint,body):
            with urlopen(Request(base+endpoint,data=json.dumps(body).encode(),headers={'X-Workbench-Token':token})) as r:return json.load(r)
        result=post('/api/mail/search',body);candidate=result['candidates'][0]
        with urlopen(base+candidate['preview']) as r:assert r.read()==pdf
        assert store.get(item['id'])['attachments']==[]
        post('/api/mail/associate',{'id':item['id'],'search_id':result['search_id'],'candidate_id':candidate['id']})
        assert len(store.get(item['id'])['attachments'])==1
        with pytest.raises(HTTPError):urlopen(base+'/mail-candidate/invalid/invalid')
    finally:
        server.shutdown();server.server_close();thread.join()
