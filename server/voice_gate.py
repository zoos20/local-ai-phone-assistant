"""Гейт озвучки стрим-сентенций /ws_voice -- класс StreamGate (Д1, шаг 2).

Чистое перемещение из voice_ws.py БЕЗ изменения поведения: замыкания
ответной фазы (holder action_seen, очередь sent_q, воркер
_sentence_worker, правило при dequeue, latch unknown-action, санитайзер
на стриме, _final_voice и финальные ветки stop-chunks) собраны в класс.
voice_ws.py -- ws_voice-оркестратор: создаёт StreamGate на ответную
фазу, стартует воркер, после _answer дренажирует сентинелом и вызывает
finish(). Инварианты не тронуты (покрыты TestStreamWorkerFaults /
TestUnknownActionLatch / TestStopChunks):
    - правило ПРИ dequeue, не при enqueue (LLM-поток не блокируется);
    - latch: первое dequeue с неизвестным action -> mute до конца стрима;
    - исключение отправки чанка глотается НА ЭЛЕМЕНТ (mute, воркер жив);
    - sentence+audio всегда РАНЬШЕ reply-события;
    - телеметрия З1 (tts_first_ms/audio_send_at) пишется в holders
      voice_ws -- строка «ws_voice timing» байт-в-байт прежняя.

Инъекция (параметры конструктора): ws (send_json), voice (голос профиля,
резолвится в ws_voice один раз на сессию), loop (event loop для
run_coroutine_threadsafe из LLM-потока), tts (модуль tts -- ленивый
import в ws_voice, synthesize вызывается по атрибуту), t_ask (старт
ответной фазы), tts_first_ms/audio_send_at (holders блока телеметрии
ws_voice: их пишет гейт, читает _timing_line -- ветка «Не расслышал»
пишет строку ещё до создания гейта).
"""
import asyncio
import base64
import logging
import time

from reply_sanitize import sanitize_reply

log = logging.getLogger("assistant")


class StreamGate:
    """Гейт озвучки стрима (З5) -- очередь сентенций + воркер + правило.

    Пересмотр гейта задачи 16 (молчавший стрим): факт action теперь
    известен ПО ХОДУ генерации -- decide_stream отдаёт его через
    on_action (З4, action-first: ДО первой сентенции reply; живой probe
    З3: 25/25), и стрим озвучивается по мере прихода:
        action == "none"         -> сентенции ОЗВУЧИВАЮТСЯ (ответ модели
                                     и есть финальный reply);
        search/send_tg/send_vk/  -> ГЛОТАЮТСЯ (dispatch почти всегда
        call/shopping_*            заменит реплику: шаблон/фолбэк/
                                   вопрос-подтверждение -- иначе «хор»,
                                   phone.log 23:00:58-23:01:08);
        action ещё неизвестен    -> ГЛОТАЮТСЯ (безопасный дефолт: старый
                                     reply-first порядок; QA З7: при
                                     первом dequeue latch -- mute до
                                     конца стрима, даже если факт придёт
                                     в середине).
    Правило применяется ПРИ dequeue (не при enqueue): LLM-поток не
    блокируется синтезом. Санитайзер применяется к каждой сентенции при
    dequeue (§3.2: чистый текст ДО sentence/audio): изменённая сентенция
    -> mute до конца стрима, финал уйдёт целиком (resp.reply_replaced
    станет true). Страховка stop-chunks: при отправленных чанках и
    замене reply (или упавшем синтезе чанка) финальные sentence+audio
    досылаются, а reply-событие несёт опц. поле reply_replaced=true --
    телефон (З6) гасит очередь чанков и играет финал.
    """

    def __init__(self, *, ws, voice: str, loop, tts, t_ask: float,
                 tts_first_ms: list[int], audio_send_at: list) -> None:
        """ws/voice/loop/tts/телеметрия -- инъекция (см. докстринг модуля)."""
        self._ws = ws                      # send_json sentence/audio/reply
        self._voice = voice                # llm.get_profile_voice() сессии
        self._loop = loop                  # run_coroutine_threadsafe
        self._tts = tts                    # модуль tts (synthesize)
        self._t_ask = t_ask                # старт ответной фазы (З1)
        self._tts_first_ms = tts_first_ms  # телеметрия З1 (holder ws_voice)
        self._audio_send_at = audio_send_at  # holder [float | None] (З1)
        # Гейт-факт: holder -- обычный список. Запись -- append из
        # LLM-потока (атомарна под GIL), чтение -- из воркера
        # event-loop'а. Гонки записи/чтения нет: при action-first (З4)
        # append исполнен ДО того, как первая сентенция вообще попала в
        # очередь; при старом порядке (reply-first) сентенции легитимно
        # dequeue'ятся до факта -- правило глотания для них и существует.
        self.action_seen: list[str] = []
        # сентенции из LLM-потока
        self.sent_q: asyncio.Queue = asyncio.Queue()
        self.muted = False    # mute-режим: до конца стрима не озвучиваем
        self.chunks_sent = 0  # отправленные audio-события (телеметрия)
        self._stream_stop = object()   # сентинел: LLM-стрим завершён

    def on_action(self, action: str) -> None:
        """З4: факт action -- из LLM-потока (атомарная запись, выше)."""
        self.action_seen.append(action)

    async def _enqueue(self, s: str) -> None:
        self.sent_q.put_nowait(s)   # очередь не ограничена -> не ждёт

    def on_sentence(self, s: str) -> None:
        """Колбэк стрима LLM: синтез НЕ здесь (заблокировал бы стрим) --
        кладём в очередь event-loop'а, озвучивает воркер."""
        asyncio.run_coroutine_threadsafe(self._enqueue(s), self._loop)

    async def _voice_chunk(self, s: str) -> None:
        """Озвучка одной стрим-сентенции (правило гейта -- при dequeue)."""
        if self.muted:
            return
        action = self.action_seen[0] if self.action_seen else None
        if action != "none":
            # действие (dispatch заменит реплику) или action ещё
            # неизвестен (reply-first) -- глотаем (безопасный дефолт).
            # QA З7 (latch unknown): при ПЕРВОМ dequeue с неизвестным
            # action mute защёлкивается ДО КОНЦА стрима (однократная
            # запись muted) -- иначе при «перепутанном» порядке
            # (факт action приходит в середине стрима) получилось бы
            # смешанное озвучивание: первые глотались, а остальные
            # после факта звучали. Для ИЗВЕСТНОГО действия latch НЕ
            # ставится: on_action однократен (attempt==0, З4), факт
            # не меняется по ходу стрима -- правило остаётся
            # пофазным (см. TestUnknownActionLatch).
            if action is None:
                self.muted = True
                log.info("ws_voice: action unknown at first dequeue "
                         "-> latch mute till stream end")
            log.info("ws_voice: stream sentence gated out "
                     "(action=%s)", action or "unknown")
            return
        # Санитайзер НА СТРИМЕ (§3.2: чистый текст ДО sentence/audio):
        # мат в сентенции -> синтеза нет и до конца стрима молчим;
        # финал после _answer уйдёт целиком -- resp.reply_replaced
        # станет true (санитайзер в answer.py меняет итоговый reply).
        clean = sanitize_reply(s)
        if clean != s:
            self.muted = True
            log.info("ws_voice: stream sentence sanitized -> mute")
            return
        try:
            wav = await asyncio.to_thread(self._tts.synthesize, clean,
                                          self._voice)
        except Exception:
            # Упал синтез чанка (детерминированное правило ТЗ З5):
            # mute + финальный полный reply-аудио с
            # reply_replaced=true -- телефон переиграет финал
            # целиком, без «дыр» в речи.
            self.muted = True
            log.warning("ws_voice: stream chunk tts failed -> mute",
                        exc_info=True)
            return
        if not wav:
            return
        if not self._tts_first_ms:
            self._tts_first_ms.append(int((time.perf_counter()
                                           - self._t_ask) * 1000))
        await self._ws.send_json({"event": "sentence", "text": clean})
        if self._audio_send_at[0] is None:
            # З1/M1: wall ПЕРВОГО send_json события audio (чанков
            # может быть несколько -- guard держит именно первый)
            self._audio_send_at[0] = time.perf_counter()
        await self._ws.send_json(
            {"event": "audio",
             "b64": base64.b64encode(wav).decode("ascii")})
        self.chunks_sent += 1

    async def _sentence_worker(self) -> None:
        # ОДИН воркер на сессию: FIFO из sent_q, синтез серийный
        # (sidecar сам серийный под threading.Lock -- параллельных
        # синтезов нет, риск 5 ТЗ принят). Отменяется при обрыве
        # сессии (finally сессии ws_voice, как seg_task: to_thread не
        # отменить, но задача завершается сразу).
        # QA З7: исключение глотается/логируется НА ЭЛЕМЕНТ (по
        # образцу _seg_job -- инвариант файла «не всплывает»):
        # обрыв клиента при отправке чанка иначе убивал воркер, а
        # await worker_task в finally перевзбуждал его исключение --
        # ws.close() пропускался, трейс всплывал дважды. Решение
        # выбрано ВМЕСТО try/except вокруг await worker_task в
        # finally: ловушка на элементе защищает ОБА await'а воркера
        # (после _answer и в finally), держит воркер живым до
        # сентинела и переиспользует mute-семантику упавшего синтеза
        # (финал уйдёт целиком с reply_replaced). CancelledError не
        # глотается (except Exception) -- отмену гасит suppress в
        # finally сессии.
        while True:
            item = await self.sent_q.get()
            try:
                if item is self._stream_stop:
                    break
                await self._voice_chunk(item)
            except Exception:
                self.muted = True
                log.warning("ws_voice: stream chunk send failed "
                            "-> mute", exc_info=True)
            finally:
                self.sent_q.task_done()

    def start_worker(self) -> asyncio.Task:
        """Воркер стартует с началом ответной фазы: чанки стрима
        синтезируются ПАРАЛЛЕЛЬНО продолжению генерации и dispatch.
        Task возвращается ws_voice -- гасится в finally сессии."""
        return asyncio.ensure_future(self._sentence_worker())

    def stream_done(self) -> None:
        """Сентинел в очередь: LLM-стрим завершён -- воркер доест
        очередь и выйдет (ws_voice затем await worker_task)."""
        self.sent_q.put_nowait(self._stream_stop)

    async def _final_voice(self, reply_text: str) -> bool:
        """Синтез+отправка sentence+audio финальной реплики (тот же
        формат, что у чанков). True -- audio ушло."""
        try:
            wav = await asyncio.to_thread(self._tts.synthesize,
                                          reply_text, self._voice)
        except Exception:
            log.warning("ws_voice: tts(final reply) failed",
                        exc_info=True)
            return False
        if not wav:
            return False
        if not self._tts_first_ms:
            self._tts_first_ms.append(int((time.perf_counter()
                                           - self._t_ask) * 1000))
        await self._ws.send_json({"event": "sentence", "text": reply_text})
        if self._audio_send_at[0] is None:
            self._audio_send_at[0] = time.perf_counter()
        await self._ws.send_json(
            {"event": "audio",
             "b64": base64.b64encode(wav).decode("ascii")})
        self.chunks_sent += 1
        return True

    async def finish(self, resp) -> None:
        """Финал: три ветки по отправленным чанкам + reply-событие.

        Confirm/token -- телефону нужно для перехода в State.CONFIRM
        (как в POST /voice; без них ломалось подтверждение
        поиска/отправки). streamed=True: аудио уже ушло -- телефон НЕ
        озвучивает reply повторно (иначе двойная речь). При упавшем
        TTS streamed=False -- финальный reply скажет локальный TTS.
        """
        reply_ev = {"event": "reply", "reply": resp.reply,
                    "confirm": resp.needs_confirm, "token": resp.token}
        if self.chunks_sent == 0:
            # Чанков не было (глотание / нет сентенций / не LLM-путь):
            # прежний путь -- единственный синтез, финальный reply.
            # Текст уже чист от мата: санитайзер задачи 21 в
            # answer._answer (общий путь этого ws-endpoint); /confirm
            # чистится явными вызовами sanitize_reply в server.py.
            reply_ev["streamed"] = await self._final_voice(resp.reply)
        elif resp.reply_replaced or self.muted:
            # dispatch/санитайзер заменили reply ЛИБО упал синтез
            # чанка: прозвучавшие чанки НЕ равны финалу -> финальные
            # sentence+audio (полный resp.reply, уже чист) и опц. поле
            # reply_replaced=true (З6: телефон гасит очередь чанков и
            # играет финал; отсутствие поля = старое поведение).
            reply_ev["streamed"] = await self._final_voice(resp.reply)
            reply_ev["reply_replaced"] = True
        else:
            # Чанки уже озвучили reply == decision.reply: финальный
            # audio НЕ досылаем (повтор == двойная речь);
            # streamed=True -- телефон ждёт конца очереди и НЕ
            # озвучивает reply повторно.
            reply_ev["streamed"] = True
        await self._ws.send_json(reply_ev)
