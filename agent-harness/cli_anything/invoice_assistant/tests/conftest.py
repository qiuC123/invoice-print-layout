import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pytest

ROOT = Path(__file__).resolve().parents[4]


def _resolve_cli():
    path = shutil.which('cli-anything-invoice-assistant')
    if path:
        print('[_resolve_cli] ' + path)
        return [path]
    if os.environ.get('CLI_ANYTHING_FORCE_INSTALLED') == '1': raise RuntimeError('Installed invoice CLI missing in PATH')
    return [sys.executable, '-m', 'cli_anything.invoice_assistant']


@pytest.fixture
def real():
    file = Path(os.environ.get('INVOICE_CLI_REAL_EXPECTATIONS', ROOT/'agent-harness/outputs/acceptance/sample-expectations.private.json'))
    return json.loads(file.read_text(encoding='utf-8'))


@pytest.fixture
def invoke():
    def run(connection, *args, check=True, input=None, project=True):
        command = _resolve_cli() + ['--json', '--url', connection['url'], '--workspace-id', connection['workspace_id']]
        if project: command += ['--project', connection['project']]
        result = subprocess.run(command + list(map(str, args)), input=input, capture_output=True, text=True, encoding='utf-8')
        if check: assert result.returncode == 0, result.stdout + result.stderr
        return result, json.loads(result.stdout) if input is None else [json.loads(s) for s in result.stdout.splitlines()]
    return run


@pytest.fixture
def native(tmp_path):
    script = Path(__file__).with_name('native_fixture.py'); info = tmp_path/'native-info.json'
    env = os.environ.copy(); env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    log = (tmp_path/'native-server.log').open('w', encoding='utf-8')
    process = subprocess.Popen([str(ROOT/'.venv/Scripts/python.exe'), '-X', 'utf8', '-B', str(script), str(tmp_path/'workspace'), str(info)],
                               stdin=subprocess.PIPE, stdout=log, stderr=log, env=env,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 30
        while not info.exists() and process.poll() is None and time.monotonic() < deadline: time.sleep(0.1)
        assert info.exists(), (tmp_path/'native-server.log').read_text(encoding='utf-8')
        yield json.loads(info.read_text(encoding='utf-8'))
    finally:
        process.stdin.close()
        try: process.wait(timeout=15)
        except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=10)
        log.close()
