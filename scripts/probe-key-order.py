# -*- coding: utf-8 -*-
"""Probe З3: держит ли Gemma порядок ключей JSON при action-first промпте.

Одноразовый исследовательский скрипт фичи «почти мгновенный голосовой
ответ» (задача З3 в project-tasks/voice-latency-tasklist.md). НЕ серверный
модуль: прод-код его не импортирует; llm/decide.py, llm/stream_parser.py,
voice_ws.py и прочие прод-файлы НЕ трогаются (только read-only импорт).
Коммитов нет.

Суть: system-промпт = ТОЧНАЯ копия продового SYSTEM_PROMPT (llm/decide.py),
в которой схема и ВСЕ примеры переставлены в порядок
{"action": ..., "args": ..., "reply": ...}. Тексты реплик/args/правила --
байт-в-байт прод: перестройка программная, из живого SYSTEM_PROMPT
(дрейф текста исключён, контроль полноты -- счётчик переставленных строк).
Остальное -- как в проде decide_stream:
  system[0] = промпт + personality_block() (профиль из active_profile.txt);
  system[1] = время отдельным system-сообщением (_time_system из decide.py);
  история не нужна (реплики одноходовые);
  запрос -- стримом llm.client.chat_stream (temperature 0.2, max_tokens
  1200, enable_thinking=false -- те же дефолты, что в проде);
  повторных попыток НЕТ: гейт потоковой озвучки видит только первую
  попытку стрима, повтор с _SCHEMA_REMINDER её поведение не спасает.

Метрики на реплику:
  1) позиция первого "action" vs "reply" в СЫРОМ стриме (символы от начала
     накопленного content) + позиция, где значение action дописано;
  2) extract_json + Decision (pydantic) валиден;
  3) action соответствует классу фразы (информационно);
  4) время генерации + первый токен.

Вердикт (критерий плана): нарушений порядка 0 на >=20 репликах -> A
(action-first + страховка stop-chunks); 1-2 случая -> повтор прогона;
систематические -> B (фолбэк stop-chunks, промпт не переставлять).

probe НЕ вызывает dispatch/хранилище: data/shopping_lists.json не меняется,
pending/confirm не создаются -- вызывается только llama.

Запуск из корня (llama-server должен быть запущен на settings.llm_base_url):
  .venv\\Scripts\\python scripts\\probe-key-order.py [--limit N] [--out PATH]
Выход: консоль -- ASCII-статус; по умолчанию
data/voice_latency/key-order-probe.md -- полный отчёт (таблица >=20 строк,
вердикт A/B, промпт-кандидат, дата) + рядом key-order-probe-run.json
(сырые ответы для разбора/повтора).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import httpx  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from config import settings  # noqa: E402
from llm.client import LLMError, chat_stream  # noqa: E402
from llm.decide import SYSTEM_PROMPT, Decision, _time_system  # noqa: E402
from llm.profiles import get_profile, personality_block  # noqa: E402
from llm.stream_parser import _StreamParser, extract_json  # noqa: E402

DEFAULT_OUT = BASE_DIR / "data" / "voice_latency" / "key-order-probe.md"
RUN_JSON = BASE_DIR / "data" / "voice_latency" / "key-order-probe-run.json"

# --- перестановка прод-промпта в action-first (З3) ---------------------------
# Строка-пример/схема вида {"reply": R, "action": A, "args": X} ->
# {"action": A, "args": X, "reply": R}; R/A/X не меняются. \r? -- на случай
# CRLF в исходнике (сейчас LF, но код не должен зависеть от этого).
_SWAP_RE = re.compile(
    r'^\{"reply": (?P<reply>"(?:[^"\\]|\\.)*"), '
    r'"action": (?P<action>"(?:[^"\\]|\\.)*"), '
    r'"args": (?P<args>\{.*\})\}(?P<cr>\r?)$',
    re.M,
)
_REPLY_LINE_RE = re.compile(r'^\{"reply"', re.M)
_ACTION_VAL_RE = re.compile(r'"action"\s*:\s*"')

# (класс, реплика, допустимые action). Реплики -- микс из задания З3.
# Класс-мисматчи -- ИНФОРМАЦИОННЫЕ: вердикт A/B считается ТОЛЬКО по
# порядку ключей (критерий плана).
PHRASES: list[tuple[str, str, frozenset[str]]] = [
    ("none", "привет", frozenset({"none"})),
    ("search", "погода в Москве завтра", frozenset({"search"})),
    ("shopping_create", "создай список покупок на неделю",
     frozenset({"shopping_create"})),
    ("call", "позвони маме", frozenset({"call"})),
    ("none", "расскажи что-нибудь интересное", frozenset({"none"})),
    ("send_tg", "отправь сообщение Диме", frozenset({"send_tg"})),
    ("shopping_add", "добавь в список покупок молоко и хлеб",
     frozenset({"shopping_add"})),
    ("search", "кто выиграл матч", frozenset({"search"})),
    ("ambiguous", "добавь в список", frozenset({"none"})),
    ("shopping_read", "что в списке покупок?", frozenset({"shopping_read"})),
    ("none", "кто такой Пушкин", frozenset({"none"})),
    ("send_tg", "напиши Маше", frozenset({"send_tg"})),
    ("shopping_lists", "какие списки у меня есть",
     frozenset({"shopping_lists"})),
    ("search", "найди рецепт борща", frozenset({"search"})),
    ("ambiguous", "удали список", frozenset({"none", "shopping_delete"})),
    ("call", "набери папу", frozenset({"call"})),
    ("none", "как дела", frozenset({"none"})),
    ("shopping_create", "создай список покупок",
     frozenset({"shopping_create"})),
    ("send_tg", "отправь Ане: я буду через час", frozenset({"send_tg"})),
    ("search", "сколько стоит биткоин", frozenset({"search"})),
    ("shopping_update", "поменяй молоко на кефир",
     frozenset({"shopping_update"})),
    ("shopping_check", "вычеркни хлеб из списка",
     frozenset({"shopping_check"})),
    ("search", "новости спорта за сегодня", frozenset({"search"})),
    ("shopping_read", "что у меня по списку покупок?",
     frozenset({"shopping_read"})),
    ("ambiguous", "создай ещё один список", frozenset({"none"})),
]


def _log(msg: str) -> None:
    print(f"[probe] {msg}", flush=True)


def _reconfigure_utf8() -> None:
    """UTF-8 stdout/stderr; консольный вывод держим ASCII по дизайну."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, OSError, ValueError):
        pass


def action_first_prompt() -> tuple[str, int]:
    """Прод-SYSTEM_PROMPT с переставленными ключами (схема + все примеры).

    Возвращает (промпт, число переставленных строк); падает, если не все
    строки вида {"reply"...} переставились (дрейф прод-промпта).
    """
    total = len(_REPLY_LINE_RE.findall(SYSTEM_PROMPT))

    def _swap(m: re.Match[str]) -> str:
        return ('{"action": ' + m.group("action") + ', "args": '
                + m.group("args") + ', "reply": ' + m.group("reply") + "}"
                + (m.group("cr") or ""))

    prompt, swapped = _SWAP_RE.subn(_swap, SYSTEM_PROMPT)
    if swapped == 0 or swapped != total:
        raise SystemExit(
            f"prompt drift: swapped={swapped} of {total} reply-lines -- "
            "SYSTEM_PROMPT отличается от ожидаемой формы; проверь _SWAP_RE")
    return prompt, swapped


@dataclass
class Row:
    """Результат одной реплики probe."""

    idx: int
    cls: str
    phrase: str
    expected: frozenset[str]
    gen_ms: int = 0
    first_ms: int | None = None
    raw: str = ""
    pos_action: int = -1
    pos_reply: int = -1
    pos_args: int = -1
    action_value: str = ""
    action_ready: int = -1
    decision_ok: bool = False
    decision_action: str = ""
    decision_reply: str = ""
    error: str = ""
    parse_error: str = ""

    @property
    def order_ok(self) -> bool | None:
        """True=action раньше reply; False=нарушение порядка;
        None=нет ни одного ключа (не-JSON/битый ответ -- вне критерия)."""
        if self.error:
            return None
        if self.pos_action == -1 and self.pos_reply == -1:
            return None
        return self.pos_action != -1 and (
            self.pos_reply == -1 or self.pos_action < self.pos_reply)

    @property
    def class_ok(self) -> bool | None:
        if not self.decision_ok:
            return None
        return self.decision_action in self.expected


def run_phrase(idx: int, cls: str, phrase: str, expected: frozenset[str],
               candidate: str) -> Row:
    """Одна реплика: стрим -> метрики порядка -> Decision."""
    row = Row(idx=idx, cls=cls, phrase=phrase, expected=expected)
    messages = [
        {"role": "system",
         "content": candidate + "\n\n" + personality_block()},
        {"role": "system", "content": _time_system()},
        {"role": "user", "content": phrase},
    ]
    acc: list[str] = []
    first_ms: int | None = None
    t0 = time.perf_counter()

    def _on_token(delta: str) -> None:
        nonlocal first_ms
        if first_ms is None:
            first_ms = int((time.perf_counter() - t0) * 1000)
        acc.append(delta)

    try:
        chat_stream(messages, on_token=_on_token)
    except LLMError as exc:
        row.gen_ms = int((time.perf_counter() - t0) * 1000)
        row.error = f"LLMError: {exc}"
        return row
    row.gen_ms = int((time.perf_counter() - t0) * 1000)
    row.first_ms = first_ms
    row.raw = "".join(acc)  # сырой стрим: позиции считаем по нему

    row.pos_action = row.raw.find('"action"')
    row.pos_reply = row.raw.find('"reply"')
    row.pos_args = row.raw.find('"args"')
    m = _ACTION_VAL_RE.search(row.raw)
    if m:
        start = m.end()
        end = _StreamParser._closed_str_end(row.raw[start:])
        if end >= 0:
            row.action_value = row.raw[start:start + end]
            row.action_ready = start + end + 1
    try:
        decision = Decision.model_validate(extract_json(row.raw))
    except (ValueError, json.JSONDecodeError, ValidationError) as exc:
        row.parse_error = f"{type(exc).__name__}: {exc}"[:300]
    else:
        row.decision_ok = True
        row.decision_action = decision.action
        row.decision_reply = decision.reply
    return row


def _console_row(row: Row, total: int) -> None:
    if row.error:
        _log(f"[{row.idx:02d}/{total}] cls={row.cls:<15} ERROR "
             f"{row.error[:80]}")
        return
    af = "YES" if row.order_ok else ("NO" if row.order_ok is False else "N/A")
    dec = "OK" if row.decision_ok else "FAIL"
    act = (row.action_value or row.decision_action or "-")[:18]
    match = "-" if row.class_ok is None else ("OK" if row.class_ok else "MISS")
    ft = (f"{row.first_ms / 1000:.1f}s"
          if row.first_ms is not None else "-")
    _log(f"[{row.idx:02d}/{total}] cls={row.cls:<15} af={af:<3} dec={dec:<4} "
         f"act={act:<18} match={match:<4} gen={row.gen_ms / 1000:.1f}s "
         f"ft={ft}")
    if row.order_ok is False:
        preview = row.raw[:100].encode("ascii", "backslashreplace").decode()
        _log(f"    VIOLATION pos_action={row.pos_action} "
             f"pos_reply={row.pos_reply} raw: {preview}")
    elif row.order_ok is None and row.raw:
        preview = row.raw[:100].encode("ascii", "backslashreplace").decode()
        _log(f"    NO-JSON raw: {preview}")


def _median(values: list[int]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    if n % 2:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def verdict_of(rows: list[Row]) -> tuple[str, str, str]:
    """Вердикт по критерию плана З3 (только порядок ключей)."""
    valid = [r for r in rows if not r.error]
    n = len(valid)
    af = [r for r in valid if r.order_ok is True]
    viol = [r for r in valid if r.order_ok is False]
    if n < 20:
        return ("INVALID",
                "Прогон некорректен (валидных ответов < 20)",
                f"Валидных ответов {n} из {len(rows)} -- критерий "
                "«>=20 реплик» не выполнен; проверить доступность llama "
                "и повторить прогон.")
    pct = 100.0 * len(af) / n
    if not viol:
        return ("A",
                "A — action-first + страховка stop-chunks",
                f"Нарушений порядка 0 на {n} репликах (action-first "
                f"{len(af)}/{n} = {pct:.0f}%): модель держит порядок ключей "
                '{"action", "args", "reply"} при переставленном промпте. '
                "Гейт З4/З5 строится на action-first (stream_parser отдаёт "
                "action-факт до первой сентенции reply); страховка "
                "stop-chunks / reply_replaced остаётся по плану З5.")
    if len(viol) <= 2:
        return ("REPEAT",
                "Повтор прогона (1–2 нарушения)",
                f"Нарушений {len(viol)} из {n} (action-first {len(af)}/{n} "
                f"= {pct:.0f}%) -- по критерию плана 1–2 случая требуют "
                "повторного прогона тем же скриптом; финальный вердикт -- "
                "по повтору.")
    return ("B",
            "B — фолбэк stop-chunks (промпт НЕ переставлять)",
            f"Систематические нарушения: {len(viol)} из {n} (action-first "
            f"{len(af)}/{n} = {pct:.0f}%) -- порядок ключей ненадёжен. "
            "Эскалация пользователю с цифрами: З4 делает ТОЛЬКО извлечение "
            "action + on_action (промпт и _SCHEMA_REMINDER не меняются), "
            "З5 -- гейт с дефолтом глотания (stop-chunks).")


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _row_json(r: Row) -> dict:
    return {
        "idx": r.idx,
        "class": r.cls,
        "phrase": r.phrase,
        "expected": sorted(r.expected),
        "gen_ms": r.gen_ms,
        "first_ms": r.first_ms,
        "raw": r.raw,
        "pos_action": r.pos_action,
        "pos_reply": r.pos_reply,
        "pos_args": r.pos_args,
        "action_value": r.action_value,
        "action_ready": r.action_ready,
        "decision_ok": r.decision_ok,
        "decision_action": r.decision_action,
        "decision_reply": r.decision_reply,
        "error": r.error,
        "parse_error": r.parse_error,
        "order_ok": r.order_ok,
        "class_ok": r.class_ok,
    }


def _write_report(out_path: Path, rows: list[Row], candidate: str,
                  swapped: int, profile_id: str, smoke: bool) -> None:
    """Полный отчёт md: таблица, детали, итог/вердикт, промпт-кандидат."""
    now = _dt.datetime.now()
    valid = [r for r in rows if not r.error]
    af = [r for r in valid if r.order_ok is True]
    viol = [r for r in valid if r.order_ok is False]
    broken = [r for r in valid if r.order_ok is None]
    mism = [r for r in valid if r.class_ok is False]
    gate_ok = [r for r in af
               if r.action_ready != -1
               and (r.pos_reply == -1 or r.action_ready < r.pos_reply)]
    gen_ms_list = [r.gen_ms for r in valid]
    ft_ms_list = [r.first_ms for r in valid if r.first_ms is not None]
    verdict, title, just = verdict_of(rows)
    if smoke:
        title += " (SMOKE-прогон -- таблица неполная, вердикт НЕ финальный)"

    lines: list[str] = [
        "# З3. Живая проверка порядка ключей JSON (action-first probe)",
        "",
        f"- **Дата прогона:** {now:%d.%m.%Y %H:%M}",
        f"- **Модель:** `{settings.llm_model}` @ `{settings.llm_base_url}` "
        "(llama-server, живой прогон; probe без dispatch -- хранилище "
        "списков `data/shopping_lists.json` НЕ менялось)",
        f"- **Профиль:** `{profile_id}` -- system[0] = промпт-кандидат + "
        "`personality_block()` (как в проде: `llm.profiles`, профиль из "
        "`active_profile.txt`); время -- отдельным system-сообщением "
        "(`_time_system`), как в проде",
        "- **Промпт:** ТОЧНАЯ копия `SYSTEM_PROMPT` из `llm/decide.py` "
        "(программная перестройка живого значения -- дрейф текста "
        "исключён); схема и все примеры (" + str(swapped) + " шт.) "
        "переставлены в порядок `{\"action\", \"args\", \"reply\"}`; "
        "тексты реплик/args/правила -- без изменений",
        "- **Запросы:** стрим `llm.client.chat_stream` (temperature 0.2, "
        "max_tokens 1200, enable_thinking=false -- дефолты прода); история "
        "не подмешивается (реплики одноходовые); БЕЗ повторных попыток "
        "(гейт потоковой озвучки видит только первую попытку стрима)",
        "- **AF (action-first):** позиция первого вхождения `\"action\"` в "
        "сыром стриме РАНЬШЕ первого `\"reply\"`; нарушения считаются "
        "только по порядку ключей (критерий плана)",
        "",
        "## Таблица (" + str(len(rows)) + " реплик)",
        "",
        "| # | Реплика | Класс | AF | pos action | pos reply | action | "
        "expected | Decision | класс ok | время, с |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r.error:
            af_cell = "ошибка"
            act_cell = "—"
            dec_cell = "—"
            cls_cell = "—"
        else:
            af_cell = ("да" if r.order_ok
                       else ("нет" if r.order_ok is False else "—"))
            act_cell = r.action_value or r.decision_action or "—"
            dec_cell = "да" if r.decision_ok else "нет"
            cls_cell = ("—" if r.class_ok is None
                        else ("да" if r.class_ok else "НЕТ"))
        lines.append(
            f"| {r.idx} | {_esc(r.phrase)} | {r.cls} | {af_cell} "
            f"| {r.pos_action if r.pos_action >= 0 else '—'} "
            f"| {r.pos_reply if r.pos_reply >= 0 else '—'} "
            f"| {act_cell} | {', '.join(sorted(r.expected)) or '—'} "
            f"| {dec_cell} | {cls_cell} | {r.gen_ms / 1000:.1f} |")
    lines += ["", "## Детали нарушений порядка", ""]
    if viol or broken:
        for r in viol + broken:
            kind = ("нарушение порядка" if r.order_ok is False
                    else "нет ключей (не-JSON/битый ответ)")
            lines.append(f"- **#{r.idx} «{_esc(r.phrase)}»** ({r.cls}) -- "
                         f"{kind}: pos_action={r.pos_action}, "
                         f"pos_reply={r.pos_reply}, pos_args={r.pos_args}, "
                         f"action_value=`{r.action_value or '—'}`, "
                         f"Decision={'да' if r.decision_ok else 'нет'} "
                         f"({r.parse_error or 'ok'})")
            lines.append("")
            lines.append("  ```text")
            lines.append("  " + r.raw[:500])
            lines.append("  ```")
            lines.append("")
    else:
        lines.append("Нарушений нет: на всех валидных репликах `\"action\"` "
                     "появился в стриме раньше `\"reply\"`.")
        lines.append("")
    lines += ["## Гейт-факт (для З4/З5)", ""]
    lines.append(
        f"На {len(gate_ok)} из {len(af)} action-first-реплик значение "
        "action было ДОПИСАНО ещё до открытия ключа `\"reply\"` "
        "(action -- короткий enum: ключ раньше reply == значение готово "
        "раньше любого текста reply). Это то, что нужно гейту З5: факт "
        "action доступен до первой сентенции reply.")
    lines.append("")
    if mism:
        lines += ["## Класс-мисматчи (информационно, на вердикт не влияют)",
                  ""]
        for r in mism:
            lines.append(
                f"- #{r.idx} «{_esc(r.phrase)}» ({r.cls}): "
                f"action=`{r.decision_action}`, expected="
                f"{', '.join(sorted(r.expected))}; reply: "
                f"«{_esc(r.decision_reply[:120])}»")
        lines.append("")
    if valid and len(valid) < len(rows):
        lines += ["## Ошибки LLM", ""]
        for r in rows:
            if r.error:
                lines.append(f"- #{r.idx} «{_esc(r.phrase)}» -- "
                             f"{_esc(r.error)}")
        lines.append("")
    lines += [
        "## Итог",
        "",
        f"- Валидных ответов: {len(valid)} из {len(rows)}",
        f"- Доля action-first: {len(af)}/{len(valid)} "
        f"({(100.0 * len(af) / len(valid) if valid else 0):.0f}%)",
        f"- Нарушений порядка: {len(viol)}",
        f"- Не-JSON/битых (вне критерия порядка): {len(broken)}",
        f"- Класс-мисматчей: {len(mism)} (информационно)",
        f"- Медианное время генерации: {_median(gen_ms_list) / 1000:.1f} с"
        + (f"; первый токен: {_median(ft_ms_list) / 1000:.1f} с"
           if ft_ms_list else ""),
        "",
        f"**Вердикт: {title}**",
        "",
        just,
        "",
        "Примечания:",
        "- probe вызывает только llama: dispatch/хранилище/confirm не "
        "затронуты.",
        "- Повторные попытки с `_SCHEMA_REMINDER` не использованы: "
        "гейт стримит только первую попытку; З4 при вердикте A также "
        "должен переставить схему в `_SCHEMA_REMINDER` "
        "({action, args, reply}).",
        "- Повтор прогона: `.venv\\Scripts\\python "
        "scripts\\probe-key-order.py` (llama должен быть запущен).",
        "",
        "## Промпт-кандидат (полный текст; применит З4 при вердикте A)",
        "",
        "Точная копия продового `SYSTEM_PROMPT` с переставленной схемой и "
        "примерами; правило follow-up'а — как в проде: время — отдельным "
        "system-сообщением, история не нужна (реплики одноходовые).",
        "",
        "```text",
        candidate,
        "```",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n",
                        encoding="utf-8", newline="\n")


def preflight() -> None:
    """llama должен отвечать: /health на корне (base_url оканчивается /v1)."""
    root = settings.llm_base_url.removesuffix("/v1")
    url = root + "/health"
    try:
        resp = httpx.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        raise SystemExit(f"llama-server недоступен ({url}): {exc}") from None
    if resp.status_code != 200:
        raise SystemExit(f"llama-server {url} -> HTTP {resp.status_code}")
    _log(f"llama OK: {url}")


def main() -> int:
    _reconfigure_utf8()
    parser = argparse.ArgumentParser(
        description="З3 probe: порядок ключей JSON (action-first)")
    parser.add_argument("--limit", type=int, default=0,
                        help="ограничить число реплик (0 = все; smoke)")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="путь отчёта md")
    args = parser.parse_args()

    preflight()
    candidate, swapped = action_first_prompt()
    profile_id = get_profile()
    _log(f"profile={profile_id} prompt_swapped_lines={swapped} "
         f"model={settings.llm_model} base={settings.llm_base_url}")

    phrases = PHRASES if args.limit <= 0 else PHRASES[:args.limit]
    rows: list[Row] = []
    for cls, phrase, expected in phrases:
        row = run_phrase(len(rows) + 1, cls, phrase, expected, candidate)
        rows.append(row)
        _console_row(row, len(phrases))

    out_path = Path(args.out)
    _write_report(out_path, rows, candidate, swapped, profile_id,
                  smoke=bool(args.limit))
    RUN_JSON.parent.mkdir(parents=True, exist_ok=True)
    RUN_JSON.write_text(
        json.dumps([_row_json(r) for r in rows], ensure_ascii=False,
                   indent=2) + "\n",
        encoding="utf-8", newline="\n")

    valid = [r for r in rows if not r.error]
    af = [r for r in valid if r.order_ok is True]
    viol = [r for r in valid if r.order_ok is False]
    verdict, _title, _just = verdict_of(rows)
    _log(f"rows={len(rows)} valid={len(valid)} af={len(af)}/{len(valid)} "
         f"({(100.0 * len(af) / len(valid) if valid else 0):.0f}%) "
         f"violations={len(viol)}")
    _log(f"VERDICT: {verdict}")
    _log(f"report: {out_path}")
    _log(f"raw answers: {RUN_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
