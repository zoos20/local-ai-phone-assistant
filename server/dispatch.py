import logging
import re
import secrets

import llm
import shopping_store
from dialog_state import Pending, _gc_pending, _pending

log = logging.getLogger("assistant")


async def _dispatch(decision: llm.Decision,
                    text: str = "") -> tuple[str, bool, str | None]:
    """Выполняет решение. Возвращает (reply, needs_confirm, token).

    Фолбэки (нет активного списка, имя не найдено/неоднозначно, ошибка
    хранилища) мутируют decision ДО возврата: answer.py пишет историю и
    строит ответ ПОСЛЕ _dispatch, поэтому action="none" + пустые args
    попадают и в ответ, и в историю (решение 4 плана фичи списков).
    """
    if decision.action == "search":
        # План, фаза 4, п. 2: сначала объявляем и ждём подтверждения.
        # «да» -> /confirm yes -> реально ищем; стоп-слово/«нет» ->
        # /confirm no -> ответ из памяти модели без поиска.
        query = str(decision.args["query"])
        ask = (f"Выполняю поиск в интернете: {query}. "
               f"Скажи «да», чтобы я продолжила.")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action="search", args={"query": query},
                                  reply=ask)
        return ask, True, token

    if decision.action in ("send_tg", "send_vk"):
        # Сначала спрашиваем подтверждение; отправка — только по /confirm.
        to = str(decision.args.get("to", "")).strip()
        message = str(decision.args.get("message", "")).strip()
        channel = "Telegram" if decision.action == "send_tg" else "ВКонтакте"
        ask = (f"Отправить {to} в {channel}: «{message}»? "
               f"Скажи «да» или «отмена».")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action=decision.action,
                                  args={"to": to, "message": message},
                                  reply=ask)
        return ask, True, token

    if decision.action == "call":
        # Фаза 4 (звонки): объявляем и ждём «да»; по /confirm yes телефон
        # получит action=call и сам наберёт номер (контакт или продиктованный).
        contact = str(decision.args.get("contact", "")).strip()
        ask = (f"Позвонить {contact}? Скажи «да» или «отмена».")
        token = secrets.token_urlsafe(8)
        _gc_pending()
        _pending[token] = Pending(action="call", args={"contact": contact},
                                  reply=ask)
        return ask, True, token

    # --- Списки покупок (фича «Списки покупок», задачи 3/22) ----------------
    # Семь веток shopping_*: адресат (args.list либо активный список)
    # разрешается ДО любых мутаций; runtime-неопределённость -> фолбэк
    # action=none + уточнение, данные не меняются. Удаление списка —
    # только через pending (как call): реально — в /confirm при yes:true.
    if decision.action == "shopping_create":
        return await _shopping_create(decision)
    if decision.action == "shopping_add":
        return await _shopping_add(decision, text)
    if decision.action == "shopping_read":
        return await _shopping_read(decision, text)
    if decision.action == "shopping_update":
        return await _shopping_update(decision, text)
    if decision.action == "shopping_check":
        return await _shopping_check(decision, text)
    if decision.action == "shopping_delete":
        return await _shopping_delete(decision, text)
    if decision.action == "shopping_lists":
        return await _shopping_lists(decision)

    return decision.reply, False, None


# --------------------------------------------------------------------------
# Списки покупок: шаблонные реплики и разрешение адресата.
#
# Правила (решение 5 плана): <=2 предложения, без markdown/спецсимволов;
# позиции в именительном падеже, количество — «молоко — 2»; чтение <=15
# позиций, дальше «…и ещё N позиций»; вычёркнутые — отдельной репликой.
# Тексты детерминированные, модель в них не участвует; при исключении
# хранилища — вежливый фолбэк на decision.reply (не 500, не краш).
# --------------------------------------------------------------------------
_READ_LIMIT = 15      # позиций в shopping_read всего (озвучка ≤15, решение 5)
_READ_DONE_LIMIT = 5  # слотов под вычёркнутые в чтении (показываются всегда)
_ECHO_LIMIT = 10      # позиций в эхо create/add/check/update
_STORE_FAIL = "Не получилось выполнить операцию, попробуй ещё раз."

# Уточнения (фолбэк action=none; данные не менять)
_Q_NO_ACTIVE = ("Нет активного списка. Назови его имя "
                "или попроси создать новый.")
_Q_NOT_FOUND = "Список «{name}» не найден. Назови его точнее или создай новый."
# Дедуп (задача 22): {names} собирает УНИКАЛЬНЫЕ имена кандидатов; если
# имя одно, а списков с ним несколько -- ««Список покупок» ×2». Хвост --
# разрешимый выход для голоса (активный уже выбран по умолчанию).
_Q_AMBIGUOUS = ("Подходит несколько списков: {names}. "
                "Скажи „в активный“ или переименуй один.")
_Q_BAD_LIST = "В каком списке? Назови его имя."
_Q_EMPTY_LISTS = "Списков покупок пока нет. Скажи „создай список покупок“."
_Q_DUP_ACTIVE = ("Список «{name}» уже есть — {cnt}, теперь он активный. "
                 "Что добавить?")

# Синонимы адресата «активный» (фоллоу-ап ревью задачи 22): подсказка
# _Q_AMBIGUOUS предлагает юзеру фразу «в активный», а модель по правилу
# «args.list только из произнесённого» эхом пришлёт её — без синонима
# случился бы тупик на предложенной же фразе. Резолвится в активный
# список ТОЛЬКО если настоящего списка с таким именем НЕТ (точное/
# подстрочное совпадение приоритетно, «Активный» не затеняется).
_ACTIVE_WORDS = frozenset(("активный", "в активный", "активный список"))

# Детерминированная сетка фразы (задача 24): токены ФРАЗЫ юзера против
# имён списков -- STT склоняет («по списку покупок» != подстрока
# «Список покупок»), а args.list 4B-модель контаминирует историей
# (живые факты 12.11 10:00). Стем токена -- первые 4 символа; стоп-
# слова (частицы/предлоги/императивы) в score НЕ участвуют, но
# предлоги работают в evidence-окне (_phrase_evidence). «списк*» и
# «покуп*» НЕ стоп -- части имён списков.
_TOKEN_RE = re.compile(r"[а-яё0-9a-z]+")
_STOP_WORDS = frozenset((
    "а", "и", "но", "в", "во", "на", "по", "о", "об", "про", "у", "для",
    "что", "как", "какие", "какой", "мой", "моя", "моё", "моем", "меня",
    "мне", "там", "это", "эта", "еще", "ещё", "опять", "скажи", "покажи",
    "добавь", "удали", "отметь", "купи", "сним",
))
# Предлоги в evidence-окне (задача 24, beta): «что ПО на неделю».
_PREPS = frozenset(("в", "во", "по", "из", "с", "со", "на", "у", "про"))
_COVER_MIN = 0.6     # порог покрытия значимых стемов имени стемами фразы

# Пустой create (задача 20, единый контракт с llm/decide): items не
# названы -- список уже создан и активен, отвечаем шаблоном С ИМЕНЕМ
# списка. Текст НЕ дублирует фолбэк decide (_SHOPPING_ITEMS_QUESTION
# «Что купить? Перечисли позиции...» -- там вопрос при мусорных args,
# а здесь позиций просто не назвали, и содержимое называем следом).
_EMPTY_CREATE = ("Список «{name}» создан. "
                 "Теперь назови, что положить в него.")


def _fallback(decision: llm.Decision, question: str) -> tuple[str, bool, None]:
    """Фолбэк: action=none + уточнение; ничего не выполняем и не меняем."""
    decision.action = "none"
    decision.args = {}
    decision.reply = question
    return question, False, None


def _new_pending(action: str, args: dict, question: str) -> str:
    """Pending-токен подтверждения (тот же механизм, что у search/call)."""
    token = secrets.token_urlsafe(8)
    _gc_pending()
    _pending[token] = Pending(action=action, args=args, reply=question)
    return token


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма числа: позиция / позиции / позиций."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _pos(n: int) -> str:
    return _plural(n, "позиция", "позиции", "позиций")


def _count_phrase(n: int) -> str:
    """Счётчик позиций для шаблонов: 0 -> «позиций нет», иначе «N позиций»."""
    return "позиций нет" if n == 0 else f"{n} {_pos(n)}"


def _fmt_item(raw: object) -> str:
    """Позиция голосом: «молоко — 2» / «молоко» (dict или строка)."""
    if isinstance(raw, dict):
        text = str(raw.get("text", "")).strip()
        qty = raw.get("qty")
    else:
        text, qty = str(raw).strip(), None
    if not text:
        return ""
    if qty is not None and str(qty).strip():
        return f"{text} — {qty}"
    return text


def _echo(items: list, limit: int = _ECHO_LIMIT) -> str:
    """Эхо позиций через запятую, не больше limit, дальше «…и ещё N»."""
    shown = list(items)[:limit]
    out = ", ".join(p for p in (_fmt_item(x) for x in shown) if p)
    rest = len(items) - len(shown)
    if rest > 0:
        tail = f"…и ещё {rest} {_pos(rest)}"
        out = f"{out}, {tail}" if out else tail
    return out


def _render_read(lst: dict) -> str:
    """shopping_read: «Список «X»: …» (≤15 позиций, «…и ещё N»);
    вычёркнутые — отдельной репликой «Вычеркнуто: …»."""
    name = lst.get("name") or shopping_store.DEFAULT_LIST_NAME
    items = lst.get("items") or []
    if not items:
        return f"Список «{name}» пуст."
    active = [it for it in items if not it.get("done")]
    done = [it for it in items if it.get("done")]
    # Вычёркнутые показываются всегда (решение 5: отдельной репликой),
    # под них резервируется слоты; активным достаётся остаток лимита.
    shown_done = done[:min(len(done), _READ_DONE_LIMIT)]
    shown_active = active[:_READ_LIMIT - len(shown_done)]
    rest = (len(active) - len(shown_active)) + (len(done) - len(shown_done))
    active_str = ", ".join(_fmt_item(it) for it in shown_active)
    reply = f"Список «{name}»: {active_str or 'активных позиций нет'}"
    if not shown_done and rest > 0:
        reply += f", …и ещё {rest} {_pos(rest)}"
    reply += "."
    if shown_done:
        reply += " Вычеркнуто: " + ", ".join(_fmt_item(it) for it in shown_done)
        if rest > 0:
            reply += f", …и ещё {rest} {_pos(rest)}"
        reply += "."
    return reply


def _update_summary(changes: list) -> str:
    """Что изменилось в shopping_update: «заменено: молоко на кефир, …»."""
    parts: list[str] = []
    for ch in list(changes)[:_ECHO_LIMIT]:
        match = str(ch.get("match", "")).strip()
        if ch.get("op") == "delete":
            parts.append(f"удалено: {match}")
            continue
        added = False
        new_text = ch.get("new_text")
        if isinstance(new_text, str) and new_text.strip():
            parts.append(f"заменено: {match} на {new_text.strip()}")
            added = True
        qty = ch.get("new_qty", ch.get("qty"))
        if qty is not None and str(qty).strip():
            parts.append(f"количество: {match} — {qty}")
            added = True
        if not added:
            parts.append(f"изменено: {match}")
    rest = len(changes) - len(parts)
    out = ", ".join(parts)
    if rest > 0:
        word = _plural(rest, "изменение", "изменения", "изменений")
        out = f"{out}, …и ещё {rest} {word}"
    return out


def _stem(tok: str) -> str:
    """Стем токена: первые 4 символа (короче -- целиком, точное сравн.)."""
    return tok[:4]


def _tokens(text: str) -> list[str]:
    """Токены фразы: casefold, без пунктуации."""
    return _TOKEN_RE.findall(text.casefold())


def _sig_stems(text: str) -> set[str]:
    """Значимые стемы текста: токены >=4 символов, не стоп-слова."""
    return {_stem(t) for t in _tokens(text)
            if len(t) >= 4 and t not in _STOP_WORDS}


def _phrase_candidates(text: str, lists: list[dict]) -> list[dict]:
    """Кандидаты списков по ТОКЕНАМ фразы (задача 24): покрытие значимых
    стемов имени стемами фразы >= _COVER_MIN (2-токенное имя требует
    оба -- 1/2 < 0.6; 1-токенное -- 1/1). Без text -- [] (старые вызовы,
    поведение байт-в-байт прежнее)."""
    phrase_stems = _sig_stems(text)
    if not phrase_stems:
        return []
    out: list[dict] = []
    for x in lists:
        name_stems = _sig_stems(x["name"])
        if name_stems and (len(name_stems & phrase_stems)
                           / len(name_stems)) >= _COVER_MIN:
            out.append(x)
    return out


def _phrase_evidence(tokens: list[str], cand: dict,
                     phrase_stems: set[str], args_named: bool) -> bool:
    """Evidence (задача 24): фраза РЕАЛЬНО называет список, а не товар.

    (alpha) во фразе есть слово-«списк*» (стем «спис»);
    (beta) совпавший стем имени -- в окне <=2 токена после предлога
           («что ПО на неделю» -> «недел» в окне после «по»);
    (gamma) args.list был задан и exact провалился (фраза -- источник).
    Без evidence единственный кандидат НЕ пере-записывает активный
    (контракт: список «Хлеб» существует, фраза «добавь хлеб» args={}
    -> активный «Основной», НЕ список «Хлеб»).
    """
    stems = [_stem(t) for t in tokens]
    if "спис" in set(stems):                                # (alpha)
        return True
    hits = _sig_stems(cand["name"]) & phrase_stems
    for i, tok in enumerate(tokens):                        # (beta)
        if tok in _PREPS:
            for j in (i + 1, i + 2):
                if j < len(stems) and stems[j] in hits:
                    return True
    return args_named                                       # (gamma)


def _ambig_names(cand: list[dict]) -> str:
    """{names} для _Q_AMBIGUOUS (задача 22): дедуп имён; одно имя на
    несколько списков -> ««X» ×N», разные -- plain-список (до 3)."""
    uniq: list[str] = []
    seen: set[str] = set()
    for x in cand:
        key_dedup = x["name"].casefold()
        if key_dedup not in seen:
            seen.add(key_dedup)
            uniq.append(x["name"])
    return (f"«{uniq[0]}» ×{len(cand)}" if len(uniq) == 1
            else ", ".join(uniq[:3]))


async def _resolve_active(
        lists: list[dict]) -> tuple[dict | None, str | None]:
    """Активный список либо вопрос «Нет активного списка...» (общая
    ветка для пустого args.list и синонима «в активный»)."""
    list_id = await shopping_store.get_active()
    for x in lists:
        if x["id"] == list_id:
            return x, None
    return None, _Q_NO_ACTIVE


async def _resolve_target(
        args: dict, text: str = "") -> tuple[dict | None, str | None]:
    """Адресат команды: имя из args["list"] либо активный список.

    Возвращает (список, вопрос-фолбэк) — ровно одно из двух: список найден
    однозначно, либо вопрос (данные не менять, action=none).

    Цепочка приоритетов (задача 24; фраза юзера -- источник истины,
    args.list 4B-модель контаминирует историей, живые факты 12.11):
    1. exact по args.list -- старое поведение, НО если ФРАЗА уникально
       указывает на другой список И есть evidence (_phrase_evidence,
       alpha/beta; gamma неприменим -- exact успешен) -- пере-запись
       (галлюцинация args.list). Без evidence exact стоит: фраза
       «добавь хлеб» при эхе args.list='Основной' не увела бы мутацию
       в список «Хлеб».
    2. без exact: уникальный кандидат по токенам ФРАЗЫ с evidence
       (_phrase_evidence) -- главнее активного списка.
    3. несколько кандидатов фразы -- существующий ambiguous-путь.
    4. ни одного кандидата (или без evidence) -- прежняя цепочка:
       подстрока -> ambiguous -> синонимы «активный» -> активный.

    Синонимы «активный»/«в активный»/«активный список» -- прежние
    (задача 22): резолвятся в активный, ТОЛЬКО когда настоящего списка
    с таким именем нет (точное/подстрочное совпадение приоритетно).
    STT склоняет («по списку покупок» не подстрока «Список покупок»),
    поэтому фраза сравнивается по 4-символьным стемам токенов
    (_phrase_candidates). text="" (старые вызовы) -- поведение
    байт-в-байт прежнее.
    """
    raw = args.get("list")
    if raw is not None and not isinstance(raw, str):
        return None, _Q_BAD_LIST
    name = (raw or "").strip()
    lists = await shopping_store.list_lists()
    # Кандидаты по ФРАЗЕ (при text="" их нет -- старое поведение).
    p_tokens = _tokens(text)
    phrase_cand = _phrase_candidates(text, lists) if p_tokens else []
    if name:
        key = name.casefold()
        exact = [x for x in lists if x["name"].casefold() == key]
        if exact:
            # (1) exact есть: фраза молчит/согласна/многокандидатна ->
            # старое поведение. Уникальный ДРУГОЙ кандидат -- галлюцинация
            # args.list, но пере-записываем ТОЛЬКО при evidence (alpha/
            # beta): фраза «добавь хлеб» при точном эхе args.list='Основной'
            # не должна уводить мутацию в список «Хлеб» (гвард коллизии
            # «имя списка = товар»; gamma неприменим -- exact успешен).
            if (len(phrase_cand) == 1
                    and exact[0]["id"] != phrase_cand[0]["id"]
                    and _phrase_evidence(p_tokens, phrase_cand[0],
                                         _sig_stems(text),
                                         args_named=False)):
                active_id = await shopping_store.get_active()
                log.info("shopping: resolve override по фразе: %r "
                         "(args.list=%r, активный=%r)",
                         phrase_cand[0]["name"], name, active_id)
                return phrase_cand[0], None
            if len(exact) == 1:
                return exact[0], None
            return None, _Q_AMBIGUOUS.format(names=_ambig_names(exact))
        # (2) args.list задан, exact провалился: уникальный кандидат
        # фразы с evidence (gamma автоматически True) главнее подстроки
        # args.list (контаминированной).
        if (len(phrase_cand) == 1
                and _phrase_evidence(p_tokens, phrase_cand[0],
                                     _sig_stems(text), args_named=True)):
            active_id = await shopping_store.get_active()
            log.info("shopping: resolve override по фразе: %r "
                     "(args.list=%r, активный=%r)",
                     phrase_cand[0]["name"], name, active_id)
            return phrase_cand[0], None
        cand = [x for x in lists if key in x["name"].casefold()
                or x["name"].casefold() in key]
        if len(cand) == 1:
            return cand[0], None
        if len(cand) > 1:
            return None, _Q_AMBIGUOUS.format(names=_ambig_names(cand))
        if key in _ACTIVE_WORDS:
            return await _resolve_active(lists)
        return None, _Q_NOT_FOUND.format(name=name)
    # (2) args.list пуст: уникальный кандидат фразы + evidence --
    # главнее активного; несколько кандидатов -- ambiguous.
    if (len(phrase_cand) == 1
            and _phrase_evidence(p_tokens, phrase_cand[0],
                                 _sig_stems(text), args_named=False)):
        active_id = await shopping_store.get_active()
        log.info("shopping: resolve override по фразе: %r "
                 "(args.list=None, активный=%r)",
                 phrase_cand[0]["name"], active_id)
        return phrase_cand[0], None
    if len(phrase_cand) >= 2:
        return None, _Q_AMBIGUOUS.format(names=_ambig_names(phrase_cand))
    return await _resolve_active(lists)


def _completion(lst: dict) -> tuple[str, bool, str] | None:
    """В списке не осталось позиций с done=false -> вопрос об удалении
    + pending shopping_delete (реально удалит /confirm при yes:true)."""
    if any(not it.get("done") for it in lst.get("items") or []):
        return None
    question = "По списку всё. Удалить список?"
    token = _new_pending("shopping_delete",
                         {"list_id": lst["id"], "name": lst["name"]},
                         question)
    return question, True, token


async def _shopping_create(decision: llm.Decision) -> tuple[str, bool, str | None]:
    """create (задачи 20/22, единый контракт с llm/decide).

    items не названы (None или []) -- создаём ПУСТОЙ список (store.create
    + set_active, те же вызовы) и отвечаем шаблоном-вопросом «Теперь
    назови, что положить». Непустой items -- прежнее поведение байт-в-байт
    (создать с позициями + эхо). Мусор вместо списка (decide обычно
    отсекает раньше) -- защитный фолбэк action=none, данные не меняются.

    Дубль-гард (задача 22, ТОЛЬКО голос): список с тем же именем
    (args.name либо дефолт стора; normalize -- strip + casefold) уже
    существует -> НОВЫЙ НЕ создаём: включаем существующий активным и
    отвечаем «уже есть — N позиций, сделал его активным». Позиции из args
    при дубле отбрасываются. При нескольких одноимённых берём ПЕРВЫЙ
    по created_at (порядок list_lists). REST POST /shopping/lists гард
    НЕ проходит сознательно: там дубль имени -- осознанный выбор
    пользователя в UI (см. test_shopping_rest).
    """
    items = decision.args.get("items")
    empty = items is None or (isinstance(items, list) and not items)
    if not empty and not isinstance(items, list):
        return _fallback(decision, "Что купить? Перечисли позиции для списка.")
    name = decision.args.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    try:
        # Дубль-гард: normalize strip+casefold; при x2 одноимённых --
        # первый по created_at (детерминизм задокументирован в докстринге).
        want = (name or shopping_store.DEFAULT_LIST_NAME).strip().casefold()
        twin = next((x for x in await shopping_store.list_lists()
                     if x["name"].strip().casefold() == want), None)
        if twin is not None:
            # Голос не разрешает дубль имён (иначе ambiguity при
            # shopping_read): делаем существующий список активным.
            await shopping_store.set_active(twin["id"])
            reply = _Q_DUP_ACTIVE.format(name=twin["name"],
                                         cnt=_count_phrase(
                                             len(twin["items"])))
            return reply, False, None
        lst = await shopping_store.create(name=name,
                                          items=[] if empty else items)
        # Новый список сразу становится активным (решение 2 плана).
        await shopping_store.set_active(lst["id"])
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_create: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_create failed")
        return _fallback(decision, _STORE_FAIL)
    if empty:
        # Позиций не назвали: имя -- args.name или дефолтное имя стора
        # (lst["name"]), список уже создан и активен.
        return _EMPTY_CREATE.format(name=lst["name"]), False, None
    summary = _echo(items)
    reply = (f"Список «{lst['name']}» создан и стал активным: {summary}."
             if summary else f"Список «{lst['name']}» создан и стал активным.")
    return reply, False, None


async def _shopping_lists(decision: llm.Decision) -> tuple[str, bool, str | None]:
    """lists (задача 22, args {} -- канон зоны A): перечислить списки.

    Только чтение (не мутирует), pending/confirm не нужны. Шаблон
    <=2 предложений: «Списки: «X» — N позиций, «Y» — M позиций.» и при
    выбранном активном вторым предложением «Активный — «X».» (если
    активный список удалён/сброшен -- не врём, отметки нет). Пусто --
    приглашение создать первый.
    """
    try:
        lists = await shopping_store.list_lists()   # порядок по created_at
        active_id = await shopping_store.get_active()
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_lists: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_lists failed")
        return _fallback(decision, _STORE_FAIL)
    if not lists:
        return _Q_EMPTY_LISTS, False, None
    items = ", ".join(f"«{x['name']}» — {_count_phrase(len(x['items']))}"
                      for x in lists)
    parts = [f"Списки: {items}"]
    cur = next((x for x in lists if x["id"] == active_id), None) \
        if active_id is not None else None
    if cur is not None:
        parts.append(f"Активный — «{cur['name']}»")
    return ". ".join(parts) + ".", False, None


async def _shopping_add(decision: llm.Decision,
                        text: str = "") -> tuple[str, bool, str | None]:
    items = decision.args.get("items")
    if not isinstance(items, list) or not items:
        return _fallback(decision,
                         "Что добавить в список? Скажи, что купить.")
    try:
        lst, question = await _resolve_target(decision.args, text)
        if lst is None:
            return _fallback(decision, question)
        lst = await shopping_store.add_items(lst["id"], items)
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_add: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_add failed")
        return _fallback(decision, _STORE_FAIL)
    return f"В список «{lst['name']}» добавлено: {_echo(items)}.", False, None


async def _shopping_read(decision: llm.Decision,
                         text: str = "") -> tuple[str, bool, str | None]:
    try:
        lst, question = await _resolve_target(decision.args, text)
        if lst is None:
            return _fallback(decision, question)
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_read: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_read failed")
        return _fallback(decision, _STORE_FAIL)
    return _render_read(lst), False, None


async def _shopping_update(decision: llm.Decision,
                           text: str = "") -> tuple[str, bool, str | None]:
    changes = decision.args.get("changes")
    if not isinstance(changes, list) or not changes:
        return _fallback(decision,
                         "Что изменить в списке? Скажи, какую позицию "
                         "и на что заменить.")
    try:
        lst, question = await _resolve_target(decision.args, text)
        if lst is None:
            return _fallback(decision, question)
        lst = await shopping_store.update_items(lst["id"], changes)
        done = _completion(lst)
        if done is not None:
            return done
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_update: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_update failed")
        return _fallback(decision, _STORE_FAIL)
    return (f"В списке «{lst['name']}» {_update_summary(changes)}.",
            False, None)


async def _shopping_check(decision: llm.Decision,
                          text: str = "") -> tuple[str, bool, str | None]:
    items = decision.args.get("items")
    if not isinstance(items, list) or not items:
        return _fallback(decision,
                         "Какие позиции вычеркнуть? Скажи, что уже куплено.")
    texts = [str(i.get("text", "")).strip() for i in items]
    texts = [t for t in texts if t]
    if not texts:
        return _fallback(decision,
                         "Какие позиции вычеркнуть? Скажи, что уже куплено.")
    try:
        lst, question = await _resolve_target(decision.args, text)
        if lst is None:
            return _fallback(decision, question)
        # done=true, позиция НЕ удаляется (только check_items).
        lst = await shopping_store.check_items(lst["id"], texts)
        done = _completion(lst)
        if done is not None:
            return done
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_check: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_check failed")
        return _fallback(decision, _STORE_FAIL)
    return f"Вычеркнуто: {_echo(texts)}.", False, None


async def _shopping_delete(decision: llm.Decision,
                           text: str = "") -> tuple[str, bool, str | None]:
    # Как call: только pending, ничего не удаляем; /confirm при yes:true
    # удалит (ветка server.py), при yes:false — «Хорошо, отменил.», список цел.
    try:
        lst, question = await _resolve_target(decision.args, text)
        if lst is None:
            return _fallback(decision, question)
    except shopping_store.ShoppingStoreError as exc:
        log.warning("shopping_delete: %s", exc)
        return _fallback(decision, str(exc))
    except Exception:
        log.exception("shopping_delete failed")
        return _fallback(decision, _STORE_FAIL)
    ask = (f"Удалить список «{lst['name']}»? "
           f"Скажи «да» или «отмена».")
    token = _new_pending("shopping_delete",
                         {"list_id": lst["id"], "name": lst["name"]}, ask)
    return ask, True, token
