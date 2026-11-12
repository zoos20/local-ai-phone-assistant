package ru.gemma.assistant

import android.content.Context
import android.graphics.Paint
import android.os.Bundle
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
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
 * Детали списка покупок: позиции с вычёркиванием (тап/чекбокс → PATCH с
 * полной заменой items через [patchBody]), добавление, редактирование и
 * удаление позиции (долгий тап → меню), «Удалить список» → AlertDialog →
 * [ServerClient.deleteShopping] → возврат к списку; переименование —
 * долгий тап по заголовку → диалог → PATCH `{"name": …}` ([renameBody],
 * спека shopping-ux-spec §3.1).
 *
 * **Контракт состояний (задача 6)**: успешность мутации (PATCH/DELETE)
 * определяется по `ShoppingRes.ok` (код), А НЕ по payload; после успеха —
 * рефетч, состояние чтения — по [screenState]. NOT_FOUND — сообщение и
 * возврат к списку.
 *
 * Сетевика — корутины `withContext(Dispatchers.IO) { ServerClient… }`
 * (CancellationException не глотается); scope отменяется в onDestroy,
 * устаревшие ответы отбрасываются по поколению [generation] (по образцу
 * `Updater.updateGeneration` — finish() во время запроса безопасен).
 * Флаг [mutating] не даёт запустить параллельные мутации: items — полная
 * замена (last-write-wins), параллельный PATCH затёр бы изменение.
 * Back-защита мутации (спека §3.3): пока [mutating], system back и
 * «Назад» игнорируются с тостом «Сохраняю…» — иначе finish() отбросил бы
 * PATCH-результат по поколению. Тач-цель «Назад» ≥ 48dp задана
 * в лейауте (спека §5) — рантайм-мутации размеров нет.
 */
class ShoppingDetailActivity : AppCompatActivity() {

    companion object {
        /** Идентификатор списка (передаёт ShoppingListsActivity). */
        const val EXTRA_LIST_ID = "list_id"
        private const val TAG = "ShoppingDetail"
    }

    private lateinit var tvTitle: TextView
    private lateinit var pbLoading: ProgressBar
    private lateinit var tvStatus: TextView
    private lateinit var lvItems: ListView
    private lateinit var etText: EditText
    private lateinit var etQty: EditText
    private lateinit var btnAdd: Button
    private lateinit var btnDeleteList: Button
    private lateinit var btnRefresh: Button
    private lateinit var adapter: ItemsAdapter

    private val scope = CoroutineScope(
        SupervisorJob() + Dispatchers.Main +
            CoroutineExceptionHandler { _, e -> AppLog.w(TAG, "ui coroutine: $e", e) },
    )

    private var generation = 0
    private var listId = ""
    /** Последние прочитанные данные списка — основа для полного PATCH. */
    private var currentList: ShoppingList? = null
    /** Мутация (PATCH/DELETE) в полёте: параллельные не запускаем. */
    private var mutating = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_shopping_detail)

        tvTitle = findViewById(R.id.tvTitle)
        pbLoading = findViewById(R.id.pbLoading)
        tvStatus = findViewById(R.id.tvStatus)
        lvItems = findViewById(R.id.lvItems)
        etText = findViewById(R.id.etText)
        etQty = findViewById(R.id.etQty)
        btnAdd = findViewById(R.id.btnAdd)
        btnDeleteList = findViewById(R.id.btnDeleteList)
        btnRefresh = findViewById(R.id.btnRefresh)
        // Material-стиль перекрашивает background залиткой colorPrimary —
        // сбрасываем тинт, чтобы видны остались градиент/контур
        btnAdd.backgroundTintList = null
        btnRefresh.backgroundTintList = null

        listId = intent.getStringExtra(EXTRA_LIST_ID).orEmpty()

        adapter = ItemsAdapter(this) { toggleDone(it) }
        lvItems.adapter = adapter

        // Системный back: при мутации игнорируется (спека §3.3 — иначе
        // finish() отбросит PATCH-результат по поколению), иначе — назад
        onBackPressedDispatcher.addCallback(
            this@ShoppingDetailActivity,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() = backWhenNotMutating()
            },
        )
        findViewById<View>(R.id.btnBack).setOnClickListener { backWhenNotMutating() }
        btnRefresh.setOnClickListener { load() }
        btnAdd.setOnClickListener { addItem() }
        btnDeleteList.setOnClickListener { confirmDeleteList() }
        // Долгий тап по заголовку — переименовать список (спека §3.1)
        tvTitle.setOnLongClickListener {
            if (!mutating && currentList != null) showRenameDialog()
            true
        }
        // Тап по строке/чекбоксу — вычёркивание; долгий тап — меню
        lvItems.setOnItemClickListener { _, _, position, _ ->
            adapter.getItem(position)?.let { toggleDone(it) }
        }
        lvItems.setOnItemLongClickListener { _, _, position, _ ->
            adapter.getItem(position)?.let { showItemMenu(it) }
            true
        }

        if (listId.isEmpty()) {
            Toast.makeText(this, "Список не найден", Toast.LENGTH_SHORT).show()
            finish()
        }
        // Кнопки стартуют в честном disabled: без данных «Добавить» нельзя
        updateActionStates()
    }

    override fun onResume() {
        super.onResume()
        if (listId.isEmpty()) return
        // токен/адрес могли измениться в настройках (как в MainActivity)
        ServerClient.token = Settings.apiToken(this)
        load() // первый показ (после поворота — тоже)
    }

    override fun onDestroy() {
        generation++   // ответ «в полёте» станет устаревшим
        scope.cancel() // корутины экрана отменяются
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // Чтение: GET /shopping/lists/{id}
    // ------------------------------------------------------------------

    /** Чтение списка; рендер — только если поколение не ушло. */
    private fun load() {
        if (mutating) return // мутация важнее: её рефетч и есть свежее чтение
        val gen = ++generation
        val server = Settings.serverUrl(this)
        val id = listId
        renderLoading()
        scope.launch {
            val res = withContext(Dispatchers.IO) { ServerClient.shoppingList(server, id) }
            if (gen != generation) return@launch // устарело (onDestroy / мутация)
            renderRead(res)
        }
    }

    /** Прогресс; при уже показанных данных список остаётся видимым. */
    private fun renderLoading() {
        pbLoading.visibility = View.VISIBLE
        if (currentList == null) showStatus("Загружаю…")
    }

    private fun renderRead(res: ShoppingRes) {
        pbLoading.visibility = View.GONE
        when (screenState(res.code, res.list != null)) {
            // list != null (2xx с данными; пустой открытый список — тоже данные)
            ScreenState.DATA -> applyList(res.list)
            // 2xx, но тело не разобралось — показываем как «пусто»
            ScreenState.EMPTY -> applyList(null)
            ScreenState.NO_CONNECTION ->
                if (currentList != null) {
                    showStatus("⛔ Нет связи — данные не обновлены.", error = true)
                } else {
                    lvItems.visibility = View.GONE
                    showStatus(
                        "⛔ Нет связи с сервером. Проверьте адрес в настройках.",
                        error = true,
                    )
                }
            // Список удалён голосом/в UI — сообщение и возврат к списку
            ScreenState.NOT_FOUND -> {
                Toast.makeText(this, "Список не найден", Toast.LENGTH_SHORT).show()
                finish()
            }
        }
    }

    // ------------------------------------------------------------------
    // Мутации: PATCH (items — полная замена) и DELETE списка
    // ------------------------------------------------------------------

    /**
     * PATCH с полной заменой items ([patchBody]) + рефетч после успеха.
     * Успех мутации — по `res.ok` (контракт задачи 6), состояние экрана —
     * по рефетчу; если рефетч не прошёл, берём тело PATCH (оно полное).
     */
    private fun mutate(newItems: List<ShoppingItem>, afterOk: () -> Unit = {}) {
        patchAndRefresh(patchBody(newItems), afterOk)
    }

    /**
     * Общая мутация PATCH + рефетч (items — полная замена из [mutate] или
     * name-only из [renameList]): тело уже собрано. Успех — по `res.ok`,
     * [afterOk] на Main после успеха, состояние экрана — по рефетчу.
     */
    private fun patchAndRefresh(body: String, afterOk: () -> Unit = {}) {
        if (mutating || currentList == null) return
        mutating = true
        pbLoading.visibility = View.GONE // иначе зависший индикатор от прерванного load()
        updateActionStates()
        val gen = ++generation
        val server = Settings.serverUrl(this)
        val id = listId
        scope.launch {
            val patch = withContext(Dispatchers.IO) {
                ServerClient.patchShopping(server, id, body)
            }
            if (gen != generation) return@launch // экран закрыли — View не трогаем
            if (!patch.ok) {
                mutating = false
                updateActionStates()
                showStatus(patchError(patch.code), error = true)
                return@launch
            }
            afterOk() // успех по res.ok: например, очистка поля ввода
            val ref = withContext(Dispatchers.IO) { ServerClient.shoppingList(server, id) }
            if (gen != generation) return@launch
            mutating = false
            renderAfterMutation(patch, ref)
        }
    }

    /** Состояние после успешного PATCH: рефетч -> [ScreenState], fallback на тело PATCH. */
    private fun renderAfterMutation(patch: ShoppingRes, ref: ShoppingRes) {
        when (screenState(ref.code, ref.list != null)) {
            ScreenState.DATA -> applyList(ref.list)
            ScreenState.EMPTY ->
                if (patch.list != null) {
                    applyList(patch.list, "⚠ Обновить список не удалось.")
                } else {
                    applyList(null)
                }
            ScreenState.NO_CONNECTION ->
                if (patch.list != null) {
                    applyList(patch.list, "⚠ Изменение сохранено, обновить список не удалось.")
                } else {
                    showStatus("⚠ Изменение сохранено, обновить список не удалось.", error = true)
                }
            ScreenState.NOT_FOUND -> {
                Toast.makeText(this, "Список не найден", Toast.LENGTH_SHORT).show()
                finish()
            }
        }
    }

    /** Добавление позиции: id пустой — сервер сгенерирует новый. */
    private fun addItem() {
        val text = etText.text.toString().trim()
        if (text.isEmpty()) {
            Toast.makeText(this, "Введите текст позиции", Toast.LENGTH_SHORT).show()
            return
        }
        val cur = currentList ?: return
        val qty = etQty.text.toString().trim().ifEmpty { null }
        mutate(cur.items + ShoppingItem(id = "", text = text, qty = qty, done = false)) {
            etText.text.clear()
            etQty.text.clear()
        }
    }

    /** Тап/чекбокс: снять или поставить вычёркивание (позиция остаётся). */
    private fun toggleDone(item: ShoppingItem) {
        val items = currentList?.items ?: return
        val idx = indexOfItem(item)
        if (idx < 0) return
        val next = items.toMutableList()
        next[idx] = next[idx].copy(done = !next[idx].done)
        mutate(next)
    }

    /** Долгий тап: изменить или удалить позицию. */
    private fun showItemMenu(item: ShoppingItem) {
        AlertDialog.Builder(this)
            .setItems(arrayOf("Изменить", "Удалить")) { _, which ->
                when (which) {
                    0 -> editItem(item)
                    1 -> deleteItem(item)
                }
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun editItem(item: ShoppingItem) {
        val idx = indexOfItem(item)
        if (idx < 0) return
        val etT = EditText(this).apply {
            setText(item.text)
            hint = "Текст"
        }
        val etQ = EditText(this).apply {
            setText(item.qty ?: "")
            hint = "Количество"
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val pad = (16 * resources.displayMetrics.density).toInt()
            setPadding(pad, 0, pad, 0)
            addView(etT)
            addView(etQ)
        }
        AlertDialog.Builder(this)
            .setTitle("Изменить позицию")
            .setView(box)
            .setPositiveButton("Сохранить") { _, _ ->
                val text = etT.text.toString().trim()
                if (text.isEmpty()) {
                    // пустой текст не сохраняем: позиция остаётся исходной
                    Toast.makeText(this, "Введите текст позиции", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                val items = currentList?.items?.toMutableList() ?: return@setPositiveButton
                if (idx >= items.size) return@setPositiveButton
                items[idx] = items[idx].copy(
                    text = text,
                    qty = etQ.text.toString().trim().ifEmpty { null },
                )
                mutate(items)
            }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun deleteItem(item: ShoppingItem) {
        val items = currentList?.items ?: return
        val idx = indexOfItem(item)
        if (idx < 0) return
        mutate(items.filterIndexed { i, _ -> i != idx })
    }

    /**
     * Переименование списка (спека §3.1): долгий тап по заголовку.
     * Кнопки те же подписи, что в диалоге позиции («Сохранить»/«Отмена»);
     * клавиатурный Done == «Сохранить»; пустое после трима → Toast
     * «Введите название», имя не меняется (как у позиции).
     */
    private fun showRenameDialog() {
        val list = currentList ?: return
        val input = EditText(this).apply {
            setText(list.name.ifEmpty { "Список покупок" })
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
            .setTitle("Переименовать список")
            .setView(box)
            .setPositiveButton("Сохранить") { _, _ ->
                val text = input.text.toString().trim()
                if (text.isEmpty()) {
                    Toast.makeText(this, "Введите название", Toast.LENGTH_SHORT).show()
                } else {
                    renameList(text)
                }
            }
            .setNegativeButton("Отмена", null)
            .create()
        // Done на клавиатуре == тап «Сохранить»
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
     * PATCH `{"name": …}` (items не трогаем — частичное тело, сервер
     * позиции хранит) + рефетч: заголовок обновится в [applyList] (паттерн
     * [patchError] для ошибок — как у остальных мутаций).
     */
    private fun renameList(newName: String) {
        patchAndRefresh(renameBody(newName))
    }

    /** «Удалить список»: подтверждение в AlertDialog, потом DELETE. */
    private fun confirmDeleteList() {
        val name = currentList?.name?.ifEmpty { "Список покупок" } ?: "Список покупок"
        AlertDialog.Builder(this)
            .setTitle("Удалить список?")
            .setMessage("«$name» и все его позиции будут удалены.")
            .setPositiveButton("Удалить") { _, _ -> deleteList() }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun deleteList() {
        if (mutating) return
        mutating = true
        pbLoading.visibility = View.GONE
        updateActionStates()
        val gen = ++generation
        val server = Settings.serverUrl(this)
        val id = listId
        scope.launch {
            val res = withContext(Dispatchers.IO) { ServerClient.deleteShopping(server, id) }
            if (gen != generation) return@launch // экран закрыли — View не трогаем
            mutating = false
            when {
                res.ok -> {
                    // Спека §4: та же форма, что у голоса — «Список «X» удалён.»
                    val listName = currentList?.name?.ifEmpty { "Список покупок" }
                    Toast.makeText(
                        this@ShoppingDetailActivity,
                        if (listName == null) "Список удалён." else "Список «$listName» удалён.",
                        Toast.LENGTH_SHORT,
                    ).show()
                    finish() // возврат к списку (onResume там перечитает данные)
                }
                res.code == 404 -> {
                    Toast.makeText(
                        this@ShoppingDetailActivity, "Список не найден", Toast.LENGTH_SHORT,
                    ).show()
                    finish()
                }
                else -> {
                    updateActionStates()
                    showStatus(
                        if (res.code == ShoppingRes.NET_ERROR) {
                            "⛔ Нет связи — удаление не отправлено."
                        } else {
                            "⛔ Ошибка сервера (код ${res.code}) — список не удалён."
                        },
                        error = true,
                    )
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // Рендер и помощники
    // ------------------------------------------------------------------

    /** Показать известные данные списка (null → «Список пуст»). */
    private fun applyList(list: ShoppingList?, note: String? = null) {
        currentList = list
        if (list == null) {
            adapter.items = emptyList()
            adapter.notifyDataSetChanged()
            lvItems.visibility = View.GONE
            showStatus("Список пуст")
            updateActionStates()
            return
        }
        tvTitle.text = list.name.ifEmpty { "Список покупок" }
        adapter.items = list.items
        adapter.notifyDataSetChanged()
        lvItems.visibility = View.VISIBLE
        if (note != null) showStatus(note, error = true) else hideStatus()
        updateActionStates()
    }

    /** Доступность действий: мутация блокирует всё, добавление — только без данных. */
    private fun updateActionStates() {
        btnAdd.isEnabled = !mutating && currentList != null
        btnDeleteList.isEnabled = !mutating
        btnRefresh.isEnabled = !mutating
        // Кастомные фоны (градиент/bg_input) не гаснут в disabled — приглушаем сами
        for (v in listOf<View>(btnAdd, btnDeleteList, btnRefresh)) {
            v.alpha = if (v.isEnabled) 1.0f else 0.5f
        }
    }

    /**
     * Back-защита мутации (спека §3.3): пока PATCH/DELETE в полёте, назад
     * нельзя — goBack при [mutating] молча отменил бы результат по
     * поколению; иначе обычный выход. Используется и header-«Назад»,
     * и system back.
     */
    private fun backWhenNotMutating() {
        if (mutating) {
            Toast.makeText(this, "Сохраняю…", Toast.LENGTH_SHORT).show()
        } else {
            finish()
        }
    }

    private fun patchError(code: Int): String =
        if (code == ShoppingRes.NET_ERROR) {
            "⛔ Нет связи — изменение не отправлено."
        } else {
            "⛔ Ошибка сервера (код $code) — изменение не отправлено."
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

    /** Индекс позиции в текущем списке: по серверному id, иначе — по значению. */
    private fun indexOfItem(item: ShoppingItem): Int {
        val items = currentList?.items ?: return -1
        val byId = items.indexOfFirst { it.id.isNotEmpty() && it.id == item.id }
        return if (byId >= 0) byId else items.indexOf(item)
    }

    /** Строка-позиция: чекбокс + текст (зачёркнутый/серый при done) + количество. */
    private class ItemsAdapter(
        private val context: Context,
        private val onToggle: (ShoppingItem) -> Unit,
    ) : BaseAdapter() {
        var items: List<ShoppingItem> = emptyList()

        override fun getCount(): Int = items.size
        override fun getItem(position: Int): ShoppingItem? = items.getOrNull(position)
        override fun getItemId(position: Int): Long = position.toLong()

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
            val view = convertView ?: android.view.LayoutInflater.from(context)
                .inflate(R.layout.item_shopping_item, parent, false)
            val item = items[position]

            val tvText = view.findViewById<TextView>(R.id.tvText)
            tvText.text = item.text
            val flags = tvText.paintFlags
            tvText.paintFlags = if (item.done) {
                flags or Paint.STRIKE_THRU_TEXT_FLAG
            } else {
                flags and Paint.STRIKE_THRU_TEXT_FLAG.inv()
            }
            tvText.setTextColor(
                ContextCompat.getColor(
                    context,
                    if (item.done) R.color.text_secondary else R.color.text_primary,
                ),
            )

            val tvQty = view.findViewById<TextView>(R.id.tvQty)
            val qty = item.qty
            if (qty.isNullOrBlank()) {
                tvQty.visibility = View.GONE
            } else {
                tvQty.visibility = View.VISIBLE
                tvQty.text = "Кол-во: $qty"
            }

            val cb = view.findViewById<CheckBox>(R.id.cbDone)
            cb.isChecked = item.done
            // отдельный listener (не OnCheckedChangeListener): при переиспользовании
            // view установка isChecked не должна дёргать обработчик
            cb.setOnClickListener { onToggle(item) }
            return view
        }
    }
}
