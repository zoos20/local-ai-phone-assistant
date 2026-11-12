import asyncio
import logging
import time

import llm
from api_models import AskResponse, VoiceResponse
from dialog_state import (
    _digest_text,
    _history_digest,
    add_turn,
    get_history,
    reset_history,
    wants_forget,
)
from dispatch import _dispatch
from profile_detect import detect_profile_command
from reply_sanitize import sanitize_reply

log = logging.getLogger("assistant")


async def _answer(text: str,
                  on_sentence=None,
                  on_first_token=None,
                  on_query=None) -> AskResponse:
    """Общая цепочка: текст -> решение модели -> действие -> ответ.

    on_sentence/on_first_token/on_query (фаза 6, п. 1) -- включают
    стриминговый путь decide_stream: предложения reply отдаются
    консумеру, пока генерация ещё идёт (первый токен ~0.3-0.5 с вместо
    ожидания полного ответа). /ask его не подключает (блокирующий
    decide()); /ws_voice подключает, но с гейта задачи 16 mid-stream
    предложения в TTS НЕ идут -- озвучивается только финальный reply.
    """
    t0 = time.perf_counter()
    log.info("ask: %r", text)

    # Явный сброс контекста -- локально, мимо модели (быстро и надёжно).
    if wants_forget(text):
        await reset_history()
        return VoiceResponse(
            reply="Хорошо, начал новый разговор. Слушаю.",
            action="none", args={}, needs_confirm=False, token=None,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Команды профиля -- тоже локально, без LLM (быстро + надёжно).
    pcmd = detect_profile_command(text)
    if pcmd is not None:
        elapsed = int((time.perf_counter() - t0) * 1000)
        if pcmd == "get":
            reply = f"Сейчас профиль: {llm.get_profile_label()}."
        elif pcmd == "list":
            labels = ", ".join(p["label"] for p in llm.list_profiles())
            reply = (f"Профили: {labels}. Скажи, например, "
                     f"«профиль цундере».")
        elif pcmd.startswith("set:"):
            pid = pcmd.split(":", 1)[1]
            llm.set_profile(pid)
            # Новый характер -- новый разговор: старая история (сухие
            # реплики без характера) тянет ответы в прежний нейтральный
            # тон, и 4B-модель повторяет её сильнее, чем system-промпт
            # (живой тест 29.10: после смены на дередере отвечала
            # нейтрально, пока история не сброшена).
            await reset_history()
            # подтверждение уже НОВЫМ профилем -> слышна смена манеры
            reply = f"Хорошо, теперь я — {llm.get_profile_label()}."
        else:
            reply = "Не понял, какой профиль."
        log.info("profile cmd %r -> %r", text, reply)
        return VoiceResponse(reply=reply, action="none", args={},
                             needs_confirm=False, token=None,
                             elapsed_ms=elapsed)

    # Быстрый путь БЕЗ LLM (фаза 6, п. 2): время/число/день недели ->
    # готовый шаблон на сервере (~0 мс вместо 2-7 с генерации).
    fast = llm.fast_reply(text)
    if fast is not None:
        elapsed = int((time.perf_counter() - t0) * 1000)
        log.info("fast reply in %d ms: %r", elapsed, fast)
        # ходы в историю -- чтобы уточнения («а завтра?») видели вопрос
        await add_turn("user", text)
        await add_turn("assistant", _digest_text(fast))
        return VoiceResponse(reply=fast, action="none", args={},
                             needs_confirm=False, token=None,
                             elapsed_ms=elapsed)

    history = await get_history()
    t_llm = time.perf_counter()

    # Стриминговый путь (фаза 6, п. 1): decide_stream шлёт готовые
    # предложения через on_sentence, пока генерация ещё идёт.
    # llama-стрим блокирующий (httpx) -> в отдельном потоке.
    if on_sentence is not None:
        spoken: list[str] = []

        def _on_sentence(s: str) -> None:
            spoken.append(s)
            try:
                on_sentence(s)
            except Exception:
                log.warning("on_sentence failed", exc_info=True)

        first_ms: list[int] = []

        def _first() -> None:
            first_ms.append(int((time.perf_counter() - t_llm) * 1000))
            if on_first_token is not None:
                try:
                    on_first_token()
                except Exception:
                    log.warning("on_first_token failed", exc_info=True)

        def _run() -> llm.Decision:
            return llm.decide_stream(text, history, on_sentence=_on_sentence,
                                     on_first_token=_first, on_query=on_query)

        try:
            decision = await asyncio.to_thread(_run)
        except Exception:
            log.exception("decide_stream failed")
            decision = llm.Decision(
                reply="Сервер модели не отвечает, попробуй ещё раз.",
                action="none")
        llm_ms = int((time.perf_counter() - t_llm) * 1000)
        log.info("decision: action=%s args=%s (llm stream %d ms, "
                 "first_token=%s, spoken=%d sentences)",
                 decision.action, decision.args, llm_ms,
                 first_ms[0] if first_ms else None, len(spoken))
    else:
        try:
            decision = await asyncio.to_thread(llm.decide, text, history)
        except Exception:
            log.exception("decide failed")
            decision = llm.Decision(
                reply="Сервер модели не отвечает, попробуй ещё раз.",
                action="none")
        llm_ms = int((time.perf_counter() - t_llm) * 1000)
        log.info("decision: action=%s args=%s (llm %d ms)",
                 decision.action, decision.args, llm_ms)

    # Итоговый reply может ОТЛИЧАТЬСЯ от реплики модели, УЖЕ отстриманной
    # через on_sentence: dispatch заменяет её фолбэком-уточнением (нет
    # активного списка / неоднозначное имя — задача 14, критерий S8) либо
    # возвращает детерминированный шаблон (чтение/эхо списков — S3).
    # Флаг reply_replaced -- ИНФОРМАЦИОННЫЙ (закреплён тестами): после
    # задачи 16 voice_ws синтезирует финальный reply БЕЗУСЛОВНО после
    # готовности решения (mid-stream болтовня в TTS не уходит вовсе),
    # так что гейт на этом поле больше не ветвит логику; /ask поле
    # отсекает через response_model=AskResponse. Формат истории НЕ
    # меняется: _history_digest(decision, reply) работает с тем же
    # decision.
    model_reply = decision.reply
    # text -- для детерминированной сетки фразы в dispatch (задача 24):
    # resolve адресата сверяет токены ФРАЗЫ с именами списков и
    # пере-записывает галлюцинации args.list из истории. REST /confirm
    # _dispatch не зовёт -- там text не нужен.
    reply, needs_confirm, token = await _dispatch(decision, text=text)
    # Санитайзер профанизма (задача 21 + фоллоу-ап ревью): точка на
    # общем пути _answer -- REST /ask+/voice, история ниже и /ws_voice
    # (он отправляет resp.reply в TTS). POST /confirm собирает реплику
    # ОТДЕЛЬНО от _answer (server.py) -- там каждый выход реплики
    # санитайзится ЯВНЫМ вызовом sanitize_reply. Пользовательские
    # args/items и шаблоны dispatch не санитайзятся; идемпотентно
    # (чистый текст 1:1).
    reply = sanitize_reply(reply)
    elapsed = int((time.perf_counter() - t0) * 1000)
    log.info("reply in %d ms: %r", elapsed, reply)
    # В историю кладём реплику ассистента в JSON-схеме {reply, action, args}
    # (фикс 30.10): формат истории == формат ответа модели держит её в режиме
    # схемы. Проза в истории ломала JSON у Gemma-4-E4B (action=search терялся),
    # Qwen она не ломала, но JSON-история ей не мешает (бенч до/после идентичен).
    await add_turn("user", text)
    await add_turn("assistant", _history_digest(decision, reply))
    # VoiceResponse — наследник AskResponse с полями text/stt_ms (с дефолтами);
    # /ask отдаёт его через response_model=AskResponse, лишние поля отфильтруются.
    return VoiceResponse(reply=reply, action=decision.action, args=decision.args,
                         needs_confirm=needs_confirm, token=token,
                         elapsed_ms=elapsed,
                         reply_replaced=reply != model_reply)
