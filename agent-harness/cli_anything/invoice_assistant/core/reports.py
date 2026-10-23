from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote
import zipfile

from cli_anything.invoice_assistant.core.workflow import expense_rows
from cli_anything.invoice_assistant.utils.client import CLIError, canonical, digest, read_json

SCHEMA = 'invoice-assistant-cli/report-v1'


def filename(value, kind):
    if (not isinstance(value, str) or not value or any(c in value for c in '/\\:\x00')
        or Path(value).name != value or Path(value).suffix.lower() != '.' + kind):
        raise CLIError('原服务报表文件名无效。', 'INVALID_ARTIFACT', 4)
    return value


def listing(client, project_id):
    groups = {}
    for row in expense_rows(client, project_id):
        files = {k: filename(v, k) for k, v in (row.get('report_files') or {}).items() if k in {'pdf', 'xlsx', 'md'}}
        if not files: continue
        key = digest({'workspace_id': client.workspace_id, 'project_id': project_id, 'files': files})[:24]
        group = groups.setdefault(key, {'id': key, 'files': files, 'expense_ids': [], 'historical_snapshot': True})
        group['expense_ids'].append(row['id'])
    return {'reports': list(groups.values()), 'status': 'complete', 'reused': True,
            'note': '历史报销包保留生成时快照；不按当前账本重新生成，也不再次提交。'}


def validate(content, kind):
    if kind == 'pdf' and not content.startswith(b'%PDF-'): raise CLIError('PDF文件格式无效。', 'RESULT_DAMAGED', 4)
    if kind == 'xlsx':
        import io
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(z.namelist()) or z.testzip(): raise ValueError()
        except (OSError, ValueError, zipfile.BadZipFile): raise CLIError('Excel文件格式无效。', 'RESULT_DAMAGED', 4) from None
    if kind == 'md':
        try: content.decode('utf-8-sig')
        except UnicodeError: raise CLIError('清单不是有效UTF-8文本。', 'RESULT_DAMAGED', 4) from None


def read(folder):
    folder = Path(folder).resolve()
    body = read_json(folder / 'invoice-cli-result.json')
    request = body.get('request')
    if body.get('schema') != SCHEMA or body.get('signature') != digest(request): raise CLIError('报表回执签名或格式无效。', 'RESULT_DAMAGED', 4)
    required = request.get('files', {}) if isinstance(request, dict) else {}
    artifacts = body.get('artifacts', [])
    if not required or len(artifacts) != len(required): raise CLIError('报表产物不完整。', 'RESULT_DAMAGED', 4)
    for row in artifacts:
        name = filename(row.get('name'), row.get('kind'))
        if required.get(row['kind']) != name: raise CLIError('报表与请求不同。', 'RESULT_DAMAGED', 4)
        path = folder / name
        if path.is_symlink() or path.parent != folder or not path.is_file(): raise CLIError('产物路径无效。', 'RESULT_DAMAGED', 4)
        data = path.read_bytes(); validate(data, row['kind'])
        if len(data) != row.get('size') or digest(data) != row.get('sha256'):
            raise CLIError('报表文件hash发生变化，未重新生成。', 'RESULT_DAMAGED', 4)
    return body


def public(body, folder):
    return {'status': 'complete', 'reused': True, 'historical_snapshot': True, 'request': body['request'],
            'result_dir': str(folder), 'receipt': str(folder / 'invoice-cli-result.json'),
            'artifacts': [{**row, 'path': str(folder / row['name'])} for row in body['artifacts']]}


@contextmanager
def output_lock(folder):
    file = folder.parent / ('.' + folder.name + '.invoice-assistant.lock')
    with file.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell(): stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: raise CLIError('输出目录正在由其他CLI请求写入。', 'OUTPUT_LOCKED', 5) from None
        try: yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


def download(client, project_id, report_id, output, dry_run=False):
    group = next((g for g in listing(client, project_id)['reports'] if g['id'] == report_id), None)
    if not group: raise CLIError('历史报表不属于当前项目或已不存在。', 'REPORT_NOT_FOUND', 4)
    request = {'workspace_id': client.workspace_id, 'project_id': project_id, 'report_id': report_id,
               'files': group['files'], 'expense_ids': sorted(group['expense_ids']), 'historical_snapshot': True}
    folder = Path(output).expanduser().resolve()
    def existing():
        value = read(folder)
        if value['request'] != request: raise CLIError('输出目录已有另一份报表，未覆盖。', 'OUTPUT_CONFLICT', 4)
        return {**public(value, folder), **({'dry_run': True} if dry_run else {})}
    if folder.exists(): return existing()
    if dry_run: return {'status': 'planned', 'dry_run': True, 'would_download': request, 'output_dir': str(folder)}
    folder.parent.mkdir(parents=True, exist_ok=True)
    with output_lock(folder):
        if folder.exists(): return existing()
        with tempfile.TemporaryDirectory(prefix='.' + folder.name + '.staging-', dir=folder.parent) as staging:
            stage = Path(staging).resolve()
            if stage.parent != folder.parent: raise CLIError('临时目录无效。', 'INVALID_PATH', 4)
            payload = stage / 'result'; payload.mkdir()
            rows = []
            for kind, name in sorted(group['files'].items()):
                client.runtime()
                data = client.request('/export/' + quote(name, safe=''), binary=True)
                client.operations['downloads'] += 1
                validate(data, kind)
                (payload / name).write_bytes(data)
                rows.append({'name': name, 'kind': kind, 'size': len(data), 'sha256': digest(data)})
            client.state(refresh=True)
            after = next((g for g in listing(client, project_id)['reports'] if g['id'] == report_id), None)
            if after != group: raise CLIError('下载期间历史包关联发生变化；未发布结果。', 'SOURCE_CHANGED', 4)
            body = {'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(), 'request': request,
                    'signature': digest(request), 'artifacts': rows,
                    'validation': 'original existing export downloaded; hashes observed now; no rerender or resubmit'}
            (payload / 'invoice-cli-result.json').write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
            read(payload)
            payload.replace(folder)
    return public(body, folder)
