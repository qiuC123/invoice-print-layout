"""Guarded local cutover: stopped services, unchanged source baseline, verified backup."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rules', type=Path, help='Reviewed private per-item confirmations and preferences')
    args = parser.parse_args()
    source, target = args.source.resolve(), args.target.resolve()
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    # Only source, tests, scripts and docs. No workspace or user data in source sync.
    files = set(baseline)
    for directory in ('src', 'tests', 'scripts', 'docs'):
        files.update(p.relative_to(source).as_posix() for p in (source / directory).rglob('*')
                     if p.is_file() and p.suffix in {'.py', '.js', '.mjs', '.cjs', '.ps1', '.md', '.html', '.css'} and '__pycache__' not in p.parts)
    changed = []
    for relative in sorted(files):
        src, dst = source / relative, target / relative
        if not src.resolve().is_relative_to(source) or not dst.resolve().is_relative_to(target):
            raise SystemExit('Source path is outside the selected checkout')
        if not src.is_file() or digest(src) == baseline.get(relative):
            continue
        if dst.exists() and digest(dst) != baseline.get(relative) and digest(dst) != digest(src):
            raise SystemExit('Main checkout changed; merge required: ' + relative)
        changed.append(relative)
    print(json.dumps({'changed': changed, 'mode': 'apply' if args.apply else 'check'}, ensure_ascii=False))
    if not args.apply:
        return
    for port in (8765, 8766):
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit(f'Port {port} is still active. Stop the old workbench before cutover; no files changed.')
    sys.path.insert(0, str(source / 'src'))
    from invoice_print_layout.workbench_migration import audit, backup
    from invoice_print_layout.expense_projects import ExpenseProjects
    from invoice_print_layout.workbench import ExpenseStore
    receipt = backup(target / 'workspace', args.backup)
    for relative in changed:
        dst = target / relative
        if dst.is_file():
            saved = args.backup / 'source' / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, saved)
    (args.backup / 'source-changes.json').write_text(json.dumps(changed, ensure_ascii=False, indent=2), encoding='utf-8')
    for relative in changed:
        dst = target / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, dst)
    store = ExpenseStore(target / 'workspace')
    migration = ExpenseProjects(store).migrate()
    if args.rules:
        from invoice_print_layout.expense_preferences import Preferences
        from invoice_print_layout.expense_review import ExpenseReview
        plan = json.loads(args.rules.read_text(encoding='utf-8'))
        preferences = Preferences(store)
        for operation in plan['confirmations']:
            item = store.get(operation['id'])
            if item['amount_cents'] != operation['expected_amount_cents'] or item['title'] != operation['expected_title']:
                raise SystemExit('Confirmed item changed; review the private migration plan before continuing.')
        for rule in plan['rules']:
            previous = next((r for r in preferences.list() if r['id'] == rule['id']), None)
            if previous:
                if any(previous.get(k) != v for k, v in rule.items()):
                    raise SystemExit('Existing preference changed; no overwrite allowed.')
            else:
                preferences.save(rule)
        for operation in plan['confirmations']:
            item = store.get(operation['id'])
            if item.get('confirmation', {}).get('reason') != operation['values']['reason']:
                ExpenseReview(store).confirm(item['id'], item['version'], operation['values'])
    after = audit(target / 'workspace')
    if json.dumps(after, sort_keys=True) != json.dumps(receipt['audit'], sort_keys=True):
        raise SystemExit('Ledger audit changed unexpectedly. Backup retained; do not start the workbench.')
    (args.backup / 'after.json').write_text(json.dumps({'audit': after, 'migration': migration}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Source integrated; ledger values, links and original hashes verified. Start the normal workbench launcher.')


if __name__ == '__main__':
    main()
