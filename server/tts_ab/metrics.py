"""Чистые хелперы метрик TTS A/B харнесса (задача 1; ТЗ §3.3/§3.4).

ТОЛЬКО stdlib (wave/json/os/io): БЕЗ numpy/torch/psutil/piper/
supertonic и без импортов прод-модулей — модуль импортируется и в
.venv (тесты), и в .venv-ab (драйвер/адаптеры, задачи 3–5).

Контракты NDJSON (одна строка запроса → одна строка результата,
UTF-8):
- запрос:    {phrase_id, text, voice}
- результат: {engine, voice, phrase_id, category, text, ok, synth_ms,
  load_ms, audio_duration_s, sample_rate, rtf, peak_rss_mb, error,
  wav_path}

results.json — тот же NDJSON: одна строка результата на строку файла.
Повторы одной фразы от -Repeats — валидные ОТДЕЛЬНЫЕ строки; слияние
убирает только ТОЧНЫЕ дубликаты (одинаковый канонический JSON) и
невалидные строки, порядок первого появления сохраняется.
"""
from collections.abc import Iterable
import io
import json
import os
import wave

REQUEST_FIELDS: tuple[str, ...] = ("phrase_id", "text", "voice")

RESULT_FIELDS: tuple[str, ...] = (
    "engine",
    "voice",
    "phrase_id",
    "category",
    "text",
    "ok",
    "synth_ms",
    "load_ms",
    "audio_duration_s",
    "sample_rate",
    "rtf",
    "peak_rss_mb",
    "error",
    "wav_path",
)

# Поля, которые обязаны присутствовать в каждой строке результата
# (могут быть None — кроме ok: ok строго bool).
REQUIRED_RESULT_FIELDS: tuple[str, ...] = (
    "engine",
    "voice",
    "phrase_id",
    "category",
    "text",
    "ok",
)


def wav_duration(
    source: str | os.PathLike[str] | bytes | bytearray,
) -> float | None:
    """Длительность WAV в секундах из заголовка: frames / framerate.

    Принимает путь к файлу (str/PathLike) или байты WAV. Дегенеративный
    заголовок (framerate <= 0) → None; 0 кадров → 0.0 (честное значение
    заголовка, RTF от него вернёт (None, False)). Битый/несуществующий
    файл — штатные исключения wave/OSError наверх.
    """
    if isinstance(source, (bytes, bytearray)):
        wav = wave.open(io.BytesIO(bytes(source)), "rb")
    else:
        wav = wave.open(os.fspath(source), "rb")
    with wav as handle:
        frames = handle.getnframes()
        framerate = handle.getframerate()
    if framerate <= 0:
        return None
    return frames / framerate


def compute_rtf(
    synth_ms: float | None,
    audio_duration_s: float | None,
) -> tuple[float | None, bool]:
    """RTF = synth_ms / 1000 / audio_duration_s.

    Возвращает пару (rtf, is_valid) — «None + явный флаг»: при
    отсутствующей (None) или неположительной длительности аудио, а
    также при отсутствующем/отрицательном synth_ms — (None, False).
    Нулевой synth_ms валиден (rtf = 0.0).
    """
    if synth_ms is None or audio_duration_s is None:
        return None, False
    if synth_ms < 0 or audio_duration_s <= 0:
        return None, False
    return synth_ms / 1000.0 / audio_duration_s, True


def validate_request_row(row: object) -> str | None:
    """None — строка запроса валидна, иначе текст проблемы.

    Требования: JSON-объект со ВСЕМИ полями REQUEST_FIELDS — непустые
    строки (text уходит в движки как есть, без clean_for_speech).
    """
    if not isinstance(row, dict):
        return "request line is not a JSON object"
    for field in REQUEST_FIELDS:
        if field not in row:
            return f"missing field {field!r}"
        value = row[field]
        if not isinstance(value, str) or not value.strip():
            return f"field {field!r} must be a non-empty string"
    return None


def validate_result_row(row: object) -> str | None:
    """None — строка результата валидна, иначе текст проблемы.

    Требования (задача 1): присутствуют ВСЕ поля
    REQUIRED_RESULT_FIELDS; ok — строго bool; при ok=false поле error
    ОБЯЗАНО быть непустой строкой (при ok=true error может
    отсутствовать или быть None). Остальные поля RESULT_FIELDS —
    опциональные.
    """
    if not isinstance(row, dict):
        return "result line is not a JSON object"
    missing = [f for f in REQUIRED_RESULT_FIELDS if f not in row]
    if missing:
        return "missing required fields: " + ", ".join(missing)
    if not isinstance(row["ok"], bool):
        return "field 'ok' must be a boolean"
    for field in ("engine", "voice", "phrase_id", "category"):
        value = row[field]
        if not isinstance(value, str) or not value:
            return f"field {field!r} must be a non-empty string"
    if not isinstance(row["text"], str):
        return "field 'text' must be a string"
    if not row["ok"]:
        error = row.get("error")
        if not isinstance(error, str) or not error.strip():
            return "ok=false requires a non-empty 'error'"
    return None


def merge_result_rows(
    rows: Iterable[dict],
) -> tuple[list[dict], list[str]]:
    """Слить строки результата: валидные, без точных дубликатов.

    Порядок первого появления сохраняется. Точная копия ранее
    встречавшейся строки (равный канонический JSON) — дубликат,
    отбрасывается с записью в problems; повторные синтезы одной фразы
    (-Repeats) различаются метриками и остаются ОТДЕЛЬНЫМИ строками.
    Невалидные строки — в problems. Проблемы НЕ прерывают слияние:
    возвращает (валидные_строки, проблемы).
    """
    merged: list[dict] = []
    problems: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        problem = validate_result_row(row)
        if problem:
            problems.append(f"row {index}: {problem}")
            continue
        key = json.dumps(row, sort_keys=True, ensure_ascii=False)
        if key in seen:
            problems.append(f"row {index}: exact duplicate dropped")
            continue
        seen.add(key)
        merged.append(row)
    return merged, problems


def read_result_rows(
    path: str | os.PathLike[str],
) -> tuple[list[dict], list[str]]:
    """Прочитать NDJSON results.json: (валидные строки, проблемы).

    Битая JSON-строка или строка с нарушением контракта — в problems,
    валидные строки возвращаются (частичный результат не теряется).
    Пустые строки пропускаются.
    """
    valid: list[dict] = []
    problems: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except ValueError as exc:
                problems.append(f"line {lineno}: invalid JSON ({exc})")
                continue
            problem = validate_result_row(row)
            if problem:
                problems.append(f"line {lineno}: {problem}")
                continue
            valid.append(row)
    return valid, problems


def write_result_rows(
    path: str | os.PathLike[str],
    rows: Iterable[dict],
) -> None:
    """Записать строки результата как NDJSON (UTF-8, LF), атомарно.

    tmp + os.replace — по образцу shopping_store: читатель не увидит
    наполовину записанный файл; ensure_ascii=False — кириллица читается
    в файле напрямую.
    """
    path = os.fspath(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
