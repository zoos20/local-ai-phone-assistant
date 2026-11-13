#Requires -Version 5.1
<#
    Stops the whole assistant stack (mirror of start-all.ps1):
      1. llama-server (LLM, port 8080)
      2. assistant API (FastAPI, port 8090)
      3. kokoro-ru TTS sidecar (port 8091, manual start)
      4. silero TTS sidecar (port 8092)
      5. tunnel (wss, port 8443)

    Processes are found by command line (llama-server.exe, server.py,
    tts_kokoro_server.py, tts_silero_server.py, tunnel.py), NOT by port
    alone -- other local services that happen to use the same ports are
    never touched. Stray duplicates of server.py / tts_kokoro_server.py /
    tts_silero_server.py are stopped too.

    Idempotent: things that are not running are reported as "not running".
    Run with -WhatIf to see what would be stopped without stopping it.

    ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
    PowerShell 5.1, and non-ASCII characters in it break the parser.
#>

param([switch]$WhatIf)

$ErrorActionPreference = 'SilentlyContinue'

function Stop-Matching([string]$Label, [string]$ExeName, [string]$Pattern) {
    # Match by exe name; add a command-line pattern (python + target script)
    # when several python processes are around and only ours must be hit.
    $procs = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -eq $ExeName -and
            (-not $Pattern -or $_.CommandLine -match $Pattern)
        }
    if (-not $procs) {
        Write-Host "[stop] $Label : not running"
        return
    }
    foreach ($p in $procs) {
        if ($WhatIf) {
            Write-Host "[stop] $Label : would kill PID $($p.ProcessId) ($($p.Name))"
        } else {
            Write-Host "[stop] $Label : killing PID $($p.ProcessId) ($($p.Name))"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

# 1. llama-server (LLM) -- by exe name only
Stop-Matching 'llama-server (8080)' 'llama-server.exe' $null

# 2. assistant API -- any python whose command line runs server.py
Stop-Matching 'assistant API (8090)' 'python.exe' '(^|[\s"\\])server\.py\s*$'

# 3. kokoro TTS sidecar -- manual start (start-kokoro.ps1); the line is
#    kept so a manually started kokoro is stopped too
Stop-Matching 'kokoro TTS (8091)' 'python.exe' 'tts_kokoro_server\.py'

# 4. silero TTS sidecar (prod TTS)
Stop-Matching 'silero TTS (8092)' 'python.exe' 'tts_silero_server\.py'

# 5. tunnel (wss 8443)
Stop-Matching 'tunnel (8443)' 'python.exe' 'tunnel\.py'

if ($WhatIf) {
    Write-Host '[stop] what-if mode -- nothing was stopped'
    exit 0
}

# Give the OS a moment, then report which ports are actually free.
Start-Sleep -Seconds 1
foreach ($port in 8080, 8090, 8091, 8092, 8443) {
    $left = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($left) {
        $owner = ($left | Select-Object -First 1).OwningProcess
        Write-Host "[stop] port $port still has a listener (PID $owner)"
    } else {
        Write-Host "[stop] port $port free"
    }
}

Write-Host '[stop] done'
