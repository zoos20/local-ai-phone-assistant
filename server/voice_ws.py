import asyncio
import json
import logging
import secrets
import time
from contextlib import suppress

from fastapi import APIRouter, WebSocket

import llm
from answer import _answer
from config import settings
from endpointing import (
    _WS_BYTES_PER_MS,
    _WS_END_TAIL_S,
    _WS_FRAME,
    _WS_MAX_PCM_BYTES,
    _WS_MAX_SESSIONS,
    _WS_RECV_IDLE_S,
    _WS_VOICE_MIN,
    EndpointDetector,
)
from search import _prefetch_search
from voice_gate import StreamGate

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
#                      {"event":"sentence","text":..}-- реплика: стрим-чанк
#                                                      (З5, озвучка стрима
#                                                      при action=none) или
#                                                      финальная реплика;
#                      {"event":"audio","b64":...}  -- WAV реплики
#                                                      (sentence+audio всегда
#                                                      РАНЬШЕ reply-event);
#                      {"event":"reply","reply":...,"streamed":bool
#                                       [,"reply_replaced":true]}
#                                                     -- итог: reply целиком
#                                                      + confirm/token;
#                                                      streamed=True -- чанки
#                                                      уже играли, НЕ
#                                                      озвучивать повторно;
#                                                      reply_replaced=true --
#                                                      опц. поле (З5): чанки
#                                                      НЕ равны финалу --
#                                                      телефон (З6) гасит
#                                                      очередь чанков и
#                                                      играет финал
#                                                      (отсутствие поля =
#                                                      старое поведение);
#                      {"event":"done"}             -- всё.
# Endpointing -- на сервере: адаптивный порог (фон ×2.5, как на телефоне)
# + тишина >= _WS_QUIET_MS после голоса. Режим детектора -- query
# ?prelude=: 1/нет (wake: прелюдия = хвост «Геммы», защита grace) /
# 0 (follow-up: команда без «Геммы», закрытие по тишине без гейта grace).
# Сегментный stt на лету параллелит распознавание с речью: после конца
# фразы whisper'у остаётся доспознать только хвост.
# --------------------------------------------------------------------------
# Константы _WS_* и EndpointDetector/FrameAction -- в endpointing.py (Д1,
# шаг 1): ЕДИНСТВЕННЫЙ источник значений, пороги §7 не меняются; сюда
# импортируются только используемые имена. _WS_ACTIVE -- мутабельный
# счётчик сессий, живёт здесь (патчится тестами).
_WS_ACTIVE = 0               # текущее число сессий (счётчик ниже)


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
    # Режим endpointing от клиента (фича «почти мгновенный голосовой
    # ответ»): телефон знает режим и сообщает его query-параметром.
    #   ?prelude=0 -- follow-up: команда без «Геммы», прелюдия = начало
    #                 самой команды, защита паузы «до команды» не нужна
    #                 (детектор закрывает приём по тишине, а не по
    #                 NO_VOICE-таймауту).
    #   ?prelude=1 или параметра НЕТ -- wake-режим с защитой прелюдии.
    # Совместимость: старый APK параметр не шлёт -> prelude=True ->
    # прежнее поведение; новый APK + старый сервер -- старый сервер не
    # читает неизвестный query-параметр (деградация к прежнему M1, не
    # поломка).
    prelude = ws.query_params.get("prelude", "1") != "0"
    det = EndpointDetector(prelude=prelude)
    log.info("ws_voice: session start (prelude=%s)", prelude)
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
    # З5: ссылка на воркер озвучки стрим-сентенций (создаётся в ответной
    # фазе); гасится при закрытии сессии -- как seg_task.
    worker_task: asyncio.Task | None = None
    heard: list[str] = []     # сегменты, распознанные на лету
    # ---------------- телеметрия стадий (З1, метка «ws_voice timing») ---
    # Все величины -- на уже существующих часах (t0/perf_counter),
    # поведение конвейера не меняется. Объявления подняты СЮДА из
    # ответной фазы, чтобы строка писалась и на ветке «Не расслышал»
    # (там first_token/decision/tts_first/audio_send естественно None:
    # LLM и озвучки не было; fast_reply/profile/forget идут полным
    # путём -- у них None только first_token).
    last_voice_at: float | None = None  # wall последнего голосового кадра
    stt_seg_ms: list[int] = []          # длительности transcribe_pcm сегментов
    audio_send_at = [None]              # holder: wall первого send_json
                                        # «audio» -- пишет StreamGate,
                                        # читает _timing_line (ниже);
                                        # список, чтобы запись из гейта
                                        # была видна здесь
    t_ask: float | None = None          # старт ответной фазы
    decision_ms: int | None = None      # длительность _answer (от t_ask)
    first_ms: list[int] = []            # первый токен LLM (от t_ask)
    tts_first_ms: list[int] = []        # первый чанк TTS (от t_ask; пишет
                                        # StreamGate)

    def _timing_line(text: str, *, chunks: int) -> None:
        """З1: ЕДИНЫЙ лог-лайн телеметрии на каждый ws-ответ С ответом.

        Пишется на всех выходах С ответом: полный путь (после done) и
        ветка «Не расслышал»; fast_reply/profile/forget идут тем же
        полным путём. Где ответа НЕТ -- строки нет (abort, disconnect,
        лимит аудио, исключение: до ответа не дошло, M1 не определён;
        abnormal-путь уже логируется «session ended abnormally»).
        Заменяет итоговую строку «ws_voice: done in ...»; пре-ответная
        «endpoint=... stt_tail=...» ОСТАВЛЕНА (решение З1: чекпоинт --
        если сессия падает в ответной фазе, услышанное и позиция
        endpoint видны по ней).
        Поля (None -- значения нет): last_voice_ms -- wall последнего
        голосового кадра от t0 (голос в хвосте ПОСЛЕ endpoint кадрами
        не разбирается -- ограничение приёма, не телеметрии);
        voice_pos_ms -- контроль: позиция det.last_voice в записи;
        audio_send_ms -- M1: wall первого send_json «audio» минус
        last_voice_ms.
        """
        audio_send: int | None = None
        if audio_send_at[0] is not None and last_voice_at is not None:
            audio_send = int((audio_send_at[0] - last_voice_at) * 1000)
        last_voice: int | None = None
        if last_voice_at is not None:
            last_voice = int((last_voice_at - t0) * 1000)
        log.info(
            "ws_voice timing: last_voice_ms=%s endpoint_ms=%d "
            "stt_total_ms=%d first_token_ms=%s decision_ms=%s "
            "tts_first_ms=%s audio_send_ms=%s total_ms=%d chunks=%d "
            "voice_pos_ms=%d heard=%r",
            last_voice, int(det.endpoint_ms), sum(stt_seg_ms) + stt_ms,
            first_ms[0] if first_ms else None, decision_ms,
            tts_first_ms[0] if tts_first_ms else None, audio_send,
            int((time.perf_counter() - t0) * 1000), chunks,
            int(det.last_voice / _WS_BYTES_PER_MS), text)

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
            t_seg = time.perf_counter()
            t = await asyncio.to_thread(stt.transcribe_pcm, pkt)
            # З1: длительность сегментного stt -- в stt_total_ms телеметрии
            # (сумма сегментов + хвостовой stt_ms в итоговой строке)
            stt_seg_ms.append(int((time.perf_counter() - t_seg) * 1000))
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
            if act.is_voice:
                # З1/M1: wall последнего голосового кадра -- каждый
                # голосовой кадр перезаписывает, остаётся ПОСЛЕДНИЙ.
                # Голос, досланный в хвосте ПОСЛЕ endpoint, кадрами не
                # разбирается: там last_voice_at не обновляется
                # (ограничение приёма, не телеметрии).
                last_voice_at = time.perf_counter()
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
        # З1: эта пре-ответная строка ОСТАВЛЕНА (решение исполнителя) --
        # чекпоинт: если сессия упадёт в ответной фазе, услышанное и
        # позиция endpoint видны по ней; полная телеметрия стадий --
        # единый «ws_voice timing» в конце ответной фазы.
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
            # З1: ответ был (пусть и «Не расслышал») -- телеметрия-строка
            # тоже пишется; LLM-поля None: ответной фазы не было
            _timing_line(text, chunks=0)
            return

        # ---------------- ответ (фаза 6, п. 1; гейт озвучки З5) --------
        # Гейт озвучки стрима -- класс StreamGate (voice_gate.py, Д1
        # шаг 2): очередь сентенций + ОДИН воркер + правило ПРИ dequeue
        # (action=none озвучивается, действия/unknown глотаются с latch,
        # санитайзер на стриме) -- правило и его инварианты описаны в
        # докстринге класса. Сюда -- только оркестрация: создать гейт,
        # стартовать воркер, после _answer дренажировать сентинелом,
        # дождаться воркера, финал -- gate.finish().
        loop = asyncio.get_running_loop()
        # t_ask/first_ms/tts_first_ms объявлены в блоке телеметрии З1
        # выше (ветка «Не расслышал» пишет строку до ответной фазы)
        t_ask = time.perf_counter()
        # Голос резолвится ОДИН раз на сессию (профиль не меняется в
        # течение ответной фазы: профиль-команды обрабатываются раньше
        # LLM-пути).
        voice = llm.get_profile_voice()
        # Инъекция в гейт: ws/voice/loop/tts (ленивый import выше) и
        # телеметрия З1 -- holders tts_first_ms/audio_send_at (их пишет
        # гейт, читает _timing_line).
        gate = StreamGate(ws=ws, voice=voice, loop=loop, tts=tts_mod,
                          t_ask=t_ask, tts_first_ms=tts_first_ms,
                          audio_send_at=audio_send_at)
        worker_task = gate.start_worker()

        def _on_first_token() -> None:
            first_ms.append(int((time.perf_counter() - t_ask) * 1000))

        def _on_query(q: str) -> None:
            # спекулятивный поиск (п. 6): уходит в интернет, пока модель
            # ещё дописывает ответ
            asyncio.run_coroutine_threadsafe(_prefetch_search(q), loop)

        resp = await _answer(text, on_sentence=gate.on_sentence,
                             on_first_token=_on_first_token,
                             on_query=_on_query, on_action=gate.on_action)
        # З1: стадия decision -- целиком _answer (от t_ask до возврата)
        decision_ms = int((time.perf_counter() - t_ask) * 1000)

        # LLM-стрим завершён: все сентенции уже в очереди (порядок
        # call_soon_threadsafe FIFO -- завершение to_thread встало в
        # очередь ПОСЛЕ enqueue'ов стрима). Сентинел -> воркер доест
        # очередь и выйдет; ждём его, чтобы финал ушёл ПОСЛЕ чанков.
        gate.stream_done()
        await worker_task
        worker_task = None   # обработан: в finally отменять нечего

        # Финал: три ветки по отправленным чанкам + reply-событие
        # (voice_gate.StreamGate.finish; sentence+audio всегда РАНЬШЕ
        # reply -- инвариант внутри finish).
        await gate.finish(resp)
        await ws.send_json({"event": "done"})
        # З1: единый телеметрия-лог ws-ответа -- заменяет прежнюю строку
        # «ws_voice: done in ...» (first_token/tts_first/итог -- внутри).
        # chunks -- ВСЕ отправленные audio-события (чанки стрима + финал).
        _timing_line(text, chunks=gate.chunks_sent)
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
        # З5: обрыв сессии гасит воркер озвучки стрима -- как seg_task
        # (to_thread не отменить: текущий синтез дозвучит в потоке, но
        # его результат уже никому не отправится; задача завершается).
        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
