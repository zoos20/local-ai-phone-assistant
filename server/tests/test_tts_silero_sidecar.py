# -*- coding: utf-8 -*-
"""Смоук tts_silero_server (sidecar silero v5_6_ru, порт 8092).

Модуль грузит ru_normalizr/torch-стек, которого нет в основном .venv, --
проверки выполняются подсессией (.venv-tts/Scripts/python.exe). Тяжёлый
тест синтеза требует весов v5_6_ru.pt в кэше torch_hub (gitignored).
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON_TTS = ROOT / ".venv-tts" / "Scripts" / "python.exe"
SILERO_WEIGHTS = (ROOT / ".cache" / "torch_hub" / "hub"
                  / "snakers4_silero-models_master" / "src" / "silero"
                  / "model" / "v5_6_ru.pt")

pytestmark = [
    pytest.mark.skipif(not PYTHON_TTS.exists(), reason=".venv-tts is absent"),
    pytest.mark.skipif(not SILERO_WEIGHTS.exists(),
                       reason="cached v5_6_ru.pt is absent (gitignored)"),
]


def _run(code: str, timeout: int = 600) -> str:
    """Гоняет snippet в .venv-tts; при ошибке падает со stderr."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [str(PYTHON_TTS), "-c", code],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        env=env,
        timeout=timeout,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return proc.stdout


def test_import_silero_sidecar_light():
    """Импорт модуля не грузит модель, не занимает порт и завершается."""
    out = _run(
        """import socket

def busy():
    s = socket.socket()
    s.settimeout(1.0)
    code = s.connect_ex(('127.0.0.1', 8092))
    s.close()
    return code == 0

before = busy()
import tts_silero_server as m
after = busy()
assert before == after, f'import changed port state: {before} -> {after}'
assert m._model is None, 'model loaded on import'
print('OK')"""
    )
    assert "OK" in out


def test_constants():
    """Константы sidecar: порт/сэмпрейт/голоса/дефолт/лимит."""
    out = _run(
        """import tts_silero_server as m
assert m.PORT == 8092, m.PORT
assert m.SAMPLE_RATE == 48000, m.SAMPLE_RATE
assert m.VOICES == ('aidar', 'baya', 'eugene', 'kseniya', 'xenia'), m.VOICES
assert m.DEFAULT_VOICE == 'eugene', m.DEFAULT_VOICE
assert m.MAX_CHARS == 2000, m.MAX_CHARS
print('OK')"""
    )
    assert "OK" in out


def test_normalizr_composition_fixes_digits():
    """КЛЮЧЕВОЙ: normalize_text разворачивает цифры/латиницу словами."""
    out = _run(
        """import re
import tts_silero_server as m

a = m.normalize_text('Встреча в 14:30 на улице Ленина.')
expected = 'Встреча в четырнадцать часов тридцать минут на улице Ленина.'
if a != expected:
    print('FACT time:', repr(a))
    assert not re.search(r'[0-9]', a), f'digits left: {a!r}'
    assert 'четырнадцать' in a and 'тридцать' in a, f'words missing: {a!r}'

b = m.normalize_text('Ошибка HTTP 404, но Wi-Fi работает.')
if b != 'Ошибка эйч ти ти пи четыреста четыре, но вайфай работает.':
    print('FACT latin:', repr(b))
assert not re.search(r'[0-9]', b), f'digits left: {b!r}'
assert 'HTTP' not in b and 'Wi-Fi' not in b, f'latin left: {b!r}'

c = m.normalize_text('Готово, держи!')
assert c == 'Готово, держи!', f'plain text changed: {c!r}'
print('OK')"""
    )
    assert "OK" in out


def test_synth_digits_n1_48k_duration():
    """Тяжёлый (модель из кэша): eugene читает цифры, WAV 48k >= 2.5 с."""
    out = _run(
        """import io, wave
import tts_silero_server as m
m.load()
wav_bytes = m.synthesize('Встреча в 14:30 на улице Ленина.', 'eugene')
with wave.open(io.BytesIO(wav_bytes)) as w:
    rate = w.getframerate()
    channels = w.getnchannels()
    width = w.getsampwidth()
    duration = w.getnframes() / w.getframerate()
assert rate == 48000, rate
assert channels == 1, channels
assert width == 2, width
assert duration >= 2.5, f'duration {duration:.2f}s < 2.5s'
print(f'DURATION {duration:.2f}')
print('OK')"""
    )
    assert "DURATION" in out
    assert "OK" in out


def test_synth_short_and_unknown_voice():
    """Неизвестный голос -> eugene (WAV валиден); пустой -> ValueError."""
    out = _run(
        """import io, wave
import tts_silero_server as m
m.load()
wav_bytes = m.synthesize('Готово, держи!', 'неизвестный')
with wave.open(io.BytesIO(wav_bytes)) as w:
    assert w.getframerate() == 48000, w.getframerate()
    assert w.getnframes() > 0, w.getnframes()
try:
    m.synthesize('')
except ValueError:
    print('EMPTY ValueError')
else:
    raise AssertionError('empty text must raise ValueError')
print('OK')"""
    )
    assert "EMPTY ValueError" in out
    assert "OK" in out


def test_no_network_when_cache_missing():
    """CWE-494: пустой кэш -> SystemExit(1) ДО torch.hub.load (стаб
    load бросил бы RuntimeError 'NETWORK CALL' -- до него не доходит);
    фактический кэш -> функция проходит без сетевых вызовов."""
    missing = _run(
        """import tempfile
import torch

def _network(*args, **kwargs):
    raise RuntimeError('NETWORK CALL')

torch.hub.get_dir = lambda: tempfile.mkdtemp(prefix='empty_hub-')
torch.hub.load = _network
import tts_silero_server as m
try:
    m._ensure_cached_repo()
except SystemExit as exc:
    assert exc.code == 1, exc.code
else:
    raise AssertionError('empty cache must SystemExit(1)')
print('OK')"""
    )
    assert "OK" in missing

    cached = _run(
        """import torch
import tts_silero_server as m

def _network(*args, **kwargs):
    raise RuntimeError('NETWORK CALL')

torch.hub.get_dir = lambda: str(m.BASE_DIR / '.cache' / 'torch_hub' / 'hub')
torch.hub.load = _network
m._ensure_cached_repo()
print('OK')"""
    )
    assert "OK" in cached
