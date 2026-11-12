"""Задачи 14+16: гейт озвучки итогового reply в /ws_voice.

Эволюция гейта:
- задача 14: dispatch мутирует decision ПОСЛЕ стриминга -- модель уже
  отстримала оптимистичный reply (on_sentence -> sentence+audio,
  chunks_sent>0), а фолбэк-уточнение «Нет активного списка...» при
  needs_confirm=false не досинтезировалось: телефон при streamed=true
  финальный reply НЕ повторяет -> пользователь слышит ложное «Добавляю...»
  и не слышит вопрос. Решение: answer.py отдаёт сигнал reply_replaced
  (итоговый reply != отстримленной реплике модели), voice_ws досинтезирует
  итог ТЕМ ЖЕ механизмом, что confirm-ветка.
- задача 16 (phone.log 23:00:58-23:01:08, «двойная озвучка»): mid-stream
  сентенции модели («Ага, создал!», «Готово, держи!») озвучивались в
  TTS ДО готовности JSON-решения, а потом досылался шаблон dispatch
  («Список «Вкусняшки» не найден») -- пользователь слышал противоречивые
  обе. Решение СТРУКТУРНОЕ: mid-stream предложения в TTS-канал НЕ идут
  вовсе (on_sentence в /ws_voice -- no-op, decide_stream остаётся
  потоковым ради first_token и спекулятивного prefetch поиска);
  озвучивается РОВНО ОДНА реплика -- финальный reply, всегда ПОСЛЕ
  решения: sentence + audio, затем reply(streamed=true) + done.

Инвариант порядка (обе задачи): sentence+audio РАНЬШЕ reply-события --
телефон (VoiceStream.kt) откладывает reply до done, а аудио-чанки до
reply проигрывает сразу. Живой прогон 09.11 (S6): sentence t=+15653 ->
audio (2975 мс) t=+15664 -> reply(confirm=true) t=+15665.

Тесты гоняют НАСТОЯЩИЙ ws_voice через FakeWS с реальным PCM: сначала
3 с громкого голоса (после grace 2500 мс включается armed), потом 1.5 с
тишины (endpoint при quiet 1300 мс). Пороги _WS_* не подменяются и не
меняются (§7): кадры дают запас, endpoint срабатывает детерминированно,
сегментный stt на паузе даёт распознанный текст.
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct

import pytest

import answer as answer_mod
import llm
import shopping_store
import stt as stt_mod
import tts as tts_mod
import voice_ws
from app_state import state
from config import settings
from dispatch import _Q_NOT_FOUND, _Q_NO_ACTIVE

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


def _events(ws: StreamWS, name: str) -> list[dict]:
    return [e for e in ws.sent if e.get("event") == name]


def _sentences(ws: StreamWS) -> list[str]:
    return [e["text"] for e in _events(ws, "sentence")]


def _fake_stream(reply: str, action: str = "none", args: dict | None = None,
                 sentence: str | list[str] | None = None):
    """Подмена llm.decide_stream: стримит предложение(я), отдаёт решение.

    sentence -- одна строка ИЛИ список (mid-stream chatter для теста
    «двойной озвучки», задача 16).
    """
    items = ([sentence] if isinstance(sentence, str)
             else list(sentence or []))

    def decide(text, history, on_sentence=None, on_first_token=None,
               on_query=None):
        if on_first_token is not None:
            on_first_token()
        if on_sentence is not None:
            for s in items:
                on_sentence(s)
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

    Задача 16: mid-stream предложения модели НЕ озвучиваются вовсе,
    стрим молчит; отправляется только решённая реплика.
    """

    def test_s8_no_active_list_question_voiced(self, voice_env, monkeypatch):
        # модель отстримала ложное «Добавляю...» (В TTS НЕ ушло), хранилище
        # пусто -> _fallback: action=none, reply=уточнение (S8)
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
        # action=none без замены: финальный reply (модельный) --
        # единственная озвучка: sentence == reply, audio == 1
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=PLAIN_REPLY, action="none", sentence="Привет,"))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == [PLAIN_REPLY]       # только финальная реплика
        assert len(_events(ws, "audio")) == 1        # один синтез на сессию
        assert voice_env == [PLAIN_REPLY]
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == PLAIN_REPLY
        assert reply_ev["streamed"] is True
        assert reply_ev["confirm"] is False

    def test_confirm_branch_intact(self, voice_env, monkeypatch):
        # needs_confirm=true: вопрос-подтверждение озвучен, модельный стрим
        # молчит (раньше вопрос ДОсинтезировался ПОСЛЕ озвученного стрима)
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
    sentence+audio финальной реплики РАНЬШЕ reply -- иначе реплика
    не прозвучит. Живой прогон 09.11 (S6, ws_voice): sentence t=+15653
    -> audio (2975 мс) t=+15664 -> reply(confirm=true) t=+15665.
    Инвариант закреплён FakeWS-тестами, чтобы регрессия поймалась
    в pytest. После задачи 16 перед reply ровно ДВА события
    (sentence+audio финала) -- стрим-болтовня больше не уходит.
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
        # никаких других стрим-событий в сессии нет (задача 16)
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


class TestSingleVoiceStage:
    """Задача 16, регрессия «двойной озвучки» (phone.log 23:00:58-01:08).

    Раньше: модель стримила «Ага, создал!» и «Готово, держи!» ->
    sentence+audio ДО решения, затем dispatch отвечал «Список не
    найден» -> пользователь слышал противоречивые ОБЕ реплики.
    Теперь: болтовня модели НЕ озвучивается вовсе; единственный
    озвученный текст == финальный reply.
    """

    CHATTER = ["Ага, создал!",            # как в живом user-smoke 09.11
               "Вот сейчас добавляю печенье и варенье.",
               "Готово, держи!"]

    def test_shopping_chatter_not_voiced(self, voice_env, monkeypatch):
        # точный сценарий user-smoke: shopping_read с несуществующим
        # именем -> фолбэк «не найден»; стрим-болтовня молчит
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

    def test_action_none_chatter_not_voiced(self, voice_env, monkeypatch):
        # даже без замены (reply_replaced=False): модель выдала другой
        # текст в стриме -> всё равно озвучивается только финальный reply
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=PLAIN_REPLY, action="none", sentence=self.CHATTER))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == [PLAIN_REPLY]
        assert len(_events(ws, "audio")) == 1
        assert voice_env == [PLAIN_REPLY]

    def test_tts_failure_sends_bare_reply(self, voice_env, monkeypatch):
        # краевой случай гейта задачи 16 (voice_ws.py:453-455,474):
        # TTS упал -> wav пуст -> sentence/audio НЕ уходят, а reply-событие
        # несёт streamed=false: телефон (AssistantService.speak()) при
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
