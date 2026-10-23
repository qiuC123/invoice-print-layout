"""Standalone stdlib-only completion validator copied into each WB input package."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any


def validate(request: dict[str, Any], digest: str, messages: list[dict[str, Any]], result: dict[str, Any]) -> None:
    if set(result) != {'schema_version', 'task_id', 'nonce', 'request_sha256', 'records'}:
        raise ValueError('结果顶层字段不符合协议')
    if result['schema_version'] != 1 or result['task_id'] != request['task_id'] or result['nonce'] != request['nonce'] or result['request_sha256'] != digest:
        raise ValueError('结果不属于本次请求')
    inputs = {r['photo_id']: r for r in request['records']}
    message_map = {m['message_id']: m for m in messages}
    rows = result['records']
    if not isinstance(rows, list) or len(rows) != len(inputs):
        raise ValueError('结果数量不完整')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'photo_id', 'kind', 'status', 'basis', 'role', 'role_basis_type', 'role_basis', 'evidence_message_ids'}:
            raise ValueError('结果行字段不符合协议')
        if any(not isinstance(row[k], str) or not row[k].strip() for k in row if k != 'evidence_message_ids'):
            raise ValueError('结果行文字字段为空或无效')
        pid = row['photo_id']
        if pid in seen or pid not in inputs:
            raise ValueError('照片ID重复或不属于本批次')
        seen.add(pid)
        if row['kind'] not in request['photo_kinds'] or row['role'] not in request['roles']:
            raise ValueError('用途或岗位超出允许值')
        if row['status'] not in ('classified', 'pending') or (row['kind'] == 'uncertain') != (row['status'] == 'pending'):
            raise ValueError('用途与状态不一致')
        if inputs[pid]['availability'] == 'missing' and (row['kind'] != 'uncertain' or row['role'] != '待确认'):
            raise ValueError('不能对缺失照片填写已分类用途或岗位')
        ids = row['evidence_message_ids']
        if not isinstance(ids, list) or any(not isinstance(v, str) or v not in message_map for v in ids):
            raise ValueError('引用了包外消息')
        if row['role'] == '待确认':
            if row['role_basis_type'] != 'unavailable':
                raise ValueError('待确认岗位的依据类型无效')
        elif row['role_basis_type'] != 'explicit_text' or not ids or not any(row['role'] in message_map[mid]['text'] for mid in ids):
            raise ValueError('已知岗位缺少包内明确文字依据')


def complete(folder: Path) -> dict[str, Any]:
    def read(p: Path) -> Any:
        return json.loads(p.read_text(encoding='utf-8-sig'))
    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    marker = read(folder / 'INPUT_READY.json')
    for rel, digest in marker['files'].items():
        path = (folder / rel).resolve()
        if Path(rel).is_absolute() or not path.is_relative_to(folder.resolve()) or sha(path) != digest:
            raise ValueError('输入文件发生变化')
    request = read(folder / 'request.json')
    if marker['request_sha256'] != sha(folder / 'request.json') or marker['task_id'] != request['task_id']:
        raise ValueError('输入标记不匹配')
    result_path = folder / 'output/result.json'
    validate(request, sha(folder / 'request.json'), read(folder / request['messages_path']), read(result_path))
    result = {'schema_version': 1, 'task_id': request['task_id'], 'nonce': request['nonce'],
              'request_sha256': sha(folder / 'request.json'), 'result_sha256': sha(result_path)}
    target = folder / 'output/COMPLETE.json'
    if target.exists():
        if read(target) != result:
            raise ValueError('已完成结果变化，不能覆盖')
    else:
        with target.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    return result


if __name__ == '__main__':
    print(json.dumps(complete(Path(__file__).resolve().parent), ensure_ascii=False))
