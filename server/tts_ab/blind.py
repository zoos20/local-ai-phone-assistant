# -*- coding: utf-8 -*-
"""Слепой режим TTS A/B харнесса (задача 5; ТЗ §3.5).

Все ok-WAV копируются в data/tts_ab/blind/ под обезличенными именами
p<NN>_s<k>.wav: группа = (фраза × гендер), в группе по одному файлу на
движок; s1..sN — случайная перестановка движков (seed по умолчанию
фиксированный — перезапуск blind воспроизводим), СВОЯ на каждую
фразу-группу. Перестановка НЕ вырождена: ни один движок не сидит на
одной и той же позиции во всех своих группах (иначе чинится обменом
слотов). Копии проходят пиковую нормализацию громкости (numpy, лениво —
в .venv его нет, риск №8 плана) и перезаписываются stdlib wave —
посторонние чанки/метаданные срезаются (анти-подсказка).

KEY.txt — предупреждение + маппинг файлов → движок×голос + seed
(открывать ПОСЛЕ прослушки). form.md — таблицы рекомендуемого подсета
(5 фраз × 2 гендерные группы: s1/m1/l1/n1/h1) + свободные замечания +
CSV-формат. Имена движков/голосов НЕ появляются ни в именах файлов,
ни в form.md.
"""
from __future__ import annotations

import random
import re
import sys
import wave
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from tts_ab.adapters import ENGINE_VOICES
from tts_ab.phrases import CATEGORY_TITLES, PHRASES, PHRASE_BY_ID
from tts_ab.report import read_results_with_header

BASE_DIR = Path(__file__).resolve().parent.parent

# Порядок движков при тасовании слотов (канонический, детерминированный).
BLIND_ENGINE_ORDER: tuple[str, ...] = ("kokoro", "piper", "silero", "supertonic")

# Фиксированный seed по умолчанию: одинаковый results.json + одинаковый
# seed → одинаковые имена слепых файлов (перезапуск blind стабилен).
DEFAULT_SEED = 20261112

# p<NN>_s<k>.wav: NN ≥ 2 цифр, k ≥ 1 (существует хотя бы 1 слот).
BLIND_NAME_RE = re.compile(r"^p\d{2,}_s[1-9]\d*\.wav$")

# Целевой пик после нормализации — 0.98 полной шкалы (запас от клиппинга).
_TARGET_PEAK = 0.98


class BlindSlot(NamedTuple):
    """Один файл группы: slot=k в имени, движок, голос, исходный wav."""

    slot: int
    engine: str
    voice: str
    wav_path: str


class BlindGroup(NamedTuple):
    """Фраза-группа: (phrase_id, gender) + слоты s1..sN."""

    group_no: int
    phrase_id: str
    gender: str  # "m" | "f"
    slots: list[BlindSlot]


class BlindPlan(NamedTuple):
    """Полный план слепых файлов: seed + группы в порядке NN."""

    seed: int
    groups: list[BlindGroup]


def blind_filename(group_no: int, slot: int) -> str:
    """Обезличенное имя файла: p<NN>_s<k>.wav (ASCII, без имён движков)."""
    name = f"p{group_no:02d}_s{slot}.wav"
    if not BLIND_NAME_RE.fullmatch(name):  # защита от мусора в номерах
        raise ValueError(f"unsafe blind filename: {name!r}")
    return name


def gender_of(engine: str, voice: str) -> str | None:
    """Гендер голоса по таблице ENGINE_VOICES; неизвестный → None."""
    pair = ENGINE_VOICES.get(engine)
    if pair is None:
        return None
    if voice == pair[0]:
        return "m"
    if voice == pair[1]:
        return "f"
    return None


def _degenerate_engines(groups: list[BlindGroup]) -> set[str]:
    """Движки, занимающие ОДНУ И ТУ ЖЕ позицию во всех своих группах."""
    slots_by_engine: dict[str, set[int]] = {}
    for group in groups:
        for slot in group.slots:
            slots_by_engine.setdefault(slot.engine, set()).add(slot.slot)
    return {engine for engine, slots in slots_by_engine.items()
            if len(slots) == 1}


def _swap(group: BlindGroup, engine_a: str, engine_b: str) -> BlindGroup:
    """Обменять движки a и b их слотами внутри группы."""
    slot_a = next(s.slot for s in group.slots if s.engine == engine_a)
    slot_b = next(s.slot for s in group.slots if s.engine == engine_b)
    slots = []
    for slot in group.slots:
        if slot.engine == engine_a:
            slots.append(slot._replace(slot=slot_b))
        elif slot.engine == engine_b:
            slots.append(slot._replace(slot=slot_a))
        else:
            slots.append(slot)
    return group._replace(slots=sorted(slots, key=lambda s: s.slot))


def _fix_degenerate(plan: BlindPlan) -> BlindPlan:
    """Убрать вырожденность обменами слотов (детерминированно).

    Шаг: берём алфавитно первый вырожденный движок и среди ВСЕХ его
    соседей по всем группам выбираем обмен по оценке результата —
    пробуем замену группы, считаем _degenerate_engines, предпочитаем
    минимум оставшихся вырожденных (при равенстве — сосед по алфавиту,
    затем номер группы). Обмен «в лоб» (первый сосед первой группы)
    зацикливается, когда два вырожденных движка — взаимные первые
    соседи: обмен А↔Б немедленно откатывается обменом Б↔А.

    Движок ровно в ОДНОЙ группе (или один на весь прогон — частичный
    прогон) не чинится: любой обмен оставляет ему одно значение —
    кандидатов нет, warning в stderr. Если бюджет шагов исчерпан, а
    вырожденность осталась у движка с соседями — RuntimeError
    (нарушен инвариант выбора обмена).
    """
    groups = list(plan.groups)
    for _ in range(4 * len(groups) + 16):
        bad = sorted(_degenerate_engines(groups))
        if not bad:
            return BlindPlan(plan.seed, groups)
        engine = bad[0]
        candidates = []
        for gi, group in enumerate(groups):
            engines_here = {s.engine for s in group.slots}
            if engine not in engines_here or len(engines_here) < 2:
                continue
            for other in sorted(engines_here - {engine}):
                trial = _swap(group, engine, other)
                remaining = _degenerate_engines(
                    groups[:gi] + [trial] + groups[gi + 1:])
                if engine in remaining:
                    continue  # обмен не даёт движку вторую позицию
                candidates.append((len(remaining), other, gi, trial))
        if not candidates:
            break  # соседей нет (или движок в одной группе) — не чинится
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))
        groups[candidates[0][2]] = candidates[0][3]
    leftover = _degenerate_engines(groups)
    unfixable = {
        engine for engine in leftover
        if all(len({s.engine for s in g.slots}) < 2
               for g in groups
               if any(s.engine == engine for s in g.slots))
    }
    if leftover - unfixable:
        raise RuntimeError(
            "tts_ab.blind: обмены не устранили вырожденность слепой "
            f"перестановки для {sorted(leftover - unfixable)} "
            "(инвариант выбора обмена в _fix_degenerate нарушен)")
    for engine in sorted(unfixable):
        print(f"tts_ab.blind: движок {engine} один во всех своих группах "
              "(частичный прогон?) — перестановка вырождена, чинить "
              "нечем", file=sys.stderr)
    return BlindPlan(plan.seed, groups)


def plan_blind_groups(
    rows: Iterable[dict],
    *,
    seed: int = DEFAULT_SEED,
    engine_order: tuple[str, ...] = BLIND_ENGINE_ORDER,
) -> BlindPlan:
    """Детерминированный план слепых файлов из ok-строк результатов.

    Вход — валидные строки контракта metrics (нужны engine, voice,
    phrase_id, ok, wav_path); строки ok=false и строки без wav_path
    пропускаются. Группы упорядочены по набору фраз (PHRASES) × гендер
    (м, ж); незнакомые phrase_id — в конце по алфавиту. Внутри группы
    слоты — случайная перестановка присутствующих движков (random.
    Random(seed), поток на весь план); результат чинится от
    вырожденности (_fix_degenerate).
    """
    by_group: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("ok"):
            continue
        engine, voice = row.get("engine"), row.get("voice")
        phrase_id, wav_path = row.get("phrase_id"), row.get("wav_path")
        if not all(isinstance(v, str) and v for v in
                   (engine, voice, phrase_id, wav_path)):
            continue
        gender = gender_of(engine, voice)
        if gender is None:
            continue
        by_group.setdefault((phrase_id, gender), {})[engine] = (voice,
                                                               wav_path)
    order = [(p.phrase_id, g) for p in PHRASES for g in ("m", "f")]
    keys = [key for key in order if key in by_group]
    keys += sorted(set(by_group) - set(order))
    rng = random.Random(seed)
    groups: list[BlindGroup] = []
    for group_no, (phrase_id, gender) in enumerate(keys, start=1):
        members = by_group[(phrase_id, gender)]
        engines = [e for e in engine_order if e in members]
        engines += sorted(set(members) - set(engines))
        rng.shuffle(engines)
        slots = [BlindSlot(slot_no, engine, members[engine][0],
                           members[engine][1])
                 for slot_no, engine in enumerate(engines, start=1)]
        groups.append(BlindGroup(group_no, phrase_id, gender, slots))
    return _fix_degenerate(BlindPlan(seed, groups))


def render_key(plan: BlindPlan) -> str:
    """KEY.txt: предупреждение + seed + маппинг файлов → движок×голос."""
    lines = [
        "НЕ ОТКРЫВАТЬ ДО ПРОСЛУШКИ",
        "=========================",
        "",
        "Это ключ слепого сравнения TTS A/B харнесса. Открывать только",
        "ПОСЛЕ заполнения data/tts_ab/blind/form.md.",
        "",
        f"Seed перестановки: {plan.seed} (одинаковый seed + тот же",
        "results.json — те же имена файлов; воспроизводимость).",
        "",
    ]
    for group in plan.groups:
        label = "м" if group.gender == "m" else "ж"
        lines.append(f"Группа {group.group_no:02d} — фраза "
                     f"{group.phrase_id}, голос {label}:")
        for slot in sorted(group.slots, key=lambda s: s.slot):
            lines.append(f"  {blind_filename(group.group_no, slot.slot)}"
                         f" = {slot.engine} × {slot.voice}")
        lines.append("")
    return "\n".join(lines)


def recommended_subset(subset_size: int = 5) -> list[str]:
    """Рекомендуемый подсет фраз формы: по одной из s/m/l/n/h (s1,m1,...).

    Детерминированно первые id категорий по кругу: size=5 →
    s1, m1, l1, n1, h1; size=7 → + s2, m2.
    """
    categories = ("s", "m", "l", "n", "h")
    buckets = [[p.phrase_id for p in PHRASES if p.category == c]
               for c in categories]
    picked: list[str] = []
    index = 0
    while (len(picked) < subset_size
           and any(index < len(b) for b in buckets)):
        for bucket in buckets:
            if len(picked) >= subset_size:
                break
            if index < len(bucket):
                picked.append(bucket[index])
        index += 1
    return picked


def render_form(plan: BlindPlan, subset_ids: Iterable[str]) -> str:
    """form.md: таблицы подсета + ранжирование + замечания + CSV.

    Анти-подсказка: только имена файлов и тексты фраз — имён движков и
    голосов здесь нет.
    """
    groups = {(g.phrase_id, g.gender): g for g in plan.groups}
    lines = [
        "# Слепая прослушка TTS A/B — форма оценки",
        "",
        "Заполняется ВРУЧНУЮ после прослушивания файлов из "
        "data/tts_ab/blind/.",
        "Ключ соответствия — data/tts_ab/blind/KEY.txt: открыть ТОЛЬКО",
        "после заполнения этой формы. Громкость всех файлов нормализована",
        "к общему пику, имена обезличены (p<NN>_s<k>.wav).",
        "",
        "Рекомендуемый подсет — по одной фразе из каждой категории",
        "(s — короткие, m — средние, l — длинные, n — числа/латиница,",
        "h — ударения) × 2 гендерные группы. Файлы остальных фраз тоже",
        "лежат в blind/ — форму можно расширить по аналогии.",
        "",
    ]
    for phrase_id in subset_ids:
        for gender in ("m", "f"):
            group = groups.get((phrase_id, gender))
            if group is None:
                continue
            phrase = PHRASE_BY_ID.get(phrase_id)
            text = phrase.text if phrase is not None else phrase_id
            title = CATEGORY_TITLES.get(phrase.category, "") if phrase else ""
            voice_label = ("мужской голос" if gender == "m"
                           else "женский голос")
            lines.append(f"## {phrase_id} ({voice_label})"
                         f" — «{text}»  [{title}]")
            lines += ["| файл | естественность 1–5 | комментарий |",
                      "|---|---|---|"]
            for slot in sorted(group.slots, key=lambda s: s.slot):
                lines.append(f"| {blind_filename(group.group_no, slot.slot)}"
                             " |  |  |")
            names = ", ".join(
                f"{blind_filename(group.group_no, s.slot)}=__"
                for s in sorted(group.slots, key=lambda s: s.slot))
            count = len(group.slots)
            lines += ["", f"Итог (ранжирование 1–{count}, 1 = лучший): "
                          f"{names}", ""]
    lines += [
        "## Свободные замечания",
        "",
        "(артефакты, ударения, интонация, обрывы — своими словами;",
        "выводы по движкам добавь ПОСЛЕ открытия KEY.txt)",
        "",
        "________________________________________________________________",
        "",
        "________________________________________________________________",
        "",
        "## CSV для быстрого ввода",
        "",
        "Формат строки: phrase_id,file,score,comment",
        "",
    ]
    for phrase_id in subset_ids:
        for gender in ("m", "f"):
            group = groups.get((phrase_id, gender))
            if group is None:
                continue
            for slot in sorted(group.slots, key=lambda s: s.slot):
                lines.append(f"{phrase_id},"
                             f"{blind_filename(group.group_no, slot.slot)},,")
    lines.append("")
    return "\n".join(lines)


# --- Нормализация и запись копий (numpy ЛЕНИВО — только .venv-ab) ---------


_FULL_SCALE = {1: 128.0, 2: 32767.0, 4: 2147483647.0}


def normalize_wav_copy(src: Path, dst: Path) -> tuple[int, int]:
    """Пиковая нормализация src → dst (numpy лениво + stdlib wave).

    Скалярный множитель к общему целевому пику (_TARGET_PEAK полной
    шкалы); запись через stdlib wave — файл собирается заново (fmt+data),
    посторонние чанки/метаданные срезаются (анти-подсказка). Возвращает
    (sample_rate, sample_width). 24-bit не поддерживается (движки
    харнесса его не пишут).
    """
    import numpy as np  # лениво: numpy есть только в .venv-ab (риск №8)

    with wave.open(str(src), "rb") as reader:
        channels = reader.getnchannels()
        width = reader.getsampwidth()
        rate = reader.getframerate()
        raw = reader.readframes(reader.getnframes())
    if width == 3:
        raise ValueError(f"24-bit WAV не поддерживается: {src.name}")
    if width not in _FULL_SCALE:
        raise ValueError(f"неподдержанная ширина сэмпла: {width}")
    if width == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(
            np.float64) - 128.0
    else:
        dtype = np.dtype("<i2") if width == 2 else np.dtype("<i4")
        data = np.frombuffer(raw, dtype=dtype).astype(np.float64)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    if peak > 0.0:
        data = data * (_TARGET_PEAK * _FULL_SCALE[width] / peak)
    if width == 1:
        out = np.rint(np.clip(data + 128.0, 0.0, 255.0)).astype(np.uint8)
    else:
        high = _FULL_SCALE[width]
        out = np.rint(np.clip(data, -(high + 1.0), high)).astype(
            np.dtype("<i2") if width == 2 else np.dtype("<i4"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(out.tobytes())
    return rate, width


def _stale_blind_files(blind_dir: Path, keep: set[str]) -> list[Path]:
    """Старые p*_s*.wav, которых нет в текущем плане (удалить)."""
    stale: list[Path] = []
    for path in blind_dir.iterdir():
        if (path.is_file() and BLIND_NAME_RE.fullmatch(path.name)
                and path.name not in keep):
            stale.append(path)
    return sorted(stale)


def run_blind(
    results_path: Path,
    blind_dir: Path,
    *,
    seed: int = DEFAULT_SEED,
    subset_size: int = 5,
) -> dict:
    """Создать data/tts_ab/blind/ (файлы + KEY.txt + form.md) из results.

    Без повторного синтеза: читает results.json, ok-WAV копирует
    нормализованными под обезличенными именами. Отсутствие results.json
    — ValueError (CLI → код 1); нет ни одного ok-WAV — внятное
    сообщение и пустая сводка (не ошибка: движки могли все fail).
    """
    if not results_path.exists():
        raise ValueError(
            f"results.json не найден: {results_path} — сначала выполни "
            "'python -m tts_ab run' (или укажи --results)")
    _header, rows, problems = read_results_with_header(results_path)
    for problem in problems:
        print(f"tts_ab.blind: {results_path.name}: {problem}", file=sys.stderr)
    missing_sources: list[str] = []
    usable: list[dict] = []
    for row in rows:
        wav_rel = row.get("wav_path")
        if not (row.get("ok") and isinstance(wav_rel, str) and wav_rel):
            continue
        if (BASE_DIR / wav_rel).exists():
            usable.append(row)
        else:
            missing_sources.append(wav_rel)
    for wav_rel in missing_sources:
        print(f"tts_ab.blind: исходник не найден, пропущен: {wav_rel}",
              file=sys.stderr)
    plan = plan_blind_groups(usable, seed=seed)
    blind_dir.mkdir(parents=True, exist_ok=True)
    if not any(group.slots for group in plan.groups):
        print("tts_ab.blind: нет ok-WAV — слепые файлы не созданы "
              "(все движки недоступны/упали?)", file=sys.stderr)
        return {"seed": seed, "groups": 0, "files": 0,
                "missing": len(missing_sources)}
    keep: set[str] = set()
    written = 0
    for group in plan.groups:
        for slot in group.slots:
            name = blind_filename(group.group_no, slot.slot)
            keep.add(name)
            normalize_wav_copy(BASE_DIR / slot.wav_path, blind_dir / name)
            written += 1
    for path in _stale_blind_files(blind_dir, keep):
        path.unlink(missing_ok=True)
        print(f"tts_ab.blind: удалён устаревший файл {path.name}",
              file=sys.stderr)
    (blind_dir / "KEY.txt").write_text(render_key(plan), encoding="utf-8")
    subset = recommended_subset(subset_size)
    (blind_dir / "form.md").write_text(
        render_form(plan, subset), encoding="utf-8")
    return {"seed": seed, "groups": len(plan.groups), "files": written,
            "missing": len(missing_sources), "subset": subset}
