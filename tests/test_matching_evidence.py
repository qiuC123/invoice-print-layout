from __future__ import annotations

import pytest

from invoice_print_layout.matching_evidence import labelled_transaction_time, matching_context


@pytest.mark.parametrize('value', [
    '2026-02-30 16:23:47', '2026-09-30 25:23:47', '2026-09-30 16:63:47',
    '2026-09-30 16:23:47.1234567', '2026-09-30 16:23:47+08:00', '2026-09-30T16:23:47Z',
    '2026-09-30 16:23:47 UTC', '2026-09-30 16:23:47 UTC+08:00', '2026-09-30 16:23:47 GMT',
])
def test_invalid_or_zoned_source_time_cannot_become_local_evidence(value: str) -> None:
    assert labelled_transaction_time('支付时间：' + value, 'payment')['value'] == ''


def test_distinct_payment_times_require_review_instead_of_choosing_first() -> None:
    evidence = labelled_transaction_time('支付时间：2026-09-30 16:23:47\n付款时间：2026-09-30 17:00:00', 'payment')
    assert evidence['ambiguous'] and not evidence['value']


def test_matching_context_ignores_even_preparsed_transaction_timestamps() -> None:
    order = {'transaction_time': labelled_transaction_time('下单时间\n2026年9月30日16:23', 'receipt')}
    payment = {'transaction_time': labelled_transaction_time('支付时间：2026-09-30 16:23:47', 'payment')}
    context = matching_context([order], [payment], ['yes'])
    assert context['shop'] == 'matched'
    assert 'time' not in context and 'time_gap_seconds' not in context
    assert any('店铺' in check for check in context['checks'])
    assert any('金额' in check for check in context['checks'])
    assert not any(marker in check for check in context['checks'] for marker in ('时间', '分钟', '秒'))
