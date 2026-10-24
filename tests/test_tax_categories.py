import json
import threading
import pymupdf
import pytest
from invoice_print_layout.tax_categories import classify_tax
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.automatic_category import AutomaticCategory
from invoice_print_layout.expense_preferences import Preferences
from invoice_print_layout.workbench_web import classify_intake

@pytest.mark.parametrize('text,category', [('*住宿服务*住宿费','酒店'),('*铁路客运*车票','高铁'),
 ('＊五金＊螺丝\n*文具用品*笔','材料采购'),('*餐饮服务*午餐',''),('*五金*鼠标\n*食品*蛋糕',''),
 ('*五金*安装锁服务费',''),('*五金*租赁设备',''),('*未知*用品','')])
def test_conservative_tax_rules(text,category):
    assert classify_tax(text)['category']==category


@pytest.mark.parametrize('manual', [False, True, 'experience'])
def test_native_intake_rule_never_overrides_manual_fields(tmp_path,manual):
    with pymupdf.open() as doc:
        p=doc.new_page()
        for i,text in enumerate(['电子发票（普通发票）','发票号码：12345678901234567890','价税合计：14.00','*五金*螺丝']):
            p.insert_text((20,40+i*30),text,fontname='china-s',fontsize=10)
        payload=doc.tobytes()
    queue=IntakeQueue(ExpenseStore(tmp_path),ai=LocalOnlyInvoiceAI())
    entry=queue.receive('synthetic.pdf',payload)
    if manual == 'experience':
        entry['fields']={'category':'酒店'};entry['field_sources']={'category':'experience'}
        with queue.store.connect() as db:
            db.execute('UPDATE intake_queue SET data=? WHERE id=?',(json.dumps(entry),entry['id']))
    elif manual:
        classify_intake(queue,{'id':entry['id'],'revision':entry['revision'],'role':'invoice','fields':{'category':'酒店'}})
    parsed=queue.prepare_batch_entry(entry['id'])
    assert parsed['fields']['category']==('酒店' if manual else '材料采购')
    if manual == 'experience':
        assert parsed['field_sources']['category']=='experience'


def test_explicit_experience_precedes_tax_class(tmp_path):
    from invoice_print_layout.tax_categories import human_category
    from invoice_print_layout import expense_classifier as classifier
    from invoice_print_layout.automatic_category import digest
    store=ExpenseStore(tmp_path)
    AutomaticCategory(store,threading.Lock())
    evidence={'purpose':'合成用途','goods':'*五金*螺丝'}
    key=digest([evidence,classifier.MODEL,classifier.CRITERIA,classifier.RULES])
    with store.connect() as db:
        db.execute('INSERT INTO category_experience VALUES (?,?,?,0)',(key,'酒店','human'))
    # Normalization lowercases English but leaves this synthetic Chinese basis unchanged.
    assert human_category(store,{'id':'entry'},{'note':'合成用途'},'*五金*螺丝')==('酒店',True)
    Preferences(store).save({'kind':'category','scope':'exact_content','match':'*五金*螺丝','purpose':'合成用途','value':'餐饮'})
    assert human_category(store,{'id':'entry'},{'note':'合成用途'},'*五金*螺丝')==('',True)
