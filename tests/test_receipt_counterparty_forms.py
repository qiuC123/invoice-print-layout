import pytest

from invoice_print_layout.receipt_intake import labelled_counterparty


@pytest.mark.parametrize('label', ['收款人（盖章）：——', '收款方（盖章）:开票人：收款人',
                                  '收款人（签字）：___'])
def test_blank_signature_form_is_not_a_second_merchant(label: str) -> None:
    value, quote, conflict = labelled_counterparty(['收款方：示例广告店', label])
    assert value == '示例广告店' and quote == '收款方：示例广告店' and not conflict


def test_two_real_collectors_still_conflict() -> None:
    assert labelled_counterparty(['收款方：示例广告店', '收款人：另一收款方']) == ('', '', True)
