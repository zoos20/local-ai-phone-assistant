import json
import logging

import httpx

from config import settings

log = logging.getLogger("assistant.llm")

class LLMError(RuntimeError):
    pass


def chat(messages: list[dict[str, str]], *, max_tokens: int = 1200,
         temperature: float = 0.2) -> str:
    """Один вызов чата llama-server. Возвращает content ассистента.

    Модель reasoning-типа: сначала пишет reasoning_content («думает»),
    потом content. Если лимит токенов кончился на размышлениях -- content
    пустой при finish=length; тогда повторяем с большим бюджетом.
    """
    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    def _request(mt: int) -> tuple[str, str]:
        try:
            with httpx.Client(base_url=settings.llm_base_url, headers=headers,
                              timeout=120.0) as client:
                resp = client.post(
                    "/chat/completions",
                    json={
                        "model": settings.llm_model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": mt,
                        # reasoning-модель «думает» до 30+ секунд и съедает
                        # бюджет токенов (content оставался пустым).
                        # Для голосового ассистента размышления отключаем:
                        # 5.8 с вместо 32-44 с.
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
                resp.raise_for_status()
                try:
                    data = resp.json()
                except (ValueError, KeyError) as exc:
                    # llama-server вернул не-JSON (обрыв/мусор) -- это не сеть,
                    # наружу не отдаём, чтобы /ask и /voice не падали на 500.
                    raise LLMError(
                        f"неожиданный ответ LLM (не JSON): {resp.text[:500]}"
                    ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM недоступна: {exc}") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            finish = choice.get("finish_reason") or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"неожиданный ответ LLM: {data}") from exc
        if not isinstance(content, str):
            raise LLMError(f"неожиданный content LLM: {data}")
        # Диагностика: пустой content при finish=length = бюджет токенов ушёл
        # в размышления/обвязку -- видно в логе, чтобы не гадать.
        if not content.strip():
            log.warning("пустой content LLM: finish=%s tokens_out=%s",
                        finish, len(data.get("usage", {}).get("completion_tokens", "") or ""))
        return content.strip(), finish

    content, finish = _request(max_tokens)
    budget = max_tokens
    while not content and finish == "length" and budget < 4096:
        budget = min(budget * 3, 4096)
        log.info("content пуст (лимит ушёл в размышления) -- повтор с "
                 "max_tokens=%d", budget)
        content, finish = _request(budget)
    if not content:
        raise LLMError("пустой ответ LLM")
    return content


def chat_stream(messages: list[dict[str, str]], *,
                on_token=None,
                max_tokens: int = 1200,
                temperature: float = 0.2) -> str:
    """Стриминговый вызов llama-server (фаза 6, п. 1).

    Шлёт stream=true и вызывает on_token(delta) по мере поступления токенов
    (первый токен ~0.3-0.5 с вместо ожидания полного ответа). Возвращает
    полный content -- тот же формат, что и chat().
    """
    headers = {}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    parts: list[str] = []
    try:
        with httpx.Client(base_url=settings.llm_base_url, headers=headers,
                          timeout=120.0) as client:
            with client.stream(
                "POST",
                "/chat/completions",
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        parts.append(delta)
                        if on_token is not None:
                            on_token(delta)
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM недоступна: {exc}") from exc

    content = "".join(parts).strip()
    if not content:
        raise LLMError("пустой ответ LLM (стрим)")
    return content
