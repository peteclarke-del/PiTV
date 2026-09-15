"""The local cache on the Pi's attached drive, filled by pitv_content.

PiTV only reads from it (cache copy > transcoded copy > NAS original) and evicts
least-recently-used files under the size cap. Files are named ``<media_id>_<name>`` so the
index is just a directory listing.

Shared-drive rules (docs/PLAN.md §7): pitv_content writes ``.part`` files and renames them
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


def _settled(p: Path) -> bool:
    """A finished file: not a directory and not something pitv_content is still writing."""
    return p.is_file() and not p.name.endswith(".part")


def _stat(p: Path, attr: str) -> float:
    """st_size / st_mtime / st_mtime, or 0 when the file vanished under us (pitv_content
    renaming a finished .part)."""
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

    def cached_path(self, media_id: int, source: str) -> Path | None:
        """`<media_id>_<original name>` (a plain copy) or `<media_id>_<stem>.mp4` (a transcode
        made by pitv_content); any settled `<media_id>_*` file counts."""
        if not self.enabled or not self.dir:
            return None
        p = self.dir / f"{media_id}_{Path(source).name}"
        if p.is_file():
            return p
        return next((c for c in self.dir.glob(f"{media_id}_*") if _settled(c)), None)

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
        files = [p for p in self.dir.iterdir() if _settled(p)]
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            free = None
        return {"enabled": True, "dir": str(self.dir), "files": len(files), "used": sum(_size(p) for p in files),
                "max": self.max_bytes, "free": free, "tool_running": self.content_tool_running()}

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
        files = [p for p in self.dir.iterdir() if p.is_file()]
        used = sum(_size(p) for p in files)
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            free = needed
        now = time.time()
        files.sort(key=lambda p: _stat(p, "st_mtime"))
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
            log.info("evicted %s from cache", p.name)
        try:
            return shutil.disk_usage(self.dir).free
        except OSError:
            return 0
