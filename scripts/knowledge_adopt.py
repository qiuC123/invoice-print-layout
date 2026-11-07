"""Manually adopt or reject a private knowledge batch; standard library only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object: {path.name}')
    return value


def files(root: Path, *, exclude: tuple[str, ...] = ()) -> list[Path]:
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        raise ValueError(f'Expected a regular directory: {root.name}')
    result = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or path.is_junction():
            raise ValueError(f'Links are not supported: {path.name}')
        if path.name in exclude:
            continue
        if path.is_dir():
            result.extend(files(path))
        elif path.is_file():
            result.append(path)
        else:
            raise ValueError(f'Expected a regular file: {path.name}')
    return result


def replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.adopt-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def batch(knowledge: Path) -> tuple[Path, list[Path]]:
    files(knowledge, exclude=('pending', 'history'))
    pending = knowledge / 'pending'
    paths = files(pending)
    if not (pending / 'SUMMARY.md').is_file():
        raise ValueError('pending/SUMMARY.md is required')
    if any(path.name.casefold() in {'pending', 'history'} for path in pending.iterdir()):
        raise ValueError('A batch cannot replace pending/ or history/')
    return pending, paths


def adopt(knowledge: Path) -> Path:
    pending, paths = batch(knowledge)
    proposed = read_object(pending / 'state.json')
    if not isinstance(proposed.get('adopted_through'), dict):
        raise ValueError('pending/state.json must contain an adopted_through object')
    state_path = knowledge / 'state.json'
    state = read_object(state_path) if state_path.exists() else {}
    payload = {path.relative_to(pending): path.read_bytes() for path in paths if path.name != 'state.json'
               or path.parent != pending}
    payload[Path('state.json')] = (json.dumps({**state, 'adopted_through': proposed['adopted_through']},
                                            ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    for relative in payload:
        target = knowledge / relative
        if target.is_dir() or any(parent.is_file() for parent in target.parents if parent != knowledge):
            raise ValueError(f'File/directory collision: {relative}')

    snapshot = knowledge / 'history' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '-before-adopt')
    snapshot.mkdir(parents=True)
    for path in knowledge.iterdir():
        if path.name not in {'pending', 'history'}:
            if path.is_dir():
                shutil.copytree(path, snapshot / path.name)
            else:
                shutil.copy2(path, snapshot / path.name)
    changed = []
    try:
        # shortcut: replacement is atomic per file; after process interruption restore the snapshot before retrying.
        for relative, data in payload.items():  # state is last, so cursors never lead the installed pages.
            changed.append(relative)
            replace(knowledge / relative, data)
    except BaseException:
        for relative in reversed(changed):
            original = snapshot / relative
            if original.is_file():
                replace(knowledge / relative, original.read_bytes())
            else:
                (knowledge / relative).unlink(missing_ok=True)
        raise
    shutil.rmtree(pending)
    return snapshot


def reject(knowledge: Path, reason: str) -> None:
    if not reason.strip():
        raise ValueError('A rejection reason is required')
    pending, _ = batch(knowledge)
    history = knowledge / 'history'
    if history.exists():
        files(history)
    log = history / 'rejections.md'
    previous = log.read_bytes() if log.exists() else b''
    entry = '\n- ' + datetime.now(timezone.utc).isoformat() + ': ' + json.dumps(reason, ensure_ascii=False) + '\n'
    replace(log, previous + entry.encode('utf-8'))
    shutil.rmtree(pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('adopt', 'reject'):
        command = commands.add_parser(name)
        command.add_argument('--knowledge', type=Path, required=True)
        if name == 'reject':
            command.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'adopt':
            print(adopt(args.knowledge.absolute()))
        else:
            reject(args.knowledge.absolute(), args.reason)
            print('rejected')
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
