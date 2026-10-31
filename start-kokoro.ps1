# Starts the kokoro-ru TTS sidecar (port 8091, separate .venv-tts).
# Stop: kill the python.exe process running tts_kokoro_server.py.
# ASCII-only: a .ps1 saved without BOM is read as ANSI by PowerShell 5.1.
$Here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$env:PYTHONUTF8 = '1'
Start-Process -FilePath (Join-Path $Here '.venv-tts\Scripts\python.exe') `
    -ArgumentList 'tts_kokoro_server.py' `
    -WorkingDirectory $Here -WindowStyle Hidden
Write-Output 'kokoro sidecar starting (port 8091), warmup ~10 s'
