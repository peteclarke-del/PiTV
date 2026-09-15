"""The local cache on the Pi's attached drive, filled by pitv_content.

PiTV only reads from it and evicts least-recently-used files under the size cap. `locate`
decides what plays: the cache copy, else the NAS original when `nas_fallback` is on, else
nothing (the player shows the technical difficulties card). Copies are named
``<media_id>_<name>`` so finding one is a directory listing.

Shared-drive rules (docs/CONTENT_CONTRACT.md section 5): pitv_content writes ``.part`` files and renames them
atomically when complete, never deletes, and touches RUNNING_MARKER while it works. PiTV
ignores ``.part`` files, never evicts files under MIN_AGE_SECONDS old or in the current
manifest (see `protect`), and does not evict at all while the marker is fresh.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("pitv.cache")

# Names pitv_content and PiTV agree on, all relative to the cache directory.
RUNNING_MARKER = ".pitv_content.running"
STATUS_FILE = "pitv_content.status.json"
LOG_FILE = "logs/pitv-content.log"
REPORTS_DIR = "reports"

MARKER_FRESH_SECONDS = 6 * 3600   # a marker older than this is a crashed run, not a busy one
MIN_AGE_SECONDS = 2 * 3600        # freshly written files are never evicted
HEADROOM_BYTES = 512 * 1024 * 1024  # free space to leave on the drive beyond what is asked for
LISTING_TTL = 5.0                 # the player asks for usage every second; a USB disk need not be listed that often


def _settled(p: Path) -> bool:
    """A finished, non-empty file that pitv_content is not still writing (`.part`). An empty
    file is a failed write, never something to play."""
    if p.name.endswith(".part") or ".part." in p.name:
        return False
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _stat(p: Path, attr: str) -> float:
    """st_size / st_mtime, or 0 when the file vanished under us (pitv_content renaming a
    finished .part)."""
    try:
        return getattr(p.stat(), attr)
    except OSError:
        return 0


def _size(p: Path) -> int:
    return int(_stat(p, "st_size"))


class MediaCache:
    def __init__(self, cache_dir: Path | None, max_bytes: int) -> None:
        self.dir = cache_dir
        self.max_bytes = max_bytes
        self.enabled = bool(cache_dir)
        self._protected: set[str] = set()
        self._listing: list[Path] = []
        self._listed_at = 0.0
        self._usage: dict[str, Any] | None = None
        if self.dir:
            try:
                # No parents: if the cache drive is not mounted the directory must not be
                # created on the SD card underneath the mount point.
                self.dir.mkdir(exist_ok=True)
            except OSError as exc:
                log.warning("cache dir unusable (%s): %s", self.dir, exc)
                self.enabled = False

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> MediaCache:
        """The cache described by the `cache_dir` / `cache_max_gb` settings (disabled when unset)."""
        cache_dir = settings.get("cache_dir") or ""
        return cls(Path(cache_dir) if cache_dir else None,
                   int(float(settings.get("cache_max_gb", 0)) * 1024 ** 3))

    def _sub(self, name: str) -> Path | None:
        return self.dir / name if self.dir else None

    @property
    def running_marker(self) -> Path | None:
        return self._sub(RUNNING_MARKER)

    @property
    def status_file(self) -> Path | None:
        return self._sub(STATUS_FILE)

    @property
    def log_file(self) -> Path | None:
        return self._sub(LOG_FILE)

    @property
    def reports_dir(self) -> Path | None:
        return self._sub(REPORTS_DIR)

    # --- reading --------------------------------------------------------------------------

    def _files(self, fresh: bool = False) -> list[Path]:
        """Settled files in the cache, listed at most every LISTING_TTL seconds."""
        if not self.dir:
            return []
        now = time.monotonic()
        if fresh or now - self._listed_at > LISTING_TTL:
            try:
                self._listing = [p for p in self.dir.iterdir() if _settled(p)]
            except OSError as exc:
                log.warning("cannot list cache dir %s: %s", self.dir, exc)
                self._listing = []
            self._listed_at = now
            self._usage = None
        return self._listing

    def invalidate(self) -> None:
        self._listed_at = 0.0
        self._usage = None

    def cached_path(self, media_id: int, source: str) -> Path | None:
        """`<media_id>_<original name>` (a plain copy) or `<media_id>_<stem>.mp4` (a transcode
        made by pitv_content); any settled `<media_id>_*` file counts. The exact name is
        checked on disk so a file that has just landed is seen at once."""
        if not self.enabled or not self.dir:
            return None
        p = self.dir / f"{media_id}_{Path(source).name}"
        if _settled(p):
            return p
        prefix = f"{media_id}_"
        return next((c for c in self._files() if c.name.startswith(prefix)), None)

    def cache_copy(self, media: dict[str, Any]) -> Path | None:
        """The playable cache file for a catalogue item: the path pitv_content reported, the
        `<media_id>_*` target it was asked to fill, or (for material that only ever lived in the
        cache) the item's own path."""
        for cand in (media.get("cache_path"),
                     media.get("path") if media.get("origin") in ("cache", "online") else None):
            if cand and _settled(Path(cand)):
                return Path(cand)
        if media.get("id") and media.get("path"):
            return self.cached_path(int(media["id"]), str(media["path"]))
        return None

    def locate(self, media: dict[str, Any] | None, nas_fallback: bool) -> tuple[str | None, str]:
        """Where to play a catalogue item from: (path, "cache") normally; (path, "nas") when the
        cache copy is missing and fallback is on; (None, reason) when nothing is playable."""
        if not media:
            return None, "no programme"
        copy = self.cache_copy(media)
        if copy is not None:
            return str(copy), "cache"
        if media.get("origin", "nas") == "nas" and media.get("path"):
            if not nas_fallback:
                return None, "not in the cache and NAS fallback is off"
            if Path(media["path"]).is_file():
                return str(media["path"]), "nas"
            return None, "not in the cache and not on the NAS"
        return None, "not in the cache (fetched material has no other copy)"

    def usage(self) -> dict[str, Any]:
        if not self.enabled or not self.dir:
            return {"enabled": False}
        files = self._files()
        if self._usage is None:
            try:
                free = shutil.disk_usage(self.dir).free
            except OSError:
                free = None
            self._usage = {"enabled": True, "dir": str(self.dir), "files": len(files),
                           "used": sum(_size(p) for p in files), "max": self.max_bytes, "free": free}
        return {**self._usage, "tool_running": self.content_tool_running()}

    def content_tool_running(self) -> bool:
        """True while pitv_content's marker is fresh (it touches the marker as it works)."""
        marker = self.running_marker
        if marker is None:
            return False
        try:
            return marker.exists() and time.time() - marker.stat().st_mtime < MARKER_FRESH_SECONDS
        except OSError:
            return False

    # --- eviction ---------------------------------------------------------------------------

    def protect(self, names: set[str]) -> None:
        """File names (targets in the current manifest) that eviction must leave alone."""
        self._protected = names

    def make_room(self, needed: int = 0) -> int:
        """Evict least-recently-used files until the cache is under its cap and the drive has
        `needed` bytes (plus headroom) free. Returns the free space afterwards."""
        if not self.enabled or not self.dir:
            return 0
        try:
            files = [p for p in self.dir.iterdir() if p.is_file()]
            free = shutil.disk_usage(self.dir).free
        except OSError as exc:
            log.warning("cannot inspect cache dir %s: %s", self.dir, exc)
            return 0
        used = sum(_size(p) for p in files)
        now = time.time()
        files.sort(key=lambda p: _stat(p, "st_mtime"))
        evicted = 0
        for p in files:
            if used + needed <= self.max_bytes and free > needed + HEADROOM_BYTES:
                break
            if p.name in self._protected or not _settled(p) or now - _stat(p, "st_mtime") < MIN_AGE_SECONDS:
                continue
            sz = _size(p)
            try:
                p.unlink()
            except OSError as exc:
                log.warning("could not evict %s: %s", p.name, exc)
                continue
            used -= sz
            free += sz
            evicted += 1
            log.info("evicted %s from cache", p.name)
        if evicted:
            self.invalidate()
        try:
            return shutil.disk_usage(self.dir).free
        except OSError:
            return 0
