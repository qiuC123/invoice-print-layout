import base64
from pathlib import Path
import re
from urllib.parse import urlencode

from cli_anything.invoice_assistant.utils.client import CLIError, digest

EXPENSE_FIELDS = ('id', 'project_id', 'title', 'expense_date', 'amount', 'amount_cents', 'amount_pending', 'category',
                  'merchant', 'shopping_platform', 'shop_name', 'payment_channel', 'project_type', 'project_name',
                  'report_choice', 'manual_report_choice', 'verified', 'stage', 'version', 'missing', 'complete',
                  'ready', 'pending_review', 'returned_to_synthesis', 'deleted', 'reimbursement_person_id', 'reimbursement_person_label', 'report_files')
MATERIAL_FIELDS = ('id', 'digest', 'name', 'project_id', 'status', 'revision', 'item_id', 'fields', 'role', 'issues',
                   'field_sources', 'result', 'reimbursement_person_id', 'reimbursement_person_label', 'material_match_id')


def pick(value, keys): return {k: value[k] for k in keys if k in value}


def material_view(value):
    result = pick(value, MATERIAL_FIELDS)
    ai = value.get('ai_analysis') or {}
    result['ai_analysis'] = pick(ai, ('status', 'model', 'message', 'revision', 'source_sha256', 'input_sha256', 'failure_code'))
    return result


def expense_view(value, state):
    return {**pick(value, EXPENSE_FIELDS), 'classification': state.get('classification', {}).get(value['id'], {})}


def material_rows(client, project_id):
    client.project(project_id)
    return [row for row in client.get('/api/intake')['queue'] if row.get('project_id') == project_id and row.get('status') not in {'deleted', 'archived', 'unused'}]


def expense_rows(client, project_id):
    client.project(project_id)
    return [r for r in client.state()['items'] if r.get('project_id') == project_id and not r.get('deleted')]


def choose(rows, ids):
    mapping = {r['id']: r for r in rows}
    if not ids or len(ids) > 200 or len(set(ids)) != len(ids): raise CLIError('请选择1–200个不重复的ID。')
    if any(key not in mapping for key in ids): raise CLIError('记录不在当前项目或已删除/归档。', 'PROJECT_BOUNDARY', 4)
    return [mapping[key] for key in ids]


def summary(rows):
    active = [r for r in rows if not r.get('returned_to_synthesis')]
    return {'expenses': len(active), 'known_total_cents': sum(r.get('amount_cents', 0) for r in active if not r.get('amount_pending')),
            'amount_pending': sum(bool(r.get('amount_pending')) for r in active),
            'pending_review': sum(bool(r.get('pending_review')) for r in active),
            'submitted': sum(r.get('stage') == 'submitted' for r in active),
            'verified': sum(bool(r.get('verified')) for r in active),
            'manual_report_choices': sum(bool(r.get('manual_report_choice')) for r in active)}


def inspect_project(client, project_id):
    project = client.project(project_id)
    expenses = expense_rows(client, project_id); materials = material_rows(client, project_id)
    return {'project': pick(project, ('id', 'name', 'revision', 'reimbursement_status', 'reimbursement_person')),
            'summary': {**summary(expenses), 'materials': len(materials), 'recognized_materials': sum(saved_material(r) for r in materials)},
            'reused': True, 'status': 'complete'}


def saved_material(row): return bool(row.get('fields')) and row.get('status') != 'received'


def prepare(client, project_id, ids, recognize_missing=False, dry_run=False):
    rows = choose(material_rows(client, project_id), ids)
    missing = [r['id'] for r in rows if not saved_material(r)]
    entries = []
    for row in rows:
        if saved_material(row): entries.append({'entry': material_view(row), 'reused': True})
        elif recognize_missing and not dry_run:
            result = client.post('/api/intake/prepare', {'id': row['id'], 'force_ai': False})
            fresh = result.get('entry', {})
            if fresh.get('project_id') != project_id: raise CLIError('原服务返回了另一项目材料。', 'PROJECT_BOUNDARY', 4)
            entries.append({'entry': material_view(fresh), 'reused': False})
        else: entries.append({'entry': material_view(row), 'reused': False, 'needs_recognition': True})
    remaining = [r['entry']['id'] for r in entries if not saved_material(r['entry'])]
    return {'entries': entries, 'needs_recognition': remaining, 'initially_missing': missing,
            'status': 'partial' if remaining else 'complete',
            'reused': not missing, 'would_recognize': missing if recognize_missing and dry_run else [],
            **({'dry_run': True} if dry_run else {})}


def classify(client, project_id, ids, classify_missing=False, dry_run=False):
    rows = choose(expense_rows(client, project_id), ids)
    state = client.state(); answers = []; missing = []
    for row in rows:
        saved = state.get('classification', {}).get(row['id'], {})
        protected = bool(row.get('manual_report_choice')) or row.get('verified') or row.get('stage') == 'submitted'
        if protected or (saved and not saved.get('pending')):
            answers.append({'id': row['id'], 'category': row.get('category'), 'classification': saved,
                            'manual_report_choice': row.get('manual_report_choice'), 'protected': bool(protected), 'reused': True})
        elif classify_missing and not dry_run:
            result = client.post('/api/category/auto', {'id': row['id'], 'retry': False})
            answers.append({'id': row['id'], 'classification': result, 'reused': False})
        else:
            missing.append(row['id']); answers.append({'id': row['id'], 'classification': saved, 'reused': False, 'needs_classification': True})
    unresolved = [a['id'] for a in answers if a.get('needs_classification') or
                  (not a.get('protected') and a.get('classification', {}).get('status') in {'error', 'needs_info', 'stale'})]
    return {'items': answers, 'needs_classification': missing, 'unresolved_classification': unresolved,
            'would_classify': missing if classify_missing and dry_run else [], 'status': 'partial' if unresolved else 'complete',
            'reused': all(a['reused'] for a in answers), **({'dry_run': True} if dry_run else {})}


def receive(client, project_id, file, dry_run=False):
    client.project(project_id)
    file = Path(file).resolve()
    if not file.is_file() or file.suffix.lower() not in {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.bmp'}:
        raise CLIError('请选择原工具支持的PDF或图片。')
    if file.stat().st_size > 20 * 1024 * 1024: raise CLIError('单个原件超过20MiB，请用原工具处理。')
    content = file.read_bytes(); sha = digest(content)
    existing = [r for r in client.get('/api/intake')['queue'] if r.get('digest') == sha]
    if existing:
        if any(r.get('project_id') != project_id for r in existing):
            raise CLIError('相同原件已有另一项目归属；不自动移入或重收。', 'PROJECT_BOUNDARY', 4)
        return {'entry': material_view(existing[0]), 'duplicate': True, 'reused': True, 'status': 'complete', **({'dry_run': True} if dry_run else {})}
    if dry_run: return {'dry_run': True, 'would_receive': file.name, 'sha256': sha, 'status': 'planned', 'reused': False}
    result = client.post('/api/intake/receive', {'project_id': project_id, 'name': file.name, 'data': base64.b64encode(content).decode(), 'batch': 'invoice-assistant-cli'})
    entry = result.get('entry', result)
    if entry.get('project_id') != project_id: raise CLIError('原服务返回材料归属不同。', 'PROJECT_BOUNDARY', 4)
    return {'entry': material_view(entry), 'duplicate': result.get('duplicate', False), 'reused': result.get('duplicate', False), 'status': 'complete'}


def matches(client, project_id, suggestions=False):
    client.project(project_id)
    path = '/api/material-matching' + ('/suggestions' if suggestions else '') + '?' + urlencode({'project': project_id})
    return client.get(path)


def create_drafts(client, project_id, body, request_id, dry_run=False):
    client.project(project_id)
    if set(body) - {'groups', 'project_id'} or body.get('project_id', project_id) != project_id:
        raise CLIError('草稿请求项目不同或包含不支持参数。', 'PROJECT_BOUNDARY', 4)
    groups = body.get('groups')
    if not isinstance(groups, list) or not 1 <= len(groups) <= 100 or not all(isinstance(r, dict) for r in groups): raise CLIError('请提供1–100组原生候选请求。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', request_id): raise CLIError('request-id需为8–128位字母/数字/下划线/连字符。')
    # The original batch transaction validates suggestion snapshots, members,
    # explicit financial fields and idempotency; do not rebuild those rules.
    if dry_run:
        board = matches(client, project_id, suggestions=True)
        available = {r['id']: r for r in board.get('suggestions', [])}
        for group in groups:
            candidate = available.get(group.get('suggestion_id'))
            if not candidate or candidate.get('snapshot_token') != group.get('snapshot_token'):
                raise CLIError('候选或快照已经变化，请重新读取建议。', 'STALE_SNAPSHOT', 4)
        return {'status': 'planned', 'dry_run': True, 'would_create_groups': len(groups), 'native_validation_on_commit': True}
    result = client.post('/api/material-matching/create-batch', {'project_id': project_id, 'groups': groups, 'idempotency_key': request_id})
    if any(row.get('project_id') != project_id for row in result.get('items', [])):
        raise CLIError('原服务返回另一项目草稿。', 'PROJECT_BOUNDARY', 4)
    pending = any(not r.get('verified') and r.get('stage') == 'draft' for r in result.get('items', []))
    return {'result': result, 'status': 'pending_review' if pending else 'complete', 'reused': result.get('replayed', False),
            'note': '保留原生核对/提交状态；本命令没有核对或提交。'}
