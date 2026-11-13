"""Фиксированный набор фраз TTS A/B харнесса (задача 1; ТЗ §3.1).

14 фраз, 5 категорий; элемент набора — (id, category, text).
Категории и объём менять НЕЛЬЗЯ (счётчики 4/3/3/2/2), текст
отредактирован только в рамках допущения 9 ТЗ. Текст уходит в движки
КАК ЕСТЬ — без clean_for_speech (сравнивается чистый синтез,
в results.json сохраняется точный текст).

Категории: s — короткие confirm-реплики, m — средние (чтение списка),
l — длинные (ответ поиска), n — числа/даты/латиница,
h — ударения/омографы.
"""
from collections.abc import Iterable
from typing import NamedTuple


class Phrase(NamedTuple):
    """Элемент набора: (id, category, text) — кортеж из 3 строк."""

    phrase_id: str
    category: str
    text: str


# Ровно 5 категорий и обязательный объём каждой (менять нельзя).
CATEGORY_COUNTS: dict[str, int] = {"s": 4, "m": 3, "l": 3, "n": 2, "h": 2}

# Человеческие названия категорий — для отчёта (задача 5, report.md).
CATEGORY_TITLES: dict[str, str] = {
    "s": "короткие confirm-реплики",
    "m": "средние — чтение списка",
    "l": "длинные — ответ поиска",
    "n": "числа/даты/латиница",
    "h": "ударения/омографы",
}

# Ударения в h-фразах (выбор варианта ЗАФИКСИРОВАН): помечены
# комбинируемым ударением U+0301 сразу после ударной гласной
# («замО́к»/«зА́мок», «мукА́»/«мУ́ка») — пара омографов сохранена в одной
# фразе (допущение 9 ТЗ). В исходнике ниже — escape \u0301, чтобы
# комбинируемый символ гарантированно присутствовал в файле (и не был
# потерян при перекодировках). Альтернатива ТЗ — переформулировать фразу
# так, чтобы движок сам угадал ударение, — НЕ использована: явные метки
# делают тест детерминированным и сразу показывают, умеет ли движок
# вообще читать ударения. Скобочные аннотации «(замок/замок)» /
# «(мука/мука)» из исходной таблицы ТЗ в текст НЕ включены.
_PHRASES: list[Phrase] = [
    # короткие confirm-реплики (s, 4)
    Phrase("s1", "s", "Готово, держи!"),
    Phrase("s2", "s", "Список создан. Что положить?"),
    Phrase("s3", "s", "Хорошо, отменил."),
    Phrase("s4", "s", "Позиция вычеркнута."),
    # средние — чтение списка (m, 3)
    Phrase("m1", "m", "Список «Покупки»: хлеб, молоко, сахар, гречка."),
    Phrase("m2", "m", "В списке «На неделю» семь позиций. Что добавить?"),
    Phrase("m3", "m", "В список добавлено: наушники, кабель, батарейка."),
    # длинные — ответ поиска (l, 3)
    Phrase("l1", "l",
        "Санкт-Петербург — город на Неве. Основан в 1703 году Петром "
        "Первым. Население около пяти с половиной миллионов человек."),
    Phrase("l2", "l",
        "Завтра в Москве будет переменная облачность, до минус двух. "
        "Ожидается северный ветер, до пяти метров в секунду. "
        "Одевайтесь теплее."),
    Phrase("l3", "l",
        "Рецепт простой: обжарить лук, добавить муку и помешать. "
        "Томить на слабом огне около десяти минут."),
    # числа/даты/латиница (n, 2)
    Phrase("n1", "n", "Встреча в 14:30 на улице Ленина."),
    Phrase("n2", "n", "Ошибка HTTP 404, но Wi-Fi работает, перезапускаю "
                      "роутер."),
    # ударения/омографы (h, 2)
    Phrase("h1", "h",
        "Замо\u0301к на двери новый, а за\u0301мок древний."),
    Phrase("h2", "h",
        "Мука\u0301 для блинов просеяна, и эта му\u0301ка оказалась "
        "лучшей."),
]


def validate_phrases(
    phrases: Iterable[tuple[str, str, str]],
) -> list[Phrase]:
    """Проверить набор фраз и вернуть его как list[Phrase].

    Правила (задача 1): уникальные id; непустой (после strip) id и
    текст; ровно 5 категорий; счётчики по категориям == CATEGORY_COUNTS
    (4/3/3/2/2). Любое нарушение — ValueError с указанием фразы.
    """
    items: list[Phrase] = []
    seen_ids: set[str] = set()
    counts: dict[str, int] = {}
    for index, item in enumerate(phrases):
        if not isinstance(item, tuple) or len(item) != 3:
            raise ValueError(
                f"phrase #{index}: expected (id, category, text) 3-tuple"
            )
        phrase = Phrase(item[0], item[1], item[2])
        if (not isinstance(phrase.phrase_id, str)
                or not phrase.phrase_id.strip()):
            raise ValueError(f"phrase #{index}: empty id")
        if phrase.phrase_id in seen_ids:
            raise ValueError(
                f"phrase #{index}: duplicate id {phrase.phrase_id!r}"
            )
        seen_ids.add(phrase.phrase_id)
        if not isinstance(phrase.text, str) or not phrase.text.strip():
            raise ValueError(f"phrase {phrase.phrase_id}: empty text")
        if phrase.category not in CATEGORY_COUNTS:
            raise ValueError(
                f"phrase {phrase.phrase_id}: "
                f"unknown category {phrase.category!r}"
            )
        counts[phrase.category] = counts.get(phrase.category, 0) + 1
        items.append(phrase)
    missing = sorted(set(CATEGORY_COUNTS) - set(counts))
    extra = sorted(set(counts) - set(CATEGORY_COUNTS))
    if missing or extra:
        raise ValueError(
            f"categories mismatch: missing={missing} extra={extra}"
        )
    for category, expected in CATEGORY_COUNTS.items():
        actual = counts[category]
        if actual != expected:
            raise ValueError(
                f"category {category!r}: {actual} phrases, "
                f"expected {expected}"
            )
    return items


# Самопроверка набора на импорте: контракт (14 фраз, 5 категорий,
# 4/3/3/2/2) гарантирован даже без тестов.
PHRASES: list[Phrase] = validate_phrases(_PHRASES)

PHRASE_BY_ID: dict[str, Phrase] = {p.phrase_id: p for p in PHRASES}
