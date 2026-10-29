"""Public matching helpers using synthetic data and a local AI test double."""
from __future__ import annotations

from typing import Any

from invoice_print_layout.material_matching import MaterialMatching
from tests.test_material_matching import queued


class FakeAI:
    def __init__(self, *, available: bool = True):
        self.enabled = True
        self.available = available
        self.calls: list[Any] = []

    def public_status(self) -> dict[str, Any]:
        return {'enabled': True, 'available': self.available, 'model': 'glm-5.3-flash'}

    def decide(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        raise AssertionError('Rules matching must never call cloud decision')


def confirmed(matcher: MaterialMatching, entry: dict[str, Any], name: str = '合成印务有限公司') -> dict[str, Any]:
    entry['fields']['merchant'] = name
    entry['text'] = str(entry.get('text') or '') + '\n商家名称：' + name
    with matcher.store.connect() as db:
        matcher._write_queue(db, entry)
    return entry


def payment(matcher: MaterialMatching, project: str, title: str, amount: str,
            *, status: str = 'succeeded', text: str = '支付成功', name: str = '合成印务有限公司',
            **extra: Any) -> dict[str, Any]:
    entry = queued(matcher, project, title, 'payment', amount, text=text, **extra)
    entry['fields']['payment_status'] = status
    return confirmed(matcher, entry, name)


def activate(matcher: MaterialMatching, *, available: bool = True) -> FakeAI:
    ai = FakeAI(available=available)
    matcher.ai = ai
    return ai
