param(
    [string]$Workspace = (Join-Path $PSScriptRoot '..\workspace'),
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$EnsureRunning,
    [switch]$BrowserTab
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-workbench.ps1') -Workspace $Workspace -Port $Port -Mode invoice -NoBrowser:$NoBrowser -EnsureRunning:$EnsureRunning -BrowserTab:$BrowserTab
if ($EnsureRunning -and $LASTEXITCODE) { exit $LASTEXITCODE }
