param(
    [string]$Workspace = (Join-Path $PSScriptRoot '..\workspace'),
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
# Bind module resolution to this checkout, even when a worktree shares a venv.
$env:PYTHONPATH = (Join-Path $project 'src') + [IO.Path]::PathSeparator + $env:PYTHONPATH
if (-not (Test-Path -LiteralPath $Workspace)) {
    New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
}
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
$python = Join-Path $project '.venv\Scripts\pythonw.exe'
$consolePython = Join-Path $project '.venv\Scripts\python.exe'
$url = "http://127.0.0.1:$Port"
$expectedJson = & $consolePython -m invoice_print_layout.workbench_runtime --workspace $Workspace
if ($LASTEXITCODE -ne 0) { throw 'Cannot read expected backend identity.' }
$expected = $expectedJson | ConvertFrom-Json

function Read-WorkbenchRuntime {
    param([switch]$Starting)
    try {
        $response = Invoke-WebRequest "$url/api/runtime" -UseBasicParsing -TimeoutSec 2
    } catch {
        if ($null -ne $_.Exception.Response) {
            throw 'Port responds but runtime identity is unavailable. Old backend or another service; no process was stopped. Update the service explicitly.'
        }
        if ($Starting) { return $null }
        $socket = New-Object System.Net.Sockets.TcpClient
        try {
            $connection = $socket.ConnectAsync('127.0.0.1', $Port)
            if ($connection.Wait(500) -and $socket.Connected) {
                throw 'Port is occupied but the service is not ready; no process was stopped.'
            }
        } catch {
            if ($socket.Connected) { throw }
        } finally { $socket.Dispose() }
        return $null
    }
    try {
        $parsed = $response.Content | ConvertFrom-Json
        if ($null -eq $parsed -or $parsed -isnot [PSCustomObject]) { throw 'Invalid response' }
        return $parsed
    } catch {
        throw 'Port returned an invalid runtime identity; no process was stopped.'
    }
}

function Assert-WorkbenchRuntime($runtime) {
    if ($runtime.app_id -ne $expected.app_id -or $runtime.workspace_id -ne $expected.workspace_id) {
        throw 'Port belongs to another service or workspace; no process was stopped.'
    }
    if ($runtime.build_id -ne $expected.build_id) {
        throw 'This workspace is running an older backend. Update the service explicitly; rerunning the launcher does not reload it.'
    }
    if ($runtime.ready -ne $true -or -not $runtime.started_at) { throw 'Backend is not ready.' }
    foreach ($capability in $expected.capabilities) {
        if ($runtime.capabilities -notcontains $capability) { throw "Backend capability unavailable: $capability" }
    }
}

$runtime = Read-WorkbenchRuntime
if ($null -ne $runtime -and $runtime.app_id -eq $expected.app_id -and $runtime.workspace_id -eq $expected.workspace_id -and $runtime.build_id -ne $expected.build_id -and $runtime.capabilities -contains 'graceful_shutdown') {
    $state = (Invoke-WebRequest "$url/api/state" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
    Invoke-WebRequest "$url/api/shutdown" -Method Post -Headers @{'X-Workbench-Token'=$state.token} -ContentType 'application/json' -Body '{}' -UseBasicParsing -TimeoutSec 5 | Out-Null
    $runtime = $null
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Seconds 1
        $runtime = Read-WorkbenchRuntime
        if ($null -eq $runtime) { break }
    }
    if ($null -ne $runtime) { throw 'Old backend did not exit after graceful shutdown; browser was not opened.' }
}
if ($null -eq $runtime) {
    Start-Process -FilePath $python -ArgumentList @('-m', 'invoice_print_layout.workbench_web', '--workspace', "`"$Workspace`"", '--port', "$Port") -WorkingDirectory $project -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        $runtime = Read-WorkbenchRuntime -Starting
        if ($null -ne $runtime) { break }
    }
    if ($null -eq $runtime) { throw 'Workbench startup timed out. No ready service; browser was not opened.' }
}
Assert-WorkbenchRuntime $runtime
if (-not $NoBrowser) { Start-Process $url }
