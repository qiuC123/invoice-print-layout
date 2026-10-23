param(
    [Parameter(Mandatory=$true)][string]$SdkRoot,
    [Parameter(Mandatory=$true)][string]$JavaHome,
    [string]$GradleHome,
    [switch]$ConnectedTests
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $SdkRoot 'platform-tools/adb.exe'))) { throw 'Android SDK platform-tools missing' }
if (-not (Test-Path -LiteralPath (Join-Path $JavaHome 'bin/javac.exe'))) { throw 'A complete compatible JDK is required' }
$fontDir = Join-Path $projectRoot 'app/src/main/assets/fonts'
$fontPath = Join-Path $fontDir 'NotoSansSC.ttf'
New-Item -ItemType Directory -Path $fontDir -Force | Out-Null
if (-not (Test-Path -LiteralPath $fontPath)) {
    Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf' -OutFile $fontPath
}
if ((Get-FileHash -LiteralPath $fontPath -Algorithm SHA256).Hash -ne 'A3041811A78C361B1DE50F953C805E0244951C21C5BD412F7232EF0D899AF0DA') { throw 'Font source changed; verify and update its pinned hash before building' }
$oldJava = $env:JAVA_HOME
$oldSdk = $env:ANDROID_HOME
try {
    $env:JAVA_HOME = $JavaHome
    $env:ANDROID_HOME = $SdkRoot
    Set-Content -LiteralPath (Join-Path $projectRoot 'local.properties') -Value ('sdk.dir=' + $SdkRoot.Replace('\','/')) -Encoding utf8
    $gradleCommand = if ($GradleHome) { Join-Path $GradleHome 'bin/gradle.bat' } else { Join-Path $projectRoot 'gradlew.bat' }
    $tasks = @(':app:assembleDebug', ':app:testDebugUnitTest', ':app:lintDebug')
    if ($ConnectedTests) { $tasks += ':app:connectedDebugAndroidTest' }
    & $gradleCommand -p $projectRoot @tasks --console=plain
    if ($LASTEXITCODE -ne 0) { throw "Gradle exited $LASTEXITCODE" }
} finally {
    $env:JAVA_HOME = $oldJava
    $env:ANDROID_HOME = $oldSdk
}
