import json
import logging
import re
from typing import Any

log = logging.getLogger("assistant.llm")

# --- потоковый разбор ответа модели (фаза 6, п. 1) -------------------------
# Модель отвечает JSON {"reply": "...", ...}: поле reply идёт ПЕРВЫМ, и его
# можно озвучивать ПО МЕРЕ поступления токенов, не дожидаясь конца генерации
# (действия/args дождутся конца). Инкрементальный парсер вырезает готовые
# предложения из частично накопленной строки reply.
_REPLY_KEY_RE = re.compile(r'"reply"\s*:\s*"')
_QUERY_KEY_RE = re.compile(r'"query"\s*:\s*"')
# конец предложения: . ! ? … (+ возможная закрывающая кавычка) + пробел/конец
_SENT_END_RE = re.compile(r"[.!?…][»\"]?(?=\s|$)")


class _StreamParser:
    """Вырезает готовые предложения из поля reply по мере стриминга.

    feed(delta) -> list[полных предложений]; close() -> хвост (последнее
    предложение без финальной пунктуации). on_query вызывается один раз,
    когда строка query полностью дописана (speculative-поиск, п. 6).
    """

    def __init__(self, on_query=None) -> None:
        self._raw = ""            # весь сырой поток
        self._reply_raw = ""      # накопленный текст ВНУТРИ строки reply
        self._reply_open = False  # нашли начало "reply": "
        self._reply_closed = False
        self._emitted = 0         # сколько символов reply уже отдано
        self._query_done = False
        self._on_query = on_query
        self.reply_text = ""      # декодированный reply (для фолбэков)
        self.spoken = ""          # всё, что уже отдано на озвучку (для is_committed)

    @staticmethod
    def _decode(s: str) -> str:
        """Частичная декодировка escape-последовательностей JSON-строки."""
        try:
            return json.loads('"' + s + '"')
        except (json.JSONDecodeError, ValueError):
            # обрыв на escape в конце ("\x") -- отрезаем хвост и честно
            # раскодируем основную часть
            for cut in (1, 2):
                if len(s) > cut:
                    try:
                        return json.loads('"' + s[:-cut] + '"')
                    except (json.JSONDecodeError, ValueError):
                        pass
            return (s.replace('\\"', '"').replace("\\n", " ")
                    .replace("\\\\", "\\"))

    @staticmethod
    def _closed_str_end(s: str) -> int:
        """Индекс НЕэкранированной закрывающей кавычки в s, или -1."""
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                return i
            i += 1
        return -1

    def feed(self, delta: str) -> list[str]:
        if self._reply_closed:
            # reply уже дописан, но query (action/args идут ПОСЛЕ reply)
            # может появиться позже -- продолжаем следить
            self._raw += delta
            self._maybe_query()
            return []
        self._raw += delta
        if not self._reply_open:
            m = _REPLY_KEY_RE.search(self._raw)
            if not m:
                # заодно ловим полный query для speculative-поиска (п. 6)
                self._maybe_query()
                return []
            self._reply_open = True
            self._reply_raw = self._raw[m.end():]
        else:
            self._reply_raw += delta
        end = self._closed_str_end(self._reply_raw)
        if end >= 0:
            self._reply_closed = True
            self._reply_raw = self._reply_raw[:end]
        text = self._decode(self._reply_raw)
        self.reply_text = text
        out: list[str] = []
        while True:
            m2 = _SENT_END_RE.search(text, self._emitted)
            if not m2:
                break
            sent = text[self._emitted:m2.end()].strip()
            self._emitted = m2.end()
            if len(sent) >= 3:
                out.append(sent)
                self.spoken = (self.spoken + " " + sent).strip()
        self._maybe_query()
        return out

    def _maybe_query(self) -> None:
        """Спекулятивный поиск (п. 6): как только query дописан целиком."""
        if self._query_done or self._on_query is None:
            return
        m = _QUERY_KEY_RE.search(self._raw)
        if not m:
            return
        q = self._raw[m.end():]
        end = self._closed_str_end(q)
        if end < 0:
            return
        self._query_done = True
        query = self._decode(q[:end]).strip()
        if query:
            try:
                self._on_query(query)
            except Exception:  # noqa: BLE001 -- префетч не должен ломать стрим
                log.exception("on_query callback failed")

    def close(self) -> str | None:
        """Хвост reply (последнее предложение без пунктуации), если есть."""
        if not self._reply_open:
            return None
        self._reply_closed = True
        tail = self.reply_text[self._emitted:].strip()
        self._emitted = len(self.reply_text)
        if len(tail) >= 3:
            self.spoken = (self.spoken + " " + tail).strip()
            return tail
        return None


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
# Скобочный дайджест истории: «(поиск в интернете: X)» / «(отправлено: …)»
_DIGEST_SEARCH_RE = re.compile(r"^\(\s*поиск в интернете:\s*(.+?)\s*\)$", re.I)
# Мета-реплики про формат ответа («буду отвечать JSON-объектом», «извините
# за формат»): озвучивать их нельзя -- пользователь слышал бы бред про
# схему вместо ответа. Ловим и валидный JSON-reply, и фолбэк-текст.
_META_JSON_RE = re.compile(
    r"(?:буду|отвечу|отвечать|отвечаете|отвечаем)\s+[^.!?]{0,60}?"
    r"(?:json|джейсон|по схеме)"
    r"|только\s+(?:одним\s+)?(?:json|джейсон)"
    r"|(?:json|джейсон)[-\s]объект"
    r"|(?:извин|простите|прошу прощения)[^.!?]{0,60}?(?:схем|формат|json)",
    re.I)


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт первый сбалансированный {...} из ответа модели."""
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("нет символа {")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("незакрытый JSON")
