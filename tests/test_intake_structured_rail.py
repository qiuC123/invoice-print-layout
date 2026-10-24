"""The ordinary upload path shares explicit rail facts with the bounded Agent."""
import copy
from pathlib import Path
from typing import Any

import pytest

from invoice_print_layout.glm_invoice_ai import LocalOnlyInvoiceAI
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from tests.test_invoice_agent_content import columnar_rail_ticket


@pytest.mark.parametrize('refund,amount,title', [(False, '21.00', '铁路车票'), (True, '9.50', '铁路退票费')])
def test_normal_upload_rail_is_ready_locally_without_ai(tmp_path: Path, monkeypatch: Any,
        refund: bool, amount: str, title: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    # Enable the normal AI branch but prove complete rail fields bypass it.
    monkeypatch.setattr(queue.ai, 'enabled', True)
    monkeypatch.setattr(queue, '_apply_ai_analysis', lambda *a, **k: pytest.fail('native rail must not call AI'))
    payload = columnar_rail_ticket(refund=refund)
    entry = queue.receive('synthetic-rail.pdf', payload)
    result = queue.prepare_batch_entry(entry['id'])
    assert {k: result['fields'][k] for k in ('amount', 'category', 'merchant', 'title')} == {
        'amount': amount, 'category': '高铁', 'merchant': '中国铁路', 'title': title}
    assert len(result['fields']['invoice_number']) == 20
    assert result['role'] == 'invoice' and '金额冲突' not in result['issues']
    assert result['ai_analysis']['status'] == 'not_needed'
    assert result['field_sources']['amount'] == 'native_pdf'
    assert Path(result['path']).read_bytes() == payload
    assert queue.store.list_items() == []


@pytest.mark.parametrize('guard', ['manual-field', 'manual-role', 'manual-amount', 'different-amount', 'conflict'])
def test_rail_completion_never_overrides_human_or_conflicting_facts(tmp_path: Path, guard: str) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path), ai=LocalOnlyInvoiceAI())
    original = queue.receive('rail.pdf', columnar_rail_ticket(refund=False))
    original['fields'] = {'category': '其他', 'title': '待核对购买凭证'}
    if guard == 'manual-field':
        original['fields'].update(category='酒店', merchant='人工商家', title='人工用途')
        original['manual_fields'] = ['category', 'merchant', 'title']
    elif guard == 'manual-role':
        original.update(manual_role=True, role='receipt')
    elif guard == 'manual-amount':
        original['fields']['amount'] = '21.00'
        original['manual_fields'] = ['amount']
    elif guard == 'different-amount':
        original['fields']['amount'] = '22.00'
    else:
        original['field_conflicts'] = [{'field': 'amount'}]
    data = copy.deepcopy(original)
    data.update(text='电子发票（铁路电子客票）\n发票号码：26310000000000000001\n电子客票号：12345678901234567890\n票价：￥21.00', issues=['金额冲突'])
    queue._complete_local_special_fields(data, original, 'native_pdf')
    if guard == 'manual-field':
        assert {k: data['fields'][k] for k in ('category', 'merchant', 'title')} == {
            'category': '酒店', 'merchant': '人工商家', 'title': '人工用途'}
        assert data['fields']['amount'] == '21.00'
    else:
        assert data['fields'] == original['fields']
        assert data['role'] == original['role'] and '金额冲突' in data['issues']
