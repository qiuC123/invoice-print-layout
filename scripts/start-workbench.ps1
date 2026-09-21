param([string]$Workspace = (Join-Path $PSScriptRoot '..\workspace'))
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not (Test-Path -LiteralPath $Workspace)) {
    New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
}
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
$python = Join-Path $project '.venv\Scripts\pythonw.exe'
$url = 'http://127.0.0.1:8765'
try { $null = Invoke-WebRequest "$url/api/state" -TimeoutSec 2 } catch {
    Start-Process -FilePath $python -ArgumentList @('-m', 'invoice_print_layout.workbench_web', '--workspace', "`"$Workspace`"") -WorkingDirectory $project -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        Start-Sleep -Seconds 1
        try { $null = Invoke-WebRequest "$url/api/state" -TimeoutSec 2; break } catch { }
    }
}
Start-Process $url
