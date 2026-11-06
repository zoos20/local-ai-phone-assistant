"""Фаза 4.1: убран глобальный monkey-patch pathlib в tts_kokoro_server.

Модуль импортирует torch/kokoro/ru_g2p, которых нет в основном .venv, --
поэтому проверки выполняются подсессией (.venv-tts/Scripts/python.exe).
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON_TTS = ROOT / ".venv-tts" / "Scripts" / "python.exe"
G2P_CONFIG = ROOT / "models" / "kokoro_ru" / "kokoro-config.json"

pytestmark = [
    pytest.mark.skipif(not PYTHON_TTS.exists(), reason=".venv-tts is absent"),
    pytest.mark.skipif(not G2P_CONFIG.exists(),
                       reason="models/kokoro_ru is absent (gitignored)"),
]


def _run(code: str) -> str:
    """Гоняет snippet в .venv-tts; при ошибке падает со stderr."""
    proc = subprocess.run(
        [str(PYTHON_TTS), "-c", code],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return proc.stdout


def test_import_does_not_patch_pathlib_read_text():
    """Импорт tts_kokoro_server не оставляет Path.read_text подменённым."""
    out = _run(
        """import pathlib
before = pathlib.Path.read_text
import tts_kokoro_server as m
assert pathlib.Path.read_text is before, 'read_text patched'
assert not hasattr(m, '_orig_read_text'), 'legacy helper left'
assert not hasattr(m, '_utf8_read_text'), 'legacy helper left'
print('OK')"""
    )
    assert "OK" in out


def test_vocab_config_copy_is_ascii_and_cp1251_safe():
    """Копия конфига для RuG2P: чистый ASCII, cp1251-чтение == utf-8-оригиналу."""
    out = _run(
        """import json
import tts_kokoro_server as m
copy = m._vocab_config_copy()
try:
    raw = copy.read_bytes()
    assert max(raw) < 128, 'non-ASCII byte in copy'
    decoded = json.loads(raw.decode('cp1251'))
    cfg_path = m.MODELS / 'kokoro-config.json'
    original = json.loads(cfg_path.read_text(encoding='utf-8'))
    assert decoded == original, 'copy != original config'
finally:
    copy.unlink(missing_ok=True)
print('OK')"""
    )
    assert "OK" in out
