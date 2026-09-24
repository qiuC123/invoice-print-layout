"""Run the actual launcher with process/network effects replaced by local probes."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('cold_start', [False, True])
def test_windows_launcher_uses_basic_parsing_for_every_health_probe(tmp_path, cold_start):
    powershell = shutil.which('powershell.exe')
    if powershell is None:
        pytest.skip('Windows PowerShell is unavailable')
    launcher = Path(__file__).resolve().parents[1] / 'scripts' / 'start-workbench.ps1'
    # Literal PowerShell quoting, with no interpolation of file contents or secrets.
    script_path = str(launcher).replace("'", "''")
    workspace_path = str(tmp_path).replace("'", "''")
    command = r'''
$ErrorActionPreference = 'Stop'
$global:probeCalls = [Collections.Generic.List[bool]]::new()
$global:launchCalls = [Collections.Generic.List[object]]::new()
function Invoke-WebRequest {
    param([string]$Uri, [switch]$UseBasicParsing, [int]$TimeoutSec)
    $global:probeCalls.Add($UseBasicParsing.IsPresent)
    if ($Uri -ne 'http://127.0.0.1:8765/api/state') { throw 'Wrong health endpoint' }
    if (COLD_START -and $global:probeCalls.Count -eq 1) { throw 'Synthetic stopped service' }
    return [PSCustomObject]@{StatusCode=200}
}
function Start-Process {
    param([string]$FilePath, [string[]]$ArgumentList, [string]$WorkingDirectory, [string]$WindowStyle)
    $global:launchCalls.Add([PSCustomObject]@{FilePath=$FilePath;ArgumentList=$ArgumentList;WindowStyle=$WindowStyle})
}
function Start-Sleep { param([int]$Seconds) }
& 'SCRIPT_PATH' -Workspace 'WORKSPACE_PATH'
[PSCustomObject]@{Probes=@($global:probeCalls.ToArray());Launches=@($global:launchCalls.ToArray())} | ConvertTo-Json -Depth 5 -Compress
'''.replace('COLD_START', '$true' if cold_start else '$false').replace('SCRIPT_PATH', script_path).replace('WORKSPACE_PATH', workspace_path)
    result = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(result.stdout)
    assert data['Probes'] == ([True, True] if cold_start else [True])
    launches = data['Launches']
    assert len(launches) == (2 if cold_start else 1)
    assert launches[-1]['FilePath'] == 'http://127.0.0.1:8765'
    if cold_start:
        assert launches[0]['FilePath'].endswith('pythonw.exe')
        assert launches[0]['WindowStyle'] == 'Hidden'
        assert launches[0]['ArgumentList'][:3] == ['-m', 'invoice_print_layout.workbench_web', '--workspace']
