# -*- coding: utf-8 -*-
"""Туннель-сервер «Гемма»: доступ к API ассистента из интернета.

Одна зашифрованная WSS-сессия (wss://<ddns>:8443/tunnel) вместо системного
VPN: телефон мультиплексирует HTTP-запросы к 8090 и голосовой поток
/ws_voice внутри одного канала, а этот процесс проксирует их на
127.0.0.1:8090. На телефоне никакого VpnService -- весь прочий трафик
телефона идёт мимо, никаких конфликтов с чужими VPN.

Протокол канала (JSON-рамки; тело -- base64):

  клиент -> сервер:
    {"t":"open","token":"..."}                 -- аутентификация (1-й кадр)
    {"t":"req","id":N,"m":"POST","p":"/voice",
     "h":{...},"b":"<base64>"}                 -- HTTP-запрос к 8090
    {"t":"ws_open","id":N,"p":"/ws_voice"}     -- сабстрим к WS на 8090
    {"t":"ws_data","id":N,"b":"<base64>"}      -- бинарный чанк в сабстрим
    {"t":"ws_text","id":N,"x":"{\\"event\\":\\"end\\"}"} -- текст в сабстрим
    {"t":"ws_close","id":N}                    -- закрыть сабстрим
    {"t":"ping"}                               -- keepalive (JSON)

  сервер -> клиент:
    {"t":"ok"} / {"t":"err","m":"..."}         -- итог аутентификации
    {"t":"res","id":N,"s":200,"ct":"...","b":"<base64>"}  -- HTTP-ответ
    {"t":"ws_data","id":N,"b":"<base64>"}      -- бинарный чанк из сабстрима
    {"t":"ws_text","id":N,"x":"..."}           -- текстовое событие сабстрима
    {"t":"ws_closed","id":N,"c":1000,"r":""}   -- сабстрим закрыт
    {"t":"pong"}

Защита: TLS (самоподписанный сертификат, пинится на телефоне) + токен
ASSISTANT_API_TOKEN при открытии канала. Пустой токен -- соединения
принимаются с ошибкой (туннель наружу не должен работать без секрета).

Запуск (start-all.ps1):  .venv\\Scripts\\python tunnel.py
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import secrets
import socket
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from config import settings

BASE_DIR = Path(__file__).resolve().parent
INTERNAL = f"http://127.0.0.1:{settings.port}"   # куда проксируем
MAX_BODY = 10 * 1024 * 1024     # лимит тела запроса (WAV до ~10 МБ, план)

# Логи в консоль и в файл: стартуют из start-all.ps1 с скрытым окном.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "tunnel.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("tunnel")

app = FastAPI(title="Gemma tunnel", version="0.1.0")


def _load_gen_cert() -> Any:
    """Загружает scripts/gen-cert.py (имя с дефисом -- через importlib)."""
    spec = importlib.util.spec_from_file_location(
        "gemma_gen_cert", BASE_DIR / "scripts" / "gen-cert.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.get("/health")
async def health() -> dict[str, str]:
    """Самопроверка туннеля (curl -k https://<host>:8443/health)."""
    return {"status": "ok", "service": "tunnel"}


# --------------------------------------------------------------------------
# Канал телефона
# --------------------------------------------------------------------------
@app.websocket("/tunnel")
async def tunnel(ws: WebSocket) -> None:
    peer = ws.client.host if ws.client else "?"
    await ws.accept()
    started = time.monotonic()
    send_lock = asyncio.Lock()             # кадры в один сокет -- сериализуем
    streams: dict[int, Any] = {}           # sid -> внутренний WS (8090)
    pumps: dict[int, asyncio.Task] = {}    # sid -> задача чтения 8090 -> канал

    async def send(obj: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(obj, ensure_ascii=False))

    # ---------------- аутентификация (первый кадр или ?token=) -----------
    token = settings.api_token
    authed = bool(token) and ws.query_params.get("token") == token
    if authed:
        pass
    elif not token:
        await send({"t": "err",
                    "m": "ASSISTANT_API_TOKEN is empty -- tunnel disabled"})
        await ws.close(code=1008)
        log.warning("channel from %s REJECTED: token not configured", peer)
        return
    deadline = time.monotonic() + 15
    while not authed and time.monotonic() < deadline:
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=deadline
                                           - time.monotonic())
        except (asyncio.TimeoutError, Exception):  # noqa: B014 -- обрыв клиента
            return
        if first.get("type") == "websocket.disconnect":
            return
        text = first.get("text")
        if not text:
            continue
        try:
            fr = json.loads(text)
        except (ValueError, TypeError):
            continue
        if fr.get("t") == "open":
            got = str(fr.get("token") or "")
            if secrets.compare_digest(got, token):
                authed = True
            else:
                await send({"t": "err", "m": "bad token"})
                await ws.close(code=1008)
                log.warning("channel from %s REJECTED: bad token", peer)
                return
    if not authed:
        await ws.close(code=1008)
        log.warning("channel from %s REJECTED: no open frame", peer)
        return
    await send({"t": "ok"})
    log.info("channel OPEN %s (certs pinned, token ok)", peer)

    # ---------------- прокси HTTP на 8090 --------------------------------
    client = httpx.AsyncClient(base_url=INTERNAL,
                               timeout=httpx.Timeout(600.0, connect=5.0))

    async def handle_req(fr: dict[str, Any]) -> None:
        sid = int(fr.get("id") or 0)
        try:
            method = str(fr.get("m") or "GET").upper()
            path = str(fr.get("p") or "/")
            body = base64.b64decode(fr["b"]) if fr.get("b") else None
            if body is not None and len(body) > MAX_BODY:
                raise ValueError(f"body too large: {len(body)}")
            headers = {str(k): str(v) for k, v in (fr.get("h") or {}).items()}
            r = await client.request(method, path, headers=headers, content=body)
            await send({
                "t": "res", "id": sid, "s": r.status_code,
                "ct": r.headers.get("content-type", ""),
                "b": base64.b64encode(r.content).decode("ascii"),
            })
        except Exception as exc:  # noqa: BLE001 -- ошибка прокси -> 502 клиенту
            log.warning("proxy %s %s failed: %s", fr.get("m"), fr.get("p"), exc)
            msg = str(exc).encode("utf-8", "replace")[:300]
            await send({"t": "res", "id": sid, "s": 502, "ct": "text/plain",
                        "b": base64.b64encode(msg).decode("ascii")})

    # ---------------- сабстримы (голосовой /ws_voice) --------------------
    async def pump(sid: int, sock: Any) -> None:
        """Читает внутренний WS 8090 и кладёт кадры в канал телефона."""
        try:
            async for msg in sock:
                if isinstance(msg, bytes):
                    await send({"t": "ws_data", "id": sid,
                                "b": base64.b64encode(msg).decode("ascii")})
                else:
                    await send({"t": "ws_text", "id": sid, "x": msg})
        except Exception as exc:  # noqa: BLE001 -- конец сабстрима
            log.info("sub-stream %d ended: %s", sid, exc)
        finally:
            streams.pop(sid, None)
            try:
                await sock.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await send({"t": "ws_closed", "id": sid, "c": 1000,
                            "r": "closed"})
            except Exception:  # noqa: BLE001 -- канал уже умер
                pass

    try:
        from websockets.asyncio.client import connect as ws_connect
    except ImportError:  # uvicorn[standard] тянет websockets -- страховка
        ws_connect = None  # type: ignore[assignment]

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if not text:
                continue                      # бинарные кадры в канал не шлём
            try:
                fr = json.loads(text)
            except (ValueError, TypeError):
                log.warning("bad frame from %s: %.80s", peer, text)
                continue
            t = fr.get("t")
            if t == "ping":
                await send({"t": "pong"})
            elif t == "req":
                asyncio.ensure_future(handle_req(fr))
            elif t == "ws_open":
                sid, path = int(fr.get("id") or 0), str(fr.get("p") or "")
                if sid in streams or ws_connect is None:
                    continue
                if not path.startswith("/ws"):
                    await send({"t": "ws_closed", "id": sid, "c": 1008,
                                "r": "path not allowed"})
                    continue
                try:
                    sock = await ws_connect(
                        f"ws://127.0.0.1:{settings.port}{path}",
                        max_size=MAX_BODY)
                except Exception as exc:  # noqa: BLE001
                    log.warning("sub-stream open %s failed: %s", path, exc)
                    await send({"t": "ws_closed", "id": sid, "c": 1011,
                                "r": f"internal ws failed: {exc}"[:120]})
                    continue
                streams[sid] = sock
                pumps[sid] = asyncio.ensure_future(pump(sid, sock))
            elif t == "ws_data":
                sock = streams.get(int(fr.get("id") or 0))
                if sock is not None:
                    data = base64.b64decode(fr.get("b") or b"")
                    if len(data) > MAX_BODY:
                        continue
                    await sock.send(data)
            elif t == "ws_text":
                sock = streams.get(int(fr.get("id") or 0))
                if sock is not None:
                    await sock.send(str(fr.get("x") or ""))
            elif t == "ws_close":
                sid = int(fr.get("id") or 0)
                task = pumps.pop(sid, None)
                if task is not None:
                    task.cancel()
                sock = streams.pop(sid, None)
                if sock is not None:
                    try:
                        await sock.close()
                    except Exception:  # noqa: BLE001
                        pass
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 -- обрыв/ошибка канала
        log.exception("channel from %s crashed", peer)
    finally:
        for task in pumps.values():
            task.cancel()
        for sock in streams.values():
            try:
                await sock.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
        log.info("channel CLOSED %s (lifetime %.0f s)",
                 peer, time.monotonic() - started)


def _listen_socket(host: str, port: int) -> socket.socket:
    """Слушающий сокет туннеля: dual-stack IPv6+IPv4 на Windows.

    uvicorn при host='0.0.0.0' слушает только IPv4, а при '::' -- только
    IPv6 (Windows по умолчанию ставит IPV6_V6ONLY=1). Основной транспорт
    плана -- входящий IPv6, запасной -- IPv4-проброс, поэтому нужен один
    сокет, принимающий оба семейства: '::' c явным V6ONLY=0 (проверено:
    принимает и ::1, и 127.0.0.1). Сокет передаётся в uvicorn через
    serve(sockets=[...]), минуя его собственный бинд.
    """
    if host in ("", "0.0.0.0", "::"):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::", port))
    else:
        # Явный адрес из .env -- ведём себя как раньше (один интерфейс)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    sock.listen(128)
    return sock


def main() -> None:
    cert = Path(settings.tunnel_cert)
    key = Path(settings.tunnel_key)
    names = [settings.ddns_hostname, "localhost"]
    try:
        mod = _load_gen_cert()
        created = mod.ensure_cert(cert, key, names=names)
        if created:
            log.info("сертификат создан: %s (SAN: %s)",
                     cert, ", ".join(n for n in names if n))
    except ImportError:
        log.error("нет пакета cryptography: .venv\\Scripts\\pip install "
                  "cryptography -- сертификат не создан")
        raise SystemExit(1)
    if not (cert.exists() and key.exists()):
        log.error("сертификат/ключ отсутствуют (%s) -- стоп", cert)
        raise SystemExit(1)

    hexs, pin = mod.cert_fingerprint(cert)
    log.info("cert SHA-256: %s", hexs)
    log.info("cert pin: %s", pin)
    if not settings.api_token:
        log.warning("ASSISTANT_API_TOKEN пуст: туннель отклоняет все "
                    "соединения (задай токен в .env)")
    if not settings.ddns_hostname:
        log.warning("ASSISTANT_DDNS_HOSTNAME пуст: SAN без DDNS-имени, "
                    "телефон не сможет подключиться по домену")
    log.info("tunnel: wss://%s:%d/tunnel -> %s",
             settings.tunnel_host, settings.tunnel_port, INTERNAL)

    # Сокет создаём сами и передаём uvicorn: так слушаем И IPv6, и IPv4
    # (см. _listen_socket -- обычный бинд '0.0.0.0' закрыл бы IPv6-транспорт)
    sock = _listen_socket(settings.tunnel_host, settings.tunnel_port)
    log.info("слушаю IPv6+IPv4 на %d (dual-stack)", settings.tunnel_port)

    config = uvicorn.Config(
        app,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        ws_max_size=MAX_BODY,      # WAV/чанки до ~10 МБ
        log_level="info",
        access_log=False,
    )
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
