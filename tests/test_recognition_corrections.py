import json
import pytest
from invoice_print_layout.workbench import ExpenseStore
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench_web import classify_intake
from invoice_print_layout.recognition_corrections import summary
from tests.test_intake_review import image_bytes


def entry(store):
    queue=IntakeQueue(store)
    item=queue.receive('synthetic.png',image_bytes())
    item.update(fields={'title':'Synthetic','amount':'14','merchant':'合成商家','category':'材料采购'},
                field_sources={'title':'native_pdf','amount':'local_ocr','merchant':'glm','category':'tax_class'})
    with store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',(json.dumps(item),item['id']))
    return queue,item


def test_only_actual_changes_are_audited_and_protection_flags_unchanged(tmp_path):
    store=ExpenseStore(tmp_path);queue,old=entry(store)
    fields={**old['fields'],'amount':'14.00','merchant':'合成另一商家'}
    changed=classify_intake(queue,{'id':old['id'],'revision':old['revision'],'role':'invoice','fields':fields})
    assert changed['manual_fields']==sorted(fields)
    with store.connect() as db:
        rows=[dict(row) for row in db.execute('SELECT * FROM recognition_corrections')]
        assert len(rows)==1 and rows[0]['field']=='merchant' and rows[0]['source']=='glm'
        assert json.loads(rows[0]['old_value'])=='合成商家'
        assert json.loads(rows[0]['new_value'])=='合成另一商家'
        assert summary(db)[-1]['rate']<=1
    classify_intake(queue,{'id':old['id'],'revision':changed['revision'],'role':'invoice','fields':fields})
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM recognition_corrections').fetchone()[0]==1
    with pytest.raises(ValueError):
        classify_intake(queue,{'id':old['id'],'revision':0,'role':'invoice','fields':{'amount':'20'}})
    assert queue.get(old['id'])['manual_fields']==sorted(fields)
    current=queue.get(old['id'])
    classify_intake(queue,{'id':old['id'],'revision':current['revision'],'role':'invoice',
                          'fields':{'merchant':'合成第三商家'}})
    with store.connect() as db:
        assert db.execute('SELECT source FROM recognition_corrections ORDER BY id DESC LIMIT 1').fetchone()[0]=='manual'


def test_review_edit_retains_original_material_source(tmp_path):
    store=ExpenseStore(tmp_path);queue,material=entry(store)
    expense=store.create({'title':'Synthetic','category':'材料采购','amount':'14','merchant':'合成商家'})
    material.update(item_id=expense['id'],status='linked')
    with store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?',(json.dumps(material),material['id']))
    store.update(expense['id'],{'merchant':'合成另一商家'})
    with store.connect() as db:
        rows=[dict(row) for row in db.execute('SELECT * FROM recognition_corrections')]
        assert len(rows)==1 and rows[0]['material_id']==material['id'] and rows[0]['source']=='glm'
        rates=[r for r in summary(db) if r['field']=='merchant' and r['source']=='glm']
        assert rates==[{'field':'merchant','source':'glm','corrected':1,'observed':1,'rate':1}]
