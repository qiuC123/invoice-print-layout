from pathlib import Path
import hashlib
import pytest

from invoice_print_layout.recognition_eval import copy_workspace, evaluate, score
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


def test_accuracy_and_coverage_have_distinct_denominators():
    result = score([{'type': 'screenshot', 'gold': {'merchant': '合成商店'}, 'predicted': {'merchant': '合成 商店'}},
                    {'type': 'screenshot', 'gold': {'merchant': '合成乙店'}, 'predicted': {}},
                    {'type': 'screenshot', 'gold': {'merchant': ''}, 'predicted': {'merchant': '无标准'}}])
    assert result['screenshot']['fields']['merchant'] == {'eligible': 2, 'extracted': 1, 'correct': 1, 'accuracy': 1, 'coverage': .5}


def test_copy_oracle_is_read_only_and_only_verified_unique_singletons_qualify(tmp_path: Path):
    source = tmp_path / 'source'
    store = ExpenseStore(source)
    for index, count in enumerate([1, 2, 1]):
        item = store.create({'title': 'Synthetic', 'category': '高铁', 'amount': '12.00'})
        for n in range(count):
            store.add_attachment(item['id'], f'{index}-{n}.pdf',
                make_invoice_pdf(tmp_path/f'{index}-{n}.pdf', amount='12.00', invoice_number=str(12345678901234567000+index*2+n)).read_bytes(), 'invoice')
        if index < 2:
            store.transition(item['id'], 'verify')
    digest = hashlib.sha256(store.database.read_bytes()).hexdigest()
    target = tmp_path / 'copy'
    copy_workspace(source, target)
    result = evaluate(target)
    assert result['samples'] == 1 and result['oracle_unchanged']
    assert result['rows'][0]['predicted']['amount'] == '12.00'
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == digest
    with pytest.raises(ValueError, match='只允许'):
        evaluate(source)
    with pytest.raises(ValueError):
        copy_workspace(source, source)
