"""FastAPI application factory for the PiTV web interface."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import db as dbm
from ..config import Config
from .api import admin, public
from .events import EventBus
from .player_client import PlayerClient
from .tasks import JobRunner

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
        except (OSError, socket.timeout):
            pass
        if app.state.player_state.get("online", False):
            app.state.player_state = {"online": False}
            app.state.bus.publish_threadsafe("player", app.state.player_state)
        stop.wait(3)


def create_app(cfg: Config) -> FastAPI:
    cfg.ensure_dirs()
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
        yield
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

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": f"{exc!r}"})

    index = STATIC_DIR / "index.html"
    if index.exists():
        assets = STATIC_DIR / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = STATIC_DIR / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)
    else:
        @app.get("/", include_in_schema=False)
        async def placeholder():
            return HTMLResponse(PLACEHOLDER)

    return app
