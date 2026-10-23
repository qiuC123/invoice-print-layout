"""Atomic return of selected submission-ready expenses to human review."""
from __future__ import annotations

import json
from typing import Any

from invoice_print_layout.expense_projects import assert_project_active, project_views
from invoice_print_layout.material_matching import MaterialMatching
from invoice_print_layout.workbench import ExpenseStore, now


def return_to_review(store: ExpenseStore, ids: Any, versions: Any,
                     project_id: Any) -> dict[str, Any]:
    """Invalidate review for one selection, retaining originals and old reports.

    The ready draft and historical-report cases mirror the submission page's
    membership. No expense, attachment, or report is deleted by this operation.
    Every version and ownership check happens before the first expense write.
    """
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 200 or
            any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids) or
            not isinstance(versions, dict) or set(versions) != set(ids) or
            any(not isinstance(value, str) or not value for value in versions.values())):
        raise ValueError('请选择1至200笔不重复费用，并提供每笔费用的完整当前版本')
    if not isinstance(project_id, str) or not project_id:
        raise ValueError('请先选择当前项目')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        assert_project_active(db, project_id)
        if not any(project['id'] == project_id for project in project_views(store, db=db)):
            raise ValueError('当前项目不存在，请重新选择')
        rows = db.execute('SELECT id,source,data FROM expenses WHERE id IN (' +
                          ','.join('?' for _ in ids) + ')', ids).fetchall()
        by_id = {row['id']: row for row in rows}
        if any(key not in by_id for key in ids):
            raise ValueError('费用不存在，请刷新后重新选择')
        ordered_rows = [by_id[key] for key in ids]
        items = store._read_items(db, ordered_rows)
        for item in items:
            if item.get('project_id') != project_id:
                raise ValueError('费用不属于当前项目，请重新选择')
            if item['version'] != versions[item['id']]:
                raise ValueError('费用或材料已变化，请刷新后重新选择')
            if (item.get('deleted') or item.get('merged_into') or item.get('returned_to_synthesis') or
                    item.get('synthesis_material_state') in {'archived', 'deleted'} or
                    not (item['stage'] == 'draft' and item['ready'] or
                         item['stage'] in {'submitted', 'reimbursed'})):
                raise ValueError('只能将当前项目可提交页面的费用退回待核对')
        timestamp = now()
        for item, row in zip(items, ordered_rows):
            MaterialMatching.preserve_edit_guard(db, item['id'])
            data = json.loads(row['data'])
            data.update(stage='draft', verified=False, review_requested=True, updated_at=timestamp)
            data.pop('missing_material_acknowledgement', None)
            db.execute('UPDATE expenses SET data=? WHERE id=?',
                       (json.dumps(data, ensure_ascii=False), item['id']))
            history_note = '；历史报销包保留' if data.get('report_files') else ''
            db.execute('INSERT INTO events(item_id,at,message) VALUES (?,?,?)',
                       (item['id'], timestamp, '批量退回待核对；撤销本次核对及缺材料确认，金额、归属及原件保留' + history_note))
    return {'items': store.get_many(ids), 'changed': len(ids),
            'message': f'已将{len(ids)}笔费用退回待核对；金额、费用归属及原件保留。'}
