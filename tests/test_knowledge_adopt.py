"""Synthetic private knowledge batches; never read the user's knowledge directory."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import knowledge_adopt as script


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def contents(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*') if path.is_file()}


@pytest.fixture
def knowledge(tmp_path):
    root = tmp_path / 'knowledge'
    write(root, 'INDEX.md', 'Old synthetic index\n')
    write(root, 'projects/synthetic.md', 'Old synthetic knowledge\n')
    write(root, 'methods.md', 'Unchanged synthetic methods\n')
    write(root, 'state.json', json.dumps({'retained': True, 'adopted_through': {'audit_id': 2}}))
    write(root, 'history/old/INDEX.md', 'Earlier synthetic index\n')
    write(root, 'pending/INDEX.md', 'New synthetic index\n')
    write(root, 'pending/projects/synthetic.md', 'New synthetic knowledge\n')
    write(root, 'pending/precedents.md', 'Synthetic verbatim quote\n')
    write(root, 'pending/SUMMARY.md', 'Added 1; modified 1; conflict 0; deleted 0. Source: synthetic audit #5.\n')
    write(root, 'pending/state.json', json.dumps({'adopted_through': {'audit_id': 5,
        'snapshots': {'classification': {'fingerprint': 'synthetic-fingerprint', 'keys': {'synthetic-item': 'synthetic-hash'}}}},
        'retained': False}))
    return root


def test_adopt_snapshots_replaces_and_updates_only_position(knowledge):
    before = {path: value for path, value in contents(knowledge).items()
              if not path.startswith(('pending/', 'history/'))}
    history_before = contents(knowledge / 'history')
    snapshot = script.adopt(knowledge)
    assert snapshot.name.endswith('-before-adopt') and contents(snapshot) == before
    assert not (snapshot / 'pending').exists() and not (snapshot / 'history').exists()
    assert (knowledge / 'INDEX.md').read_text() == 'New synthetic index\n'
    assert (knowledge / 'projects/synthetic.md').read_text() == 'New synthetic knowledge\n'
    assert (knowledge / 'methods.md').read_bytes() == before['methods.md']
    assert (knowledge / 'SUMMARY.md').is_file() and (knowledge / 'precedents.md').is_file()
    state = script.read_object(knowledge / 'state.json')
    assert state['retained'] is True and state['adopted_through']['audit_id'] == 5
    assert state['adopted_through']['snapshots']['classification']['keys'] == {'synthetic-item': 'synthetic-hash'}
    assert not (knowledge / 'pending').exists()
    assert all((knowledge / 'history' / path).read_bytes() == value for path, value in history_before.items())


def test_first_adoption_and_repeat_snapshot_names_are_unique(tmp_path):
    root = tmp_path / 'knowledge'
    snapshots = []
    for ident in (1, 2):
        write(root, 'pending/SUMMARY.md', 'Synthetic batch\n')
        write(root, 'pending/state.json', json.dumps({'adopted_through': {'audit_id': ident}}))
        snapshots.append(script.adopt(root))
    assert snapshots[0] != snapshots[1]
    assert contents(snapshots[0]) == {}
    assert script.read_object(root / 'state.json')['adopted_through']['audit_id'] == 2


def test_reject_preserves_state_bytes_and_records_reason(knowledge):
    before = {path: value for path, value in contents(knowledge).items() if not path.startswith('pending/')}
    write(knowledge, 'history/rejections.md', 'Existing synthetic history\n')
    reason = '合成退回意见\n保持原话，不推进位置'
    script.reject(knowledge, reason)
    assert not (knowledge / 'pending').exists()
    assert all((knowledge / path).read_bytes() == value for path, value in before.items())
    log = (knowledge / 'history/rejections.md').read_text(encoding='utf-8')
    assert log.startswith('Existing synthetic history\n') and json.dumps(reason, ensure_ascii=False) in log


@pytest.mark.parametrize('action', ['adopt', 'reject'])
@pytest.mark.parametrize('problem', ['missing-pending', 'missing-summary'])
def test_refuses_missing_review_batch_without_any_writes(tmp_path, action, problem):
    root = tmp_path / 'knowledge'
    root.mkdir()
    if problem == 'missing-summary':
        write(root, 'pending/state.json', '{"adopted_through": {}}')
    before = contents(root)
    with pytest.raises(ValueError):
        script.adopt(root) if action == 'adopt' else script.reject(root, 'Synthetic reason')
    assert contents(root) == before


@pytest.mark.parametrize('state', ['[]', '{}', '{"adopted_through": null}', 'broken'])
def test_invalid_batch_state_changes_nothing(knowledge, state):
    write(knowledge, 'pending/state.json', state)
    before = contents(knowledge)
    with pytest.raises(ValueError):
        script.adopt(knowledge)
    assert contents(knowledge) == before


@pytest.mark.parametrize('path', ['pending/history/unsafe.md', 'pending/pending/unsafe.md', 'pending/methods.md/unsafe.md'])
def test_rejects_reserved_or_colliding_paths_before_snapshot(knowledge, path):
    write(knowledge, path, 'Synthetic unsafe replacement\n')
    before = contents(knowledge)
    with pytest.raises(ValueError):
        script.adopt(knowledge)
    assert contents(knowledge) == before


def test_failed_file_replace_rolls_back_and_keeps_pending(knowledge, monkeypatch):
    before = contents(knowledge)
    original = script.replace
    failed = False
    def fail_once(path, data):
        nonlocal failed
        if path == knowledge / 'projects/synthetic.md' and not failed:
            failed = True
            raise OSError('Synthetic disk failure')
        original(path, data)
    monkeypatch.setattr(script, 'replace', fail_once)
    with pytest.raises(OSError, match='Synthetic disk failure'):
        script.adopt(knowledge)
    assert failed
    assert all((knowledge / path).read_bytes() == value for path, value in before.items())
    assert not (knowledge / 'SUMMARY.md').exists() and not (knowledge / 'precedents.md').exists()
    assert len(list((knowledge / 'history').glob('*-before-adopt'))) == 1
    assert not list(knowledge.rglob('.adopt-*'))


def test_empty_rejection_reason_changes_nothing(knowledge):
    before = contents(knowledge)
    with pytest.raises(ValueError, match='reason'):
        script.reject(knowledge, '  ')
    assert contents(knowledge) == before


def test_reject_cannot_discard_pending_when_log_write_fails(knowledge, monkeypatch):
    before = contents(knowledge)
    def fail(*args):
        raise OSError('Synthetic log failure')
    monkeypatch.setattr(script, 'replace', fail)
    with pytest.raises(OSError):
        script.reject(knowledge, 'Synthetic reason')
    assert contents(knowledge) == before


@pytest.mark.parametrize('kind', ['is_symlink', 'is_junction'])
@pytest.mark.parametrize('relative', ['pending', 'history', 'projects/synthetic.md', 'pending/INDEX.md'])
def test_links_are_refused_before_reading_or_writing_targets(knowledge, monkeypatch, kind, relative):
    before = contents(knowledge)
    original = getattr(Path, kind)
    monkeypatch.setattr(Path, kind, lambda path: path == knowledge / relative or original(path))
    with pytest.raises(ValueError):
        script.adopt(knowledge)
    assert contents(knowledge) == before


@pytest.mark.parametrize('action', ['adopt', 'reject'])
def test_script_entrypoint(knowledge, action):
    command = [sys.executable, str(Path(script.__file__)), action, '--knowledge', str(knowledge)]
    if action == 'reject':
        command += ['--reason', 'Synthetic rejection']
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (knowledge / 'pending').exists()
