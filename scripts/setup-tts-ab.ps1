# setup-tts-ab.ps1 - idempotent setup of the isolated .venv-ab environment for
# the TTS A/B harness. Engines living here: Silero v5 (torch.hub,
# weights -> .cache\torch_hub, TORCH_HOME is set by the harness driver at run
# time, not here) and Supertonic 3 (PyPI package `supertonic`), plus driver
# deps (psutil, numpy).
#
# HARD RULES (feature plan):
#   * every pip operation is executed ONLY through .venv-ab\Scripts\python.exe;
#   * the existing .venv and .venv-tts are NEVER touched by this script
#     (no install/list/upgrade/delete against them);
#   * the kokoro sidecar is NOT started or stopped here;
#   * supertonic install/import failure is NOT fatal: the script still
#     finishes, prints the reason, and the engine will be marked "failed" on
#     harness runs (comparison stays valid on >= 3 engines).
#
# Re-run behavior: an existing healthy .venv-ab is reused (never recreated);
# already-installed packages are no-ops for pip; final import checks always run.
#
# Weight caches (all under <project>\server\.cache\, outside git; .cache/ is in
# .gitignore):
#   - supertonic: the PyPI package downloads weights via huggingface_hub
#     snapshot_download(local_dir=...) into its OWN cache dir (~/.cache/
#     supertonic3 by default), redirected here with SUPERTONIC_CACHE_DIR=
#     .cache\hf\supertonic3 so the weights land under .cache\hf. HF_HOME is
#     set as the hub-level cache root. (Verified in supertonic-py source:
#     supertonic/loader.py + config.py; HF_HOME alone does not move the
#     weight files.)
#   - silero: torch.hub -> .cache\torch_hub (driver sets TORCH_HOME later).

$env:PYTHONUTF8 = '1'

$ScriptsDir    = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$Root          = Join-Path (Split-Path $ScriptsDir -Parent) 'server'
$VenvDir       = Join-Path $Root '.venv-ab'
$VenvPython    = Join-Path $VenvDir 'Scripts\python.exe'
$CacheDir      = Join-Path $Root '.cache'
$TorchCpuIndex = 'https://download.pytorch.org/whl/cpu'

# Pinned versions - the combo proven in .venv-tts (pip list, 12.11.2026).
# omegaconf is an extra: hubconf of snakers4/silero-models declares only
# ["torch"], but silero_tts() imports OmegaConf at runtime
# (src/silero/silero.py) - required for the v5_6_ru speaker branch.
# psutil/omegaconf are pinned to the versions actually installed on
# 12.11.2026 (review follow-up, reproducibility).
$TorchVersion       = '2.15.0+cpu'
$OnnxruntimeVersion = '1.23.1'
$NumpyVersion       = '2.5.4'
$SoundfileVersion   = '0.14.0'
$PsutilVersion      = '7.2.2'
$OmegaconfVersion   = '2.4.0'

# Supertonic install sources, tried in order until `import supertonic` works:
# 1) PyPI: supertonic 1.3.1 (verified on PyPI 12.11.2026; pinned)
# 2) git+ SDK source at the SHA pinned by the archive README
#    (supertone-oss-archive/supertonic-py, main @ df0f9686..., 12.11.2026)
# 3) git+ main archive repo at HEAD SHA (supertone-oss-archive/supertonic,
#    main @ 1e9799e9..., 12.11.2026). NOTE: that repo root is the ONNX
#    examples repo, not a pip package - kept verbatim per the task plan,
#    expected to fail.
$SupertonicSources = @(
    'supertonic==1.3.1',
    'git+https://github.com/supertone-oss-archive/supertonic-py.git@df0f9686dac7fbbde391b759e2ee5286a3737622',
    'git+https://github.com/supertone-oss-archive/supertonic.git@1e9799e964ea4c0dad7cde993b65c3c813a7b373'
)

function Invoke-AbPip {
    # Runs pip ONLY inside .venv-ab, retries once on failure.
    # Returns $true on success, $false otherwise.
    param([string[]]$Packages, [string]$IndexUrl = '', [string]$Label)
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        if ($attempt -gt 1) {
            Write-Output "[setup-ab] retry ($Label): attempt 2/2"
            Start-Sleep -Seconds 3
        }
        $pkgArgs = @('-m', 'pip', 'install', '--disable-pip-version-check') + $Packages
        if ($IndexUrl) { $pkgArgs += @('--index-url', $IndexUrl) }
        & $VenvPython @pkgArgs
        if ($LASTEXITCODE -eq 0) { return $true }
    }
    Write-Output "[setup-ab] pip FAILED for: $Label"
    return $false
}

function Test-AbSupertonicImport {
    # `import supertonic` with stderr captured and PRINTED on failure, so the
    # "reason" promised in the summary is always visible in the log.
    # Returns $true when importable, $false otherwise.
    $importOut = (& $VenvPython -c "import supertonic" 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE -eq 0) { return $true }
    $importOut = ($importOut | Where-Object { $_ -ne '' }) -join "`n"
    if ($importOut) { Write-Output $importOut }
    return $false
}

Write-Output "[setup-ab] root: $Root"

# --- 1) venv (idempotent: healthy venv is reused, never recreated) ---
if (Test-Path $VenvPython) {
    Write-Output '[setup-ab] .venv-ab already exists - reusing (no recreation)'
} else {
    if (Test-Path $VenvDir) {
        # Never delete: a transiently false Test-Path (e.g. antivirus lock) must
        # not destroy a healthy multi-GB venv. Rename it aside instead; remove
        # the renamed copy manually only after confirming it is really junk.
        $stamp      = Get-Date -Format 'yyyyMMdd-HHmmss'
        $brokenName = '{0}.broken-{1}' -f (Split-Path $VenvDir -Leaf), $stamp
        Write-Output "[setup-ab] .venv-ab exists but has no python.exe - creating a new one; old directory renamed to $brokenName (delete it manually after confirming it is junk)"
        Rename-Item -LiteralPath $VenvDir -NewName $brokenName
    }
    Write-Output '[setup-ab] creating .venv-ab (py -3.12 -m venv)'
    py -3.12 -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Output '[setup-ab] BLOCKED: py -3.12 -m venv failed'
        exit 1
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Output '[setup-ab] BLOCKED: .venv-ab\Scripts\python.exe missing after creation'
        exit 1
    }
}
& $VenvPython --version

# --- 2) torch from the CPU index (mandatory; same as .venv-tts) ---
if (-not (Invoke-AbPip -Packages @("torch==$TorchVersion") -IndexUrl $TorchCpuIndex -Label "torch $TorchVersion")) {
    Write-Output '[setup-ab] BLOCKED: torch (CPU index) not installed after retry - STOP condition'
    exit 1
}

# --- 3) core deps (mandatory) ---
$corePackages = @(
    "onnxruntime==$OnnxruntimeVersion",
    "numpy==$NumpyVersion",
    "soundfile==$SoundfileVersion",
    "psutil==$PsutilVersion",
    "omegaconf==$OmegaconfVersion"
)
if (-not (Invoke-AbPip -Packages $corePackages -Label 'onnxruntime/numpy/soundfile/psutil/omegaconf')) {
    Write-Output '[setup-ab] BLOCKED: core deps not installed after retry - STOP condition'
    exit 1
}

# --- 4) supertonic (non-fatal; first source that imports wins) ---
if (Test-AbSupertonicImport) {
    Write-Output '[setup-ab] supertonic: import already ok (no-op)'
    $supertonicOk = $true
} else {
    $supertonicOk = $false
    foreach ($src in $SupertonicSources) {
        Write-Output "[setup-ab] supertonic: trying $src"
        if (Invoke-AbPip -Packages @($src) -Label $src) {
            if (Test-AbSupertonicImport) {
                Write-Output "[setup-ab] supertonic: OK from $src"
                $supertonicOk = $true
                break
            }
        }
    }
}
if ($supertonicOk) {
    Write-Output '[setup-ab] supertonic: installed and importable'
} else {
    Write-Output '[setup-ab] supertonic: FAILED (all sources) - reason is the import traceback above'
    Write-Output '[setup-ab] supertonic: engine will be marked FAILED on harness runs; comparison stays valid on >=3 engines'
}

# --- 5) lang="ru" auto-check (only if supertonic importable) ---
# Weights must land under <project>\server\.cache (outside git). The package has
# its own cache dir (SUPERTONIC_CACHE_DIR override), so both HF_HOME and
# SUPERTONIC_CACHE_DIR are set before the check process starts.
$env:HF_HOME              = Join-Path $CacheDir 'hf'
$env:SUPERTONIC_CACHE_DIR = (Join-Path $env:HF_HOME 'supertonic3')
$ruOk = $false
if ($supertonicOk) {
    $checkPy = Join-Path $env:TEMP 'check-supertonic-ru.py'
    # ASCII-only source; the Russian phrase is written as unicode escapes
    # so the check file has no encoding dependency.
    $checkSrc = @'
import os, sys, time
import numpy as np
try:
    from supertonic import TTS
    t0 = time.perf_counter()
    tts = TTS(auto_download=True)
    style = tts.get_voice_style(voice_name="M1")
    s1 = time.perf_counter()
    wav, duration = tts.synthesize(
        text="\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0441\u0432\u044f\u0437\u0438.",
        voice_style=style,
        lang="ru",
        verbose=False,
    )
    s2 = time.perf_counter()
    # duration may be a numpy array (per-chunk), not a scalar - normalize first
    dur = np.asarray(duration).reshape(-1)
    samples = int(np.asarray(wav).size)
    out_wav = os.path.join(os.environ.get("TEMP", "."), "supertonic-ru-check.wav")
    tts.save_audio(wav, out_wav)
    print("duration_type=%s shape=%s" % (type(duration).__name__, tuple(np.asarray(duration).shape)))
    print("RU_OK load=%.1fs synth=%.2fs duration=%.3fs samples=%d wav_bytes=%d" % (
        s1 - t0, s2 - s1, float(dur[0]) if dur.size else -1.0, samples, os.path.getsize(out_wav)))
except Exception as exc:
    import traceback
    print("RU_FAIL: %s: %s" % (type(exc).__name__, exc))
    traceback.print_exc()
    sys.exit(1)
'@
    [System.IO.File]::WriteAllText($checkPy, $checkSrc, [System.Text.UTF8Encoding]::new($false))
    Write-Output "[setup-ab] supertonic lang=ru check (HF_HOME=$env:HF_HOME, SUPERTONIC_CACHE_DIR=$env:SUPERTONIC_CACHE_DIR)"
    & $VenvPython $checkPy
    if ($LASTEXITCODE -ne 0) {
        Write-Output '[setup-ab] lang=ru check failed - retrying with the archive HF namespace (risk plan: Supertone/ -> supertone-oss-archive/)'
        $env:SUPERTONIC_MODEL_REPO     = 'supertone-oss-archive/supertonic-3'
        $env:SUPERTONIC_MODEL_REVISION = 'aafc6e32416a594460b32413efc49d7fe4ce6d46'
        & $VenvPython $checkPy
        # $null assignment removes these env vars for the rest of the script
        # (so the default repo/revision pinned in the package apply again).
        $env:SUPERTONIC_MODEL_REPO     = $null
        $env:SUPERTONIC_MODEL_REVISION = $null
    }
    $ruOk = ($LASTEXITCODE -eq 0)
    if ($ruOk) {
        Write-Output '[setup-ab] supertonic lang=ru: OK'
    } else {
        Write-Output '[setup-ab] supertonic lang=ru: FAILED - reason is the RU_FAIL traceback above'
    }
} else {
    Write-Output '[setup-ab] supertonic lang=ru: SKIPPED (supertonic not installed)'
}

# --- 6) final import verification (mandatory on every run) ---
& $VenvPython -c "import torch, onnxruntime, numpy, soundfile, psutil; print('deps ok')"
$depsOk = ($LASTEXITCODE -eq 0)

$verPy = Join-Path $env:TEMP 'ab_versions.py'
$verSrc = @'
import importlib.metadata as md
for name in ('torch', 'onnxruntime', 'numpy', 'soundfile', 'psutil', 'omegaconf', 'supertonic', 'huggingface-hub'):
    try:
        print(name + '==' + md.version(name))
    except Exception:
        print(name + '==NOT INSTALLED')
'@
[System.IO.File]::WriteAllText($verPy, $verSrc, [System.Text.UTF8Encoding]::new($false))
& $VenvPython $verPy

$weightsMB = 0
if (Test-Path $env:SUPERTONIC_CACHE_DIR) {
    $sum = (Get-ChildItem -Recurse -File $env:SUPERTONIC_CACHE_DIR -ErrorAction SilentlyContinue |
        Measure-Object Length -Sum).Sum
    if ($sum) { $weightsMB = [math]::Round($sum / 1MB, 1) }
}

Write-Output '=== setup-ab SUMMARY ==='
Write-Output "venv python:              $VenvPython"
Write-Output "deps ok (mandatory):      $depsOk"
Write-Output "supertonic import:        $supertonicOk"
Write-Output "supertonic lang=ru:       $ruOk"
Write-Output "supertonic weights dir:   $env:SUPERTONIC_CACHE_DIR ($weightsMB MB)"
if (-not $depsOk) {
    Write-Output '[setup-ab] FINAL CHECK FAILED - mandatory deps not importable'
    exit 1
}
Write-Output '[setup-ab] done'
exit 0
