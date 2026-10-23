"""Explicit, read-only selected-project export from the local invoice workbench.

The Android app imports the resulting hashed portable ZIP without a desktop server.
This command never POSTs, changes review/submission, or copies API tokens/credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import zipfile


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def package(state: dict[str, Any], people: dict[str, Any], queue: list[dict[str, Any]],
            project_ids: list[str], receive: Callable[[str], bytes], output: Path,
            template: Path | None = None) -> dict[str, int]:
    chosen = set(project_ids)
    if not chosen or len(chosen) != len(project_ids):
        raise ValueError('请选择不重复的项目 ID')
    projects = [p for p in state['projects'] if p['id'] in chosen]
    if len(projects) != len(chosen):
        raise ValueError('所选项目不存在')
    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in (
        'projects', 'people', 'expenses', 'materials', 'reports', 'experience')}
    files: dict[str, dict[str, Any]] = {}
    material_by_digest: dict[tuple[str, str], dict[str, Any]] = {}
    sources: dict[str, bytes] = {}
    for p in projects:
        tables['projects'].append({'id': p['id'], 'name': p['name'],
                                  'reimbursed': int(p.get('reimbursement_status') == 'reimbursed'),
                                  'pinned': int(bool(p.get('pinned'))), 'deleted': 0})
        snapshot = people.get(p['id'], {})
        for person in [*snapshot.get('people', []), *snapshot.get('retired_people', [])]:
            tables['people'].append({'id': person['id'], 'project_id': p['id'],
                                     'name': person.get('display_label') or person['label'],
                                     'active': int(person.get('state', 'active') == 'active')})

    def material(record: dict[str, Any], project: str, expense: str | None, url: str,
                 fields: dict[str, Any] | None = None, text: str = '') -> None:
        payload = receive(url)
        if not payload or len(payload) > 20 * 1024 * 1024:
            raise ValueError('原件为空或超过20MB')
        digest = hashlib.sha256(payload).hexdigest()
        if record.get('digest') and record['digest'] != digest:
            raise ValueError('原件校验值已变化，请先在电脑版核对')
        previous = material_by_digest.get((project, digest))
        if previous:
            if previous['expense_id'] and expense and previous['expense_id'] != expense:
                raise ValueError('同一原件关联多笔费用，请先核对再迁移')
            if previous['expense_id'] is None and expense:
                previous['expense_id'] = expense
            return
        pdf = payload.startswith(b'%PDF-')
        path = 'files/' + digest + ('.pdf' if pdf else '.image')
        sources[path] = payload
        files[path] = {'sha256': digest, 'size': len(payload)}
        values = dict(fields or {})
        if 'expense_date' in values:
            values['date'] = values.pop('expense_date')
        values['manual_fields'] = list(values)  # Historical choices remain protected.
        values['manual_role'] = bool(record.get('manual_role', True))
        row = {'id': 'desktop-' + record['id'], 'project_id': project, 'digest': digest,
               'name': record['name'], 'path': path, 'mime': 'application/pdf' if pdf else 'image/*',
               'role': record.get('role', 'other'), 'fields': json.dumps(values, ensure_ascii=False),
               'text': text, 'expense_id': expense,
               'status': {'archived': 'archived', 'deleted': 'deleted'}.get(record.get('status', ''), 'active'),
               'version': 1}
        material_by_digest[project, digest] = row
        tables['materials'].append(row)

    exported_ids: set[str] = set()
    for item in state['items']:
        project = item.get('project_id')
        if project not in chosen or item.get('merged_into') or item.get('returned_to_synthesis'):
            continue
        identifier = 'desktop-' + item['id']
        exported_ids.add(item['id'])
        data = {key: item.get(key, '') for key in ('title', 'merchant', 'note', 'category',
                'order_number', 'shopping_platform', 'shop_name', 'payment_channel')}
        owner_id = str(item.get('reimbursement_person_id') or '')
        data.update(owner=item.get('reimbursement_person_label') or '', owner_id=owner_id,
                    date=item.get('expense_date') or '', cents=item.get('amount_cents', 0),
                    basis=item.get('material_basis', 'category'),
                    missing=item.get('missing', []), missing_confirmed=bool(item.get('missing_acknowledged')),
                    desktop_source={'id': item['id'], 'version': item.get('version'),
                                    'stage': item.get('stage'), 'events': item.get('events', [])})
        choice = item.get('report_choice') or item.get('manual_report_choice') or {}
        for key in ('project_type', 'project_name'):
            if choice.get(key):
                data[key] = choice[key]
        if owner_id and not any(p['id'] == owner_id for p in tables['people']):
            raise ValueError('费用归属人不在人员快照中，请先核对人员合并关系')
        stage = ('submitted' if item.get('stage') in {'submitted', 'reimbursed'} else
                 'verified' if item.get('ready') else 'review')
        tables['expenses'].append({'id': identifier, 'project_id': project,
                                   'data': json.dumps(data, ensure_ascii=False), 'state': stage,
                                   'version': 1, 'deleted': int(bool(item.get('deleted')) or item.get('stage') == 'cancelled')})
        for attachment in item.get('attachments', []):
            material(attachment, project, identifier, '/file/' + quote(attachment['id'], safe=''))
    for entry in queue:
        project = entry.get('project_id')
        if project not in chosen or entry.get('item_id') in exported_ids:
            continue
        material(entry, project, None, '/intake-file/' + quote(entry['id'], safe=''),
                 entry.get('fields'), entry.get('text', ''))
    manifest: dict[str, Any] = {'format': 'invoice-assistant-portable', 'version': 1,
                               'tables': tables, 'files': files, 'source': 'desktop-read-only-export'}
    if template:
        payload = template.read_bytes()
        if len(payload) > 5 * 1024 * 1024:
            raise ValueError('模板超过5MB')
        manifest['template'] = 'files/template.xlsx'
        sources['files/template.xlsx'] = payload
        files['files/template.xlsx'] = {'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload)}
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open('xb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            for name, payload in sources.items():
                archive.writestr(name, payload)
    except FileExistsError:
        raise ValueError('目标文件已存在，不会覆盖') from None
    return {table: len(rows) for table, rows in tables.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--project-id', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--template', type=Path)
    args = parser.parse_args()
    address = urlsplit(args.base_url)
    if address.scheme != 'http' or address.hostname != '127.0.0.1' or address.username or address.password or address.path not in ('', '/') or address.query or address.fragment:
        parser.error('迁移源仅支持本机 127.0.0.1 的发票工作台')
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def receive(path: str) -> bytes:
        with opener.open(args.base_url.rstrip('/') + path, timeout=60) as response:
            payload = response.read(25 * 1024 * 1024 + 1)
        if len(payload) > 25 * 1024 * 1024:
            raise ValueError('返回文件过大')
        return payload

    state = json.loads(receive('/api/state'))
    if state.get('app_mode') != 'invoice':
        parser.error('迁移源不是发票独立工作台')
    people = {p: json.loads(receive('/api/reimbursement-people?project=' + quote(p, safe='')))
              for p in args.project_id}
    queue = json.loads(receive('/api/intake'))['queue']
    counts = package(state, people, queue, args.project_id, receive, args.output, args.template)
    print(json.dumps({'output': str(args.output.resolve()), 'counts': counts}, ensure_ascii=False))


if __name__ == '__main__':
    main()
