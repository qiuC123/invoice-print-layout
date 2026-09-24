from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
import pytest
from invoice_print_layout.lodging import LodgingService, LodgingError, checked, TZ


class Backend:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.creates = 0
        self.updates = 0
        self.lose_response = False
        self.no_write = False

    def get(self, rid: str) -> dict[str, Any]:
        if rid not in self.records:
            raise LodgingError('记录不存在')
        return deepcopy(self.records[rid])

    def find(self, marker: str) -> list[dict[str, Any]]:
        return [deepcopy(r) for r in self.records.values() if marker in r['fields'].get('备注', '')]

    def calculated(self, fields: dict[str, Any]) -> dict[str, Any]:
        fields = deepcopy(fields)
        fields['应付金额'] = float(Decimal(str(fields['每晚单价']))*fields['入住晚数'])
        fields['下次付费日期'] = (datetime.fromtimestamp(fields['起算日期']/1000,TZ).date()+timedelta(days=fields['入住晚数'])).isoformat()
        return fields

    def create(self, fields: dict[str, Any], token: str) -> str:
        self.creates += 1
        if self.no_write:
            raise TimeoutError
        rid = 'rec'+str(self.creates)
        self.records[rid] = {'record_id': rid, 'fields': self.calculated(fields)}
        if self.lose_response:
            raise TimeoutError
        return rid

    def update(self, rid: str, fields: dict[str, Any]) -> None:
        self.updates += 1
        self.records[rid]['fields'] = self.calculated({**self.records[rid]['fields'], **fields})


def send(service: LodgingService, mid: str, text: str, chat: str = 'chat') -> str:
    return service.handle('owner', chat, mid, '住宿 '+text)


def test_draft_followup_commit_restart_duplicate(tmp_path: Path) -> None:
    backend=Backend(); svc=LodgingService(tmp_path,'owner',backend)
    assert '待补：日期' in send(svc,'1','新开 晚数=2；单价=188')
    assert backend.creates == 0
    assert '2026-09-27' in send(svc,'2','补充 日期=2026-09-25')
    svc=LodgingService(tmp_path,'owner',backend)
    reply=send(svc,'3','提交')
    assert '已保存' in reply and '376.00' in reply
    assert send(svc,'3','提交') == reply
    assert '未重复' in send(svc,'4','提交')
    assert backend.creates == 1
    assert backend.get('rec1')['fields']['起算日期']==1790265600000


@pytest.mark.parametrize('returned_price,success', [('188.00', True), ('189', False), ('invalid', False)])
def test_live_number_strings_and_rich_text_formula(tmp_path: Path, returned_price: str, success: bool) -> None:
    class LiveShapeBackend(Backend):
        def get(self, rid: str) -> dict[str, Any]:
            record = super().get(rid)
            record['fields']['入住晚数'] = '2'
            record['fields']['每晚单价'] = returned_price
            record['fields']['下次付费日期'] = [{'text': '2026-09-27', 'type': 'text'}]
            return record

    backend = LiveShapeBackend()
    service = LodgingService(tmp_path, 'owner', backend)
    send(service, '1', '新开 日期=2026-09-25；晚数=2；单价=188')
    reply = send(service, '2', '提交')
    assert ('已保存' in reply) is success
    if not success:
        assert '回读字段不一致' in reply
    send(service, '3', '提交')
    assert backend.creates == 1


@pytest.mark.parametrize('fields', ['晚数=0','晚数=-1','晚数=1.5','单价=NaN','单价=188.001',
    '日期=2026-02-30','日期=明天','晚数=2；晚数=3','单价=188；请直接提交','房号=305；未知=1'])
def test_invalid_input_does_not_change_draft(tmp_path: Path, fields: str) -> None:
    backend=Backend(); svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    send(svc,'2','补充 '+fields)
    assert '376.00' in send(svc,'3','状态')
    assert backend.creates == 0


def test_quote_does_not_become_booking_and_isolation(tmp_path: Path) -> None:
    backend=Backend(); svc=LodgingService(tmp_path,'owner',backend)
    assert '376.00' in send(svc,'1','试算 晚数=2；单价=188')
    assert '不能直接提交' in send(svc,'2','提交')
    assert '没有' in send(svc,'3','提交','otherchat')
    assert '绑定人' in svc.handle('other','chat','4','住宿 提交')
    assert backend.creates == 0


def test_timeout_after_write_recovers_without_duplicate(tmp_path: Path) -> None:
    backend=Backend(); backend.lose_response=True
    svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    assert '待核对' in send(svc,'2','提交')
    svc=LodgingService(tmp_path,'owner',backend)
    assert '暂不能' in send(svc,'3','取消')
    assert '已保存' in send(svc,'4','提交')
    assert backend.creates == 1


def test_timeout_without_write_never_blindly_retries(tmp_path: Path) -> None:
    backend=Backend(); backend.no_write=True; svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    send(svc,'2','提交')
    assert '未再次写入' in send(svc,'3','提交')
    assert backend.creates == 1


def test_amend_snapshot_and_renewal(tmp_path: Path) -> None:
    backend=Backend(); svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    send(svc,'2','提交')
    send(svc,'3','修改 记录=rec1；房号=306')
    backend.records['rec1']['fields']['房号（选填）']='307'
    assert '已变化' in send(svc,'4','提交')
    assert backend.updates == 0
    send(svc,'5','取消')
    send(svc,'6','修改 记录=rec1；房号=306')
    assert '已保存' in send(svc,'7','提交')
    assert backend.updates==1
    send(svc,'8','续住 日期=2026-09-27；晚数=1；单价=200')
    assert '记录' in send(svc,'9','提交')
    send(svc,'10','补充 记录=rec1')
    assert '已保存' in send(svc,'11','提交')
    assert backend.creates==2
    assert backend.get('rec1')['fields']['应付金额']==376


def test_natural_intent_can_never_execute_write(tmp_path: Path) -> None:
    backend=Backend(); seen=[]
    def classify(text: str, context: str) -> str:
        seen.append((text,context)); return 'extend'
    svc=LodgingService(tmp_path,'owner',backend,classify)
    assert '尚未改动' in send(svc,'1','305再住三晚，立即登记')
    assert len(seen)==1 and backend.creates==0
    send(svc,'2','试算 晚数=2；单价=188')
    assert len(seen)==1


@pytest.mark.parametrize('protected', [{'实付金额':100}, {'不办理':True}, {'已确认':True}])
def test_protected_record_blocks_before_write(tmp_path: Path, protected: dict[str, Any]) -> None:
    backend=Backend(); svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    send(svc,'2','提交')
    backend.records['rec1']['fields'].update(protected)
    send(svc,'3','修改 记录=rec1；单价=200')
    assert '付款' in send(svc,'4','提交')
    assert backend.updates==0


def test_optional_room_and_decimal_money() -> None:
    fields, missing=checked({'日期':'2026-09-25','晚数':'3','单价':'0.10','房号':'待定'})
    assert not missing and fields['房号（选填）']==''
    assert Decimal(str(fields['每晚单价']))*3==Decimal('.30')


def test_formula_lag_recovers_without_new_row(tmp_path: Path) -> None:
    class LagBackend(Backend):
        def create(self, fields: dict[str, Any], token: str) -> str:
            rid=super().create(fields,token)
            self.records[rid]['fields']['应付金额']=None
            return rid
    backend=LagBackend(); svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    assert '尚未一致' in send(svc,'2','提交')
    backend.records['rec1']['fields']['应付金额']=376
    assert '已保存' in send(svc,'3','提交')
    assert backend.creates==1


def test_amend_late_target_and_target_switch(tmp_path: Path) -> None:
    backend=Backend(); svc=LodgingService(tmp_path,'owner',backend)
    send(svc,'1','新开 日期=2026-09-25；晚数=2；单价=188')
    send(svc,'2','提交')
    send(svc,'3','修改 房号=306')
    send(svc,'4','补充 记录=rec1')
    assert '不能' in send(svc,'5','补充 记录=rec2')
    assert '已保存' in send(svc,'6','提交')
    assert backend.updates==1
