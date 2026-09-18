"""FastAPI application factory for the PiTV web interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import db as dbm
from .. import sdnotify
from ..config import Config
from ..logsetup import setup_logging
from ..stream import Streams
from .api import admin, content, public, stream, wanted
from .auth import content_token
from .events import EventBus
from .keeper import Keeper
from .player_client import PlayerClient
from .tasks import JobRunner

log = logging.getLogger("pitv.web")
WEB_MEMORY_LIMIT_MB = 400   # exit for a clean restart above this (systemd caps it harder)
STATIC_DIR = Path(__file__).resolve().parent / "static"

PLACEHOLDER = """<!doctype html><meta charset=utf-8><title>PiTV</title>
<style>body{font-family:system-ui;margin:3rem;color:#222}code{background:#eee;padding:.1em .3em}</style>
<h1>PiTV</h1><p>The web interface has not been built yet. On a development machine run:</p>
<pre><code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code></pre>
<p>The JSON API is available under <a href="/api/docs">/api/docs</a>.</p>"""

MAX_LINE = 1 << 20          # a player state line is a few KB; anything bigger is a broken peer
MAX_BODY = 1 << 20          # every JSON request the UI sends is a few KB
MAX_DOCUMENT_BODY = 32 << 20
# Whole documents: a library index, a line-up export, a pitv_content delivery report. These
# endpoints read their body only after the admin check (deps.admin_json), so the larger cap
# is not something an anonymous caller can make the service parse.
DOCUMENT_PATHS = frozenset({"/api/catalogue/import", "/api/lineup/import", "/api/content/report"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"same-origin")]


def _cross_site(headers: dict[str, str]) -> bool:
    """True for a browser request sent on behalf of another site.

    The admin is open until a password is set, and SameSite=Lax does not cover a page on
    another port of the same host, so a cookie alone cannot tell a request the admin made from
    one a hostile page made in the admin's browser. Browsers say which it is: Sec-Fetch-Site
    where supported, otherwise Origin against Host. Scripts and pitv_content send neither."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return False
    # "null" (a sandboxed frame, a file: page) has no host and never matches.
    netloc = urlsplit(origin).netloc.lower()
    ours = {h.lower() for h in (headers.get("host"), headers.get("x-forwarded-host")) if h}
    return not netloc or netloc not in ours


async def _refuse(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            *SECURITY_HEADERS]})
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    """Refuses cross-site state changes and oversized bodies before any route sees them, and
    adds the standard hardening headers to every response.

    Pure ASGI rather than BaseHTTPMiddleware so the SSE stream is passed through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if scope["method"] in UNSAFE_METHODS and _cross_site(headers):
            await _refuse(send, 403, "cross-site request refused")
            return
        limit = MAX_DOCUMENT_BODY if scope["path"] in DOCUMENT_PATHS else MAX_BODY
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await _refuse(send, 413, "request body too large")
            return
        received = 0

        async def capped_receive() -> Message:
            # A chunked body declares no length, so count what actually arrives.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "request body too large")
            return message

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, capped_receive, send_with_headers)


def _keepalive_wanted(cfg: Config) -> bool:
    """The keep-alive setting, read fresh on its own connection: settings change at runtime and
    SQLite connections stay on the thread that made them."""
    conn = dbm.connect(cfg.db_path)
    try:
        return bool(dbm.get_setting(conn, "player_keepalive", True))
    finally:
        conn.close()


def _player_subscriber(app: FastAPI, stop: threading.Event) -> None:
    """Keep a subscription to the player's state stream and relay it to the event bus.

    The player heartbeats every few seconds; a quiet socket therefore means a wedged player,
    not an idle one, and the connection is dropped so the UI shows it offline."""
    cfg: Config = app.state.cfg
    while not stop.is_set():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(str(cfg.player_socket))
                s.sendall(b'{"cmd": "subscribe"}\n')
                s.settimeout(60)
                buf = b""
                while not stop.is_set():
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > MAX_LINE:
                        log.warning("player state line too long; reconnecting")
                        break
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            state = json.loads(line)
                        except ValueError:
                            continue
                        app.state.player_state = state
                        app.state.bus.publish_threadsafe("player", state)
        except OSError as exc:
            log.debug("player socket: %s", exc)  # normal while the player is down; retried below
        if app.state.player_state.get("online", False):
            app.state.player_state = {"online": False}
            app.state.bus.publish_threadsafe("player", app.state.player_state)
        stop.wait(3)


def create_app(cfg: Config) -> FastAPI:
    cfg.ensure_dirs()
    setup_logging(cfg, "web")
    conn = dbm.connect(cfg.db_path)
    dbm.init_db(conn)
    conn.close()

    bus = EventBus()
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.attach(asyncio.get_running_loop())
        t = threading.Thread(target=_player_subscriber, args=(app, stop), name="pitv-player-sub", daemon=True)
        t.start()
        Keeper(cfg, lambda: app.state.player_state, lambda: _keepalive_wanted(cfg), stop).start()
        sdnotify.ready()

        async def heartbeat() -> None:
            interval = sdnotify.watchdog_interval() or 15
            while True:
                sdnotify.watchdog()
                rss = sdnotify.rss_mb()
                if rss and rss > WEB_MEMORY_LIMIT_MB:
                    log.error("web process at %.0f MB; exiting for a clean restart", rss)
                    os._exit(3)
                await asyncio.sleep(interval)

        hb = asyncio.create_task(heartbeat())
        yield
        hb.cancel()
        stop.set()
        app.state.streams.shutdown()

    app = FastAPI(title="PiTV", version="0.1", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  redoc_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.bus = bus
    app.state.jobs = JobRunner(bus)
    app.state.player = PlayerClient(cfg.player_socket)
    app.state.player_state = {"online": False}
    app.state.started = time.time()
    app.state.content_token = content_token(cfg.data_dir)
    app.state.streams = Streams(cfg)

    app.add_middleware(RequestGuard)
    app.include_router(public.router)
    app.include_router(stream.router)
    app.include_router(admin.router)
    app.include_router(wanted.router)
    app.include_router(content.exchange)
    app.include_router(content.router)

    @app.exception_handler(sqlite3.IntegrityError)
    async def _conflict(request: Request, exc: sqlite3.IntegrityError):
        # An id in the body that names nothing (foreign keys are enforced) or a duplicate;
        # the caller's mistake, not a fault, and the constraint text stays in the log.
        log.info("integrity error in %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=409, content={"detail": "the request names a record that does not exist, or duplicates one"})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # The traceback goes to the log only: exception text can carry file paths and SQL.
        log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "internal error (see the web log)"})

    index = STATIC_DIR / "index.html"
    if index.exists():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        # index.html must never be cached: it names hashed asset files that change on every
        # build, and a stale copy renders a blank page. The hashed assets themselves are immutable.
        no_cache = {"Cache-Control": "no-cache, no-store, must-revalidate"}

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):   # sync: resolve() and is_file() touch the disk, so off the event loop
            # Only files inside the built bundle are served; anything that resolves elsewhere
            # (".." segments, symlinks) falls through to the app shell.
            candidate = (STATIC_DIR / path).resolve()
            if path and candidate.is_relative_to(STATIC_DIR) and candidate.is_file():
                if path.startswith("assets/"):
                    return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
                return FileResponse(candidate, headers=no_cache)
            return FileResponse(index, headers=no_cache)
    else:
        @app.get("/", include_in_schema=False)
        async def placeholder():
            return HTMLResponse(PLACEHOLDER)

    return app
