package ru.gemma.assistant

import org.json.JSONObject
import org.vosk.Recognizer

private const val TAG = "GemmaService"

internal val YES_WORDS = setOf(
    "да", "ага", "угу", "давай", "конечно", "ок", "окей", "поехали",
    "отправляй", "отправь", "отправить", "да отправь"
)
internal val NO_WORDS = setOf(
    "нет", "отмена", "отмени", "не надо", "не нужно", "стоп", "отбой",
    "отменить", "не отправляй"
)
// Стоп-слова: произносятся во время записи команды (COMMAND) -- сервис
// тут же прекращает слушать, сбрасывает запись и уходит в фоновое
// ожидание «Гемма», ничего не отправляя. В CONFIRM работают как
// отмена (см. classifyAnswer). Одиночные слова -- сравнение идёт по
// токенам; составные фразы («хватит уже») не включаем: токенизатор
// Vosk может разбить их иначе, чем мы.
internal val STOP_WORDS = setOf(
    "стоп", "хватит", "перестань", "остановись", "замолчи",
    "закройся", "отбой", "отвали"
)

/**
 * Стоп-слово в тексте распознавания (partial или final), иначе null.
 *
 * Токенизация -- как в проверке handleCommand: lowercase, всё вне
 * «русских» букв и пробелов -> пробел, токены >= 2 символов, сравнение
 * по токенам (одиночные слова, см. комментарий к [STOP_WORDS]).
 * Вынесена в отдельную функцию, потому что одна и та же проверка
 * нужна и в COMMAND, и в окне дослушивания (FOLLOWUP).
 */
internal fun stopWordHit(text: String?): String? {
    if (text.isNullOrBlank()) return null
    return text.lowercase()
        .replace(Regex("[^а-яёa-z ]"), " ")
        .split(Regex("\\s+"))
        .filter { it.length >= 2 }
        .firstOrNull { it in STOP_WORDS }
}

// Варианты wake word для verify (белый список искажений): расширяется
// по логам `wake verify heard`. Сравнение -- строго по строке;
// фонетический порог == 0 (см. verifyWakeWord) НЕ расширяем: класс
// «кино/зима/тима/лимa/гимн» имеет d_ph = 1 и при пороге <= 1 давал
// ложные срабатывания (лог 05.11 22:51: verify heard 'кино кино' -> hit).
// Живые искажения из smoke-f3-5b (06.11, вызовы 16:35-16:49): свободный
// Vosk слышал настоящую «Гемму» как «димо/демо/дима» (tima ~ gima,
// d_ph = 1 -> отсекались порогом) и «модема» (d_ph = 3). «дима» формально
// принадлежит FP-классу и является именем, но в свежем смоке все
// вхождения -- искажения «Геммы» в фазе вызовов; добавляется ТОЧНЫМ
// совпадением (фонетический класс «тима/лима/кино» остаётся отказом).
internal val WAKE_VARIANTS = setOf(
    "гемма", "геммы", "гемме", "гемму", "геммой", "гемм",
    "гамма", "гема", "емма", "хемма", "темма",
    "димо", "демо", "дима", "модема"
)

/**
 * Слово из окна wake-верификации похоже на «Гемму» (MATCH)?
 *
 * Три независимых ветки (все обязаны остаться):
 *  1) точное совпадение с белым списком [WAKE_VARIANTS];
 *  2) префикс самой wake-фразы -- усечённое «гем»/«гемор» из лога
 *     (verify запускается ТОЛЬКО на ударе узкой грамматики или в окне
 *     повтора после него, т.е. «в связке с ударом грамматики»
 *     выполняется структурно);
 *  3) расстояние <= 1 до самой фразы (однобуквенные правки «гемна»)
 *     либо фонетическое == 0 (класс «кино/зима» -- d_ph = 1, отказ).
 */
internal fun wakeWordHit(word: String, phrase: String): Boolean {
    val w = word
    return w in WAKE_VARIANTS ||
        (phrase.length >= 3 && w.startsWith(phrase.substring(0, 3))) ||
        editDistance(w, phrase) <= 1 ||
        editDistance(phonetic(w), phonetic(phrase)) == 0
}

/** Кормит кадр в указанный Vosk; текст (partial или final) либо null. */
internal fun feedRecognizer(
    rec: Recognizer?, frame: ByteArray, size: Int
): String? = feedRecognizerEx(rec, frame, size)?.second

/**
 * Как [feedRecognizer], но с признаком финального результата:
 * первый элемент Pair = true, если Vosk закончил фразу (финал).
 * В CONFIRM фолбэк «прочая речь -> нет» срабатывает ТОЛЬКО на финале:
 * на partial он слышал бы обрывки и отменял подтверждение по шуму.
 */
internal fun feedRecognizerEx(
    rec: Recognizer?, frame: ByteArray, size: Int
): Pair<Boolean, String>? {
    if (rec == null) return null
    return try {
        // Vosk 0.3.75: acceptWaveForm(byte[], offset) возвращает true,
        // когда найден конец речи (финальный результат).
        if (rec.acceptWaveForm(frame.copyOf(size), size)) {
            true to textOf(rec.result)
        } else {
            false to textOf(rec.partialResult)
        }
    } catch (t: Throwable) {
        AppLog.e(TAG, "recognizer error", t)
        null
    }
}

/** Расстояние Левенштейна (для сопоставления «гемма»/«геммы»/…). */
internal fun editDistance(a: String, b: String): Int {
    val m = a.length
    val n = b.length
    if (m == 0) return n
    if (n == 0) return m
    var prev = IntArray(n + 1) { it }
    val cur = IntArray(n + 1)
    for (i in 1..m) {
        cur[0] = i
        for (j in 1..n) {
            val cost = if (a[i - 1] == b[j - 1]) 0 else 1
            cur[j] = minOf(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        }
        // cur полностью перезаписан -- можно копировать в prev
        System.arraycopy(cur, 0, prev, 0, n + 1)
    }
    return prev[n]
}

/**
 * Грубая фонетическая нормализация слова для сравнения с wake word.
 *
 * Малая модель Vosk постоянно путает похожие звуки, поэтому сводим
 * оба слова к классам звуков и сравниваем уже в них:
 *   - гласные двух типов: открытые «а/о/у» -> 'a', закрытые
 *     «е/ё/и/ы/э/ю/я» -> 'i' («гемма» -> "gima", «гамма» -> "gima");
 *   - пары глухость/звонкость -> один символ (г/к -> g, д/т -> t,
 *     б/п -> p, в/ф -> f, ж/ш -> z, з/с -> s), т.к. малая модель
 *     путает их постоянно;
 *   - «й/ь/ъ» и удвоения выпадают («геммы» -> "gima").
 * Так «гамма»/«гема»/«емма» дают расстояние 0-1 к "gima", а «плен»
 * ("plin"~3), «тлен» ("tlin"~3), «дом» ("tam"~3) -- отсекаются.
 */
internal fun phonetic(word: String): String {
    val sb = StringBuilder(word.length)
    for (ch in word) {
        when (ch) {
            'а', 'о', 'у' -> sb.append('a')
            'е', 'ё', 'и', 'ы', 'э', 'ю', 'я' -> sb.append('i')
            'г', 'к' -> sb.append('g')
            'д', 'т' -> sb.append('t')
            'б', 'п' -> sb.append('p')
            'в', 'ф' -> sb.append('f')
            'ж', 'ш' -> sb.append('z')
            'з', 'с' -> sb.append('s')
            'х' -> sb.append('h')
            'м' -> sb.append('m')
            'н' -> sb.append('n')
            'л' -> sb.append('l')
            'р' -> sb.append('r')
            'ц', 'ч', 'щ' -> sb.append('c')
            'й', 'ь', 'ъ' -> { /* выпадают: не несут класс звука */ }
            else -> sb.append(ch)    // латиница и прочее -- как есть
        }
        // удвоение согласных не несёт смысла: "гемм" -> "gim"
        if (sb.length >= 2 && sb[sb.length - 1] == sb[sb.length - 2] &&
            sb[sb.length - 1] !in "ai"
        ) {
            sb.deleteCharAt(sb.length - 1)
        }
    }
    return sb.toString()
}

/** Среднеквадратичное отклонение кадра -- энергия голоса. */
internal fun pcmRms(pcm: ByteArray, size: Int, offset: Int = 0): Int {
    var sum = 0L
    var count = 0
    val end = offset + size
    var i = offset
    while (i + 1 < end) {
        val v = (pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)
        val s = if (v >= 32768) v - 65536 else v
        sum += s.toLong() * s
        count++
        i += 2
    }
    return if (count == 0) 0 else Math.sqrt(sum.toDouble() / count).toInt()
}

internal fun textOf(json: String): String =
    try {
        JSONObject(json).optString("text").ifBlank {
            JSONObject(json).optString("partial")
        }
    } catch (_: Exception) {
        json
    }

/** Пиковая амплитуда PCM (0..32767) — для диагностики уровня микрофона. */
internal fun pcmPeak(pcm: ByteArray): Int {
    var peak = 0
    var i = 0
    while (i + 1 < pcm.size) {
        val v = (pcm[i].toInt() and 0xFF) or (pcm[i + 1].toInt() shl 8)
        val s = if (v >= 32768) v - 65536 else v
        val a = if (s < 0) -s else s
        if (a > peak) peak = a
        i += 2
    }
    return peak
}

internal fun classifyAnswer(text: String): Boolean? {
    val words = text.lowercase()
        .replace(Regex("[^а-яёa-z ]"), " ")
        .split(Regex("\\s+"))
        .filter { it.isNotBlank() }
    if (words.any { it in NO_WORDS || it in STOP_WORDS }) return false
    if (words.any { it in YES_WORDS }) return true
    return null // не разобрали — продолжаем слушать
}
