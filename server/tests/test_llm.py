# -*- coding: utf-8 -*-
"""Характеризационные тесты llm.py: стриминговый парсер, извлечение JSON,
быстрые шаблоны. Фиксируют ПОВЕДЕНИЕ до рефакторинга — при распиле модуля
тесты должны пройти без правок (только смена импорта)."""
from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

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


class TestStreamParserAction:
    """З4 (вердикт A З3, 25/25 action-first): факт action извлекается из
    стрима -- атрибут action заполняется, как только значение дописано;
    работает в ОБОИХ порядках ключей."""

    def test_action_first_known_before_first_sentence(self):
        events = []
        p = llm._StreamParser(on_action=events.append)
        out = p.feed('{"action": "search", "args": {"query": "по')
        assert out == []              # reply ещё не открыт
        assert p.action == "search"   # факт уже известен ДО reply
        assert events == ["search"]   # колбэк уже стрельнул
        out = p.feed('года Москва"}, "reply": "Ищу погоду. Идёт."}')
        assert out == ["Ищу погоду.", "Идёт."]  # сентенции reply ПОСЛЕ факта
        assert p.reply_text == "Ищу погоду. Идёт."

    def test_action_callback_fires_once(self):
        events = []
        p = llm._StreamParser(on_action=events.append)
        p.feed('{"action": "none", "args": {}, "reply": "Привет!"}')
        p.feed('{"action": "search"}')  # повторный ключ в сыром потоке
        assert events == ["none"]       # ровно один раз
        assert p.action == "none"

    def test_old_reply_first_order_still_recognised(self):
        # обратная совместимость: старый порядок reply -> action
        events = []
        p = llm._StreamParser(on_action=events.append)
        out = p.feed(
            '{"reply": "Привет! Как дела?", "action": "none", "args": {}}')
        assert out == ["Привет!", "Как дела?"]
        assert p.action == "none"
        assert events == ["none"]

    def test_action_after_reply_closed_watched(self):
        # старый порядок: action приходит ПОСЛЕ закрытия reply --
        # парсер продолжает следить (ветка _reply_closed)
        p = llm._StreamParser()
        assert p.feed('{"reply": "Готово.", ') == ["Готово."]
        assert p.action is None
        assert p.feed('"action": "search", "args": {}}') == []
        assert p.action == "search"

    def test_action_attribute_without_callback(self):
        p = llm._StreamParser()
        assert p.feed('{"action": "call", "args": {}, "reply": "Звоню."}') \
            == ["Звоню."]
        assert p.action == "call"  # атрибут заполняется и без колбэка

    def test_partial_action_value_not_reported(self):
        # значение ещё не дописано (нет закрывающей кавычки) -- тишина
        events = []
        p = llm._StreamParser(on_action=events.append)
        p.feed('{"action": "se')
        assert p.action is None
        assert events == []
        p.feed('arch", "args": {}}')
        assert p.action == "search"
        assert events == ["search"]

    def test_action_callback_exception_does_not_break_stream(self):
        # гейт не должен ломать стрим (как on_query)
        def boom(_action):
            raise RuntimeError("gate down")

        p = llm._StreamParser(on_action=boom)
        out = p.feed(
            '{"action": "none", "args": {}, "reply": "Привет! Пока."}')
        assert out == ["Привет!", "Пока."]  # стрим выжил
        assert p.action == "none"


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

    def test_weekday_stt_variant_without_and(self):
        # Д6: STT-вариант «какой день неделя» (без «и») -- тоже
        # fast-ответ, а не LLM (M1 3711-3853 против ~2800 у fast,
        # data/voice_latency/baseline.md). «день недели» и «день»
        # уже матчились; «день рождения» и т.п. НЕ расширяются --
        # только кварцевый вопрос дня недели.
        assert llm.fast_reply("какой день неделя") is not None
        assert llm.fast_reply("какой день недели") is not None
        assert llm.fast_reply("какой день") is not None
        assert llm.fast_reply("Гемма, какой день неделя") is not None
        assert llm.fast_reply("какой день рождения") is None
        assert llm.fast_reply("какой день недели сегодня") is None


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


# ---------------------------------------------------------------------------
# З4 (вердикт A З3): on_action -- факт action для гейта озвучки (З5).
# action-first стрим: колбэк стреляет ДО первой сентенции reply, ровно
# один раз; старый порядок тоже распознаётся; вторая попытка молчит.
# ---------------------------------------------------------------------------
class TestDecideStreamOnAction:
    @staticmethod
    def _patch_stream(monkeypatch, raw, chunk=7):
        """Подмена chat_stream: кормит raw по кусочкам (эмуляция стрима)."""
        import importlib
        # importlib, а не `import llm.decide`: фасад llm реэкспортирует
        # функцию decide -- getattr(llm, "decide") даёт её, а не подмодуль
        decide_mod = importlib.import_module("llm.decide")

        def fake_chat_stream(msgs, on_token=None):
            for i in range(0, len(raw), chunk):
                on_token(raw[i:i + chunk])
            return raw

        monkeypatch.setattr(decide_mod, "chat_stream", fake_chat_stream)

    def test_action_known_before_first_sentence(self, monkeypatch):
        self._patch_stream(
            monkeypatch,
            '{"action": "search", "args": {"query": "погода Москва"}, '
            '"reply": "Ищу погоду. Идёт."}')
        events: list[tuple[str, str]] = []
        d = llm.decide_stream(
            "погода", on_sentence=lambda s: events.append(("s", s)),
            on_action=lambda a: events.append(("a", a)))
        assert d.action == "search"
        assert events[0] == ("a", "search")   # факт раньше первой сентенции
        assert events[1] == ("s", "Ищу погоду.")
        assert [k for k, _ in events].count("a") == 1  # ровно один раз

    def test_old_reply_first_order_still_fires(self, monkeypatch):
        self._patch_stream(
            monkeypatch,
            '{"reply": "Привет! Как дела?", "action": "none", "args": {}}')
        events: list[tuple[str, str]] = []
        d = llm.decide_stream(
            "привет", on_sentence=lambda s: events.append(("s", s)),
            on_action=lambda a: events.append(("a", a)))
        assert d.action == "none"
        assert events[0] == ("s", "Привет!")   # старый порядок: reply первым
        assert events[-1] == ("a", "none")     # action-факт приходит позже

    def test_second_attempt_is_silent(self, monkeypatch):
        # первая попытка -- невалидный JSON (пустой reply) -> повтор с
        # напоминанием; on_action стреляет ТОЛЬКО на первой попытке
        # (гейт З5 видит факт action только от первого стрима), вторая
        # идёт блокирующим chat() молча. Реплику первой попытки берём
        # ПУСТОЙ: любой непустой reply стрим-путь отдаст хвостом через
        # on_sentence (is_committed) -- повтор тогда не случится вовсе.
        import importlib
        decide_mod = importlib.import_module("llm.decide")

        def fake_chat_stream(msgs, on_token=None):
            raw = '{"reply": "", "action": "none", "args": {}}'
            on_token(raw)
            return raw

        def fake_chat(msgs, max_tokens=None):
            return ('{"reply": "Повтор", "action": "search", '
                    '"args": {"query": "погода"}}')

        monkeypatch.setattr(decide_mod, "chat_stream", fake_chat_stream)
        monkeypatch.setattr(decide_mod, "chat", fake_chat)
        actions: list[str] = []
        sentences: list[str] = []
        d = llm.decide_stream("привет", on_sentence=sentences.append,
                              on_action=actions.append)
        assert actions == ["none"]           # вторая попытка молчит
        assert sentences == []               # озвученного не было
        assert d.action == "search"          # решение со второй попытки

    def test_on_action_optional(self, monkeypatch):
        self._patch_stream(
            monkeypatch,
            '{"action": "none", "args": {}, "reply": "Привет!"}')
        sentences: list[str] = []
        d = llm.decide_stream("привет", on_sentence=sentences.append)
        assert d.action == "none"            # kwarg опционален
        assert sentences == ["Привет!"]

    def test_on_action_without_on_sentence(self, monkeypatch):
        # on_action -- самостоятельный потребитель факта: стрим-путь
        # включается ради него и без on_sentence
        self._patch_stream(
            monkeypatch,
            '{"action": "search", "args": {"query": "погода"}, '
            '"reply": "Ищу."}')
        actions: list[str] = []
        d = llm.decide_stream("погода", on_action=actions.append)
        assert actions == ["search"]
        assert d.action == "search"


# ---------------------------------------------------------------------------
# Списки покупок (фича, задача 4): SYSTEM_PROMPT, Decision, валидация args
# ---------------------------------------------------------------------------
_SHOPPING_ACTIONS = (
    "shopping_create",
    "shopping_add",
    "shopping_read",
    "shopping_update",
    "shopping_check",
    "shopping_delete",
    "shopping_lists",
)


def _raw_decision(action: str, args: dict) -> str:
    """Сырой ответ модели заданного вида (для _mk_make)."""
    return json.dumps({"reply": "Готово", "action": action, "args": args},
                      ensure_ascii=False)


class TestShoppingPrompt:
    """Статический SYSTEM_PROMPT описывает 7 shopping-action + примеры
    (задачи 4/20/22)."""

    def test_all_shopping_actions_documented(self):
        for action in _SHOPPING_ACTIONS:
            assert action in llm.SYSTEM_PROMPT

    def test_six_or_more_shopping_examples(self):
        # З4: примеры и схема в порядке action-first (строка начинается
        # с {"action"); схемная строка считается примером, как и раньше
        lines = llm.SYSTEM_PROMPT.splitlines()
        examples = [ln for ln in lines if ln.startswith('{"action"')]
        shopping = [ln for ln in examples if '"action": "shopping_' in ln]
        assert len(shopping) >= 6  # >=6 примеров реплика -> JSON
        assert len(examples) >= 9  # старые 3 + >=6 новых (+ строка схемы)

    def test_examples_cover_required_utterances(self):
        p = llm.SYSTEM_PROMPT
        for phrase in (
            "Создай список покупок",    # создание сразу с позициями
            "Добавь в список",          # «добавь…»
            "по списку покупок",        # «что у меня по списку покупок?»
            "Поменяй молоко на кефир",  # изменение позиции
            "Вычеркни хлеб",            # вычёркивание
            "В списке на неделю",       # реплика с именем списка
            "не сказано, что именно",   # уточняющий вопрос-кейс
        ):
            assert phrase in p, phrase

    def test_delete_has_example(self):
        # QA: shopping_delete — деструктивный, обязан иметь пример
        lines = [ln for ln in llm.SYSTEM_PROMPT.splitlines()
                 if ln.startswith('{"action"')]
        assert any('"action": "shopping_delete"' in ln for ln in lines), \
            "нет примера shopping_delete"

    def test_update_op_values_documented(self):
        # QA: значения op берём из shopping_store.update_items:
        # "update" (по умолчанию) | "delete"
        p = llm.SYSTEM_PROMPT
        assert "«update»" in p
        assert "«delete»" in p


class TestSystemPromptStatic:
    """Контракт keep-alive (§10.5): system_prompt() статична, без даты/времени."""

    def test_no_date_or_time_in_prompt(self):
        p = llm.system_prompt()
        assert "2026" not in p
        assert re.search(r"\b\d{2}:\d{2}\b", p) is None
        assert re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", p) is None

    def test_keepalive_contract(self):
        # keep-alive обязан нести ТОТ ЖЕ промпт, что и реальные запросы
        assert llm.system_prompt() == (
            llm.SYSTEM_PROMPT + "\n\n" + llm.personality_block())

    def test_repeated_calls_identical(self):
        assert llm.system_prompt() == llm.system_prompt()


class TestDecisionShoppingActions:
    """Decision принимает новые action, неизвестные — отвергает."""

    def test_decision_accepts_shopping_actions(self):
        for action in _SHOPPING_ACTIONS:
            d = llm.Decision(reply="ok", action=action, args={})
            assert d.action == action

    def test_decision_rejects_unknown_action(self):
        with pytest.raises(ValidationError):
            llm.Decision(reply="ok", action="shopping_erase", args={})


class TestDecideWithShoppingValidation:
    """Битые shopping-args -> action=none + уточняющий вопрос (данные не трогаем)."""

    def test_valid_create_passes(self):
        raw = _raw_decision(
            "shopping_create",
            {"items": [{"text": "молоко", "qty": 2},
                       {"text": "хлеб", "qty": "пара"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "shopping_create"

    def test_create_user_name_without_items_passes(self):
        # задача 22 / факт 1: «Создай список покупок на неделю» (с именем,
        # без товаров) -> shopping_create с args.name, БЕЗ items
        # (create-релакс задачи 20 + конвенция имени из слов пользователя)
        raw = _raw_decision("shopping_create",
                            {"name": "Список покупок на неделю"})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "shopping_create"
        assert d.args["name"] == "Список покупок на неделю"

    def test_add_with_and_without_spoken_list_passes(self):
        # задачи 22 (3)/(4): args.list допустим ТОЛЬКО если имя сказано
        # («добавь скотч в список покупок»); без сказанного имени add идёт
        # в активный список вообще без list -- контракты 3/4, не дубликаты
        for args in ({}, {"list": "Список покупок"}):
            d = llm._decide_with([], _mk_make(_raw_decision(
                "shopping_add", {"items": [{"text": "скотч"}], **args})))
            assert d.action == "shopping_add", args

    def test_valid_read_with_and_without_list(self):
        for args in ({}, {"list": "На неделю"}):
            d = llm._decide_with(
                [], _mk_make(_raw_decision("shopping_read", args)))
            assert d.action == "shopping_read", args

    def test_list_none_is_ok(self):
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_read", {"list": None})))
        assert d.action == "shopping_read"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_list_means_active_and_not_passed_downstream(self, value):
        # QA: пустая/состриженная в пустоту строка = «не задан» (активный
        # список); ключ убирается, чтобы downstream не получил list=""
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_read", {"list": value})))
        assert d.action == "shopping_read"
        assert "list" not in d.args

    def test_create_without_items_canonical_args(self):
        # (a) задача 20: create БЕЗ названных позиций теперь ВАЛИДЕН -- канон
        # args {} (живой факт 09.11: «Нет, я хочу чтобы ты создал новый
        # список» -> фолбэк после стрима-обещания «создаю»); dispatch
        # создаст пустой список и сам спросит товары
        for args in ({}, {"name": "На выходные"}, {"items": []},
                     {"items": None, "name": "На выходные"}):
            d = llm._decide_with(
                [], _mk_make(_raw_decision("shopping_create", args)))
            assert d.action == "shopping_create", args
        canonical = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_create", {})))
        assert canonical.args == {}  # канон -- пустые args

    def test_create_with_named_items_scotch_glue(self):
        # (b) «создай список, и запиши туда скотч и клей»: позиции названы ->
        # прежний строгий путь, items обязателен и структурно валиден
        raw = _raw_decision(
            "shopping_create",
            {"items": [{"text": "скотч"}, {"text": "клей"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "shopping_create"
        assert [i["text"] for i in d.args["items"]] == ["скотч", "клей"]

    def test_create_item_without_text_still_falls_back(self):
        # (c) мусор-структура в create (items присланы, но без text) --
        # прежний фолбэк; КОНТРАСТ: выдумки структурно валидны, их ловят
        # правила промпта (TestShoppingNoInvention), не валидатор
        raw = _raw_decision("shopping_create",
                            {"items": [{"qty": 2}, {"text": "хлеб"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    def test_add_with_empty_items_falls_back(self):
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_add", {"items": []})))
        assert d.action == "none"
        assert "?" in d.reply

    def test_add_item_without_text_falls_back(self):
        raw = _raw_decision("shopping_add", {"items": [{"qty": 2}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    def test_add_item_blank_text_falls_back(self):
        raw = _raw_decision("shopping_add", {"items": [{"text": "   "}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    def test_items_not_a_list_falls_back(self):
        raw = _raw_decision("shopping_create", {"items": "молоко"})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    def test_update_without_changes_falls_back(self):
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_update", {})))
        assert d.action == "none"
        assert "?" in d.reply

    def test_update_change_without_match_falls_back(self):
        raw = _raw_decision("shopping_update",
                            {"changes": [{"new_text": "кефир"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    def test_valid_update_passes(self):
        raw = _raw_decision(
            "shopping_update",
            {"list": "На неделю",
             "changes": [{"match": "молоко", "new_text": "кефир"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "shopping_update"

    def test_check_without_items_falls_back(self):
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_check", {})))
        assert d.action == "none"
        assert "?" in d.reply

    def test_valid_check_passes(self):
        raw = _raw_decision("shopping_check", {"items": [{"text": "хлеб"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "shopping_check"

    @pytest.mark.parametrize("action", ["shopping_add", "shopping_read",
                                        "shopping_update", "shopping_check",
                                        "shopping_delete"])
    def test_non_string_list_falls_back(self, action):
        # items/changes валидны, чтобы сработала именно проверка list
        raw = _raw_decision(action, {
            "list": 42,
            "items": [{"text": "хлеб"}],
            "changes": [{"match": "хлеб"}],
        })
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    def test_create_non_string_name_falls_back(self):
        raw = _raw_decision("shopping_create",
                            {"name": 123, "items": [{"text": "хлеб"}]})
        d = llm._decide_with([], _mk_make(raw))
        assert d.action == "none"
        assert "?" in d.reply

    @pytest.mark.parametrize("action,args", [
        ("search", {"query": "погода Москва"}),
        ("send_tg", {"to": "Маша", "message": "привет"}),
        ("send_vk", {"to": "Пётр", "message": "привет"}),
        ("call", {"contact": "Маша"}),
        ("none", {}),
    ])
    def test_old_actions_work_as_before(self, action, args):
        d = llm._decide_with([], _mk_make(_raw_decision(action, args)))
        assert d.action == action
        assert d.reply == "Готово"


# ---------------------------------------------------------------------------
# Правила против выдумок (фикс живого smoke): расплывчатая реплика вида
# «добавь в список» выдала shopping_create с ВЫДУМАННЫМ именем и позициями —
# структурная валидация их пропускает, поэтому запрет идёт словами промпта
# и примерами отказа от выдумок.
# ---------------------------------------------------------------------------
class TestShoppingNoInvention:
    """SYSTEM_PROMPT запрещает выдумывать позиции/имена/количества."""

    def test_never_invent_rule_present(self):
        p = llm.SYSTEM_PROMPT
        assert "НИКОГДА не придумывай" in p
        assert "которые пользователь произнёс сам" in p

    def test_add_without_items_is_refused_not_create(self):
        # задача 20: отказ-без-товаров теперь ТОЛЬКО для add (add строг);
        # «создай список» без позиций -- валидный shopping_create,
        # канон args {}; анти-выдумки про create/add сохранены дословно
        p = llm.SYSTEM_PROMPT
        assert "«Добавь в список» без единого названного товара" in p
        assert "«Что купить? Назови товары и количество.»" in p
        assert ("shopping_create и shopping_add с выдуманными "
                "позициями делать нельзя" in p)

    def test_no_reconstruction_no_invented_name(self):
        p = llm.SYSTEM_PROMPT
        assert "реконструируй" in p  # прошлые списки не переносим в args
        assert "сервер сам назовёт список «Список покупок»" in p
        assert "Выдумывать имя списка нельзя" in p

    def test_refusal_examples_present(self):
        p = llm.SYSTEM_PROMPT
        # задача 20: add без позиций -> отказ none; create без позиций ->
        # разрешён с ПУСТЫМИ args (канон), товары не выдуманы
        assert "«Добавь в список» — не сказано, что именно" in p
        assert "«Создай список покупок» — не назван ни один товар" in p

    def test_refusal_examples_answer_verbatim(self):
        # реплики примеров отвечают предписанной формулировкой;
        # порядок ключей action-first (З4, вердикт A З3):
        # add-отказ -- вербатим из правила; create без позиций --
        # нейтральное обещание и ПУСТЫЕ args (канон задачи 20)
        p = llm.SYSTEM_PROMPT
        assert ('{"action": "none", "args": {}, '
                '"reply": "Что докупить? Назови товары и количество."}') in p
        assert ('{"action": "shopping_create", "args": {}, "reply": '
                '"Создаю список. Назови, что в него положить."}') in p


# ---------------------------------------------------------------------------
# Стиль ответа (живой smoke): модель стримила в промежуточные сентенции
# «Жди, сука.» / «Чё, что докупить?» — TTS озвучивает их пользователю.
# Правила в статическом SYSTEM_PROMPT (uncensored-модель, стиль-гейта нет).
# Примеров с нецензурной репликой пользователя в few-shot НЕ добавляем —
# не учим модель мату на примерах.
# ---------------------------------------------------------------------------
class TestSystemPromptNoProfanity:
    """SYSTEM_PROMPT запрещает мат и нормирует промежуточные фразы."""

    def test_never_profanity_rule_present(self):
        p = llm.SYSTEM_PROMPT
        assert "НИКОГДА не употребляй мат" in p
        # запрет охватывает и reply, и стрим-промежуточные фразы
        assert "ни в reply, ни в промежуточных фразах" in p

    def test_intermediate_phrases_neutral_at_most_one(self):
        p = llm.SYSTEM_PROMPT
        assert "Промежуточные фразы во время обдумывания" in p
        assert "короткие и нейтральные" in p
        assert "не больше одной" in p

    def test_user_profanity_is_not_repeated(self):
        # мат из исходной реплики пользователя не воспроизводится
        p = llm.SYSTEM_PROMPT
        assert "повторять его нельзя" in p
        assert "перефразируй вежливо" in p


# ---------------------------------------------------------------------------
# Задача 22 (живые факты 10.11 22:30-22:36): дубликаты имён списков,
# args.list, выуженный из истории, ложь о хранилище. Новое действие
# shopping_lists (перечисление ВСЕХ списков -- исполняет dispatch из store)
# + правила маршрутизации. Канон shopping_lists: args {} -- перечисление
# делает dispatch; посторонние поля (items/list/name/...) -- мусор модели,
# фолбэк action=none с вопросом (по образцу остальных shopping-фолбэков).
# ---------------------------------------------------------------------------
class TestShoppingListsAndRouting:
    """shopping_lists (args {}) и правила: продолжение -> add, list только
    из произнесённого имени, без утверждений о хранилище из памяти."""

    def test_lists_empty_args_valid(self):
        # args {} -- единственная валидная форма перечисления
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_lists", {})))
        assert d.action == "shopping_lists"
        assert d.args == {}
        # Literal принял новое действие
        assert llm.Decision(reply="ok",
                            action="shopping_lists").action == "shopping_lists"

    @pytest.mark.parametrize("args", [
        {"items": [{"text": "молоко"}]},
        {"list": "Список покупок"},
        {"name": "Список покупок"},
        {"query": "все"},
    ])
    def test_lists_garbage_args_fall_back(self, args):
        # лишние поля -- мусор модели -> фолбэк action=none с вопросом
        d = llm._decide_with(
            [], _mk_make(_raw_decision("shopping_lists", args)))
        assert d.action == "none", args
        assert "?" in d.reply

    def test_lists_documented_in_prompt_with_example(self):
        p = llm.SYSTEM_PROMPT
        assert "action=shopping_lists" in p
        # (1) «какие списки у меня есть» -> shopping_lists args {}
        # (порядок ключей action-first, З4)
        assert "«Какие списки у меня есть?»" in p
        assert ('{"action": "shopping_lists", "args": {}, '
                '"reply": "Перечисляю."}') in p

    def test_products_after_items_question_are_add(self):
        # факт 1: продолжение «Хлеб и сахар» после вопроса «что купить?» --
        # это shopping_add (без list -> активный), НЕ повторный create
        p = llm.SYSTEM_PROMPT
        assert ("Прямой ответ пользователя товарами "
                "сразу после «что добавить/купить?»" in p)
        assert ("shopping_add с items и БЕЗ list (в активный список), "
                "НЕ повторный create" in p)
        assert ('{"action": "shopping_add", "args": {}, '
                '"reply": "Записываю хлеб и сахар."}') in p

    def test_list_arg_only_when_name_spoken(self):
        # факт 3: имя списка, выуженное из истории, дало дубликаты --
        # args.list пишется только по имени из СВОЕЙ фразы
        p = llm.SYSTEM_PROMPT
        assert ("list в args -- только имя, произнесённое "
                "пользователем в ЭТОЙ фразе" in p)
        # (2) «создай список покупок на неделю» -> create с именем,
        # собранным ТОЛЬКО из слов пользователя (порядок action-first, З4)
        assert "«Создай список покупок на неделю» (имя -- из слов пользователя)" in p
        assert ('{"action": "shopping_create", "args": '
                '{"name": "Список покупок на неделю"}, "reply": '
                '"Создаю список «Список покупок на неделю»."}') in p
        # старая нормализация «На неделю» устранена из примеров
        assert '"name": "На неделю"' not in p

    def test_no_memory_claims_and_another_list_asks_name(self):
        # факт 4: без вранья о хранилище; «какие списки» -> shopping_lists,
        # «что в X» -> shopping_read; «ещё один список» без имени -> вопрос
        p = llm.SYSTEM_PROMPT
        assert "не утверждай из памяти, сколько списков и что в них" in p
        assert "«ещё один список» без имени -- уточни имя, не создавай" in p
        # (5) «Создаю ещё один список» -> none + вопрос, не второй дубль
        # (порядок ключей action-first, З4)
        assert "«Создаю ещё один список»" in p
        assert ('{"action": "none", "args": {}, '
                '"reply": "Как назвать новый список?"}') in p


# ---------------------------------------------------------------------------
# Задача 24 (живые факты 12.11 10:00): модель брала имя списка из ИСТОРИИ
# («На неделю» из прошлого хода) для «Что у меня по списку покупок?» --
# чтение ушло в чужой список; «А что по списку, в список покупок?» без имени
# ушло в активный (снова «На неделю», снова мимо). Правило усилено: имя
# ТОЛЬКО из текущей фразы + канонизация склонений; пример чтения переучен
# с args {} (фолбэк на активный) на каноническое имя; добавлен
# контраст-пример «что по на неделю».
# ---------------------------------------------------------------------------
class TestShoppingReadNameCanon:
    """args.list -- имя из ТЕКУЩЕЙ фразы; склонения канонизируются
    («по списку покупок» = «Список покупок»); история -- не источник имён."""

    def test_rule_canonizes_declensions_and_bans_history(self):
        p = llm.SYSTEM_PROMPT
        assert "склонения «по списку покупок», «в списке покупок»" in p
        assert "это имя «Список покупок»" in p
        assert "имя из истории диалога не переноси" in p

    def test_read_example_canonical_name_replaces_empty_args(self):
        # (1)+(2) «что у меня по списку покупок?» -> shopping_read с
        # каноническим args.list «Список покупок»; прежний args {}
        # (фолбэк на активный -- живой факт 10:00:20, мимо) переучен:
        # read-примеров с пустыми args больше нет (порядок action-first,
        # З4: args стоит сразу после action)
        p = llm.SYSTEM_PROMPT
        assert ('{"action": "shopping_read", "args": '
                '{"list": "Список покупок"}, '
                '"reply": "Читаю список покупок."}') in p
        assert '"action": "shopping_read", "args": {}' not in p

    def test_read_second_example_na_nedelyu_verbatim(self):
        # (3) «что по на неделю» -> {"list": "На неделю"} -- контраст-пример:
        # каноническое имя хранилища пишется как есть
        p = llm.SYSTEM_PROMPT
        assert "«Что по на неделю?»" in p
        assert ('{"action": "shopping_read", "args": {"list": "На неделю"},'
                ' "reply": "Читаю список на неделю."}') in p

    def test_task22_contracts_untouched(self):
        # (4) контракт задачи 22 не ослаблен: имя во фразе => args.list
        # допустим (пример сметаны остался); «Хлеб и сахар» после вопроса --
        # add без list (пример не тронут); существующие тест-фразы
        # («по списку покупок», «В списке на неделю») живы
        p = llm.SYSTEM_PROMPT
        assert ('{"action": "shopping_add", "args": {"list": "На неделю", '
                '"items": [{"text": "сметана"}]}, '
                '"reply": "Добавляю сметану в список на неделю."}') in p
        assert ('{"action": "shopping_add", "args": {}, '
                '"reply": "Записываю хлеб и сахар."}') in p
        assert "по списку покупок" in p
        assert "В списке на неделю" in p


# ---------------------------------------------------------------------------
# З4 (вердикт A З3, 25/25 action-first): схема и ВСЕ примеры SYSTEM_PROMPT
# переставлены в порядок {action, args, reply}; тексты реплик/args/правил
# не менялись (алгоритм перестановки З3 -- scripts/probe-key-order.py).
# ---------------------------------------------------------------------------
class TestActionFirstPromptOrder:
    def test_schema_line_action_first(self):
        lines = llm.SYSTEM_PROMPT.splitlines()
        schema = [ln for ln in lines if ln.startswith('{"action": "<search|')]
        assert len(schema) == 1
        assert schema[0].endswith('"reply": "<текст для озвучки вслух>"}')

    def test_no_line_left_in_old_order(self):
        # ни одна строка-схема/пример не осталась в старом порядке reply-first
        old = [ln for ln in llm.SYSTEM_PROMPT.splitlines()
               if ln.startswith('{"reply"')]
        assert old == []

    def test_schema_reminder_action_first(self):
        import importlib
        decide_mod = importlib.import_module("llm.decide")
        reminder = decide_mod._SCHEMA_REMINDER
        assert "{action, args, reply}" in reminder
        assert "{reply, action, args}" not in reminder


# ---------------------------------------------------------------------------
# З4 (живой инцидент 14.11 13:28:46): «кто такой Пушкин?» ушёл в
# action=search (интернет + confirm-гейт), хотя ответ есть в общих знаниях
# модели. Фикс -- промпт-уровень: правило выбора search/none рядом с
# описанием search + few-shot контрпример «Пушкин» -> action=none.
# Поведенческие фолбэки НЕ тронуты (и не проверяются здесь):
# дайджест -> search (test_search_digest_fallback), ответ из памяти при
# отказе (answer_from_memory), валидация пустого query
# (test_missing_search_query_rejected).
# ---------------------------------------------------------------------------
class TestKnowledgeVsSearchRouting:
    """SYSTEM_PROMPT: вопросы из общих знаний -- action=none БЕЗ интернета;
    action=search -- только актуальные/быстро меняющиеся данные."""

    def test_knowledge_rule_present(self):
        p = llm.SYSTEM_PROMPT
        # устойчивые подстроки правила выбора (рядом с описанием search)
        assert "Отвечай сам (action=none)" in p
        assert "есть в твоих общих знаниях" in p
        assert "АКТУАЛЬНЫХ данных" in p
        assert "либо если ты честно не знаешь ответ" in p

    def test_pushkin_counterexample_none(self):
        p = llm.SYSTEM_PROMPT
        # реплика-кейс с пояснением и JSON-контрпример (action-first порядок
        # ключей, З4 -- см. TestActionFirstPromptOrder)
        assert "«Кто такой Пушкин?» — ответ из общих знаний, поиск НЕ нужен" in p
        assert ('{"action": "none", "args": {}, "reply": '
                '"Пушкин — великий русский поэт XIX века, '
                'автор «Евгения Онегина»."}') in p

    def test_conversational_prefix_none(self):
        # Д3 (живой промах 14.11, data/voice_latency/baseline.md):
        # вариация «Значит, кто такой Пушкин?» ушла в search 1/3.
        # few-shot-пример с префиксом-связкой учит: префикс маршрут
        # не меняет (общие знания -> none, поиск не нужен). Сосед
        # без префикса не сломан; few-shot -- часть SYSTEM_PROMPT,
        # поэтому проверяется текст промпта (без вызова реальной LLM).
        p = llm.SYSTEM_PROMPT
        assert ("«Значит, кто такой Пушкин?» — префикс-связка не меняет "
                "маршрут: общие знания, поиск НЕ нужен") in p
        # JSON-контрпример при префиксе тот же none (action-first,
        # как у соседних) -- строка-ответ ровно у обоих примеров
        assert p.count('{"action": "none", "args": {}, "reply": '
                       '"Пушкин — великий русский поэт XIX века, '
                       'автор «Евгения Онегина»."}') == 2
        # сосед без префикса не сломался
        assert "«Кто такой Пушкин?» — ответ из общих знаний, поиск НЕ нужен" in p
