"""Evidence inference, weak-payment questions and same-job continuation boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import pytest

from invoice_print_layout.agent_jobs import AgentLoop, Jobs
from invoice_print_layout.agent_tools import InvoiceTools
from invoice_print_layout.agent_relations import candidates, similar_views
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.workbench import ExpenseStore
from tests.test_invoice_agent_combinations import entry, invoice_text


@pytest.fixture
def relation_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Jobs, InvoiceTools, str]:
    store=ExpenseStore(tmp_path)
    project=ProjectRegistry(tmp_path/'后勤/tasks.sqlite3').create_project('Semantic fixture')['id']
    jobs=Jobs(store)
    tools=InvoiceTools(jobs)
    monkeypatch.setattr(tools.relations.ai,'public_status',lambda: {'available':True,'enabled':True,'model':'fixture'})
    from invoice_print_layout import report_print_preview
    def preview(store: ExpenseStore, ids: list[str], options: dict[str,Any]) -> dict[str,Any]:
        return {'total_cents':sum(store.get(i)['amount_cents'] for i in ids),'page_count':1,
                'pdf':'fixture.pdf','xlsx':'fixture.xlsx','preview_pages':[]}
    monkeypatch.setattr(report_print_preview,'create_report_print_preview',preview)
    return jobs,tools,project


def weak_pair(env: tuple[Jobs,InvoiceTools,str]) -> list[dict[str,Any]]:
    invoice=entry(env,'invoice','29.60',invoice_text('饮用水'),category='餐饮',shop='合成饮料销售方')
    payment=entry(env,'payment','29.60','支付成功\n商品 美团订单',shop='平台聚合商户')
    return [invoice,payment]


def supported_pair(env: tuple[Jobs,InvoiceTools,str]) -> list[dict[str,Any]]:
    """Concrete shared goods admit bounded semantic review across shop roles."""
    invoice=entry(env,'invoice','17.00',invoice_text('鼠标'),category='材料采购',shop='合成用品开票方')
    payment=entry(env,'payment','17.00','支付成功\n商品名称：鼠标',category='材料采购',shop='合成工具门店')
    return [invoice,payment]


def model_answer(payload: str, *, status: str='supported') -> dict[str,Any]:
    request=json.loads(payload)
    facts={m['id']:m for m in request['materials']}
    result=[]
    for candidate in request['candidates']:
        members=[k.removeprefix('q:') for k in candidate['voucher_keys']]+candidate['payment_ids']+candidate['support_ids']
        evidence=[]
        for ident in members:
            fact=facts[ident]
            quote=(fact['goods'][0]['name'] if fact['goods'] else fact['text'].splitlines()[0])
            evidence.append({'material_id':ident,'quote':quote,'supports':'goods'})
        result.append({'candidate_id':candidate['id'],'decision':status,'evidence':evidence,'reason':'仅作未核对候选'})
    return {'decisions':result}


def test_generic_platform_payment_asks_missing_fact_without_calling_model(
        relation_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs,tools,project=relation_env
    rows=weak_pair(relation_env)
    calls=[]
    def request(rules: str,payload: str,*,max_tokens: int) -> dict[str,Any]:
        calls.append(payload)
        return model_answer(payload)
    monkeypatch.setattr(tools.relations.ai,'_request',request)
    job=jobs.create(project,[e['id'] for e in rows],'weak-platform',use_model=True)
    waiting=AgentLoop(jobs).run(job['id'],tools)
    assert waiting['state']=='needs_input' and waiting['models']==0 and len(calls)==0
    assert not tools.store.list_items()
    assert waiting['question']['type']=='material_relation'
    assert {r['digest'] for r in waiting['question']['material_refs']}=={e['digest'] for e in rows}
    assert len([c for c in waiting['question']['choices'] if c['action']=='confirm_relation'])==1


def test_user_relation_fact_continues_to_preview_without_model_or_budget_reset(
        relation_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs,tools,project=relation_env
    rows=weak_pair(relation_env)
    monkeypatch.setattr(tools.relations.ai,'_request',lambda rules,payload,**kw:model_answer(payload))
    job=jobs.create(project,[e['id'] for e in rows],'user-relation',use_model=True)
    waiting=AgentLoop(jobs).run(job['id'],tools)
    assert waiting['models']==0
    choice=next(c for c in waiting['question']['choices'] if c['action']=='confirm_relation')
    resumed=jobs.answer(job['id'],choice['id'],waiting['question']['id'])
    assert resumed['models']==waiting['models'] and resumed['tools']==waiting['tools']
    assert resumed['elapsed']>=waiting['elapsed']
    assert resumed['limits']==waiting['limits']
    monkeypatch.setattr(tools.relations.ai,'_request',lambda *args,**kwargs:pytest.fail('Answer must not need another model call'))
    done=AgentLoop(jobs).run(job['id'],tools)
    assert done['state']=='ready' and done['models']==waiting['models']
    assert done['preview']['total_cents']==2960
    item=tools.store.list_items()[0]
    assert item['verified'] is False and item['stage']=='draft'
    assert len(item['attachments'])==2 and item['agent_relation']['relation_kind']=='user_confirmed'
    assert len(done['confirmations'])==1 and done['confirmations'][0]['actor']=='user'


def test_declining_relation_stops_without_writes_or_repeat_question(relation_env: Any) -> None:
    jobs,tools,project=relation_env
    rows=weak_pair(relation_env)
    job=jobs.create(project,[e['id'] for e in rows],'decline-relation')
    waiting=AgentLoop(jobs).run(job['id'],tools)
    decline=next(c for c in waiting['question']['choices'] if c['action']=='not_same_expense')
    done=jobs.answer(job['id'],decline['id'],waiting['question']['id'])
    assert done['state']=='stopped' and done['question'] is None and done['models']==0
    assert not tools.store.list_items()
    with pytest.raises(ValueError,match='新增依据'):
        jobs.retry(job['id'])


@pytest.mark.parametrize('mutation',['unknown_candidate','fabricated_quote','foreign_material'])
def test_model_cannot_invent_identity_or_evidence(relation_env: Any,monkeypatch: pytest.MonkeyPatch,mutation: str) -> None:
    jobs,tools,project=relation_env
    rows=supported_pair(relation_env)
    job=jobs.create(project,[e['id'] for e in rows],'bad-model-'+mutation,use_model=True)
    job=jobs.update(job['id'],state='running',run_started=time.time())
    scan=tools._scan(job)
    def request(rules: str,payload: str,**kwargs: Any) -> dict[str,Any]:
        answer=model_answer(payload)
        decision=answer['decisions'][0]
        if mutation=='unknown_candidate':
            decision['candidate_id']='outside-candidate'
        elif mutation=='foreign_material':
            decision['evidence'][0]['material_id']='outside-source'
        else:
            decision['evidence'][0]['quote']='原件从未出现的商品'
        return answer
    monkeypatch.setattr(tools.relations.ai,'_request',request)
    with pytest.raises(ValueError):
        tools.execute('investigate_relations',{},job)
    assert jobs.get(job['id'])['models']==1 and not tools.store.list_items()
    assert not tools.relations.scan(job,scan['fingerprint'],rows,
        {e['id']:{'name_conflict':False} for e in rows})['proposals']


def test_distinct_specific_goods_cannot_be_confirmed_as_a_same_amount_group(
        relation_env: Any) -> None:
    _,tools,_=relation_env
    invoice=entry(relation_env,'invoice','17.00',invoice_text('雨衣'),category='材料采购',shop='合成用品开票方')
    order=entry(relation_env,'purchase','17.00','商品名称：鼠标\n实付17.00',category='材料采购',shop='合成工具门店')
    rows=[invoice,order]
    facts=tools.relations.facts(rows)
    assert candidates(rows,{e['id']:{'name_conflict':False} for e in rows},facts)==[]


def test_missing_and_incomplete_proofs_never_become_answerable_candidates(relation_env: Any) -> None:
    _,tools,_=relation_env
    invoice=entry(relation_env,'invoice','29.60','电子发票\n发票号码：12345678901234567890\n商品名称：饮用水',
                  category='餐饮',shop='合成商家')
    payment=entry(relation_env,'payment','29.60','支付成功\n商品 平台订单',shop='合成支付商家')
    rows=[invoice,payment]
    facts=tools.relations.facts(rows)
    assert not facts[invoice['id']]['complete']
    assert candidates(rows,{e['id']:{'name_conflict':False} for e in rows},facts)==[]


def test_payment_views_are_never_deduplicated_from_same_amount_or_time() -> None:
    rows=[{'id':str(i),'role':'payment','text':'同一宾馆\n-154.00\n支付时间12:00:00'} for i in range(2)]
    facts={e['id']:{'goods':[{'name':'住宿'}],'total_cents':15400} for e in rows}
    assert not similar_views(rows[0],rows[1],facts)


def test_unknown_amount_competitor_blocks_inference(relation_env: Any) -> None:
    _,tools,_=relation_env
    rows=weak_pair(relation_env)
    broken=entry(relation_env,'payment','1','支付成功但金额裁掉',shop='合成收款方')
    broken['fields']['amount']=''
    rows.append(broken)
    facts=tools.relations.facts(rows)
    assert candidates(rows,{e['id']:{'name_conflict':False} for e in rows[:2]},facts)==[]


def test_product_and_delivery_split_reaches_preview_without_question(
        relation_env: Any,monkeypatch: pytest.MonkeyPatch) -> None:
    jobs,tools,project=relation_env
    order=entry(relation_env,'order','58.30','商品名称：茶饮料\n共6件\n订单已送达\n实付58.30',
                category='外卖',shop='青山饮品店')
    goods=entry(relation_env,'invoice','56.20',invoice_text('茶饮料'),category='其他',shop='合成餐饮开票公司')
    delivery=entry(relation_env,'invoice','2.10',invoice_text('物流网络配送费','12345678901234567891'),
                   category='其他',shop='合成配送服务公司')
    monkeypatch.setattr(tools.relations.ai,'_request',lambda rules,payload,**kw:model_answer(payload))
    job=jobs.create(project,[e['id'] for e in [order,goods,delivery]],'split-food',use_model=True)
    done=AgentLoop(jobs).run(job['id'],tools)
    assert done['state']=='ready' and done['models']==1 and done['question'] is None
    item=tools.store.list_items()[0]
    assert item['amount_cents']==5830 and len(item['attachments'])==3 and not item['verified']
    assert item['agent_relation']['relation_kind']=='model_inference'
    assert item['category'] in {'餐饮','外卖'} and '茶饮' in item['title']


def test_shared_distinctive_hotel_name_is_candidate_not_merchant_alias(
        relation_env: Any,monkeypatch: pytest.MonkeyPatch) -> None:
    jobs,tools,project=relation_env
    invoice=entry(relation_env,'invoice','300',invoice_text('住宿服务')+'\n销售方名称：青山春雨宾馆',
                  category='酒店',shop='青山春雨宾馆')
    payments=[entry(relation_env,'payment',str(amount),'支付成功\n商品说明 车站春雨宾馆',shop='合成旅行平台')
              for amount in [100,200]]
    monkeypatch.setattr(tools.relations.ai,'_request',lambda rules,payload,**kw:model_answer(payload))
    job=jobs.create(project,[e['id'] for e in [invoice,*payments]],'hotel-platform',use_model=True)
    done=AgentLoop(jobs).run(job['id'],tools)
    assert done['state']=='ready' and done['models']==1, {
        'message':done['message'],'question':done.get('question'),
        'results':[(r['tool'],r['result'].get('tool_error')) for r in done['last_results']]}
    item=tools.store.list_items()[0]
    assert item['amount_cents']==30000 and len(item['attachments'])==3
    assert item['category']=='酒店' and not item['verified']
    assert item['agent_relation']['relation_kind']=='model_inference'


def test_two_distinct_concrete_orders_remain_competing_even_when_model_supports_both(
        relation_env: Any,monkeypatch: pytest.MonkeyPatch) -> None:
    jobs,tools,project=relation_env
    invoice=entry(relation_env,'invoice','17',invoice_text('鼠标'),category='材料采购',shop='合成开票公司')
    orders=[entry(relation_env,'purchase','17','商品名称：鼠标\n颜色：'+color+'\n实付17',
                  category='材料采购',shop='合成工具店') for color in ['黑色','白色']]
    monkeypatch.setattr(tools.relations.ai,'_request',lambda rules,payload,**kw:model_answer(payload))
    job=jobs.create(project,[e['id'] for e in [invoice,*orders]],'competing-semantic',use_model=True)
    done=AgentLoop(jobs).run(job['id'],tools)
    assert done['state']=='needs_input' and not tools.store.list_items()
    assert len([c for c in done['question']['choices'] if c['action']=='confirm_relation'])==2


def test_explicit_payment_product_conflict_has_no_confirmation_option(relation_env: Any) -> None:
    jobs,tools,project=relation_env
    invoice=entry(relation_env,'invoice','29.60',invoice_text('饮用水'),category='餐饮',shop='合成供应商')
    payment=entry(relation_env,'payment','29.60','支付成功\n商品名称：奶茶',category='餐饮',shop='合成饮品商家')
    job=jobs.create(project,[invoice['id'],payment['id']],'different-food')
    waiting=AgentLoop(jobs).run(job['id'],tools)
    assert waiting['state']=='needs_input' and not tools.store.list_items()
    assert not waiting['question']['choices']


def test_unreadable_native_page_limit_cannot_be_overridden_by_relation_answer(
        relation_env: Any) -> None:
    import hashlib
    import pymupdf
    jobs,tools,project=relation_env
    rows=weak_pair(relation_env)
    invoice=rows[0]
    with pymupdf.open(invoice['path']) as pdf:
        for _ in range(10):
            pdf.new_page()
        data=pdf.tobytes()
    Path(invoice['path']).write_bytes(data)
    invoice['digest']=hashlib.sha256(data).hexdigest()
    with tools.store.connect() as db:
        tools.matching._write_queue(db,invoice)
    job=jobs.create(project,[e['id'] for e in rows],'unreadable-pages')
    waiting=AgentLoop(jobs).run(job['id'],tools)
    assert waiting['state']=='needs_input' and not tools.store.list_items()
    assert not waiting['question']['choices']


def test_bad_model_evidence_asks_fact_once_instead_of_stalling(
        relation_env: Any,monkeypatch: pytest.MonkeyPatch) -> None:
    jobs,tools,project=relation_env
    rows=supported_pair(relation_env)
    count=[]
    def request(rules: str,payload: str,**kwargs: Any) -> dict[str,Any]:
        count.append(1)
        answer=model_answer(payload)
        answer['decisions'][0]['evidence'][0]['quote']='虚构的关联证据'
        return answer
    monkeypatch.setattr(tools.relations.ai,'_request',request)
    job=jobs.create(project,[e['id'] for e in rows],'bad-evidence-loop',use_model=True)
    waiting=AgentLoop(jobs).run(job['id'],tools)
    assert waiting['state']=='needs_input' and waiting['question']['type']=='material_relation'
    assert waiting['models']==1 and len(count)==1
    assert len([r for r in waiting['last_results'] if r['tool']=='investigate_relations'])==1
    assert not tools.store.list_items()


def test_invalid_json_from_relation_model_keeps_answerable_question(
        relation_env: Any,monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_print_layout.glm_invoice_ai import GlmAIError
    jobs,tools,project=relation_env
    rows=supported_pair(relation_env)
    def broken(*args: Any,**kwargs: Any) -> dict[str,Any]:
        raise GlmAIError('',failure_code='json_invalid')
    monkeypatch.setattr(tools.relations.ai,'_request',broken)
    job=jobs.create(project,[e['id'] for e in rows],'invalid-json-relation',use_model=True)
    waiting=AgentLoop(jobs).run(job['id'],tools)
    assert waiting['state']=='needs_input' and waiting['question']['choices']
    assert waiting['models']==1 and not tools.store.list_items()


@pytest.mark.parametrize('action', [
    {'tool':'ask_user','arguments':{},'reason':'extra field'},
    {'tool':'shell','arguments':{'command':'unsafe'}},
    {'tool':'ask_user'},
    {'tool':'read_material','arguments':'wrong type'},
    {'tool':'ask_user','arguments':{'question':'x'*2001}},
    {'tool':'ask_user','arguments':{'untrusted_instruction':'invent a fee'}},
])
def test_invalid_planner_protocol_asks_missing_fact_once(
        relation_env: Any, monkeypatch: pytest.MonkeyPatch, action: dict[str,Any]) -> None:
    from invoice_print_layout.agent_tools import ModelPlanner
    jobs,tools,project=relation_env
    invoice=entry(relation_env,'invoice','40.50',invoice_text('超市服务'),shop='合成超市')
    payment=entry(relation_env,'payment','40.50','支付成功\n商品 散单运费-合成快递',shop='合成快递')
    planner=ModelPlanner(tools.store.root)
    monkeypatch.setattr(planner.ai,'public_status',lambda:{'available':True})
    calls=[]
    def choose(*args: Any,**kwargs: Any) -> dict[str,Any]:
        calls.append(1)
        return action
    monkeypatch.setattr(planner.ai,'_request',choose)
    job=jobs.create(project,[invoice['id'],payment['id']],'bad-planner-protocol',use_model=True)
    waiting=AgentLoop(jobs).run(job['id'],tools,planner)
    assert waiting['state']=='needs_input' and waiting['models']==1 and len(calls)==1
    assert waiting['question']['choices']==[] and not tools.store.list_items()
    assert '商品' in waiting['question']['text'] or '购买' in waiting['question']['text']


def test_old_relation_approval_cannot_fill_unknown_supermarket_goods(relation_env: Any) -> None:
    jobs,tools,project=relation_env
    invoice=entry(relation_env,'invoice','40.50',invoice_text('超市服务'),shop='合成超市')
    payment=entry(relation_env,'payment','40.50','支付成功\n商品 散单运费-合成快递',shop='合成快递')
    job=jobs.create(project,[invoice['id'],payment['id']],'old-unknown-goods')
    scan=tools._scan(job)
    assert scan['choices']==[] and scan['proposals']==[]
    choice={'id':'old-choice','voucher_keys':['q:'+invoice['id']],'payment_ids':[payment['id']]}
    job=jobs.update(job['id'],state='running',run_started=time.time(),
                    approval={'snapshot':scan['fingerprint'],'choice':choice['id'],'choice_data':choice})
    with pytest.raises(ValueError):
        tools.execute('validate_group',{'voucher_keys':choice['voucher_keys'],
                                       'payment_ids':choice['payment_ids']},job)
    assert not tools.store.list_items()
