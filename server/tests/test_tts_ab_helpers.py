"""Тесты хелперов TTS A/B харнесса (задачи 1 и 5 плана tts-ab-tasklist.md).

ТОЛЬКО чистые хелперы tts_ab.phrases/tts_ab.metrics/tts_ab.blind:
ЗАПРЕЩЕНО импортировать torch, piper, supertonic, psutil, numpy и любые
прод-модули (тесты гоняются в .venv, где тяжёлых зависимостей нет и
быть не должно — риск №8 ТЗ; numpy в blind.py импортируется ЛЕНИВО).
Итог по файлу 15–25 тестов (задача 5 добавила тесты слепых имён).
"""
import wave
import pytest

from tts_ab.adapters import ENGINE_VOICES
from tts_ab.blind import (
    BLIND_NAME_RE,
    blind_filename,
    gender_of,
    plan_blind_groups,
    recommended_subset,
    render_form,
    render_key,
)
from tts_ab.metrics import (
    REQUEST_FIELDS,
    compute_rtf,
    merge_result_rows,
    read_result_rows,
    validate_request_row,
    validate_result_row,
    wav_duration,
    write_result_rows,
)
from tts_ab.phrases import (
    CATEGORY_COUNTS,
    PHRASES,
    PHRASE_BY_ID,
    Phrase,
    validate_phrases,
)


def _write_wav(path, seconds, rate=8000, width=2, channels=1):
    """Синтетический WAV через stdlib wave во tmp_path; вернуть кадры-байты."""
    payload = b"\x00" * (int(seconds * rate) * width * channels)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return payload


# --- WAV: длительность из заголовка (frames/framerate) -------------------


def test_wav_duration_from_path(tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav, 1.5)
    assert wav_duration(wav) == pytest.approx(1.5)


def test_wav_duration_from_bytes(tmp_path):
    wav = tmp_path / "b.wav"
    _write_wav(wav, 0.75, rate=16000)
    assert wav_duration(wav.read_bytes()) == pytest.approx(0.75)


def test_wav_duration_zero_frames_and_corrupt(tmp_path):
    empty = tmp_path / "empty.wav"
    _write_wav(empty, 0)
    assert wav_duration(empty) == 0.0
    with pytest.raises(wave.Error):
        wav_duration(b"this is not a wav file")


# --- RTF: synth_ms / 1000 / audio_duration_s -----------------------------


def test_rtf_basic_and_zero_synth_ms():
    assert compute_rtf(500.0, 2.0) == (0.25, True)
    assert compute_rtf(0.0, 2.0) == (0.0, True)


def test_rtf_invalid_inputs():
    assert compute_rtf(500.0, 0.0) == (None, False)
    assert compute_rtf(500.0, None) == (None, False)
    assert compute_rtf(500.0, -1.0) == (None, False)
    assert compute_rtf(None, 2.0) == (None, False)
    assert compute_rtf(-5.0, 2.0) == (None, False)


# --- Набор фраз: 14 фраз, 5 категорий, 4/3/3/2/2 -------------------------


def test_phrases_dataset_shape():
    assert len(PHRASES) == 14
    ids = [p.phrase_id for p in PHRASES]
    assert ids == [
        "s1", "s2", "s3", "s4",
        "m1", "m2", "m3",
        "l1", "l2", "l3",
        "n1", "n2",
        "h1", "h2",
    ]
    assert set(CATEGORY_COUNTS) == {"s", "m", "l", "n", "h"}
    assert {p.category for p in PHRASES} == set(CATEGORY_COUNTS)
    # элемент — (id, category, text)
    assert PHRASES[0] == ("s1", "s", "Готово, держи!")


def test_phrases_homograph_marks():
    # выбор зафиксирован: комбинируемое ударение U+0301 после ударной
    # гласной; скобочные аннотации в текст НЕ включены (допущение 9)
    h1 = PHRASE_BY_ID["h1"].text
    h2 = PHRASE_BY_ID["h2"].text
    assert h1 == "Замо\u0301к на двери новый, а за\u0301мок древний."
    assert h2 == "Мука\u0301 для блинов просеяна, и эта му\u0301ка " \
                 "оказалась лучшей."
    assert "\u0301" in h1
    assert "\u0301" in h2
    assert "замок/замок" not in h1
    assert "мука/мука" not in h2


def test_validate_phrases_ok():
    assert validate_phrases(PHRASES) == PHRASES


def test_validate_phrases_duplicate_id():
    broken = [PHRASES[0]._replace(phrase_id="s2")] + list(PHRASES[1:])
    with pytest.raises(ValueError, match="duplicate id"):
        validate_phrases(broken)


def test_validate_phrases_empty_text():
    broken = list(PHRASES)
    broken[3] = broken[3]._replace(text="   ")
    with pytest.raises(ValueError, match="empty text"):
        validate_phrases(broken)


def test_validate_phrases_bad_counts():
    broken = list(PHRASES)
    broken.append(Phrase("s5", "s", "Ещё одна короткая реплика."))
    with pytest.raises(ValueError, match=r"category 's'"):
        validate_phrases(broken)


def test_validate_phrases_unknown_category():
    broken = list(PHRASES)
    broken[0] = broken[0]._replace(category="x")
    with pytest.raises(ValueError, match="unknown category"):
        validate_phrases(broken)


# --- Строки результатов: контракт, ok=false → error, слияние -------------


def _result_row(**overrides):
    row = {
        "engine": "piper",
        "voice": "denis",
        "phrase_id": "s1",
        "category": "s",
        "text": "Готово, держи!",
        "ok": True,
        "synth_ms": 123.4,
        "load_ms": 500.0,
        "audio_duration_s": 1.25,
        "sample_rate": 22050,
        "rtf": 0.099,
        "peak_rss_mb": 300.5,
        "error": None,
        "wav_path": "data/tts_ab/wav/piper/denis/s1.wav",
    }
    row.update(overrides)
    return row


def test_validate_result_row_ok_true():
    assert validate_result_row(_result_row()) is None
    minimal = {
        "engine": "kokoro",
        "voice": "dima",
        "phrase_id": "h1",
        "category": "h",
        "text": "Замо\u0301к на двери новый.",
        "ok": True,
    }
    assert validate_result_row(minimal) is None


def test_validate_result_row_missing_required():
    row = _result_row()
    del row["ok"]
    problem = validate_result_row(row)
    assert problem is not None
    assert "ok" in problem
    assert validate_result_row("not a dict") is not None


def test_validate_result_row_ok_false_requires_error():
    problem = validate_result_row(_result_row(ok=False))
    assert problem is not None
    assert "error" in problem
    problem = validate_result_row(_result_row(ok=False, error="   "))
    assert problem is not None
    failed = _result_row(
        ok=False,
        error="voice not found",
        synth_ms=None,
        rtf=None,
        audio_duration_s=None,
        wav_path=None,
    )
    assert validate_result_row(failed) is None


def test_merge_result_rows_repeats_dupes_invalid():
    good = _result_row()
    repeat = _result_row(synth_ms=130.0, rtf=0.104)  # -Repeats: валидна
    bad = {"engine": "piper", "phrase_id": "s2"}  # нет обязательных полей
    merged, problems = merge_result_rows([good, dict(good), repeat, bad])
    assert merged == [good, repeat]
    assert any("duplicate" in p for p in problems)
    assert any("missing required" in p for p in problems)


# --- results.json: NDJSON чтение/запись ----------------------------------


def test_read_write_result_rows_roundtrip_and_bad_lines(tmp_path):
    failed = _result_row(
        ok=False,
        error="boom",
        synth_ms=None,
        rtf=None,
        audio_duration_s=None,
        wav_path=None,
    )
    rows = [_result_row(), failed]
    path = tmp_path / "results.json"
    write_result_rows(path, rows)
    loaded, problems = read_result_rows(path)
    assert loaded == rows
    assert problems == []
    # битые строки: невалидный JSON + нарушение контракта
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
        handle.write('{"engine":"piper"}\n')
    loaded, problems = read_result_rows(path)
    assert loaded == rows
    assert len(problems) == 2


# --- Контракт строки запроса ---------------------------------------------


def test_validate_request_row():
    assert set(REQUEST_FIELDS) == {"phrase_id", "text", "voice"}
    assert validate_request_row(
        {"phrase_id": "s1", "text": "Готово, держи!", "voice": "denis"}
    ) is None
    problem = validate_request_row({"phrase_id": "s1", "text": "текст"})
    assert problem is not None
    assert "voice" in problem
    problem = validate_request_row(
        {"phrase_id": "s1", "text": 5, "voice": "denis"}
    )
    assert problem is not None
    assert validate_request_row("not a dict") is not None


# --- Слепые имена: план групп, KEY, форма (задача 5) ----------------------

_BLIND_ENGINES = ("kokoro", "piper", "silero", "supertonic")


def _blind_row(engine, voice, phrase_id):
    return {
        "engine": engine,
        "voice": voice,
        "phrase_id": phrase_id,
        "category": phrase_id[0],
        "text": "текст",
        "ok": True,
        "wav_path": f"data/tts_ab/wav/{engine}/{voice}/{phrase_id}.wav",
    }


def _blind_rows(phrase_ids=("s1", "m1", "l1", "n1", "h1")):
    rows = []
    for phrase_id in phrase_ids:
        for engine in _BLIND_ENGINES:
            male, female = ENGINE_VOICES[engine]
            rows.append(_blind_row(engine, male, phrase_id))
            rows.append(_blind_row(engine, female, phrase_id))
    return rows


def test_blind_filename_ascii_and_pattern():
    name = blind_filename(1, 2)
    assert name == "p01_s2.wav"
    assert BLIND_NAME_RE.fullmatch(name)
    assert name.isascii()
    for engine, pair in ENGINE_VOICES.items():
        assert engine not in name
        assert pair[0] not in name and pair[1] not in name
    with pytest.raises(ValueError):
        blind_filename(1, 0)  # s0 не входит в паттерн p<NN>_s<k>.wav


def test_blind_plan_groups_and_slots():
    assert gender_of("piper", "denis") == "m"
    assert gender_of("kokoro", "чужой") is None
    assert gender_of("нет-такого", "denis") is None
    plan = plan_blind_groups(_blind_rows(), seed=7)
    assert plan.seed == 7
    assert len(plan.groups) == 10  # 5 фраз × 2 гендерные группы
    assert [g.group_no for g in plan.groups] == list(range(1, 11))
    assert (plan.groups[0].phrase_id, plan.groups[0].gender) == ("s1", "m")
    assert plan.groups[1].phrase_id == "s1" and plan.groups[1].gender == "f"
    for group in plan.groups:
        assert len(group.slots) == 4
        assert sorted(s.slot for s in group.slots) == [1, 2, 3, 4]
        assert {s.engine for s in group.slots} == set(_BLIND_ENGINES)
        gender_index = 0 if group.gender == "m" else 1
        for slot in group.slots:
            assert slot.voice == ENGINE_VOICES[slot.engine][gender_index]
            name = blind_filename(group.group_no, slot.slot)
            assert name.isascii()
            for engine, pair in ENGINE_VOICES.items():
                assert engine not in name
                assert pair[0] not in name and pair[1] not in name


def test_blind_plan_not_degenerate():
    plan = plan_blind_groups(_blind_rows(), seed=42)
    positions: dict[str, set[int]] = {}
    for group in plan.groups:
        for slot in group.slots:
            positions.setdefault(slot.engine, set()).add(slot.slot)
    assert set(positions) == set(_BLIND_ENGINES)
    for engine, slots in positions.items():
        # перестановка не вырождена: ни один движок не сидит на одной
        # позиции во всех своих группах
        assert len(slots) >= 2, (engine, slots)


def test_blind_plan_seed_reproducible():
    rows = _blind_rows()
    plan_a = plan_blind_groups(rows, seed=123)
    plan_b = plan_blind_groups(rows, seed=123)
    assert plan_a.groups == plan_b.groups
    assert plan_a.seed == plan_b.seed == 123


def test_blind_plan_partial_engines():
    rows = [row for row in _blind_rows(("s1",))
            if row["engine"] in ("kokoro", "piper")]
    plan = plan_blind_groups(rows, seed=5)
    assert len(plan.groups) == 2  # s1 × (м, ж)
    for group in plan.groups:
        assert sorted(s.slot for s in group.slots) == [1, 2]
        assert {s.engine for s in group.slots} == {"kokoro", "piper"}
    assert all(len(g.slots) == 2 for g in plan.groups)


def test_blind_key_matches_plan():
    plan = plan_blind_groups(_blind_rows(), seed=99)
    key = render_key(plan)
    assert key.startswith("НЕ ОТКРЫВАТЬ ДО ПРОСЛУШКИ")
    assert "Seed перестановки: 99" in key
    for group in plan.groups:
        for slot in group.slots:
            line = (f"{blind_filename(group.group_no, slot.slot)}"
                    f" = {slot.engine} × {slot.voice}")
            assert line in key


def test_blind_form_subset_and_no_hints():
    assert recommended_subset(5) == ["s1", "m1", "l1", "n1", "h1"]
    assert recommended_subset(2) == ["s1", "m1"]
    plan = plan_blind_groups(_blind_rows(), seed=11)
    form = render_form(plan, recommended_subset(5))
    assert form.count("| файл | естественность 1–5 | комментарий |") == 10
    assert "Итог (ранжирование 1–4" in form
    assert "phrase_id,file,score,comment" in form
    for phrase_id in ("s1", "m1", "l1", "n1", "h1"):
        assert f"## {phrase_id}" in form
        assert f"{phrase_id},p" in form  # готовые CSV-строки подсета
    for engine, pair in ENGINE_VOICES.items():
        assert engine not in form  # анти-подсказка: имён движков нет
        assert pair[0] not in form and pair[1] not in form
