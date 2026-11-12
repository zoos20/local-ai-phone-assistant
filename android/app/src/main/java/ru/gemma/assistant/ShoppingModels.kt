package ru.gemma.assistant

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject

/**
 * DTO и чистые функции списков покупок (фича «Списки покупок», задача 6).
 *
 * Только чистые функции и org.json — без Android-зависимостей, чтобы JUnit
 * гонял их без Robolectric (задача 9). Контракт REST (см. tasklist):
 *  - `POST /shopping/lists`    тело `{"name"?}` -> 200/201 объект созданного списка
 *    (пустое/отсутствие name -> сервер ставит дефолт «Список покупок», [createBody]);
 *  - `GET /shopping/lists`     -> 200 `{"lists": [{id, name, items, created_at, updated_at}]}`
 *  - `GET /shopping/lists/{id}` -> 200 объект списка | 404
 *  - `PATCH /shopping/lists/{id}` тело `{"name"?}` (переименование, [renameBody])
 *    или `{"items":[...]}` (полная замена) -> 200 | 400 | 404 | 413
 *  - `DELETE /shopping/lists/{id}` -> 200/204 | 404
 */

/** Позиция списка покупок (поле REST: `{id, text, qty?, done}`). */
data class ShoppingItem(
    val id: String,
    val text: String,
    /** Опциональное количество: сервер может отдать число или строку — нормализуем в строку. */
    val qty: String? = null,
    val done: Boolean = false,
)

/** Список покупок (поле REST: `{id, name, items, created_at, updated_at}`). */
data class ShoppingList(
    val id: String,
    val name: String,
    val items: List<ShoppingItem> = emptyList(),
    /** ISO-строка `updated_at` (фолбэк на `created_at`, иначе ""). */
    val updatedAt: String = "",
)

/**
 * Единый результат shopping-вызовов [ServerClient]: HTTP-код (0 — сетевая
 * ошибка/таймаут, не глотается) + уже разобранный payload, чтобы Activity
 * не парсил raw-тело сама.
 *
 * Маппинг в состояние экрана — чистая функция [screenState]:
 *  - «все списки»: `screenState(code, lists.isNotEmpty())`;
 *  - «детали»: `screenState(code, list != null)` (200 без позиций — это
 *    валидные данные, а не EMPTY: список открыт и пустой).
 */
data class ShoppingRes(
    /** HTTP-код; [NET_ERROR] — сетевая ошибка/таймаут. */
    val code: Int,
    /** Заполнен для `GET /shopping/lists`. */
    val lists: List<ShoppingList> = emptyList(),
    /** Заполнен для `GET`/`PATCH .../{id}`, если тело разобрано. */
    val list: ShoppingList? = null,
) {
    val ok: Boolean get() = code in 200..299

    /** Состояние экрана «все списки покупок». */
    fun listsState(): ScreenState = screenState(code, lists.isNotEmpty())

    companion object {
        /** Код результата при сетевой ошибке/таймауте (→ NO_CONNECTION). */
        const val NET_ERROR = 0
    }
}

/** Состояния экранов списков покупок (маппинг HTTP-результата, чистая функция [screenState]). */
enum class ScreenState { DATA, EMPTY, NO_CONNECTION, NOT_FOUND }

/**
 * HTTP-код -> состояние экрана: 2xx с данными → DATA, 2xx пусто → EMPTY,
 * 404 → NOT_FOUND, всё остальное (400/401/413/5xx и сетевая ошибка [ShoppingRes.NET_ERROR])
 * → NO_CONNECTION.
 *
 * Маппинг для ЧТЕНИЯ и состояния ПОСЛЕ рефетча. Успешность мутаций
 * (PATCH/DELETE) определяется до рефетча по `ShoppingRes.ok` (2xx), А НЕ
 * по наличию payload: успех → рефетч [parseList]/[parseLists] и состояние
 * по коду, иначе — по коду ошибки.
 */
fun screenState(code: Int, hasItems: Boolean): ScreenState = when {
    code == 404 -> ScreenState.NOT_FOUND
    code in 200..299 -> if (hasItems) ScreenState.DATA else ScreenState.EMPTY
    else -> ScreenState.NO_CONNECTION
}

/**
 * Разбор `GET /shopping/lists`: `{"lists": [...]}` (канон) или голый массив.
 * Битый/неполный JSON → пустой список без краша; списки без `id` пропускаются.
 */
fun parseLists(json: String): List<ShoppingList> {
    val src = json.trim()
    if (src.isEmpty()) return emptyList()
    val arr = try {
        if (src.startsWith("[")) JSONArray(src)
        else JSONObject(src).optJSONArray("lists")
    } catch (_: JSONException) {
        null
    } ?: return emptyList()
    val out = ArrayList<ShoppingList>(arr.length())
    for (i in 0 until arr.length()) {
        parseListObject(arr.optJSONObject(i))?.let { out.add(it) }
    }
    return out
}

/**
 * Разбор объекта одного списка (`GET`/`PATCH /shopping/lists/{id}`).
 * Битый/неполный JSON или отсутствие `id` → null без краша.
 */
fun parseList(json: String): ShoppingList? {
    val src = json.trim()
    if (src.isEmpty()) return null
    return try {
        parseListObject(JSONObject(src))
    } catch (_: JSONException) {
        null
    }
}

/**
 * Тело `PATCH /shopping/lists/{id}`: `{"items": [...]}` — полная замена
 * массива позиций (семантика last-write-wins, решение 3 плана). `qty`
 * включается только если задан; `id` — только непустой, иначе сервер
 * сгенерирует новый (решение 3: «нет в теле — новая позиция»).
 */
fun patchBody(items: List<ShoppingItem>): String {
    val arr = JSONArray()
    for (it in items) {
        val o = JSONObject()
        if (it.id.isNotEmpty()) o.put("id", it.id)
        o.put("text", it.text)
        if (it.qty != null) o.put("qty", it.qty)
        o.put("done", it.done)
        arr.put(o)
    }
    return JSONObject().put("items", arr).toString()
}

/**
 * Тело `POST /shopping/lists` (создание из UI, задача 18): `{"name": …}`.
 * Имя тримится; пустое/null после трима — ключ `name` НЕ добавляется
 * (`{}`): сервер сам ставит дефолт «Список покупок» — семантика как у
 * голосовой ветки `shopping_create`, где `name` не пишется.
 */
fun createBody(name: String?): String {
    val n = name?.trim().orEmpty()
    val root = JSONObject()
    if (n.isNotEmpty()) root.put("name", n)
    return root.toString()
}

/**
 * Тело `PATCH /shopping/lists/{id}` для переименования (спека §3.1):
 * `{"name": …}` без `items` — частичное тело меняет имя, позиции не трогает.
 */
fun renameBody(name: String): String =
    JSONObject().put("name", name.trim()).toString()

// ------------------------------------------------------------------
// Внутренние разборщики (возвращают null вместо исключений)
// ------------------------------------------------------------------

private fun str(o: JSONObject, key: String): String =
    if (!o.has(key) || o.isNull(key)) "" else o.optString(key, "")

private fun parseListObject(o: JSONObject?): ShoppingList? {
    if (o == null) return null
    val id = str(o, "id")
    if (id.isEmpty()) return null
    val items = ArrayList<ShoppingItem>()
    val arr = o.optJSONArray("items")
    if (arr != null) {
        for (i in 0 until arr.length()) {
            parseItem(arr.optJSONObject(i))?.let { items.add(it) }
        }
    }
    val updated = str(o, "updated_at").ifEmpty { str(o, "created_at") }
    return ShoppingList(id = id, name = str(o, "name"), items = items, updatedAt = updated)
}

private fun parseItem(o: JSONObject?): ShoppingItem? {
    if (o == null) return null
    val text = str(o, "text")
    if (text.isBlank()) return null // не позиция — пропуск без краша
    return ShoppingItem(
        id = str(o, "id"),
        text = text,
        qty = str(o, "qty").ifEmpty { null },
        done = !o.isNull("done") && o.optBoolean("done", false),
    )
}
