# run-tts-ab.ps1 - thin wrapper for the TTS A/B harness.
# Runs the driver CLI under .venv-ab (created in <project>\server by
# setup-tts-ab.ps1):
#   1) run   - sequential engine subprocesses -> data\tts_ab\results.json
#              (+ blind files and report.md at the end of the run);
#   2) blind - rebuild blind files / KEY.txt / form.md (no re-synthesis);
#   3) report- rebuild data\tts_ab\report.md (no re-synthesis).
# Steps 2-3 are also runnable separately (.venv-ab\Scripts\python -m tts_ab
# blind|report) and are re-run here so parameter changes (-BlindSubset)
# take effect without a new synthesis run.
#
# HARD RULES:
#   * NO pip operations here (environment is created by setup-tts-ab.ps1);
#   * the kokoro sidecar is NOT started or stopped (user runs
#     start-kokoro.ps1 first; if it is down, kokoro is marked
#     "unavailable" and the run continues);
#   * all child processes get PYTHONUTF8=1 (Windows cp1251 protection).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\run-tts-ab.ps1 `
#       [-Engines all|kokoro,piper,silero,supertonic] `
#       [-Voices m,f] [-Repeats 1] [-BlindSubset 5]

param(
    [string]$Engines = 'all',
    [string]$Voices = 'm,f',
    [int]$Repeats = 1,
    [int]$BlindSubset = 5
)

$ErrorActionPreference = 'Continue'
$env:PYTHONUTF8 = '1'

# The harness package (server\tts_ab) and its venvs live in <project>\server.
$ScriptsDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$Root       = Join-Path (Split-Path $ScriptsDir -Parent) 'server'
$VenvPython = Join-Path $Root '.venv-ab\Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Output "[run-tts-ab] BLOCKED: .venv-ab\Scripts\python.exe not found"
    Write-Output "[run-tts-ab] create the environment first: scripts\setup-tts-ab.ps1"
    exit 1
}

Write-Output "[run-tts-ab] run: engines=$Engines voices=$Voices repeats=$Repeats subset=$BlindSubset"

# Driver must run with cwd = server (BASE_DIR of the tts_ab package; results
# land in server\data\tts_ab, weights cache in server\.cache).
Push-Location $Root
try {
    & $VenvPython -m tts_ab run --engines $Engines --voices $Voices --repeats $Repeats --subset $BlindSubset
    $runExit = $LASTEXITCODE

    # blind + report separately (no re-synthesis; results.json must exist)
    Write-Output "[run-tts-ab] blind (rebuild, no synthesis)"
    & $VenvPython -m tts_ab blind --subset $BlindSubset
    $blindExit = $LASTEXITCODE

    Write-Output "[run-tts-ab] report (rebuild, no synthesis)"
    & $VenvPython -m tts_ab report
    $reportExit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($runExit -ne 0) { exit $runExit }
if ($blindExit -ne 0) { exit $blindExit }
exit $reportExit