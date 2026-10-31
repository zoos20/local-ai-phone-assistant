# Starts the kokoro-ru TTS sidecar (port 8091, separate .venv-tts in <project>\server).
# Stop: kill the python.exe process running tts_kokoro_server.py.
# ASCII-only: a .ps1 saved without BOM is read as ANSI by PowerShell 5.1.
$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$ServerDir  = Join-Path (Split-Path $ScriptsDir -Parent) 'server'
$env:PYTHONUTF8 = '1'
Start-Process -FilePath (Join-Path $ServerDir '.venv-tts\Scripts\python.exe') `
    -ArgumentList 'tts_kokoro_server.py' `
    -WorkingDirectory $ServerDir -WindowStyle Hidden
Write-Output 'kokoro sidecar starting (port 8091), warmup ~10 s'
