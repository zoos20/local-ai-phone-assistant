"""Фича «Silero -> прод-TTS», задача 3: голоса профилей по движку TTS.

llm/profiles.py: каждый профиль получил поле voice_silero (прод-голос
silero v5_6_ru, 48 кГц) рядом с voice (kokoro-голос -- ветка-страховка).
Резолв по settings.tts_engine в _resolved_voice(): silero ->
voice_silero, иначе -- kokoro-голос из voice (ветки piper/unknown живут
по старым полям). Пол/род НЕ меняются: kuudere остаётся мужским на
обоих движках (kokoro dima / silero aidar -- оба мужские).

Тесты гоняют чистые геттеры через monkeypatch (state.profile и
settings.tts_engine) -- active_profile.txt на диске не трогается.
"""
from __future__ import annotations

import llm
from app_state import state
from config import settings
from llm.profiles import _VOICE_GENDER

# таблица «профиль -> прод-голос silero v5_6_ru» (llm/profiles.py)
SILERO = {
    "censored": "eugene",
    "uncensored": "aidar",
    "kuudere": "aidar",
    "tsundere": "xenia",
    "yandere": "kseniya",
    "deredere": "baya",
}
# таблица «профиль -> kokoro-голос» (прежний контракт, поле voice)
KOKORO = {
    "censored": "dima",
    "uncensored": "dima",
    "kuudere": "dima",
    "tsundere": "sveta",
    "yandere": "masha",
    "deredere": "masha",
}


def test_mapping_all_six(monkeypatch):
    """silero-движок -> voice_silero каждого из 6 профилей (ровно таблица)."""
    monkeypatch.setattr(settings, "tts_engine", "silero")
    assert set(SILERO) == set(llm.PROFILES)  # таблица покрывает ровно 6
    for pid, expected in SILERO.items():
        monkeypatch.setattr(state, "profile", pid)
        assert llm.get_profile_voice() == expected, pid


def test_kokoro_voices_unchanged(monkeypatch):
    """kokoro-движок -> прежние dima/sveta/masha (контракт не поехал)."""
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    for pid, expected in KOKORO.items():
        monkeypatch.setattr(state, "profile", pid)
        assert llm.get_profile_voice() == expected, pid


def test_engine_switch(monkeypatch):
    """Переключение движка на одном профиле меняет голос dima <-> eugene."""
    monkeypatch.setattr(state, "profile", "censored")
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    assert llm.get_profile_voice() == "dima"
    monkeypatch.setattr(settings, "tts_engine", "silero")
    assert llm.get_profile_voice() == "eugene"
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    assert llm.get_profile_voice() == "dima"


def test_gender_table_silero():
    """Пол silero-голосов: eugene/aidar -- муж, xenia/baya/kseniya -- жен."""
    assert _VOICE_GENDER["eugene"] == "мужской"
    assert _VOICE_GENDER["aidar"] == "мужской"
    assert _VOICE_GENDER["xenia"] == "женский"
    assert _VOICE_GENDER["baya"] == "женский"
    assert _VOICE_GENDER["kseniya"] == "женский"


def test_kuudere_male_on_both_engines(monkeypatch):
    """Род не поехал: kuudere -- мужской и на silero (aidar), и на kokoro (dima)."""
    monkeypatch.setattr(state, "profile", "kuudere")
    monkeypatch.setattr(settings, "tts_engine", "silero")
    block = llm.personality_block()
    assert "aidar" in block
    assert "мужской" in block
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    block = llm.personality_block()
    assert "dima" in block
    assert "мужской" in block


def test_personality_block_uses_resolved_voice(monkeypatch):
    """gender-блок берёт голос из РАЗРЕШЁННОГО по движку поля."""
    monkeypatch.setattr(state, "profile", "censored")
    monkeypatch.setattr(settings, "tts_engine", "silero")
    assert "eugene" in llm.personality_block()
    monkeypatch.setattr(settings, "tts_engine", "kokoro")
    assert "dima" in llm.personality_block()


def test_unknown_engine_falls_back_to_kokoro_voice(monkeypatch):
    """Неизвестный движок (piper) живёт по старым полям -- kokoro-голос."""
    monkeypatch.setattr(settings, "tts_engine", "piper")
    for pid, expected in KOKORO.items():
        monkeypatch.setattr(state, "profile", pid)
        assert llm.get_profile_voice() == expected, pid


def test_list_profiles_untouched():
    """UI-контракт не менялся: [{id, label}, ...] без voice."""
    assert llm.list_profiles() == [
        {"id": "censored", "label": "Цензурный"},
        {"id": "uncensored", "label": "Нецензурный"},
        {"id": "tsundere", "label": "Цундере"},
        {"id": "yandere", "label": "Яндере"},
        {"id": "kuudere", "label": "Куудере"},
        {"id": "deredere", "label": "Дередере"},
    ]
    assert all(set(p) == {"id", "label"} for p in llm.list_profiles())
