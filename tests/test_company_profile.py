import json
from pathlib import Path
import pymupdf
import pytest

from invoice_print_layout.company_profile import check_pdf, check_screenshot, identity

NAME='合成测试公司（甲）'
TAX='12345678901234567A'


def page(doc,name=NAME,tax=TAX):
    p=doc.new_page(width=600,height=420)
    for x,y,text in [(30,75,'购买方'),(80,75,'名称：'),(120,75,name),(80,100,'统一社会信用代码：'),
                     (175,100,tax),(330,75,'销售方'),(380,75,'名称：'),(420,75,'合成销方公司')]:
        p.insert_text((x,y),text,fontname='china-s',fontsize=9)
    return p


@pytest.mark.parametrize('name,tax,status',[(NAME,TAX,'match'),('合成另一公司',TAX,'mismatch'),
                                           (NAME,'12345678901234567B','mismatch')])
def test_native_buyer_and_tax_id(tmp_path,name,tax,status):
    (tmp_path/'company-profile.json').write_text(json.dumps({'names':[identity(NAME)],'tax_ids':[TAX]}),encoding='utf-8')
    with pymupdf.open() as doc:
        result=check_pdf(tmp_path,page(doc,name,tax))
    assert result['status']==status
    assert result['name_read'] and result['tax_id_read']
    assert TAX not in json.dumps(result)


def test_no_profile_unreadable_and_screenshot_requires_buyer_label(tmp_path):
    with pymupdf.open() as doc:
        assert check_pdf(tmp_path,page(doc)) is None
    (tmp_path/'company-profile.json').write_text(json.dumps({'names':[NAME],'tax_ids':[TAX]}),encoding='utf-8')
    with pymupdf.open() as doc:
        assert check_pdf(tmp_path,doc.new_page())['status']=='unreadable'
    assert check_screenshot(tmp_path,'名称：'+NAME)['status']=='unreadable'
    assert check_screenshot(tmp_path,'购买方名称： 合成 测试公司(甲)')['status']=='partial'
