# -*- coding: utf-8 -*-
"""silero v5_6_ru sidecar: HTTP-сервис озвучки (.venv-tts), порт 8092.

Запуск (см. start-silero.ps1):
    <project>\server\.venv-tts\\Scripts\\python.exe tts_silero_server.py

Модель (torch CPU, пакет v5_6_ru -- все 5 ru-голосов в одном инстансе)
грузится один раз при старте из кэша torch_hub и живёт в процессе; текст
нормализуется ru-normalizr (профиль tts) поверх пришедшего текста --
прод-чистку speech_text.py делает tts.py ДО запроса сюда. Возвращает
WAV 48 kHz mono PCM16.

Эндпоинты:
    GET  /health                 -> {"status":"ok","engine":...}
    POST /synth {"text","voice"} -> WAV-байты (48 kHz, mono, PCM16)
"""
from __future__ import annotations

import io
import json
import logging
import os
import socket
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ASCII-кэши ДО импорта torch (дом с кириллицей ломает C++-ридеры).
# Веса v5_6_ru.pt (~141 МБ) уже в кэше -- sidecar не качает их заново.
os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parent / ".cache" / "hf"))
os.environ.setdefault("TORCH_HOME", str(Path(__file__).resolve().parent / ".cache" / "torch_hub"))

BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
os.makedirs(LOGS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOGS_DIR / "silero_sidecar.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("silero.sidecar")

import numpy as np  # noqa: E402  (лёгкий импорт, зависимость torch)
from ru_normalizr import NormalizeOptions, Normalizer  # noqa: E402

# Пакет v5_6_ru несёт все 5 голосов; один инстанс модели обслуживает все.
VOICES: tuple[str, ...] = ("aidar", "baya", "eugene", "kseniya", "xenia")
DEFAULT_VOICE = "eugene"
SAMPLE_RATE = 48000
MAX_CHARS = 2000
HOST = "127.0.0.1"
PORT = 8092
WARMUP_TEXT = "Проверка связи"

_NORMALIZER = Normalizer(NormalizeOptions.tts())

# Кэш-репо torch.hub: имя каталога и веса ВНУТРИ репо (ref не пиним:
# "repo:commit" сменил бы имя кэш-каталога -> cache-miss -> клон).
_REPO_DIRNAME = "snakers4_silero-models_master"
_WEIGHTS_RELPATH = Path("src") / "silero" / "model" / "v5_6_ru.pt"
# Потолок тела запроса (по образцу _read_json_body в server.py).
_MAX_BODY_BYTES = 64 * 1024

_lock = threading.Lock()
_model: object | None = None   # torch-модель (лениво -- только в load())
_voice = DEFAULT_VOICE         # последний запрошенный голос (для /health)


def normalize_text(text: str) -> str:
    """normalizr(tts) ПОВЕРХ пришедшего текста.

    Порядок композиции зафиксирован: clean_for_speech ->
    normalize_for_speech (прод speech_text.py) -> normalizr(tts) --
    первые два этапа делает tts.py до запроса, здесь НЕ дублируются.
    Крэш нормализатора не валит синтез: лог + исходный текст.
    """
    try:
        return _NORMALIZER.normalize(text)
    except Exception:  # noqa: BLE001 -- синтез важнее идеального текста
        log.exception("ошибка нормализатора -- синтезирую исходный текст")
        return text


def _ensure_cached_repo() -> None:
    """Жёсткий отказ без сети при пустом кэше (CWE-494).

    torch.hub.load при отсутствии кэша клонирует snakers4/silero-models
    с GitHub master и исполняет hubconf.py -- сетевой путь кода.
    Разрешаем ТОЛЬКО заранее наполненный кэш: каталог репо с hubconf.py
    и весами v5_6_ru.pt (веса лежат внутри репо). Отсутствие -> лог +
    SystemExit(1), НИ ОДНОГО сетевого обращения. Вызывается в load()
    ПОСЛЕ set_dir (пин должен указывать в фактический кэш).
    """
    import torch  # тот же ленивый импорт, что в load()

    repo = Path(torch.hub.get_dir()) / _REPO_DIRNAME
    weights = repo / _WEIGHTS_RELPATH
    if not (repo / "hubconf.py").is_file() or not weights.is_file():
        log.error("кэш silero-models не найден: %s (нужны hubconf.py и "
                  "%s) -- sidecar НЕ качает из сети; наполните кэш "
                  "(см. README) и перезапустите", repo, _WEIGHTS_RELPATH)
        raise SystemExit(1)


def load() -> None:
    """Грузит модель silero v5_6_ru один раз (до bind порта)."""
    global _model
    import torch  # лениво: импорт модуля остаётся лёгким

    # Жёсткий пин кэша хаба (зеркально tts_kokoro_server.py:49): пина
    # TORCH_HOME через setdefault недостаточно -- чужой TORCH_HOME из
    # окружения пользователя перекрыл бы его, и torch.hub.load молча
    # ушёл бы в чужой кэш/в сеть. Каталог -- ...\torch_hub\hub, а не
    # ...\torch_hub как в kokoro: torch.hub.get_dir() всегда добавляет
    # "hub", и именно там лежит кэш-репо snakers4_silero-models_master
    # (kokoro через torch.hub.load не грузится -- там пин совпадает с
    # TORCH_HOME, для silero пин должен попадать в фактический кэш).
    torch.hub.set_dir(str(BASE_DIR / ".cache" / "torch_hub" / "hub"))
    _ensure_cached_repo()
    log.info("загрузка silero v5_6_ru (torch.hub, кэш %s)...",
             torch.hub.get_dir())
    _model, _ = torch.hub.load("snakers4/silero-models", "silero_tts",
                               language="ru", speaker="v5_6_ru",
                               trust_repo=True)
    log.info("модель silero v5_6_ru готова (голоса: %s)", ", ".join(VOICES))


def _warmup_all() -> None:
    """Прогрев «Проверка связи» КАЖДЫМ из 5 голосов до bind порта.

    apply_tts лениво грузит саб-пакет спикера (~1 с/голос) -- убираем
    этот хвост с первого запроса.
    """
    assert _model is not None, "вызови load() до _warmup_all()"
    for voice in VOICES:
        started = time.perf_counter()
        with _lock:
            _model.apply_tts(text=WARMUP_TEXT, speaker=voice,
                             sample_rate=SAMPLE_RATE)
        log.info("прогрев голоса '%s' готов за %.1f с",
                 voice, time.perf_counter() - started)


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Текст -> WAV-байты 48 kHz PCM16 mono.

    Под локом: модель не потокобезопасна (как kokoro). Голос
    неизвестный/пустой -> дефолтный; пустой текст -> ValueError
    (HTTP-слой отдаёт 400 до вызова сюда).
    """
    global _voice
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    with _lock:
        v = voice if voice in VOICES else DEFAULT_VOICE
        assert _model is not None, "модель не загружена (вызови load())"
        audio = _model.apply_tts(text=normalize_text(text), speaker=v,
                                 sample_rate=SAMPLE_RATE)
        _voice = v
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        # Пик аудио модели ~0.92 -- клиппинга нет (проверено tts_ab).
        pcm16 = np.rint(np.clip(audio.numpy(), -1.0, 1.0) * 32767.0
                        ).astype("<i2")
        wav.writeframes(pcm16.tobytes())
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
            self._json(200, {"status": "ok", "engine": "silero-v5_6_ru",
                             "voice": _voice, "voices": list(VOICES),
                             "sample_rate": SAMPLE_RATE})
        else:
            self._json(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/synth":
            self._json(404, {"error": "unknown path"})
            return
        # Потолок тела ДО чтения (по образцу _read_json_body server.py):
        # честный Content-Length сразу даёт 400/413 без накопления памяти.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "bad Content-Length"})
            return
        if length < 0:
            self._json(400, {"error": "bad Content-Length"})
            return
        if length > _MAX_BODY_BYTES:
            self._json(413, {"error": "payload too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            text = (payload.get("text") or "").strip()
            voice = (payload.get("voice") or "").strip()
            if not text:
                self._json(400, {"error": "empty text"})
                return
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS]
            wav = synthesize(text, voice)
        except Exception as exc:  # noqa: BLE001 -- наружу generic (CWE-209)
            log.exception("ошибка синтеза (%s: %s)", type(exc).__name__, exc)
            self._json(500, {"error": "synthesis failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http: " + fmt, *args)


def _port_in_use() -> bool:
    """True, если 127.0.0.1:PORT уже кем-то слушается.

    Windows SO_REUSEADDR допускает двойной bind (у kokoro -- 4 процесса
    на 8091), поэтому проверяем TCP-connect'ом, а не bind'ом.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        return sock.connect_ex((HOST, PORT)) == 0


def main() -> None:
    if _port_in_use():
        log.info("silero sidecar уже слушает %s:%d -- выхожу", HOST, PORT)
        raise SystemExit(0)
    load()
    _warmup_all()
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        log.error("не удалось занять порт %d: %s", PORT, exc)
        raise SystemExit(1) from exc
    log.info("silero sidecar слушает %s:%d (%d голосов)", HOST, PORT,
             len(VOICES))
    server.serve_forever()


if __name__ == "__main__":
    main()
