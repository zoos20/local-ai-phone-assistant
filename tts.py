# -*- coding: utf-8 -*-
"""Озвучка: Piper (офлайн-нейро-TTS, русский голос irina).

Голос лежит в models/ru_RU-irina-medium.onnx. Синтез в WAV 16 бит.
Модель грузится лениво.
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import urllib.request
import wave
from pathlib import Path

from config import settings

log = logging.getLogger("assistant.tts")

_voice = None
_lock = threading.Lock()

BASE_DIR = Path(__file__).resolve().parent

# Связка «профиль <-> голос» (план): у профиля поле voice = id голоса
# kokoro-ru (sveta/masha/dima). Фолбэк на piper, если kokoro упал:
# женские sveta/masha -> irina, мужской dima -> denis.
_PIPER_BY_VOICE = {
    "sveta": "ru_RU-irina-medium",
    "masha": "ru_RU-irina-medium",
    "dima": "ru_RU-denis-medium",
}

# --- санация текста перед синтезом (жалоба 28.10 «говорит про звёзды») ----
# espeak/piper ЧИТАЕТ markdown-символы вслух: «**дата**» -> «звезда звезда
# дата звезда звезда» (проверено на phonemize), «#» -> «решётка»,
# «~» -> «тильда», URL в [текст](url) -> чтение адреса целиком.
# Ответы модели/поисковика нередко приходят с markdown -- чистим ПЕРЕД
# озвучкой (сам reply для истории/логов не трогаем).
_RE_FENCE = re.compile(r"```.*?```", re.S)          # блоки кода
_RE_LINK = re.compile(r"!\[([^\]]*)\]\([^)]*\)")     # картинки -> alt
_RE_LINK2 = re.compile(r"\[([^\]]*)\]\([^)]*\)")     # ссылки -> текст
_RE_MD = re.compile(r"(\*\*|__|\*|_|~~|`|~)")        # акценты/код
_RE_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)  # «# Заголовок»
_RE_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)         # «> цитата»
_RE_HR = re.compile(r"^\s*[-*_]{3,}\s*$", re.M)       # разделители
# emoji/символы-украшения (звёзды, сердца...): piper читает их либо
# пусто, либо непредсказуемо -- убираем
_RE_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # основной блок emoji
    "\U0001F1E6-\U0001F1FF"   # флаги
    "☀-➿"        # Misc Symbols / Dingbats (★, ♥, ✅...)
    "⬀-⯿"        # Misc Symbols and Arrows (⭐ и др.)
    "️"           # variation selector
    "]"
)


def clean_for_speech(text: str) -> str:
    """Убирает markdown/эмодзи, которые TTS произносит вслух как мусор."""
    t = text or ""
    t = _RE_FENCE.sub(" ", t)
    t = _RE_LINK.sub(r"\1", t)
    t = _RE_LINK2.sub(r"\1", t)
    t = _RE_HEADING.sub("", t)
    t = _RE_QUOTE.sub("", t)
    t = _RE_HR.sub(" ", t)
    t = _RE_MD.sub("", t)
    t = _RE_EMOJI.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# --- нормализация «неосмысленных» мест (фаза 7, п.4 «подбор движка») ----
# espeak/piper неплохо читает простые числа, но путается на сокращениях
# (т.д., т.к. -- читает по буквам), валюте/процентах («₽/%» читает как
# «знак» или пропускает), диапазонах «5-10» и цифровых датах «30.10.2026».
# Разворачиваем в слова с правильными окончаниями:
#   1 процент / 2 процента / 5 процентов (и 11-14 -- «процентов»).
_RE_URL = re.compile(r"(?:https?://|www\.)\S+")
_RE_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_RE_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_RE_RANGE = re.compile(r"\b(\d+)\s*[-–—]\s*(\d+)\b")
_RE_PERCENT = re.compile(r"(\d+)\s*%")
_RE_RUB = re.compile(r"(\d+)\s*(?:₽|руб\.)")
_RE_USD = re.compile(r"(\d+)\s*\$")
_RE_EUR = re.compile(r"(\d+)\s*€")
_MONTHS_GEN = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
# сокращения: ТОЧКА в паттерне обязательна (без неё «тк» съело бы начало
# слов: «ткс» -> «потому чтос»)
_ABBREV = (
    (re.compile(r"\bи\s+т\s*\.\s*д\s*\.?", re.I), "и так далее"),
    (re.compile(r"\bи\s+т\s*\.\s*п\s*\.?", re.I), "и тому подобное"),
    (re.compile(r"\bт\s*\.\s*к\s*\.?\s*", re.I), "потому что "),
    (re.compile(r"\bт\s*\.\s*е\s*\.?\s*", re.I), "то есть "),
    (re.compile(r"\bнапр\s*\.\s*", re.I), "например, "),
    (re.compile(r"\bприбл\s*\.\s*", re.I), "примерно "),
)
# единицы измерения после числа: (rx, один, несколько, много, хвост).
# Порядок важен: «км/ч» ДО «км» (иначе «60 км/ч» -> «60 километров/ч»)
_UNITS = (
    (re.compile(r"(\d+)\s*км\s*/\s*ч"),
     "километр", "километра", "километров", " в час"),
    (re.compile(r"(\d+)\s*м\s*/\s*с"),
     "метр", "метра", "метров", " в секунду"),
    (re.compile(r"(\d+)\s*°\s*[cCсС℃]?"),
     "градус", "градуса", "градусов", ""),
    (re.compile(r"(\d+)\s*кг\b"),
     "килограмм", "килограмма", "килограммов", ""),
    (re.compile(r"(\d+)\s*км\b"),
     "километр", "километра", "километров", ""),
    (re.compile(r"(\d+)\s*см\b"),
     "сантиметр", "сантиметра", "сантиметров", ""),
)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """1 процент / 2 процента / 5 процентов (11-14 -> «много»)."""
    n = abs(int(n))
    if n % 100 in (11, 12, 13, 14):
        return many
    if n % 10 == 1:
        return one
    if n % 10 in (2, 3, 4):
        return few
    return many


def normalize_for_speech(text: str) -> str:
    """Разворачивает то, что TTS произносит неосмысленно (фаза 7, п.4).

    Вызывается ПОСЛЕ clean_for_speech (markdown уже убран). Простые числа
    НЕ трогаем -- espeak читает их правильно; правим только проблемные
    формы: сокращения, %/валюту, диапазоны, время, даты, единицы, URL.
    """
    t = text or ""

    # URL -> «ссылка» (espeak читает адрес по буквам как мусор)
    t = _RE_URL.sub(" ссылка ", t)

    # сокращения
    for rx, rep in _ABBREV:
        t = rx.sub(rep, t)

    # дата в цифрах: 30.10.2026 -> 30 октября 2026 года
    def _date(m: re.Match) -> str:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{d} {_MONTHS_GEN[mo - 1]} {y} года"
        return m.group(0)

    t = _RE_DATE.sub(_date, t)

    # время в цифрах: 17:32 -> 17 часов 32 минуты; 18:00 -> 18 часов
    def _time(m: re.Match) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        out = f"{h} {_plural(h, 'час', 'часа', 'часов')}"
        if mi:
            out += f" {mi} {_plural(mi, 'минута', 'минуты', 'минут')}"
        return out

    t = _RE_TIME.sub(_time, t)

    # диапазон: 5-10 -> от 5 до 10
    t = _RE_RANGE.sub(lambda m: f"от {m.group(1)} до {m.group(2)}", t)

    # проценты и валюта (с окончанием по числу)
    t = _RE_PERCENT.sub(
        lambda m: f"{m.group(1)} "
                  f"{_plural(m.group(1), 'процент', 'процента', 'процентов')}",
        t)
    t = _RE_RUB.sub(
        lambda m: f"{m.group(1)} "
                  f"{_plural(m.group(1), 'рубль', 'рубля', 'рублей')}", t)
    t = _RE_USD.sub(
        lambda m: f"{m.group(1)} "
                  f"{_plural(m.group(1), 'доллар', 'доллара', 'долларов')}", t)
    t = _RE_EUR.sub(r"\1 евро", t)   # «евро» несклоняемое

    # единицы измерения (все с захваченным числом -> правильное окончание)
    for rx, one, few, many, tail in _UNITS:
        t = rx.sub(
            lambda m, o=one, f=few, mn=many, tl=tail:
                f"{m.group(1)} {_plural(m.group(1), o, f, mn)}{tl}", t)

    return re.sub(r"\s+", " ", t).strip()


def _get_voice(piper_voice: str | None = None):
    """Ленивая загрузка piper-голоса; по умолчанию settings.tts_voice."""
    global _voice
    name = piper_voice or settings.tts_voice
    with _lock:
        if _voice is None or getattr(_voice, "_name", None) != name:
            from piper import PiperVoice

            onnx = BASE_DIR / "models" / f"{name}.onnx"
            if not onnx.exists():
                raise FileNotFoundError(f"Голос {name} не найден: {onnx}")
            log.info("загрузка piper '%s'...", name)
            _voice = PiperVoice.load(str(onnx))
            try:
                _voice._name = name  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 -- просто метка для кэша
                pass
            log.info("piper готов")
        return _voice


def _synthesize_kokoro(text: str, voice: str = "") -> bytes:
    """Отправляет текст на kokoro-ru sidecar (порт 8091) -> WAV-байты.

    Sidecar живёт отдельным процессом в .venv-tts (torch CPU), модели
    загружены заранее. voice -- голос из профиля (связка «профиль <->
    голос»); пустой -> дефолтный sidecar'а. Ошибки пробрасываются --
    вызывающий решает, фолбэчить ли на piper.
    """
    payload: dict[str, str] = {"text": text}
    if voice:
        payload["voice"] = voice
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8091/synth", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    if not data.startswith(b"RIFF"):
        raise RuntimeError("kokoro-ru sidecar вернул не WAV")
    return data


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Текст -> WAV-байты (частота модели). Пустой текст -> b"".

    voice -- голос kokoro-ru из профиля (связка «профиль <-> голос»);
    None -> дефолтный. При падении kokoro -- фолбэк на piper (женские
    голоса -> irina, мужской dima -> denis).
    """
    cleaned = normalize_for_speech(clean_for_speech(text))
    if not cleaned and (text or "").strip():
        # Жалоба 30.10 «один из запросов не был озвучен»: чистка убрала
        # ВЕСЬ текст (например, reply целиком из markdown/ссылок/эмодзи) ->
        # ответ пропадал молча. Логируем исходник и озвучиваем как есть.
        log.warning("tts: clean-for-speech убрал весь текст, исходник: %r",
                    text[:200])
        cleaned = normalize_for_speech((text or "").strip())
    text = cleaned
    if not text:
        return b""

    if settings.tts_engine == "kokoro":
        try:
            return _synthesize_kokoro(text, voice or "")
        except Exception:
            # фолбэк на piper: ассистент не должен замолкнуть из-за TTS
            log.exception("kokoro-ru недоступен (запущен start-kokoro.ps1?), "
                          "фолбэк на piper")

    piper_name = _PIPER_BY_VOICE.get(voice or "", settings.tts_voice)
    v = _get_voice(piper_name)
    buf = io.BytesIO()
    # synthesize_wav пишет готовый WAV-контейнер в файлоподобный объект.
    with wave.open(buf, "wb") as wav:
        v.synthesize_wav(text, wav)
    return buf.getvalue()


def is_available() -> bool:
    """Есть ли файл голоса (для /health, без загрузки модели)."""
    return (BASE_DIR / "models" / f"{settings.tts_voice}.onnx").exists()
