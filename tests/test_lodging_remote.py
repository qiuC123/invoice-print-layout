from dataclasses import replace
from types import SimpleNamespace
from typing import Any
import pytest
from invoice_print_layout.lodging_remote import FeishuLodgingBackend
from invoice_print_layout.lodging import LodgingError
from invoice_print_layout.feishu_bot import is_lodging_message
from tests.test_bot import message


def test_route_preserves_owner_and_old_commands() -> None:
    sample=message('om1',text='住宿 新开',sender='owner')
    assert is_lodging_message(sample,'owner')
    assert not is_lodging_message(sample,'other')
    assert not is_lodging_message(replace(sample,chat_type='group'),'owner')
    assert not is_lodging_message(replace(sample,text='登记 酒店 500 两晚住宿'),'owner')
    assert not is_lodging_message(replace(sample,message_type='image'),'owner')


def test_backend_create_uses_stable_token_and_readback_fields() -> None:
    requests=[]
    def create(req: Any) -> Any:
        requests.append(req)
        return SimpleNamespace(success=lambda:True,data=SimpleNamespace(record=SimpleNamespace(record_id='recTest')))
    client=SimpleNamespace(bitable=SimpleNamespace(v1=SimpleNamespace(app_table_record=SimpleNamespace(create=create))))
    backend=FeishuLodgingBackend(client,'baseTest','tblTest')
    assert backend.create({'每晚单价':188},'tokenTest')=='recTest'
    assert requests[0].client_token=='tokenTest'
    assert requests[0].request_body.fields=={'每晚单价':188}


def test_backend_find_reads_all_pages_and_matches_exact_line() -> None:
    pages=[SimpleNamespace(items=[SimpleNamespace(record_id='recWrong',fields={'备注':'quoted lodging-task:test'})],has_more=True,page_token='p2'),
           SimpleNamespace(items=[SimpleNamespace(record_id='recRight',fields={'备注':[{'text':'lodging-task:test\n{}'}]})],has_more=False)]
    def listing(req: Any) -> Any:
        if len(pages)==1:
            assert req.page_token=='p2'
        return SimpleNamespace(success=lambda:True,data=pages.pop(0))
    client=SimpleNamespace(bitable=SimpleNamespace(v1=SimpleNamespace(app_table_record=SimpleNamespace(list=listing))))
    assert FeishuLodgingBackend(client,'b','t').find('lodging-task:test')[0]['record_id']=='recRight'


def test_backend_error_does_not_echo_sensitive_response() -> None:
    with pytest.raises(LodgingError) as err:
        FeishuLodgingBackend.data(SimpleNamespace(success=lambda:False,code=99991672,msg='secret-value'))
    assert '99991672' in str(err.value) and 'secret-value' not in str(err.value)
