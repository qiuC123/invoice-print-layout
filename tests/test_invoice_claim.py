from pathlib import Path
import json

from invoice_print_layout import invoice_claim
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.workbench import ExpenseStore
from tests.helpers import make_invoice_pdf


def test_unknown_claim_link_stays_manual_without_network(tmp_path: Path, monkeypatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    monkeypatch.setattr(invoice_claim, 'request', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))
    result = invoice_claim.claim(queue, 'https://unknown.example/invoice')
    assert result['status'] == 'manual'
    assert not queue.list()


def test_verified_tax_endpoint_receives_pdf_not_qr(tmp_path: Path, monkeypatch) -> None:
    queue = IntakeQueue(ExpenseStore(tmp_path))
    pdf = make_invoice_pdf(tmp_path / 'fixture.pdf').read_bytes()
    calls = []
    def request(endpoint, values, **kwargs):
        calls.append(endpoint)
        if endpoint == 'queryFpjcxxb':
            assert values == {'Cs': '2_12345678901234567890_20260925120000ABCDE'}
            return json.dumps({'code': '1', 'fphm': '12345678901234567890', 'kprq': '2026-09-25 12:00:00', 'jym': 'fixture'}).encode()
        if endpoint == 'exportDzfpwjEwm':
            return pdf
        return b'{"code":"1"}'
    monkeypatch.setattr(invoice_claim, 'request', request)
    result = invoice_claim.claim(queue, 'https://dppt.shanghai.chinatax.gov.cn/v/2_12345678901234567890_20260925120000ABCDE')
    assert result['status'] == 'received'
    assert len(calls) == 4 and Path(result['entry']['path']).read_bytes() == pdf
    assert not queue.store.list_items()
