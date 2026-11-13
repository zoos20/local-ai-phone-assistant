# -*- coding: utf-8 -*-
"""CLI TTS A/B харнесса (задача 5): python -m tts_ab run|blind|report.

Часть A — драйвер прогона (run): адаптеры движков запускаются
СУБПРОЦЕССАМИ СТРОГО ПОСЛЕДОВАТЕЛЬНО (никогда параллельно) с явным
интерпретатором: piper → .venv\\Scripts\\python.exe; kokoro/silero/
supertonic → .venv-ab\\Scripts\\python.exe. Протокол — NDJSON stdin/
stdout (tts_ab.adapters), строки UTF-8. Драйвер:

- ждёт готовность kokoro (/health ≤ 60 с, в ДРАЙВЕРЕ) ДО запуска
  адаптера; недоступен → движок unavailable со строкой «запусти
  scripts/start-kokoro.ps1» (sidecar сам не запускает и не гасит);
- per-phrase READ-таймаут на stdout адаптера 130 с (адаптер сам держит
  120 с — QA-фикс задачи 4; +10 с запас, чтобы штатная timeout-строка
  адаптера приходила раньше) → фраза ok=false «timeout»;
- потолок ~10 мин на движок: движок обрывается graceful с причиной,
  зависший движок не блокирует остальные;
- крах адаптера (неординарный exit) → движок failed/unavailable с уже
  накопленными строками; прогон НИКОГДА не валится из-за одного движка;
- peak_rss_mb: psutil (только .venv-ab) — пик RSS дерева субпроцесса
  адаптера в окне каждой фразы (опрос ~100 мс); для kokoro — пик RSS
  процесса sidecar (cmdline «tts_kokoro_server.py»; не найден → null);
- окружение детей: PYTHONUTF8=1, TORCH_HOME, HF_HOME,
  SUPERTONIC_CACHE_DIR (обе переменные supertonic: одной HF_HOME
  недостаточно — у пакета свой cache-dir);
- шапка прогона → первая строка results.json; все строки результата —
  валидация/слияние metrics; после синтеза — blind и report.

Состояние — локальные переменные драйвера: app_state прод-сервера
харнесс не касается.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from tts_ab import metrics
from tts_ab.adapters import (
    ENGINE_VOICES,
    ENGINES,
    PHRASE_TIMEOUT_S,
    _KOKORO_HEALTH_BUDGET_S,
    _KOKORO_HINT,
    _kokoro_base_url,
    _kokoro_wait_health,
    _row,
)
from tts_ab.blind import DEFAULT_SEED, run_blind
from tts_ab.phrases import PHRASES
from tts_ab.report import (
    engine_statuses,
    run_report,
    write_results_with_header,
)

BASE_DIR = Path(__file__).resolve().parent.parent
RESULTS_PATH = BASE_DIR / "data" / "tts_ab" / "results.json"
REPORT_PATH = BASE_DIR / "data" / "tts_ab" / "report.md"
BLIND_DIR = BASE_DIR / "data" / "tts_ab" / "blind"
CACHE_DIR = BASE_DIR / ".cache"

# Драйвер читает ответ адаптера дольше, чем сам адаптер держит фразу
# (120 с) — штатная timeout-строка адаптера успевает прийти первой.
DRIVER_PHRASE_TIMEOUT_S = PHRASE_TIMEOUT_S + 10.0
# Потолок на движок ~10 мин (ТЗ §6): дальше — graceful обрыв с причиной.
ENGINE_CEILING_S = 600.0
# Окно RSS-опроса psutil (ТЗ §3.4).
RSS_POLL_S = 0.1
_GENDERS = ("m", "f")


def _child_env() -> dict[str, str]:
    """Окружение детей-адаптеров: UTF-8 + кэши весов (ТЗ задания 5)."""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["TORCH_HOME"] = str(CACHE_DIR / "torch_hub")
    env["HF_HOME"] = str(CACHE_DIR / "hf")
    env["SUPERTONIC_CACHE_DIR"] = str(CACHE_DIR / "hf" / "supertonic3")
    return env


def _interpreter_path(engine: str) -> Path:
    if engine == "piper":  # piper-tts уже в .venv — без pip-операций
        return BASE_DIR / ".venv" / "Scripts" / "python.exe"
    return BASE_DIR / ".venv-ab" / "Scripts" / "python.exe"


# --- RSS-окна ---------------------------------------------------------------


class _RssSampler:
    """Пик RSS в окнах фраз (psutil; polled ~100 мс из цикла чтения)."""

    def __init__(self, rss_fn) -> None:
        self._rss_fn = rss_fn  # () -> байты | None
        self._windows: dict[tuple[str, str, int], float] = {}

    def open(self, key: tuple[str, str, int]) -> None:
        self._windows[key] = 0.0
        self.sample(key)

    def sample(self, key: tuple[str, str, int]) -> None:
        current = self._windows.get(key)
        if current is None:
            return
        value = self._rss_fn()
        if value is not None and value > current:
            self._windows[key] = value

    def peak_mb(self, key: tuple[str, str, int]) -> float | None:
        value = self._windows.get(key)
        if value is None or value <= 0.0:
            return None
        return round(value / (1024.0 * 1024.0), 1)


def _make_adapters_rss_fn(proc) -> object:
    """RSS дерева субпроцесса адаптера (proc + дети), байты или None."""

    def rss_fn():
        try:
            total = proc.memory_info().rss
            for child in proc.children(recursive=True):
                total += child.memory_info().rss
            return total
        except Exception:  # psutil.Error и гонки умершего процесса
            return None

    return rss_fn


def _find_sidecar():
    """Процесс kokoro sidecar по cmdline «tts_kokoro_server.py» | None."""
    import psutil  # лениво: только .venv-ab

    for info in psutil.process_iter(["cmdline"]):
        try:
            cmdline = info.info.get("cmdline") or []
        except Exception:
            continue
        if any("tts_kokoro_server.py" in str(part) for part in cmdline):
            return info
    return None


def _make_kokoro_rss_fn() -> object:
    """RSS процесса sidecar; не найден → None всегда (не ошибка)."""

    def rss_fn():
        sidecar = _find_sidecar()
        if sidecar is None:
            return None
        try:
            return sidecar.memory_info().rss
        except Exception:
            return None

    return rss_fn


# --- Запуск одного движка ---------------------------------------------------


def _reader_thread(proc, lines: queue.Queue) -> None:
    """stdout адаптера → очередь строк; в конце — None-сентинел EOF."""
    try:
        for line in proc.stdout:
            line = line.strip()
            if line:
                lines.put(line)
    finally:
        lines.put(None)


def _stderr_thread(proc, tail: deque) -> None:
    """Слив stderr адаптера в кольцевой буфер (для причин краха)."""
    try:
        for line in proc.stderr:
            tail.append(line.rstrip())
    except Exception:
        pass


def _read_row(lines: queue.Queue, deadline: float, sampler: _RssSampler,
              window: tuple[str, str, int], *, eof: list[bool]):
    """Строка ответа с READ-таймаутом + RSS-опрос в окне фразы.

    Возвращает строку или None (таймаут). EOF адаптера фиксируется в
    eof[0] и тоже вернёт None немедленно (дальше фразу ждать нечего).
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            item = lines.get(timeout=min(RSS_POLL_S, remaining))
        except queue.Empty:
            sampler.sample(window)
            continue
        if item is None:
            eof[0] = True
            return None
        sampler.sample(window)
        try:
            row = json.loads(item)
        except ValueError:
            print(f"    [!] не-JSON строка адаптера: {item[:160]}")
            continue
        return row


def _aggregate_row(engine: str, voice: str, phrase_id: str,
                   error: str) -> dict:
    """Строка-причина драйвера (ок=false): контракт metrics соблюдён."""
    row = _row(engine, voice, phrase_id, "", False, None, error=error)
    row["text"] = ""
    return row


def _terminate(proc) -> None:
    """Graceful остановка адаптера: stdin EOF → wait → kill."""
    if proc is None:
        return
    try:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _run_engine(engine: str, phrases: list, voices: list[str], repeats: int,
                log, notes: dict) -> list[dict]:
    """Полный прогон одного движка (последовательно) → строки результата.

    Никогда не бросает исключений наружу по причинам движка: крах
    адаптера/таймауты → ok=false-строки + причина, остальные движки
    продолжают (graceful fail, ТЗ §3.3). notes — словарь для побочных
    фактов (статус kokoro для шапки).
    """
    interpreter = _interpreter_path(engine)
    if not interpreter.exists():
        log(f"[{engine}] FAILED: интерпретатор не найден: {interpreter}")
        return [_aggregate_row(engine, voices[0], "startup",
                               f"failed: интерпретатор не найден: "
                               f"{interpreter}")]
    proc_box: list = []  # catch-all видит proc сразу после Popen
    try:
        return _run_engine_inner(engine, interpreter, phrases, voices,
                                 repeats, log, notes, proc_box)
    except Exception as exc:  # неожиданный баг драйвера не валит прогон
        # Страховка от сироты: исключение между Popen и защищённым
        # блоком цикла (например, psutil.Process на мгновенно умершем
        # адаптере) — убиваем реальный дочерний процесс, а не None.
        _terminate(proc_box[0] if proc_box else None)
        log(f"[{engine}] FAILED: внутренняя ошибка драйвера: "
            f"{type(exc).__name__}: {exc}")
        return [_aggregate_row(engine, voices[0], "startup",
                               f"failed: драйвер: {type(exc).__name__}: "
                               f"{exc}")]


def _run_engine_inner(engine, interpreter, phrases, voices, repeats,
                      log, notes, proc_box) -> list[dict]:
    """Реальный цикл одного движка (без per-движок try/catch драйвера).

    proc_box — mutable-контейнер: сразу после Popen процесс
    записывается в него, чтобы catch-all в _run_engine мог
    терминировать сироту при исключении до входа в защищённый блок.
    """
    import psutil  # лениво: только .venv-ab
    def log_retry(text: str) -> None:
        print(f"    [{engine}] {text}")

    if engine == "kokoro":
        # Готовность sidecar ждёт ДРАЙВЕР до запуска адаптера (ТЗ §6);
        # TTS_AB_KOKORO_URL поддержан адаптером kokoro — тот же URL.
        base = _kokoro_base_url()
        log(f"[{engine}] жду /health {base} (бюджет "
            f"{_KOKORO_HEALTH_BUDGET_S:.0f} с)...")
        health = _kokoro_wait_health(base, _KOKORO_HEALTH_BUDGET_S)
        if health is None:
            notes["kokoro_status"] = (
                f"unavailable: не ответил на /health ({base}) за "
                f"{_KOKORO_HEALTH_BUDGET_S:.0f} с — {_KOKORO_HINT}")
            log(f"[{engine}] UNAVAILABLE: {notes['kokoro_status']}")
            return [_aggregate_row(
                engine, voices[0], "startup",
                f"unavailable: kokoro sidecar не ответил на /health "
                f"({base}) за {_KOKORO_HEALTH_BUDGET_S:.0f} с — "
                f"{_KOKORO_HINT}")]
        voices_known = ", ".join(health.get("voices", []) or [])
        notes["kokoro_status"] = f"ok: /health ответил (voices: {voices_known})"
        log(f"[{engine}] sidecar готов ({voices_known})")

    child_env = _child_env()
    cmd = [str(interpreter), "-m", "tts_ab.adapters", engine]
    proc = subprocess.Popen(
        cmd, cwd=str(BASE_DIR), env=child_env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", bufsize=1)
    proc_box.append(proc)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader_thread, args=(proc, lines),
                     daemon=True).start()
    tail: deque = deque(maxlen=200)
    threading.Thread(target=_stderr_thread, args=(proc, tail),
                     daemon=True).start()

    if engine == "kokoro":
        rss_fn = _make_kokoro_rss_fn()
    else:
        rss_fn = _make_adapters_rss_fn(psutil.Process(proc.pid))
    sampler = _RssSampler(rss_fn)

    rows: list[dict] = []
    eof = [False]
    engine_deadline = time.monotonic() + ENGINE_CEILING_S
    expected_pairs = len(phrases) * len(voices) * max(repeats, 1)
    done_pairs = 0
    aborted = False
    try:
        for phrase in phrases:
            for voice in voices:
                for repeat_no in range(1, max(repeats, 1) + 1):
                    if time.monotonic() >= engine_deadline:
                        left = expected_pairs - done_pairs
                        rows.append(_aggregate_row(
                            engine, voice, "aborted",
                            f"aborted: превышен потолок ~"
                            f"{ENGINE_CEILING_S / 60:.0f} мин на движок, "
                            f"осталось {left} синтезов"))
                        aborted = True
                        break
                    if eof[0]:
                        rows.append(_aggregate_row(
                            engine, voice, "aborted",
                            f"aborted: адаптер завершился преждевременно "
                            f"(exit={proc.poll()}); stderr: "
                            f"{'; '.join(list(tail)[-3:]) or 'пусто'}"))
                        aborted = True
                        break
                    window = (phrase.phrase_id, voice, repeat_no)
                    sampler.open(window)
                    request = {"phrase_id": phrase.phrase_id,
                               "text": phrase.text, "voice": voice}
                    try:
                        proc.stdin.write(json.dumps(request,
                                                    ensure_ascii=False)
                                         + "\n")
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        rows.append(_aggregate_row(
                            engine, voice, "aborted",
                            f"aborted: stdin адаптера закрыт "
                            f"(exit={proc.poll()}); stderr: "
                            f"{'; '.join(list(tail)[-3:]) or 'пусто'}"))
                        aborted = True
                        break
                    # Блокировка одной фразы: ждём СВОЮ строку (протокол
                    # «одна строка запроса → одна строка результата»);
                    # чужие/поздние строки пропускаются в этом же окне.
                    wait_started = time.monotonic()
                    deadline = min(wait_started + DRIVER_PHRASE_TIMEOUT_S,
                                   engine_deadline)
                    row = None
                    while True:
                        candidate = _read_row(lines, deadline, sampler,
                                              window, eof=eof)
                        if candidate is None:
                            break
                        if (isinstance(candidate, dict)
                                and candidate.get("phrase_id")
                                == phrase.phrase_id
                                and candidate.get("voice") == voice):
                            row = candidate
                            break
                        log_retry(f"пропущена чужая строка: "
                                  f"{str(candidate)[:120]}")
                    if row is None:
                        if eof[0]:
                            rows.append(_aggregate_row(
                                engine, voice, "aborted",
                                f"aborted: адаптер завершился преждевременно"
                                f" (exit={proc.poll()}); stderr: "
                                f"{'; '.join(list(tail)[-3:]) or 'пусто'}"))
                            aborted = True
                            break
                        waited = time.monotonic() - wait_started
                        rows.append(_row(
                            engine, voice, phrase.phrase_id, phrase.text,
                            False, None,
                            error=f"timeout: строка результата не пришла "
                                  f"за {waited:.0f} с "
                                  f"(READ-таймаут драйвера)"))
                        log_retry(f"{phrase.phrase_id}/{voice}: TIMEOUT "
                                  f"(фраза ok=false, продолжаю)")
                        continue
                    problem = metrics.validate_result_row(row)
                    if problem is not None:
                        rows.append(_row(
                            engine, voice, phrase.phrase_id, phrase.text,
                            False, None,
                            error=f"адаптер ответил невалидной строкой "
                                  f"({problem})"))
                        continue
                    row["peak_rss_mb"] = sampler.peak_mb(window)
                    rows.append(row)
                    done_pairs += 1
                    status = "ok" if row["ok"] else "FAIL"
                    log_retry(f"{phrase.phrase_id}/{voice} r{repeat_no}: "
                              f"{status} synth="
                              f"{row.get('synth_ms')}мс rtf="
                              f"{row.get('rtf')} rss={row['peak_rss_mb']}МБ")
                if aborted:
                    break
            if aborted:
                break
    finally:
        _terminate(proc)
    ok_count = sum(1 for r in rows if r.get("ok"))
    log(f"[{engine}] готово: {ok_count}/{expected_pairs} ok-строк"
        + (" (оборван по потолку/краху)" if aborted else ""))
    return rows


# --- Шапка прогона ----------------------------------------------------------


def _build_header(params: dict, kokoro_status: str) -> dict:
    """Шапка прогона (ТЗ §3.4): CPU, python, параметры, статус kokoro."""
    import psutil  # лениво: только .venv-ab

    cores = (psutil.cpu_count(logical=False),
             psutil.cpu_count(logical=True))
    cpu_name = ""
    try:  # Windows: человекочитаемая модель из реестра
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            cpu_name, _type = winreg.QueryValueEx(key, "ProcessorNameString")
    except (OSError, ValueError):
        cpu_name = ""
    if not cpu_name:
        cpu_name = platform.processor() or "unknown"
    cpu = (f"{cpu_name.strip()}; {cores[0]} физ. / {cores[1]} лог. ядер")
    return {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu": cpu,
        "python": (f"{platform.python_version()} "
                   f"({sys.executable})"),
        "params": params,
        "kokoro_status": kokoro_status,
        "warning": ("фоновая нагрузка (llama-server и пр.) влияет на числа "
                    "— закройте тяжёлые процессы на время прогона"),
    }


# --- Команды CLI ------------------------------------------------------------


def _parse_engines(raw: str) -> list[str] | None:
    raw = (raw or "all").strip().lower()
    if raw in ("", "all"):
        return ["kokoro", "piper", "silero", "supertonic"]
    engines = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [e for e in engines if e not in ENGINES]
    if unknown or not engines:
        return None
    return engines


def _parse_voices(raw: str) -> list[str] | None:
    raw = (raw or "m,f").strip().lower()
    genders = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [g for g in genders if g not in _GENDERS]
    if unknown or not genders:
        return None
    return genders


def _print_summary(rows: list[dict]) -> None:
    print("\n=== Сводка прогона ===")
    for engine, status, reason in engine_statuses(rows):
        suffix = f" — {reason}" if reason else ""
        print(f"  {engine:<11} {status}{suffix}")


def cmd_run(args) -> int:
    """Прогон: адаптеры последовательно → results.json → blind → report."""
    try:
        import psutil  # noqa: F401 — проверка окружения драйвера
    except ImportError:
        print("tts_ab: psutil недоступен — драйвер запускается под "
              ".venv-ab (см. scripts/setup-tts-ab.ps1): "
              ".venv-ab\\Scripts\\python -m tts_ab run", file=sys.stderr)
        return 1
    engines = _parse_engines(args.engines)
    if engines is None:
        print("tts_ab: --engines: ожидалось all или список из "
              "kokoro,piper,silero,supertonic", file=sys.stderr)
        return 2
    genders = _parse_voices(args.voices)
    if genders is None:
        print("tts_ab: --voices: ожидалось m,f", file=sys.stderr)
        return 2
    repeats = max(int(args.repeats), 1)
    results_path = Path(args.results)
    print(f"tts_ab run: engines={','.join(engines)} "
          f"voices={','.join(genders)} repeats={repeats} → {results_path}")

    rows: list[dict] = []
    kokoro_status: str | None = None
    for engine in engines:
        voices = [ENGINE_VOICES[engine][0] if g == "m" else
                  ENGINE_VOICES[engine][1] for g in genders]
        notes: dict = {}
        rows.extend(_run_engine(
            engine, PHRASES, voices, repeats,
            lambda text: print(text, flush=True), notes))
        if engine == "kokoro":
            kokoro_status = notes.get(
                "kokoro_status", "kokoro: статус не зафиксирован")
    _print_summary(rows)

    merged, merge_problems = metrics.merge_result_rows(rows)
    for problem in merge_problems:
        print(f"tts_ab: слияние строк: {problem}", file=sys.stderr)
    header = _build_header(
        {"engines": engines, "voices": genders, "repeats": repeats,
         "argv": list(sys.argv[1:])},
        kokoro_status or "kokoro не выбран в -Engines")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    write_results_with_header(results_path, header, merged)
    print(f"tts_ab: results.json записан: {len(merged)} строк "
          f"(шапка — строка 1)")
    if not any(row.get("ok") for row in merged):
        print("tts_ab: ВНИМАНИЕ: ни одной ok-строки — см. статусы движков "
              "выше", file=sys.stderr)

    # Слепые файлы и отчёт — той же командой (критерий §5 «одной командой»);
    # их проблемы не валят сам прогон.
    try:
        blind_summary = run_blind(results_path, BLIND_DIR,
                                  seed=DEFAULT_SEED,
                                  subset_size=args.subset)
        print(f"tts_ab: blind: {blind_summary['files']} файлов, "
              f"{blind_summary['groups']} групп (seed="
              f"{blind_summary['seed']})")
    except Exception as exc:
        print(f"tts_ab: blind не выполнен: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    try:
        report_summary = run_report(results_path, REPORT_PATH)
        print(f"tts_ab: report.md записан: движков ok "
              f"{report_summary['engines_ok']}/{report_summary['engines']}")
    except Exception as exc:
        print(f"tts_ab: report не выполнен: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    return 0


def cmd_blind(args) -> int:
    try:
        summary = run_blind(Path(args.results), BLIND_DIR,
                            seed=args.seed, subset_size=args.subset)
    except ValueError as exc:  # нет results.json / нет строк — внятно
        print(f"tts_ab: {exc}", file=sys.stderr)
        return 1
    print(f"tts_ab: blind: {summary['files']} файлов, "
          f"{summary['groups']} групп, seed={summary['seed']} → "
          f"{BLIND_DIR}")
    return 0


def cmd_report(args) -> int:
    try:
        summary = run_report(Path(args.results), REPORT_PATH)
    except ValueError as exc:  # нет results.json / нет строк — внятно
        print(f"tts_ab: {exc}", file=sys.stderr)
        return 1
    print(f"tts_ab: report.md записан: движков ok "
          f"{summary['engines_ok']}/{summary['engines']}, строк "
          f"{summary['rows']} → {REPORT_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tts_ab",
        description="TTS A/B харнесс «Гемма» (исследовательский CLI; "
                    "прод-конвейер не трогает)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="прогон движков (последовательно)")
    p_run.add_argument("--engines", default="all",
                       help="all | kokoro,piper,silero,supertonic")
    p_run.add_argument("--voices", default="m,f", help="гендеры: m,f")
    p_run.add_argument("--repeats", type=int, default=1,
                       help="N тёплых прогонов каждой фразы (медиана)")
    p_run.add_argument("--subset", type=int, default=5,
                       help="фраз рекомендуемого подсета формы (blind)")
    p_run.add_argument("--results", default=str(RESULTS_PATH))
    p_run.set_defaults(func=cmd_run)

    p_blind = sub.add_parser("blind", help="слепые файлы (без синтеза)")
    p_blind.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p_blind.add_argument("--subset", type=int, default=5)
    p_blind.add_argument("--results", default=str(RESULTS_PATH))
    p_blind.set_defaults(func=cmd_blind)

    p_report = sub.add_parser("report", help="report.md (без синтеза)")
    p_report.add_argument("--results", default=str(RESULTS_PATH))
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
