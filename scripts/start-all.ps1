#Requires -Version 5.1
<#
    Autostart for the local AI phone assistant stack.

    Starts the whole stack, idempotently:
      1. llama-server (LLM) on port 8080        -> $LlamaStart (see below)
      2. assistant API (FastAPI) on port 8090   -> <project>\server\server.py
      3. silero TTS sidecar on port 8092        -> <project>\server\tts_silero_server.py
                                                   (.venv-tts, mirror of stop-all.ps1)
      4. tunnel (wss, TLS + token) on port 8443 -> <project>\server\tunnel.py

    Layout: python code + .venv + .env live in <project>\server\,
    this script lives in <project>\scripts\.

    "Stop the server" in this project means stopping ALL of them, so the
    autostart brings ALL back. Each one is skipped when its health check
    already answers, so re-running the script (or logging on twice) is harmless.

    NOTE: the kokoro-ru TTS sidecar (8091) is deliberately NOT in the
    autostart anymore (12.11.2026, prod TTS switched to silero v5_6_ru):
    kokoro eats ~0.9 GB RAM vs ~0.25 GB for silero. The tts_engine="kokoro"
    branch stays as a fallback; start the kokoro sidecar manually with
    scripts\start-kokoro.ps1 when needed (stop-all.ps1 still stops it).

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

function Test-Tcp([string]$Target, [int]$Port, [int]$TimeoutMs = 1500) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $ok = $c.ConnectAsync($Target, $Port).Wait($TimeoutMs)
        return ($ok -and $c.Connected)
    } catch {
        return $false
    } finally {
        $c.Close()
    }
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

# --- 3. silero TTS sidecar (8092) ------------------------------------------
# Health: GET /health -> {"status":"ok"} (same matcher as the API check).
# The sidecar loads the voice model BEFORE binding the port (warmup ~15 s),
# so a bare TCP/port check would race a still-warming instance and start a
# duplicate: also skip when a python process already runs
# tts_silero_server.py (same detection as stop-all.ps1).
$TtsPython  = Join-Path $ServerDir '.venv-tts\Scripts\python.exe'
$sileroBusy = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match 'tts_silero_server\.py'
    }
if (Test-Health 'http://127.0.0.1:8092/health') {
    Write-Host "[autostart] silero TTS already healthy on 8092 -- skip"
} elseif ($sileroBusy) {
    Write-Host "[autostart] silero TTS process already running (warming up) -- skip"
} elseif (Test-Path $TtsPython) {
    Write-Host "[autostart] starting silero TTS sidecar (warmup ~15 s)..."
    Start-Process -FilePath $TtsPython `
        -ArgumentList 'tts_silero_server.py' -WorkingDirectory $ServerDir `
        -WindowStyle Hidden | Out-Null
} else {
    Write-Host "[autostart] $TtsPython not found -- TTS NOT started (voice falls back to piper)"
}

# --- 4. tunnel (wss 8443) -------------------------------------------------
# Health check is a bare TCP connect to 8443 (the TLS handshake needs a
# client cert check we cannot do from here). Started only when its port is
# free; requires ASSISTANT_API_TOKEN in server\.env, otherwise it rejects
# every channel.
if (Test-Tcp '127.0.0.1' 8443) {
    Write-Host "[autostart] tunnel already listening on 8443 -- skip"
} elseif (Test-Path $Python) {
    Write-Host "[autostart] starting tunnel.py..."
    Start-Process -FilePath $Python `
        -ArgumentList 'tunnel.py' -WorkingDirectory $ServerDir `
        -WindowStyle Hidden | Out-Null
} else {
    Write-Host "[autostart] $Python not found -- tunnel NOT started"
}

Write-Host "[autostart] done"
