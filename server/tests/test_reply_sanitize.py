"""Задача 21: санитайзер профанизма финального reply (reply_sanitize).

Юнит: закрытый стем-лист (бля/бляд-, хуй-формы, еб-/ёб-, пизд-, манд-,
хер-), кириллическая граница слова (?<![а-яёА-ЯЁ]), case-insensitive,
удаление + чистка провисшей пунктуации («, блядь.» -> «.»);
идемпотентность; живой регресс (server.log 10.11: «...Назови товары
и количество, блядь.»). Ложно-позитивы НЕ задеваются: хулиган/
инженер/посёлок/эбонит/надрываться + защитные по дизайну стем-листа:
мандат, мандарин, херувим, бляха, небольшой, блажь.

Интеграция: ЕДИНСТВЕННАЯ точка применения -- answer._answer (общий
путь REST /ask+/voice, история и /ws_voice: voice_ws берёт оттуда
resp.reply для TTS и всех событий) -- sentence/audio чисты, история
state чистая. ws-тест гоняет НАСТОЯЩИЙ ws_voice по образцу
test_voice_fallback_audio (подмена llm.decide_stream, реальный PCM).
"""
from __future__ import annotations

import asyncio
import json
import struct

import pytest
from fastapi.testclient import TestClient

import answer as answer_mod
import dialog_state
import llm
import server
import shopping_store
import stt as stt_mod
import tts as tts_mod
import voice_ws
from app_state import state
from config import settings
from dispatch import _new_pending
from reply_sanitize import sanitize_reply

# Живой факт (server.log 10.11 19:35) и ожидаемый чистый текст.
LIVE_REGRESSION = ("Готов создать список! Что купить? Назови товары "
                   "и количество, блядь.")
LIVE_EXPECTED = "Готов создать список! Что купить? Назови товары и количество."


class TestStemsRemoved:
    """Матёрные слова удаляются целиком, пунктуация подчищается."""

    @pytest.mark.parametrize("dirty, clean", [
        ("бля, поехали.", "поехали."),
        ("БЛЯДЬ, забыл про яйца.", "забыл про яйца."),      # case-insensitive
        ("Готово, бля, ты справился.", "Готово, ты справился."),
        ("он там хуесос конченый!", "он там конченый!"),   # хвост слова
        ("хуйня какая-то", "какая-то"),
        ("и херня тут получается", "и тут получается"),    # хер + хвост
        ("Ёб твою мать, опять мороз.", "твою мать, опять мороз."),
        ("ПИЗДЕЦ как холодно", "как холодно"),
        ("да и манда какая-то", "да и какая-то"),           # манд- падеж
        ("БЛЯЯЯ, поехали.", "поехали."),                    # растяжка caps
        ("ну бляаа и хватит", "ну и хватит"),               # растяжка «а»
        ("Список «бляю» создан.", "Список «» создан."),     # слово вырезано
    ])
    def test_stem_removed_and_tidy(self, dirty, clean):
        assert sanitize_reply(dirty) == clean

    def test_no_stem_in_clean_text(self):
        # без матов текст 1:1 (пунктуация/склонения не ломаются)
        text = "Привет, я тут, слушаю тебя."
        assert sanitize_reply(text) == text


class TestFalsePositivesKept:
    """Закреплённый стоп-лист + защитные по дизайну слова."""

    @pytest.mark.parametrize("word", [
        "хулиган", "инженер", "посёлок", "эбонит", "надрываться",
        # ниже -- защита, заложенная в стем-лист (см. reply_sanitize):
        "мандат", "мандарин", "херувим", "бляха", "блашка", "благовест",
        "небольшой", "блажь",
    ])
    def test_word_not_replaced(self, word):
        sentence = f"Это просто {word}, самое обычное слово здесь."
        assert sanitize_reply(sentence) == sentence


class TestIdempotentAndRegression:
    """Идемпотентность + живой регресс (факт из server.log)."""

    @pytest.mark.parametrize("text", [
        LIVE_REGRESSION,
        "бля, поехали.",
        "он там хуесос конченый!",
        "Привет, я тут, слушаю тебя.",       # чистый текст 1:1
    ], ids=["live", "interjection", "tail", "already_clean"])
    def test_second_pass_is_noop(self, text):
        once = sanitize_reply(text)
        assert sanitize_reply(once) == once

    def test_live_regression(self):
        assert sanitize_reply(LIVE_REGRESSION) == LIVE_EXPECTED


# --------------------------------------------------------------------------
# Интеграция: единственная точка в answer._answer (REST + история + ws).
# ws-харнес -- мини-копия образца из test_voice_fallback_audio.py
# (FakeWS + реальный PCM до endpoint; thresholds _WS_* не меняются).
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
    """Подмена llm.decide_stream: стримит предложение(я), отдаёт решение."""
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Изоляция state: tmp-стор списков, свежий лок, пустые история/pending."""
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(shopping_store, "_SHOPPING_FILE",
                        tmp_path / "shopping_lists.json")
    monkeypatch.setattr(state, "shopping_lock", asyncio.Lock())
    monkeypatch.setattr(state, "active_list_id", None)
    state.history.clear()
    state.history_stamp = 0.0
    state.pending.clear()
    yield
    state.history.clear()


@pytest.fixture()
def voice_env(tmp_path, monkeypatch):
    """ws-вариант env: LAN + подменённые stt/tts (образец voice_env)."""
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
                        lambda pcm: "создай список покупок")
    monkeypatch.setattr(llm, "get_profile_voice", lambda: "test-voice")
    yield tts_calls
    state.history.clear()


class TestSanitizeIntegration:
    """Мат в reply МОДЕЛИ не доходит ни до одного выходного канала."""

    def test_rest_reply_and_history_clean(self, env, monkeypatch):
        monkeypatch.setattr(llm, "decide", lambda text, history: llm.Decision(
            reply=LIVE_REGRESSION, action="none", args={}))
        resp = asyncio.run(answer_mod._answer("так, давай новый список"))

        assert resp.reply == LIVE_EXPECTED          # REST-ответ чист
        assert resp.needs_confirm is False
        last = json.loads(state.history[-1]["content"])
        assert last == {"reply": LIVE_EXPECTED, "action": "none",
                        "args": {}}                 # история чиста

    def test_ws_sentence_audio_and_history_clean(self, voice_env,
                                                 monkeypatch):
        # модель стримит болтовню и мат в финальном reply: гейт задачи 16
        # болтовню глотает, единственная озвучка -- САНИТИЗИРОВАННЫЙ reply
        monkeypatch.setattr(llm, "decide_stream", _fake_stream(
            reply=LIVE_REGRESSION, action="none", args={},
            sentence="Бля, готовлюсь создать список."))
        ws = _run_ws(monkeypatch)

        assert _sentences(ws) == [LIVE_EXPECTED]    # единственный текст
        assert len(_events(ws, "audio")) == 1
        reply_ev = _events(ws, "reply")[0]
        assert reply_ev["reply"] == LIVE_EXPECTED
        assert reply_ev["streamed"] is True
        assert voice_env == [LIVE_EXPECTED]         # TTS получил ЧИСТЫЙ
        last = json.loads(state.history[-1]["content"])
        assert last["reply"] == LIVE_EXPECTED       # история чиста


class TestConfirmSanitizeIntegration:
    """Фоллоу-ап ревью: POST /confirm -- ЯВНЫЕ точки санитайзера.

    _answer тут не участвует: /confirm собирает reply сам (server.py),
    поэтому каждая ветка санитайзит реплику ЯВНЫМ вызовом. Здесь --
    модельная ветка (yes=false, search): ответ llm.answer_from_memory
    uncensored-модели, уходящий и в ConfirmResponse, и в add_turn.
    """

    def test_confirm_model_reply_and_history_clean(self, env, monkeypatch):
        monkeypatch.setattr(
            llm, "answer_from_memory", lambda q: LIVE_REGRESSION)
        dialog_state._pending.clear()
        token = _new_pending("search", {"query": "погода на завтра"},
                             "Искать в интернете? Скажи «да».")
        client = TestClient(server.app, raise_server_exceptions=False)
        resp = client.post("/confirm", json={"token": token, "yes": False})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["reply"] == LIVE_EXPECTED          # реплика чиста
        last = state.history[-1]["content"]
        assert last == LIVE_EXPECTED                   # история чиста
        assert token not in dialog_state._pending      # токен потреблён
