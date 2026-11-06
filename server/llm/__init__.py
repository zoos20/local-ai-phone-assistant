"""Клиент llama.cpp + промпт со строгой JSON-схемой ответа.

У Gemma нет встроенного function calling, поэтому действуем так:
  1) просим ТОЛЬКО JSON по схеме {reply, action, args};
  2) вырезаем первый {...} из ответа (модель любит заворачивать в ```json);
  3) валидируем pydantic-моделью; при любой ошибке -- action=none с вежливым
     уточнением вместо галлюцинации.
"""

from llm._time import _now_str, _spoken_time
from llm.client import LLMError, chat, chat_stream

# приватное, но нужное тестам
from llm.decide import (
    SYSTEM_PROMPT,
    Decision,
    _decide_with,
    answer_from_memory,
    decide,
    decide_stream,
    summarize_search,
    system_prompt,
)
from llm.fast_reply import fast_reply
from llm.profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    get_profile,
    get_profile_label,
    get_profile_voice,
    list_profiles,
    personality_block,
    set_profile,
)
from llm.stream_parser import _StreamParser, extract_json

__all__ = [
    "DEFAULT_PROFILE",
    "PROFILES",
    "SYSTEM_PROMPT",
    "Decision",
    "LLMError",
    "_StreamParser",
    "_decide_with",
    "_now_str",
    "_spoken_time",
    "answer_from_memory",
    "chat",
    "chat_stream",
    "decide",
    "decide_stream",
    "extract_json",
    "fast_reply",
    "get_profile",
    "get_profile_label",
    "get_profile_voice",
    "list_profiles",
    "personality_block",
    "set_profile",
    "summarize_search",
    "system_prompt",
]
