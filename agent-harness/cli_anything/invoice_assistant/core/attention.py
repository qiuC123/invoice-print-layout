from urllib.parse import urlencode

from cli_anything.invoice_assistant.utils.client import CLIError


def list_attention(client, project_id, offset, limit, all_statuses=False, fingerprint=''):
    if project_id and not all_statuses:
        client.project(project_id)
    query = {'project': project_id or '', 'offset': offset, 'limit': limit}
    if fingerprint and not all_statuses:
        raise CLIError('--fingerprint 仅适用于 --all。')
    if all_statuses:
        query.update(all=1, fingerprint=fingerprint)
    return client.get('/api/attention?' + urlencode(query))


def context(client, project_id, obj):
    result = client.get('/api/attention/context?' + urlencode({'object': obj}))
    if project_id and result.get('fields', {}).get('project_id', result.get('record', {}).get('project_id')) != project_id:
        raise CLIError('疑点不属于当前项目。', 'PROJECT_BOUNDARY', 4)
    return result


def resolve(client, project_id, obj, expected_version, request, dry_run=False):
    if set(request) - {'action', 'values', 'decided_by', 'evidence'}:
        raise CLIError('写回请求只接受action/values/decided_by/evidence。')
    current = context(client, project_id, obj)
    if current.get('status') != 'open' or current.get('version') != expected_version:
        raise CLIError('疑点或原件已变化；请重新查看上下文。', 'VERSION_CHANGED', 4)
    if dry_run:
        return {'status': 'planned', 'dry_run': True, 'object': obj, 'request': request,
                'native_validation_on_commit': True}
    return client.post('/api/attention/resolve', {**request, 'object': obj, 'expected_version': expected_version})
