"""Interpret the launcher with network/start effects replaced; never touch the live service."""
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from invoice_print_layout.workbench_runtime import identity, source_build_id


@pytest.mark.parametrize('mode', ['ready', 'cold', 'timeout', 'stale', 'workspace', 'foreign', 'capability', 'malformed', 'legacy', 'empty', 'not-ready', 'occupied'])
def test_launcher_identity_readiness_and_failure(tmp_path, mode):
    powershell = shutil.which('powershell.exe')
    if not powershell:
        pytest.skip('Windows PowerShell unavailable')
    root = Path(__file__).resolve().parents[1]
    expected = {**identity(tmp_path, source_build_id()), 'ready': True, 'started_at': '2026-01-01T00:00:00Z'}
    if mode == 'stale': expected['build_id'] = 'old'
    if mode == 'workspace': expected['workspace_id'] = 'different'
    if mode == 'foreign': expected['app_id'] = 'another-app'
    if mode == 'capability': expected['capabilities'] = []
    if mode == 'not-ready': expected['ready'] = False
    payload = tmp_path/'runtime.json'
    payload.write_text('invalid' if mode == 'malformed' else 'null' if mode == 'empty' else json.dumps(expected), encoding='utf-8')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    if mode == 'occupied': sock.listen()
    else: sock.close()
    harness = r'''$ErrorActionPreference = 'Stop'
$global:probes = 0
$global:basic = $true
$global:launches = [Collections.Generic.List[object]]::new()
function Invoke-WebRequest {
    param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
    $global:probes++
    $global:basic = $global:basic -and $UseBasicParsing.IsPresent
    if ($Uri -ne 'http://127.0.0.1:PORT/api/runtime') { throw 'Wrong endpoint' }
    if ('MODE' -eq 'legacy') {
        Add-Type 'public class LegacyError : System.Exception { public object Response = new object(); }'
        throw (New-Object LegacyError)
    }
    if ('MODE' -in @('timeout', 'occupied') -or ('MODE' -eq 'cold' -and $global:probes -eq 1)) { throw 'Offline' }
    return [PSCustomObject]@{Content=(Get-Content -Raw -LiteralPath 'PAYLOAD')}
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle)
    $global:launches.Add([PSCustomObject]@{FilePath=$FilePath;WindowStyle=$WindowStyle;Arguments=$ArgumentList})
}
function Start-Sleep { param($Seconds) }
$failure = $null
try { & 'LAUNCHER' -Workspace 'WORKSPACE' -Port PORT } catch { $failure = $_.Exception.Message }
[PSCustomObject]@{Probes=$global:probes;Basic=$global:basic;Launches=@($global:launches.ToArray());Failure=$failure} | ConvertTo-Json -Depth 6 -Compress
'''
    for key,value in {'MODE':mode,'PORT':str(port),'PAYLOAD':str(payload),'LAUNCHER':str(root/'scripts/start-workbench.ps1'),'WORKSPACE':str(tmp_path)}.items():
        harness = harness.replace(key, value.replace("'", "''"))
    script = tmp_path/'harness.ps1'
    script.write_text(harness, encoding='utf-8-sig')
    try:
        result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(script)],
                                capture_output=True, text=True, timeout=40, check=True, cwd=root)
    finally:
        sock.close()
    data = json.loads(result.stdout)
    assert data['Basic']
    launches = data['Launches']
    if mode in {'ready','cold'}:
        assert data['Failure'] is None
        assert len(launches) == (2 if mode == 'cold' else 1)
        assert launches[-1]['FilePath'] == f'http://127.0.0.1:{port}'
    else:
        assert data['Failure']
        assert len(launches) == (1 if mode == 'timeout' else 0)
    if mode in {'cold','timeout'}:
        assert launches[0]['FilePath'].endswith('pythonw.exe')
        assert launches[0]['WindowStyle'] == 'Hidden'
    if mode == 'timeout': assert data['Probes'] == 16
