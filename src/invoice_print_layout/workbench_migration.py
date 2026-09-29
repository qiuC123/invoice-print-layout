"""Offline backup/restore and ledger integrity receipts for controlled cutovers."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any


def audit(workspace: Path) -> dict[str, Any]:
    database = workspace / '工作台' / 'expenses.sqlite3'
    with sqlite3.connect(f'file:{database.resolve().as_posix()}?mode=ro', uri=True) as db:
        items = [json.loads(row[0]) for row in db.execute('SELECT data FROM expenses ORDER BY id')]
        files = db.execute('SELECT id,item_id,role,path,digest FROM attachments ORDER BY id').fetchall()
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok':
        raise ValueError('数据库完整性检查未通过')
    hashes = {}
    for key, _, _, path, digest in files:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError('原件哈希与入账记录不一致')
        hashes[key] = actual
    return {'items': len(items), 'amount_cents': sum(x['amount_cents'] for x in items),
            'draft_cents': sum(x['amount_cents'] for x in items if x['stage'] == 'draft'),
            'expense_values': [{k: x.get(k) for k in ('id', 'amount_cents', 'stage', 'verified')} for x in items],
            'attachments': len(files), 'links': [(x[0], x[1], x[2]) for x in files], 'hashes': hashes}


def backup(workspace: Path, destination: Path) -> dict[str, Any]:
    """Call while the workbench is stopped; never overwrite an earlier backup."""
    if destination.exists():
        raise ValueError('备份目录已存在，请选择新目录')
    before = audit(workspace)
    destination.mkdir(parents=True)
    for relative in ('工作台/expenses.sqlite3', '后勤/tasks.sqlite3'):
        source = workspace / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(f'file:{source.resolve().as_posix()}?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
                src.backup(dst)
    with sqlite3.connect(workspace / '工作台/expenses.sqlite3') as db:
        paths = {row[0] for row in db.execute('SELECT path FROM attachments')}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='intake_queue'").fetchone():
            paths.update(json.loads(row[0])['path'] for row in db.execute('SELECT data FROM intake_queue'))
    originals = destination / 'originals'
    originals.mkdir()
    manifest = []
    for index, name in enumerate(sorted(paths)):
        path = Path(name)
        target = originals / f'{index}{path.suffix}'
        shutil.copy2(path, target)
        manifest.append({'original': str(path), 'backup': str(target.relative_to(destination)), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
    after = audit(workspace)
    if after != before:
        raise ValueError('备份期间账目变化，请停止工作台后重试；本次副本保留供检查')
    receipt = {'workspace': str(workspace.resolve()), 'audit': before, 'files': manifest}
    (destination / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt


def restore(workspace: Path, source: Path) -> None:
    """Restore only a matching workspace after stopping it and backing up newer data."""
    receipt = json.loads((source / 'receipt.json').read_text(encoding='utf-8'))
    if str(workspace.resolve()) != receipt['workspace']:
        raise ValueError('备份不属于此工作区')
    for entry in receipt['files']:
        file = source / entry['backup']
        if hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('备份文件校验失败')
    for entry in receipt['files']:
        target = Path(entry['original'])
        # Original paths came from the matching backup, never from HTTP input.
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / entry['backup'], target)
    for relative in ('工作台/expenses.sqlite3', '后勤/tasks.sqlite3'):
        file = source / relative
        if file.is_file():
            with sqlite3.connect(file) as src, sqlite3.connect(workspace / relative) as dst:
                src.backup(dst)
    if audit(workspace) != receipt['audit']:
        # JSON round-trips tuples to lists, so compare serialized values.
        if json.dumps(audit(workspace), sort_keys=True) != json.dumps(receipt['audit'], sort_keys=True):
            raise ValueError('恢复后的审计不一致')
