#Requires -Version 5.1
<#
    Autostart for the local AI phone assistant stack.

    Starts BOTH servers, idempotently:
      1. llama-server (LLM) on port 8080  -> $LlamaStart (see below)
      2. assistant API (FastAPI) on port 8090 -> <project>\server\server.py

    Layout: python code + .venv + .env live in <project>\server\,
    this script lives in <project>\scripts\.

    "Stop the server" in this project means stopping BOTH, so the autostart
    brings BOTH back. Each one is skipped when its health check already
    answers, so re-running the script (or logging on twice) is harmless.

    NOTE: if 127.0.0.1:8080 is owned by another local service, the model
    health check must go through the LAN IP (same rule as switch-model.ps1).

    ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
    PowerShell 5.1, and non-ASCII characters in it break the parser.
#>

# Your own llama.cpp launcher; override with -LlamaStart <path> if needed.
param([string]$LlamaStart = '')

$ErrorActionPreference = 'SilentlyContinue'

$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$Root       = Split-Path $ScriptsDir -Parent
$ServerDir  = Join-Path $Root 'server'
$Python     = Join-Path $ServerDir '.venv\Scripts\python.exe'
if (-not $LlamaStart) {
    # Sibling of the project dir, e.g. <parent>\llm\start-model.ps1
    $LlamaStart = Join-Path (Split-Path $Root -Parent) 'llm\start-model.ps1'
}

function Get-LanIp {
    (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -like '192.168.*' } |
        Select-Object -First 1).IPAddress
}

function Test-Health([string]$Url) {
    $h = curl.exe -s --ssl-no-revoke --max-time 4 $Url 2>$null
    return ($h -match '"status"\s*:\s*"ok"')
}

$lan = Get-LanIp
if (-not $lan) {
    # No LAN yet (Wi-Fi not up) -- retry a few times before giving up.
    Start-Sleep -Seconds 10
    $lan = Get-LanIp
}

# --- 1. llama-server (model) --------------------------------------------
$modelUrl = if ($lan) { "http://${lan}:8080/health" } else { 'http://127.0.0.1:8080/health' }
if (Test-Health $modelUrl) {
    Write-Host "[autostart] model already healthy on 8080 -- skip"
} elseif (Test-Path $LlamaStart) {
    Write-Host "[autostart] starting llama-server..."
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-ExecutionPolicy', 'Bypass', '-NoProfile', '-WindowStyle', 'Hidden', '-File', $LlamaStart) `
        -WindowStyle Hidden | Out-Null
} else {
    Write-Host "[autostart] $LlamaStart not found -- model NOT started"
    Write-Host "[autostart] start llama-server yourself and re-run if needed"
}

# --- 2. assistant API ----------------------------------------------------
if (Test-Health 'http://127.0.0.1:8090/health') {
    Write-Host "[autostart] API already healthy on 8090 -- skip"
} elseif (Test-Path $Python) {
    Write-Host "[autostart] starting server.py..."
    Start-Process -FilePath $Python `
        -ArgumentList 'server.py' -WorkingDirectory $ServerDir `
        -WindowStyle Hidden | Out-Null
} else {
    Write-Host "[autostart] $Python not found -- API NOT started"
}

Write-Host "[autostart] done"
