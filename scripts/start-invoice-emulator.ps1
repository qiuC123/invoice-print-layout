param(
    [string]$SdkRoot = 'E:/Tools/Android/sdk',
    [string]$AvdHome = 'E:/Tools/Android/avd'
)
$ErrorActionPreference = 'Stop'
$adb = Join-Path $SdkRoot 'platform-tools/adb.exe'
$emulator = Join-Path $SdkRoot 'emulator/emulator.exe'
foreach ($requiredPath in @($adb, $emulator, (Join-Path $AvdHome 'Invoice_Assistant_API35.ini'))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) { throw "Missing emulator dependency: $requiredPath" }
}
$env:ANDROID_HOME = $SdkRoot
$env:ANDROID_AVD_HOME = $AvdHome
$devices = & $adb devices
if (-not ($devices -match '^emulator-5560\s+device')) {
    $projectRoot = Split-Path $PSScriptRoot -Parent
    $logs = Join-Path $projectRoot 'workspace/android-emulator-runtime'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    Start-Process -FilePath $emulator -ArgumentList @('-avd','Invoice_Assistant_API35','-port','5560','-no-window','-no-audio','-no-boot-anim','-no-snapshot','-gpu','swiftshader','-memory','3072','-cores','2','-camera-back','emulated','-camera-front','none') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'emulator.stdout.log') -RedirectStandardError (Join-Path $logs 'emulator.stderr.log')
}
$deadline = (Get-Date).AddMinutes(2)
do {
    $boot = & $adb -s emulator-5560 shell getprop sys.boot_completed 2>$null
    if ($boot -match '^1') {
        # Emulator pointer events can trigger Gboard's floating handwriting panel.
        # Keep ordinary keyboard input available in this dedicated test device.
        & $adb -s emulator-5560 shell settings put secure stylus_handwriting_enabled 0
        if ($LASTEXITCODE -ne 0) { throw 'Could not disable emulator stylus handwriting' }
        & $adb -s emulator-5560 shell settings put secure show_ime_with_hard_keyboard 1
        if ($LASTEXITCODE -ne 0) { throw 'Could not enable the emulator soft keyboard' }
        Write-Output 'Invoice_Assistant_API35 ready: emulator-5560 (standard keyboard)'
        exit 0
    }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
throw 'The emulator has not finished booting; inspect workspace/android-emulator-runtime/emulator.stderr.log'
