"""Workspace-wide read-only consolidation inputs; never use computed expense views."""
from urllib.parse import urlencode

from cli_anything.invoice_assistant.utils.client import CLIError, digest


def listing(client, project_id, source, *, after_id=0, offset=0, limit=200, fingerprint=''):
    if project_id:
        raise CLIError('整理输入覆盖整个工作区（含已删除和无项目记录），请省略--project。')
    if source in {'audit', 'corrections'}:
        return client.get('/api/knowledge/' + source + '?' + urlencode({'after_id': after_id, 'limit': limit}))
    if source == 'classification':
        return client.get('/api/knowledge/classification?' + urlencode(
            {'offset': offset, 'limit': limit, 'fingerprint': fingerprint}))
    key, collection = ('evidence_key', 'learned') if source == 'experience' else ('id', 'rules')
    rows = sorted(client.get('/api/preferences')[collection], key=lambda row: row[key])
    hashes = {row[key]: digest(row) for row in rows}
    version = digest(hashes)
    if fingerprint and fingerprint != version:
        raise CLIError('快照已变化，请从第一页重新读取。', 'SNAPSHOT_CHANGED', 4)
    page = rows[offset:offset + limit]
    return {'status': 'complete', 'items': page, 'total': len(rows), 'offset': offset, 'limit': limit,
            'has_more': offset + limit < len(rows), 'fingerprint': version,
            'fingerprints': {row[key]: hashes[row[key]] for row in page}}
