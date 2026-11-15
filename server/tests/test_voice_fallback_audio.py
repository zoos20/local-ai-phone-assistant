"""Гейт озвучки /ws_voice: что и когда телефон слышит (задачи 14/16 + З5).

Эволюция гейта:
- задача 14: dispatch мутирует decision ПОСЛЕ стриминга -- модель уже
  отстримела оптимистичный reply, а фолбэк-уточнение «Нет активного
  списка...» не досинтезировалось: телефон при streamed=true финальный
  reply НЕ повторяет. Решение: answer.py отдаёт сигнал reply_replaced
  (итоговый reply != отстримленной реплике модели), voice_ws
  досинтезирует итог тем же механизмом.
- задача 16 (phone.log 23:00:58-23:01:08, «двойная озвучка»): mid-stream
  сентенции модели («Ага, создал!», «Готово, держи!») озвучивались ДО
  готовности JSON-решения, а потом досылался шаблон dispatch («Список
  «Вкусняшки» не найден») -- противоречивый хор. Решение тогда:
  стрим глотался целиком, озвучивался ровно один финальный reply.
- З5 (пересмотр гейта; факт action -- из З4): решение известно по ходу
  генерации -- decide_stream отдаёт action через on_action ДО первой
  сентенции (action-first, живой probe З3: 25/25). Стрим ОЗВУЧИВАЕТСЯ
  по мере прихода при action=none (ответ модели и есть финальный
  reply; финальный audio при этом НЕ досылается -- он уже звучал
  чанками) и ГЛОТАЕТСЯ при действиях search/send_tg/send_vk/call/
  shopping_* (dispatch почти всегда заменит реплику) и при ещё
  неизвестном action (старый reply-first порядок -- безопасный
  дефолт). Правило применяется ПРИ dequeue; сентенции идут через
  asyncio.Queue в ОДИН FIFO-воркер (синтез серийный, LLM-поток не
  блокируется). Санитайзер применяется к каждой сентенции при
  dequeue: изменённая сентенция (мат) -> mute до конца стрима, финал
  уйдёт целиком. Страховка stop-chunks: при отправленных чанках и
  замене reply (или упавшем синтезе чанка) финальные sentence+audio
  досылаются, reply-событие несёт опц. поле reply_replaced=true --
  телефон (З6) гасит очередь чанков и играет финал.
- QA З7 (микро-фиксы, поведение гейта не меняется): (1) обрыв клиента
  при отправке чанка не убивает воркер -- исключение глотается НА
  ЭЛЕМЕНТ (по образцу _seg_job), mute как при упавшем синтезе,
  ws.close() исполняется всегда; (2) latch: при первом dequeue с
  неизвестным action mute защёлкивается до конца стрима -- при факте,
  пришедшем в середине (гипотетический «перепутанный» порядок),
  смешанного озвучивания нет, ВСЕ сентенции глотаются.

Инвариант порядка (все этапы): sentence+audio РАНЬШЕ reply-события --
телефон (VoiceStream.kt) откладывает reply до done, а аудио-чанки до
reply проигрывает сразу. Живой прогон 09.11 (S6): sentence t=+15653 ->
audio (2975 мс) t=+15664 -> reply(confirm=true) t=+15665.

Тесты гоняют НАСТОЯЩИЙ ws_voice через FakeWS с реальным PCM: сначала
3 с громкого голоса (после grace 2500 мс включается armed), потом 1.5 с
тишины (endpoint при quiet 900 мс -- З8, 15.11; до него 1300). Пороги
_WS_* не подменяются и не меняются (§7): кадры дают запас, endpoint
срабатывает детерминированно. С З8 порог закрытия сравнялся с сегментным
(_WS_SEG_QUIET_MS=900), а endpoint проверяется РАНЬШЕ -- сегментный stt
на лету не срабатывает, распознанный текст даёт финальный tail-stt.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time

import pytest

import answer as answer_mod
import llm
import shopping_store
import stt as stt_mod
import tts as tts_mod
import voice_ws
from endpointing import (
    _WS_NO_VOICE_MS,
    _WS_QUIET_MS,
    EndpointDetector,
)
from app_state import state
from config import settings
from dispatch import _Q_NOT_FOUND, _Q_NO_ACTIVE
from reply_sanitize import sanitize_reply

MODEL_SENTENCE = "Добавляю яйца в список."
PLAIN_REPLY = "Привет, я тут, слушаю тебя."


# --------------------------------------------------------------------------
# FakeWS + реальный PCM до endpoint (см. докстринг)
# --------------------------------------------------------------------------
def _pcm(amplitude: int) -> bytes:
    """100 мс PCM s16le mono 16 кГц (3200 байта -- один RMS-кадр)."""
    if not amplitude:
        return bytes(voice_ws._WS_FRAME)
    samples = [amplitude] * (voice_ws._WS_FRAME // 2)
    return struct.pack(f"<{len(samples)}h", *samples)


def _stream_messages() -> list[dict]:
    """30 кадров голоса (после grace) + 15 тишины (quiet) + {"event":"end"}."""
    frames = [_pcm(8000)] * 30 + [_pcm(0)] * 15
    msgs: list[dict] = [{"type": "websocket.receive", "bytes": f}
                        for f in frames]
    msgs.append({"type": "websocket.receive",
                 "text": json.dumps({"event": "end"})})
    return msgs


class StreamWS:
    """WebSocket-заглушка: отдаёт заряженный поток, пишет отправленное."""

    def __init__(self) -> None:
        self.query_params: dict[str, str] = {}
        self.sent: list[dict] = []
        self._msgs = _stream_messages()

    async def accept(self) -> None:
        pass

    async def send_json(self, obj: dict) -> None:
        self.sent.append(obj)

    async def close(self, code: int | None = None) -> None:
        pass

    async def receive(self) -> dict:
        if self._msgs:
            return self._msgs.pop(0)
        return {"type": "websocket.disconnect"}


class DyingWS(StreamWS):
    """Клиент «умер» ПОСЛЕ endpoint (реальный обрыв mid-stream): все
    отправки ответной фазы (sentence/audio/reply/done/error) поднимаются,
    как send_json по мёртвому сокету; ready/endpoint/hear успевают уйти.
    close() считается -- инвариант «ws.close() исполняется всегда»."""

    def __init__(self) -> None:
        super().__init__()
        self.closes = 0
        self.failed_sends = 0

    async def send_json(self, obj: dict) -> None:
        if obj.get("event") in ("sentence", "audio", "reply", "done",
                                "error"):
            self.failed_sends += 1
            raise RuntimeError("broken pipe (client gone)")
        self.sent.append(obj)

    async def close(self, code: int | None = None) -> None:
        self.closes += 1


def _events(ws: StreamWS, name: str) -> list[dict]:
    return [e for e in ws.sent if e.get("event") == name]


def _sentences(ws: StreamWS) -> list[str]:
    return [e["text"] for e in _events(ws, "sentence")]


def _timing_lines(caplog) -> list[str]:
    """Лог-лайны «ws_voice timing» из caplog (логгер assistant, З1)."""
    return [r.getMessage() for r in caplog.records
            if r.name == "assistant" and "ws_voice timing" in r.getMessage()]


def _timing_fields(line: str) -> dict[str, str]:
    """Поля лог-лайна в dict (heard -- последний токен: repr с пробелами)."""
    head, _, heard = line.partition(" heard=")
    out: dict[str, str] = {"heard": heard}
    for tok in head.split(" ")[2:]:
        key, _, val = tok.partition("=")
        out[key] = val
    return out


def _fake_stream(reply: str, action: str = "none", args: dict | None = None,
                 sentence: str | list[str] | None = None,
                 action_first: bool = True, action_gap: float = 0.0):
    """Подмена llm.decide_stream: стримит предложение(я), отдаёт решение.

    З5: mock вызывает on_action (факт action) и on_sentence (сентенции)
    как реальный decide_stream с З4. action_first=True (промпт З4,
    живой probe З3 25/25) -- on_action ДО первой сентенции: гейт видит
    факт до любого dequeue. action_first=False -- старый порядок
    (reply-first): сентенции уходят первыми, on_action после
    action_gap секунд простоя потока (за это время воркер event-loop'а
    успевает dequeue'нуть всё -- гейт по умолчанию их глотает).
    sentence -- одна строка ИЛИ список (mid-stream болтовня).
    """
    items = ([sentence] if isinstance(sentence, str)
             else list(sentence or []))

    def decide(text, history, on_sentence=None, on_first_token=None,
               on_query=None, on_action=None):
        if on_first_token is not None:
            on_first_token()
        if action_first and on_action is not None:
            on_action(action)
        if on_sentence is not None:
            for s in items:
                on_sentence(s)
        if not action_first:
            if action_gap:
                time.sleep(action_gap)   # стрим «обогнал» факт action
            if on_action is not None:
                on_action(action)
        return llm.Decision(reply=reply, action=action, args=args or {})

    return decide


def _run_ws(monkeypatch) -> StreamWS:
    ws = StreamWS()
    asyncio.run(voice_ws.ws_voice(ws))
    return ws


def _seed_active(name: str, items: list[str]) -> None:
    """Активный список (паттерн _seed из test_shopping_rest)."""

    async def go() -> None:
        lst = await shopping_store.create(name=name, items=items)
        await shopping_store.set_active(lst["id"])

    asyncio.run(go())


@pytest.fixture()
def voice_env(tmp_path, monkeypatch):
    """Изолированное окружение ws_voice: LAN, tmp-стор, подменённые stt/tts."""
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(voice_ws, "_WS_ACTIVE", 0)
    monkeypatch.setattr(shopping_store, "_SHOPPING_FILE",
                        tmp_path / "shopping_lists.json")
    monkeypatch.setattr(state, "shopping_lock", asyncio.Lock())
    monkeypatch.setattr(state, "active_list_id", None)
    state.history.clear()
    state.history_stamp = 0.0
    state.pending.clear()

    tts_calls: list[str] = []

    def fake_synth(text, voice=None):
        tts_calls.append(text)
        return ("WAV:" + text).encode("utf-8")

    monkeypatch.setattr(tts_mod, "synthesize", fake_synth)
    monkeypatch.setattr(stt_mod, "transcribe_pcm",
                        lambda pcm: "добавь яйца в список")
    monkeypatch.setattr(llm, "get_profile_voice", lambda: "test-voice")
    yield tts_calls


class TestFallbackQuestionVoiced:
    """Финальный reply dispatch'а (шаблон/фолбэк) -- ЕДИНСТВЕННАЯ озвучка.

    З5: при действии (shopping_*) факт action известен ДО сентенций
    (action-first) -- стрим глотается при dequeue, единственная
    озвучка -- решённая реплика (фолбэк/шаблон).
    """

    def test_s8_no_active_list_question_voiced(self, voice_env, monkeypatch):
        # модель отстримела ложное «Добавляю...» (гейт ГЛОТАЕТ: action
        # известен до dequeue), хранилище пусто -> _fallback: action=none,
        # reply=уточнение (S8)
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=MODEL_SENTENCE, action="shopping_add",
            args={"items": [{"text": "яйца"}]}, sentence=MODEL_SENTENCE))
        ws = _run_ws(monkeypatch)

        sentences = _sentences(ws)
        assert sentences == [_Q_NO_ACTIVE]          # единственный текст
        assert MODEL_SENTENCE not in sentences      # стрим-болтовня НЕ слышна
        assert len(_events(ws, "audio")) == 1       # ровно один аудио-чанк

        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == _Q_NO_ACTIVE
        assert reply_ev["confirm"] is False
        assert reply_ev["streamed"] is True         # телефон дубль не скажет
        assert "reply_replaced" not in reply_ev     # чанков не было
        assert _events(ws, "done")                  # сессия закрыта штатно
        # tts вызван ровно один раз -- финальным уточнением
        assert voice_env == [_Q_NO_ACTIVE]
        # история: формат {reply, action, args} не расширен (§10.4)
        last = json.loads(state.history[-1]["content"])
        assert last == {"reply": _Q_NO_ACTIVE, "action": "none", "args": {}}

    def test_not_found_question_voiced(self, voice_env, monkeypatch):
        # имя списка не найдено -> второй фолбэк-текст, тот же гейт
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Читаю список покупок.", action="shopping_read",
            args={"list": "НеСуществует"},
            sentence="Читаю список покупок."))
        ws = _run_ws(monkeypatch)

        expected = _Q_NOT_FOUND.format(name="НеСуществует")
        assert _sentences(ws) == [expected]         # шаблон -- единственный
        assert "Читаю список покупок." not in _sentences(ws)
        assert _events(ws, "reply")[0]["reply"] == expected
        assert _events(ws, "reply")[0]["streamed"] is True
        assert len(_events(ws, "audio")) == 1
        assert voice_env == [expected]

    def test_success_template_voiced(self, voice_env, monkeypatch):
        # детерминированный шаблон dispatch'а (эхо добавления) --
        # единственная озвучка: стрим модели («Добавляю...») молчит
        _seed_active("На неделю", ["яйца"])
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=MODEL_SENTENCE, action="shopping_add",
            args={"items": [{"text": "яйца"}]}, sentence=MODEL_SENTENCE))
        ws = _run_ws(monkeypatch)

        final = "В список «На неделю» добавлено: яйца."
        sentences = _sentences(ws)
        assert sentences == [final]
        assert _events(ws, "reply")[0]["reply"] == final
        assert _events(ws, "reply")[0]["streamed"] is True
        assert voice_env == [final]


class TestExistingBehaviorIntact:
    """Не-заменённый reply и confirm-ветка при новом гейте работают так же."""

    def test_plain_reply_voiced_once(self, voice_env, monkeypatch):
        # action=none без замены: стрим == reply (модель отстримела его
        # целиком одной сентенцией) -- чанк озвучен по мере прихода,
        # финальный audio НЕ досылается (было бы двойной речью);
        # синтез на сессию ровно один
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=PLAIN_REPLY, action="none", sentence=PLAIN_REPLY))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == [PLAIN_REPLY]       # стрим-чанк == reply
        assert len(_events(ws, "audio")) == 1        # один синтез на сессию
        assert voice_env == [PLAIN_REPLY]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == PLAIN_REPLY
        assert reply_ev["streamed"] is True          # чанки уже играли
        assert reply_ev["confirm"] is False
        assert "reply_replaced" not in reply_ev

    def test_confirm_branch_intact(self, voice_env, monkeypatch):
        # needs_confirm=true: вопрос-подтверждение озвучен, модельный стрим
        # молчит (гейт по action=search; раньше вопрос ДОсинтезировался
        # ПОСЛЕ озвученного стрима)
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Ищу погоду в Москве.", action="search",
            args={"query": "погода Москва"},
            sentence="Ищу погоду в Москве."))
        ws = _run_ws(monkeypatch)

        ask = ("Выполняю поиск в интернете: погода Москва. "
               "Скажи «да», чтобы я продолжила.")
        assert _sentences(ws) == [ask]
        assert voice_env == [ask]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["confirm"] is True
        assert reply_ev["streamed"] is True
        assert reply_ev["token"] and reply_ev["token"] in state.pending
        assert state.pending[reply_ev["token"]].action == "search"


class TestEventOrdering:
    """Порядок событий ws_voice: sentence+audio финальной реплики ДО reply.

    Телефон (VoiceStream.kt) откладывает reply до done, а аудио-чанки,
    пришедшие до reply, проигрывает сразу. Значит сервер обязан слать
    sentence+audio РАНЬШЕ reply -- иначе реплика не прозвучит. Живой
    прогон 09.11 (S6, ws_voice): sentence t=+15653 -> audio (2975 мс)
    t=+15664 -> reply(confirm=true) t=+15665.
    Инвариант закреплён FakeWS-тестами в ОБОИХ ветках З5: стрим-ветка
    (action=none -- чанки sentence+audio по мере прихода) и финал-ветка
    (действия -- единственные sentence+audio финала).
    """

    @staticmethod
    def _idx(ws: StreamWS, name: str) -> int:
        """Индекс первого события с данным event (ValueError, если нет)."""
        return [e.get("event") for e in ws.sent].index(name)

    def test_confirm_question_audio_precedes_reply(self, voice_env,
                                                   monkeypatch):
        # needs_confirm=true: вопрос-подтверждение озвучивается
        # (sentence+audio) ДО reply-события с confirm-токеном.
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Ищу погоду в Москве.", action="search",
            args={"query": "погода Москва"},
            sentence="Ищу погоду в Москве."))
        ws = _run_ws(monkeypatch)

        ask = ("Выполняю поиск в интернете: погода Москва. "
               "Скажи «да», чтобы я продолжила.")
        idx_reply = self._idx(ws, "reply")
        # последние два события перед reply -- ЕДИНСТВЕННАЯ озвучка
        # (финальный вопрос): sentence с текстом + его аудио
        assert ws.sent[idx_reply - 2] == {"event": "sentence", "text": ask}
        assert base64.b64decode(ws.sent[idx_reply - 1]["b64"]) == (
            b"WAV:" + ask.encode("utf-8"))
        # никаких других стрим-событий в сессии нет (гейт по action)
        assert len(_events(ws, "sentence")) == 1
        assert len(_events(ws, "audio")) == 1
        # done -- сразу после reply, вопрос успел уйти до reply
        assert self._idx(ws, "done") == idx_reply + 1
        # TTS вызван ровно один раз -- финальным вопросом
        assert voice_env == [ask]

    def test_fallback_question_audio_precedes_reply(self, voice_env,
                                                    monkeypatch):
        # S8: reply_replaced -> фолбэк-уточнение «Нет активного списка...»
        # уходит sentence+audio ДО reply-события (иначе телефон при
        # streamed=true промолчит).
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=MODEL_SENTENCE, action="shopping_add",
            args={"items": [{"text": "яйца"}]}, sentence=MODEL_SENTENCE))
        ws = _run_ws(monkeypatch)

        idx_reply = self._idx(ws, "reply")
        assert ws.sent[idx_reply - 2] == {"event": "sentence",
                                          "text": _Q_NO_ACTIVE}
        assert base64.b64decode(ws.sent[idx_reply - 1]["b64"]) == (
            b"WAV:" + _Q_NO_ACTIVE.encode("utf-8"))
        assert len(_events(ws, "sentence")) == 1     # модельная молчит
        reply_ev = ws.sent[idx_reply]
        assert reply_ev["confirm"] is False
        assert reply_ev["streamed"] is True
        assert self._idx(ws, "done") == idx_reply + 1

    def test_stream_chunks_audio_precedes_reply(self, voice_env,
                                                monkeypatch):
        # стрим-ветка (action=none): КАЖДАЯ сентенция -- sentence+audio,
        # reply -- после последнего чанка (инвариант в стрим-ветке).
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Раз. Два.", action="none",
            sentence=["Раз.", "Два."]))
        ws = _run_ws(monkeypatch)

        seq = [e["event"] for e in ws.sent
               if e["event"] in ("sentence", "audio", "reply", "done")]
        assert seq == ["sentence", "audio", "sentence", "audio",
                       "reply", "done"]
        assert _sentences(ws) == ["Раз.", "Два."]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == "Раз. Два."
        assert reply_ev["streamed"] is True          # чанки уже играли


class TestSingleVoiceStage:
    """З5: стрим озвучивается при action=none (== финальный reply,
    финальный дубль не досылается); при действиях стрим глотается --
    единственная озвучка == финальный reply.

    Историческая регрессия «двойной озвучки» (phone.log
    23:00:58-01:08: «Ага, создал!» + шаблон «Список не найден») теперь
    закрыта ГЕЙТОМ: шаблонные замены случаются только у действий,
    а их стрим молчит.
    """

    CHATTER = ["Ага, создал!",            # как в живом user-smoke 09.11
               "Вот сейчас добавляю печенье и варенье.",
               "Готово, держи!"]

    def test_shopping_chatter_not_voiced(self, voice_env, monkeypatch):
        # точный сценарий user-smoke: shopping_read с несуществующим
        # именем -> фолбэк «не найден»; стрим-болтовня глотается гейтом
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Добавляю печенье и варенье.", action="shopping_read",
            args={"list": "Вкусняшки"}, sentence=self.CHATTER))
        ws = _run_ws(monkeypatch)

        expected = _Q_NOT_FOUND.format(name="Вкусняшки")
        sentences = _sentences(ws)
        assert sentences == [expected]               # первый и единственный
        for piece in self.CHATTER:                   # болтовня НЕ слышна
            assert piece not in sentences
        assert len(_events(ws, "audio")) == 1
        assert voice_env == [expected]               # TTS == финальному reply
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == expected
        assert reply_ev["streamed"] is True

    def test_action_none_stream_is_voiced(self, voice_env, monkeypatch):
        # action=none (action-first): стрим ОЗВУЧИВАЕТСЯ по мере прихода;
        # финальный audio не досылается (чанки == reply), синтез -- по
        # чанку, без второго прохода
        reply = " ".join(self.CHATTER)
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=reply, action="none", sentence=self.CHATTER))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == self.CHATTER
        assert len(_events(ws, "audio")) == 3
        assert voice_env == self.CHATTER
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == reply
        assert reply_ev["streamed"] is True
        assert "reply_replaced" not in reply_ev

    def test_action_none_chatter_not_voiced(self, voice_env, monkeypatch):
        # старый порядок (reply-first, action_first=False): сентенции
        # dequeue'ятся ДО факта action (action_gap даёт воркеру время
        # разобрать очередь) -> безопасный дефолт глотания; озвучивается
        # только финальный reply, как до З5
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=PLAIN_REPLY, action="none", sentence=self.CHATTER,
            action_first=False, action_gap=0.1))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == [PLAIN_REPLY]
        assert len(_events(ws, "audio")) == 1
        assert voice_env == [PLAIN_REPLY]

    def test_tts_failure_sends_bare_reply(self, voice_env, monkeypatch):
        # краевой случай (voice_ws, ветка «чанков не было»): TTS финального
        # reply упал -> sentence/audio НЕ уходят, reply-событие несёт
        # streamed=false: телефон (AssistantService.speak()) при
        # streamed=false озвучит финальную реплику ЛОКАЛЬНЫМ TTS --
        # пользователь не остаётся без ответа (иначе он услышал бы
        # пустоту: при streamed=true телефон reply не повторяет).
        expected = _Q_NOT_FOUND.format(name="НеСуществует")
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Читаю список покупок.", action="shopping_read",
            args={"list": "НеСуществует"},
            sentence="Читаю список покупок."))

        def raiser(text, voice=None):
            raise RuntimeError("tts down")

        monkeypatch.setattr(tts_mod, "synthesize", raiser)   # поверх fake
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == []                # озвучки не было вовсе
        assert _events(ws, "audio") == []
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == expected       # финальный текст intact
        assert reply_ev["streamed"] is False       # скажет локальный TTS
        assert _events(ws, "done")                 # сессия закрыта штатно


class TestReplyReplacedFlag:
    """Сигнал answer.py -> voice_ws (только добавочное поле, история цела)."""

    def _answer(self, monkeypatch, text: str, *, reply: str, action: str,
                args: dict, sentence: str):
        monkeypatch.setattr(llm, "decide_stream",
                            _fake_stream(reply, action, args, sentence))
        spoken: list[str] = []
        resp = asyncio.run(
            answer_mod._answer(text, on_sentence=spoken.append))
        return resp, spoken

    def test_flag_true_when_dispatch_replaces_reply(self, voice_env,
                                                    monkeypatch):
        resp, spoken = self._answer(
            monkeypatch, "добавь яйца в список", reply=MODEL_SENTENCE,
            action="shopping_add", args={"items": [{"text": "яйца"}]},
            sentence=MODEL_SENTENCE)
        assert spoken == [MODEL_SENTENCE]        # отстримано репликой модели
        assert resp.reply == _Q_NO_ACTIVE        # замена dispatch'ом
        assert resp.action == "none" and resp.args == {}
        assert resp.reply_replaced is True
        assert resp.needs_confirm is False

    def test_flag_false_when_reply_unchanged(self, voice_env, monkeypatch):
        resp, spoken = self._answer(
            monkeypatch, "привет", reply=PLAIN_REPLY, action="none",
            args={}, sentence="Привет,")
        assert spoken == ["Привет,"]
        assert resp.reply == PLAIN_REPLY
        assert resp.reply_replaced is False      # замены не было


class TestTelemetry:
    """З1: единый лог-лайн «ws_voice timing» на каждый ws-ответ.

    Стадии собираются на существующих часах (t0/perf_counter); поведение
    конвейера не меняется. Харнесс тот же (FakeWS + реальный PCM,
    _fake_stream -- мок decide_stream); строка ловится caplog по логгеру
    «assistant». В фейковом времени кадры идут без пауз, поэтому
    wall-поля (last_voice_ms/audio_send_ms) -- это РАЗНОСТИ около нуля,
    а НЕ позиции в записи: позиции в записи несут voice_pos_ms
    (контроль: 30 голосовых кадров по 100 мс == 3000) и endpoint_ms
    (после конца голоса, детерминированно > 3000). З5: chunks --
    отправленные audio-события (чанки стрима + финал); tts_first_ms --
    первый из них.
    """

    FIELDS = {"last_voice_ms", "endpoint_ms", "stt_total_ms",
              "first_token_ms", "decision_ms", "tts_first_ms",
              "audio_send_ms", "total_ms", "chunks", "voice_pos_ms",
              "heard"}

    def test_full_path_line_with_all_fields(self, voice_env, monkeypatch,
                                            caplog):
        caplog.set_level(logging.INFO, logger="assistant")
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=PLAIN_REPLY, action="none", sentence="Привет,"))
        ws = _run_ws(monkeypatch)

        assert _events(ws, "reply")[0]["reply"] == PLAIN_REPLY
        lines = _timing_lines(caplog)
        assert len(lines) == 1                    # ровно один лог-лайн
        fields = _timing_fields(lines[0])
        assert set(fields) == self.FIELDS         # полный набор полей
        assert fields["heard"] == repr("добавь яйца в список")
        assert fields["chunks"] == "1"            # один отправленный чанк
        assert fields["last_voice_ms"] != "None"  # голос был, M1 определён
        assert int(fields["last_voice_ms"]) >= 0
        assert int(fields["audio_send_ms"]) >= 0  # M1 -- разность, >= 0
        for key in ("endpoint_ms", "stt_total_ms", "total_ms",
                    "voice_pos_ms"):
            assert int(fields[key]) >= 0
        for key in ("first_token_ms", "decision_ms", "tts_first_ms"):
            assert fields[key] != "None"          # LLM/TTS были
        # контроль позиции/endpoint (порог тишины не привязываем --
        # его вправе менять только A/B задачи З8)
        assert int(fields["voice_pos_ms"]) == 3000
        assert int(fields["endpoint_ms"]) > 3000
        # прежняя итоговая строка заменена; пре-ответная ОСТАВЛЕНА
        assert not any("ws_voice: done in" in r.getMessage()
                       for r in caplog.records)
        assert any("ws_voice: endpoint=" in r.getMessage()
                   for r in caplog.records)

    def test_not_heard_reply_line_with_nones(self, voice_env, monkeypatch,
                                             caplog):
        # STT пусто -> ветка «Не расслышал»: ответ ЕСТЬ -> строка пишется,
        # LLM/TTS-поля None (ответной фазы не было)
        caplog.set_level(logging.INFO, logger="assistant")
        monkeypatch.setattr(stt_mod, "transcribe_pcm", lambda pcm: "")
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(PLAIN_REPLY))
        ws = _run_ws(monkeypatch)

        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == "Не расслышал, повтори, пожалуйста."
        lines = _timing_lines(caplog)
        assert len(lines) == 1
        fields = _timing_fields(lines[0])
        assert set(fields) == self.FIELDS
        for key in ("first_token_ms", "decision_ms", "tts_first_ms",
                    "audio_send_ms"):
            assert fields[key] == "None"
        assert fields["chunks"] == "0"
        assert fields["last_voice_ms"] != "None"  # голосовые кадры были
        assert int(fields["last_voice_ms"]) >= 0
        assert fields["heard"] == repr("")
        assert int(fields["endpoint_ms"]) > 0     # endpoint по тишине


class TestStopChunks:
    """З5, страховка stop-chunks: чанки стрима != финал.

    Правила (детерминированные, по ТЗ З5):
    (а) при отправленных чанках и замене reply -> финальные
        sentence+audio + опц. поле reply_replaced=true;
    (б) чанки == reply (не заменён) -> финального audio НЕТ,
        streamed=true;
    (в) упал синтез чанка -> mute + финальный полный reply-аудио
        с reply_replaced=true;
    (г) action=действие -> стрим не озвучивается вовсе, финал как раньше;
    (д) вторая попытка decide_stream молчит -- закреплено в
        tests/test_llm.py (test_second_attempt_is_silent: on_action и
        стрим только на attempt==0);
    (е) санитайзер на стриме: сентенция с матом не озвучивается, финал
        чистый с reply_replaced=true.
    """

    def test_replaced_final_with_flag(self, voice_env, monkeypatch):
        # (а): action=none -- стрим «Раз. Два.» озвучен двумя чанками;
        # dispatch ЗАМЕНИЛ reply (тестовая подмена answer._dispatch) ->
        # resp.reply_replaced=true -> финальные sentence+audio
        # (полный чистый reply) + поле reply_replaced=true
        async def fake_dispatch(decision, text=""):
            return ("Замена от dispatch.", False, None)

        monkeypatch.setattr(answer_mod, "_dispatch", fake_dispatch)
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Раз. Два.", action="none", sentence=["Раз.", "Два."]))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == ["Раз.", "Два.", "Замена от dispatch."]
        assert len(_events(ws, "audio")) == 3
        assert voice_env == ["Раз.", "Два.", "Замена от dispatch."]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == "Замена от dispatch."
        assert reply_ev["streamed"] is True
        assert reply_ev["reply_replaced"] is True
        # финальные sentence+audio -- непосредственно ПЕРЕД reply
        seq = [e["event"] for e in ws.sent
               if e["event"] in ("sentence", "audio", "reply", "done")]
        assert seq[-4:] == ["sentence", "audio", "reply", "done"]

    def test_unreplaced_chunks_no_final_audio(self, voice_env, monkeypatch):
        # (б): чанки == decision.reply -- финальный audio НЕ досылается
        # (повтор == двойная речь); streamed=true, опц. поля нет
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Раз. Два.", action="none", sentence=["Раз.", "Два."]))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == ["Раз.", "Два."]
        assert len(_events(ws, "audio")) == 2
        assert voice_env == ["Раз.", "Два."]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == "Раз. Два."
        assert reply_ev["streamed"] is True
        assert "reply_replaced" not in reply_ev

    def test_chunk_synth_failure_mutes_and_finals(self, voice_env,
                                                  monkeypatch):
        # (в): второй чанк упал при синтезе -> mute до конца стрима;
        # финал: полный reply целиком + reply_replaced=true (телефон
        # переиграет финал, без «дыр» в речи)
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Раз. Два.", action="none", sentence=["Раз.", "Два."]))

        def flaky(text, voice=None):
            if text == "Два.":
                raise RuntimeError("chunk tts down")
            voice_env.append(text)
            return ("WAV:" + text).encode("utf-8")

        monkeypatch.setattr(tts_mod, "synthesize", flaky)   # поверх fake
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == ["Раз.", "Раз. Два."]  # «Два.» НЕ звучал
        assert len(_events(ws, "audio")) == 2
        assert voice_env == ["Раз.", "Раз. Два."]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == "Раз. Два."
        assert reply_ev["streamed"] is True
        assert reply_ev["reply_replaced"] is True

    def test_action_stream_swallowed_entirely(self, voice_env, monkeypatch):
        # (г): action=search -- стрим НЕ озвучивается ВОВСЕ (несколько
        # сентенций); единственная озвучка -- вопрос-подтверждение
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply="Ищу погоду в Москве.", action="search",
            args={"query": "погода Москва"},
            sentence=["Ищу погоду.", "Секунду."]))
        ws = _run_ws(monkeypatch)

        ask = ("Выполняю поиск в интернете: погода Москва. "
               "Скажи «да», чтобы я продолжила.")
        assert _sentences(ws) == [ask]
        assert "Ищу погоду." not in _sentences(ws)
        assert "Секунду." not in _sentences(ws)
        assert len(_events(ws, "audio")) == 1
        assert voice_env == [ask]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["confirm"] is True
        assert reply_ev["token"] in state.pending

    def test_stream_sanitized_not_voiced_with_flag(self, voice_env,
                                                   monkeypatch):
        # (е): мат во ВТОРОЙ сентенции стрима: первая чистая -- озвучена,
        # вторая изменена санитайзером -> mute до конца стрима; финал --
        # полный чистый reply + reply_replaced=true
        full = "Привет. Да бля, держи."
        final = sanitize_reply(full)
        assert final != full                      # финал будет заменён
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=full, action="none",
            sentence=["Привет.", "Да бля, держи."]))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == ["Привет.", final]
        for text in _sentences(ws):               # мат никуда не дошёл
            assert "бля" not in text
        assert len(_events(ws, "audio")) == 2
        assert voice_env == ["Привет.", final]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == final
        assert reply_ev["streamed"] is True
        assert reply_ev["reply_replaced"] is True

    def test_stream_mat_first_chunk_old_style_final(self, voice_env,
                                                    monkeypatch):
        # (е), крайний случай: мат в ПЕРВОЙ сентенции -> mute сразу,
        # чанков не было -> прежний путь: финальный чистый reply одним
        # аудио; опц. поле НЕ ставится (отсутствие == старое поведение,
        # телефону нечего останавливать)
        full = "Да бля, держи."
        final = sanitize_reply(full)
        assert final != full
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=full, action="none", sentence=[full]))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == [final]
        assert len(_events(ws, "audio")) == 1
        assert voice_env == [final]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == final
        assert reply_ev["streamed"] is True
        assert "reply_replaced" not in reply_ev


class TestStreamWorkerFaults:
    """QA З7 (фикс 1): обрыв клиента при отправке чанка не должен
    убивать воркер и всплывать наружу.

    Прежде: ws.send_json по мёртвому сокету поднимал исключение В
    _voice_chunk, воркер умирал, а await worker_task в finally
    перевзбуждал исключение -> ws.close() пропускался, трейс всплывал
    дважды (asyncio.run поднял бы его -- тест упал бы прямо на _run_ws).
    Теперь: исключение ловится НА ЭЛЕМЕНТ в _sentence_worker (по образцу
    _seg_job -- инвариант файла «не всплывает»), mute включается как при
    упавшем синтезе, воркер живёт до сентинела; abnormal-выход
    закономерен (клиент мёртв), но ws.close() исполняется всегда.
    """

    def test_client_gone_midchunk_closes_gracefully(self, voice_env,
                                                    monkeypatch, caplog):
        # send_json падает на всех событиях ответной фазы (клиент умер
        # после endpoint): первое падение -- внутри воркера на стрим-
        # событии sentence; финальная отправка тоже падает ->
        # abnormal-ветка -> finally с ws.close().
        caplog.set_level(logging.WARNING, logger="assistant")
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=PLAIN_REPLY, action="none", sentence=PLAIN_REPLY))
        ws = DyingWS()
        asyncio.run(voice_ws.ws_voice(ws))   # поднятие = провал теста

        assert ws.closes == 1                # ws.close() исполнен
        assert ws.failed_sends >= 2          # чанк + финал/error
        assert _events(ws, "reply") == []    # до reply не дошло
        assert _events(ws, "audio") == []    # до audio не дошло
        # исключение поймано ИМЕННО воркером (по элементу) + abnormal-лог
        assert any("stream chunk send failed" in r.getMessage()
                   for r in caplog.records)
        assert any("session ended abnormally" in r.getMessage()
                   for r in caplog.records)


class TestUnknownActionLatch:
    """QA З7 (фикс 2): latch «unknown action -> mute до конца стрима».

    Reply-first стрим, в котором факт action приходит В СЕРЕДИНЕ
    (гипотетический «перепутанный» порядок ключей): при ПЕРВОМ dequeue
    с неизвестным action mute защёлкивается -- ВСЕ сентенции глотаются,
    включая идущие ПОСЛЕ факта (action=none). Без latch получилось бы
    смешанное озвучивание: «Раз.»/«Два.» глотались, а «Три.» звучала бы
    -- и финал при этом не досылался бы (чанки == reply, streamed=true
    без финала) -- телефон услышал бы недоговорённый ответ.
    Для ИЗВЕСТНОГО действия latch не ставится: on_action однократен
    (attempt==0, З4), факт не меняется по ходу стрима -- правило
    пофазного глотания остаётся (закреплено TestStopChunks).
    """

    def test_action_known_midstream_all_swallowed(self, voice_env,
                                                  monkeypatch, caplog):
        def decide(text, history, on_sentence=None, on_first_token=None,
                   on_query=None, on_action=None):
            if on_first_token is not None:
                on_first_token()
            if on_sentence is not None:
                on_sentence("Раз.")
                time.sleep(0.1)      # воркер успевает dequeue «Раз.»
                on_sentence("Два.")
                time.sleep(0.1)      # воркер успевает dequeue «Два.»
                on_action("none")    # факт action ПРИХОДИТ В СЕРЕДИНЕ
                time.sleep(0.1)
                on_sentence("Три.")  # после факта -- без latch звучала бы
                time.sleep(0.1)
            return llm.Decision(reply="Раз. Два. Три.", action="none",
                                args={})

        caplog.set_level(logging.INFO, logger="assistant")
        monkeypatch.setattr(llm, "decide_stream", decide)
        ws = _run_ws(monkeypatch)

        # latch: ВСЕ сентенции глотаны (в т.ч. «Три.» ПОСЛЕ факта),
        # озвучен только финальный reply -- как при цельном глотании
        assert _sentences(ws) == ["Раз. Два. Три."]
        assert len(_events(ws, "audio")) == 1
        assert voice_env == ["Раз. Два. Три."]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == "Раз. Два. Три."
        assert reply_ev["streamed"] is True
        assert "reply_replaced" not in reply_ev
        # latch сработал именно на unknown при первом dequeue
        assert any("latch mute" in r.getMessage() for r in caplog.records)


def _det_pcm(ms: float) -> bytearray:
    """Буфер длины ms мс для ПРЯМЫХ тестов детектора (rms подаётся в
    feed отдельно, содержимое неважно) -- аналог _pcm(ms) из
    tests/test_endpoint_detector.py; здесь имя _pcm занято кадром-
    амплитудой для FakeWS-стрима (см. выше)."""
    return bytearray(int(ms * voice_ws._WS_BYTES_PER_MS))


# те же величины, что в tests/test_endpoint_detector.py (значения
# заведомо выше/ниже порога голоса max(echo*2.5, _WS_VOICE_MIN=250))
VOICE_RMS = 600.0
QUIET_RMS = 40.0


class TestEndpointDetectorPrelude:
    """Режим follow-up детектора (флаг prelude=False, query ?prelude=0).

    Живой диагноз 22:01 (follow-up «который час» БЕЗ wake): вся фраза в
    прелюдии, голос кончился на записи 2400 (или 1700) -- ВСЕ голосовые
    кадры до grace 2500 -> armed=False -> text_end в not-armed ветке
    ЗАМОРОЖЕН -> тихотное правило не проверяется -> endpoint только по
    NO_VOICE-таймауту 5000 (M1 4756/4791 мс, ~2.6-3.3 с мёртвой тишины).
    Чинить влоб нельзя (armed «на любой голос» ломал бы прелюдию wake
    «Гемма…(пауза)…команда») -- поэтому телефон сообщает режим
    query-параметром, и при prelude=False:
      - ЛЮБОЙ голос взводит armed (гейт grace не нужен);
      - quiet-правило применяется БЕЗ гейта t >= grace;
      - text_end обновляется на каждом тихом кадре (С З8 -- 15.11:
        quiet-порог 900 сравнялся с сегментным _WS_SEG_QUIET_MS, а
        endpoint проверяется раньше -- сегментный stt на лету в
        follow-up не срабатывает, фразу целиком берёт финальный stt);
      - NO_VOICE-таймаут при голосе ВООБЩЕ сохранён (тихая запись ->
        «Не расслышал»).
    prelude=True (дефолт) -- байт-в-байт прежняя семантика (покрыта
    test_endpoint_detector.py: голос в grace не взводит armed, endpoint
    только по NO_VOICE).
    """

    def test_followup_phrase_in_prelude_endpoint_by_quiet(self):
        # (а) голос кончился на 2400 (целиком в прелюдии, до grace) ->
        # endpoint на 2400+900=3300 (З8, 15.11; до него 2400+1300=3700),
        # НЕ на NO_VOICE 5000
        det = EndpointDetector(prelude=False)
        act = det.feed(_det_pcm(2400), rms=VOICE_RMS, t_ms=2400.0,
                       stt_busy=False)
        assert act.is_voice and det.armed   # ЛЮБОЙ голос взводит armed
        t1 = 2400.0 + _WS_QUIET_MS - 1
        act = det.feed(_det_pcm(t1), rms=QUIET_RMS, t_ms=t1,
                       stt_busy=False)
        assert not act.endpoint             # тишина 899 < 900
        t2 = 2400.0 + _WS_QUIET_MS
        act = det.feed(_det_pcm(t2), rms=QUIET_RMS, t_ms=t2,
                       stt_busy=False)
        assert act.endpoint and act.segment is None
        assert det.endpointed and det.endpoint_ms == t2

    def test_followup_very_short_phrase_endpoint(self):
        # (б) совсем короткая «да»: голос кончился на 500 -> endpoint на
        # 500+900=1400 (З8, 15.11; до него 500+1300=1800)
        det = EndpointDetector(prelude=False)
        act = det.feed(_det_pcm(500), rms=VOICE_RMS, t_ms=500.0,
                       stt_busy=False)
        assert act.is_voice and det.armed
        t1 = 500.0 + _WS_QUIET_MS - 1
        act = det.feed(_det_pcm(t1), rms=QUIET_RMS, t_ms=t1,
                       stt_busy=False)
        assert not act.endpoint             # тишина 899 < 900
        t2 = 500.0 + _WS_QUIET_MS
        act = det.feed(_det_pcm(t2), rms=QUIET_RMS, t_ms=t2,
                       stt_busy=False)
        assert act.endpoint and det.endpoint_ms == t2

    def test_followup_no_voice_at_all_times_out(self):
        # (в) голоса не было вообще -> endpoint по NO_VOICE-таймауту
        # (тихая запись -> финальный stt пуст -> «Не расслышал»)
        det = EndpointDetector(prelude=False)
        act = det.feed(_det_pcm(4999), rms=QUIET_RMS, t_ms=4999.0,
                       stt_busy=False)
        assert not act.endpoint and not det.armed
        act = det.feed(_det_pcm(_WS_NO_VOICE_MS), rms=QUIET_RMS,
                       t_ms=float(_WS_NO_VOICE_MS), stt_busy=False)
        assert act.endpoint
        assert det.endpoint_ms == float(_WS_NO_VOICE_MS)

    def test_followup_quiet_closes_not_segment(self):
        # (д) С З8 (15.11) _WS_QUIET_MS == _WS_SEG_QUIET_MS (900), а
        # endpoint-правило проверяется РАНЬШЕ сегментного: на тишине
        # >= 900 мс в follow-up приём ЗАКРЫВАЕТСЯ (endpoint), сегмент не
        # срезается -- фразу целиком возьмёт финальный stt
        det = EndpointDetector(prelude=False)
        det.feed(_det_pcm(2400), rms=VOICE_RMS, t_ms=2400.0, stt_busy=False)
        t = 2400.0 + _WS_QUIET_MS   # 3300: тишина ровно 900
        act = det.feed(_det_pcm(t), rms=QUIET_RMS, t_ms=t, stt_busy=False)
        assert act.endpoint and act.segment is None
        assert det.endpointed and det.endpoint_ms == t

    def test_default_mode_is_wake(self):
        # (г) дефолт -- prelude=True (wake-режим, семантика байт-в-байт
        # прежняя; поведенческое покрытие -- tests/test_endpoint_detector.py)
        assert EndpointDetector().prelude is True
