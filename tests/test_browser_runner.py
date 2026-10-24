"""The entry point must list every contract and continue after a failure."""
from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('timeout', [False, True])
def test_runner_discovers_all_and_reports_failure_without_stopping(tmp_path: Path, timeout: bool) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the browser runner contract')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'tests').mkdir()
    runner = tmp_path / 'scripts/run-browser-tests.mjs'
    shutil.copyfile(Path(__file__).parents[1] / 'scripts/run-browser-tests.mjs', runner)
    for name, code in {
        'a.cjs': "console.log('first')",
        'b.cjs': "setInterval(()=>{},1000)" if timeout else "console.error('synthetic failure');process.exitCode=1",
        'c.cjs': "console.log('after failure')",
    }.items():
        (tmp_path / 'tests' / name).write_text(code, encoding='utf-8')
    listed = subprocess.run([node, str(runner), '--list'], capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert listed.returncode == 0 and listed.stdout.splitlines() == ['a.cjs', 'b.cjs', 'c.cjs']
    fake = tmp_path / 'node_modules/playwright'
    fake.mkdir(parents=True)
    (fake / 'index.js').write_text('module.exports={}', encoding='utf-8')
    output = tmp_path / 'results'
    result = subprocess.run([node, str(runner), '--output=' + str(output)], capture_output=True,
        text=True, encoding='utf-8', timeout=15, env={**os.environ, 'BROWSER_TEST_TIMEOUT_MS': '1000'})
    assert result.returncode == 1
    report = json.loads((output / 'results.json').read_text(encoding='utf-8'))
    assert report['complete'] and report['total'] == 3 and report['passed'] == 2 and report['failed'] == 1
    assert report['results'][1]['timedOut'] is timeout
    assert 'after failure' in (output / 'c.cjs.log').read_text(encoding='utf-8')
