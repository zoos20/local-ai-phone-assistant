# local-ai-phone-assistant

Локальный голосовой ассистент: телефон слушает постоянно, «мозг» живёт на домашнем
ПК. Ничего не уходит в облако — LLM, распознавание речи и синтез голоса работают
офлайн на вашем железе.

```
┌─────────────────────┐   Wi-Fi (LAN)   ┌──────────────────────────────────┐
│  Android-телефон    │ ──────────────► │  ПК (без GPU, Python 3.12)       │
│  • wake word (Vosk) │   WebSocket /   │  • llama.cpp + LLM (порт 8080)   │
│  • запись речи      │   HTTP REST     │  • server.py  — API (порт 8090)  │
│  • озвучка ответа   │ ◄────────────── │  • kokoro-ru  — TTS (порт 8091)  │
│  • кнопки/интерфейс │   WAV + JSON    │  • faster-whisper — STT (CPU)    │
└─────────────────────┘                 └──────────────────────────────────┘
```

## Возможности

- **Постоянное слушание**: wake word («Гемма») распознаётся на телефоне офлайн
  (Vosk, ограниченная грамматика — только слово-триггер), дальше речь стримится
  на сервер.
- **Диалог**: контекст из последних реплик, уточнения («а завтра?»), follow-up
  запросы в течение короткого окна после ответа.
- **Профили характера с голосами** (переключение голосом: «профиль цундере»):

  | Профиль | Голос | Пол |
  |---|---|---|
  | Цензурный | dima | мужской |
  | Нецензурный | dima | мужской |
  | Цундере | sveta | женский |
  | Яндере | masha | женский |
  | Куудере | dima | мужской |
  | Дередере | masha | женский |

  Пол ассистента выводится из голоса озвучки и задаётся в system-промпте:
  на «ты мужчина или женщина?» он отвечает в роде своего голоса.
- **Распознавание команд профиля**: точные стемы + нечёткое сопоставление
  (difflib) — STT может коверкать имена («Дэри Дэра», «нетнзурный»,
  «сундейра»), и команда всё равно выполняется.
- **Действия с подтверждением**: отправка сообщений в Telegram/VK и звонки
  выполняются только после голосового/кнопочного подтверждения (окно ~30 с).
- **Поиск в интернете** (DuckDuckGo по умолчанию, без API-ключей) с кратким
  голосовым пересказом найденного.
- **Звонки**: поиск контакта в телефонной книге и набор номера.
- **Озвучка**: kokoro-ru (sidecar, несколько русских голосов) с фолбэком на
  Piper.
- **Надёжность**: VAD-фильтр тишины, детект конца фразы, повтор verify при
  ложном wake, подавление обработки во время озвучки.

## Структура проекта

```
server.py               FastAPI: /ask /voice /ws_voice /tts /confirm /profile /health
llm.py                  промпт, профили (характер ↔ голос ↔ пол), стриминг
                        ответа (chat_stream/decide_stream) от llama-server
stt.py                  распознавание речи (faster-whisper, CPU, int8)
tts.py                  озвучка: kokoro-ru через sidecar, фолбэк Piper
tts_kokoro_server.py    sidecar kokoro-ru на 8091 (отдельное окружение .venv-tts)
tools.py                действия: поиск, Telegram, VK, звонки
config.py               настройки из .env (префикс ASSISTANT_)
login_tg.py             разовый вход в Telegram (Telethon user session)
scripts/                скрипты запуска/настройки (PowerShell):
  start-all.ps1           автозапуск стека (llama-server + server.py)
  start-kokoro.ps1        запуск TTS-sidecar
  setup-firewall.ps1      разовый: правило фаервола для порта 8090 (админ)
  setup-adb.ps1           разовый: ADB-интерфейс телефона + фаервол (админ)
android/                клиент-приложение (Kotlin, foreground service)
models/                 голоса Piper (*.onnx) — не в репозитории
```

Python-модули намеренно лежат в корне (плоские импорты `import llm` и пути
`Path(__file__).parent` для `.env`/моделей/данных).

## Требования

- **ПК**: Windows, Python 3.12, без GPU (всё на CPU), llama.cpp с запущенной
  моделью на порту 8080.
- **Телефон**: Android 8.0+ (minSdk 26), приложение в той же Wi-Fi-сети, что и ПК.
- **Сеть**: телефон должен достучаться до портов 8090 (API) и 8080 (LLM).

## Установка сервера

```powershell
# 1. Клонировать репозиторий и перейти в каталог
cd <каталог-проекта>

# 2. Основное окружение
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 3. Окружение для TTS-sidecar (kokoro-ru)
python -m venv .venv-tts
.venv-tts\Scripts\pip install kokoro phonopy pyyaml  # см. start-kokoro.ps1

# 4. Голоса Piper (фолбэк озвучки)
.venv\Scripts\python -m piper.download_voices ru_RU-denis-medium --data-dir models
.venv\Scripts\python -m piper.download_voices ru_RU-irina-medium  --data-dir models

# 5. Настройки
copy .env.example .env     # заполнить (см. «Настройка .env»)
```

Модель для llama.cpp (Gemma/Qwen-класс, квантованная) кладётся в llama.cpp
отдельно — этот репозиторий модель не содержит.

## Запуск

```powershell
# Весь стек сразу (llama-server + API):
powershell -ExecutionPolicy Bypass -File scripts\start-all.ps1

# Или по частям:
<путь-к-llama-server>                      # LLM на 8080
.venv\Scripts\python server.py             # API на 8090
powershell -File scripts\start-kokoro.ps1  # TTS на 8091 (нужен .venv-tts)

# Проверка:
curl http://127.0.0.1:8090/health
```

Разовый сетевой доступ (от имени администратора):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-firewall.ps1   # порт 8090 из LAN
```

## Настройка .env

Скопируйте `.env.example` в `.env` и заполните:

| Ключ | Назначение |
|---|---|
| `ASSISTANT_LLM_BASE_URL` | URL llama.cpp API, напр. `http://192.168.1.100:8080/v1` |
| `ASSISTANT_LLM_MODEL` | имя модели, как её видит llama-server |
| `ASSISTANT_TG_API_ID` / `ASSISTANT_TG_API_HASH` | https://my.telegram.org → API development tools |
| `ASSISTANT_TG_SESSION` | путь к файлу сессии Telethon (создаётся в `login_tg.py`) |
| `ASSISTANT_VK_TOKEN` | токен ВКонтакте с правом `messages` |
| `ASSISTANT_SEARCH_PROVIDER` | `duckduckgo` (по умолчанию) или `brave` |
| `ASSISTANT_BRAVE_API_KEY` | нужен только для `brave` |
| `ASSISTANT_WHISPER_MODEL` | `small` (по умолчанию) или `medium` (точнее, медленнее) |
| `ASSISTANT_TTS_ENGINE` | `kokoro` (по умолчанию) или `piper` |
| `ASSISTANT_HOST` / `ASSISTANT_PORT` | адрес и порт API (по умолчанию `0.0.0.0:8090`) |

Telegram и VK работают **необязательно**: при пустых токенах команды отправки
отвечают отказом, всё остальное функционирует.

Один раз войти в Telegram (нужен заполненный `ASSISTANT_TG_API_ID/HASH`):

```powershell
.venv\Scripts\python login_tg.py
```

## API

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| GET | `/health` | — | `{"status","model","stt_model","tts_engine",...}` |
| POST | `/ask` | `{"text":"..."}` | `{"reply","action","args","needs_confirm","token","elapsed_ms"}` |
| POST | `/voice` | multipart `file` (wav/ogg/webm/m4a) | как `/ask` + `{"text","stt_ms"}` |
| WS | `/ws_voice` | стрим аудио-чанков | события: `speech`/`hear`/`sentence`/`audio` (чанки WAV) → `reply` → `done` |
| POST | `/tts` | `{"text":"...","voice":"..."}` | WAV (`audio/wav`), голос берётся из профиля |
| POST | `/confirm` | `{"token":"...","yes":true\|false}` | `{"reply","ok"}` |
| GET | `/profile` | — | текущий профиль, голос, список доступных |
| POST | `/profile` | `{"profile":"tsundere"}` | переключение профиля |

`action`: `search` | `send_tg` | `send_vk` | `call` | `none`.
Отправка сообщений и звонки выполняются **только** после `/confirm` с
`yes:true` (окно подтверждения ограничено `ASSISTANT_CONFIRM_TTL`).

### Стриминговый конвейер `/ws_voice` (фаза 6)

Ответ озвучивается **по мере генерации**, а не после полного готового текста:

1. LLM стримится через `llm.chat_stream()`; инкрементальный парсер
   (`_StreamParser`) вырезает законченные предложения из поля `reply` и
   отдельно ловит поле `query` (поисковый запрос для спекулятивного поиска).
2. Каждое предложение уходит в TTS (kokoro-ru), и WAV-чанк сразу летит
   телефону событием `audio`; текст — событием `sentence`.
3. **Спекулятивный поиск**: поле `query` из первого токена запускает поиск
   заранее (кэш 180 с, таймаут 15 с) — когда приходит `needs_confirm`,
   готовый результат уже в `/confirm`, не дожидаясь таймаута.
4. Телефон играет чанки по очереди (`chunkQueue`) под SPEAK_LOCK, уважая
   стоп-слово и паузу барж-ин; финальный `reply` с флагом `streamed=true`
   повторно НЕ озвучивается.

Замеры (ПК без GPU, Gemma-4-E4B): первый токен ~1 с, первый звук ~3.5 с
от старта LLM (раньше — 6–15 с после полного ответа).

## Android-приложение

Kotlin-клиент (`android/`), foreground service с доступом к микрофону:

- распознаёт wake word офлайн через **Vosk** (модель подгружается в приложение),
- записывает речь и стримит её на сервер (`/ws_voice`), проигрывает ответ
  по мере готовности — очередью WAV-чанков (события `sentence`/`audio`),
- поддерживает Bluetooth-аудио и системную громкость озвучки,
- follow-up до 10 уточнений подряд без повторного «Гемма»,
- экран показывает адрес сервера, статус подключения и кнопки управления.

Сборка и установка (adb не обязан быть в PATH):

```powershell
$env:JAVA_HOME = '<путь-к-jdk-17>'
cd android
.\gradlew.bat assembleDebug

# Установка на подключённый телефон:
$adb = "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
& $adb push app\build\outputs\apk\debug\app-debug.apk /data/local/tmp/app.apk
& $adb shell pm install -r -d /data/local/tmp/app.apk
```

Адрес сервера вводится прямо на экране приложения (по умолчанию подсказка
`http://192.168.1.100:8090` — замените на IP вашего ПК).

## Константы таймингов

Телефон (`AssistantService.kt`):

| Константа | Значение | Назначение |
|---|---|---|
| `CONFIRM_MS` | 30 000 | окно подтверждения действия |
| `QUIET_END_MS` | 3 000 | тишина, после которой фраза считается законченной |
| `MAX_COMMAND_MS` | 20 000 | предел длины команды |
| `NO_INPUT_MS` | 10 000 | ожидание речи после wake |
| `MIN_SPEECH_MS` | 1 500 | минимум речи, чтобы обрабатывать |
| `SPEECH_RMS` / `WAKE_MIN_RMS` | 330 / 120 | пороги громкости (речь / wake) |
| `VERIFY_RETRY_MS` | 1 500 | окно повторного wake после ложного срабатывания |
| `FOLLOWUP_START_MS` / `FOLLOWUP_TRIG_MS` | 5 000 / 300 | follow-up запросы после ответа |
| `MAX_FOLLOWUP` | 10 | максимум follow-up подряд |

Сервер (`server.py`, конвенции `/ws_voice`):

| Константа | Значение | Назначение |
|---|---|---|
| `_WS_QUIET_MS` | 1 300 | тишина, после которой фраза считается сказанной |
| `_WS_SEG_QUIET_MS` | 900 | пауза между сегментами в предложении |
| `_WS_GRACE_MS` | 2 500 | льготное окно после «конца» фразы |
| prefetch-поиск | TTL 180 с / таймаут 15 с | спекулятивный поиск по `query` из LLM |

Контекст LLM — 32768 токенов (`-c 32768` в llama.cpp), история диалога —
`ASSISTANT_HISTORY_SIZE` реплик с TTL.

## Ограничения

- Работает только в одной локальной сети (телефон ↔ ПК), наружу ничего не
  отдаётся.
- Ответы LLM короткие (1–2 предложения) — это специально: ассистент говорит
  вслух.
- Озвучка и остановка во время произношения управляются с телефона;
  барж-ин (перебивание) намеренно не реализован.

## Лицензия

Apache License 2.0 — см. [LICENSE](LICENSE).
