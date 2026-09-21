from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_print_layout.takeout import (
    OrderReceipt,
    TakeoutError,
    TakeoutInvoice,
    pair_takeout,
)


def _invoice(path: Path, amount: str, number: str) -> TakeoutInvoice:
    return TakeoutInvoice(
        render_path=path,
        source_path=path,
        amount=Decimal(amount),
        invoice_date=date(2026, 8, 4),
        invoice_number=number,
        sha256=number,
        crop=None,
    )


def test_one_order_can_match_merchant_and_platform_invoices(tmp_path: Path) -> None:
    order = OrderReceipt(
        path=tmp_path / "order.jpg",
        order_number="1000000000000000001",
        merchant="测试咖啡店",
        paid_amount=Decimal("101.00"),
        sha256="order",
        crop=(0, 0, 100, 100),
    )
    invoices = [
        _invoice(tmp_path / "merchant.jpg", "100.00", "26000000000000000001"),
        _invoice(tmp_path / "platform.pdf", "1.00", "26000000000000000002"),
    ]

    groups = pair_takeout([order], invoices)

    assert len(groups) == 1
    assert [item.amount for item in groups[0].invoices] == [
        Decimal("100.00"),
        Decimal("1.00"),
    ]


def test_takeout_pairing_fails_closed_when_invoice_sum_differs(tmp_path: Path) -> None:
    order = OrderReceipt(
        path=tmp_path / "order.jpg",
        order_number="1000000000000000001",
        merchant="测试咖啡店",
        paid_amount=Decimal("101.00"),
        sha256="order",
        crop=(0, 0, 100, 100),
    )

    with pytest.raises(TakeoutError, match="金额"):
        pair_takeout(
            [order],
            [_invoice(tmp_path / "merchant.jpg", "100.00", "26000000000000000001")],
        )
