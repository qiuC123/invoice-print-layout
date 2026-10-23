param([string]$Workspace = (Join-Path $PSScriptRoot '..\workspace'))
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    throw 'This helper prepares the Windows x64 client only.'
}
New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
$Workspace = (Resolve-Path -LiteralPath $Workspace).Path
$runtimeRoot = Join-Path $Workspace 'phone-remote-runtime'
$binRoot = Join-Path $runtimeRoot 'bin'
$pipeFile = Join-Path $runtimeRoot 'pipe.txt'
New-Item -ItemType Directory -Path $binRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $pipeFile)) {
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $key = -join ($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Workspace.ToLowerInvariant())) | ForEach-Object { $_.ToString('x2') }) }
    finally { $hasher.Dispose() }
    ('\\.\pipe\invoice-assistant-private-' + $key.Substring(0, 16)) | Set-Content -LiteralPath $pipeFile -Encoding ASCII
}
function Assert-TailscaleSignature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Tailscale Inc\.,') {
        throw "Official Tailscale signature verification failed: $Path"
    }
}
$cliPath = Join-Path $binRoot 'tailscale.exe'
$daemonPath = Join-Path $binRoot 'tailscaled.exe'
if ((Test-Path -LiteralPath $cliPath) -and (Test-Path -LiteralPath $daemonPath)) {
    Assert-TailscaleSignature $cliPath
    Assert-TailscaleSignature $daemonPath
    Write-Output 'Private connection components are already prepared. In the app, choose Phone connection > Remote access.'
    exit 0
}
$scratch = Join-Path $runtimeRoot ('install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch | Out-Null
try {
    $msi = Join-Path $scratch 'tailscale.msi'
    Invoke-WebRequest -Uri 'https://pkgs.tailscale.com/stable/tailscale-setup-1.104.1-amd64.msi' -OutFile $msi -UseBasicParsing
    if ((Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash -ne '6DA8BCDB0D45A214CAE0E8A88C2987AD48DEC134BF04DFFB9C05F1354C02B0B4') {
        throw 'Tailscale package checksum verification failed.'
    }
    Assert-TailscaleSignature $msi
    $extract = Join-Path $scratch 'extracted'
    $installLog = Join-Path $scratch 'extract.log'
    # Administrative extraction unpacks files; it does not install a driver or service.
    $unpack = Start-Process msiexec.exe -ArgumentList @('/a', ('"' + $msi + '"'), '/qn', ('TARGETDIR="' + $extract + '"'), '/l*v', ('"' + $installLog + '"')) -PassThru -Wait -WindowStyle Hidden
    if ($unpack.ExitCode -ne 0) { throw "Tailscale extraction failed: $($unpack.ExitCode)" }
    foreach ($name in @('tailscale.exe', 'tailscaled.exe')) {
        $source = Join-Path $extract ('PFiles64\Tailscale\' + $name)
        Assert-TailscaleSignature $source
        Copy-Item -LiteralPath $source -Destination (Join-Path $binRoot $name)
    }
    Write-Output 'Prepared. Start the app, choose Phone connection > Remote access, and log in with your own account.'
} finally {
    $resolvedScratch = [IO.Path]::GetFullPath($scratch)
    $resolvedRoot = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\') + '\'
    if (-not $resolvedScratch.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unexpected cleanup path.' }
    Remove-Item -LiteralPath $resolvedScratch -Recurse -Force
}
