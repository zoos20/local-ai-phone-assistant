"""Тесты беспроводного обновления: GET /update/manifest и GET /update/apk.

Настоящий каталог releases/ тесты НЕ трогают: settings.apk_dir подменяется
на tmp_path (monkeypatch). Мидлварь токена реально работает — при заданном
api_token оба эндпоинта без X-Gemma-Token -> 401 (в _TOKEN_EXEMPT их нет).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from config import settings

TOKEN = "test-token"


@pytest.fixture()
def releases(tmp_path, monkeypatch):
    """Каталог релизов в tmp_path + включённый токен API."""
    monkeypatch.setattr(settings, "apk_dir", str(tmp_path))
    monkeypatch.setattr(settings, "api_token", TOKEN)
    return tmp_path


def _client() -> TestClient:
    # raise_server_exceptions=False: HTTPException -> JSON-ответ, а не исключение
    return TestClient(server.app, raise_server_exceptions=False)


def _get(path: str, token: str | None = TOKEN):
    headers = {"X-Gemma-Token": token} if token else {}
    return _client().get(path, headers=headers)


def _write_release(releases, apk_bytes: bytes = b"PK\x03\x04 fake apk") -> dict:
    """Кладёт APK + manifest.json так же, как scripts/build-apk.ps1."""
    apk = releases / "app-debug.apk"
    apk.write_bytes(apk_bytes)
    manifest = {
        "version_code": 42,
        "version_name": "1.2.3",
        "file": "app-debug.apk",
        "size_bytes": len(apk_bytes),
        "sha256": hashlib.sha256(apk_bytes).hexdigest(),
        "released_at": "2026-11-05T12:00:00Z",
    }
    (releases / "manifest.json").write_text(json.dumps(manifest),
                                            encoding="utf-8")
    return manifest


def _patch_manifest(releases, **overrides) -> None:
    """Переписывает manifest.json, подменяя только указанные поля."""
    manifest = json.loads(
        (releases / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(overrides)
    (releases / "manifest.json").write_text(json.dumps(manifest),
                                            encoding="utf-8")


class TestTokenRequired:
    """Оба эндпоинта под токеном: без X-Gemma-Token -> 401 (не 404/200)."""

    def test_manifest_401_without_token(self, releases):
        _write_release(releases)
        resp = _get("/update/manifest", token=None)
        assert resp.status_code == 401

    def test_apk_401_without_token(self, releases):
        _write_release(releases)
        resp = _get("/update/apk", token=None)
        assert resp.status_code == 401

    def test_manifest_401_with_wrong_token(self, releases):
        _write_release(releases)
        resp = _get("/update/manifest", token="wrong-token")
        assert resp.status_code == 401

    def test_apk_401_with_wrong_token(self, releases):
        _write_release(releases)
        resp = _get("/update/apk", token="wrong-token")
        assert resp.status_code == 401


class TestManifestOk:
    """С валидными файлами манифест отдаётся как JSON с нужными полями."""

    def test_200_and_fields_match(self, releases):
        written = _write_release(releases)
        resp = _get("/update/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data == written
        assert data["version_code"] == 42
        assert data["version_name"] == "1.2.3"
        assert data["file"] == "app-debug.apk"

    def test_sha256_and_size_match_real_apk(self, releases):
        _write_release(releases, apk_bytes=b"real apk bytes here")
        resp = _get("/update/manifest")
        assert resp.status_code == 200
        data = resp.json()
        apk = releases / "app-debug.apk"
        assert data["sha256"] == hashlib.sha256(apk.read_bytes()).hexdigest()
        assert data["size_bytes"] == apk.stat().st_size
        assert len(data["sha256"]) == 64  # lowercase hex, SHA-256


class TestApkOk:
    """Скачивание APK: байты идентичны файлу, media_type — APK-архив."""

    def test_200_bytes_identical(self, releases):
        apk_bytes = b"PK\x03\x04 binary apk payload \x00\x01\x02"
        _write_release(releases, apk_bytes=apk_bytes)
        resp = _get("/update/apk")
        assert resp.status_code == 200
        assert resp.content == apk_bytes
        assert (releases / "app-debug.apk").read_bytes() == resp.content

    def test_media_type_apk(self, releases):
        _write_release(releases)
        resp = _get("/update/apk")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == \
            "application/vnd.android.package-archive"


class TestMissingFiles:
    """Нет манифеста/файлов -> 404 с внятным detail (не 500)."""

    def test_nothing_404_both(self, releases):
        resp_m = _get("/update/manifest")
        resp_a = _get("/update/apk")
        assert resp_m.status_code == 404
        assert resp_a.status_code == 404
        assert "manifest.json" in resp_m.json()["detail"]
        assert resp_a.json()["detail"]
        # detail без абсолютных путей сервера (QA #4): путь -- только в лог
        assert str(releases) not in resp_m.json()["detail"]
        assert str(releases) not in resp_a.json()["detail"]

    def test_manifest_without_apk_404(self, releases):
        _write_release(releases)
        (releases / "app-debug.apk").unlink()   # манифест есть, файла нет
        assert _get("/update/manifest").status_code == 200
        resp = _get("/update/apk")
        assert resp.status_code == 404
        assert "app-debug.apk" in resp.json()["detail"]
        assert str(releases) not in resp.json()["detail"]

    def test_broken_manifest_404_both(self, releases):
        (releases / "manifest.json").write_text("{ not json at all",
                                                encoding="utf-8")
        resp_m = _get("/update/manifest")
        resp_a = _get("/update/apk")
        assert resp_m.status_code == 404
        assert resp_a.status_code == 404   # /update/apk тоже читает манифест
        assert "manifest.json" in resp_m.json()["detail"]

    def test_manifest_missing_required_field_404(self, releases):
        # валидный JSON, но без обязательных полей -> 404, а не отдача мусора
        (releases / "manifest.json").write_text('{"version_code": 1}',
                                                encoding="utf-8")
        resp_m = _get("/update/manifest")
        resp_a = _get("/update/apk")
        assert resp_m.status_code == 404
        assert resp_a.status_code == 404
        assert "manifest.json" in resp_m.json()["detail"]

    def test_manifest_not_object_404(self, releases):
        (releases / "manifest.json").write_text("[1, 2, 3]",
                                                encoding="utf-8")
        resp = _get("/update/manifest")
        assert resp.status_code == 404
        assert resp.json()["detail"]


class TestInvalidManifestValues:
    """Битые типы/значения полей -> 404, а не 200 с мусором (QA #2)."""

    @pytest.mark.parametrize("overrides, why", [
        ({"version_code": "42"}, "строка вместо int"),
        ({"version_code": True}, "bool проходил как int (регрессия QA #2)"),
        ({"file": 123}, "число вместо имени файла"),
        ({"sha256": "nope"}, "не 64 hex-символа"),
        ({"size_bytes": 0}, "пустой APK не бывает"),
    ])
    def test_bad_value_404(self, releases, overrides, why):
        _write_release(releases)
        _patch_manifest(releases, **overrides)
        resp_m = _get("/update/manifest")
        resp_a = _get("/update/apk")
        # валидация общая: 404 на обоих, а не отдача мусора парсеру телефона
        assert resp_m.status_code == 404, why
        assert resp_a.status_code == 404, why
        assert "manifest.json" in resp_m.json()["detail"]

    def test_bad_sha256_length_404(self, releases):
        # обрезанный (63 символа) хеш -- отдельно от "nope" из parametrize
        _write_release(releases)
        _patch_manifest(releases, sha256="a" * 63)
        assert _get("/update/manifest").status_code == 404


class TestPathTraversal:
    """Поле `file` с выходом за каталог релизов -> 404, ловушка не отдаётся."""

    def _make_trap(self, releases) -> Path:
        # Файл-ловушка РЕАЛЬНО существует, но за пределами apk_dir:
        # если traversal сработает, он уедет наружу -- этого быть не должно.
        trap = releases.parent / "secret.txt"
        trap.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
        _write_release(releases)
        return trap

    def test_relative_escape_404(self, releases):
        trap = self._make_trap(releases)
        _patch_manifest(releases, file="../secret.txt")
        resp = _get("/update/apk")
        assert resp.status_code == 404
        assert b"TOP-SECRET-CONTENT" not in resp.content
        assert trap.exists()   # ловушка на месте -- её просто не отдаём

    def test_absolute_path_404(self, releases):
        trap = self._make_trap(releases)
        _patch_manifest(releases, file=str(trap))
        resp = _get("/update/apk")
        assert resp.status_code == 404
        assert b"TOP-SECRET-CONTENT" not in resp.content
        assert trap.exists()

    def test_manifest_itself_still_served(self, releases):
        # traversal ловит только отдачу файла: манифест структурно валиден
        # (file -- непустая строка), /update/manifest его отдаёт как есть
        self._make_trap(releases)
        _patch_manifest(releases, file="../secret.txt")
        assert _get("/update/manifest").status_code == 200


class TestToctou:
    """Файл исчез между is_file() и stat() -> 404, а не 500 (QA #1)."""

    def test_apk_vanishes_before_stat_404(self, releases, monkeypatch):
        # Реалистично: scripts/build-apk.ps1 перезаписывает releases/
        # в момент скачивания. Патч is_file -> True имитирует пройденную
        # проверку, unlink -- исчезнувший файл; stat() бросает
        # FileNotFoundError -> обязан стать 404, а не 500.
        _write_release(releases)
        (releases / "app-debug.apk").unlink()
        monkeypatch.setattr(Path, "is_file", lambda self: True)
        resp = _get("/update/apk")
        assert resp.status_code == 404
        assert "app-debug.apk" in resp.json()["detail"]


class TestApkSizeMismatch:
    """Фактический размер APK != size_bytes манифеста -> 404 (финальное ревью).

    build-apk.ps1 перезаписывает APK на месте (Copy-Item -Force): пока файл
    не дописан, манифест старый -- телефон не должен качать битый APK.
    """

    def test_mismatch_404_apk_manifest_200(self, releases):
        _write_release(releases, apk_bytes=b"0123456789")   # 10 байт
        _patch_manifest(releases, size_bytes=999_999)
        # манифест сам по себе валиден -> отдаётся как есть
        assert _get("/update/manifest").status_code == 200
        resp = _get("/update/apk")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        assert "манифест" in detail          # внятный отказ, а не скачивание
        assert str(releases) not in detail    # без путей сервера (QA #4)


class TestManifestSizeLimit:
    """Лимит чтения манифеста: больше лимита -> 404, а не 500."""

    def test_oversized_404_both(self, releases):
        # валидный JSON, но больше лимита (клиент принимает до 64 КБ)
        pad = "x" * (server._MANIFEST_MAX_BYTES + 1)
        (releases / "manifest.json").write_text(json.dumps({"pad": pad}),
                                                encoding="utf-8")
        resp_m = _get("/update/manifest")
        resp_a = _get("/update/apk")
        assert resp_m.status_code == 404     # не 500 и не OOM
        assert resp_a.status_code == 404
        assert "слишком большой" in resp_m.json()["detail"]
        assert str(releases) not in resp_m.json()["detail"]

    def test_deep_nesting_404_not_500(self, releases):
        # в пределах лимита размера (10 КБ), но глубже лимита рекурсии
        # json: RecursionError раньше -> 500, теперь 404 как битый манифест
        (releases / "manifest.json").write_text("[" * 10_000,
                                                encoding="utf-8")
        resp_m = _get("/update/manifest")
        resp_a = _get("/update/apk")
        assert resp_m.status_code == 404
        assert resp_a.status_code == 404
        assert "manifest.json" in resp_m.json()["detail"]
