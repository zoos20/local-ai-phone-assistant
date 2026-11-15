# Starts the kokoro-ru TTS sidecar (port 8091, separate .venv-tts in <project>\server).
# Stop: kill the python.exe process running tts_kokoro_server.py (stop-all.ps1 stops it too).
#
# stdout/stderr go to logs\tts_kokoro.stdout.log / logs\tts_kokoro.stderr.log
# so a silent death (OOM kill, MemoryError traceback) leaves a trace on disk.
# Start-Process cannot merge both streams into ONE file, hence two files.
# Every start rotates the previous run to *.old.log (one .old generation,
# no unbounded growth). Run only while the sidecar is NOT running: a live
# process holds its log open (the rotation rename fails with a visible
# error). kokoro does NOT check the port before bind (unlike silero's
# _port_in_use): a duplicate instance will run alongside the live one
# (SO_REUSEADDR on Windows allows re-bind). Ensure it is stopped first
# (scripts\stop-all.ps1 kills python.exe running tts_kokoro_server.py).
# ASCII-only on purpose: PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
# non-ASCII characters break the parser.
$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$ServerDir  = Join-Path (Split-Path $ScriptsDir -Parent) 'server'
$env:PYTHONUTF8 = '1'
$LogDir = Join-Path $ServerDir 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
foreach ($f in 'tts_kokoro.stdout.log', 'tts_kokoro.stderr.log') {
    $p = Join-Path $LogDir $f
    $old = $p -replace '\.log$', '.old.log'
    if (Test-Path -LiteralPath $p) { Move-Item -LiteralPath $p -Destination $old -Force }
}
Start-Process -FilePath (Join-Path $ServerDir '.venv-tts\Scripts\python.exe') `
    -ArgumentList 'tts_kokoro_server.py' `
    -WorkingDirectory $ServerDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $LogDir 'tts_kokoro.stdout.log') `
    -RedirectStandardError (Join-Path $LogDir 'tts_kokoro.stderr.log')
Write-Output 'kokoro sidecar starting (port 8091), warmup ~10 s'
Write-Output 'stdout/stderr: logs\tts_kokoro.stdout.log / logs\tts_kokoro.stderr.log'
