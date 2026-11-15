#Requires -Version 5.1
<#
    watch-tts.ps1 -- watchdog for the TTS sidecars (tech debt D2).

    Problem: the TTS sidecars can die silently (OOM kill / MemoryError,
    incident 13.11 22:15 -- both sidecars gone, no traceback anywhere).
    The prod pipeline then degrades to the piper fallback
    (tts_first 2117-5680 ms) and nothing brings the sidecar back.
    stdout/stderr redirects (exp. fix 14.11) only record the death,
    they do not recover from it.

    Prod status (15.11.2026, user decision): ENABLED. start-all.ps1 starts
    this watchdog at the end (hidden); stop-all.ps1 stops it FIRST
    (otherwise it would resurrect the silero sidecar within ~3 intervals).
    Re-running start-all is harmless: the single-instance guard logs
    "watchdog-already-running" and starts nothing.

    Usage:
      scripts\watch-tts.ps1                   # watch silero :8092, poll every 60 s
      scripts\watch-tts.ps1 -IntervalSec 30   # different poll interval
      scripts\watch-tts.ps1 -AlsoPort 8091    # also watch kokoro :8091
      scripts\watch-tts.ps1 -Once             # one check cycle, for tests
      scripts\watch-tts.ps1 -Stop             # stop a running watchdog

    How it works:
      - Every cycle polls http://127.0.0.1:<port>/health (timeout 4 s,
        Invoke-RestMethod; any HTTP answer = alive).
      - 3 consecutive failed polls -> restart:
          port 8092 -> scripts\start-silero.ps1  (its own warmup + log redirect)
          port 8091 -> scripts\start-kokoro.ps1  (only via -AlsoPort, see below)
        After a restart (ok or fail) the degradation counter resets to zero,
        so the next restart needs 3 fresh consecutive failures.
      - Before invoking the start script a cmdline guard skips it when a
        python process for that sidecar already exists (it may just be
        warming up -- the sidecar binds the port only AFTER model warmup,
        same guard as start-all.ps1). The watchdog itself never kills
        anything. Consequence: a hung (alive but not answering) sidecar is
        reported in the log as repeating restart-skip lines, not restarted.
      - After triggering the start script the watchdog waits up to
        -RestartWaitSec (default 90 s) for /health (warmup ~15 s) and logs
        restart-ok / restart-fail. No other checks happen during that
        wait, so a warming sidecar is not double-started.
      - Consecutive FAILED restarts (restart-fail) are counted. From the
        3rd in a row the watchdog backs off exponentially (60 -> 120 ->
        240 s ..., cap 600 s) and logs ONE "restart-loop suspected" event
        per new count -- a permanently dead sidecar (e.g. broken model
        cache) then causes a few restarts per hour instead of a tight
        restart loop. A successful restart (or a healthy check) resets
        everything.

    kokoro is deliberately OFF since 14.11 (RAM policy, see NOTE in
    start-all.ps1). Therefore a port given via -AlsoPort is MONITOR-ONLY
    until it answers /health once ("armed"); only then will its death be
    restarted. So passing -AlsoPort 8091 while kokoro is down will NOT
    raise it by surprise; the default run (no -AlsoPort) never touches
    kokoro at all. Any port without a known restart script (not 8091/8092)
    is monitor-only too.

    Deliberate manual stop: the watchdog restarts any ARMED sidecar whose
    port does not answer. If you stop a sidecar on purpose, stop the
    watchdog FIRST (scripts\watch-tts.ps1 -Stop) or use stop-all.ps1 --
    since 15.11 stop-all stops the watchdog as its FIRST step, so a plain
    stop-all now keeps everything stopped. A manual Stop-Process of a
    sidecar without stopping the watchdog brings it back within ~3 poll
    intervals.

    -Stop stops ONLY the watchdog process itself. Discovery (per D2 spec):
    powershell processes whose command line has BOTH '-File' AND the
    'watch-tts.ps1' marker (Get-CimInstance Win32_Process, excluding the
    caller), PLUS the PID recorded by a running watchdog in
    logs\tts_watchdog.pid (alive + powershell/pwsh + the process
    CreationDate recorded at startup; the file also covers opaque launches
    such as -EncodedCommand; a CreationDate mismatch means the PID was
    REUSED by another process -- the file is STALE: removed, never killed
    from). A cmdline merely MENTIONING the script path (editor, agent
    harnesses, log greps) is not a watchdog: -Stop NEVER matches
    'server\.py' and never kills sidecars -- killing python.exe by a broad
    'server\.py' mask also kills tts_*_server.py sidecars (AGENTS.md
    incident 14.11 13:48). Use stop-all.ps1 for the sidecars.

    Own event log: logs\tts_watchdog.log (append, one line per event,
    timestamp + event). Events: check-ok / check-down / degrade N/3 /
    restart-triggered / restart-skip / restart-ok / restart-fail /
    restart-loop suspected / watchdog-started / watchdog-already-running /
    stop-request.
    It never writes to server.log -- that file is owned by server.py
    (its own rotation; a second writer would fight the rotation).

    ASCII-only on purpose: PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
    non-ASCII characters break the parser.
#>

param(
    [ValidateRange(1, 3600)][int]$IntervalSec = 60,
    [ValidateRange(1, 65535)][int]$Port = 8092,
    [ValidateRange(0, 65535)][int]$AlsoPort = 0,
    [ValidateRange(1, 3600)][int]$RestartWaitSec = 90,
    [switch]$Stop,
    [switch]$Once
)

$ErrorActionPreference = 'Continue'

$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$LogDir  = Join-Path (Split-Path $ScriptsDir -Parent) 'logs'
$LogFile = Join-Path $LogDir 'tts_watchdog.log'
$PidFile = Join-Path $LogDir 'tts_watchdog.pid'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Watch([string]$Message) {
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $LogFile -Value $line -Encoding ASCII
    Write-Host "[watch] $line"
}

function Get-WatchProcesses {
    # The watchdog process itself: a powershell running this script via
    # -File. Requires BOTH '-File' AND the watch-tts.ps1 marker in the
    # command line (never 'server\.py') -- so a cmdline that merely MENTIONS
    # the script (agent harness: powershell -Command "...watch-tts.ps1...",
    # editors, log greps) is not taken for a watchdog.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.Name -eq 'powershell.exe' -or $_.Name -eq 'pwsh.exe') -and
            $_.CommandLine -match '\-File' -and
            $_.CommandLine -match 'watch-tts\.ps1'
        }
}

function Get-WatchCandidates {
    # Every process that is REALLY a running watchdog: a powershell whose
    # own command line has '-File' + the watch-tts.ps1 marker, plus the PID
    # a running watchdog records in the .pid file TOGETHER WITH its process
    # CreationDate (also covers opaque launches, e.g. -EncodedCommand).
    # A CreationDate mismatch means the recorded PID was reused by another
    # process -- the pid file is STALE: the caller must not act on it
    # (guard ignores it and rewrites; -Stop removes it and kills only by
    # the cmdline marker). PidFileStale is also set for a missing/dead/
    # non-powershell recorded PID or a malformed file.
    param([int[]]$ExcludePid = @())
    $result = [pscustomobject]@{
        Candidates   = @()
        PidFileStale = $false
        PidFileSeen  = $false
    }
    $cands = @{}
    foreach ($p in @(Get-WatchProcesses)) {
        if ($ExcludePid -notcontains $p.ProcessId) { $cands[[int]$p.ProcessId] = $p }
    }
    if (Test-Path -LiteralPath $PidFile) {
        $result.PidFileSeen = $true
        $lines = @(Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue)
        $recorded = 0
        $stamp = ''
        if ($lines.Count -ge 1) { [void][int]::TryParse([string]$lines[0], [ref]$recorded) }
        if ($lines.Count -ge 2) { $stamp = ([string]$lines[1]).Trim() }
        if ($recorded -gt 0 -and $ExcludePid -notcontains $recorded -and -not $cands.ContainsKey($recorded)) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$recorded" -ErrorAction SilentlyContinue
            if ($p -and ($p.Name -eq 'powershell.exe' -or $p.Name -eq 'pwsh.exe') -and
                $stamp -eq $p.CreationDate.ToString('yyyyMMddHHmmss.fff')) {
                $cands[$recorded] = $p
            } else {
                $result.PidFileStale = $true
            }
        }
    }
    $result.Candidates = @($cands.Values)
    return $result
}

if ($Stop) {
    $res = Get-WatchCandidates -ExcludePid @($PID)
    $procs = @($res.Candidates)
    if (-not $procs) {
        Write-Watch 'stop-request: no watchdog process found'
    } else {
        foreach ($p in $procs) {
            Write-Watch "stop-request: killing watchdog PID $($p.ProcessId)"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    # Whatever the pid file describes (a killed watchdog or a reused PID)
    # is wrong after this stop: remove it. The reused-PID case is loud,
    # the foreign process it now points to was never touched.
    if ($res.PidFileSeen) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        if ($res.PidFileStale) {
            Write-Watch 'stop-request: stale pid file removed (pid reuse, foreign process NOT killed)'
        }
    }
    exit 0
}

# Target table: port -> restart script, sidecar cmdline-guard pattern, armed.
# 8092 (silero, prod TTS) is always armed: keeping it up is the whole point.
# 8091 (kokoro, deliberate-off) arms only after the first healthy answer.
function New-Target([int]$TargetPort) {
    $t = [pscustomobject]@{
        Port           = $TargetPort
        Script         = ''
        ProcPattern    = ''
        StderrLog      = ''
        Armed          = $false
        Degraded       = 0
        FailedRestarts = 0        # consecutive restart-fail (reset on restart-ok / check-ok)
        LoopLoggedN    = 0        # N of the last "restart-loop suspected" event (log once per new N)
        BackoffUntil   = $null    # backoff window after a failed restart at N >= 3
    }
    switch ($TargetPort) {
        8092 {
            $t.Script      = Join-Path $ScriptsDir 'start-silero.ps1'
            $t.ProcPattern = 'tts_silero_server\.py'
            $t.StderrLog   = 'logs\tts_silero.stderr.log'
            $t.Armed       = $true
        }
        8091 {
            $t.Script      = Join-Path $ScriptsDir 'start-kokoro.ps1'
            $t.ProcPattern = 'tts_kokoro_server\.py'
            $t.StderrLog   = 'logs\tts_kokoro.stderr.log'
            # Armed stays $false: monitor-only until first seen healthy.
        }
        default { } # no known restart script -> monitor-only
    }
    return $t
}

$Targets = @(New-Target $Port)
if ($AlsoPort -gt 0 -and $AlsoPort -ne $Port) { $Targets += @(New-Target $AlsoPort) }

function Test-SidecarHealth([int]$TargetPort) {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$TargetPort/health" `
            -TimeoutSec 4 -UseBasicParsing -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Wait-SidecarUp([int]$TargetPort, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        if (Test-SidecarHealth $TargetPort) { return $true }
    }
    return $false
}

function Get-RestartBackoff([int]$FailedRestarts) {
    # Backoff after consecutive FAILED restarts: 3rd failure -> 60 s, then
    # doubling (120, 240, ...), capped at 600 s (one start attempt per
    # 10 min at worst -- a permanently dead sidecar must not be restarted
    # in a tight loop).
    return [int][Math]::Min(600, 60 * [Math]::Pow(2, $FailedRestarts - 3))
}

function Invoke-Restart([object]$Target) {
    Write-Watch "restart-triggered port=$($Target.Port) (start $($Target.Script))"

    # Cmdline guard (same detection as stop-all.ps1 / start-all.ps1):
    # a live sidecar process means it is probably warming up (the port binds
    # after warmup) -- never start a duplicate over it.
    $guard = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -eq 'python.exe' -and $_.CommandLine -match $Target.ProcPattern
            }
    )
    if ($guard) {
        $ids = ($guard | ForEach-Object { $_.ProcessId }) -join ','
        Write-Watch "restart-skip port=$($Target.Port): sidecar process alive (PID $ids), likely warming up"
        $Target.Degraded = 0
        return
    }

    # Run the start script in a detached child powershell, ASYNCHRONOUSLY
    # (same pattern as start-all.ps1). A synchronous `& powershell.exe ... 2>&1`
    # would deadlock: the sidecar python spawned by start-silero.ps1 inherits
    # the child's stdout/stderr pipe write ends, so EOF never arrives and the
    # watchdog blocks on the pipe forever (observed live 14.11 15:32). The
    # child's own stdout/stderr messages are cosmetic; the real verification
    # of the restart is the Wait-SidecarUp health poll below.
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Target.Script `
        -WindowStyle Hidden | Out-Null
    $t0 = Get-Date
    if (Wait-SidecarUp $Target.Port $RestartWaitSec) {
        $sec = [int]((Get-Date) - $t0).TotalSeconds
        Write-Watch "restart-ok port=$($Target.Port) (health answered after ${sec}s)"
        $Target.FailedRestarts = 0
        $Target.LoopLoggedN = 0
        $Target.BackoffUntil = $null
    } else {
        Write-Watch "restart-fail port=$($Target.Port): no health within ${RestartWaitSec}s (check $($Target.StderrLog))"
        $Target.FailedRestarts = $Target.FailedRestarts + 1
        $n = $Target.FailedRestarts
        if ($n -ge 3) {
            $backoff = Get-RestartBackoff $n
            if ($n -gt $Target.LoopLoggedN) {
                # One loud event per new failed-restart count, not per cycle.
                Write-Watch "restart-loop suspected port=$($Target.Port) ($n failed restarts, backing off ${backoff}s)"
                $Target.LoopLoggedN = $n
            }
            $Target.BackoffUntil = (Get-Date).AddSeconds($backoff)
        }
    }
    $Target.Degraded = 0
}

# Single-instance guard (loop mode only; -Once must always work for tests):
# two watchdogs would race each other's restarts.
if (-not $Once) {
    $res = Get-WatchCandidates -ExcludePid @($PID)
    $others = @($res.Candidates)
    if ($others) {
        Write-Watch "watchdog-already-running PID $($others[0].ProcessId) -- not starting a second instance"
        exit 0
    }
    # A stale/reused pid file (if any) is simply overwritten by this
    # incarnation; the CreationDate line pins the pid to THIS process.
    $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue
    Set-Content -LiteralPath $PidFile -Value @($PID, $me.CreationDate.ToString('yyyyMMddHHmmss.fff')) -Encoding ASCII
    $also = ''
    if ($AlsoPort) { $also = " also=$AlsoPort" }
    Write-Watch "watchdog-started interval=${IntervalSec}s port=$Port$also"
}

do {
    foreach ($t in $Targets) {
        # Backoff window after a failed restart: no checks, no logging,
        # no restart attempts until the window expires.
        if ($null -ne $t.BackoffUntil -and (Get-Date) -lt $t.BackoffUntil) {
            continue
        }
        if (Test-SidecarHealth $t.Port) {
            if (-not $t.Armed) {
                $t.Armed = $true
                Write-Watch "check-ok port=$($t.Port) (armed: restarts enabled)"
            } else {
                $t.Degraded = 0
                Write-Watch "check-ok port=$($t.Port)"
            }
            # The sidecar is healthy: a failed-restart streak is over.
            $t.FailedRestarts = 0
            $t.LoopLoggedN = 0
            $t.BackoffUntil = $null
        } elseif (-not $t.Armed) {
            Write-Watch "check-down port=$($t.Port) (monitor-only: not armed, no restart)"
        } else {
            $t.Degraded = $t.Degraded + 1
            Write-Watch "degrade port=$($t.Port) $($t.Degraded)/3"
            if ($t.Degraded -ge 3) {
                if ($t.Script) {
                    Invoke-Restart $t
                } else {
                    Write-Watch "restart-skip port=$($t.Port): no restart script known for this port (monitor-only)"
                    $t.Degraded = 0
                }
            }
        }
    }
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSec
} while ($true)
