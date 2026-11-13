# -*- coding: utf-8 -*-
"""Задача 2 фичи «Silero -> прод-TTS»: ветка tts_engine="silero" в tts.py.

Проверяется маршрутизация движков (silero -> sidecar :8092, kokoro ->
:8091, иначе piper), payload/URL запроса, фолбэк piper по полу для
silero-голосов, сохранность kokoro-ветки и старого маппинга, дефолт
config.py, контракт пустого текста и фолбэк при не-WAV ответе.
Все тесты БЕЗ сети и sidecar'ов: HTTP подменяется monkeypatch'ем.
"""
from __future__ import annotations

import json

import tts as tts_mod
from config import Settings, settings


class FakeResp:
    """Заглушка ответа urlopen: контекст-менеджер с read()."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeVoice:
    """Заглушка piper-голоса: synthesize_wav пишет валидный WAV-контейнер."""

    def synthesize_wav(self, text: str, wav) -> None:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16)


def _recorder(monkeypatch, attr: str, result: bytes) -> list[tuple]:
    """Подменяет tts.<attr> функцией-рекордером, возвращает список вызовов."""
    calls: list[tuple] = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(tts_mod, attr, fake)
    return calls


def _patch_piper(monkeypatch) -> list[str]:
    """Подменяет _get_voice: записывает имя голоса, отдаёт фейк-piper."""
    names: list[str] = []

    def fake_get_voice(name):
        names.append(name)
        return _FakeVoice()

    monkeypatch.setattr(tts_mod, "_get_voice", fake_get_voice)
    return names


class TestSileroRouting:
    """Ветка tts_engine="silero" уходит в sidecar :8092, не в kokoro."""

    def test_silero_engine_routes_to_sidecar(self, monkeypatch):
        monkeypatch.setattr(settings, "tts_engine", "silero")  # образец :149
        silero_calls = _recorder(monkeypatch, "_synthesize_silero", b"RIFFxxxx")
        kokoro_calls = _recorder(monkeypatch, "_synthesize_kokoro", b"RIFFkkk")

        out = tts_mod.synthesize("  Привет!  ", "eugene")

        assert out == b"RIFFxxxx"
        # вызван с чистым текстом («Привет!» не меняется) и голосом профиля
        assert silero_calls == [(("Привет!", "eugene"), {})]
        assert kokoro_calls == []            # kokoro-ветка не задействована

    def test_silero_payload_and_url(self, monkeypatch):
        monkeypatch.setattr(settings, "tts_engine", "silero")
        seen: list[tuple] = []

        def fake_urlopen(req, timeout=None):
            seen.append((req, timeout))
            return FakeResp(b"RIFF24k")

        monkeypatch.setattr(tts_mod.urllib.request, "urlopen", fake_urlopen)

        assert tts_mod.synthesize("Встреча.", "eugene") == b"RIFF24k"

        req, timeout = seen[0]
        assert req.full_url == "http://127.0.0.1:8092/synth"
        assert req.get_method() == "POST"
        assert timeout == 120
        assert json.loads(req.data.decode("utf-8")) == (
            {"text": "Встреча.", "voice": "eugene"})

        # пустой voice -> поля "voice" в payload нет (как у kokoro)
        seen.clear()
        tts_mod.synthesize("Встреча.", None)
        payload = json.loads(seen[0][0].data.decode("utf-8"))
        assert payload == {"text": "Встреча."}
        assert "voice" not in payload

    def test_silero_not_riff_raises_and_falls_back(self, monkeypatch):
        # sidecar вернул не-WAV -> RuntimeError съеден веткой фолбэка,
        # озвучка уходит на piper (ассистент не молчит)
        monkeypatch.setattr(settings, "tts_engine", "silero")
        monkeypatch.setattr(tts_mod.urllib.request, "urlopen",
                            lambda req, timeout=None: FakeResp(b"NOTWAV"))
        names = _patch_piper(monkeypatch)

        assert tts_mod.synthesize("Привет", "eugene").startswith(b"RIFF")
        assert names == ["ru_RU-denis-medium"]   # eugene -- муж -> denis


class TestSileroFallbackPiper:
    """Мёртвый sidecar -> штатный piper-фолбэк, имя голоса по полу."""

    def test_silero_fallback_to_piper_by_gender(self, monkeypatch):
        monkeypatch.setattr(settings, "tts_engine", "silero")

        def broken(req, timeout=None):
            raise OSError("sidecar недоступен")

        monkeypatch.setattr(tts_mod.urllib.request, "urlopen", broken)
        names = _patch_piper(monkeypatch)

        assert tts_mod.synthesize("Привет", "eugene").startswith(b"RIFF")
        assert tts_mod.synthesize("Привет", "aidar").startswith(b"RIFF")
        assert names[-2:] == ["ru_RU-denis-medium", "ru_RU-denis-medium"]

        assert tts_mod.synthesize("Привет", "xenia").startswith(b"RIFF")
        assert tts_mod.synthesize("Привет", "baya").startswith(b"RIFF")
        assert tts_mod.synthesize("Привет", "kseniya").startswith(b"RIFF")
        assert names[2:] == ["ru_RU-irina-medium"] * 3

        # voice=None -> дефолт settings.tts_voice
        tts_mod.synthesize("Привет", None)
        assert names[-1] == settings.tts_voice


class TestKokoroBranchIntact:
    """Kokoro-ветка байт-в-байт прежняя; piper-путь без sidecar'ов."""

    def test_kokoro_branch_not_broken(self, monkeypatch):
        monkeypatch.setattr(settings, "tts_engine", "kokoro")
        kokoro_calls = _recorder(monkeypatch, "_synthesize_kokoro",
                                 b"RIFFkokoro")
        silero_calls = _recorder(monkeypatch, "_synthesize_silero",
                                 b"RIFFsilero")

        assert tts_mod.synthesize("Привет", "dima") == b"RIFFkokoro"
        assert kokoro_calls == [(("Привет", "dima"), {})]
        assert silero_calls == []

        # engine="piper" -> ни один sidecar не задействован
        monkeypatch.setattr(settings, "tts_engine", "piper")
        kokoro_calls.clear()
        names = _patch_piper(monkeypatch)
        assert tts_mod.synthesize("Привет", "sveta").startswith(b"RIFF")
        assert kokoro_calls == []
        assert silero_calls == []
        assert names == ["ru_RU-irina-medium"]   # piper по полу голоса


class TestMappingAndDefaults:
    """Маппинг piper расширен, дефолт движка -- silero, пустой текст = b\"\"."""

    def test_piper_mapping_preserves_kokoro_and_adds_silero(self):
        m = tts_mod._PIPER_BY_VOICE
        # старые записи НЕ удалены (kokoro-ветка жива)
        assert m["sveta"] == "ru_RU-irina-medium"
        assert m["masha"] == "ru_RU-irina-medium"
        assert m["dima"] == "ru_RU-denis-medium"
        # пять новых записей (silero -> piper по полу)
        assert m["eugene"] == "ru_RU-denis-medium"
        assert m["aidar"] == "ru_RU-denis-medium"
        assert m["xenia"] == "ru_RU-irina-medium"
        assert m["baya"] == "ru_RU-irina-medium"
        assert m["kseniya"] == "ru_RU-irina-medium"

    def test_config_default_is_silero(self):
        # Settings читает .env: ключа ASSISTANT_TTS_ENGINE там НЕТ
        # (проверено планировщиком 12.11) -- действует дефолт config.py.
        # Если тест упал, в .env появился этот ключ -> эскалация юзеру
        # (перекрытие дефолта задокументирует задача 5).
        assert Settings().tts_engine == "silero"

    def test_empty_text_returns_b_empty(self, monkeypatch):
        # текущий контракт: пустой текст -> b"" при любом движке
        silero_calls = _recorder(monkeypatch, "_synthesize_silero", b"RIFFxxxx")
        kokoro_calls = _recorder(monkeypatch, "_synthesize_kokoro", b"RIFFkkk")

        monkeypatch.setattr(settings, "tts_engine", "silero")
        assert tts_mod.synthesize("", "eugene") == b""

        monkeypatch.setattr(settings, "tts_engine", "kokoro")
        assert tts_mod.synthesize("", "eugene") == b""

        assert silero_calls == []
        assert kokoro_calls == []
