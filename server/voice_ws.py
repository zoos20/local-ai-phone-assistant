import asyncio
import base64
import json
import logging
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass

from fastapi import APIRouter, WebSocket

import llm
from answer import _answer
from config import settings
from search import _prefetch_search

log = logging.getLogger("assistant")

router = APIRouter()


# --------------------------------------------------------------------------
# /ws_voice -- потоковый конвейер (фаза 6, п. 1). «Самая крупная правка».
#
# Телефон стримит PCM (s16le, mono, 16 кГц) во время речи, НЕ дожидаясь
# конца (убираем мёртвые QUIET_END_MS=3 с на телефоне):
#   клиент -> сервер:  бинарные фреймы-чанки PCM, затем {"event":"end"};
#   сервер -> клиент:  {"event":"ready"}            -- подключились;
#                      {"event":"endpoint"}         -- тишина, закрывай
#                                                      запись (досылай
#                                                      хвост и шли end);
#                      {"event":"hear","text":...}  -- сегмент распознан
#                                                      НА ЛЕТУ (stt идёт
#                                                      параллельно речи);
#                      {"event":"sentence","text":..}-- финальная реплика
#                                                      (задача 16: mid-stream
#                                                      предложения модели
#                                                      НЕ озвучиваются);
#                      {"event":"audio","b64":...}  -- WAV финальной реплики
#                                                      (sentence+audio всегда
#                                                      РАНЬШЕ reply-event);
#                      {"event":"reply","reply":...,"streamed":bool}
#                                                     -- итог: reply целиком
#                                                      + confirm/token;
#                                                      streamed=True -- чанки
#                                                      уже играли, НЕ
#                                                      озвучивать повторно;
#                      {"event":"done"}             -- всё.
# Endpointing -- на сервере: адаптивный порог (фон ×2.5, как на телефоне)
# + тишина >= _WS_QUIET_MS после голоса. Сегментный stt на лету
# параллелит распознавание с речью: после конца фразы whisper'у остаётся
# доспознать только хвост.
# --------------------------------------------------------------------------
WS_RATE = 16000                                   # PCM от телефона: 16 кГц
_WS_BYTES_PER_MS = WS_RATE * 2 // 1000           # 32 байта/мс (s16le mono)
_WS_FRAME = _WS_BYTES_PER_MS * 100               # RMS-кадр: 100 мс
_WS_QUIET_MS = 1300          # тишина после голоса -> endpoint (было 3000
                             # на телефоне; хвост досылается клиентом).
                             # 1600->1300 (фаза 6, п. 4): сокращаем
                             # мёртвую паузу перед ответом; если «не
                             # дослушивает» -- вернуть 1600.
_WS_GRACE_MS = 2500          # первые мс не проверяем: хвост «Геммы»
                             # (prelude) + пауза до команды
_WS_SEG_QUIET_MS = 900       # тишина -> речевой сегмент закрыт (stt на лету)
_WS_SEG_MIN_MS = 600         # сегменсы короче не распознаём отдельно
# Голос: rms > фон*2.5 И > _WS_VOICE_MIN. Замер 29.10 на живых записях:
# фон стабильно 100-130, речь p25=170-580, p50=290-730; кадры >400 --
# лишь 12-21% (починка «endpoint=5000 во всех сессиях»: при старом пороге
# 500*2.5=1250 речь НЕ проходила никогда -> no_voice таймаут 5 с).
_WS_VOICE_MIN = 250.0
_WS_ECHO_START = 120.0       # стартовый фон = реальный фон комнаты (120)
_WS_ECHO_DECAY = 0.97        # адаптивный фон: экспоненциальное скольжение
_WS_END_TAIL_S = 6.0         # ждём «end»/хвост от клиента после endpoint
_WS_NO_VOICE_MS = 5000       # голоса не было совсем -> endpoint (иначе
                             # телефон висит в записи до своего лимита)
# Лимиты ресурсов (план ревью A1): защита от «вечной» заливки PCM,
# простоя подключившегося клиента и параллельных сессий (стримы делят
# CPU whisper'а, больше двух убивают отзывчивость).
_WS_MAX_PCM_S = 90           # максимум аудио на одну сессию, секунд
_WS_MAX_PCM_BYTES = WS_RATE * 2 * _WS_MAX_PCM_S
_WS_RECV_IDLE_S = 45.0       # прость клиента в основном цикле -> обрыв
_WS_MAX_SESSIONS = 2         # одновременных голосовых сессий
_WS_ACTIVE = 0               # текущее число сессий (счётчик ниже)


# --------------------------------------------------------------------------
# Endpointing-автомат (п. 2.7 рефакторинга): изменяемое состояние приёма
# (адаптивный фон echo, armed/last_voice/text_end, позиция сегмента
# seg_start, финал endpointed/endpoint_ms) вынесено из ws_voice в класс с
# чистым состоянием -- одна точка и точка для тестов. ws_voice лишь подаёт
# кадр и исполняет решения (endpoint -> отправить событие и разорвать
# приём; segment -> запустить фоновый stt). ПОРОГИ _WS_* выше НЕ трогаются
# (читаются как есть), поведение идентично прежнему инлайн-коду.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FrameAction:
    """Решение автомата по одному RMS-кадру (исполняет ws_voice)."""

    is_voice: bool = False
    endpoint: bool = False         # закрывать приём (событие endpoint)
    segment: bytes | None = None   # срезанный речевой сегмент -> фоновый stt


class EndpointDetector:
    """Серверный endpointing + сегментирование по тишине (конвейер ws_voice).

    Состояние сессии (чистое, без I/O -- тестируется напрямую):
        echo      -- адаптивный уровень фона (экспоненциальное скольжение);
        armed     -- уже был голос ПОСЛЕ grace (можно закрывать по тишине);
        last_voice, text_end -- конец последнего голоса и длина тишины;
        seg_start -- начало нераспознанного речевого сегмента (в байтах);
        endpointed / endpoint_ms -- финал автомата.
    """

    def __init__(self) -> None:
        self.echo: float = _WS_ECHO_START   # скользящий уровень фона
        self.armed: bool = False            # уже был голос (после grace)
        self.last_voice: int = 0            # конец последнего голоса (байты)
        self.text_end: float = 0.0          # мс тишины после последнего голоса
        self.seg_start: int = 0             # начало сегмента (байты)
        self.endpointed: bool = False
        self.endpoint_ms: float = 0.0

    def feed(self, pcm: bytes | bytearray, *, rms: float, t_ms: float,
             stt_busy: bool) -> FrameAction:
        """Один RMS-кадр -> решение (голос / endpoint / срез сегмента).

        pcm, rms, t_ms -- те же величины, что считал ws_voice (rms --
        последний 100-мс кадр, t_ms -- позиция в записи); stt_busy --
        не запущен ли уже фоновый сегмент (два параллельных инференса
        делили бы CPU whisper'а).
        """
        if self.endpointed:
            # приём уже разорван (ws_voice выходит из цикла сразу после
            # endpoint) -- кадр больше не кормим
            return FrameAction()
        is_voice = rms > max(self.echo * 2.5, _WS_VOICE_MIN)
        if is_voice:
            # фон НЕ обновляем голосом (иначе за 2-3 с речи порог
            # echo*2.5 догонит сам голос и armed залипнет -- фикс
            # живого теста 29.10)
            if t_ms >= _WS_GRACE_MS:
                # живой голос ПОСЛЕ prelude -> можно закрывать по тишине
                # (голос из prelude не включает armed: иначе тишина
                # «между Геммой и командой» дала бы endpoint до начала
                # команды)
                self.armed = True
            self.last_voice = len(pcm)
            self.text_end = 0.0
            return FrameAction(is_voice=True)

        # адаптируемся только к тишине/фону
        self.echo = self.echo * _WS_ECHO_DECAY + rms * (1 - _WS_ECHO_DECAY)
        if not self.armed:
            if t_ms >= _WS_NO_VOICE_MS:
                # голоса не было ВООБЩЕ (тишина/шум: armed не включился)
                # -> endpoint по таймауту, иначе телефон висит в записи
                # до своего локального лимита. stt финала скажет пусто ->
                # «Не расслышал».
                self.endpointed = True
                self.endpoint_ms = t_ms
                return FrameAction(endpoint=True)
            return FrameAction()

        self.text_end = t_ms - self.last_voice / _WS_BYTES_PER_MS
        # endpoint: тишина >= порога после голоса (и только
        # после grace: хвост «Геммы» prelude не должен рвать)
        if self.text_end >= _WS_QUIET_MS and t_ms >= _WS_GRACE_MS:
            self.endpointed = True
            self.endpoint_ms = t_ms
            # сегмент в этот кадр не срезаем (как и раньше: break до
            # проверки сегмента) -- его целиком возьмёт финальный stt
            return FrameAction(endpoint=True)
        # ранний сегментный stt: пауза в речи -> сегмент закрыт.
        # ВАЖНО (фикс живого прогона 29.10): cut НЕ по last_voice
        # (тихий конец фразы не проходит адаптивный порог ->
        # «нахуй, Дима» терялось), а по ТЕКУЩЕЙ позиции: в кусок
        # попадает уже накопленная тишина (~0.9 с), её VAD
        # отфильтрует, а речь гарантированно целиком.
        if (not stt_busy and self.text_end >= _WS_SEG_QUIET_MS
                and (len(pcm) - self.seg_start)
                >= _WS_SEG_MIN_MS * _WS_BYTES_PER_MS):
            pkt = bytes(pcm[self.seg_start:len(pcm)])
            self.seg_start = len(pcm)
            return FrameAction(segment=pkt)
        return FrameAction()


@router.websocket("/ws_voice")
async def ws_voice(ws: WebSocket) -> None:
    """Стриминговый голосовой конвейер (см. комментарий выше секцией)."""
    global _WS_ACTIVE
    # Аутентификация ДО accept (план ревью A1): при несовпадении close
    # без accept -> клиент получает отказ на handshake (HTTP 403).
    # Пустой api_token = режим «только LAN» (как в HTTP-мидлваре).
    # Токен в query-параметре: клиент/туннель шлют его так.
    tok = settings.api_token
    if tok:
        got = ws.query_params.get("token", "")
        # compare_digest на str падает TypeError на не-ASCII токене
        if not secrets.compare_digest(got.encode(), tok.encode()):
            log.warning("ws_voice: rejected (bad token)")
            await ws.close(code=1008)
            return

    import numpy as np

    import stt
    import tts as tts_mod

    # Лимит одновременных сессий: сверх _WS_MAX_SESSIONS не берём.
    if _WS_ACTIVE >= _WS_MAX_SESSIONS:
        log.warning("ws_voice: rejected (active sessions=%d)", _WS_ACTIVE)
        await ws.close(code=1013)
        return
    _WS_ACTIVE += 1

    # accept/send -- вне общего try ниже: падение здесь (клиент умер на
    # handshake) не должно утекать слот сессии, иначе после
    # _WS_MAX_SESSIONS таких утечек сервер перестанет брать новые
    # голосовые сессии до рестарта. Синхронная инициализация ниже
    # (t0/pcm/...) исключений не бросает.
    try:
        await ws.accept()
        await ws.send_json({"event": "ready"})
    except Exception:
        _WS_ACTIVE = max(0, _WS_ACTIVE - 1)
        log.warning("ws_voice: accept/ready failed", exc_info=True)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 -- handshake мог уже умереть
            log.debug("ws_voice: close after failed accept")
        return
    t0 = time.perf_counter()
    pcm = bytearray()
    # endpointing-автомат (п. 2.7): echo/armed/last_voice/text_end/seg_start
    # и endpointed/endpoint_ms живут в EndpointDetector (чистое состояние,
    # пороги _WS_* не трогаем); здесь кадры только подаются и решения
    # исполняются.
    det = EndpointDetector()
    stt_busy = False
    # Событие «фоновый сегмент дораспознан» (п. 2.1 рефакторинга): ждём
    # конца _seg_job через await ev.wait() вместо busy-wait
    # «while stt_busy: await asyncio.sleep(0.05)» (каждые 50 мс просыпал
    # event loop впустую). Ссылка живёт в том же scope, что и stt_busy.
    # Изначально выставлено: фоновых сегментов нет -- ждать нечего.
    stt_idle = asyncio.Event()
    stt_idle.set()
    # Ссылка на текущую задачу сегмента (п. 2.2): без неё задача утекала
    # -- исключение невозможно было диагностировать («exception never
    # retrieved») и нельзя было погасить при закрытии сессии.
    seg_task: asyncio.Task | None = None
    heard: list[str] = []     # сегменты, распознанные на лету

    def now_ms() -> float:
        return len(pcm) / _WS_BYTES_PER_MS

    def _text_event(raw: str) -> str:
        """{'event': 'end'|'abort'} из текстового фрейма (ошибка -> end)."""
        try:
            return str(json.loads(raw).get("event", "end"))
        except Exception:  # noqa: BLE001
            return "end"

    async def _seg_job(pkt: bytes) -> None:
        """Распознавание завершённого сегмента в фоне (не мешает приёму)."""
        nonlocal stt_busy
        try:
            t = await asyncio.to_thread(stt.transcribe_pcm, pkt)
            if t:
                heard.append(t)
                await ws.send_json(
                    {"event": "hear", "text": t,
                     "at_ms": int((time.perf_counter() - t0) * 1000)})
        except Exception:
            log.warning("ws_voice: seg stt failed", exc_info=True)
        finally:
            # стоп-сигнал для await stt_idle.wait() (п. 2.1): сбрасывается
            # ТОЛЬКО здесь, рядом с stt_busy = False
            stt_busy = False
            stt_idle.set()

    try:
        # ---------------- приём стрима + endpointing ----------------
        client_ended = False   # клиент уже прислал end -- хвоста не будет
        limit_hit = False      # перебор по аудио -> приём прерван
        while not det.endpointed:
            # прость клиента (подключился и молчит) -> обрыв, не висим
            try:
                msg = await asyncio.wait_for(ws.receive(),
                                             timeout=_WS_RECV_IDLE_S)
            except asyncio.TimeoutError:
                log.warning("ws_voice: idle timeout (%.0fs)",
                            _WS_RECV_IDLE_S)
                break
            if msg["type"] == "websocket.disconnect":
                return
            chunk = msg.get("bytes")
            if not chunk:
                # текстовый фрейм: {"event":"end"} -- хватит слушать;
                # {"event":"abort"} -- стоп-слово на телефоне: молчим
                if msg.get("text"):
                    if _text_event(msg["text"]) == "abort":
                        log.info("ws_voice: aborted by client")
                        return
                    client_ended = True
                    break
                continue
            pcm += chunk
            if len(pcm) > _WS_MAX_PCM_BYTES:
                # лимит записи исчерпан: рвём приём (хвост/ответ не идут)
                limit_hit = True
                log.warning("ws_voice: audio limit exceeded (%d bytes)",
                            len(pcm))
                await ws.send_json({"event": "error",
                                    "detail": "audio limit exceeded"})
                break
            # RMS последнего 100-мс кадра
            frame = bytes(pcm[-_WS_FRAME:])
            if len(frame) < 64:
                continue
            arr = np.frombuffer(frame, dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean(arr * arr)))
            t_ms = now_ms()
            # решение по кадру принимает EndpointDetector (п. 2.7): echo/
            # armed/last_voice/seg_start и пороги _WS_* живут там, здесь --
            # только ИСПОЛНЕНИЕ решения (endpoint -> событие и break;
            # segment -> фоновый stt)
            act = det.feed(pcm, rms=rms, t_ms=t_ms, stt_busy=stt_busy)
            if act.endpoint:
                await ws.send_json({"event": "endpoint",
                                    "at_ms": int(det.endpoint_ms)})
                break
            if act.segment is not None:
                stt_busy = True
                stt_idle.clear()   # пока сегмент не готов
                # ссылку ДЕРЖИМ (п. 2.2): иначе GC может собрать
                # задачу до конца и «exception never retrieved»
                # станет недиагностируемым
                seg_task = asyncio.ensure_future(_seg_job(act.segment))

        if limit_hit:
            return

        # ---------------- хвост от клиента (после endpoint) ----------
        # клиент досылает последние ~1-2 с записи и шлёт {"event":"end"}.
        # Если end уже пришёл в первом цикле (локальный quiet на телефоне
        # обогнал endpoint) -- хвоста не будет, ждать 6 с впустую нельзя.
        deadline = time.monotonic() + _WS_END_TAIL_S
        while not client_ended and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await asyncio.wait_for(ws.receive(),
                                             timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001 -- обрыв клиента
                break
            if msg["type"] == "websocket.disconnect":
                break
            chunk = msg.get("bytes")
            if chunk:
                pcm += chunk
                continue
            if msg.get("text"):
                if _text_event(msg["text"]) == "abort":
                    log.info("ws_voice: aborted by client")
                    return
                break  # {"event":"end"}

        # ---------------- финальный stt + ответ ----------------------
        # сначала ДОЖИДАЕМСЯ фоновых сегментов: два параллельных
        # инференса на одной модели делят CPU (живой прогон 29.10:
        # оба ~6 с вместо ~2.5) и мешают друг другу. Событие ставит
        # finally _seg_job (п. 2.1) -- без busy-wait.
        await stt_idle.wait()
        tail = bytes(pcm[det.seg_start:])
        tail_text = ""
        if len(tail) >= 3200:
            # хвост после endpoint -- обычно ЧИСТАЯ ТИШИНА (клиент досылает
            # последние ~1-2 с): whisper на тишине галлюцинирует («Как
            # видимо.» в прогоне 29.10). Распознаём только если в хвосте
            # реально был голос (дослал ещё слово после endpoint).
            arr = np.frombuffer(tail, dtype="<i2").astype(np.float32)
            max_rms = float(np.max(np.abs(arr))) if len(arr) else 0.0
            if max_rms >= _WS_VOICE_MIN:
                t_stt = time.perf_counter()
                tail_text = await asyncio.to_thread(stt.transcribe_pcm, tail)
                stt_ms = int((time.perf_counter() - t_stt) * 1000)
            else:
                stt_ms = 0
                log.info("ws_voice: tail is quiet (%d ms rms<%.0f) -- "
                         "skip stt", int(len(tail) / _WS_BYTES_PER_MS),
                         _WS_VOICE_MIN)
        else:
            stt_ms = 0
        if tail_text:
            heard.append(tail_text)
        text = " ".join(heard).strip()
        total_ms = int((time.perf_counter() - t0) * 1000)
        log.info("ws_voice: endpoint=%d ms, stt_tail=%d ms, total=%d ms, "
                 "heard=%r -> %r", int(det.endpoint_ms), stt_ms, total_ms,
                 " ".join(heard), text)

        if not text:
            # Единообразие схемы: все reply-события несут confirm/streamed
            # (телефон дефолтит, но явные false -- документация факта:
            # озвучки не было, финальную фразу скажет локальный TTS).
            await ws.send_json(
                {"event": "reply",
                 "reply": "Не расслышал, повтори, пожалуйста.",
                 "confirm": False, "streamed": False})
            await ws.send_json({"event": "done"})
            return

        # ---------------- ответ (фаза 6, п. 1; гейт задачи 16) --------
        # Модель стримит предложения через on_sentence ПОКА решение ещё
        # не готово: живьём телефон слышал «болтовню» модели («Ага,
        # создал! ... Готово, держи!»), а после решения dispatch досыпал
        # шаблон-фолбэк («Список «Вкусняшки» не найден») -- двойная
        # озвучка с противоречивым текстом (phone.log 23:00:58-23:01:08).
        # Структурный гейт (задача 16): стрим ГЛОТАЕТСЯ -- on_sentence
        # остаётся (иначе answer ушёл бы в блокирующий decide() без
        # first_token и спекулятивного prefetch поиска), но в TTS эти
        # предложения НЕ идут. Озвучивается РОВНО ОДНА реплика --
        # финальный reply (шаблон dispatch / вопрос-подтверждение /
        # ответ модели), всегда ПОСЛЕ готовности решения.
        loop = asyncio.get_running_loop()
        t_ask = time.perf_counter()
        first_ms: list[int] = []
        tts_first_ms: list[int] = []

        def _on_sentence(_s: str) -> None:
            pass  # pre-decision chatter в TTS-канал не уходит (задача 16)

        def _on_first_token() -> None:
            first_ms.append(int((time.perf_counter() - t_ask) * 1000))

        def _on_query(q: str) -> None:
            # спекулятивный поиск (п. 6): уходит в интернет, пока модель
            # ещё дописывает ответ
            asyncio.run_coroutine_threadsafe(_prefetch_search(q), loop)

        resp = await _answer(text, on_sentence=_on_sentence,
                             on_first_token=_on_first_token,
                             on_query=_on_query)

        # Единственная озвучка -- финальная реплика: sentence + audio
        # ДО reply-события (инвариант задач 14/16: телефон играет
        # аудио-чанки по приходу и НЕ повторяет reply при
        # streamed=true). Confirm-ворота needs_confirm/reply_replaced
        # больше не разделяют ветки: reply уже финальный. Текст уже
        # чист от мата: санитайзер задачи 21 в answer._answer (общий
        # путь этого ws-endpoint); /confirm чистится явными вызовами
        # sanitize_reply в server.py.
        chunks_sent = 0
        try:
            wav = await asyncio.to_thread(tts_mod.synthesize, resp.reply,
                                          llm.get_profile_voice())
        except Exception:
            log.warning("ws_voice: tts(final reply) failed", exc_info=True)
            wav = b""
        if wav:
            if not tts_first_ms:
                tts_first_ms.append(int((time.perf_counter() - t_ask)
                                        * 1000))
            chunks_sent += 1
            await ws.send_json({"event": "sentence", "text": resp.reply})
            await ws.send_json(
                {"event": "audio",
                 "b64": base64.b64encode(wav).decode("ascii")})

        # confirm/token -- телефону нужно для перехода в State.CONFIRM
        # (как в POST /voice; без них ломалось подтверждение поиска/отправки)
        # streamed=True: аудио финальной реплики уже ушло -- телефон НЕ
        # озвучивает reply повторно (иначе двойная речь). При упавшем
        # TTS streamed=False -- финальный reply скажет локальный TTS.
        await ws.send_json({"event": "reply", "reply": resp.reply,
                            "confirm": resp.needs_confirm,
                            "token": resp.token,
                            "streamed": chunks_sent > 0})
        await ws.send_json({"event": "done"})
        # тайминги этапов (фаза 6, п. 7): llm первый токен, tts первого
        # предложения, итог -- от них зависит задержка до первого звука
        log.info("ws_voice: done in %d ms (first_token=%s, "
                 "tts_first=%s, to_first_sound=%s, chunks=%d)",
                 int((time.perf_counter() - t0) * 1000),
                 first_ms[0] if first_ms else None,
                 tts_first_ms[0] if tts_first_ms else None,
                 (first_ms[0] + tts_first_ms[0])
                 if first_ms and tts_first_ms else None,
                 chunks_sent)
    except Exception:
        try:
            await ws.send_json({"event": "error",
                                "detail": "server error"})
        except Exception:  # noqa: BLE001 -- клиент мог уже уйти
            pass
        log.warning("ws_voice: session ended abnormally", exc_info=True)
    finally:
        _WS_ACTIVE = max(0, _WS_ACTIVE - 1)   # слот сессии свободен
        # п. 2.2: закрытие сессии гасит фоновый сегмент -- задача больше
        # не нужна (to_thread не отменить, но задача завершается сразу),
        # а её исключение глотается suppress, а не всплывает как
        # «exception never retrieved».
        if seg_task is not None:
            seg_task.cancel()
            with suppress(asyncio.CancelledError):
                await seg_task
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
