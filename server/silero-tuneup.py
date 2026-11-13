# -*- coding: utf-8 -*-
"""Демо-прогон «silero tune-up» (исследовательский артефакт; прод не трогает).

Контекст: слепая фаза A/B TTS (tts_ab) прошла — пользователь выбрал silero
фаворитом, но слышал «акцент» и отсутствие цифр («14:30», «404» молча
пропадают — исследовательский вердикт, n-фразы ~100 мс). Прогон:
(1) доказывает фикс чисел нормализацией ru-normalizr, (2) даёт прослушать
ВСЕ 5 голосов silero v5_6_ru для выбора. Коммитов нет; прод-файлы,
tts_ab/ и прод-venv'ы не модифицируются (только read-only импорт).

Матрица синтеза (СТРОГО ПОСЛЕДОВАТЕЛЬНО, по одному файлу за раз):
  A) обзор голосов: {s1, m1} × {aidar, baya, eugene, kseniya, xenia} = 10
  B) цифры до/после: {n1, n2} × {raw, fixed} × {eugene, baya} = 8
  C) год 1703: l1 fixed × {eugene, baya} = 2
  D) эталон kokoro dima: {s1, n1, n2, l1} через ЖИВОЙ sidecar :8091 = 4
     (прод-текст без normalizr — kokoro цифры читает сам; sidecar
     недоступен → graceful пропуск группы с warning, форма отметит)

Варианты текста:
  raw   = текст фразы КАК ЕСТЬ из tts_ab/phrases.py (без нормализации —
          так текст уходил в движки харнесса, docstring phrases.py);
  prod  = clean_for_speech → normalize_for_speech (прод-пайплайн
          speech_text.py; цифры ОСТАВЛЯЕТ цифрами: «14:30» → «14 часов
          30 минут»);
  fixed = silero_normalize = normalizr(tts)(prod) — будущий прод-композит
          для silero (порядок этапов clean → speech_text → normalizr
          зафиксирован).

Запуск (из каталога server): .venv-ab\\Scripts\\python.exe silero-tuneup.py
Зависимость: ru-normalizr==1.3.0 в .venv-ab (pip ТОЛЬКО туда).
Выход: data/tts_ab/silero_demo/ — WAV + manifest.json +
normalized_texts.md + form.md.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Окружение ДО первого импорта torch (адаптер импортирует его лениво в
# load()): кэш весов silero — как в драйвере харнесса (_child_env).
os.environ["TORCH_HOME"] = str(BASE_DIR / ".cache" / "torch_hub")
os.environ.setdefault("PYTHONUTF8", "1")

from ru_normalizr import Normalizer, NormalizeOptions  # noqa: E402
from speech_text import clean_for_speech, normalize_for_speech  # noqa: E402
import tts_ab.adapters as adapters  # noqa: E402
from tts_ab.phrases import PHRASE_BY_ID  # noqa: E402

DEMO_DIR = BASE_DIR / "data" / "tts_ab" / "silero_demo"

# Голоса silero v5_6_ru: мужские aidar/eugene, женские baya/kseniya/xenia.
SILERO_VOICES: tuple[str, ...] = ("aidar", "baya", "eugene", "kseniya", "xenia")
DIGIT_VOICES: tuple[str, ...] = ("eugene", "baya")

GROUP_A_PIDS: tuple[str, ...] = ("s1", "m1")
GROUP_B_PIDS: tuple[str, ...] = ("n1", "n2")
KOKORO_PIDS: tuple[str, ...] = ("s1", "n1", "n2", "l1")
# Все фразы с fixed-текстом — для normalized_texts.md.
REVIEW_PIDS: tuple[str, ...] = ("s1", "m1", "n1", "n2", "l1")

_KOKORO_VOICE = "dima"
_DIGITS_RE = re.compile(r"[0-9]")

try:
    _NORMALIZR_VERSION = importlib.metadata.version("ru-normalizr")
except importlib.metadata.PackageNotFoundError:
    raise SystemExit(
        "ru-normalizr не установлен в .venv-ab — установи: "
        ".venv-ab\\Scripts\\python.exe -m pip install ru-normalizr==1.3.0"
    ) from None

_NORMALIZER = Normalizer(NormalizeOptions.tts())


def _log(msg: str) -> None:
    print(f"[tuneup] {msg}", flush=True)


def _reconfigure_utf8() -> None:
    """UTF-8 stdout/stderr независимо от кодовой страницы консоли."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def prod_text(text: str) -> str:
    """Прод-пайплайн speech_text: clean_for_speech → normalize_for_speech."""
    return normalize_for_speech(clean_for_speech(text))


def silero_normalize(text: str) -> str:
    """Фикс для silero: normalizr(tts) ПОВЕРХ прод-пайплайна.

    Порядок этапов СОХРАНЁН (clean → speech_text-normalize → normalizr):
    это будущая прод-композиция. Смоук показал, что композиция даёт лучший
    стиль, чем normalizr по сырому тексту: «14 часов 30 минут» →
    «четырнадцать часов тридцать минут» (без висячей запятой).
    """
    return _NORMALIZER.normalize(prod_text(text))


def _build_silero():
    """Адаптер silero харнесса с регистрацией ВСЕХ 5 голосов v5_6_ru.

    Адаптер валидирует голоса по харнесс-паре ENGINE_VOICES["silero"]
    (eugene/xenia), а демо требует все 5 голосов пакета. Грузим модель
    РОВНО тем же adapter.load("eugene") (torch.hub → кэш TORCH_HOME) и
    регистрируем ОДИН инстанс на все голоса: пакет v5_6_ru содержит их
    все (см. докстринг _SileroEngine в tts_ab/adapters.py), спикер
    выбирается на синтезе apply_tts(speaker=...). Регистрация идёт в
    инстанс-кэш адаптера (_voices) — модуль tts_ab не модифицируется.
    Дальше синтез идёт штатными engine.warmup/engine.synth адаптера
    (per-phrase timeout 120 с, PCM16 mono 48 кГц, synth_ms без диска).
    """
    engine = adapters.ENGINES["silero"]()
    engine.load("eugene")
    model = engine._voices["eugene"]
    for voice in SILERO_VOICES:
        engine._voices[voice] = model
    return engine


def _build_kokoro() -> tuple[object, str | None]:
    """Kokoro-адаптер харнесса + ЖИВОЙ sidecar :8091 (НЕ запуск/стоп).

    Возвращает (engine, down_error): down_error не None — sidecar не
    ответил на /health (штатный graceful исход харнесса), группу D
    пропускаем с warning.
    """
    engine = adapters.ENGINES["kokoro"]()
    engine.startup()
    return engine, getattr(engine, "_down_error", None)


def _synth_row(engine, voice: str, phrase_id: str, text: str,
               file_name: str, group: str, variant: str,
               warmed: set[str]) -> dict:
    """Один синтез (строго последовательно) → строка манифеста."""
    if voice not in warmed:
        engine.warmup(voice)  # «Проверка связи» — прогрев, наружу не идёт
        warmed.add(voice)
    path = DEMO_DIR / file_name
    synth_ms = engine.synth(text, voice, path)
    duration, rate = adapters._wav_stats(path)
    duration_s = None if duration is None else round(duration, 3)
    row = {
        "file": file_name,
        "group": group,
        "voice": voice,
        "phrase_id": phrase_id,
        "variant": variant,
        "text_final": text,
        "synth_ms": round(synth_ms, 1),
        "duration_s": duration_s,
        "sample_rate": rate,
    }
    dur = "?" if duration_s is None else f"{duration_s:.1f} с"
    _log(f"{group} {file_name}: {voice}, synth={synth_ms:.0f} мс, "
         f"{dur} @ {rate} Гц")
    if duration_s is None or duration_s <= 0:
        _log(f"ВНИМАНИЕ: {file_name}: пустая длительность ({duration_s})!")
    elif duration_s < 0.5:
        _log(f"ВНИМАНИЕ: {file_name}: короче 0.5 с ({duration_s} с) — "
             f"это ожидаемо для raw-вариантов (цифры пропущены), "
             f"но не для fixed")
    return row


def _variant_rows(variants: dict[str, dict[str, str]]) -> None:
    """Лог-заголовок цепочек текста (raw → prod → fixed)."""
    for pid in REVIEW_PIDS:
        _log(f"{pid} raw  : {variants[pid]['raw']}")
        _log(f"{pid} prod : {variants[pid]['prod']}")
        _log(f"{pid} fixed: {variants[pid]['fixed']}")


def _write_normalized_texts(variants: dict[str, dict[str, str]],
                            digit_warnings: list[str],
                            kokoro_skips: list[str]) -> None:
    """normalized_texts.md: исходник → итоговый текст silero + проверка цифр."""
    lines: list[str] = [
        "# silero tune-up — нормализация текста (что прочитает silero)",
        "",
        f"- ru-normalizr {_NORMALIZR_VERSION}, профиль `tts`;"
        " порядок этапов зафиксирован:",
        "  `clean_for_speech` → `normalize_for_speech` (прод speech_text.py)"
        " → `normalizr(tts)`.",
        "- `raw` = текст КАК ЕСТЬ из `tts_ab/phrases.py` (без нормализации"
        " — так текст уходил в движки харнесса);",
        "- `prod` = прод-пайплайн (цифры ОСТАВЛЯЕТ цифрами:"
        " «14:30» → «14 часов 30 минут»);",
        "- `fixed` = `silero_normalize` (будущий прод-композит для silero).",
        "- raw-варианты группы B синтезированы из `raw`, группы A/C —"
        " из `fixed`, группа D (kokoro) — из `prod`.",
        "",
        "| pid | raw (исходник) | prod (прод-пайплайн) |"
        " fixed (прочитает silero) | [0-9] в fixed? |",
        "|---|---|---|---|---|",
    ]
    for pid in REVIEW_PIDS:
        v = variants[pid]
        has_digits = bool(_DIGITS_RE.search(v["fixed"]))
        verdict = ("**ОСТАЛИСЬ ЦИФРЫ**" if has_digits else "**нет — OK**")
        lines.append(
            f"| {pid} | {v['raw']} | {v['prod']} | {v['fixed']} | {verdict} |")
    lines += [
        "",
        "## Проверка цифр n1/n2/l1 (жирно — вердикт)",
        "",
    ]
    for pid in ("n1", "n2", "l1"):
        v = variants[pid]
        has_digits = bool(_DIGITS_RE.search(v["fixed"]))
        if has_digits:
            lines.append(f"- **{pid}: В fixed-тексте ОСТАЛИСЬ ASCII-ЦИФРЫ**:"
                         f" `{v['fixed']}`")
        else:
            lines.append(f"- **{pid}: ASCII-цифр в fixed-тексте НЕТ** —"
                         f" silero прочтёт всё словами: `{v['fixed']}`")
    lines += [
        "",
        "## Замечания нормализатора (что он сделал неожиданного)",
        "",
        "- Время: прод-«14 часов 30 минут» → «четырнадцать часов"
        " тридцать минут» (развёрнутый стиль); normalizr по сырой"
        " «14:30» дал бы «четырнадцать, тридцать» — поэтому порядок"
        " прод-пайплайн → normalizr зафиксирован.",
        "- Год: «в 1703 году» → «в тысяча семьсот третьем году» —"
        " согласование в предложном падеже.",
        "- Латиница: HTTP → «эйч ти ти пи», Wi-Fi → «вайфай»"
        " (латинизация tts-профиля).",
        "- Кавычки-ёлочки «Покупки» normalizr меняет на прямые"
        ' "Покупки" — для TTS беззвучно.',
        "- Дроби словами («пяти с половиной миллионов») normalizr"
        " не трогает — уже читаемо.",
        "",
    ]
    if digit_warnings:
        lines.append(f"- **ПРЕДУПРЕЖДЕНИЕ: фразы с оставшимися цифрами:"
                     f" {', '.join(digit_warnings)}**")
    if kokoro_skips:
        lines.append(f"- Kokoro-эталон (группа D): пропуски —"
                     f" {'; '.join(kokoro_skips)}")
    path = DEMO_DIR / "normalized_texts.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    _log(f"записан {path.name}")


def _write_form(kokoro_skips: list[str]) -> None:
    """form.md — форма прослушки (структура ТЗ демо)."""
    lines: list[str] = [
        "# silero tune-up — форма прослушки",
        "",
        "Файлы в `data/tts_ab/silero_demo/`. Обозначения:"
        " **raw** = текст как есть (числа пропущены — так и было),"
        " **fixed** = silero + ru-normalizr (композиция будущего прода),"
        " **prod** = прод-текст speech_text (kokoro цифры читает сам).",
        "Голоса silero v5_6_ru: **aidar** (м), **baya** (ж),"
        " **eugene** (м), **kseniya** (ж), **xenia** (ж).",
        "",
        "## Таблица A — обзор 5 голосов",
        "",
        "| голос | файлы | s1 естественность 1–5 |"
        " m1 естественность 1–5 | акцент: нет/лёгкий/заметный (в чём?)"
        " | впечатление строкой |",
        "|---|---|---|---|---|---|",
    ]
    for voice in SILERO_VOICES:
        gender = "м" if voice in ("aidar", "eugene") else "ж"
        files = f"`char_{voice}_s1.wav` / `char_{voice}_m1.wav`"
        lines.append(f"| {voice} ({gender}) | {files} | | | | |")
    lines += [
        "",
        "**лучший муж. голос:** ______________________",
        "",
        "**лучшая жен. голос:** ______________________",
        "",
        "## Таблица B — цифры до/после (raw vs fixed)",
        "",
        "| фраза | вариант | голос | файл | цифры произносятся?"
        " да/нет | комментарий |",
        "|---|---|---|---|---|---|",
    ]
    for pid in GROUP_B_PIDS:
        for voice in DIGIT_VOICES:
            lines.append(f"| {pid} | raw | {voice} |"
                         f" `digits_raw_{pid}_{voice}.wav` | | |")
            lines.append(f"| {pid} | fixed | {voice} |"
                         f" `digits_fixed_{pid}_{voice}.wav` | | |")
    lines += [
        "",
        "## Таблица C — год 1703 (l1 fixed)",
        "",
        "| фраза | голос | файл | как прочитан 1703?"
        " нормально/странно (как именно) |",
        "|---|---|---|---|",
    ]
    for voice in DIGIT_VOICES:
        lines.append(f"| l1 | {voice} | `year_fixed_l1_{voice}.wav` | | |")
    lines += [
        "",
        "## Таблица D — kokoro dima (эталон, prod-текст) vs silero fixed",
        "",
        "| фраза | kokoro dima | silero fixed (eugene)"
        " | ранжирование 1–2 | комментарий |",
        "|---|---|---|---|---|",
    ]
    d_pairs = {
        "s1": ("ref_kokoro_s1_dima.wav", "char_eugene_s1.wav"),
        "n1": ("ref_kokoro_n1_dima.wav", "digits_fixed_n1_eugene.wav"),
        "n2": ("ref_kokoro_n2_dima.wav", "digits_fixed_n2_eugene.wav"),
        "l1": ("ref_kokoro_l1_dima.wav", "year_fixed_l1_eugene.wav"),
    }
    for pid, (ref, sil) in d_pairs.items():
        lines.append(f"| {pid} | `{ref}` | `{sil}` | | |")
    lines += [
        "",
        "(для n2/l1 есть и baya-варианты:"
        " `digits_fixed_n2_baya.wav`, `year_fixed_l1_baya.wav`)",
        "",
    ]
    if kokoro_skips:
        lines += [
            "**kokoro-эталон недоступен (graceful): строки группы D"
            f" пропущены — {'; '.join(kokoro_skips)}**",
            "",
        ]
    lines += [
        "## Свободные замечания",
        "",
        "- ",
        "- ",
        "",
        "## Если где-то слышится акцент — в каких фразах/словах?",
        "",
        "- ",
        "- ",
        "",
    ]
    path = DEMO_DIR / "form.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    _log(f"записан {path.name}")


def main() -> int:
    _reconfigure_utf8()
    started = time.perf_counter()
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"ru-normalizr {_NORMALIZR_VERSION} (профиль tts);"
         f" фразы — tts_ab/phrases.py; выход — {DEMO_DIR}")

    # Цепочки текста для всех фраз демо + проверка цифр в fixed.
    variants: dict[str, dict[str, str]] = {}
    digit_warnings: list[str] = []
    for pid in REVIEW_PIDS:
        raw = PHRASE_BY_ID[pid].text
        variants[pid] = {
            "raw": raw,
            "prod": prod_text(raw),
            "fixed": silero_normalize(raw),
        }
        if _DIGITS_RE.search(variants[pid]["fixed"]):
            digit_warnings.append(pid)
    _variant_rows(variants)
    if digit_warnings:
        _log(f"ВНИМАНИЕ: в fixed-текстах остались цифры: {digit_warnings}")

    rows: list[dict] = []
    warmed: set[str] = set()

    # --- A: обзор голосов {s1, m1} × 5 голосов -----------------------------
    _log("=== A: обзор голосов {s1, m1} × {aidar, baya, eugene,"
         " kseniya, xenia} ===")
    silero = _build_silero()
    for voice in SILERO_VOICES:
        for pid in GROUP_A_PIDS:
            rows.append(_synth_row(
                silero, voice, pid, variants[pid]["fixed"],
                f"char_{voice}_{pid}.wav", "A", "fixed", warmed))

    # --- B: цифры до/после {n1, n2} × {raw, fixed} × {eugene, baya} --------
    _log("=== B: цифры до/после {n1, n2} × {raw, fixed} ×"
         " {eugene, baya} ===")
    for pid in GROUP_B_PIDS:
        for voice in DIGIT_VOICES:
            rows.append(_synth_row(
                silero, voice, pid, variants[pid]["raw"],
                f"digits_raw_{pid}_{voice}.wav", "B", "raw", warmed))
            rows.append(_synth_row(
                silero, voice, pid, variants[pid]["fixed"],
                f"digits_fixed_{pid}_{voice}.wav", "B", "fixed", warmed))

    # --- C: год 1703 — l1 fixed × {eugene, baya} ---------------------------
    _log("=== C: год 1703 — l1 fixed × {eugene, baya} ===")
    for voice in DIGIT_VOICES:
        rows.append(_synth_row(
            silero, voice, "l1", variants["l1"]["fixed"],
            f"year_fixed_l1_{voice}.wav", "C", "fixed", warmed))

    # --- D: эталон kokoro dima (живой sidecar :8091; graceful) -------------
    _log("=== D: эталон kokoro dima {s1, n1, n2, l1}"
         " (прод-текст, без normalizr) ===")
    kokoro_skips: list[str] = []
    kokoro, down = _build_kokoro()
    if down is not None:
        kokoro_skips.append(f"sidecar недоступен: {down}")
        _log(f"ВНИМАНИЕ: kokoro sidecar недоступен — группа D пропущена"
             f" ({down})")
    else:
        for pid in KOKORO_PIDS:
            try:
                rows.append(_synth_row(
                    kokoro, _KOKORO_VOICE, pid, variants[pid]["prod"],
                    f"ref_kokoro_{pid}_dima.wav", "D", "prod", warmed))
            except Exception as exc:
                skip = f"{pid}: {type(exc).__name__}: {exc}"
                kokoro_skips.append(skip)
                _log(f"ВНИМАНИЕ: kokoro {pid} пропущена — {skip}")

    # --- Выход: манифест + md ----------------------------------------------
    manifest = DEMO_DIR / "manifest.json"
    manifest.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    _log(f"записан manifest.json ({len(rows)} строк)")
    _write_normalized_texts(variants, digit_warnings, kokoro_skips)
    _write_form(kokoro_skips)

    groups: dict[str, int] = {}
    for row in rows:
        groups[row["group"]] = groups.get(row["group"], 0) + 1
    _log(f"Готово за {time.perf_counter() - started:.0f} с:"
         f" {len(rows)} wav ({groups}) → {DEMO_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
