"""Execute launch/task scripts with effects replaced, never touching real tasks/services."""
import json
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from invoice_print_layout.workbench_runtime import identity, source_build_id


ROOT = Path(__file__).resolve().parents[1]


def run_script(tmp_path, script, replacements):
    powershell = shutil.which('powershell.exe')
    if not powershell:
        pytest.skip('Windows PowerShell unavailable')
    for key, value in replacements.items():
        script = script.replace(f'@@{key}@@', str(value).replace("'", "''"))
    path = tmp_path / 'harness.ps1'
    path.write_text(script, encoding='utf-8-sig')
    return subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(path)],
                          cwd=ROOT, capture_output=True, text=True, timeout=40)


@pytest.mark.parametrize('case', ['ready', 'old', 'unready', 'cold', 'timeout', 'foreign', 'workspace', 'workbench', 'malformed', 'occupied'])
def test_ensure_running_never_stops_or_opens_browser(tmp_path, case):
    runtime = {**identity(tmp_path, source_build_id(), 'invoice'), 'ready': True, 'started_at': '2026-01-01'}
    if case == 'old':
        runtime['build_id'] = 'old'
        runtime['capabilities'] = []
    if case == 'unready':
        runtime['ready'] = False
    if case == 'foreign':
        runtime['app_id'] = 'foreign'
    if case == 'workspace':
        runtime['workspace_id'] = 'foreign'
    if case == 'workbench':
        runtime['app_id'] = 'invoice-print-layout.workbench'
    payload = tmp_path / 'runtime.json'
    payload.write_text('invalid' if case == 'malformed' else json.dumps(runtime), encoding='utf-8')
    log = tmp_path / '工作台' / 'startup.log'
    log.parent.mkdir()
    log.write_text('x' * 300000, encoding='utf-8')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    if case == 'occupied':
        sock.listen()
    else:
        sock.close()
    script = r'''$ErrorActionPreference = 'Stop'
$global:probes = 0
function Invoke-WebRequest {
    param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
    if ($Uri -notlike '*/api/runtime') { throw 'Unexpected endpoint; shutdown is forbidden' }
    $global:probes++
    if ('@@CASE@@' -in @('timeout', 'occupied') -or ('@@CASE@@' -eq 'cold' -and $global:probes -eq 1)) { throw 'Offline' }
    if ('@@CASE@@' -eq 'malformed') { return [PSCustomObject]@{Content='invalid'} }
    $payload = Get-Content -Raw -LiteralPath '@@PAYLOAD@@' | ConvertFrom-Json
    if ('@@CASE@@' -eq 'cold') { $payload.build_id = $expected.build_id }
    return [PSCustomObject]@{Content=($payload | ConvertTo-Json -Depth 6 -Compress)}
}
function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle)
    if ($FilePath -notlike '*pythonw.exe' -or $WindowStyle -ne 'Hidden') { throw 'Visible launch forbidden' }
    Add-Content -LiteralPath '@@LAUNCHES@@' -Value 'hidden pythonw'
}
function Start-Sleep { param($Seconds) }
& '@@LAUNCHER@@' -Workspace '@@WORKSPACE@@' -Port @@PORT@@ -NoBrowser -EnsureRunning
exit $LASTEXITCODE
'''
    try:
        result = run_script(tmp_path, script, {'CASE': case, 'PAYLOAD': payload, 'PORT': port,
                            'LAUNCHES': tmp_path / 'launches.txt', 'LAUNCHER': ROOT / 'scripts/start-invoice-assistant.ps1',
                            'WORKSPACE': tmp_path})
    finally:
        sock.close()
    success = case in {'ready', 'old', 'unready', 'cold'}
    assert result.returncode == (0 if success else 1), result.stderr
    launches = tmp_path / 'launches.txt'
    assert launches.exists() == (case in {'cold', 'timeout'}), log.read_text(encoding='utf-8-sig')
    if success:
        assert log.stat().st_size == 300000
    else:
        text = log.read_text(encoding='utf-8-sig')
        assert log.stat().st_size < 262144
        assert 'no process was stopped' in text or 'startup timed out' in text or 'invalid runtime identity' in text
        assert re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}', text)
        assert not result.stderr


@pytest.mark.parametrize('connected', [False, True])
def test_occupied_port_is_rejected_even_if_tcp_completion_is_delayed(tmp_path, connected):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        result = run_script(tmp_path, r'''$ErrorActionPreference = 'Stop'
function Invoke-WebRequest { throw 'Offline' }
function New-Object {
    param($TypeName)
    if ($TypeName -ne 'System.Net.Sockets.TcpClient') { throw 'Unexpected constructor' }
    $task = [PSCustomObject]@{}
    $task | Add-Member ScriptMethod Wait { param($Milliseconds) return $false }
    $client = [PSCustomObject]@{Connected=[bool]::Parse('@@CONNECTED@@');Task=$task}
    $client | Add-Member ScriptMethod ConnectAsync { param($Address,$Port) return $this.Task }
    $client | Add-Member ScriptMethod Dispose { }
    return $client
}
function Start-Process { Add-Content -LiteralPath '@@LAUNCHES@@' -Value 'unexpected launch' }
function Start-Sleep { param($Seconds) }
& '@@LAUNCHER@@' -Workspace '@@WORKSPACE@@' -Port @@PORT@@ -NoBrowser -EnsureRunning
exit $LASTEXITCODE
''', {'CONNECTED': str(connected), 'LAUNCHES': tmp_path / 'launches.txt',
      'PORT': listener.getsockname()[1],
      'LAUNCHER': ROOT / 'scripts/start-invoice-assistant.ps1', 'WORKSPACE': tmp_path})
    assert result.returncode == 1, result.stderr
    assert not (tmp_path / 'launches.txt').exists()
    assert 'Port is occupied' in (tmp_path / '工作台/startup.log').read_text(encoding='utf-8-sig')


@pytest.mark.parametrize('case', ['install', 'replace-own', 'remove-own', 'remove-absent', 'foreign', 'foreign-remove', 'foreign-action'])
def test_startup_task_ownership_and_xml(tmp_path, case):
    result = run_script(tmp_path, r'''$ErrorActionPreference = 'Stop'
function Get-ScheduledTask {
    param($TaskName, $TaskPath, $ErrorAction)
    if ('@@CASE@@' -in @('install', 'remove-absent')) { return $null }
    $scriptPath = '@@SCRIPT@@'
    $directory = Split-Path $scriptPath
    $execute = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $arguments = '//B //Nologo "' + (Join-Path $directory 'assistant-startup.vbs') + '"'
    $description = 'Invoice Print Layout background launcher: ' + (Resolve-Path (Join-Path $directory '..')).Path
    if ('@@CASE@@' -like 'foreign*') { $description = 'another program' }
    if ('@@CASE@@' -eq 'foreign-action') { $description = 'Invoice Print Layout background launcher: ' + (Resolve-Path (Join-Path $directory '..')).Path; $arguments = 'foreign.vbs' }
    return [PSCustomObject]@{Description=$description;Actions=@([PSCustomObject]@{Execute=$execute;Arguments=$arguments})}
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Xml, [switch]$Force)
    Set-Content -LiteralPath '@@OUTPUT@@' -Value $Xml -Encoding UTF8
}
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, [switch]$Confirm)
    Set-Content -LiteralPath '@@REMOVED@@' -Value $TaskName
}
& '@@SCRIPT@@' -Remove:('@@CASE@@' -like '*remove*')
''', {'CASE': case, 'SCRIPT': ROOT / 'scripts/install-assistant-startup.ps1',
       'OUTPUT': tmp_path / 'task.xml', 'REMOVED': tmp_path / 'removed.txt'})
    if case.startswith('foreign'):
        assert result.returncode != 0
        assert not (tmp_path / 'task.xml').exists()
        assert not (tmp_path / 'removed.txt').exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path / 'removed.txt').exists() == (case == 'remove-own')
        assert (tmp_path / 'task.xml').exists() == (case in {'install', 'replace-own'})
        if case in {'install', 'replace-own'}:
            from xml.etree import ElementTree as ET
            xml = ET.fromstring((tmp_path / 'task.xml').read_text(encoding='utf-8-sig'))
            ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            def value(path):
                return xml.find(path, ns).text
            assert value('t:Triggers/t:LogonTrigger/t:Repetition/t:Interval') == 'PT10M'
            assert xml.find('t:Triggers/t:LogonTrigger/t:Repetition/t:Duration', ns) is None
            assert value('t:Principals/t:Principal/t:LogonType') == 'InteractiveToken'
            assert value('t:Settings/t:MultipleInstancesPolicy') == 'IgnoreNew'
            assert value('t:Settings/t:StartWhenAvailable') == 'true'
            assert value('t:Settings/t:DisallowStartIfOnBatteries') == 'false'
            assert value('t:Settings/t:StopIfGoingOnBatteries') == 'false'
            assert value('t:Actions/t:Exec/t:Command').endswith('wscript.exe')
            assert 'assistant-startup.vbs' in value('t:Actions/t:Exec/t:Arguments')


def test_hidden_host_passes_background_arguments():
    source = (ROOT / 'scripts/assistant-startup.vbs').read_text()
    assert 'shell.Run(command, 0, True)' in source
    assert '-NoBrowser -EnsureRunning' in source
    assert '-WindowStyle Hidden' in source


def test_hidden_host_executes_fake_launcher_without_visible_console(tmp_path):
    wscript = shutil.which('wscript.exe')
    if not wscript:
        pytest.skip('Windows Script Host unavailable')
    shutil.copyfile(ROOT / 'scripts/assistant-startup.vbs', tmp_path / 'assistant-startup.vbs')
    (tmp_path / 'start-invoice-assistant.ps1').write_text(r'''param([switch]$NoBrowser, [switch]$EnsureRunning)
Add-Type 'using System; using System.Runtime.InteropServices; public class StartupWindow { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr handle); }'
$visible = [StartupWindow]::IsWindowVisible([StartupWindow]::GetConsoleWindow())
@{NoBrowser=$NoBrowser.IsPresent;EnsureRunning=$EnsureRunning.IsPresent;ConsoleVisible=$visible} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'result.json') -Encoding UTF8
''', encoding='utf-8-sig')
    result = subprocess.run([wscript, '//B', '//Nologo', str(tmp_path / 'assistant-startup.vbs')],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'result.json').read_text(encoding='utf-8-sig')) == {
        'NoBrowser': True, 'EnsureRunning': True, 'ConsoleVisible': False}
