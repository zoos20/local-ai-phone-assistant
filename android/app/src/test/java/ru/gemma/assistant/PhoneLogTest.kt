package ru.gemma.assistant

import kotlinx.coroutines.channels.Channel
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.InetAddress

/**
 * JUnit-тесты канала логов [PhoneLogChannel] (задача 8 фичи
 * «Беспроводной дебагинг»; FR-B3/B5/B6/B7/B8) — только чистая логика,
 * без Android/Robolectric (новые зависимости запрещены, §10.1):
 *
 *  - [LogRing]: лимит буфера 1000 с вытеснением старых (FIFO, FR-B5),
 *    пачка ≤200 записей ([PhoneLogChannel.BATCH_MAX]) и бюджет байт
 *    ([LogRing.peek]); guard снятия после отправки
 *    ([LogRing.dropHeadIfMatches]) и бамп поколения в
 *    [PhoneLogChannel.stop] — защита от гонок QA-ревью задачи 7
 *    (вытеснение/очистка кольца во время POST);
 *  - [PhoneLogChannel.buildBody]/[PhoneLogChannel.escapeJson]:
 *    сериализация батча `{ts,level,tag,msg}` и экранирование JSON
 *    (кавычки, переводы строк, управляющие символы; `%`/`%s` — дословно);
 *  - [PhoneLogChannel.maskSecrets]: маскирование секретов (FR-B7):
 *    значения токена/пина и `?token=`/`&token=` в URL;
 *  - [PhoneLogChannel.gateOn]: гейт `BuildConfig.DEBUG` × переключатель
 *    по таблице истинности, включая переходы (FR-B8);
 *  - [PhoneLogChannel.isSendableUrl]: guard адреса (CWE-319) — LAN-http
 *    (RFC1918/loopback) либо любой https; публичный cleartext, мусор и
 *    ошибка резолва -> отказ, DNS только для hostname;
 *  - [PhoneLogChannel.shouldLogSkip]: throttle события guard'а —
 *    ≤1 событие в 60 с: флап DNS «упал→прошёл→упал» не спамит
 *    каденсом flush;
 *  - константы flush (2 с / ≥50 записей) и реальное «условие размера»
 *    в [PhoneLogChannel.enqueue]: будильник срабатывает ровно с 50-й
 *    записи, выключенный канал буфер не заполняет (FR-B6).
 *
 * Тайминг suspend-цикла `flushLoop` (`delay(FLUSH_MS)` /
 * `withTimeoutOrNull(FLUSH_MS)`) юнит-тестом не покрыт — живая проверка
 * «события на ПК в течение ≤5 с» выполняется в smoke (задача 11).
 * Приватное состояние канала (`enabled`/`ring`/`signal`/`cached*`)
 * для тестов гейта и порога готовится рефлексией (precedent:
 * UpdaterTest.safeBaseName) и сбрасывается после каждого теста;
 * прод-код не изменялся.
 */
class PhoneLogTest {

    private val token = "s3cr3t-t0ken"
    private val pin = "9471"

    // ------------------------------------------------------------------
    // Доступ к состоянию singleton'а (рефлексия) + сброс между тестами
    // ------------------------------------------------------------------

    private fun field(name: String) =
        PhoneLogChannel::class.java
            .getDeclaredField(name)
            .apply { isAccessible = true }

    private fun setField(name: String, value: Any?) {
        field(name).set(PhoneLogChannel, value)
    }

    private fun ring(): LogRing =
        field("ring").get(PhoneLogChannel) as LogRing

    @Suppress("UNCHECKED_CAST")
    private fun signal(): Channel<Unit> =
        field("signal").get(PhoneLogChannel) as Channel<Unit>

    /** Будильник — конфликтный: между тестами обязан быть пуст. */
    private fun drainSignal() {
        val s = signal()
        while (s.tryReceive().isSuccess) {
            // очистка остатка от предыдущего теста
        }
    }

    /** Гейт выключен, буфер пуст, будильник пуст, секреты не закэшированы. */
    private fun resetChannelState() {
        setField("enabled", false)
        ring().clear()
        drainSignal()
        setField("cachedToken", "")
        setField("cachedPin", "")
        // состояние события guard'а: флаг не объявлен, throttle сброшен
        // к «первый отказ логируется всегда»
        setField("skipLoggedUrl", null)
        setField("lastSkipLogAtMs", -PhoneLogChannel.SKIP_LOG_INTERVAL_MS)
        // поколение — монотонный счётчик; к тестам не относится,
        // но сбрасываем для детерминизма (stop() его бампит)
        setField("generation", 0)
    }

    private fun generation(): Int =
        field("generation").get(PhoneLogChannel) as Int

    private fun enableChannel() = setField("enabled", true)

    private fun entry(i: Int) = PhoneLogEntry(i.toLong(), "I", "T", "m$i")

    private fun buffered(): List<PhoneLogEntry> =
        ring().peek(PhoneLogChannel.CAPACITY, Int.MAX_VALUE)

    @Before fun resetBefore() = resetChannelState()

    @After fun resetAfter() = resetChannelState()

    // ------------------------------------------------------------------
    // Gating: debug-сборка × переключатель (FR-B8)
    // ------------------------------------------------------------------

    @Test fun gateOnDebugAndSwitchOnOpens() =
        assertTrue(PhoneLogChannel.gateOn(debugBuild = true, switchOn = true))

    @Test fun gateOnDebugWithSwitchOffClosed() =
        assertFalse(PhoneLogChannel.gateOn(debugBuild = true, switchOn = false))

    // release + включённый переключатель: канал всё равно закрыт (FR-B8)
    @Test fun gateOnReleaseWithSwitchOnClosed() =
        assertFalse(PhoneLogChannel.gateOn(debugBuild = false, switchOn = true))

    @Test fun gateOnReleaseWithSwitchOffClosed() =
        assertFalse(PhoneLogChannel.gateOn(debugBuild = false, switchOn = false))

    // Переходы переключателя в debug: вкл -> выкл -> вкл
    @Test fun gateOnFollowsSwitchTransitionsInDebug() {
        assertTrue(PhoneLogChannel.gateOn(true, true))    // включили
        assertFalse(PhoneLogChannel.gateOn(true, false))  // выключили
        assertTrue(PhoneLogChannel.gateOn(true, true))    // включили снова
    }

    // release: любые переключения переключателя канал не открывают
    @Test fun gateOnReleaseNeverOpensOnSwitchToggle() {
        assertFalse(PhoneLogChannel.gateOn(false, true))
        assertFalse(PhoneLogChannel.gateOn(false, false))
        assertFalse(PhoneLogChannel.gateOn(false, true))
    }

    // ------------------------------------------------------------------
    // Маскирование секретов (FR-B7)
    // ------------------------------------------------------------------

    @Test fun masksTokenValueEveryOccurrence() {
        val r = PhoneLogChannel.maskSecrets(
            "tok=$token and $token again", token, pin)
        assertEquals("tok=*** and *** again", r)
        assertFalse(r.contains(token))
    }

    @Test fun masksPinValue() {
        val r = PhoneLogChannel.maskSecrets("pin $pin rejected", token, pin)
        assertEquals("pin *** rejected", r)
        assertFalse(r.contains(pin))
    }

    @Test fun emptySecretsLeaveTextUnchanged() =
        assertEquals(
            "plain text 123",
            PhoneLogChannel.maskSecrets("plain text 123", "", ""))

    @Test fun masksQuestionMarkTokenParam() {
        val r = PhoneLogChannel.maskSecrets(
            "GET http://pc:8090/?token=abcdef123 ok", "", "")
        assertEquals("GET http://pc:8090/?token=*** ok", r)
    }

    @Test fun masksAmpersandTokenParam() =
        assertEquals(
            "/x?a=1&token=***&b=2",
            PhoneLogChannel.maskSecrets("/x?a=1&token=abcdef123&b=2", "", ""))

    // Значение параметра ограничено кавычкой: URL в кавычках не рвётся
    @Test fun tokenParamValueStopsBeforeQuote() =
        assertEquals(
            "url=\"http://pc/?token=***\"",
            PhoneLogChannel.maskSecrets("url=\"http://pc/?token=abc123\"", "", ""))

    // Известное значение токена маскируется ДО разбора параметра URL
    @Test fun knownTokenValueMaskedInsideUrl() {
        val r = PhoneLogChannel.maskSecrets(
            "open http://pc/?token=$token", token, pin)
        assertEquals("open http://pc/?token=***", r)
        assertFalse(r.contains(token))
    }

    @Test fun plainTextUnchangedWithSecretsConfigured() {
        val r = PhoneLogChannel.maskSecrets("wake ok, status=0", token, pin)
        assertEquals("wake ok, status=0", r)
    }

    // По коду regex требует ?/& перед token=: голое `token=` без
    // URL-контекста правилом параметра не маскируется (не известный
    // секрет — правило значений его тоже не находит)
    @Test fun bareTokenParamWithoutUrlPrefixUntouched() =
        assertEquals(
            "field token=abcXYZ",
            PhoneLogChannel.maskSecrets("field token=abcXYZ", "", ""))

    // Регистр параметра regex не проверяет, но значение известного токена
    // маскируется правилом значений — секрет не утекает
    @Test fun uppercaseTokenParamStillMasksKnownValue() {
        val r = PhoneLogChannel.maskSecrets("GET /?TOKEN=$token", token, pin)
        assertEquals("GET /?TOKEN=***", r)
        assertFalse(r.contains(token))
    }

    @Test fun multipleTokenParamsAllMasked() =
        assertEquals(
            "?token=***&x=1&token=***",
            PhoneLogChannel.maskSecrets("?token=aaa&x=1&token=bbb", "", ""))

    // ------------------------------------------------------------------
    // Экранирование JSON
    // ------------------------------------------------------------------

    @Test fun quotesAndBackslashEscaped() =
        assertEquals(
            "say \\\"hi\\\" and \\\\ slash",
            PhoneLogChannel.escapeJson("say \"hi\" and \\ slash"))

    @Test fun newlineReturnTabEscaped() =
        assertEquals(
            "a\\nb\\rc\\td",
            PhoneLogChannel.escapeJson("a\nb\rc\td"))

    // Управляющие символы < U+0020 -> \uXXXX (иначе батч — не JSON)
    @Test fun controlCharsUnicodeEscaped() =
        assertEquals(
            "\\u0000\\u0001\\u001f",
            PhoneLogChannel.escapeJson("\u0000\u0001\u001f"))

    // `%`/`%s` — дословно: клиент текст не форматирует, сервер пишет
    // строку как есть
    @Test fun percentSpecifiersSurviveVerbatim() {
        val s = "progress 50% %s and %d"
        assertEquals(s, PhoneLogChannel.escapeJson(s))
    }

    @Test fun plainAndCyrillicTextReturnedAsIs() {
        val s = "привет, мир! ok"
        assertEquals(s, PhoneLogChannel.escapeJson(s))
    }

    // ------------------------------------------------------------------
    // Сериализация батча
    // ------------------------------------------------------------------

    @Test fun singleEntryExactJsonWithFourFields() {
        val body = PhoneLogChannel.buildBody(
            listOf(PhoneLogEntry(1700000000000L, "I", "Tag", "msg")))
        assertEquals(
            """{"entries":[{"ts":1700000000000,"level":"I","tag":"Tag","msg":"msg"}]}""",
            body)
    }

    @Test fun multipleEntriesJoinedWithComma() {
        val body = PhoneLogChannel.buildBody(listOf(
            PhoneLogEntry(1L, "D", "A", "one"),
            PhoneLogEntry(2L, "E", "B", "two"),
        ))
        assertEquals(
            """{"entries":[{"ts":1,"level":"D","tag":"A","msg":"one"},""" +
                """{"ts":2,"level":"E","tag":"B","msg":"two"}]}""",
            body)
    }

    @Test fun emptyListYieldsEmptyEntriesArray() =
        assertEquals(
            """{"entries":[]}""",
            PhoneLogChannel.buildBody(emptyList()))

    // Спецсимволы обязаны уезжать экранированными: сырая строка/кавычка
    // в теле развалила бы серверную строку `[ts] LEVEL/tag: msg`,
    // а парсер org.json подтверждает корректность экранирования
    @Test fun specialCharsRoundTripThroughJsonParser() {
        val msg = "line1\nline2 \"quoted\"\t\u0001end"
        val tag = "Ta\"g"
        val body = PhoneLogChannel.buildBody(
            listOf(PhoneLogEntry(42L, "W", tag, msg)))
        assertFalse("сырой перевод строки в теле", body.contains("\n"))
        assertTrue(body.contains("\\n"))
        assertTrue(body.contains("\\\""))
        assertTrue(body.contains("\\u0001"))
        val e = JSONObject(body).getJSONArray("entries").getJSONObject(0)
        assertEquals(42L, e.getLong("ts"))
        assertEquals("W", e.getString("level"))
        assertEquals(tag, e.getString("tag"))
        assertEquals(msg, e.getString("msg"))
    }

    @Test fun percentFormatSpecifiersSurviveSerialization() {
        val msg = "download 50% %s done"
        val body = PhoneLogChannel.buildBody(
            listOf(PhoneLogEntry(7L, "I", "T", msg)))
        assertTrue("проценты дословно: $body", body.contains("50% %s"))
        assertEquals(
            msg,
            JSONObject(body).getJSONArray("entries").getJSONObject(0)
                .getString("msg"))
    }

    // ------------------------------------------------------------------
    // Кольцевой буфер: лимит 1000 с вытеснением старых (FR-B5)
    // ------------------------------------------------------------------

    @Test fun keepsAllEntriesUnderCapacity() {
        val r = LogRing(10)
        repeat(3) { r.add(entry(it)) }
        assertEquals(3, r.size())
        assertEquals(listOf(0, 1, 2), bufferedOf(r).map { it.ts.toInt() })
    }

    private fun bufferedOf(r: LogRing): List<PhoneLogEntry> =
        r.peek(PhoneLogChannel.CAPACITY, Int.MAX_VALUE)

    // План: буфер ≤1000 записей, при переполнении вытесняются СТАРЫЕ
    @Test fun realCapacityEvictsOldestFifo() {
        val r = LogRing(PhoneLogChannel.CAPACITY)
        repeat(1100) { r.add(entry(it)) }
        assertEquals(1000, r.size())
        val kept = r.peek(PhoneLogChannel.CAPACITY, Int.MAX_VALUE)
        assertEquals(1000, kept.size)
        // первые 100 (0..99) вытеснены, порядок вставки сохранён
        assertEquals((100..1099).toList(), kept.map { it.ts.toInt() })
    }

    // Сервер принимает ≤200 записей: пачка из кольца — не больше лимита
    // (сам buildBody лимита не накладывает — он сериализует, что дали)
    @Test fun peekCapsAtTwoHundredOldestFirst() {
        val r = LogRing(500)
        repeat(500) { r.add(entry(it)) }
        val batch = r.peek(PhoneLogChannel.BATCH_MAX, Int.MAX_VALUE)
        assertEquals(200, batch.size)
        assertEquals((0..199).toList(), batch.map { it.ts.toInt() })
    }

    // Первая запись берётся всегда: иначе слишком большая запись
    // навсегда заблокировала бы очередь отправки
    @Test fun peekAlwaysTakesFirstEntryOverBudget() {
        val r = LogRing(10)
        repeat(3) { r.add(PhoneLogEntry(it.toLong(), "I", "T", "x".repeat(100))) }
        // оценка записи ≈ (100+1)*3+64 = 367 > бюджета 100
        assertEquals(1, r.peek(10, 100).size)
    }

    @Test fun peekRespectsByteBudgetForNextEntries() {
        val r = LogRing(10)
        repeat(3) { r.add(PhoneLogEntry(it.toLong(), "I", "T", "x".repeat(10))) }
        // оценка записи ≈ (10+1)*3+64 = 97: 2+97=99, 99+97=196 <= 200,
        // 196+97=293 > 200 -> третья не влезает
        assertEquals(2, r.peek(10, 200).size)
    }

    @Test fun dropFirstRemovesOldest() {
        val r = LogRing(10)
        repeat(5) { r.add(entry(it)) }
        r.dropFirst(2)
        assertEquals(3, r.size())
        assertEquals(listOf(2, 3, 4), bufferedOf(r).map { it.ts.toInt() })
    }

    // dropFirst вызывается после успешной отправки; гонка не должна
    // ронять канал — снятие больше, чем есть, безопасно
    @Test fun dropFirstBeyondSizeIsEmpty() {
        val r = LogRing(10)
        r.add(entry(1))
        r.dropFirst(50)
        assertEquals(0, r.size())
        assertTrue(r.peek(10, Int.MAX_VALUE).isEmpty())
    }

    @Test fun clearEmptiesBuffer() {
        val r = LogRing(10)
        repeat(5) { r.add(entry(it)) }
        r.clear()
        assertEquals(0, r.size())
        assertTrue(r.peek(10, Int.MAX_VALUE).isEmpty())
    }

    @Test fun singleSlotRingKeepsNewest() {
        val r = LogRing(1)
        r.add(entry(1))
        r.add(entry(2))
        assertEquals(1, r.size())
        assertEquals(2, r.peek(1, Int.MAX_VALUE).single().ts.toInt())
    }

    // ------------------------------------------------------------------
    // Константы канала (решения плана; к таймингам §7 не относятся)
    // ------------------------------------------------------------------

    @Test fun bufferAndBatchConstantsMatchPlan() {
        assertEquals("лимит буфера (FR-B5)", 1000, PhoneLogChannel.CAPACITY)
        assertEquals(
            "лимит записей в POST (серверный PhoneLogBatch)",
            200, PhoneLogChannel.BATCH_MAX)
    }

    @Test fun flushIntervalAndThresholdMatchPlan() {
        assertEquals(
            "интервал flush, мс (FR-B3: пачки раз в 2 с)",
            2000L, PhoneLogChannel.FLUSH_MS)
        assertEquals(
            "порог раннего flush (FR-B3: ≥50 записей)",
            50, PhoneLogChannel.FLUSH_AT)
    }

    // ------------------------------------------------------------------
    // enqueue: гейт буфера и «условие размера» порога flush
    // (реальный путь; состояние канала готовится рефлексией — см. шапку)
    // ------------------------------------------------------------------

    // FR-B6: выключенный переключатель -> буфер не заполняется
    @Test fun disabledChannelDoesNotFillBuffer() {
        // enabled=false после @Before (как после stop()/выключения)
        repeat(10) { PhoneLogChannel.enqueue("I", "T", "m$it") }
        assertEquals(0, ring().size())
    }

    @Test fun fortyNineEntriesDoNotWakeFlusher() {
        enableChannel()
        repeat(49) { PhoneLogChannel.enqueue("I", "T", "m$it") }
        assertEquals(49, ring().size())
        assertFalse(
            "будильник раньше порога FLUSH_AT=50 не срабатывает",
            signal().tryReceive().isSuccess)
    }

    @Test fun fiftiethEntryWakesFlusher() {
        enableChannel()
        repeat(49) { PhoneLogChannel.enqueue("I", "T", "m$it") }
        PhoneLogChannel.enqueue("I", "T", "m49")
        assertEquals(50, ring().size())
        assertTrue(
            "порог FLUSH_AT=50 достигнут -> будильник отправки",
            signal().tryReceive().isSuccess)
    }

    @Test fun enqueueStoresEntryFieldsWithTimestamp() {
        enableChannel()
        val before = System.currentTimeMillis()
        PhoneLogChannel.enqueue("W", "VoiceStream", "net down")
        val after = System.currentTimeMillis()
        val e = buffered().single()
        assertEquals("W", e.level)
        assertEquals("VoiceStream", e.tag)
        assertEquals("net down", e.msg)
        assertTrue("ts в окне вызова: ${e.ts}", e.ts in before..after)
    }

    // Обрезка до MAX_MSG: одна запись = одна строка phone.log
    @Test fun enqueueTruncatesLongMessageToMaxMsg() {
        assertEquals("граница обрезки", 512, PhoneLogChannel.MAX_MSG)
        enableChannel()
        PhoneLogChannel.enqueue("I", "T", "a".repeat(600))
        val e = buffered().single()
        assertEquals(PhoneLogChannel.MAX_MSG, e.msg.length)
        assertEquals("a".repeat(PhoneLogChannel.MAX_MSG), e.msg)
    }

    // FR-B7 на реальном пути: закэшированный токен маскируется ДО
    // попадания записи в буфер
    @Test fun enqueueMasksCachedSecretBeforeBuffering() {
        enableChannel()
        setField("cachedToken", token)
        PhoneLogChannel.enqueue("I", "T", "open http://pc:8090/?token=$token")
        val e = buffered().single()
        assertEquals("open http://pc:8090/?token=***", e.msg)
        assertFalse(e.msg.contains(token))
    }

    @Test fun enqueueAppendsThrowableClassAndMessage() {
        enableChannel()
        PhoneLogChannel.enqueue(
            "E", "T", "boom", IllegalStateException("bad state"))
        val e = buffered().single()
        assertEquals("boom [IllegalStateException: bad state]", e.msg)
    }

    // ------------------------------------------------------------------
    // Снятие батча после отправки: guard по голове кольца + поколение
    // (гонки «за окно отправки кольцо вытеснило / очистили»)
    // ------------------------------------------------------------------

    // Голова на месте -> снимается ровно батч, свежие записи остаются
    @Test fun dropHeadIfMatchesRemovesOnlySentBatch() {
        val r = LogRing(10)
        repeat(5) { r.add(entry(it)) }
        val batch = r.peek(3, Int.MAX_VALUE)          // ts 0,1,2
        assertTrue(r.dropHeadIfMatches(batch))
        assertEquals(listOf(3, 4), bufferedOf(r).map { it.ts.toInt() })
    }

    // Батч покрывает всё кольцо -> кольцо пустеет
    @Test fun dropHeadIfMatchesDropsWholeRingWhenBatchCoversIt() {
        val r = LogRing(10)
        repeat(2) { r.add(entry(it)) }
        assertTrue(r.dropHeadIfMatches(r.peek(10, Int.MAX_VALUE)))
        assertEquals(0, r.size())
    }

    // За окно отправки кольцо вытеснило голову: снимать НЕЛЬЗЯ —
    // dropFirst(2) ушёл бы на свежие неотправленные 100,101 (потеря до
    // BATCH_MAX строк); остаёмся с полным кольцом, отправит следующий круг
    @Test fun dropHeadIfMatchesRefusesWhenHeadEvicted() {
        val r = LogRing(4)
        repeat(4) { r.add(entry(it)) }                // 0,1,2,3
        val batch = r.peek(2, Int.MAX_VALUE)           // 0,1
        repeat(2) { r.add(entry(100 + it)) }           // вытеснило 0,1
        assertFalse(
            "голова сменилась -> снимать нельзя",
            r.dropHeadIfMatches(batch))
        assertEquals(
            listOf(2, 3, 100, 101),
            bufferedOf(r).map { it.ts.toInt() })
    }

    // Кольцо очищено и набрано заново (stop / выключение переключателя)
    // -> снимать нечего, свежие не трогаем
    @Test fun dropHeadIfMatchesRefusesAfterClearAndRefill() {
        val r = LogRing(10)
        repeat(3) { r.add(entry(it)) }
        val batch = r.peek(3, Int.MAX_VALUE)
        r.clear()
        r.add(entry(50))
        assertFalse(r.dropHeadIfMatches(batch))
        assertEquals(listOf(50), bufferedOf(r).map { it.ts.toInt() })
    }

    @Test fun dropHeadIfMatchesEmptyBatchIsNoop() {
        val r = LogRing(10)
        r.add(entry(1))
        assertTrue(r.dropHeadIfMatches(emptyList()))
        assertEquals(1, r.size())
    }

    // Гонка «stop() во время блокирующего send()»: cancel() не прерывает
    // send (в нём нет suspension-точек), поэтому stop() обязан бампать
    // поколение — иначе быстрый рестарт поднял бы второй flushLoop,
    // снимающий тот же батч (потеря до BATCH_MAX свежих записей)
    @Test fun stopBumpsGenerationForInFlightFlusher() {
        val before = generation()
        PhoneLogChannel.stop()
        assertTrue(
            "stop() должен бампать generation",
            generation() > before)
    }

    // ------------------------------------------------------------------
    // Guard адреса отправки (CWE-319): LAN-http либо любой https
    // ------------------------------------------------------------------

    // RFC1918 проходит; IP-литералы не ходят в DNS — resolver вообще
    // не вызывается (проверка дешёвая при КАЖДОМ send)
    @Test fun isSendableUrlAcceptsPrivateIpv4WithoutDns() {
        val calls = mutableListOf<String>()
        val resolve: (String) -> Array<InetAddress> = { h ->
            calls.add(h)
            arrayOf(InetAddress.getByName("192.168.1.5"))
        }
        for (ip in listOf(
            "192.168.1.100", "10.1.2.3", "172.16.0.9", "172.31.255.1")) {
            assertTrue(
                "$ip — приватный -> отправка разрешена",
                PhoneLogChannel.isSendableUrl("http://$ip:8090/debug/log", resolve))
        }
        assertTrue("IP-литерал не резолвится в DNS", calls.isEmpty())
    }

    // 127.0.0.0/8 и ::1 (IPv6 в URL — в скобках) -> loopback -> разрешён
    @Test fun isSendableUrlAcceptsLoopback() {
        assertTrue(
            PhoneLogChannel.isSendableUrl("http://127.0.0.1:8090/debug/log"))
        assertTrue(
            PhoneLogChannel.isSendableUrl("http://127.5.5.5/debug/log"))
        assertTrue(
            PhoneLogChannel.isSendableUrl("http://[::1]:8090/debug/log"))
    }

    // Публичный IPv4 по cleartext http -> отказ (README: адрес только
    // LAN-сервер); 172.32.x — за границей RFC1918 (172.16-31)
    @Test fun isSendableUrlRejectsPublicIpv4() {
        val calls = mutableListOf<String>()
        val resolve: (String) -> Array<InetAddress> = { h ->
            calls.add(h)
            arrayOf(InetAddress.getByName("192.168.1.5"))
        }
        assertFalse(
            PhoneLogChannel.isSendableUrl("http://8.8.8.8/debug/log", resolve))
        assertFalse(
            PhoneLogChannel.isSendableUrl("http://172.32.0.1/debug/log", resolve))
        assertFalse("литерал без DNS", calls.isNotEmpty())
    }

    // TLS шифрует канал сам -> https на любой адрес разрешён; решение
    // принимается ДО резолва (resolver не вызывается)
    @Test fun isSendableUrlAcceptsHttpsAnywhere() {
        val calls = mutableListOf<String>()
        val resolve: (String) -> Array<InetAddress> = { h ->
            calls.add(h)
            arrayOf(InetAddress.getByName("8.8.8.8"))
        }
        assertTrue(
            PhoneLogChannel.isSendableUrl("https://8.8.8.8:8090/debug/log", resolve))
        assertTrue(
            PhoneLogChannel.isSendableUrl("https://logs.example.com/debug/log", resolve))
        assertTrue("https не требует резолва", calls.isEmpty())
    }

    // Мусор, пустота, адрес без схемы, схема не http/https — отказ
    // (fail-safe, батч остаётся в буфере)
    @Test fun isSendableUrlRejectsGarbageEmptyAndForeignSchemes() {
        assertFalse(PhoneLogChannel.isSendableUrl(""))
        assertFalse(PhoneLogChannel.isSendableUrl("garbage"))
        assertFalse(PhoneLogChannel.isSendableUrl("192.168.1.100:8090"))
        assertFalse(PhoneLogChannel.isSendableUrl("http://"))
        assertFalse(PhoneLogChannel.isSendableUrl("ftp://192.168.1.100/x"))
    }

    // Hostname резолвится ТОЛЬКО через resolver (в тесте подменён, без
    // реального DNS): приватный -> разрешён, публичный -> отказ,
    // ошибка DNS -> отказ
    @Test fun isSendableUrlChecksHostnameThroughResolver() {
        val calls = mutableListOf<String>()
        val toPrivate: (String) -> Array<InetAddress> = { h ->
            calls.add(h)
            arrayOf(InetAddress.getByName("192.168.1.100"))
        }
        assertTrue(
            PhoneLogChannel.isSendableUrl("http://gemmapc.lan:8090/debug/log", toPrivate))
        assertEquals(listOf("gemmapc.lan"), calls)

        val toPublic: (String) -> Array<InetAddress> = { _ ->
            arrayOf(InetAddress.getByName("8.8.8.8"))
        }
        assertFalse(
            PhoneLogChannel.isSendableUrl("http://evil.example.com/debug/log", toPublic))

        val failing: (String) -> Array<InetAddress> = {
            throw java.net.UnknownHostException("nxdomain")
        }
        assertFalse(
            PhoneLogChannel.isSendableUrl("http://gone.example.com/debug/log", failing))
    }

    // ------------------------------------------------------------------
    // Throttle события guard'а: флап DNS не даёт каденс flush (2 с)
    // ------------------------------------------------------------------

    // «упал→прошёл→упал» в пределах 60 с: событие ОДНО (второй отказ —
    // ещё внутри интервала), после 60 с молчания первый отказ пишется
    // снова — ровно ≤1 событие в 60 с
    @Test fun skipEventSingleOnDnsFlapWithinSixtySeconds() {
        val url = "http://gemmapc.lan:8090/debug/log"
        val t0 = 3_600_000L                     // фейковое elapsedRealtime
        // упал -> событие (первый отказ; lastSkipLogAtMs сброшен в @Before)
        assertTrue("упал -> событие", PhoneLogChannel.shouldLogSkip(url, t0))
        // прошёл: send() после 2xx сбрасывает флаг, время throttle — нет
        setField("skipLoggedUrl", null)
        // упал снова: интервал не прошёл -> без события (спама каденсом нет)
        assertFalse("флап +2 с -> без события",
            PhoneLogChannel.shouldLogSkip(url, t0 + 2_000))
        setField("skipLoggedUrl", null)
        assertFalse("флап +59 с -> без события",
            PhoneLogChannel.shouldLogSkip(url, t0 + 59_000))
        // молчание ровно 60 с -> первый отказ пишется снова
        assertTrue("через 60 с -> событие снова",
            PhoneLogChannel.shouldLogSkip(url, t0 + 60_000))
        // итог: событий в окне [t0; t0+60 с) — ровно одно
    }
}
