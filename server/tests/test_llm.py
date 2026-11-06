# -*- coding: utf-8 -*-
"""Характеризационные тесты llm.py: стриминговый парсер, извлечение JSON,
быстрые шаблоны. Фиксируют ПОВЕДЕНИЕ до рефакторинга — при распиле модуля
тесты должны пройти без правок (только смена импорта)."""
from __future__ import annotations

import pytest

import llm


# ---------------------------------------------------------------------------
# _StreamParser: вырезание предложений из частичного потока reply
# ---------------------------------------------------------------------------
class TestStreamParser:
    def test_full_reply_one_shot(self):
        p = llm._StreamParser()
        out = p.feed('{"reply": "Привет! Как дела?", "action": "none", "args": {}}')
        assert out == ["Привет!", "Как дела?"]
        assert p.close() is None  # всё уже отдано

    def test_incremental_by_tokens(self):
        p = llm._StreamParser()
        got = []
        for ch in '{"reply": "Ищу погоду в Москве. Завтра будет тепло.", "action": "search"}':
            got.extend(p.feed(ch))
        assert got == ["Ищу погоду в Москве.", "Завтра будет тепло."]

    def test_tail_without_punctuation(self):
        p = llm._StreamParser()
        out = p.feed('{"reply": "Короткий ответ')
        assert out == []
        tail = p.close()
        assert tail == "Короткий ответ"

    def test_escaped_quotes_inside_reply(self):
        p = llm._StreamParser()
        out = p.feed('{"reply": "Он сказал \\"привет\\". Потом ушёл.", "a": 1}')
        assert out == ['Он сказал "привет".', "Потом ушёл."]

    def test_partial_escape_at_chunk_boundary(self):
        # дельта обрывается на экранированной кавычке — не должно падать
        p = llm._StreamParser()
        p.feed('{"reply": "Слово \\"')
        p.feed('ещё и хвост."}')
        assert p.close() is None

    def test_on_query_fires_once(self):
        queries = []
        p = llm._StreamParser(on_query=queries.append)
        p.feed('{"reply": "Ищу.", "action": "search", "args": {"query": ' + '"погода Москва"')
        assert queries == ["погода Москва"]
        # повторно не вызывается даже при ещё одном query
        p.feed('{"query": "другое"}')
        assert queries == ["погода Москва"]

    def test_reply_after_close_keeps_watching_query(self):
        queries = []
        p = llm._StreamParser(on_query=queries.append)
        p.feed('{"reply": "Готово.", "action": "search", "args": {"query": "новости"}}')
        assert queries == ["новости"]
        assert p.reply_text == "Готово."

    def test_spoken_accumulates(self):
        p = llm._StreamParser()
        p.feed('{"reply": "Раз. Два.", "x": 0}')
        assert p.spoken == "Раз. Два."


# ---------------------------------------------------------------------------
# extract_json: первый сбалансированный объект
# ---------------------------------------------------------------------------
class TestExtractJson:
    def test_plain(self):
        assert llm.extract_json('{"reply": "ok", "action": "none"}') == {
            "reply": "ok", "action": "none"}

    def test_fenced(self):
        raw = 'Вот ответ:\n```json\n{"reply": "ok", "action": "none"}\n```\n'
        assert llm.extract_json(raw)["reply"] == "ok"

    def test_nested_and_string_with_braces(self):
        raw = '{"reply": "скобки {внутри} строки", "args": {"q": "}"}, "action": "none"}'
        obj = llm.extract_json(raw)
        assert obj["reply"] == "скобки {внутри} строки"
        assert obj["args"]["q"] == "}"

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            llm.extract_json("просто текст без скобок")

    def test_unclosed_raises(self):
        with pytest.raises(ValueError):
            llm.extract_json('{"reply": "обрыв')


# ---------------------------------------------------------------------------
# fast_reply: шаблоны без LLM
# ---------------------------------------------------------------------------
class TestFastReply:
    def test_time_question(self):
        assert llm.fast_reply("который час") is not None
        assert llm.fast_reply("сколько времени") is not None

    def test_wake_word_stripped(self):
        # «Гемма, который час» — после снятия wake word совпадает с шаблоном
        assert llm.fast_reply("Гемма, который час") is not None

    def test_non_template_goes_to_llm(self):
        assert llm.fast_reply("расскажи что-нибудь интересное") is None
        assert llm.fast_reply("сколько времени до поезда") is None  # не fullmatch

    def test_long_text_not_matched(self):
        assert llm.fast_reply("который час " * 10) is None


# ---------------------------------------------------------------------------
# _decide_with: фолбэки разбора ответа модели
# ---------------------------------------------------------------------------
def _mk_make(raw_text: str, calls: list | None = None):
    """make_chat, возвращающий фиксированный сырой ответ."""
    def make(attempt: int):
        def chat(messages):
            if calls is not None:
                calls.append(attempt)
            return raw_text
        return chat
    return make


class TestDecideWith:
    def test_valid_json(self):
        d = llm._decide_with([], _mk_make('{"reply": "Привет", "action": "none"}'))
        assert d.reply == "Привет"
        assert d.action == "none"

    def test_prose_without_json_taken_as_reply(self):
        # фолбэк 29.10: осмысленный текст без JSON = готовый ответ, без повтора
        calls = []
        d = llm._decide_with([], _mk_make("У меня всё, а у тебя? Что нового?", calls))
        assert d.reply == "У меня всё, а у тебя? Что нового?"
        assert d.action == "none"
        assert calls == [0]  # повтор НЕ делался

    def test_search_digest_fallback(self):
        d = llm._decide_with(
            [], _mk_make("(поиск в интернете: погода Москва)"))
        assert d.action == "search"
        assert d.args["query"] == "погода Москва"

    def test_llm_error_returns_polite_message(self):
        def make(attempt):
            def chat(messages):
                raise llm.LLMError("LLM недоступна: connection refused")
            return chat
        d = llm._decide_with([], make)
        assert d.action == "none"
        assert "не отвечает" in d.reply

    def test_meta_json_answer_retried_then_fallback(self):
        # мета-ответ про формат: первая попытка отвергается, повтор с напоминанием
        calls = []
        raws = iter(['{"reply": "Буду отвечать JSON-объектом", "action": "none"}',
                     "Я не совсем понял, повтори."])
        def make(attempt):
            def chat(messages):
                calls.append(attempt)
                return next(raws)
            return chat
        d = llm._decide_with([], make)
        assert calls == [0, 1]  # был повтор
        assert d.action == "none"

    def test_missing_search_query_rejected(self):
        d = llm._decide_with(
            [], _mk_make('{"reply": "Ищу", "action": "search", "args": {}}'))
        assert d.action == "none"  # query пуст -> уточняющий вопрос


# ---------------------------------------------------------------------------
# decide() end-to-end: фабрика make_chat обязана принимать attempt
# ---------------------------------------------------------------------------
def test_decide_end_to_end(monkeypatch):
    # Регрессия 06.11: decide() передавал в _decide_with ламбу БЕЗ аргументов
    # (lambda: lambda msgs: ...), а _decide_with вызывает make_chat(attempt).
    # TypeError не является LLMError -> не ловился в _decide_with, decide()
    # падал целиком -> answer.py отдавал «Сервер модели не отвечает» на
    # /voice и /ask, хотя llama-server был жив (лог server.log 10:36/10:37).
    import importlib
    mod = importlib.import_module("llm.decide")
    seen: list = []

    def fake_chat(msgs, max_tokens=None):
        seen.append(msgs)
        return '{"reply": "Привет", "action": "none"}'

    monkeypatch.setattr(mod, "chat", fake_chat)
    d = llm.decide("привет")
    assert d.reply == "Привет"
    assert d.action == "none"
    # запрос реально ушёл в chat с репликой пользователя последней
    assert seen and seen[0][-1] == {"role": "user", "content": "привет"}
