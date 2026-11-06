# -*- coding: utf-8 -*-
"""Характеризационные тесты tts.py: санация и нормализация текста для озвучки.

Фиксируют ПОВЕДЕНИЕ до рефакторинга (выноса в speech_text.py): при распиле
тесты должны пройти без правок (только смена импорта)."""
from __future__ import annotations

import tts


# ---------------------------------------------------------------------------
# clean_for_speech: markdown/эмодзи, которые TTS читает вслух как мусор
# ---------------------------------------------------------------------------
class TestCleanForSpeech:
    def test_bold_removed(self):
        # «**дата**» piper читал как «звезда звезда дата звезда звезда»
        assert tts.clean_for_speech("**дата**") == "дата"

    def test_links_keep_text(self):
        assert tts.clean_for_speech("[текст](http://example.com)") == "текст"

    def test_image_alt_kept(self):
        assert tts.clean_for_speech("![описание](img.png)") == "описание"

    def test_code_fence_removed(self):
        assert tts.clean_for_speech("до ```code``` после") == "до после"

    def test_heading_prefix_removed(self):
        assert tts.clean_for_speech("# Заголовок") == "Заголовок"

    def test_quote_prefix_removed(self):
        assert tts.clean_for_speech("> цитата") == "цитата"

    def test_emoji_removed(self):
        assert tts.clean_for_speech("привет 🎉 и звезда ★") == "привет и звезда"

    def test_whitespace_collapsed(self):
        assert tts.clean_for_speech("  много   пробелов  ") == "много пробелов"

    def test_empty_input(self):
        assert tts.clean_for_speech("") == ""
        assert tts.clean_for_speech(None) == ""


# ---------------------------------------------------------------------------
# normalize_for_speech: сокращения, валюта, даты, единицы
# ---------------------------------------------------------------------------
class TestNormalizeForSpeech:
    def test_abbreviations(self):
        assert "потому что" in tts.normalize_for_speech("потому т.к. да")
        assert "то есть" in tts.normalize_for_speech("это т.е. норм")
        assert "и так далее" in tts.normalize_for_speech("и т.д. и т.п.")

    def test_percent_pluralization(self):
        # 1 процент / 2 процента / 5 процентов / 12 процентов
        assert "1 процент" in tts.normalize_for_speech("вырос на 1%")
        assert "2 процента" in tts.normalize_for_speech("вырос на 2%")
        assert "5 процентов" in tts.normalize_for_speech("вырос на 5%")
        assert "12 процентов" in tts.normalize_for_speech("вырос на 12%")
        assert "21 процент" in tts.normalize_for_speech("вырос на 21%")

    def test_currency(self):
        assert "5 рублей" in tts.normalize_for_speech("5 ₽")
        assert "10 долларов" in tts.normalize_for_speech("10 $")

    def test_date_expansion(self):
        out = tts.normalize_for_speech("30.10.2026")
        assert "30 октября 2026 года" in out

    def test_time_expansion(self):
        out = tts.normalize_for_speech("в 17:32")
        assert "17 часов 32 минуты" in out
        out2 = tts.normalize_for_speech("в 18:00")
        assert "18 часов" in out2

    def test_range(self):
        assert "от 5 до 10" in tts.normalize_for_speech("от 5-10 дней")

    def test_url_neutralized(self):
        out = tts.normalize_for_speech("смотри https://example.com/path")
        assert "https" not in out
        assert "ссылка" in out

    def test_units_pluralization(self):
        assert "5 километров" in tts.normalize_for_speech("5 км")
        assert "1 километр" in tts.normalize_for_speech("1 км")
        assert "2 километра" in tts.normalize_for_speech("2 км")

    def test_km_per_hour_order(self):
        # «км/ч» должен обрабатываться ДО «км» (иначе «60 километров/ч»)
        out = tts.normalize_for_speech("60 км/ч")
        assert "километров/ч" not in out
        assert "в час" in out

    def test_temperature(self):
        assert "20 градусов" in tts.normalize_for_speech("20°C")

    def test_plain_numbers_untouched(self):
        # простые числа НЕ трогаем — espeak читает их правильно
        assert tts.normalize_for_speech("у меня 5 яблок") == "у меня 5 яблок"


# ---------------------------------------------------------------------------
# Связка: clean -> normalize (порядок, как в synthesize)
# ---------------------------------------------------------------------------
class TestPipeline:
    def test_markdown_percent_combined(self):
        out = tts.normalize_for_speech(tts.clean_for_speech("**рост 5%**"))
        assert "5 процентов" in out
        assert "*" not in out

    def test_pure_markdown_not_empty_after_clean(self):
        # фикс 30.10: если clean убрал ВСЁ — synthesize должен взять исходник,
        # но сам clean для «**x**» возвращает текст, а не пусто
        assert tts.clean_for_speech("**x**") == "x"
