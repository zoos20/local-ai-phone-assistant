#Requires -Version 5.1
<#
  Build the debug APK and publish it for wireless update (one command).

    1. gradlew assembleDebug -> android/app/build/outputs/apk/debug/app-debug.apk
    2. copy the APK into the releases dir (settings.apk_dir)
    3. write <releases>/manifest.json: version_code, version_name, file,
       size_bytes, sha256, released_at

  server.py serves the result over LAN: GET /update/manifest + GET /update/apk.

  The releases dir is resolved exactly like config.py (apk_dir):
    -ApkDir argument -> ASSISTANT_APK_DIR env -> ASSISTANT_APK_DIR in
    <root>\.env -> <root>\releases
  An empty value means "default" (config.py _empty_apk_dir); a present env
  variable wins over .env even when empty (pydantic-settings priority). A
  leading ~ is expanded to the user profile and a relative path is resolved
  against the project root, not the cwd (config.py _resolve_apk_dir).

  ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
  PowerShell 5.1, and non-ASCII characters in it break the parser.
#>
[CmdletBinding()]
param(
    # Override the releases dir (skips ASSISTANT_APK_DIR from .env).
    [string]$ApkDir = ''
)

$ErrorActionPreference = 'Stop'

$Root       = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$AndroidDir = Join-Path $Root 'android'
$Gradlew    = Join-Path $AndroidDir 'gradlew.bat'
$GradleKts  = Join-Path $AndroidDir 'app\build.gradle.kts'
$BuiltApk   = Join-Path $AndroidDir 'app\build\outputs\apk\debug\app-debug.apk'

# --- resolve the releases dir (same precedence as config.py) ---------------
# -ApkDir (CLI) wins; otherwise: env ASSISTANT_APK_DIR > <root>\.env > default.
if (-not $ApkDir) {
    $envVal = [Environment]::GetEnvironmentVariable('ASSISTANT_APK_DIR')
    if ($null -ne $envVal) {
        # Present in the environment (even empty): pydantic-settings gives env
        # priority over .env, so .env is not consulted. An empty value falls
        # through to the default below (config.py _empty_apk_dir).
        $ApkDir = $envVal
    }
    else {
        $envFile = Join-Path $Root '.env'
        if (Test-Path $envFile) {
            foreach ($line in Get-Content $envFile) {
                if ($line -match '^\s*ASSISTANT_APK_DIR\s*=\s*(.*?)\s*$') {
                    $ApkDir = $Matches[1].Trim().Trim('"').Trim("'")
                    break
                }
            }
        }
    }
}
if (-not $ApkDir) { $ApkDir = 'releases' }
# Expand a leading ~ to the user profile (config.py: Path.expanduser()).
if ($ApkDir -match '^~(?=$|[/\\])') {
    $rest = $ApkDir.Substring(1) -replace '^[/\\]', ''
    $ApkDir = if ($rest) { Join-Path $env:USERPROFILE $rest } else { $env:USERPROFILE }
}
# A relative path is resolved against the project root, not the cwd
# (config.py: BASE_DIR / p).
if (-not [IO.Path]::IsPathRooted($ApkDir)) { $ApkDir = Join-Path $Root $ApkDir }

# --- 1. build ---------------------------------------------------------------
if (-not (Test-Path $Gradlew)) {
    Write-Host "[build-apk] gradlew.bat not found: $Gradlew" -ForegroundColor Red
    exit 1
}
if (-not $env:JAVA_HOME) {
    Write-Host "[build-apk] WARNING: JAVA_HOME is not set (JDK 17 required, see README)" -ForegroundColor Yellow
}

Write-Host "[build-apk] gradlew assembleDebug..."
Push-Location $AndroidDir
try {
    & .\gradlew.bat assembleDebug
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($code -ne 0) {
    Write-Host "[build-apk] BUILD FAILED (gradlew exit $code) - releases dir NOT updated" -ForegroundColor Red
    exit $code
}
if (-not (Test-Path $BuiltApk)) {
    Write-Host "[build-apk] build succeeded but APK missing: $BuiltApk" -ForegroundColor Red
    exit 1
}

# --- 2. version from build.gradle.kts --------------------------------------
$kts = Get-Content $GradleKts -Raw
$vc = [regex]::Match($kts, 'versionCode\s*=\s*(\d+)').Groups[1].Value
$vn = [regex]::Match($kts, 'versionName\s*=\s*"([^"]+)"').Groups[1].Value
if (-not $vc -or -not $vn) {
    Write-Host "[build-apk] cannot parse versionCode/versionName from $GradleKts" -ForegroundColor Red
    exit 1
}

# --- 3. publish APK + manifest.json ----------------------------------------
New-Item -ItemType Directory -Force -Path $ApkDir | Out-Null
$apkName = 'app-debug.apk'
$destApk = Join-Path $ApkDir $apkName
$jsonPath = Join-Path $ApkDir 'manifest.json'
# Remove the manifest BEFORE copying the APK (the copy of ~70 MB takes
# seconds): while the copy runs, the server must answer 404 "manifest.json
# not found" instead of serving the old manifest against a half-written APK.
if (Test-Path $jsonPath) { Remove-Item $jsonPath -Force }
Copy-Item $BuiltApk $destApk -Force

$item = Get-Item $destApk
$sha  = (Get-FileHash -Path $destApk -Algorithm SHA256).Hash.ToLowerInvariant()
$manifest = [ordered]@{
    version_code = [int]$vc
    version_name = $vn
    file         = $apkName
    size_bytes   = [long]$item.Length
    sha256       = $sha
    released_at  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}
# UTF-8 without BOM: the phone parses it as plain JSON.
[IO.File]::WriteAllText($jsonPath, ($manifest | ConvertTo-Json) + "`n",
    (New-Object Text.UTF8Encoding($false)))

Write-Host "[build-apk] OK  $apkName  v$vn (code $vc), $($item.Length) bytes" -ForegroundColor Green
Write-Host "[build-apk]     sha256 $sha"
Write-Host "[build-apk]     manifest: $jsonPath"
