# -*- coding: utf-8 -*-
"""Отчёт TTS A/B харнесса (задача 5; ТЗ §3.6) + формат results.json.

results.json — NDJSON: ПЕРВАЯ строка файла — шапка прогона
``{"header": {...}}`` (один ключ), остальные строки — строки результата
(контракт tts_ab.metrics.RESULT_FIELDS). Читатель ниже распознаёт шапку
только в строке 1; строки без шапки читаются обычным
metrics.read_result_rows (шапка попала бы в problems — безвредно).

Только stdlib (json/statistics/os): numpy/psutil отчёту не нужны.
"""
from __future__ import annotations

import json
import os
import statistics
from collections.abc import Iterable, Sequence
from pathlib import Path

from tts_ab import metrics
from tts_ab.adapters import ENGINE_VOICES
from tts_ab.phrases import CATEGORY_TITLES, PHRASES

HEADER_KEY = "header"

# Канонический порядок категорий/движков в таблицах отчёта.
CATEGORY_ORDER: tuple[str, ...] = ("s", "m", "l", "n", "h")
ENGINE_ORDER: tuple[str, ...] = ("kokoro", "piper", "silero", "supertonic")

# Агрегированные строки-причины драйвера (не фразы набора).
_AGGREGATE_IDS: frozenset[str] = frozenset({"aborted", "startup"})

_BASE_DIR = Path(__file__).resolve().parent.parent


def _pair_sort_key(pair: tuple[str, str]) -> tuple:
    """(индекс движка, индекс голоса в ENGINE_VOICES) — порядок таблиц."""
    engine, voice = pair
    eng_i = ENGINE_ORDER.index(engine) if engine in ENGINE_ORDER else 99
    pair_voices = ENGINE_VOICES.get(engine, ("", ""))
    voc_i = 0 if voice == pair_voices[0] else (
        1 if voice == pair_voices[1] else 2)
    return (eng_i, voc_i, engine, voice)


# --- Формат results.json (NDJSON: шапка строкой 1 + строки результата) ----


def write_results_with_header(
    path: str | os.PathLike[str],
    header: dict,
    rows: Iterable[dict],
) -> None:
    """Атомарно записать шапку (строка 1) + строки результата (NDJSON)."""
    path = os.fspath(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({HEADER_KEY: header}, ensure_ascii=False) + "\n")
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_results_with_header(
    path: str | os.PathLike[str],
) -> tuple[dict | None, list[dict], list[str]]:
    """Прочитать results.json: (шапка|None, валидные строки, проблемы).

    Шапкой считается только объект с единственным ключом "header" в
    строке 1. Невалидные/битые строки — в problems (частичный результат
    не теряется).
    """
    header: dict | None = None
    rows: list[dict] = []
    problems: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except ValueError as exc:
                problems.append(f"line {lineno}: invalid JSON ({exc})")
                continue
            if (lineno == 1 and isinstance(obj, dict)
                    and set(obj) == {HEADER_KEY}):
                head = obj[HEADER_KEY]
                if isinstance(head, dict):
                    header = head
                else:
                    problems.append("line 1: header must be an object")
                continue
            problem = metrics.validate_result_row(obj)
            if problem is not None:
                problems.append(f"line {lineno}: {problem}")
                continue
            rows.append(obj)
    return header, rows, problems


# --- Агрегации ------------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _fmt(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def engine_statuses(rows: Iterable[dict]) -> list[tuple[str, str, str]]:
    """Статус каждого движка: [(engine, статус, причина)].

    Порядок — ENGINE_ORDER, затем прочие движки из строк (сортировка).
    Статусы: ok / ok (частично) / unavailable / failed / нет данных.
    «unavailable» — нет ok-строк и есть error с префиксом «unavailable:».
    """
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("engine")), []).append(row)
    engines = [e for e in ENGINE_ORDER if e in grouped]
    engines += sorted(set(grouped) - set(engines))
    result: list[tuple[str, str, str]] = []
    for engine in engines:
        engine_rows = grouped[engine]
        ok_pairs = {(r["phrase_id"], r["voice"]) for r in engine_rows if r["ok"]}
        all_pairs = {(r["phrase_id"], r["voice"]) for r in engine_rows}
        errors = [str(r["error"]) for r in engine_rows
                  if not r["ok"] and r.get("error")]
        unavailable = any(e.startswith("unavailable:") for e in errors)
        if not ok_pairs:
            status = "unavailable" if unavailable else "failed"
            reason = errors[0] if errors else "нет ok-строк"
        elif ok_pairs < all_pairs:
            status = "ok (частично)"
            reason = errors[0] if errors else ""
        else:
            status = "ok"
            reason = ""
        if len(reason) > 200:
            reason = reason[:197] + "..."
        result.append((engine, status, reason))
    return result


def summary_by_voice(rows: Iterable[dict]) -> list[dict]:
    """Сводная по движок×голос: медианы synth_ms по категориям и пр.

    synth_ms по каждой категории — МЕДИАНA по всем ok-строкам (при
    -Repeats>1 каждая строка — отдельный тёплый прогон, медиана по ТЗ);
    mean RTF по всем ok-строкам; peak RSS — максимум по ok-строкам;
    cold load_ms — медиана по непустым load_ms; sample_rate — медиана;
    errors — число ok=false строк этой пары.
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((str(row["engine"]), str(row["voice"])), []).append(row)

    summary: list[dict] = []
    for (engine, voice), voice_rows in sorted(grouped.items(),
                                              key=lambda i: _pair_sort_key(i[0])):
        ok_rows = [r for r in voice_rows if r["ok"]]
        bad_rows = [r for r in voice_rows if not r["ok"]]
        per_category: dict[str, float | None] = {}
        for category in CATEGORY_ORDER:
            values = [float(r["synth_ms"]) for r in ok_rows
                      if r.get("category") == category
                      and isinstance(r.get("synth_ms"), (int, float))]
            per_category[category] = _median(values)
        rtf_values = [float(r["rtf"]) for r in ok_rows
                      if isinstance(r.get("rtf"), (int, float))]
        rss_values = [float(r["peak_rss_mb"]) for r in ok_rows
                      if isinstance(r.get("peak_rss_mb"), (int, float))]
        load_values = [float(r["load_ms"]) for r in ok_rows
                       if isinstance(r.get("load_ms"), (int, float))]
        rate_values = [int(r["sample_rate"]) for r in ok_rows
                       if isinstance(r.get("sample_rate"), int)]
        summary.append({
            "engine": engine,
            "voice": voice,
            "synth_ms": per_category,
            "mean_rtf": _mean(rtf_values),
            "peak_rss_mb": max(rss_values) if rss_values else None,
            "load_ms": _median(load_values),
            "sample_rate": _median(rate_values),
            "errors": len(bad_rows),
            "ok_count": len(ok_rows),
        })
    return summary


def detail_rows(rows: Iterable[dict]) -> list[tuple[str, str, dict[tuple[str, str], str]]]:
    """Детальная таблица фраза×(движок×голос): медиана synth_ms | «err».

    Агрегированные строки-причины драйвера («aborted»/«startup») в
    таблицу фраз не входят.
    """
    ok_values: dict[str, dict[tuple[str, str], list[float]]] = {}
    err_values: dict[str, dict[tuple[str, str], list[str]]] = {}
    for row in rows:
        phrase_id = str(row["phrase_id"])
        if phrase_id in _AGGREGATE_IDS:
            continue
        key = (str(row["engine"]), str(row["voice"]))
        if row["ok"] and isinstance(row.get("synth_ms"), (int, float)):
            ok_values.setdefault(phrase_id, {}).setdefault(key, []).append(
                float(row["synth_ms"]))
        elif not row["ok"] and row.get("error"):
            err = str(row["error"])
            if len(err) > 40:
                err = err[:37] + "..."
            err_values.setdefault(phrase_id, {}).setdefault(key, []).append(err)
    pairs = sorted({k for per in ok_values.values() for k in per}
                   | {k for per in err_values.values() for k in per},
                   key=_pair_sort_key)
    out: list[tuple[str, str, dict[tuple[str, str], str]]] = []
    for phrase in PHRASES:
        row_cells: dict[tuple[str, str], str] = {}
        for pair in pairs:
            values = ok_values.get(phrase.phrase_id, {}).get(pair)
            if values:
                row_cells[pair] = _fmt(_median(values), 1)
                continue
            errs = err_values.get(phrase.phrase_id, {}).get(pair)
            row_cells[pair] = errs[0] if errs else "—"
        out.append((phrase.phrase_id, phrase.category, row_cells))
    return out


# --- Рендер report.md ------------------------------------------------------


def _voices_of(engine: str) -> list[str]:
    pair = ENGINE_VOICES.get(engine)
    return list(pair) if pair else []


_QA_FOOTNOTES: tuple[str, ...] = (
    "peak_rss silero = 2 per-voice инстанса × ~440–520 МБ (обе гендерные "
    "группы держат свою модель); прод-сценарий с одним инстансом "
    "≈ 440–520 МБ.",
    "piper cold load_ms ~4 c на этой машине — это загрузка onnxruntime/"
    "модели, НЕ «первый звук»; внешний ориентир первого звука 180–283 мс "
    "относится к тёплому синтезу; тёплый RTF здесь ~0.05 — совпадает по "
    "порядку.",
    "supertonic RTF 0.25–0.38 против внешних ~0.195 — расхождение "
    "~1.5–2× (<10×), разница CPU/фраз.",
    "таймеры synth_ms у piper включают int16-конверсию (~1–2 мс), у "
    "silero/supertonic она вне таймера — эффект <2%, ниже шума.",
)


def _render_header(header: dict | None) -> list[str]:
    lines = ["# TTS A/B — отчёт прогона (kokoro-ru / Piper / Silero v5 / Supertonic 3)", ""]
    if not isinstance(header, dict):
        lines += ["Шапка прогона: нет данных (results.json без шапки).", ""]
        return lines
    params = header.get("params")
    if isinstance(params, dict):
        params = json.dumps(params, ensure_ascii=False)
    kokoro = header.get("kokoro_status")
    lines += [
        f"- Дата/время: {header.get('started_at', '—')}",
        f"- CPU: {header.get('cpu', '—')}",
        f"- Python: {header.get('python', '—')}",
        f"- Параметры запуска: {params or '—'}",
        f"- kokoro sidecar: {kokoro or '—'}",
        f"- ВНИМАНИЕ: {header.get('warning', '—')}",
        "",
    ]
    return lines


def render_report(header: dict | None, rows: list[dict]) -> str:
    """Полный markdown-отчёт по строкам results.json (ТЗ §3.6)."""
    lines: list[str] = _render_header(header)
    lines += ["## Статус движков", "",
              "| движок | статус | причина |", "|---|---|---|"]
    statuses = engine_statuses(rows) if rows else []
    if not statuses:
        lines.append("| (нет строк) | нет данных | results.json пуст |")
    for engine, status, reason in statuses:
        reason_md = reason.replace("|", "\\|") or "—"
        lines.append(f"| {engine} | {status} | {reason_md} |")
    lines.append("")

    lines += ["## Сводная таблица (движок × голос)", "",
              "synth_ms — медиана по категории, мс; RTF — средний по ok-строкам; "
              "peak RSS — максимум по ok-строкам; load_ms — холодная загрузка "
              "голоса (kokoro — None: модель живёт в sidecar). При -Repeats>1 "
              "медиана по всем тёплым повторам.", ""]
    cat_heads = " | ".join(f"{c} ({CATEGORY_TITLES[c].split(' ')[0]})"
                           for c in CATEGORY_ORDER)
    lines += ["| движок | голос | " + cat_heads
              + " | mean RTF | peak RSS, МБ | cold load_ms | Гц | ошибок |",
              "|---|---|" + "---|" * (len(CATEGORY_ORDER) + 6)]
    summary = summary_by_voice(rows)
    if not summary:
        lines.append("| (нет ok-строк) | | | | | | | | |")
    for item in summary:
        cats = " | ".join(_fmt(item["synth_ms"].get(c), 1)
                          for c in CATEGORY_ORDER)
        lines.append(
            f"| {item['engine']} | {item['voice']} | {cats} "
            f"| {_fmt(item['mean_rtf'], 3)} | {_fmt(item['peak_rss_mb'], 1)} "
            f"| {_fmt(item['load_ms'], 0)} | "
            f"{int(item['sample_rate']) if item['sample_rate'] else '—'} "
            f"| {item['errors']} |")
    lines.append("")

    lines += ["## Детальная таблица (фраза × движок × голос)", "",
              "Ячейка — медиана synth_ms, мс по повторам; «err» — ошибка. "
              "Полные данные (RTF, длительности, RSS по каждому повтору, "
              "точные тексты ошибок): `data/tts_ab/results.json`.", ""]
    detail = detail_rows(rows)
    pairs: list[tuple[str, str]] = []
    for _, _, row_cells in detail:
        for key in row_cells:
            if key not in pairs:
                pairs.append(key)
    heads = " | ".join(f"{e}/{v}" for e, v in pairs)
    lines += ["| фраза | кат. | " + heads + " |",
              "|---|---|" + "---|" * len(pairs)]
    for phrase_id, category, row_cells in detail:
        if phrase_id in ("aborted", "startup"):
            continue  # агрегированные строки-причины — не фраза
        cells = " | ".join(row_cells.get(p, "—") for p in pairs)
        lines.append(f"| {phrase_id} | {category} | {cells} |")
    lines.append("")

    lines += ["## Пути к WAV", "",
              "- Нативные (как синтезировал движок): "
              "`data/tts_ab/wav/<engine>/<voice>/<phrase_id>.wav` "
              "(14 фраз × выбранные голоса на движок).",
              "- Слепые (обезличенные, нормализованные): "
              "`data/tts_ab/blind/p<NN>_s<k>.wav`; маппинг — "
              "`data/tts_ab/blind/KEY.txt` (открывать после прослушки), "
              "форма оценки — `data/tts_ab/blind/form.md`.",
              "", "## Лицензии", ""]
    lines += [
        "- **Silero v5** (v5_6_ru, torch.hub snakers4/silero-models): веса "
        "**CC BY-NC-SA 4.0** — некоммерческое использование; личное домашнее "
        "использование — ок (зафиксировано решением по фиче). Веса кэшируются "
        "в `.cache/torch_hub/` (вне git).",
        "- **Supertonic 3**: код pip-пакета `supertonic` — **MIT**; веса "
        "(HF `Supertone/supertonic-3`) — **OpenRAIL-M**. Кэш весов — "
        "`.cache/hf/supertonic3/` (вне git).",
        "- **kokoro-ru**: модель уже установлена в проекте — `models/` "
        "(используется прод-sidecar `tts_kokoro_server.py`, `.venv-tts`); "
        "источник/лицензия — по карточке модели в `models/` (kokoro 0.9.4, "
        "код Apache-2.0).",
        "- **Piper medium**: голоса `models/ru_RU-denis-medium.onnx`, "
        "`models/ru_RU-irina-medium.onnx` (piper-tts 1.3.0 уже в `.venv`); "
        "источник/лицензия — по карточкам голосов rhasspy/piper-voices.",
        "",
    ]
    lines += ["## QA-сноски (обязательные, из ревью задач 3–4)", ""]
    for i, note in enumerate(_QA_FOOTNOTES, start=1):
        lines.append(f"{i}. {note}")
    lines.append("")

    lines += ["## Шаблон решения (заполнить после слепой прослушки)", "",
              "Матрица — по строкам критерии, по столбцам движки; качество "
              "берётся из form.md (слепая оценка), остальное — из таблиц выше.",
              "", "| критерий | kokoro-ru | Piper | Silero v5 | Supertonic 3 |",
              "|---|---|---|---|---|",
              "| скорость (synth_ms/RTF) |  |  |  |  |",
              "| память (peak RSS) |  |  |  |  |",
              "| качество (из form.md) |  |  |  |  |",
              "| лицензия |  |  |  |  |",
              "| стабильность |  |  |  |  |",
              "",
              "### Вариант A — оставить kokoro",
              "", "Аргументы: _", "",
              "### Вариант B — заменить на X",
              "", "X = _; аргументы: _", "",
              "### Вариант C — гибрид (kokoro для X, Y для Z)",
              "", "X = _; Y = _; Z = _; аргументы: _", ""]
    return "\n".join(lines)


def run_report(results_path: Path, out_path: Path) -> dict:
    """Собрать report.md из results.json (без синтеза).

    Отсутствие results.json / пустой файл — ValueError с внятным текстом
    (CLI переводит в код выхода 1, не краш).
    """
    if not results_path.exists():
        raise ValueError(
            f"results.json не найден: {results_path} — сначала выполни "
            "'python -m tts_ab run' (или укажи --results)")
    header, rows, problems = read_results_with_header(results_path)
    for problem in problems:
        print(f"tts_ab.report: {results_path.name}: {problem}")
    if not rows:
        raise ValueError(
            f"в {results_path} нет валидных строк результата — прогон не "
            "состоялся; перезапусти 'python -m tts_ab run'")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_report(header, rows), encoding="utf-8")
    statuses = engine_statuses(rows)
    ok_engines = sum(1 for _, status, _ in statuses
                     if status.startswith("ok"))
    return {"engines": len(statuses), "engines_ok": ok_engines,
            "rows": len(rows), "problems": len(problems)}
