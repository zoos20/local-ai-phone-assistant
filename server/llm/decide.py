import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from llm._time import _now_str, _spoken_time
from llm.client import LLMError, chat, chat_stream
from llm.profiles import personality_block
from llm.stream_parser import (
    _DIGEST_SEARCH_RE,
    _META_JSON_RE,
    _StreamParser,
    extract_json,
)

log = logging.getLogger("assistant.llm")

SYSTEM_PROMPT = """Ты — голосовой ассистент «Гемма». Ты получаешь текст, распознанный из речи пользователя, и должен решить, что делать.

Отвечай ТОЛЬКО одним JSON-объектом. Без markdown, без пояснений, без текста вокруг. Схема:

{"reply": "<текст для озвучки вслух>", "action": "<search|send_tg|send_vk|call|shopping_create|shopping_add|shopping_read|shopping_update|shopping_check|shopping_delete|shopping_lists|none>", "args": {...}}

Правила:
- reply: 1-2 коротких предложения (до 25 слов) живым языком, их произнесут вслух. Развёрнутый ответ — только если просят прямо («расскажи подробнее», «подробнее»). Без ссылок, без разметки, без эмодзи.
- action=search — нужен ответ из интернета. args: {"query": "<поисковый запрос на русском>"}.
- action=send_tg — отправить сообщение в Telegram. args: {"to": "<имя или @username>", "message": "<текст сообщения>"}.
- action=send_vk — отправить сообщение ВКонтакте. args: {"to": "<имя или id>", "message": "<текст сообщения>"}.
- action=call — позвонить по телефону. args: {"contact": "<имя контакта из телефонной книги в ИМЕНИТЕЛЬНОМ падеже — «кто?»: Маша, а не Маше/Машу; либо сам номер, если его продиктовали>"}.
- action=shopping_create — создать список покупок. args: {"name": "<имя списка, необязательно; без него — «Список покупок»>", "items": [{"text": "<позиция>", "qty": "<сколько, необязательно: число или словами>"}]}. Позиций не названо -- создавай список без items (args {} или только name), товары спросят после; items обязателен только если пользователь назвал товары.
- action=shopping_add — добавить позиции в список. args: {"list": "<имя списка, необязательно — без него в активный список>", "items": [...]}. items обязателен и непустой.
- action=shopping_read — озвучить содержимое списка. args: {"list": "<имя списка, необязательно — без него активный>"}.
- action=shopping_update — изменить позиции списка. args: {"list": "<имя, необязательно>", "changes": [{"match": "<какую позицию меняем>", "new_text": "<новый текст, необязательно>", "new_qty": "<новое количество, необязательно>", "op": "<необязательно: «update» — изменить позицию (значение по умолчанию, можно не указывать) либо «delete» — удалить позицию>"}]}. changes обязателен и непустой; при op=delete new_text/new_qty не нужны.
- action=shopping_check — вычеркнуть купленные позиции: пометить выполненными, но НЕ удалять. args: {"list": "<имя, необязательно>", "items": [{"text": "<позиция>"}]}. items обязателен и непустой.
- action=shopping_delete — удалить список целиком (ассистент сначала спросит подтверждение). args: {"list": "<имя списка, необязательно — без него активный>"}.
- action=shopping_lists — перечислить вслух все списки пользователя. args: {} — без других полей.
- action=none — обычная реплика, приветствие, вопрос, отказ, уточняющий вопрос. args: {}.
- Если команда непонятна или в ней не хватает данных (кому писать, что искать, что добавить или изменить в списке) — action=none и в reply задай уточняющий вопрос.
- Списки покупок: позиции пиши коротко, в именительном падеже («молоко», «хлеб», а не «молока», «хлеба»). Поле list — только имя списка, не содержимое; отсутствие list (или пустая строка) означает активный список.
- Не выдумывай факты. Не выполняй отправку, если пользователь прямо не просил.
- Списки покупок: НИКОГДА не придумывай товары, названия списков и количества. В args перечисляются только те позиции, которые пользователь произнёс сам; количество -- только если пользователь его назвал.
- «Добавь в список» без единого названного товара -- это action=none с уточняющим вопросом («Что купить? Назови товары и количество.»). shopping_create и shopping_add с выдуманными позициями делать нельзя: позиции попадают в args только если пользователь назвал их сам.
- Прошлые списки из диалога не «реконструируй» и их содержимое в args не переноси. Непонятно, про какой список речь (несколько списков, неоднозначное имя) -- action=none с уточняющим вопросом.
- Прямой ответ пользователя товарами сразу после «что добавить/купить?» -- это shopping_add с items и БЕЗ list (в активный список), НЕ повторный create.
- list в args -- только имя, произнесённое пользователем в ЭТОЙ фразе; склонения «по списку покупок», «в списке покупок» -- это имя «Список покупок»; имя из истории диалога не переноси, если во фразе назван другой список; имя не сказано -- list не указывай (активный подберётся).
- Имя списка для shopping_create -- только если пользователь его назвал. Не назвал -- поле name не пиши вовсе: сервер сам назовёт список «Список покупок». Выдумывать имя списка нельзя.
- Хранилище списков не видно: не утверждай из памяти, сколько списков и что в них. «Какие списки есть» -- shopping_lists; «что в списке X» -- shopping_read; «ещё один список» без имени -- уточни имя, не создавай.
- НИКОГДА не употребляй мат, грубости и оскорбления -- ни в reply, ни в промежуточных фразах.
- Промежуточные фразы во время обдумывания -- короткие и нейтральные («Сейчас отвечу», «Смотрю»), не больше одной.
- В исходной реплике пользователя бывает мат -- повторять его нельзя, перефразируй вежливо.
- В reply НИКОГДА не упоминай JSON, схему, формат ответа и прочие технические детали -- ты просто говоришь голосом. Если спрашивают, «понял ли ты формат» или «будешь отвечать JSON», коротко ответь по делу без технических слов.
- Никогда не добавляй ничего кроме JSON-объекта.

Полезные примеры (формат, а не содержимое):
{"reply": "Ищу погоду в Москве", "action": "search", "args": {"query": "погода Москва сегодня"}}
{"reply": "Звоню Маше", "action": "call", "args": {"contact": "Маша"}}
{"reply": "Кому отправить сообщение?", "action": "none", "args": {}}

Примеры со списками покупок (реплика пользователя -> JSON):
«Создай список покупок на неделю: молоко два, яйца десяток»
{"reply": "Список «Список покупок на неделю» готов: молоко и яйца.", "action": "shopping_create", "args": {"name": "Список покупок на неделю", "items": [{"text": "молоко", "qty": 2}, {"text": "яйца", "qty": "десяток"}]}}
«Создай список покупок на неделю» (имя -- из слов пользователя)
{"reply": "Создаю список «Список покупок на неделю».", "action": "shopping_create", "args": {"name": "Список покупок на неделю"}}
«Добавь в список яйца»
{"reply": "Добавляю яйца в список.", "action": "shopping_add", "args": {"items": [{"text": "яйца"}]}}
«В списке на неделю добавь сметану»
{"reply": "Добавляю сметану в список на неделю.", "action": "shopping_add", "args": {"list": "На неделю", "items": [{"text": "сметана"}]}}
«Хлеб и сахар» (сразу после вопроса «что добавить?»)
{"reply": "Записываю хлеб и сахар.", "action": "shopping_add", "args": {}}
«Что у меня по списку покупок?» (склонение -- имя «Список покупок»)
{"reply": "Читаю список покупок.", "action": "shopping_read", "args": {"list": "Список покупок"}}
«Что по на неделю?»
{"reply": "Читаю список на неделю.", "action": "shopping_read", "args": {"list": "На неделю"}}
«Какие списки у меня есть?»
{"reply": "Перечисляю.", "action": "shopping_lists", "args": {}}
«Создаю ещё один список»
{"reply": "Как назвать новый список?", "action": "none", "args": {}}
«Поменяй молоко на кефир»
{"reply": "Меняю молоко на кефир.", "action": "shopping_update", "args": {"changes": [{"match": "молоко", "new_text": "кефир"}]}}
«Вычеркни хлеб»
{"reply": "Отмечаю хлеб выполненным.", "action": "shopping_check", "args": {"items": [{"text": "хлеб"}]}}
«Удали список на неделю»
{"reply": "Удаляю список на неделю.", "action": "shopping_delete", "args": {"list": "На неделю"}}
«Добавь в список» — не сказано, что именно; товары не выдумываем
{"reply": "Что докупить? Назови товары и количество.", "action": "none", "args": {}}
«Создай список покупок» — не назван ни один товар, ничего не придумываем
{"reply": "Создаю список. Назови, что в него положить.", "action": "shopping_create", "args": {}}"""

# Дубликаты строк-реплик (backlog: дедуп после распила llm.py в пакет) --
# текст побайтово идентичен прежним литералам, поведение не меняется.
_SCHEMA_REMINDER = (
    "Отвечай ТОЛЬКО одним JSON-объектом по схеме "
    "{reply, action, args}. Без текста вокруг. "
    "И не упоминай в reply слова JSON, схема, "
    "формат -- отвечай как обычный голосовой "
    "собеседник."
)
_NOT_UNDERSTOOD = "Я не совсем понял. Скажи ещё раз, пожалуйста."

# --- валидация args списков покупок (фича «списки покупок», задачи 4 и 20) --
# Структурно битые args отсекаются ЗДЕСЬ -> action=none + уточняющий вопрос;
# данные не меняются (runtime-неопределённости -- в dispatch, решение 4 плана).
# Задача 20 (живой факт 09.11 «создай новый список» -> фолбэк после «создаю»):
# create РЕЛАКСНУТ -- пустой/отсутствующий items ВАЛИДЕН (канон args {}),
# отсечение -- только структура-мусор; add/check остаются строгими.
_SHOPPING_ITEMS_QUESTION = {
    "shopping_create": "Что купить? Перечисли позиции для списка.",
    "shopping_add": "Что добавить в список? Скажи, что купить.",
    "shopping_check": "Какие позиции вычеркнуть? Скажи, что уже куплено.",
}


def _shopping_items_ok(value: Any) -> bool:
    """items (create/add/check): непустой список, каждый элемент -- объект
    с непустым строковым text (иначе «маленький LLM мусорит в args»)."""
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
    return True


def _shopping_create_ok(args: dict[str, Any]) -> bool:
    """Единый контракт (задача 20): shopping_create МОЖЕТ не нести items.
    Позиций не названо -> канон args {} (или только name) -- ВАЛИДНО:
    dispatch создаст пустой список и сам спросит «что положить».
    items прислал? -- [] равнозначен отсутствию, непустой обязан пройти
    строгий _shopping_items_ok (отсекает мусор-структуру, но НЕ выдумки:
    те структурно валидны и держатся правилами промпта).
    shopping_add релаксации НЕ касается (строгий items обязателен)."""
    items = args.get("items")
    if items is None:
        return True
    if isinstance(items, list) and not items:
        return True  # [] -- то же «позиции не названы»
    return _shopping_items_ok(items)


def _shopping_changes_ok(value: Any) -> bool:
    """changes (update): непустой список, каждый элемент -- объект
    с непустым строковым match."""
    if not isinstance(value, list) or not value:
        return False
    for change in value:
        if not isinstance(change, dict):
            return False
        match = change.get("match")
        if not isinstance(match, str) or not match.strip():
            return False
    return True


def _shopping_list_ok(args: dict[str, Any]) -> bool:
    """list (add/read/update/check/delete) опционален; задан (не None) ->
    только строка-имя списка. Отсутствие/None -> активный список."""
    value = args.get("list")
    return value is None or isinstance(value, str)


def _shopping_name_ok(args: dict[str, Any]) -> bool:
    """name (create) опционален; задан (не None) -> только строка."""
    value = args.get("name")
    return value is None or isinstance(value, str)


def _shopping_lists_ok(args: dict[str, Any]) -> bool:
    """lists (задача 22): канон args {} (или вовсе без args) -- перечисление
    делает dispatch из хранилища. Любое другое содержимое (items/list/
    name/...) -- мусор модели, фолбэк action=none с уточнением."""
    return not args


class Decision(BaseModel):
    """Структурированное решение модели."""

    reply: str = Field(min_length=1, max_length=1500)
    action: Literal[
        "search", "send_tg", "send_vk", "call", "none",
        "shopping_create", "shopping_add", "shopping_read",
        "shopping_update", "shopping_check", "shopping_delete",
        "shopping_lists",
    ] = "none"
    args: dict[str, Any] = Field(default_factory=dict)


def system_prompt() -> str:
    """Статичный system-промпт decide(): схема + личность. БЕЗ времени.

    Вынесен в функцию, чтобы keep-alive на сервере поддерживал ТОТ ЖЕ
    префикс промпта, что и реальные запросы (см. server.keep-alive):
    иначе мини-запрос затирает кэш KV слота и каждый живой вопрос
    платит ~5 с префилла заново.

    ВРЕМЯ ЗДЕСЬ НЕТ (ускорение 30.10): строка времени меняется каждую
    минуту и сбивала кэш префикса -- на границе минуты каждый запрос
    платил ~2 с префилла заново (медиана /ask 4 с вместо 1.1-1.7).
    Статичный системник кэшируется и греется keep-alive'ом; время
    подаётся ОТДЕЛЬНЫМ system-сообщением (см. _time_system()), которое
    стоит ПОСЛЕ статичного -- при смене минуты из кэша выбивается только
    оно + история + реплика (~80-130 токенов), а не весь системник.
    Проверено на гемме: «привет» отвечает без даты (баг старого формата
    со временем в user-сообщении), кэш бьёт 727/728 токенов.
    """
    return SYSTEM_PROMPT + "\n\n" + personality_block()


def _time_system() -> str:
    """Отдельное system-сообщение с текущим временем/датой.

    Стоит ПОСЛЕ статичного system_prompt() (см. decide()), чтобы не
    сбивать кэш префикса большим системником: меняется только эта
    короткая строка, а схема+личность (≈660 токенов) остаются в кэше.
    """
    return (f"Текущие дата и время: {_now_str()}. "
            f"Время для озвучки уже записано словами: «{_spoken_time()}» "
            "-- отвечай по нему этими словами, не переводи цифры сама. "
            "Если спрашивают о времени, дате, «сейчас», «сегодня», "
            "дне недели -- отвечай по ним, а не по памяти модели.")


def _decide_messages(user_text: str,
                     history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Сообщения decide(): system[0] статичный (кэш префикса), system[1]
    время, история, реплика пользователя."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": _time_system()},
    ]
    for h in (history or []):
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            # Мета-реплика про формат не должна копиться в истории:
            # иначе модель на следующих вопросах продолжает «извиняться».
            if role == "assistant" and _META_JSON_RE.search(content):
                continue
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})
    return messages


def _decide_with(messages: list[dict[str, str]],
                 make_chat,
                 is_committed=None) -> Decision:
    """Общая цепочка decide/decide_stream: запрос -> разбор -> фолбэки.

    make_chat(attempt) -> callable(messages) -> raw-текст ответа (вызывается
    на КАЖДОЙ попытке -- для стриминга важен свежий парсер на повторе).
    is_committed() -> True, если часть ответа УЖЕ озвучена (стримингом):
    тогда повтор с новым текстом задвоил бы речь -> не повторяем, а
    возвращаем то, что уже сказано.
    """
    decision: Decision | None = None
    for attempt in range(2):
        try:
            raw = make_chat(attempt)(messages)
        except LLMError as exc:
            if attempt == 0:
                if is_committed is not None and is_committed():
                    # стрим уже озвучил часть ответа -- повтор задвоил бы
                    # речь; возвращаем сказанное
                    log.warning("chat() ошибка после стрима (%s) -- берём "
                                "озвученное", exc)
                    return Decision(reply=is_committed(), action="none")
                log.warning("chat() ошибка (%s) -- повтор", exc)
                continue
            return Decision(reply="Сервер модели не отвечает, попробуй ещё раз.",
                            action="none")

        try:
            decision = Decision.model_validate(extract_json(raw))
            if _META_JSON_RE.search(decision.reply):
                # Валидный JSON, но reply -- мета-реплика про формат
                # («буду отвечать JSON-объектом»): не озвучиваем, повтор.
                raise ValueError("мета-ответ про формат")
            break
        except (ValueError, json.JSONDecodeError, ValidationError):
            clean = " ".join(raw.split())
            # Фолбэк-разбор и на ПЕРВОЙ попытке (фикс скорости 29.10):
            # осмысленный текст без JSON чаще всего УЖЕ готовый ответ
            # («У меня всё, а у тебя? Что нового?»), а повтор стоил
            # лишних +3-6 с к латентности. Повтор с напоминанием
            # оставляем только для явных сбоев формата (мета-реплика
            # про JSON, обрывки {...) -- здесь он реально помогает.
            m = _DIGEST_SEARCH_RE.match(clean)
            if m:
                query = m.group(1).strip()
                log.info("фолбэк: дайджест истории -> search (%r)", query)
                return Decision(reply=f"Ищу в интернете: {query}",
                                action="search", args={"query": query})
            if clean and _META_JSON_RE.search(clean):
                if attempt == 0 and (is_committed is None or not is_committed()):
                    log.warning("мета-ответ про формат (%r) -- повтор "
                                "с напоминанием", clean[:120])
                    messages.append({
                        "role": "user",
                        "content": _SCHEMA_REMINDER,
                    })
                    continue
                if is_committed is not None and is_committed():
                    # мета-фраза уже озвучена стримом -- не задваиваем
                    # «Я не совсем понял», история = то, что слышали
                    return Decision(reply=is_committed(), action="none")
                return Decision(
                    reply=_NOT_UNDERSTOOD,
                    action="none",
                )
            # Обычный текст (не мета, не дайджест, не скобки/JSON) ->
            # готовая реплика, БЕЗ повтора.
            if clean and not clean.startswith("{") and not clean.startswith("("):
                log.info("фолбэк: текстовый ответ без JSON -> reply "
                         "без повтора (%r)", clean[:120])
                return Decision(reply=clean[:1500], action="none")
            if attempt == 0 and (is_committed is None or not is_committed()):
                # Сломанный JSON/пусто -- здесь повтор с напоминанием
                # о схеме уместен (но НЕ если стрим уже озвучил reply:
                # повтор задвоил бы речь).
                log.warning("модель ответила не по схеме (%r) -- повтор",
                            raw[:200])
                messages.append({
                    "role": "user",
                    "content": _SCHEMA_REMINDER,
                })
                continue
            if is_committed is not None and is_committed():
                return Decision(reply=is_committed(), action="none")
            return Decision(
                reply=_NOT_UNDERSTOOD,
                action="none",
            )

    if decision is None:
        return Decision(reply=_NOT_UNDERSTOOD, action="none")

    # Проверяем, что для действия хватает аргументов.
    if decision.action == "search" and not str(decision.args.get("query", "")).strip():
        return Decision(reply="Что поискать? Скажи тему запроса.", action="none")
    if decision.action in ("send_tg", "send_vk"):
        if not str(decision.args.get("to", "")).strip():
            return Decision(reply="Кому отправить? Назови получателя.", action="none")
        if not str(decision.args.get("message", "")).strip():
            return Decision(reply="Какой текст отправить?", action="none")
    if decision.action == "call" and not str(decision.args.get("contact", "")).strip():
        return Decision(reply="Кому позвонить? Назови имя контакта.", action="none")
    # Списки покупок: структурная валидация args (по образцу search/call --
    # см. test_missing_search_query_rejected): битые данные -> вежливый
    # уточняющий вопрос, действие НЕ выполняем, ничего не меняем.
    if decision.action.startswith("shopping_"):
        # Пустая/состриженная в пустоту строка list = «не задан» ->
        # активный список: убираем ключ ДО проверок, чтобы downstream
        # (dispatch) не получал list="" как имя списка.
        lst = decision.args.get("list")
        if isinstance(lst, str) and not lst.strip():
            decision.args.pop("list", None)
    if decision.action in _SHOPPING_ITEMS_QUESTION:
        # create релакснут (задача 20): items отсутствует/пуст -> ВАЛИДНО
        # (канон args {} -- dispatch создаёт пустой список и спросит
        # позиции); мусор-структура отсекается как раньше. add/check
        # остаются строгими.
        ok = (_shopping_create_ok(decision.args)
              if decision.action == "shopping_create"
              else _shopping_items_ok(decision.args.get("items")))
        if not ok:
            return Decision(reply=_SHOPPING_ITEMS_QUESTION[decision.action],
                            action="none")
    if decision.action == "shopping_update":
        if not _shopping_changes_ok(decision.args.get("changes")):
            return Decision(
                reply="Что изменить в списке? Скажи, какую позицию "
                      "и на что заменить.",
                action="none")
    if decision.action in ("shopping_add", "shopping_read", "shopping_update",
                           "shopping_check", "shopping_delete"):
        if not _shopping_list_ok(decision.args):
            return Decision(
                reply="В каком списке? Назови его имя.",
                action="none")
    if decision.action == "shopping_create" and not _shopping_name_ok(
            decision.args):
        return Decision(reply="Как назвать список?", action="none")
    if decision.action == "shopping_lists":
        # перечисление делает dispatch из хранилища; лишние поля -- мусор
        # модели (задача 22): фолбэк action=none с уточнением.
        if not _shopping_lists_ok(decision.args):
            return Decision(
                reply="Что рассказать о списках? Назови один или спроси "
                      "прямо «какие списки есть».",
                action="none")
    return decision


def decide(user_text: str,
           history: list[dict[str, str]] | None = None) -> Decision:
    """Текст пользователя -> валидное решение (reply/action/args).

    history — предыдущие реплики диалога (роли user/assistant), чтобы
    работали уточнения («а завтра?») и местоимения. Подмешиваются перед
    текущей репликой.
    """
    messages = _decide_messages(user_text, history)
    # Диагностика: что реально уходит в LLM (системник + время + история +
    # реплика). system[0] статичный, system[1] -- время.
    log.info("LLM messages: system=%d hist=%d user=%r",
             len(messages[0]["content"]), len(history or []), user_text)
    # make_chat -- фабрика: получает номер попытки (см. _decide_with),
    # иначе TypeError не ловится как LLMError и decide() падает целиком
    # (баг 06.11: «Сервер модели не отвечает» на /voice и /ask).
    return _decide_with(
        messages, lambda attempt: lambda msgs: chat(msgs, max_tokens=1200))


def decide_stream(user_text: str,
                  history: list[dict[str, str]] | None = None,
                  *,
                  on_sentence=None,
                  on_first_token=None,
                  on_query=None) -> Decision:
    """Стриминговый decide (фаза 6, п. 1): тот же результат, что и decide(),
    но reply уходит по предложениям через on_sentence, пока генерация ещё
    идёт (первый токен ~0.3-0.5 с вместо 1.5-8 с полного ответа).

    on_sentence(text)  -- готовое предложение для озвучки (вызывается из
                          потока llama-стрима; на сервере -- в отдельном
                          потоке, очередь в asyncio);
    on_first_token()   -- первый токен content получен (замер таймингов);
    on_query(query)    -- поисковый запрос дописан целиком (спекулятивный
                          старт поиска, п. 6).
    """
    messages = _decide_messages(user_text, history)
    log.info("LLM messages (stream): system=%d hist=%d user=%r",
             len(messages[0]["content"]), len(history or []), user_text)

    # Озвученный текст (для is_committed): если первая попытка уже
    # стримнула предложения -- повтор с новым текстом задвоил бы речь.
    spoken: list[str] = []

    def _make_stream_chat(attempt: int):
        # Свежий парсер на каждую попытку: повтор -- новый ответ с нуля.
        parser = _StreamParser(on_query)
        first = False

        def _stream(msgs: list[dict[str, str]]) -> str:
            nonlocal first

            def _on_token(delta: str) -> None:
                nonlocal first
                if not first:
                    first = True
                    if on_first_token is not None:
                        on_first_token()
                if on_sentence is not None:
                    for sent in parser.feed(delta):
                        spoken.append(sent)
                        on_sentence(sent)

            # На второй попытке НЕ стримим: первая могла уже озвучить
            # часть реплики (см. is_committed) -- повтор идёт молча,
            # озвучка пойдёт финальным событием на сервере.
            if on_sentence is not None and attempt == 0:
                raw = chat_stream(msgs, on_token=_on_token)
                tail = parser.close()
                if tail:
                    spoken.append(tail)
                    on_sentence(tail)
            else:
                raw = chat(msgs, max_tokens=1200)
            return raw

        return _stream

    return _decide_with(messages, _make_stream_chat,
                        is_committed=lambda: " ".join(spoken).strip())


def answer_from_memory(question: str) -> str:
    """Ответ из памяти модели, БЕЗ поиска в интернете (п. 2 фазы 4).

    Используется, когда пользователь отказался от поиска (сказал «нет»
    или стоп-слово после объявления «Выполняю поиск в интернете»).
    """
    prompt = (
        f"Сейчас: {_now_str()}.\n\n"
        "Пользователь отказался от поиска в интернете и просит ответить "
        "из того, что тебе уже известно:\n"
        f"{question}\n\n"
        "Ответь вслух, живым языком, 2-3 предложения. Если точно не "
        "знаешь -- честно скажи об этом и предложи поискать, если нужно."
    )
    try:
        return chat(
            [
                {"role": "system", "content": "Ты голосовой ассистент, "
                                              "отвечаешь по памяти модели." +
                                              personality_block()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=800,
            temperature=0.3,
        )
    except LLMError as exc:
        log.warning("ответ из памяти не удался (%s)", exc)
        return "Не смог вспомнить точно. Если нужно -- скажи «да», и я поищу."


def summarize_search(question: str, results: list[dict[str, str]]) -> str:
    """Просит модель коротко пересказать выдачу -- для озвучки вслух."""
    if not results:
        return "Ничего не нашёл по этому запросу."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')} — {r.get('body', '')} "
                     f"({r.get('url', '')})")
    block = "\n".join(lines)
    prompt = (
        f"Сейчас: {_now_str()}.\n\n"
        "Пользователь спросил голосом:\n"
        f"{question}\n\n"
        "Результаты поиска:\n"
        f"{block}\n\n"
        "Ответь вслух, живым языком, 2-3 предложения: самое важное по сути "
        "вопроса. Не перечисляй заголовки и не копируй фрагменты сайтов "
        "дословно -- перефразируй. Можно назвать источник (домен) устно, "
        "без полных ссылок. Если в результатах нет ответа -- честно скажи, "
        "что не нашёл."
    )
    try:
        text = chat(
            [
                {"role": "system", "content": "Ты кратко пересказываешь "
                                              "результаты поиска для голосового ответа. "
                                              "В этом задании JSON НЕ нужен: отвечай "
                                              "ТОЛЬКО обычным текстом для озвучки, "
                                              "без схем, скобок и кавычек." +
                                              personality_block()},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,
            temperature=0.3,
        )
        # Жалоба живого теста 30.10 «озвучивает json»: модель приучена к
        # схеме {reply, action, args} и иногда заворачивает пересказ в
        # JSON -- такая озвучка читает «фишка-репли-квоут...». Если ответ
        # всё же распарсился как JSON -- берём текстовое поле.
        try:
            obj = extract_json(text)
            for key in ("reply", "text", "answer", "result"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    log.info("summarize_search: ответ пришёл JSON -- "
                             "взял поле %r", key)
                    return val.strip()
        except ValueError:
            pass  # обычный текст -- как есть
        return text
    except LLMError as exc:
        # Фолбэк: НЕ отдаём сырой title+body -- Piper читает цифры/дефисы
        # вслух («435… от 5 до 10… минус 1%»), это и были «кракозябры»
        # (живой тест 29.10 11:35). Отдаём чистую короткую фразу с самой
        # темой; данные по возможности берём из первого body, но без
        # спецсимволов/цифровых таблиц.
        log.warning("пересказ поиска не удался (%s) -- упрощённый ответ", exc)
        first = results[0]
        title = " ".join(str(first.get("title", "")).split())[:120]
        host = ""
        try:
            from urllib.parse import urlparse
            host = (urlparse(str(first.get("url", ""))).hostname or "").lstrip("www.")
        except Exception:
            host = ""
        tail = f" Подробности -- на сайте {host}." if host else ""
        if title:
            return f"Нашёл по теме: {title}.{tail}"
        return "Нашёл результаты по запросу, но не смог пересказать их вслух."
