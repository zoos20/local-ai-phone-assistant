# -*- coding: utf-8 -*-
"""kokoro-ru sidecar: HTTP-сервис озвучки в отдельном окружении (.venv-tts).

Запуск (см. start-kokoro.ps1):
    <project>\server\.venv-tts\\Scripts\\python.exe tts_kokoro_server.py

Модель (torch CPU, 82M, голос dima по умолчанию) грузится один раз при
старте и живёт в процессе; server.py шлёт сюда нормализованный текст и
получает WAV 24 kHz. Порт 8091 (8080 — llama, 8090 — server.py).

Эндпоинты:
    GET  /health          -> {"status":"ok","voice":...}
    POST /synth {"text"}  -> WAV-байты (24 kHz, mono, PCM16)
"""
from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ASCII-кэши ДО импорта torch/hf (дом с кириллицей ломает C++-ридеры)
import os

os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parent / ".cache" / "hf"))
os.environ.setdefault("TORCH_HOME", str(Path(__file__).resolve().parent / ".cache" / "torch_hub"))

BASE_DIR = Path(__file__).resolve().parent
MODELS = BASE_DIR / "models" / "kokoro_ru"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "kokoro_sidecar.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("kokoro.sidecar")

sys.path.insert(0, str(MODELS))            # ru_g2p.py + espeak-data рядом
import soundfile as sf                     # noqa: E402
import torch                               # noqa: E402

torch.hub.set_dir(str(BASE_DIR / ".cache" / "torch_hub"))

from kokoro import KModel                  # noqa: E402
from ru_g2p import RuG2P                   # noqa: E402

# (чекпоинт, voicepack): sveta/masha делят base, dima -- отдельный
VOICES = {
    "sveta": ("kokoro-ru-v2-base.pth", "sveta.pt"),
    "masha": ("kokoro-ru-v2-base.pth", "masha.pt"),
    "dima": ("kokoro-ru-v2-dima.pth", "dima.pt"),
}
DEFAULT_VOICE = "dima"
SAMPLE_RATE = 24000
MAX_CHARS = 2000

_lock = threading.Lock()
_models: dict = {}        # чекпоинт -> KModel (кэш: base + dima -- 2 шт)
_packs: dict = {}         # имя голоса -> voicepack (лёгкие тензоры)
_g2p = None
_voice = DEFAULT_VOICE    # последний запрошенный (для /health)


def _ensure_voice(voice: str):
    """Ленивая загрузка модели/voicepack с кэшем (вызывается под lock).

    Связка «профиль <-> голос» (план): у каждого профиля свой голос, а
    sidecar раньше умел только один голос на процесс. Переключение на
    уже загруженный голос -- мгновенно; первый запрос новым голосом --
    с загрузкой чекпоинта (~секунды), после -- кэш.
    """
    global _voice
    ckpt, pack_name = VOICES[voice]
    if ckpt not in _models:
        log.info("загрузка чекпоинта '%s'...", ckpt)
        _models[ckpt] = KModel(repo_id="zaakirio/kokoro-ru",
                               model=str(MODELS / ckpt)).eval()
        log.info("чекпоинт '%s' готов", ckpt)
    if voice not in _packs:
        _packs[voice] = torch.load(str(MODELS / "voices" / pack_name),
                                   map_location="cpu", weights_only=True)
    _voice = voice
    return _models[ckpt], _packs[voice]


def _vocab_config_copy() -> Path:
    """ASCII-копия kokoro-config.json для RuG2P (владелец -- вызывающий).

    ru_g2p.RuG2P читает конфиг через Path.read_text() без encoding=, а на
    Windows это cp1251 -> UnicodeDecodeError (в файле не-ASCII байты).
    json.dumps(ensure_ascii=True) экранирует не-ASCII в \\uXXXX: копия
    становится чистым ASCII (cp1251 читает без ошибок), а json.loads
    восстанавливает исходные символы -- семантика конфига не меняется.
    """
    cfg = json.loads((MODELS / "kokoro-config.json").read_text(encoding="utf-8"))
    fd, tmp = tempfile.mkstemp(prefix="kokoro-config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            json.dump(cfg, fh, ensure_ascii=True)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Path(tmp)


def _make_g2p() -> RuG2P:
    """Конструирует RuG2P без глобального патча pathlib.

    Раньше Path.read_text подменялся на весь процесс (ради чтения конфига
    выше); теперь utf-8 применяется только к этой копии, остальной код
    читает файлы обычным способом.
    """
    copy = _vocab_config_copy()
    try:
        return RuG2P(vocab_path=copy)
    finally:
        copy.unlink(missing_ok=True)


def load(voice: str) -> None:
    """Грузит G2P + дефолтный голос (вызывается до старта сервера)."""
    global _g2p
    log.info("загрузка G2P (RUAccent)...")
    _g2p = _make_g2p()
    with _lock:
        _ensure_voice(voice)
    log.info("модель '%s' готова", voice)


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Текст -> WAV-байты 24 kHz PCM16 (под локом: модель не
    потокобезопасна). voice -- голос из запроса; неизвестный/пустой ->
    дефолтный."""
    with _lock:
        v = voice if voice in VOICES else DEFAULT_VOICE
        model, pack = _ensure_voice(v)
        ipa, oov = _g2p(text)
        if oov:
            log.warning("OOV-символы: %s", sorted(oov))
        with torch.no_grad():
            audio = model(ipa, pack[len(ipa) - 1], 1.0,
                          return_output=True).audio
    buf = io.BytesIO()
    sf.write(buf, audio.cpu().numpy(), SAMPLE_RATE,
             format="WAV", subtype="PCM_16")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/health":
            self._json(200, {"status": "ok", "engine": "kokoro-ru",
                             "voice": _voice, "voices": list(VOICES)})
        else:
            self._json(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/synth":
            self._json(404, {"error": "unknown path"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = (payload.get("text") or "").strip()
            voice = (payload.get("voice") or "").strip() or None
            if not text:
                self._json(400, {"error": "empty text"})
                return
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS]
            wav = synthesize(text, voice)
        except Exception as exc:  # noqa: BLE001 -- отдаём ошибку клиенту
            log.exception("ошибка синтеза")
            self._json(500, {"error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http: " + fmt, *args)


def main() -> None:
    voice = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VOICE
    if voice not in VOICES:
        raise SystemExit(f"голос должен быть из {list(VOICES)}")
    load(voice)
    server = ThreadingHTTPServer(("127.0.0.1", 8091), Handler)
    log.info("kokoro-ru sidecar слушает 127.0.0.1:8091 (голос %s)", voice)
    server.serve_forever()


if __name__ == "__main__":
    main()
