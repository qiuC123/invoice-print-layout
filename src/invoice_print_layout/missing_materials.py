"""Explicit acknowledgements preserve missing-proof facts and their source snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING
from invoice_print_layout.file_cache import file_digest

if TYPE_CHECKING:
    from invoice_print_layout.workbench import ExpenseStore


_MATERIAL_GAPS = frozenset({
    '支付凭证', '发票', '行程单', '发票或收据', '付款截图', '购买明细／收据',
    '发票或微信／支付宝扣费记录', '12306发票', '订单记录或购买明细／收据',
    '订单记录', '运单明细',
})
_LIFECYCLE_FIELDS = frozenset({
    'verified', 'stage', 'updated_at', 'review_requested', 'report_files',
    'missing_material_acknowledgement',
})


def can_acknowledge_missing(item: dict[str, Any]) -> bool:
    missing = item.get('missing')
    return (not item.get('amount_pending') and isinstance(missing, list) and bool(missing)
            and all(isinstance(gap, str) and gap in _MATERIAL_GAPS for gap in missing))


def expense_scope(stored: dict[str, Any]) -> str:
    fields = {key: value for key, value in stored.items() if key not in _LIFECYCLE_FIELDS}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_snapshot(attachments: list[dict[str, Any]], workspace: Path) -> list[dict[str, Any]]:
    """Bind confirmation to every source, including unclassified or unavailable ones."""
    sources = []
    for attachment in sorted(attachments, key=lambda entry: str(entry['id'])):
        path = Path(attachment['path']).resolve()
        available = path.is_relative_to(workspace) and path.is_file()
        digest = None
        if available:
            try:
                digest = file_digest(path)
            except OSError:
                available = False
        sources.append({'id': attachment['id'], 'role': attachment['role'],
                        'available': available, 'sha256': digest})
    return sources


def acknowledgement_valid(stored: dict[str, Any], item: dict[str, Any],
                          sources: list[dict[str, Any]]) -> bool:
    acknowledgement = stored.get('missing_material_acknowledgement')
    return (stored.get('verified') is True and can_acknowledge_missing(item)
            and isinstance(acknowledgement, dict)
            and acknowledgement.get('missing') == item['missing']
            and acknowledgement.get('expense_scope') == expense_scope(stored)
            and acknowledgement.get('sources') == sources)


def acknowledged_missing_note(item: dict[str, Any]) -> str:
    return '人工核对完成；已确认仍缺：' + '、'.join(item['missing'])


def validate_acknowledged_sources(store: ExpenseStore, items: list[dict[str, Any]]) -> None:
    """An acknowledgement must still cover the exact sources being published."""
    for expected in items:
        if not expected.get('missing_acknowledged'):
            continue
        current = store.get(expected['id'])
        if (current['version'] != expected['version'] or not current['missing_acknowledged'] or
                not current['ready'] or current['stage'] != expected['stage']):
            raise ValueError('制作期间事项或缺失材料确认来源已变化，请刷新后重新核对')
