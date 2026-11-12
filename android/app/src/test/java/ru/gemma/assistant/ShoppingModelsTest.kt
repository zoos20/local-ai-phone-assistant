package ru.gemma.assistant

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JUnit-тесты чистых функций фичи «Списки покупок» (задача 9):
 * разбор JSON → DTO ([parseLists]/[parseList]), маппинг HTTP-кодов в
 * состояние экрана ([screenState]), тело PATCH ([patchBody]) и маппинг
 * результата [ShoppingRes].
 *
 * Только чистые функции и org.json (реальная реализация из
 * testImplementation) — без Robolectric, сети и Android-зависимостей.
 */
class ShoppingModelsTest {

    /** Канонический ответ `GET /shopping/lists` (контракт решения 1 плана). */
    private val canonicalJson = """
        {"lists":[
          {"id":"l1","name":"Продукты","items":[
             {"id":"i1","text":"Молоко","qty":2,"done":true},
             {"id":"i2","text":"Хлеб","done":false}],
           "created_at":"2026-11-01T10:00:00Z","updated_at":"2026-11-02T11:00:00Z"},
          {"id":"l2","name":"Аптека","items":[],
           "created_at":"2026-11-03T09:00:00Z"}
        ]}
    """.trimIndent()

    // ------------------------------------------------------------------
    // 1. parseLists: валидный канонический JSON -> DTO
    // ------------------------------------------------------------------

    @Test fun canonicalJsonParsesBothLists() {
        val lists = parseLists(canonicalJson)
        assertEquals(2, lists.size)
        assertEquals("l1", lists[0].id)
        assertEquals("Продукты", lists[0].name)
        assertEquals(2, lists[0].items.size)
        assertEquals("l2", lists[1].id)
        assertEquals("Аптека", lists[1].name)
    }

    @Test fun itemFieldsAreMapped() {
        val item = parseLists(canonicalJson)[0].items[0]
        assertEquals("i1", item.id)
        assertEquals("Молоко", item.text)
        assertEquals("2", item.qty) // число 2 -> строка "2"
        assertTrue(item.done)
    }

    @Test fun itemDoneFalseAndMissingQtyAreMapped() {
        val item = parseLists(canonicalJson)[0].items[1]
        assertEquals("Хлеб", item.text)
        assertFalse(item.done)
        assertNull(item.qty)
    }

    @Test fun quantityStringIsPreserved() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N","items":[{"id":"i","text":"t","qty":"3 шт"}]}]}""")
        assertEquals("3 шт", lists[0].items[0].qty)
    }

    @Test fun quantityEmptyStringIsNull() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N","items":[{"id":"i","text":"t","qty":""}]}]}""")
        assertNull(lists[0].items[0].qty)
    }

    @Test fun quantityJsonNullIsNull() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N","items":[{"id":"i","text":"t","qty":null}]}]}""")
        assertNull(lists[0].items[0].qty)
    }

    @Test fun updatedAtIsPreferredOverCreatedAt() {
        val lists = parseLists(canonicalJson)
        assertEquals("2026-11-02T11:00:00Z", lists[0].updatedAt)
    }

    @Test fun updatedAtFallsBackToCreatedAt() {
        val lists = parseLists(canonicalJson)
        assertEquals("2026-11-03T09:00:00Z", lists[1].updatedAt)
    }

    @Test fun bothTimestampsMissingGiveEmptyUpdatedAt() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N"}]}""")
        assertEquals("", lists[0].updatedAt)
    }

    @Test fun itemsMissingGiveEmptyList() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N"}]}""")
        assertTrue(lists[0].items.isEmpty())
    }

    // ------------------------------------------------------------------
    // 2. parseLists: толерантность (битый/неполный JSON, голый массив)
    // ------------------------------------------------------------------

    @Test fun bareArrayIsAccepted() {
        // парсер поддерживает и голый массив, и канон `{"lists": [...]}`
        val lists = parseLists("""[{"id":"l1","name":"Продукты"}]""")
        assertEquals(1, lists.size)
        assertEquals("l1", lists[0].id)
    }

    @Test fun brokenJsonGivesEmptyListWithoutCrash() {
        assertTrue(parseLists("""{"lists":[{"id":"l","na""").isEmpty())
    }

    @Test fun notAnObjectGivesEmptyList() {
        // голый массив поддержан, но его элементы обязаны быть объектами
        assertTrue(parseLists("""["l1","l2"]""").isEmpty())
        assertTrue(parseLists("42").isEmpty())
        assertTrue(parseLists("null").isEmpty())
    }

    @Test fun emptyAndBlankStringGiveEmptyList() {
        assertTrue(parseLists("").isEmpty())
        assertTrue(parseLists("   \n  ").isEmpty())
    }

    @Test fun missingListsKeyGivesEmptyList() {
        assertTrue(parseLists("""{"other":1}""").isEmpty())
    }

    @Test fun listsNullGivesEmptyList() {
        assertTrue(parseLists("""{"lists":null}""").isEmpty())
    }

    @Test fun listsNotArrayGivesEmptyList() {
        assertTrue(parseLists("""{"lists":"nope"}""").isEmpty())
        assertTrue(parseLists("""{"lists":{"id":"l"}}""").isEmpty())
    }

    @Test fun listWithoutIdIsSkipped() {
        val lists = parseLists("""{"lists":[{"name":"Без id"},{"id":"l","name":"С id"}]}""")
        assertEquals(1, lists.size)
        assertEquals("l", lists[0].id)
    }

    @Test fun nonObjectListElementIsSkipped() {
        val lists = parseLists("""{"lists":[1,"x",null,{"id":"l"}]}""")
        assertEquals(1, lists.size)
        assertEquals("l", lists[0].id)
    }

    @Test fun itemWithoutTextIsSkipped() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N","items":[{"id":"i1","qty":2,"done":true}]}]}""")
        assertTrue(lists[0].items.isEmpty())
    }

    @Test fun itemWithBlankTextIsSkipped() {
        val lists = parseLists("""{"lists":[{"id":"l","name":"N","items":[{"id":"i1","text":"   "}]}]}""")
        assertTrue(lists[0].items.isEmpty())
    }

    // ------------------------------------------------------------------
    // 3. parseList: объект одного списка
    // ------------------------------------------------------------------

    @Test fun parseListMapsSingleObject() {
        val list = parseList(
            """{"id":"l1","name":"Продукты","items":[{"id":"i1","text":"Молоко","qty":2,"done":true}],
               "created_at":"2026-11-01T10:00:00Z","updated_at":"2026-11-02T11:00:00Z"}""".trimIndent()
        )
        requireNotNull(list)
        assertEquals("l1", list.id)
        assertEquals("Продукты", list.name)
        assertEquals(1, list.items.size)
        assertEquals("2", list.items[0].qty)
        assertTrue(list.items[0].done)
        assertEquals("2026-11-02T11:00:00Z", list.updatedAt)
    }

    @Test fun parseListBrokenJsonGivesNull() {
        assertNull(parseList("""{"id":"l1","na"""))
    }

    @Test fun parseListEmptyGivesNull() {
        assertNull(parseList(""))
        assertNull(parseList("    "))
    }

    @Test fun parseListWithoutIdGivesNull() {
        assertNull(parseList("""{"name":"Без id","items":[]}"""))
    }

    @Test fun parseListNonObjectGivesNull() {
        assertNull(parseList("""[{"id":"l1"}]"""))
        assertNull(parseList("42"))
    }

    // ------------------------------------------------------------------
    // 4. screenState(code, hasItems): все ветки
    // ------------------------------------------------------------------

    @Test fun successWithDataIsData() {
        assertEquals(ScreenState.DATA, screenState(200, true))
    }

    @Test fun otherSuccessWithDataIsData() {
        // ветка `code in 200..299`
        assertEquals(ScreenState.DATA, screenState(201, true))
    }

    @Test fun successWithoutDataIsEmpty() {
        assertEquals(ScreenState.EMPTY, screenState(200, false))
        assertEquals(ScreenState.EMPTY, screenState(204, false))
    }

    @Test fun notFoundBeatsData() {
        // 404 проверяется раньше 2xx-ветки: даже с payload — NOT_FOUND
        assertEquals(ScreenState.NOT_FOUND, screenState(404, true))
        assertEquals(ScreenState.NOT_FOUND, screenState(404, false))
    }

    @Test fun unauthorizedIsNoConnection() {
        assertEquals(ScreenState.NO_CONNECTION, screenState(401, true))
        assertEquals(ScreenState.NO_CONNECTION, screenState(401, false))
    }

    @Test fun payloadTooLargeIsNoConnection() {
        assertEquals(ScreenState.NO_CONNECTION, screenState(413, false))
    }

    @Test fun serverErrorIsNoConnection() {
        assertEquals(ScreenState.NO_CONNECTION, screenState(500, false))
        assertEquals(ScreenState.NO_CONNECTION, screenState(503, true))
    }

    @Test fun otherClientErrorIsNoConnection() {
        // 400 (битое тело PATCH) и прочие коды вне 2xx/404
        assertEquals(ScreenState.NO_CONNECTION, screenState(400, false))
        assertEquals(ScreenState.NO_CONNECTION, screenState(302, false))
    }

    @Test fun networkErrorIsNoConnection() {
        // ShoppingRes.NET_ERROR (0) — сетевая ошибка/таймаут
        assertEquals(ScreenState.NO_CONNECTION, screenState(ShoppingRes.NET_ERROR, false))
        assertEquals(ScreenState.NO_CONNECTION, screenState(0, true))
    }

    // ------------------------------------------------------------------
    // 5. patchBody: roundtrip-сериализация `{"items": [...]}`
    // ------------------------------------------------------------------

    @Test fun patchBodyRoundtripPreservesFields() {
        val body = patchBody(
            listOf(
                ShoppingItem(id = "i1", text = "Молоко", qty = "2", done = true),
                ShoppingItem(id = "i2", text = "Хлеб", done = false),
            )
        )
        val root = JSONObject(body)
        assertTrue(root.has("items"))
        assertEquals(1, root.length()) // только ключ items
        val arr = root.getJSONArray("items")
        assertEquals(2, arr.length())

        val first = arr.getJSONObject(0)
        assertEquals("i1", first.getString("id"))
        assertEquals("Молоко", first.getString("text"))
        assertEquals("2", first.getString("qty"))
        assertTrue(first.getBoolean("done"))

        val second = arr.getJSONObject(1)
        assertEquals("i2", second.getString("id"))
        assertFalse(second.getBoolean("done"))
        assertFalse(second.has("qty"))
    }

    @Test fun patchBodyWithoutIdOmitsKey() {
        // нет id в теле -> сервер сгенерирует новый (решение 3 плана)
        val body = patchBody(listOf(ShoppingItem(id = "", text = "Яйца")))
        val item = JSONObject(body).getJSONArray("items").getJSONObject(0)
        assertFalse(item.has("id"))
        assertEquals("Яйца", item.getString("text"))
        assertTrue(item.has("done")) // done сериализуется всегда
        assertFalse(item.getBoolean("done")) // дефолт done=false
    }

    @Test fun patchBodyQtyIsOptional() {
        val body = patchBody(
            listOf(
                ShoppingItem(id = "i1", text = "а", qty = null),
                ShoppingItem(id = "i2", text = "б", qty = "5"),
            )
        )
        val arr = JSONObject(body).getJSONArray("items")
        assertFalse(arr.getJSONObject(0).has("qty"))
        assertEquals("5", arr.getJSONObject(1).getString("qty"))
    }

    @Test fun patchBodyEmptyListGivesEmptyItemsArray() {
        val root = JSONObject(patchBody(emptyList()))
        assertTrue(root.has("items"))
        assertEquals(0, root.getJSONArray("items").length())
    }

    @Test fun patchBodyDoneFalseSurvivesRoundtrip() {
        val body = patchBody(listOf(ShoppingItem(id = "i1", text = "t", done = false)))
        val item = JSONObject(body).getJSONArray("items").getJSONObject(0)
        assertTrue(item.has("done"))
        assertFalse(item.getBoolean("done"))
    }

    @Test fun patchBodyEscapedTextSurvivesRoundtrip() {
        val text = "Сметана \"высшая\"\nмарка «ёлки» — №5"
        val body = patchBody(listOf(ShoppingItem(id = "i1", text = text)))
        val item = JSONObject(body).getJSONArray("items").getJSONObject(0)
        assertEquals(text, item.getString("text"))
    }

    // ------------------------------------------------------------------
    // 6. ShoppingRes: ok / listsState()
    // ------------------------------------------------------------------

    @Test fun okIsTrueFor2xx() {
        assertTrue(ShoppingRes(200).ok)
        assertTrue(ShoppingRes(201).ok)
        assertTrue(ShoppingRes(204).ok)
        assertTrue(ShoppingRes(299).ok)
    }

    @Test fun okIsFalseForErrorsAndNetwork() {
        assertFalse(ShoppingRes(400).ok)
        assertFalse(ShoppingRes(401).ok)
        assertFalse(ShoppingRes(404).ok)
        assertFalse(ShoppingRes(413).ok)
        assertFalse(ShoppingRes(500).ok)
        assertFalse(ShoppingRes(ShoppingRes.NET_ERROR).ok)
    }

    @Test fun listsStateWithDataIsData() {
        val res = ShoppingRes(200, lists = listOf(ShoppingList(id = "l", name = "N")))
        assertEquals(ScreenState.DATA, res.listsState())
    }

    @Test fun listsStateWithoutDataIsEmpty() {
        assertEquals(ScreenState.EMPTY, ShoppingRes(200).listsState())
    }

    @Test fun listsStateNotFoundOn404() {
        // 404 приоритетнее наличия данных — как в screenState
        val res = ShoppingRes(404, lists = listOf(ShoppingList(id = "l", name = "N")))
        assertEquals(ScreenState.NOT_FOUND, res.listsState())
    }

    @Test fun listsStateNoConnectionOnNetworkError() {
        assertEquals(ScreenState.NO_CONNECTION, ShoppingRes(ShoppingRes.NET_ERROR).listsState())
        assertEquals(ScreenState.NO_CONNECTION, ShoppingRes(500).listsState())
    }

    @Test fun netErrorConstantIsZero() {
        assertEquals(0, ShoppingRes.NET_ERROR)
    }

    // ------------------------------------------------------------------
    // 7. createBody: тело POST /shopping/lists (создание из UI, задача 18)
    // ------------------------------------------------------------------

    @Test fun createBodyTrimsAndSendsName() {
        // поле диалога может прийти с пробелами — тримится при сборке тела
        val body = createBody("  Завтрак на даче  ")
        val root = JSONObject(body)
        assertTrue(root.has("name"))
        assertEquals("Завтрак на даче", root.getString("name"))
        assertEquals(1, root.length()) // только name
    }

    @Test fun createBodyBlankNameOmitsKey() {
        // пустое после трима — имя не отправляем вовсе (`{}`): сервер сам
        // ставит дефолт «Список покупок», семантика как у голосовой ветки
        for (raw in listOf<String?>(null, "", "   ", " \n\t ")) {
            val root = JSONObject(createBody(raw))
            assertFalse(root.has("name"))
            assertEquals(0, root.length())
        }
    }

    @Test fun createBodyEscapedNameSurvivesRoundtrip() {
        val name = "Дача «загород» \"2026\" — №\\1"
        val root = JSONObject(createBody(name))
        assertEquals(name, root.getString("name"))
    }

    // ------------------------------------------------------------------
    // 8. renameBody: тело PATCH для переименования списка (имя-only)
    // ------------------------------------------------------------------

    @Test fun renameBodyCarriesTrimmedNameOnly() {
        val root = JSONObject(renameBody("  Огород  "))
        assertTrue(root.has("name"))
        assertEquals("Огород", root.getString("name"))
        assertFalse(root.has("items")) // частичное тело: позиции не трогает
        assertEquals(1, root.length())
    }

    // ------------------------------------------------------------------
    // 9. Разбор ответа POST /shopping/lists (объект созданного списка)
    // ------------------------------------------------------------------

    @Test fun createResponseParsesToShoppingList() {
        // фиктивный JSON сервера по контракту задачи 18: POST возвращает созданный объект
        val list = parseList(
            """{"id":"l7","name":"Список покупок","items":[],
               "created_at":"2026-11-10T12:00:00Z","updated_at":"2026-11-10T12:01:00Z"}"""
        )
        requireNotNull(list)
        assertEquals("l7", list.id)
        assertEquals("Список покупок", list.name)
        assertTrue(list.items.isEmpty())
        assertEquals("2026-11-10T12:01:00Z", list.updatedAt)
    }

    @Test fun createResponseWithDefaultNameParses() {
        // поле в диалоге было пустым: сервер вернул дефолтное имя — клиенту неважно
        val list = parseList(
            """{"id":"l8","name":"Список покупок","items":[],"created_at":"2026-11-10T12:00:00Z"}"""
        )
        requireNotNull(list)
        assertEquals("l8", list.id)
        assertEquals("Список покупок", list.name)
    }

    @Test fun createResponseMissingNameParsesEmptyNameWithoutCrash() {
        // сервер прислал объект вообще без `name` — толерантный разбор,
        // UI сам покажет «Список покупок» (ifEmpty)
        val list = parseList("""{"id":"l9","items":[],"created_at":"2026-11-10T12:00:00Z"}""")
        requireNotNull(list)
        assertEquals("l9", list.id)
        assertEquals("", list.name)
        assertTrue(list.items.isEmpty())
    }

    @Test fun createResponseItemsAreParsedToo() {
        // POST может вернуть список уже с позициями — контракт тот же объект списка
        val list = parseList(
            """{"id":"la","name":"Дача","items":[{"id":"i1","text":"Яблоки","qty":3,"done":false}],
               "created_at":"2026-11-10T12:00:00Z"}"""
        )
        requireNotNull(list)
        assertEquals(1, list.items.size)
        assertEquals("Яблоки", list.items[0].text)
        assertEquals("3", list.items[0].qty)
        assertFalse(list.items[0].done)
    }
}
