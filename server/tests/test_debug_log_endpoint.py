"""Тесты приёмника логов: POST /debug/log («беспроводной дебагинг», задача 4).

Критерии: KA7 (под токеном: 401 / 2xx / 4xx, не 500), решения плана п.1
(схема батча, лимиты 200 записей и 256 КБ, битое тело -> 400) и п.5 (формат
строки `[<ts ISO-8601>] <LEVEL>/<tag>: <msg>`), FR-C1 — существующие тесты
не ослабляются и не удаляются (правится только этот новый файл).

Каталог логов переопределён в tmp_path ДО утверждений о содержимом: дефолтный
logs/phone.log создаётся уже при импорте server.py, поэтому фикстура ставит
свой хендлер на tmp-файл (прежние _setup_phone_log снимает и закрывает);
в teardown дефолтный хендлер пересоздаётся той же server._setup_phone_log() --
реальный logs/phone.log тестами не трогается.

Кейсы из QA-ревью задачи 3: >200 записей, обе ветки лимита тела (предпроверка
по Content-Length и chunked-счётчик без CL), ложный/битый Content-Length,
gzip/multipart/глубокая вложенность JSON -> 400 (не 500), фолбэк времени на
непредставимый ts, обрезка msg/tag, дословная запись "%s", изоляция логгера
"phone" от общего root (server.log/консоль).
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from config import settings

TOKEN = "test-token"
# Точный формат строки (решение плана п.5): [<ts ISO-8601>] <LEVEL>/<tag>:
LINE_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T.*\] [DIWE]/")


@pytest.fixture()
def phone_log_path(tmp_path, monkeypatch):
    """Файл логов в tmp_path + включённый токен API (мидлварь активна).

    Хендлер переопределяется ДО первого запроса: дефолтный logs/phone.log
    создаётся при импорте server.py, утверждения о содержимом относятся
    только к tmp-файлу.
    """
    monkeypatch.setattr(settings, "api_token", TOKEN)
    target = tmp_path / "phone.log"
    server._setup_phone_log(target)      # прежние хендлеры снимаются и закрываются
    yield target
    # re-add сохранённых хендлеров вернул бы мёртвый (закрытый) хендлер:
    # пересоздаём дефолтный хендлер logs/phone.log той же фабрикой
    server._setup_phone_log()


def _client() -> TestClient:
    # raise_server_exceptions=False: HTTPException -> JSON-ответ, не исключение
    return TestClient(server.app, raise_server_exceptions=False)


def _post(body, *, token: str | None = TOKEN, extra_headers: dict | None = None):
    """POST /debug/log; body — bytes или итератор байтов (chunked, без CL)."""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Gemma-Token"] = token
    if extra_headers:
        headers.update(extra_headers)
    return _client().post("/debug/log", content=body, headers=headers)


def _post_entries(entries: list[dict], *, token: str | None = TOKEN):
    return _post(json.dumps({"entries": entries}).encode(), token=token)


def _entry(**overrides) -> dict:
    entry = {"ts": 0, "level": "I", "tag": "Tag", "msg": "hello"}
    entry.update(overrides)
    return entry


def _read(path) -> str:
    """Содержимое лог-файла; файла может не быть, если записей не было."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestTokenMiddleware:
    """Мидлварь require_api_token: /debug/log под токеном, /health — нет."""

    def test_401_without_token(self, phone_log_path):
        resp = _post_entries([_entry()], token=None)
        assert resp.status_code == 401
        assert _read(phone_log_path) == ""   # отказ ДО записи

    def test_401_with_wrong_token(self, phone_log_path):
        resp = _post_entries([_entry()], token="wrong-token")
        assert resp.status_code == 401
        assert _read(phone_log_path) == ""

    def test_health_200_without_token(self, phone_log_path):
        # /health остаётся в исключениях мидлвари (регрессия расширения)
        resp = _client().get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_debug_log_not_in_token_exempt(self):
        assert "/debug/log" not in server._TOKEN_EXEMPT

    def test_token_exempt_exactly_health_and_cert(self):
        # запрет №5 плана: _TOKEN_EXEMPT = {"/health", "/cert"} не расширять
        assert server._TOKEN_EXEMPT == {"/health", "/cert"}


class TestValidBatch:
    """Валидный батч: 200, ответ {ok, written}, строка в tmp-файле."""

    def test_200_ok_written_and_lines(self, phone_log_path):
        resp = _post_entries([_entry(), _entry(msg="second")])
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "written": 2}
        assert len(_read(phone_log_path).splitlines()) == 2

    def test_line_exact_format_and_content(self, phone_log_path):
        resp = _post_entries([_entry(ts=1700000000000, level="W",
                                     tag="MyTag", msg="hello world")])
        assert resp.status_code == 200
        lines = _read(phone_log_path).splitlines()
        assert len(lines) == 1
        # точный формат строки без серверного префикса `asctime INFO phone:`
        assert re.fullmatch(
            r"\[\d{4}-\d{2}-\d{2}T[0-9:.]+\] W/MyTag: hello world",
            lines[0]), lines[0]

    def test_line_format_regex_all_levels(self, phone_log_path):
        # формат из плана (решение 5) на всех уровнях D|I|W|E
        entries = [_entry(level=lv, msg=f"m-{lv}") for lv in "DIWE"]
        resp = _post_entries(entries)
        assert resp.status_code == 200
        lines = _read(phone_log_path).splitlines()
        assert len(lines) == 4
        for line in lines:
            assert LINE_RE.match(line), line

    def test_handler_redirected_to_tmp_not_root_log(self, phone_log_path):
        # санити фикстуры: пишем в tmp, а не в корневой logs/phone.log
        handler = server.phone_log.handlers[0]
        assert Path(handler.baseFilename).resolve() == phone_log_path.resolve()
        assert phone_log_path.resolve() != server._PHONE_LOG_PATH.resolve()


class TestBrokenBody400:
    """Битое/невалидное тело -> 400, не 500 (решение плана п.1)."""

    @pytest.mark.parametrize("body, why", [
        (b"{ not json at all", "не-JSON"),
        (b"{}", "нет поля entries"),
        (b'{"entries": []}', "пустой entries"),
        (b"[1, 2, 3]", "JSON-массив вместо объекта"),
        (b'{"entries": [{"ts": 0, "level": "V", "tag": "t", "msg": "m"}]}',
         "невалидный level (не D|I|W|E)"),
        (b'{"entries": [{"ts": 0, "level": "I", "tag": "t", "msg": 123}]}',
         "msg не строка"),
    ])
    def test_broken_body_400(self, phone_log_path, body, why):
        resp = _post(body)
        assert resp.status_code == 400, why
        assert _read(phone_log_path) == ""

    def test_too_many_entries_400(self, phone_log_path):
        # лимит батча — 200 записей (решение плана по open question №1)
        resp = _post_entries([_entry() for _ in range(201)])
        assert resp.status_code == 400
        assert _read(phone_log_path) == ""

    def test_broken_body_generic_detail(self, phone_log_path):
        # security-фикс: detail НЕ эхоит payload/input_value клиента
        # (содержимое ошибки уходит только в server.log) — строгое равенство
        # generic-текста; статус-коды покрыты соседними тестами, не дублируем
        generic = "битое тело запроса (см. server.log)"
        # (1) битое тело (не-JSON)
        resp = _post(b"{ not json at all")
        assert resp.json()["detail"] == generic
        # (2) валидный JSON, ошибка типизации (level не D|I|W|E)
        resp = _post(json.dumps({"entries": [_entry(level="X")]}).encode())
        assert resp.json()["detail"] == generic


class TestBodyLimit256Kb:
    """Лимит тела 256 КБ: обе ветки -> 413/400, не 500 и не запись."""

    def test_oversized_honest_content_length_413(self, phone_log_path):
        # лимит зафиксирован планом (решение №1): 256 КБ
        assert server._DEBUG_LOG_MAX_BYTES == 256 * 1024
        # честный CL больше лимита: тело реально больше 256 КБ
        pad = "x" * (server._DEBUG_LOG_MAX_BYTES + 1024)
        resp = _post(json.dumps({"entries": [_entry(msg=pad)]}).encode())
        assert resp.status_code == 413
        assert _read(phone_log_path) == ""

    def test_content_length_precheck_before_reading_413(self, phone_log_path):
        # CL заявлен больше лимита, тело короткое: 413 ДО чтения тела
        # (без предпроверки сервер прочитал бы тело и вернул 400)
        resp = _post(b'{"entries": []}',
                     extra_headers={"Content-Length":
                                    str(server._DEBUG_LOG_MAX_BYTES + 1)})
        assert resp.status_code == 413
        assert _read(phone_log_path) == ""

    def test_oversized_chunked_without_content_length_413(
            self, phone_log_path):
        # итератор -> transfer-encoding без content-length: ловит
        # нарастающий счётчик, а не предпроверка
        chunk = b"x" * 8192

        def body():
            for _ in range(40):        # 320 КБ > 256 КБ
                yield chunk

        resp = _post(body())
        assert resp.status_code == 413
        assert _read(phone_log_path) == ""

    def test_lying_content_length_413(self, phone_log_path):
        # клиент заявил 10 байт, а отправил 300 КБ -> счётчик без CL-доверия
        pad = "x" * (300 * 1024)
        resp = _post(json.dumps({"entries": [_entry(msg=pad)]}).encode(),
                     extra_headers={"Content-Length": "10"})
        assert resp.status_code == 413
        assert _read(phone_log_path) == ""

    def test_broken_content_length_400(self, phone_log_path):
        # не-int Content-Length -> явный 400, а не 500 внутри int()
        resp = _post(b'{"entries": []}',
                     extra_headers={"Content-Length": "abc"})
        assert resp.status_code == 400
        assert _read(phone_log_path) == ""


class TestHostilePayloads:
    """Чужие форматы/парсерные грабли -> 400, а не 500."""

    def test_gzip_body_400(self, phone_log_path):
        resp = _post(gzip.compress(
            json.dumps({"entries": [_entry()]}).encode()))
        assert resp.status_code == 400
        assert _read(phone_log_path) == ""

    def test_multipart_body_400(self, phone_log_path):
        body = (b"--bnd\r\nContent-Disposition: form-data; name=\"file\";"
                b"\r\n\r\nnot json\r\n--bnd--\r\n")
        resp = _post(body)
        assert resp.status_code == 400
        assert _read(phone_log_path) == ""

    def test_deep_nested_json_400_not_500(self, phone_log_path):
        # глубже рекурсии json в пределах лимита размера: RecursionError
        # должен стать 400 (тот же приём, что в manifest)
        resp = _post(("[" * 20000 + "]" * 20000).encode())
        assert resp.status_code == 400
        assert _read(phone_log_path) == ""


class TestTimestampFallback:
    """ts=0/отрицательный/непредставимый -> время сервера, 200, не 500."""

    @pytest.mark.parametrize("ts, why", [
        (0, "ts=0 -> серверное время"),
        (-1, "отрицательный ts"),
        (10**30, "год вне datetime (OverflowError)"),
        (10**400, "переполнение float при делении на 1000"),
    ])
    def test_unusable_ts_200_server_time(self, phone_log_path, ts, why):
        resp = _post_entries([_entry(ts=ts, msg="fallback")])
        assert resp.status_code == 200, why
        lines = _read(phone_log_path).splitlines()
        assert len(lines) == 1
        match = re.fullmatch(
            r"\[(?P<when>\d{4}-\d{2}-\d{2}T[0-9:.]+)\] I/Tag: fallback",
            lines[0])
        assert match, lines[0]
        when = datetime.fromisoformat(match.group("when"))
        # фолбэк — время СЕРВЕРА (с запасом на любые часы), не клиентское
        assert abs((datetime.now() - when).total_seconds()) < 300, why


class TestTruncation:
    """Длинные msg/tag и переводы строк -> обрезка/схлопывание, не 400."""

    def test_long_msg_truncated_to_4096(self, phone_log_path):
        resp = _post_entries([_entry(msg="m" * 5000)])
        assert resp.status_code == 200        # обрезка, а не отказ батча
        line = _read(phone_log_path).splitlines()[0]
        msg = line.split(": ", 1)[1]
        assert msg == "m" * server._PHONE_LOG_MSG_MAX
        assert server._PHONE_LOG_MSG_MAX == 4096   # задокументированный лимит

    def test_long_tag_truncated_to_100(self, phone_log_path):
        resp = _post_entries([_entry(tag="t" * 200, msg="ok")])
        assert resp.status_code == 200
        line = _read(phone_log_path).splitlines()[0]
        tag = line.split("] ", 1)[1].split("/", 1)[1].split(": ", 1)[0]
        assert tag == "t" * server._PHONE_LOG_TAG_MAX
        assert server._PHONE_LOG_TAG_MAX == 100

    def test_newlines_collapsed_one_line_per_entry(self, phone_log_path):
        # одна запись = одна строка файла (иначе ломается tail/фильтрация)
        resp = _post_entries([_entry(msg="line1\nline2\r\nline3")])
        assert resp.status_code == 200
        lines = _read(phone_log_path).splitlines()
        assert len(lines) == 1
        assert "line1 line2 line3" in lines[0]


class TestControlSanitization:
    """Управляющие C0/C1 и Unicode-разделители -> пробелы (security-фикс).

    Без санитизации ANSI-эскейпы (\x1b…) перекрашивали бы консоль оператора
    в `Get-Content -Wait`, а \x00/\x85/\u2028/\u2029 ломали бы
    splitlines-инструменты (вторая «запись» из одной строки). tab \x09,
    кириллица и эмодзи — НЕ управляющие и остаются дословно.
    """

    def test_controls_stripped_text_kept(self, phone_log_path):
        resp = _post_entries([
            _entry(tag="A\x9bB\x7fC",            # C1 CSI + DEL
                   msg="\x1b[31mred\u2028end\x00x"),
        ])
        assert resp.status_code == 200
        lines = _read(phone_log_path).splitlines()
        # разделителей в файле не осталось: одна запись = одна строка
        assert len(lines) == 1
        line = lines[0]
        assert LINE_RE.match(line), line          # формат строки цел
        # текст дошёл, управляющие схлопнуты в пробелы
        assert "red end x" in line, line
        assert "A B C" in line, line              # tag тоже санитизирован
        for ch in ("\x1b", "\x00", "\x07", "\x7f", "\x85", "\x9b",
                   "\u2028", "\u2029"):
            assert ch not in line, repr(ch)

    def test_tab_preserved(self, phone_log_path):
        # tab \x09 санитайзер не трогает — сообщение не ломаем
        resp = _post_entries([_entry(msg="a\tb")])
        assert resp.status_code == 200
        line = _read(phone_log_path).splitlines()[0]
        assert LINE_RE.match(line), line
        assert "a\tb" in line, line

    def test_cyrillic_emoji_untouched(self, phone_log_path):
        # классы санитайзера только управляющие: обычный текст дословно
        text = "Привет, мир! 🌍 done"
        resp = _post_entries([_entry(tag="Тег", msg=text)])
        assert resp.status_code == 200
        line = _read(phone_log_path).splitlines()[0]
        assert LINE_RE.match(line), line
        assert text in line, line


class TestFormatSafety:
    """Сообщение с % записывается дословно, а не уходит в %-формат логгера."""

    def test_percent_msg_written_verbatim(self, phone_log_path):
        raw = "100%% done: %s %d %0.1f %"
        resp = _post_entries([_entry(msg=raw)])
        assert resp.status_code == 200
        assert raw in _read(phone_log_path)


class TestNoWriteOnFailure:
    """После всех неудачных запросов в файл не добавилось ни строки."""

    def test_failed_requests_leave_file_empty(self, phone_log_path):
        bad_bodies = [
            b"{ not json",
            b"{}",
            b'{"entries": []}',
            json.dumps({"entries": [_entry()] * 201}).encode(),
            json.dumps({"entries": [_entry(level="V")]}).encode(),
            b"x" * (server._DEBUG_LOG_MAX_BYTES + 1),
        ]
        for body in bad_bodies:
            assert _post(body).status_code in (400, 413), body[:40]
        assert _post_entries([_entry()], token=None).status_code == 401
        assert _read(phone_log_path) == ""


class TestIsolation:
    """Записи приёмника не уходят в общий root -> не попадают в server.log.

    caplog здесь НЕ использовать: pytest 9 сам вешает свой handler на все
    non-propagating логгеры (_pytest/logging.py:363-372), поэтому caplog
    увидел бы запись даже при корректном propagate=False. Проба — свой
    handler прямо на root: при регрессии (propagate=True) запись приёмника
    попадёт в пробу, и тест упадёт.
    """

    def test_phone_records_not_propagated_to_root(self, phone_log_path):
        captured: list[str] = []

        class _RootProbe(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record.getMessage())

        probe = _RootProbe(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(probe)
        try:
            marker = "ISOLATION-MARKER-8f21"
            resp = _post_entries([_entry(msg=marker)])
            assert resp.status_code == 200
            assert marker in _read(phone_log_path)   # запись состоялась
            # контроль: запись обычного логгера доходит до root-хендлера
            # (иначе тест не умеет падать, если проба ничего не ловит)
            logging.getLogger("assistant").warning("ROOT-CONTROL-8f21")
            assert "ROOT-CONTROL-8f21" in captured
            # а запись приёмника в root не попала -> не уходит в server.log
            assert not any(marker in line for line in captured)
        finally:
            root.removeHandler(probe)
