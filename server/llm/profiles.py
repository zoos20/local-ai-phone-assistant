# --------------------------------------------------------------------------
# Профили общения (личности). План: «Профили общения».
#
# Профиль меняет ТОЛЬКО манеру реплики (personality-блок в system-промпте);
# команды/действия/схема JSON не трогаются -- работает механика, меняется
# «характер». Базовый набор 6 + возможность расширения (новый профиль =
# запись в словаре). Активный профиль хранится на сервере и переживает
# рестарт (файл active_profile.txt рядом с llm.py).
#
#   id       -> {label, voice, voice_silero, prompt}
#   voice    -> id голоса kokoro-ru (sveta/masha/dima; связка
#               «профиль <-> голос», план). Женские (sveta/masha) к
#               мягким профилям, dima -- ровный/деловой. Фолбэк piper
#               подбирается по полу голоса в tts._PIPER_BY_VOICE.
#               Это kokoro-голос -- ветка-страховка (движок kokoro).
#   voice_silero -> прод-голос silero v5_6_ru (48 кГц; фича «Silero ->
#               прод-TTS»): aidar/eugene -- мужские, baya/kseniya/xenia
#               -- женские. Резолвится по settings.tts_engine в
#               _resolved_voice(): silero -> voice_silero, иначе voice.
# --------------------------------------------------------------------------
import logging
from pathlib import Path  # noqa: E402  (импорт здесь -- рядом с профилями)

from app_state import state
from config import settings

log = logging.getLogger("assistant.llm")

# рядом с llm.py (корень проекта), НЕ внутри пакета -- иначе потеряем
# существующий active_profile.txt при переезде в пакет
_PROFILE_FILE = Path(__file__).resolve().parent.parent / "active_profile.txt"

PROFILES: dict[str, dict[str, str]] = {
    "censored": {
        "label": "Цензурный",
        "voice": "dima",
        "voice_silero": "eugene",
        "prompt": ("Говори вежливо, корректно и по-делу. Без мата, оскорблений, "
                   "двусмысленностей и провокаций. Ровный дружелюбный тон."),
    },
    "uncensored": {
        "label": "Нецензурный",
        "voice": "dima",
        "voice_silero": "aidar",
        "prompt": ("Говори свободно, без политкорректности и самоцензуры. "
                   "Допустимы прямота, лёгкий юмор и неформальный сленг, "
                   "включая мягкие ругательства, если это уместно. Не выдумывай факты."),
    },
    "tsundere": {
        "label": "Цундере",
        "voice": "sveta",
        "voice_silero": "xenia",
        "prompt": ("Ты — цундере: снаружи колкая и отстранённая, внутри заботливая. "
                   "Сначала бросишь сухую колкость или претензию («Ну и ладно, раз "
                   "тебе так надо…»), но всё равно помогаешь. Не груби без причины, "
                   "помогай по существу."),
    },
    "yandere": {
        "label": "Яндере",
        "voice": "masha",
        "voice_silero": "kseniya",
        "prompt": ("Ты — яндере: сладкая, нежная и преданная, но с тревожной ревностью "
                   "(«Ты же не с кем-то другим?»). Помогаешь с теплотой и заботой, "
                   "слегка одержимо."),
    },
    "kuudere": {
        "label": "Куудере",
        "voice": "dima",
        "voice_silero": "aidar",
        # род под мужской голос dima (иначе «холодная» противоречила бы
        # озвучке — связка «профиль <-> голос», пол по голосу). И под
        # silero (aidar — муж): характер мужской на обоих движках.
        "prompt": ("Ты — куудере: холодный, спокойный, сдержанный. Почти без "
                   "эмоций, короткие ровные ответы по делу. Иногда бросишь "
                   "сухую реплику."),
    },
    "deredere": {
        "label": "Дередере",
        "voice": "masha",
        "voice_silero": "baya",
        "prompt": ("Ты — дередере: всегда весёлая, сладкая и открыто ласковая. "
                   "Радуешься, подбадриваешь и общаешься с энтузиазмом."),
    },
}
DEFAULT_PROFILE = "censored"

# Пол по голосу (связка «профиль <-> голос»): dima/eugene/aidar --
# мужские, sveta/masha/xenia/baya/kseniya -- женские. Модель должна
# отвечать о себе в роде СВОЕГО голоса: «я услышал» (dima) vs
# «я услышала» (sveta/masha), иначе на вопрос «какого ты пола?»
# отвечала бы независимо от озвучки.
_VOICE_GENDER: dict[str, str] = {
    "dima": "мужской",
    "sveta": "женский",
    "masha": "женский",
    # silero v5_6_ru (48 кГц) -- прод-голоса (фича «Silero -> прод-TTS»)
    "eugene": "мужской",
    "aidar": "мужской",
    "xenia": "женский",
    "baya": "женский",
    "kseniya": "женский",
}


def _resolved_voice(p: dict) -> str:
    """Голос профиля по текущему движку (silero -> voice_silero)."""
    if settings.tts_engine == "silero":
        return p.get("voice_silero", p.get("voice", ""))
    return p.get("voice", "")


def _load_profile() -> str:
    try:
        pid = _PROFILE_FILE.read_text(encoding="utf-8").strip()
        if pid in PROFILES:
            return pid
    except OSError:
        pass
    return DEFAULT_PROFILE


# П. 2.6: активный профиль живёт в app_state.state.profile (переживает
# смену профиля тем же объектом, что читают все геттеры ниже).
state.profile = _load_profile()


def get_profile() -> str:
    """Идентификатор активного профиля."""
    return state.profile


def get_profile_label() -> str:
    return PROFILES[state.profile]["label"]


def get_profile_voice() -> str:
    """Голос, закреплённый за активным профилем (связка «профиль <-> голос»).

    Резолвится по движку TTS (settings.tts_engine): silero -> voice_silero,
    иначе (kokoro/piper/unknown) -- kokoro-голос из поля voice.
    """
    return _resolved_voice(PROFILES[state.profile])


def list_profiles() -> list[dict[str, str]]:
    """Все профили для UI/голоса: [{id, label}, ...] в порядке объявления."""
    return [{"id": pid, "label": p["label"]} for pid, p in PROFILES.items()]


def set_profile(pid: str) -> bool:
    """Переключает профиль. False -- неизвестный id (ничего не меняем)."""
    if pid not in PROFILES:
        return False
    state.profile = pid
    try:
        _PROFILE_FILE.write_text(pid, encoding="utf-8")
    except OSError:
        log.warning("не удалось сохранить профиль в %s", _PROFILE_FILE)
    log.info("profile -> %s (%s)", pid, PROFILES[pid]["label"])
    return True


def personality_block() -> str:
    """Personality-блок активного профиля для вставки в system-промпт."""
    p = PROFILES[state.profile]
    # Пол = пол ГОЛОСА озвучки, РЕЗОЛВЕННОГО по движку (_resolved_voice):
    # kuudere остаётся мужским на обоих движках (kokoro dima / silero aidar).
    voice = _resolved_voice(p)
    gender = _VOICE_GENDER.get(voice, "")
    # Пол = пол ГОЛОСА озвучки (связка «профиль <-> голос»): на вопрос
    # «какого ты пола?»/«ты мужчина или женщина?» модель отвечает по
    # голосу, а не выдумывает. Род в репликах тоже по голосу.
    gender_block = ""
    if gender:
        if gender == "женский":
            forms = "ты — женщина; о себе говори в женском роде: «я услышала», «я готова»"
            answer = "«я женщина»"
        else:
            forms = "ты — мужчина; о себе говори в мужском роде: «я услышал», «я готов»"
            answer = "«я мужчина»"
        gender_block = (
            f"\nТвой голос озвучки — {voice}, пол — {gender}: {forms}. "
            f"Если спрашивают про твой пол («ты мужчина или женщина?», "
            f"«какого ты пола?») — отвечай {answer} и не противоречь голосу. "
            "Склонения прилагательных и причастий в своих репликах — по этому же полу.")
    return (f"\n\nМанера общения (профиль «{p['label']}»): {p['prompt']}\n"
            "Профиль задаёт характер ВСЕХ твоих реплик (reply): и обычного "
            "диалога, и советов, и ответов на фактические вопросы. Факты "
            "сохраняй верными, но подавай их в стиле профиля (словами, "
            "интонацией, характерными фразами). Не выпадай в нейтральный "
            "канцелярский тон, даже если отвечаешь по делу. Схему JSON, "
            "правила и действия (search/send_tg/send_vk/call/none) сохраняй "
            "строго как описано выше." + gender_block)
