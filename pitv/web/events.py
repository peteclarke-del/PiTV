"""In-process event bus feeding the Server-Sent Events stream.

Background threads (catalogue import, scheduler, player subscription) publish with `publish_threadsafe`;
the SSE endpoint subscribes with an asyncio queue per client.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

QUEUE_SIZE = 200        # events held for one client; a client that stops reading loses the excess
MAX_SUBSCRIBERS = 32    # open streams; a household has a handful of tabs, so more is a runaway client


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.last: dict[str, Any] = {}  # latest event per type, for late joiners

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def is_full(self) -> bool:
        with self._lock:
            return len(self._subs) >= MAX_SUBSCRIBERS

    def subscribe(self) -> asyncio.Queue | None:
        """A queue for one client, or None when MAX_SUBSCRIBERS streams are already open."""
        with self._lock:
            if len(self._subs) >= MAX_SUBSCRIBERS:
                return None
            q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, data: Any) -> None:
        msg = {"event": event, "data": data, "ts": time.time()}
        self.last[event] = msg
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # a client that stopped reading; it loses events rather than blocking publishers

    def publish_threadsafe(self, event: str, data: Any) -> None:
        if self._loop is None or self._loop.is_closed():
            self.publish(event, data)
            return
        self._loop.call_soon_threadsafe(self.publish, event, data)


def format_sse(msg: dict[str, Any]) -> str:
    return f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
