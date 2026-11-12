package ru.gemma.assistant

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Экран «Списки покупок»: все списки с датой обновления и счётчиком
 * позиций (вычеркнутые — отдельно). Тап по строке открывает детали
 * ([ShoppingDetailActivity]); создание из UI — шапка `create_list_top` и
 * empty-state CTA `create_empty` ведут на один диалог →
 * [ServerClient.createShopping] → детали нового списка (спека
 * shopping-ux-spec §1).
 *
 * Состояния — [ScreenState] из ShoppingModels (задача 6): загрузка,
 * данные, пусто («Списков покупок пока нет»), нет связи; NOT_FOUND —
 * сообщение и возврат назад. `create_empty` виден только в EMPTY; при
 * NO_CONNECTION create-кнопки выключены и приглушены (кастомный градиент
 * не гаснет сам), «Обновить» остаётся кнопкой повтора.
 *
 * Сетевика — корутины `withContext(Dispatchers.IO) { ServerClient… }`
 * (CancellationException не глотается). Scope отменяется в onDestroy,
 * ответ «в полёте» отбрасывается по поколению [generation] — по образцу
 * `Updater.updateGeneration`: закрытый экран не получает рендера.
 */
class ShoppingListsActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "ShoppingLists"
    }

    private lateinit var pbLoading: ProgressBar
    private lateinit var tvStatus: TextView
    private lateinit var lvLists: ListView
    private lateinit var btnRefresh: Button
    private lateinit var btnCreateTop: Button
    private lateinit var btnCreateEmpty: Button
    private lateinit var adapter: ListsAdapter

    /** Создание (POST) в полёте — против двойной отправки (спека §1). */
    private var creating = false

    // Отмена корутин экрана в.onDestroy; неожиданные ошибки уходят в
    // канал логов, CancellationException сюда не попадает (отмена — не failure)
    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.Main +
            CoroutineExceptionHandler { _, e -> AppLog.w(TAG, "ui coroutine: $e", e) },
    )

    // Поколение операций (по образцу Updater.updateGeneration): каждый load()
    // берёт своё значение, устаревший ответ (onDestroy, повторный onResume,
    // новая загрузка) отбрасывается по расхождению и не трогает View
    private var generation = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_shopping_lists)

        pbLoading = findViewById(R.id.pbLoading)
        tvStatus = findViewById(R.id.tvStatus)
        lvLists = findViewById(R.id.lvLists)
        btnRefresh = findViewById(R.id.btnRefresh)
        btnCreateTop = findViewById(R.id.create_list_top)
        btnCreateEmpty = findViewById(R.id.create_empty)
        // Material-стиль перекрашивает background залиткой colorPrimary —
        // сбрасываем тинт, чтобы видна оставалась градиентная заливка
        btnRefresh.backgroundTintList = null
        btnCreateTop.backgroundTintList = null
        btnCreateEmpty.backgroundTintList = null

        adapter = ListsAdapter(this)
        lvLists.adapter = adapter

        findViewById<View>(R.id.btnBack).setOnClickListener { finish() }
        btnRefresh.setOnClickListener { load() }
        // Обе точки создания — один диалог (спека §1: шапка + empty-state CTA)
        btnCreateTop.setOnClickListener { showCreateDialog() }
        btnCreateEmpty.setOnClickListener { showCreateDialog() }
        lvLists.setOnItemClickListener { _, _, position, _ ->
            val list = adapter.getItem(position) ?: return@setOnItemClickListener
            startActivity(
                Intent(this, ShoppingDetailActivity::class.java)
                    .putExtra(ShoppingDetailActivity.EXTRA_LIST_ID, list.id),
            )
        }
    }

    override fun onResume() {
        super.onResume()
        // токен/адрес могли измениться в настройках (как в MainActivity)
        ServerClient.token = Settings.apiToken(this)
        // первый показ и возврат из деталей — всегда свежий GET /shopping/lists
        load()
    }

    override fun onDestroy() {
        generation++   // ответ «в полёте» станет устаревшим
        scope.cancel() // корутины экрана отменяются
        super.onDestroy()
    }

    /** GET /shopping/lists на IO; рендер — только если поколение не ушло. */
    private fun load() {
        val gen = ++generation
        val server = Settings.serverUrl(this)
        renderLoading()
        scope.launch {
            val res = withContext(Dispatchers.IO) { ServerClient.shoppingLists(server) }
            if (gen != generation) return@launch // устарело (onDestroy / новый load)
            render(res)
        }
    }

    /** Индикатор и «Загружаю…» (только пока данных нет — при обновлении список остаётся). */
    private fun renderLoading() {
        pbLoading.visibility = View.VISIBLE
        // CTA виден только в EMPTY (ветка render); загрузка — без запретов
        btnCreateEmpty.visibility = View.GONE
        setCreateEnabled(true)
        if (adapter.items.isEmpty()) showStatus("Загружаю…")
    }

    /**
     * Единый рендер состояния экрана: успех чтения — по [ScreenState]
     * из [ShoppingRes.listsState]; при потере связи с уже показанными
     * данными скрываем их не будем — просто предупреждаем.
     */
    private fun render(res: ShoppingRes) {
        pbLoading.visibility = View.GONE
        when (res.listsState()) {
            ScreenState.DATA -> {
                adapter.items = res.lists
                adapter.notifyDataSetChanged()
                lvLists.visibility = View.VISIBLE
                btnCreateEmpty.visibility = View.GONE
                setCreateEnabled(true)
                hideStatus()
            }
            ScreenState.EMPTY -> {
                adapter.items = emptyList()
                adapter.notifyDataSetChanged()
                lvLists.visibility = View.GONE
                btnCreateEmpty.visibility = View.VISIBLE // CTA пустого экрана
                setCreateEnabled(true)
                showStatus("Списков покупок пока нет")
            }
            ScreenState.NO_CONNECTION -> {
                btnCreateEmpty.visibility = View.GONE
                // Создание недоступно без сервера; «Обновить» остаётся
                // кнопкой повтора (спека §2: авто-retry нет — повтор вручную)
                setCreateEnabled(false)
                if (adapter.items.isNotEmpty()) {
                    showStatus("⛔ Нет связи — данные не обновлены.", error = true)
                } else {
                    lvLists.visibility = View.GONE
                    showStatus(
                        "⛔ Нет связи с сервером. Проверьте адрес в настройках.",
                        error = true,
                    )
                }
            }
            // Список удалён в другом месте (голосом) — сообщение и назад
            ScreenState.NOT_FOUND -> {
                Toast.makeText(this, "Список не найден", Toast.LENGTH_SHORT).show()
                finish()
            }
        }
    }

    /**
     * Обе create-кнопки: enabled + приглушение alpha — кастомная градиентная
     * заливка не гаснет сама в disabled (спека §6.10).
     */
    private fun setCreateEnabled(enabled: Boolean) {
        btnCreateTop.isEnabled = enabled
        btnCreateEmpty.isEnabled = enabled
        btnCreateTop.alpha = if (enabled) 1.0f else 0.5f
        btnCreateEmpty.alpha = if (enabled) 1.0f else 0.5f
    }

    /** POST создания в полёте: блокируем create-кнопки и «Обновить» — двойная отправка запрещена. */
    private fun setBusy(busy: Boolean) {
        btnRefresh.isEnabled = !busy
        btnRefresh.alpha = if (busy) 0.5f else 1.0f
        setCreateEnabled(!busy)
    }

    // ------------------------------------------------------------------
    // Создание списка из UI (спека shopping-ux-spec §1)
    // ------------------------------------------------------------------

    /**
     * Один диалог для обеих точек создания: поле предзаполнено «Список
     * покупок» и выделено (подтвердить или вписать своё); «Создать»/«Отмена»;
     * Done на клавиатуре == тап «Создать». Пустое после трима имя отправляем
     * как отсутствующее поле (`createBody`) — сервер ставит дефолт.
     */
    private fun showCreateDialog() {
        val input = EditText(this).apply {
            setText("Список покупок")
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT
            imeOptions = EditorInfo.IME_ACTION_DONE
        }
        val pad = (16 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, 0, pad, 0)
            addView(input)
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("Создать список")
            .setView(box)
            .setPositiveButton("Создать") { _, _ -> createList(input.text.toString()) }
            .setNegativeButton("Отмена", null)
            .create()
        // Done на клавиатуре == тап «Создать» (закрывает диалог, отправляет)
        input.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
                true
            } else {
                false
            }
        }
        dialog.show()
        input.requestFocus()
        input.selectAll()
    }

    /**
     * POST /shopping/lists → успех: открыть детали нового списка (back из
     * деталей вернёт сюда, onResume перечитает GET); 2xx без разобранного
     * id — просто перечитываем списки; ошибка — тост паттерна (спека §1),
     * диалог уже закрыт — повтор той же кнопкой.
     */
    private fun createList(rawName: String) {
        if (creating) return // двойная отправка запрещена: POST ≤10 с в полёте
        creating = true
        setBusy(true)
        val server = Settings.serverUrl(this)
        scope.launch {
            val res = withContext(Dispatchers.IO) { ServerClient.createShopping(server, rawName) }
            creating = false
            setBusy(false)
            when {
                res.ok && res.list != null -> startActivity(
                    Intent(this@ShoppingListsActivity, ShoppingDetailActivity::class.java)
                        .putExtra(ShoppingDetailActivity.EXTRA_LIST_ID, res.list.id),
                )
                res.ok -> load() // создан, но тело не разобралось — перечитаем списки
                else -> Toast.makeText(
                    this@ShoppingListsActivity, createError(res.code), Toast.LENGTH_SHORT,
                ).show()
            }
        }
    }

    private fun createError(code: Int): String =
        if (code == ShoppingRes.NET_ERROR) {
            "⛔ Нет связи — список не создан."
        } else {
            "⛔ Ошибка сервера (код $code) — список не создан."
        }

    private fun showStatus(text: String, error: Boolean = false) {
        tvStatus.visibility = View.VISIBLE
        tvStatus.setTextColor(
            ContextCompat.getColor(this, if (error) R.color.status_error else R.color.status_idle),
        )
        tvStatus.text = text
    }

    private fun hideStatus() {
        tvStatus.visibility = View.GONE
    }

    /** Строка списка: имя, счётчик позиций, дата обновления. */
    private class ListsAdapter(private val context: Context) : BaseAdapter() {
        var items: List<ShoppingList> = emptyList()

        override fun getCount(): Int = items.size
        override fun getItem(position: Int): ShoppingList? = items.getOrNull(position)
        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val view = convertView ?: LayoutInflater.from(context)
                .inflate(R.layout.item_shopping_list, parent, false)
            val list = items[position]
            view.findViewById<TextView>(R.id.tvName).text =
                list.name.ifEmpty { "Список покупок" }

            val total = list.items.size
            val done = list.items.count { it.done }
            view.findViewById<TextView>(R.id.tvMeta).text = when {
                total == 0 -> "Позиций нет"
                done == 0 -> "$total ${plural(total, "позиция", "позиции", "позиций")}"
                else -> "$total ${plural(total, "позиция", "позиции", "позиций")} · " +
                    "$done вычеркнуто"
            }

            val updated = formatUpdated(list.updatedAt)
            val tvDate = view.findViewById<TextView>(R.id.tvDate)
            tvDate.text = updated
            tvDate.visibility = if (updated == null) View.GONE else View.VISIBLE
            return view
        }
    }
}

/** Русская форма числа: «1 позиция», «2 позиции», «5 позиций». */
private fun plural(n: Int, one: String, few: String, many: String): String = when {
    n % 10 == 1 && n % 100 != 11 -> one
    n % 10 in 2..4 && n % 100 !in 12..14 -> few
    else -> many
}

/** Дата из ISO `updated_at` («2026-11-08T12:34:56» → «2026-11-08 12:34»). */
private fun formatUpdated(iso: String): String? {
    val s = iso.trim()
    if (s.isEmpty()) return null
    return "Обновлён " + s.substring(0, minOf(16, s.length)).replace('T', ' ')
}
