# Starts the silero v5_6_ru TTS sidecar (port 8092, separate .venv-tts in <project>\server).
# Stop: kill the python.exe process running tts_silero_server.py (stop-all.ps1 stops it too).
# ASCII-only: a .ps1 saved without BOM is read as ANSI by PowerShell 5.1.
$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$ServerDir  = Join-Path (Split-Path $ScriptsDir -Parent) 'server'
$env:PYTHONUTF8 = '1'
Start-Process -FilePath (Join-Path $ServerDir '.venv-tts\Scripts\python.exe') `
    -ArgumentList 'tts_silero_server.py' `
    -WorkingDirectory $ServerDir -WindowStyle Hidden
Write-Output 'silero sidecar starting (port 8092), warmup ~15 s'