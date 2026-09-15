"""The local cache on the Pi's attached drive, filled by pitv_content.

PiTV only reads from it (cache copy > transcoded copy > NAS original) and evicts
least-recently-used files under the size cap. Files are named ``<media_id>_<name>`` so the
index is just a directory listing.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("pitv.cache")


class MediaCache:
    def __init__(self, cache_dir: Path | None, max_bytes: int) -> None:
        self.dir = cache_dir
        self.max_bytes = max_bytes
        self.enabled = bool(cache_dir)
        self._lock = threading.Lock()
        if self.dir:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("cache dir unusable (%s): %s", self.dir, exc)
                self.enabled = False

    def cached_path(self, media_id: int, source: str) -> Path | None:
        """`<media_id>_<original name>` (a plain copy) or `<media_id>_<stem>.mp4` (a transcode
        made by pitv_content); any `<media_id>_*` file counts."""
        if not self.enabled or not self.dir:
            return None
        p = self.dir / f"{media_id}_{Path(source).name}"
        if p.is_file():
            return p
        for cand in self.dir.glob(f"{media_id}_*"):
            if cand.is_file() and not cand.name.endswith(".part"):
                return cand
        return None

    def resolve(self, media: dict[str, Any] | None) -> str | None:
        """Best available path: cached copy > transcoded copy > original."""
        if not media:
            return None
        original = media.get("path")
        for cand in (self.cached_path(media["id"], original) if original else None,
                     media.get("transcoded_path"), original):
            if cand and Path(cand).is_file():
                return str(cand)
        return None

    def usage(self) -> dict[str, Any]:
        if not self.enabled or not self.dir:
            return {"enabled": False}
        files = [p for p in self.dir.iterdir() if p.is_file() and not p.name.endswith(".part")]
        used = sum(p.stat().st_size for p in files)
        try:
            du = shutil.disk_usage(self.dir)
            free = du.free
        except OSError:
            free = None
        return {"enabled": True, "dir": str(self.dir), "files": len(files), "used": used,
                "max": self.max_bytes, "free": free, "tool_running": self.content_tool_running()}

    def content_tool_running(self) -> bool:
        """pitv_content touches <cache>/.pitv_content.running while it works the manifest."""
        if not self.dir:
            return False
        marker = self.dir / ".pitv_content.running"
        try:
            return marker.exists() and time.time() - marker.stat().st_mtime < 6 * 3600
        except OSError:
            return False

    def evict(self, needed: int = 0) -> None:
        """Periodic LRU eviction under the cap (protected names are never removed)."""
        if self.enabled:
            self._make_room(needed)

    def make_room(self, needed: int) -> int:
        """Evict for an external writer; returns free bytes afterwards."""
        self._make_room(needed)
        try:
            return shutil.disk_usage(self.dir).free if self.dir else 0
        except OSError:
            return 0

    def _make_room(self, needed: int) -> None:
        if not self.dir:
            return
        files = [p for p in self.dir.iterdir() if p.is_file()]
        used = sum(p.stat().st_size for p in files)
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            free = needed
        # Evict least recently used until under the cap and the disk has room. Never touch
        # files pitv_content is still writing (.part), anything under two hours old, or files
        # in the current manifest (protected names).
        now = time.time()
        files.sort(key=lambda p: p.stat().st_atime)
        for p in files:
            if used + needed <= self.max_bytes and free > needed + 512 * 1024 * 1024:
                break
            if self._protected(p) or ".part" in p.name or now - p.stat().st_mtime < 2 * 3600:
                continue
            sz = p.stat().st_size
            try:
                p.unlink()
                used -= sz
                free += sz
                log.info("evicted %s from cache", p.name)
            except OSError:
                pass

    _protected_names: set[str] = set()

    def protect(self, names: set[str]) -> None:
        self._protected_names = names

    def _protected(self, p: Path) -> bool:
        return p.name in self._protected_names
