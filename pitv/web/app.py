"""FastAPI application factory for the PiTV web interface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db as dbm
from .. import sdnotify
from ..config import Config
from ..logsetup import setup_logging
from .api import admin, content, public, wanted
from .events import EventBus
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


def _player_subscriber(app: FastAPI, stop: threading.Event) -> None:
    """Keep a subscription to the player's state stream and relay it to the event bus."""
    cfg: Config = app.state.cfg
    while not stop.is_set():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(str(cfg.player_socket))
                s.sendall(b'{"cmd": "subscribe"}\n')
                s.settimeout(30)
                buf = b""
                while not stop.is_set():
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
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
        except (OSError, socket.timeout) as exc:
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

    app = FastAPI(title="PiTV", version="0.1", docs_url="/api/docs", openapi_url="/api/openapi.json",
                  redoc_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.bus = bus
    app.state.jobs = JobRunner(bus)
    app.state.player = PlayerClient(cfg.player_socket)
    app.state.player_state = {"online": False}
    app.state.started = time.time()

    app.include_router(public.router)
    app.include_router(admin.router)
    app.include_router(wanted.router)
    app.include_router(content.router)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.error("unhandled error in %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": f"{exc!r}"})

    index = STATIC_DIR / "index.html"
    if index.exists():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        # index.html must never be cached: it names hashed asset files that change on every
        # build, and a stale copy renders a blank page. The hashed assets themselves are immutable.
        no_cache = {"Cache-Control": "no-cache, no-store, must-revalidate"}

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = STATIC_DIR / path
            if path and candidate.is_file():
                if path.startswith("assets/"):
                    return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
                return FileResponse(candidate, headers=no_cache)
            return FileResponse(index, headers=no_cache)
    else:
        @app.get("/", include_in_schema=False)
        async def placeholder():
            return HTMLResponse(PLACEHOLDER)

    return app
