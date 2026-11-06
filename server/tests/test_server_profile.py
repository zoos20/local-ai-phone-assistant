# -*- coding: utf-8 -*-
"""Характеризационные тесты server.py: определение команд профиля.

Фиксируют поведение detect_profile_command/_fuzzy_profile до рефакторинга
(выноса в отдельный модуль): при распиле тесты должны пройти без правок
(только смена импорта). Кейсы -- из живых тестов 29.10-31.10 (STT коверкает
имена, триггеры ловили ложные срабатывания)."""
from __future__ import annotations

from server import _fuzzy_profile, detect_profile_command


class TestGetTrigger:
    """Вопрос о текущем профиле -- «get», даже если внутри есть имя."""

    def test_which_profile(self):
        assert detect_profile_command("какой профиль") == "get"

    def test_which_profile_now(self):
        # 29.10: «Какой сейчас профиль?» уходил в list
        assert detect_profile_command("какой сейчас профиль") == "get"

    def test_what_profile(self):
        assert detect_profile_command("что за профиль у меня") == "get"

    def test_profile_now(self):
        # фикс: regex «профил(?:ь)?\s+сейчас» — раньше «профиль сейчас»
        # не матчился (пропуск «ь») и уходил в list вопреки комментарию
        assert detect_profile_command("профиль сейчас") == "get"

    def test_profile_now_bare_stem(self):
        assert detect_profile_command("профил сейчас") == "get"

    def test_name_me(self):
        assert detect_profile_command("как меня зовут профиль") == "get"

    def test_get_checked_before_set(self):
        # «какой профиль яндера» -- вопрос, а не переключение
        assert detect_profile_command("какой профиль яндера") == "get"


class TestSetTrigger:
    """Имя профиля + триггер -> set:<pid>."""

    def test_strong_trigger_with_name(self):
        assert detect_profile_command("смени профиль на цундере") == "set:tsundere"

    def test_weak_trigger_with_name(self):
        assert detect_profile_command("стань яндере") == "set:yandere"

    def test_stem_tsund(self):
        # стем «цунд» ловит склонения: «Профиль Цундера»
        assert detect_profile_command("профиль цундера") == "set:tsundere"

    def test_stt_typos_uncensored(self):
        # живой тест 31.10: STT пишет «нетнзурный»/«нецнзурный»
        # («включи» не триггер — команда идёт со словом «профиль»/«смени»)
        assert detect_profile_command("смени нетнзурный") == "set:uncensored"
        assert detect_profile_command("профиль нецнзурный") == "set:uncensored"

    def test_censored_is_default_fallback(self):
        # «цензур» входит в оба (uncensored проверяется первым)
        assert detect_profile_command("профиль цензурный") == "set:censored"

    def test_kuudere(self):
        assert detect_profile_command("будь куудере") == "set:kuudere"

    def test_deredere_stem(self):
        assert detect_profile_command("сделайся дередере") == "set:deredere"


class TestListTrigger:
    """Сильный триггер без имени -> список."""

    def test_bare_profile_word(self):
        assert detect_profile_command("профиль") == "list"

    def test_bare_mode_word(self):
        assert detect_profile_command("режим") == "list"

    def test_plural_stem(self):
        # 31.10: «Профили ДРДР» — сильный триггер ловит мн. число,
        # fuzzy переводит «дрдр» в deredere (задуманный результат, см. код)
        assert detect_profile_command("профили дрдр") == "set:deredere"


class TestNotAProfileCommand:
    """Обычные вопросы -- не команда профиля (уходят в LLM)."""

    def test_greeting(self):
        assert detect_profile_command("привет как дела") is None

    def test_nichto_byud(self):
        # 29.10: «будь» внутри «что-нибудь» не должен срабатывать (\b)
        assert detect_profile_command("расскажи что-нибудь интересное") is None

    def test_polite_phrase_short(self):
        # «будь добр» -- слабый триггер без имени -> не list, не set
        assert detect_profile_command("будь добр") is None

    def test_polite_phrase_with_verb_false_positive(self):
        # Регрессия к фиксу (фаза 4.6): fuzzy ловил глагол «передай»
        # (0.667 >= 0.60) -> вежливая фраза переключала профиль на
        # deredere. Стоп-лист _FUZZY_STOPWORDS отсекает такие кандидаты.
        assert detect_profile_command("будь добр и передай привет") is None

    def test_hand_over_greeting_not_a_profile_command(self):
        # Регрессия к тому же фиксу: передача привета — не команда профиля
        assert detect_profile_command("передай привет маме") is None

    def test_channel_change(self):
        assert detect_profile_command("смени канал на первый") is None

    def test_time_question(self):
        assert detect_profile_command("который час") is None


class TestFuzzyProfile:
    """Нечёткое сопоставление вариантов STT (порог 0.60)."""

    def test_dery_dera(self):
        # живой тест 31.10: «Дэри Дэра» -> deredere, а не yandere
        # (длинная пара «дерадера» приоритетнее короткой «дера»)
        assert _fuzzy_profile("Дэри Дэра") == "deredere"

    def test_dera_dera(self):
        assert _fuzzy_profile("Профиль Дэра Дэра") == "deredere"

    def test_sundeyra(self):
        assert _fuzzy_profile("сундейра") == "tsundere"

    def test_netcenzurny(self):
        assert _fuzzy_profile("нетнзурный") == "uncensored"

    def test_no_match_returns_none(self):
        # raw fuzzy без триггера -- кейс с реально низким сходством
        assert _fuzzy_profile("да ладно") is None

    def test_fuzzy_bare_can_false_positive(self):
        # ИЗВЕСТНОЕ поведение (не контракт detect): raw «привет как дела»
        # содержит слово «дела» (4 символа) со сходством 0.60 до «яндера».
        # В detect это безопасно -- fuzzy вызывается только при триггере.
        assert _fuzzy_profile("привет как дела") == "yandere"

    def test_short_words_ignored(self):
        # слова < 4 символов не являются кандидатами
        assert _fuzzy_profile("да ладно") is None

    def test_yo_normalization(self):
        # «ё» -> «е» перед сравнением
        assert _fuzzy_profile("яндера") == "yandere"
