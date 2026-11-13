# -*- coding: utf-8 -*-
"""Адаптеры движков TTS A/B харнесса (задача 3; ТЗ §3.2/§3.3/§3.4).

Каждый адаптер — самостоятельная программа:

    <python.exe> -m tts_ab.adapters <engine>

Протокол (зафиксирован планом): stdin — JSON-строки запроса
{"phrase_id", "text", "voice"} по одной на строку (UTF-8); stdout —
JSON-строки результата (контракт tts_ab/metrics.RESULT_FIELDS, UTF-8,
одна строка на запрос). Ошибка одной фразы → строка ok=false с error,
адаптер ПРОДОЛЖАЕТ со следующей; EOF stdin → чистый выход (0). Каждая
исходящая строка валидируется metrics.validate_result_row ДО печати;
невалидная (баг) в stdout не идёт — причина уходит в stderr.

Семантика полей:
- load_ms — холодная загрузка модели СВОЕГО голоса: измеряется при
  первом появлении каждого голоса («один раз на движок×голос», ТЗ
  §3.4). kokoro — None: модель живёт в sidecar-процессе, адаптеру
  нечего грузить;
- synth_ms — wall time (perf_counter) вокруг вызова синтеза, тёплый:
  каждый голос перед первым измерением прогревается фразой
  «Проверка связи» (синтез не измеряется и наружу не отдаётся);
- peak_rss_mb — None: RSS измеряет ДРАЙВЕР через psutil (задача 5);
- sample_rate / audio_duration_s — фактические, из заголовка WAV.

WAV адаптер пишет сам: data/tts_ab/wav/<engine>/<voice>/<phrase_id>.wav
(имена ASCII, каталоги создаются); wav_path в ответе — относительный
posix-путь от корня проекта.

Политика недоступности kokoro: /health не отвечает (опрос ≤60 с) →
каждая фраза получает ok=false с error, начинающимся с «unavailable:»,
и подсказкой «запусти scripts/start-kokoro.ps1» — БЕЗ краша и трейса.
Тот же префикс — при потере связи с sidecar во время синтеза. Драйвер
(задача 5) помечает движок unavailable по этому префиксу.

Движки: piper — импорт piper-tts ЛЕНИВЫЙ (внутри load(), другие ветки
не тянут его numpy); kokoro — только stdlib (urllib/json/wave); silero —
torch ЛЕНИВЫЙ (внутри load(); TORCH_HOME адаптер не переопределяет —
torch.hub читает переменную сам, дефолт без неё); supertonic — импорт
пакета ЛЕНИВЫЙ в startup(): пакет не установлен или автопроверка
lang="ru" не прошла → движок failed (ясная причина у каждой фразы,
дефолт §8 ТЗ), без импорта и без краша. Для тестов TTS_AB_KOKORO_URL
перекрывает базовый URL sidecar (дефолт http://127.0.0.1:8091). Модуль
исследовательский: состояние — локальное в процессе адаптера, прод-
сервер (app_state) не касается.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections.abc import Callable
from pathlib import Path

from tts_ab import metrics
from tts_ab.phrases import PHRASE_BY_ID

BASE_DIR = Path(__file__).resolve().parent.parent  # корень проекта
WAV_ROOT = BASE_DIR / "data" / "tts_ab" / "wav"

# Warmup-фраза (ТЗ §3.2): синтезируется при первом появлении голоса,
# НЕ измеряется, наружу не отдаётся.
WARMUP_TEXT = "Проверка связи"

# Таймаут фразы — как в проде tts.py:83 (HTTP-таймаут kokoro); для
# локальных движков без встроенного таймаута — потолок потока синтеза.
PHRASE_TIMEOUT_S = 120.0

# Безопасные имена каталогов/файлов WAV (ASCII, без разделителей путей).
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Таблица голосов engine → (мужской, женский) — для драйвера (задача 5):
# гендерные группы -Voices m,f берутся отсюда. supertonic — пресеты
# подтверждены API (tts.voice_style_names = F1..F5, M1..M5): дефолт
# задания M1/F1 (best-preset-интуиции API не даёт — стили равноправны).
ENGINE_VOICES: dict[str, tuple[str, str]] = {
    "piper": ("denis", "irina"),
    "kokoro": ("dima", "sveta"),
    "silero": ("eugene", "xenia"),
    "supertonic": ("M1", "F1"),
}


# --- Общие хелперы раннера ------------------------------------------------


class _EngineUnavailable(Exception):
    """Движок недоступен; str() уже содержит «unavailable: …» и подсказку."""


class _RunState:
    """Состояние запуска адаптера (локальный процесс, не app_state прод)."""

    def __init__(self) -> None:
        # load_ms — на движок×голос (ТЗ §3.4): каждый голос при первом
        # появлении в запуске измеряет СВОЮ холодную загрузку (kokoro
        # ничего не грузит и в словарь не пишется).
        self.load_ms: dict[str, float] = {}
        self.ready: set[str] = set()  # голоса, прошедшие load + warmup


def _reconfigure_utf8() -> None:
    """UTF-8 stdin/stdout независимо от кодовой страницы консоли.

    PYTHONUTF8=1 детям ставит драйвер (ТЗ §6); reconfigure — страховка
    на случай ручного запуска в cp1251-консоли Windows. errors=replace
    на stdin: битые байты не убивают адаптер — строка отвалится в
    JSON-парсе и уйдёт в stderr, протокол продолжится.
    """
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               newline="\n")
    except (AttributeError, OSError, ValueError):
        pass


def _emit(row: dict) -> None:
    """Напечатать строку результата после валидации контракта metrics.

    Невалидная строка (баг) в stdout НЕ идёт — протокол для драйвера
    остаётся чистым, причина уходит в stderr.
    """
    line = json.dumps(row, ensure_ascii=False)
    problem = metrics.validate_result_row(row)
    if problem is None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    else:
        print(f"tts_ab.adapters: skip invalid result row ({problem}): "
              f"{line[:300]}", file=sys.stderr)


def _call_with_timeout(job: Callable[[], object], timeout_s: float) -> object:
    """Выполнить job в отдельном потоке с потолком времени.

    Для локальных движков (piper, silero, supertonic) у синтеза нет
    встроенного таймаута, как у HTTP. Зависший джоб бросается
    (daemon-поток), фраза получает ok=false с таймаутом, адаптер
    продолжает со следующей строки stdin.

    Цена броска: брошенный daemon-поток может продолжать жечь CPU во
    время замера СЛЕДУЮЩЕЙ фразы и держать хэндл WAV (на Windows
    _unlink_quiet тогда молча не сработает — файл останется до конца
    запуска). Каскадные зависания ограничивает потолок ~10 мин на
    движок в драйвере (задача 5).
    """
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["value"] = job()
        except Exception as exc:  # исключение наверх через box
            box["error"] = exc

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        raise TimeoutError(f"превышен таймаут фразы ({timeout_s:.0f} с)")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _wav_stats(path: Path) -> tuple[float | None, int]:
    """(длительность_с, sample_rate) из заголовка WAV.

    Семантика duration == metrics.wav_duration: rate <= 0 → None.
    Читается ФАКТИЧЕСКИЙ заголовок — единственный источник истины
    по частоте движка (заявленные 22050/24000 проверяются тестом).
    """
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return (frames / rate if rate > 0 else None), rate


def _wav_rel_path(engine: str, voice: str, phrase_id: str) -> tuple[Path, str]:
    """(абсолютный путь WAV, относительный posix-путь) фразы.

    Составные имена — ASCII (регекс выше), иначе ValueError: путь
    попадает в results.json и отчёт (ТЗ §6 — пути без кириллицы).
    Каталоги создаются по необходимости.
    """
    for name, label in ((engine, "engine"), (voice, "voice"),
                        (phrase_id, "phrase_id")):
        if not _ID_RE.fullmatch(name):
            raise ValueError(f"unsafe {label} for wav path: {name!r}")
    path = WAV_ROOT / engine / voice / f"{phrase_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, path.relative_to(BASE_DIR).as_posix()


def _category_of(phrase_id: str) -> str:
    """Категория фразы из набора; неизвестный id → «unknown» (строка)."""
    phrase = PHRASE_BY_ID.get(phrase_id)
    return phrase.category if phrase is not None else "unknown"


def _shown_load_ms(state: _RunState, voice: str) -> float | None:
    """load_ms этого голоса (его первое появление в запуске) или None."""
    value = state.load_ms.get(voice)
    return None if value is None else round(value, 1)


def _unlink_quiet(path: Path) -> None:
    """Удалить частичный WAV после сбоя фразы (best effort).

    На Windows файл может остаться занят брошенным потоком таймаута —
    молча пропускаем: мусорный файл всё равно не попадёт в results.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _row(engine: str, voice: str, phrase_id: str, text: str, ok: bool,
         load_ms: float | None, *, synth_ms: float | None = None,
         duration_s: float | None = None, sample_rate: int | None = None,
         rtf: float | None = None, error: str | None = None,
         wav_path: str | None = None) -> dict:
    """Полная строка контракта metrics.RESULT_FIELDS (фикс. порядок)."""
    return {
        "engine": engine,
        "voice": voice,
        "phrase_id": phrase_id,
        "category": _category_of(phrase_id),
        "text": text,
        "ok": ok,
        "synth_ms": synth_ms,
        "load_ms": load_ms,
        "audio_duration_s": duration_s,
        "sample_rate": sample_rate,
        "rtf": rtf,
        "peak_rss_mb": None,  # измеряет ДРАЙВЕР (psutil), задача 5
        "error": error,
        "wav_path": wav_path,
    }


# --- Движок kokoro-ru (sidecar :8091; только stdlib) ----------------------


_KOKORO_URL_DEFAULT = "http://127.0.0.1:8091"  # прод-sidecar (порт 8091)
_KOKORO_URL_ENV = "TTS_AB_KOKORO_URL"  # оверрайд для тестов (закрытый порт)
_KOKORO_HEALTH_BUDGET_S = 60.0  # ожидание готовности sidecar (ТЗ §3.2/§6)
_KOKORO_HTTP_TIMEOUT_S = 120.0  # таймаут HTTP — как в проде tts.py:83
_KOKORO_MAX_CHARS = 2000  # как в sidecar tts_kokoro_server.py
_KOKORO_HINT = "запусти scripts/start-kokoro.ps1"


def _kokoro_base_url() -> str:
    """Базовый URL sidecar: TTS_AB_KOKORO_URL (тесты) или прод-дефолт."""
    raw = (os.environ.get(_KOKORO_URL_ENV) or "").strip()
    if not raw:
        return _KOKORO_URL_DEFAULT
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw.rstrip("/")
    print(f"tts_ab.adapters: {raw!r} из {_KOKORO_URL_ENV} не похож на URL —"
          f" беру дефолт {_KOKORO_URL_DEFAULT}", file=sys.stderr)
    return _KOKORO_URL_DEFAULT


def _kokoro_wait_health(base: str, budget_s: float) -> dict | None:
    """Опрос GET /health до готовности, но не дольше budget_s.

    Готовность = JSON {"status": "ok", ...}. Ошибка соединения/не-ok
    статус — «не готов»: во время прогрева (~11 с) порт ещё не слушает,
    «отказ соединения» ≠ «упал». Бюджет исчерпан → None.
    """
    deadline = time.monotonic() + budget_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            with urllib.request.urlopen(f"{base}/health",
                                        timeout=min(5.0, remaining)) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return payload
        except (OSError, ValueError):
            pass
        time.sleep(0.5)


class _KokoroEngine:
    """kokoro-ru через прод-sidecar :8091 — ТОЛЬКО stdlib (urllib/json).

    Модель живёт в процессе sidecar (чекпоинт грузится при его старте,
    голоса — лениво на первый запрос), поэтому адаптеру нечего «холодно
    грузить»: load_ms = None во всех строках. Sidecar харнесс НЕ
    запускает и НЕ останавливает; его недоступность — штатный исход.
    """

    name = "kokoro"

    def __init__(self) -> None:
        self.base = _kokoro_base_url()
        self.voices_known: tuple[str, ...] | None = None
        self._down_error: str | None = None

    def startup(self) -> None:
        """Дождаться /health ≤60 с; иначе — состояние unavailable."""
        health = _kokoro_wait_health(self.base, _KOKORO_HEALTH_BUDGET_S)
        if health is None:
            self._down_error = (
                f"unavailable: kokoro sidecar не ответил на /health "
                f"({self.base}) за {_KOKORO_HEALTH_BUDGET_S:.0f} с — "
                f"{_KOKORO_HINT}")
            return
        voices = health.get("voices")
        if (isinstance(voices, list) and voices
                and all(isinstance(v, str) for v in voices)):
            self.voices_known = tuple(voices)
        else:
            # Иначе валидация голосов молча отключилась бы: опечатка
            # голоса ушла бы в sidecar и молча подменилась дефолтом
            # (данные A/B были бы подменены). Предупреждаем в stderr.
            print(f"tts_ab.adapters: sidecar {self.base} не отдал список "
                  f"голосов в /health — валидация голосов отключена",
                  file=sys.stderr)

    def load(self, voice: str) -> float | None:
        # Адаптер не грузит модель — см. docstring класса: чекпоинт и
        # voicepack уже в sidecar-процессе. Отсюда load_ms = None.
        return None

    def _check_voice(self, voice: str) -> None:
        # Sidecar молча подставляет дефолт для неизвестного голоса —
        # для A/B это подмена данных, поэтому валидируем по /health.
        if self.voices_known is not None and voice not in self.voices_known:
            known = ", ".join(self.voices_known)
            raise ValueError(
                f"неизвестный голос {voice!r} для kokoro (sidecar: {known})")

    def _post_synth(self, text: str, voice: str) -> bytes:
        """POST /synth → WAV-байты; таймаут 120 с (прод tts.py:80-87)."""
        if self._down_error:
            raise _EngineUnavailable(self._down_error)
        self._check_voice(voice)
        # Обрезка зеркалит MAX_CHARS sidecar; в строке результата при
        # этом остаётся ИСХОДНЫЙ text запроса (осознанное поведение:
        # results.json хранит ровно то, что просили синтезировать; у
        # фраз харнесса < 2000 символов обрезка не срабатывает).
        payload = {"text": text[:_KOKORO_MAX_CHARS], "voice": voice}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/synth", data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(
                    req, timeout=_KOKORO_HTTP_TIMEOUT_S) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(
                f"kokoro sidecar вернул HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise _EngineUnavailable(
                f"unavailable: kokoro sidecar недоступен во время синтеза "
                f"({type(exc).__name__}) — {_KOKORO_HINT}") from exc
        if not data.startswith(b"RIFF"):  # проверка как в проде tts.py:85
            raise RuntimeError("kokoro sidecar вернул не WAV")
        return data

    def warmup(self, voice: str) -> None:
        """«Проверка связи»: прогрев чекпоинта голоса, наружу не идёт."""
        self._post_synth(WARMUP_TEXT, voice)

    def synth(self, text: str, voice: str, wav_path: Path) -> float:
        """Синтез фразы в wav_path; вернуть wall-ms (HTTP — сам синтез)."""
        t0 = time.perf_counter()
        data = self._post_synth(text, voice)
        synth_ms = (time.perf_counter() - t0) * 1000.0
        wav_path.write_bytes(data)
        return synth_ms


# --- Движок piper (.venv, piper-tts 1.3.0; импорт ЛЕНИВЫЙ) ----------------


_PIPER_MODELS = BASE_DIR / "models"
_PIPER_VOICES = {"denis": "ru_RU-denis-medium", "irina": "ru_RU-irina-medium"}


class _PiperEngine:
    """Piper medium — API по образцу прод-фолбэка tts.py:53-59,127-128.

    PiperVoice.load + synthesize_wav (wave.Wave_write), БЕЗ pip-операций.
    Импорт piper (и его numpy) — ленивый, внутри load(): режимы
    kokoro/silero/supertonic его зависимостей не тянут.
    """

    name = "piper"

    def __init__(self) -> None:
        self._voices: dict[str, object] = {}

    def startup(self) -> None:
        return None  # модель грузится лениво, на первом запросе голоса

    def load(self, voice: str) -> float | None:
        """Холодная загрузка голоса; ms — только при реальной загрузке."""
        if voice in self._voices:
            return None
        model_name = _PIPER_VOICES.get(voice)
        if model_name is None:
            known = ", ".join(sorted(_PIPER_VOICES))
            raise ValueError(
                f"неизвестный голос {voice!r} для piper (доступны: {known})")
        onnx = _PIPER_MODELS / f"{model_name}.onnx"
        if not onnx.exists():
            raise FileNotFoundError(f"голос {model_name} не найден: {onnx}")
        from piper import PiperVoice  # лениво: только в piper-режиме

        t0 = time.perf_counter()
        self._voices[voice] = PiperVoice.load(str(onnx))
        return (time.perf_counter() - t0) * 1000.0

    def warmup(self, voice: str) -> None:
        """«Проверка связи» в память: не измеряется, наружу не отдаётся."""

        def _job() -> None:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                self._voices[voice].synthesize_wav(WARMUP_TEXT, wav)

        _call_with_timeout(_job, PHRASE_TIMEOUT_S)

    def synth(self, text: str, voice: str, wav_path: Path) -> float:
        """Синтез фразы в wav_path; вернуть wall-ms синтеза БЕЗ диска.

        Синтез идёт в io.BytesIO (по образцу прод tts.py:125-128)
        внутри таймера, WAV пишется на диск ПОСЛЕ стопа таймера — так
        synth_ms piper сопоставим с kokoro (там таймер покрывает только
        HTTP-синтез): ни в одном замере нет дискового I/O.
        """

        def _job() -> tuple[bytes, float]:
            buf = io.BytesIO()
            t0 = time.perf_counter()
            with wave.open(buf, "wb") as wav:
                self._voices[voice].synthesize_wav(text, wav)
            return buf.getvalue(), (time.perf_counter() - t0) * 1000.0

        data, synth_ms = _call_with_timeout(_job, PHRASE_TIMEOUT_S)
        wav_path.write_bytes(data)
        return synth_ms


# --- Движок silero v5 (.venv-ab, torch.hub; импорт torch ЛЕНИВЫЙ) ----------


_SILERO_REPO = "snakers4/silero-models"
_SILERO_SPEAKER_PACK = "v5_6_ru"  # пакет со всеми ru-голосами модели
# Максимум из sample_rate models.yml для v5_6_ru: [8000, 24000, 48000]
# (допущение 2 ТЗ §7 — сравниваем лучшее качество движка; фактически
# использованный rate в каждой строке — из заголовка WAV, _wav_stats).
_SILERO_SAMPLE_RATE = 48000


class _SileroEngine:
    """Silero v5 (v5_6_ru) через torch.hub — .venv-ab (torch-cpu).

    TORCH_HOME адаптер НЕ переопределяет: torch.hub читает переменную
    сам при каждом load() (драйвер ставит <project>\server\.cache\
    torch_hub; без переменной — дефолт ~/.cache/torch/hub). Первая
    загрузка — сеть (GitHub-zipball репо, веса v5_6_ru.pt внутри);
    ошибка сети → ok=false с ясной причиной у фразы, адаптер жив,
    следующая фраза повторит попытку (graceful путь _handle).

    Пакет v5_6_ru содержит ВСЕ ru-голоса (aidar/baya/kseniya/eugene/
    xenia), но протокол меряет load_ms «своего голоса»: каждый голос
    при первом появлении грузит СВОЙ инстанс модели — повторный
    torch.hub.load идёт из кэша репо без сети (~4 с) и остаётся честной
    холодной загрузкой. Первый synth нового инстанса лениво подгружает
    саб-пакет спикера (~1 с) — это покрывает warmup голоса.

    Синтез — в память: model.apply_tts(text, speaker=voice,
    sample_rate=48000) — официальный путь README; save_wav модели
    пишет на диск ВНУТРИ вызова (дисковый I/O попал бы в synth_ms и
    сломал единообразие с piper/kokoro, где таймер его не покрывает).
    WAV PCM16 mono 48 kHz собирается stdlib wave в BytesIO ПОСЛЕ стопа
    таймера — по образцу piper; у конверсии float→int16 нет клиппинга
    (пик аудио модели ~0.92, проверено живым прогоном).
    """

    name = "silero"

    def __init__(self) -> None:
        self._voices: dict[str, object] = {}

    def startup(self) -> None:
        return None  # модель грузится лениво, на первом запросе голоса

    def load(self, voice: str) -> float | None:
        """Холодная загрузка модели своего голоса (torch.hub)."""
        if voice in self._voices:
            return None
        if voice not in ENGINE_VOICES["silero"]:
            raise ValueError(
                f"неизвестный голос {voice!r} для silero "
                f"(харнесс: {'/'.join(ENGINE_VOICES['silero'])})")
        import torch  # лениво: torch тянется только в silero-режиме

        t0 = time.perf_counter()
        model, _example = torch.hub.load(
            repo_or_dir=_SILERO_REPO,
            model="silero_tts",
            language="ru",
            speaker=_SILERO_SPEAKER_PACK,
            trust_repo=True,  # torch>=2.x: иначе интерактивный промпт доверия
        )
        self._voices[voice] = model
        return (time.perf_counter() - t0) * 1000.0

    def warmup(self, voice: str) -> None:
        """«Проверка связи»: прогрев инстанса и саб-пакета спикера."""

        def _job() -> None:
            self._voices[voice].apply_tts(
                text=WARMUP_TEXT, speaker=voice,
                sample_rate=_SILERO_SAMPLE_RATE)

        _call_with_timeout(_job, PHRASE_TIMEOUT_S)

    def synth(self, text: str, voice: str, wav_path: Path) -> float:
        """Синтез фразы в wav_path; вернуть wall-ms синтеза БЕЗ диска."""

        def _job() -> tuple[bytes, float]:
            import numpy as np  # лениво: зависимость torch

            t0 = time.perf_counter()
            audio = self._voices[voice].apply_tts(
                text=text, speaker=voice, sample_rate=_SILERO_SAMPLE_RATE)
            synth_ms = (time.perf_counter() - t0) * 1000.0
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(_SILERO_SAMPLE_RATE)
                pcm16 = np.rint(
                    np.clip(audio.numpy(), -1.0, 1.0) * 32767.0
                ).astype("<i2")
                wav.writeframes(pcm16.tobytes())
            return buf.getvalue(), synth_ms

        data, synth_ms = _call_with_timeout(_job, PHRASE_TIMEOUT_S)
        wav_path.write_bytes(data)
        return synth_ms


# --- Движок supertonic 3 (.venv-ab, onnxruntime; импорт ЛЕНИВЫЙ) -----------


_SUPERTONIC_LANG = "ru"  # одна настройка языка на все фразы движка


class _SupertonicEngine:
    """Supertonic 3 (pip-пакет supertonic 1.3.1) — .venv-ab, без torch.

    API: TTS(auto_download=True) → get_voice_style(voice_name=…) →
    synthesize(text, voice_style=style, lang="ru") → (wav ndarray
    (1, samples) float32, duration ndarray shape (1,)). Пресеты
    подтверждены API (voice_style_names = F1..F5, M1..M5): харнесс
    берёт дефолт задания M1 (м) / F1 (ж) — ENGINE_VOICES. duration
    НЕ используется: audio_duration_s/sample_rate считаются из
    заголовка фактического WAV (_wav_stats задачи 3) — единообразно
    со всеми движками.

    Кэш весов: HF_HOME + SUPERTONIC_CACHE_DIR ставит драйвер/
    запускающий (см. scripts/setup-tts-ab.ps1; одной HF_HOME мало —
    у пакета свой cache-dir); адаптер переменные НЕ переопределяет.
    auto_download=True докачает веса при пустом кэше (сеть) — ошибка
    сети → failed-причина у каждой фразы, без краша.

    Политика failed (дефолт §8 ТЗ): пакет не установлен → startup()
    фиксирует причину БЕЗ импорта; загрузка модели не удалась (сеть)
    или автопроверка lang="ru" (синтез короткой русской фразы) не
    прошла → модель выгружается, причина у каждой фразы. Префикс
    «unavailable:» здесь НЕ используется — это состояние kokoro-
    протокола (упавший sidecar); драйвер пометит движок failed.
    """

    name = "supertonic"

    def __init__(self) -> None:
        self._tts: object | None = None
        self._styles: dict[str, object] = {}
        self._fail_reason: str | None = None

    def startup(self) -> None:
        """Импорт пакета + загрузка модели + автопроверка lang="ru".

        Любой шаг, упавший по любой причине, переводит движок в
        состояние failed: причина хранится локально, каждая фраза
        получает ok=false с ней (RuntimeError в load()), процесс
        живёт и корректно отвечает по протоколу до EOF.
        """
        try:
            from supertonic import TTS  # лениво: только supertonic-режим
        except ImportError as exc:
            self._fail_reason = (
                "supertonic: пакет не установлен в .venv-ab "
                "(см. scripts/setup-tts-ab.ps1): "
                f"{type(exc).__name__}: {exc}")
            return
        try:
            self._tts = TTS(auto_download=True)
        except Exception as exc:
            self._tts = None
            self._fail_reason = (
                "supertonic: загрузка модели не удалась — "
                f"{type(exc).__name__}: {exc}")
            return
        try:
            # Проверка русского — синтезом (не списком языков API):
            # исключение при русской фразе = «русского нет» (§8 ТЗ).
            style = self._tts.get_voice_style(voice_name="M1")
            self._tts.synthesize(WARMUP_TEXT, voice_style=style,
                                 lang=_SUPERTONIC_LANG, verbose=False)
        except Exception as exc:
            self._tts = None
            self._fail_reason = (
                'supertonic: автопроверка lang="ru" не прошла — '
                f"{type(exc).__name__}: {exc}")

    def load(self, voice: str) -> float | None:
        """Холодная загрузка стиля голоса (JSON-векторы из кэша модели)."""
        if self._tts is None:
            raise RuntimeError(
                self._fail_reason or "supertonic: движок не инициализирован")
        if voice in self._styles:
            return None
        names = list(getattr(self._tts, "voice_style_names", []))
        if voice not in names:
            known = ", ".join(names) or "?"
            raise ValueError(
                f"неизвестный голос {voice!r} для supertonic "
                f"(доступны: {known})")
        t0 = time.perf_counter()
        self._styles[voice] = self._tts.get_voice_style(voice_name=voice)
        return (time.perf_counter() - t0) * 1000.0

    def warmup(self, voice: str) -> None:
        """«Проверка связи»: прогрев onnxruntime/стиля, наружу не идёт."""

        def _job() -> None:
            self._tts.synthesize(WARMUP_TEXT,
                                 voice_style=self._styles[voice],
                                 lang=_SUPERTONIC_LANG, verbose=False)

        _call_with_timeout(_job, PHRASE_TIMEOUT_S)

    def synth(self, text: str, voice: str, wav_path: Path) -> float:
        """Синтез фразы в wav_path; вернуть wall-ms синтеза БЕЗ диска.

        Таймер покрывает только synthesize (диффузия onnxruntime);
        WAV пишется soundfile'ом в BytesIO (PCM16 mono — формат
        заголовка одинаков для всех движков харнесса) ПОСЛЕ стопа
        таймера — по образцу piper.
        """

        def _job() -> tuple[bytes, float]:
            import soundfile as sf  # лениво: зависимость supertonic

            t0 = time.perf_counter()
            wav, _duration = self._tts.synthesize(
                text=text, voice_style=self._styles[voice],
                lang=_SUPERTONIC_LANG, verbose=False)
            synth_ms = (time.perf_counter() - t0) * 1000.0
            buf = io.BytesIO()
            sf.write(buf, wav.squeeze(), self._tts.sample_rate,
                     format="WAV", subtype="PCM_16")
            return buf.getvalue(), synth_ms

        data, synth_ms = _call_with_timeout(_job, PHRASE_TIMEOUT_S)
        wav_path.write_bytes(data)
        return synth_ms


# --- Реестр и раннер -------------------------------------------------------


# Классы в реестре — НЕ импорты движков: тяжёлые зависимости грузятся
# лениво внутри load()/startup()/synth() (torch — только silero,
# supertonic+soundfile — только supertonic, piper — только piper).
ENGINES: dict[str, type] = {
    "piper": _PiperEngine,
    "kokoro": _KokoroEngine,
    "silero": _SileroEngine,
    "supertonic": _SupertonicEngine,
}


def _handle(engine, state: _RunState, req: dict) -> dict:
    """Одна строка запроса → одна строка результата (контракт metrics)."""
    phrase_id = req["phrase_id"]
    text = req["text"]
    voice = req["voice"]
    wav_path: Path | None = None
    try:
        if voice not in state.ready:
            load_ms = engine.load(voice)
            if load_ms is not None and voice not in state.load_ms:
                state.load_ms[voice] = load_ms  # своя загрузка голоса
            engine.warmup(voice)
            state.ready.add(voice)
        wav_path, wav_rel = _wav_rel_path(engine.name, voice, phrase_id)
        synth_ms = engine.synth(text, voice, wav_path)
        duration, sample_rate = _wav_stats(wav_path)
        rtf, _ = metrics.compute_rtf(synth_ms, duration)
        return _row(
            engine.name, voice, phrase_id, text, True,
            _shown_load_ms(state, voice),
            synth_ms=round(synth_ms, 1),
            duration_s=None if duration is None else round(duration, 3),
            sample_rate=sample_rate,
            rtf=None if rtf is None else round(rtf, 4),
            wav_path=wav_rel,
        )
    except _EngineUnavailable as exc:
        return _row(engine.name, voice, phrase_id, text, False,
                    _shown_load_ms(state, voice), error=str(exc))
    except Exception as exc:  # graceful fail фразы — адаптер продолжает
        if wav_path is not None:
            _unlink_quiet(wav_path)
        return _row(engine.name, voice, phrase_id, text, False,
                    _shown_load_ms(state, voice),
                    error=f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    """Точка входа адаптера: python -m tts_ab.adapters <engine>."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ENGINES:
        known = ", ".join(sorted(ENGINES))
        print(f"usage: python -m tts_ab.adapters <engine> ({known})",
              file=sys.stderr)
        return 2
    _reconfigure_utf8()
    engine = ENGINES[args[0]]()
    engine.startup()
    state = _RunState()
    for raw in sys.stdin:
        line = raw.strip()
        if line.startswith("\ufeff"):
            line = line[1:].strip()
        if not line:
            continue  # пустые строки протоколу не мешают
        try:
            req = json.loads(line)
        except ValueError as exc:
            print(f"tts_ab.adapters: skip invalid JSON ({exc})",
                  file=sys.stderr)
            continue
        problem = metrics.validate_request_row(req)
        if problem is not None:
            print(f"tts_ab.adapters: skip request ({problem})",
                  file=sys.stderr)
            continue
        _emit(_handle(engine, state, req))
    return 0  # EOF stdin — чистый выход


if __name__ == "__main__":
    raise SystemExit(main())
