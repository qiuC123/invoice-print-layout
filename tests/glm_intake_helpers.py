"""Public intake helpers; all document content and identities are synthetic."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Callable, cast

import pymupdf
import pytest
from PIL import Image

from invoice_print_layout import intake_queue as module, mail_search
from invoice_print_layout.glm_invoice_ai import GlmInvoiceAI, GlmAIError
from invoice_print_layout.intake_queue import IntakeQueue
from invoice_print_layout.project_registry import ProjectRegistry
from invoice_print_layout.takeout import OcrLine
from invoice_print_layout.workbench import ExpenseStore


NUMBER = '1234567890' * 2  # Deliberately patterned synthetic invoice identity.


class FakeAI:
    enabled = True

    def __init__(self, result: dict[str, Any] | None = None,
                 callback: Callable[[], None] | None = None, fail: bool = False):
        self.result = result
        self.callback = callback
        self.fail = fail
        self.calls: list[tuple[str, list[bytes] | None]] = []

    def extract(self, text: str, images: list[bytes] | None = None) -> dict[str, Any]:
        self.calls.append((text, images))
        if self.callback:
            self.callback()
        if self.fail:
            raise GlmAIError('Synthetic client failure')
        return self.result if self.result is not None else response(text=text) if text.strip() else response()


def response(*, text: str = '支付成功\n商家名称：合成商店\n付款金额：40.00\n商品：晚餐',
             role: str = 'payment', fields: dict[str, str] | None = None,
             issues: list[str] | None = None) -> dict[str, Any]:
    values = fields if fields is not None else {'category': '餐饮', 'amount': '40.00',
        'merchant': '合成商店', 'payment_status': 'succeeded', 'goods': '晚餐'}
    return {'text': text, 'role': role, 'fields': values, 'issues': issues or [],
            'evidence': {key: text for key in values}, 'model': 'glm-5.3-flash'}


def picture() -> bytes:
    stream = io.BytesIO()
    Image.new('RGB', (120, 240), 'white').save(stream, format='PNG')
    return stream.getvalue()


def pdf(*, scanned: bool = False, pages: int = 1, amount: str = '14.00', seller: str = 'Synthetic Seller') -> bytes:
    with pymupdf.open() as document:
        for _ in range(pages):
            page = document.new_page()
            if not scanned:
                for index, text in enumerate(['电子发票（普通发票）', '发票号码：' + NUMBER,
                    'TOTAL WITH TAX:' + amount, 'SELLER: ' + seller, '商品：晚餐']):
                    page.insert_text((20, 40 + index * 30), text, fontname='china-s', fontsize=10)
        return document.tobytes()


def setup(tmp_path: Path, ai: FakeAI) -> tuple[IntakeQueue, str]:
    store = ExpenseStore(tmp_path)
    project = ProjectRegistry(tmp_path / '后勤' / 'tasks.sqlite3').create_project('Synthetic project')['id']
    return IntakeQueue(store, ai=cast(GlmInvoiceAI, ai)), project


def local_ocr(monkeypatch: pytest.MonkeyPatch, rows: list[str] | None = None, *, fail: bool = False) -> None:
    monkeypatch.setattr(mail_search, 'qr_value', lambda *_: '')
    def read(*_: Any) -> list[OcrLine]:
        if fail:
            raise RuntimeError('Synthetic OCR failure')
        return [OcrLine(value, 0, index * 30, 400, index * 30 + 20)
                for index, value in enumerate(rows or ['微信支付', '合成商店', '-40.00', '支付成功', '商品：晚餐'])]
    monkeypatch.setattr(module, 'read_receipt', read)


def update(queue: IntakeQueue, entry: dict[str, Any], **changes: Any) -> dict[str, Any]:
    data = {**entry, **changes}
    with queue.store.connect() as db:
        db.execute('UPDATE intake_queue SET data=? WHERE id=?', (json.dumps(data, ensure_ascii=False), data['id']))
    return data
