"""The local cache on the Pi's attached drive, filled by pitv_content.

PiTV reads from it, marks each copy it puts on air (`touch_used`), and evicts the least
recently used files under the size cap. `locate` decides what plays: the cache copy, else
the NAS original when `nas_fallback` is on, else nothing (the player shows the technical
difficulties card). Copies are named ``<media_id>_<name>`` so finding one is a directory
listing.

Shared-drive rules (docs/CONTENT_CONTRACT.md section 5): pitv_content writes ``.part`` files
and renames them atomically when complete, never deletes, and touches RUNNING_MARKER while
it works. PiTV
ignores ``.part`` files, never evicts files under MIN_AGE_SECONDS old or in the current
manifest (see `protect`), and does not evict at all while the marker is fresh.

One instance is shared by the player's main thread, its maintenance thread and the control
socket threads, so the cached listing is guarded by a lock.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any

from ..db import DEFAULT_SETTINGS

log = logging.getLogger("pitv.cache")

# Names pitv_content and PiTV agree on, all relative to the cache directory.
RUNNING_MARKER = ".pitv_content.running"
STATUS_FILE = "pitv_content.status.json"
LOG_FILE = "logs/pitv-content.log"
REPORTS_DIR = "reports"
# A cache copy is `<media_id>_<name>`. Only such files are counted or evicted, so a cache_dir
# pointed at the wrong folder cannot lose anything else, and pitv_content's own files
# (status, marker) and subfolders (reports, logs, acquired material) are never touched.
_COPY_NAME = re.compile(r"\d+_")

MARKER_FRESH_SECONDS = 6 * 3600   # a marker older than this is a crashed run, not a busy one
MIN_AGE_SECONDS = 2 * 3600        # freshly written files are never evicted
HEADROOM_BYTES = 512 * 1024 * 1024  # free space to leave on the drive beyond what is asked for
LISTING_TTL = 5.0                 # the player asks for usage every second; a USB disk need not be listed that often


def _is_part(name: str) -> bool:
    return name.endswith(".part") or ".part." in name


def _is_copy(name: str) -> bool:
    """A cache copy, finished or still being written."""
    return _COPY_NAME.match(name) is not None


def _settled(p: Path) -> bool:
    """A finished, non-empty file that pitv_content is not still writing (`.part`). An empty
    file is a failed write, never something to play."""
    if _is_part(p.name):
        return False
    try:
        st = p.stat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


def tree_bytes(root: Path) -> int:
    """Bytes held under `root`, symlinks not followed. The cap covers the whole cache drive
    folder, fetched library included, which is how pitv_content measures it too."""
    total = 0
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _size(p: Path) -> int:
    """0 when the file vanished under us (pitv_content renaming a finished .part)."""
    try:
        return p.stat().st_size
    except OSError:
        return 0


class MediaCache:
    def __init__(self, cache_dir: Path | None, max_bytes: int) -> None:
        self.dir = cache_dir
        self.max_bytes = max_bytes
        self.enabled = bool(cache_dir)
        self._protected: frozenset[str] = frozenset()
        self._lock = threading.Lock()
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
        max_gb = settings.get("cache_max_gb", DEFAULT_SETTINGS["cache_max_gb"])
        return cls(Path(cache_dir) if cache_dir else None, int(float(max_gb) * 1024 ** 3))

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

    def _files(self) -> list[Path]:
        """Settled media files in the cache, listed at most every LISTING_TTL seconds."""
        if not self.dir:
            return []
        with self._lock:
            now = time.monotonic()
            if now - self._listed_at > LISTING_TTL or not self._listed_at:
                try:
                    self._listing = [p for p in self.dir.iterdir() if _is_copy(p.name) and _settled(p)]
                except OSError as exc:
                    log.warning("cannot list cache dir %s: %s", self.dir, exc)
                    self._listing = []
                self._listed_at = now
                self._usage = None
            return self._listing

    def invalidate(self) -> None:
        with self._lock:
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
        """Size and state of the cache, recomputed at most every LISTING_TTL seconds."""
        if not self.enabled or not self.dir:
            return {"enabled": False}
        files = self._files()
        with self._lock:
            usage = self._usage
        if usage is None:
            try:
                free = shutil.disk_usage(self.dir).free
            except OSError:
                free = None
            usage = {"enabled": True, "dir": str(self.dir), "files": len(files),
                     "used": sum(_size(p) for p in files), "max": self.max_bytes, "free": free,
                     "tool_running": self.content_tool_running()}
            with self._lock:
                self._usage = usage
        return dict(usage)

    def content_tool_running(self) -> bool:
        """True while pitv_content's marker is fresh (it touches the marker as it works)."""
        marker = self.running_marker
        if marker is None:
            return False
        try:
            return time.time() - marker.stat().st_mtime < MARKER_FRESH_SECONDS
        except OSError:
            return False

    # --- eviction ---------------------------------------------------------------------------

    def protect(self, names: set[str]) -> None:
        """File names (targets in the current manifest) that eviction must leave alone."""
        self._protected = frozenset(names)

    def make_room(self, needed: int = 0) -> int:
        """Evict least-recently-used cache copies until the copies are under the cap and the
        drive has `needed` bytes (plus headroom) free. Returns the free space afterwards. Copies
        still being written count towards the cap but are never evicted.

        A file was last used at the later of its access time (set by `touch_used` when it goes
        on air) and its modification time (when pitv_content delivered it)."""
        if not self.enabled or not self.dir:
            return 0
        entries: list[tuple[float, float, int, Path]] = []   # (last used, written, size, path)
        try:
            free = shutil.disk_usage(self.dir).free
            for p in self.dir.iterdir():
                if not _is_copy(p.name):
                    continue
                try:
                    st = p.lstat()   # a symlink is not a copy: neither counted nor followed
                except OSError:
                    continue   # renamed or removed by pitv_content since the listing
                if stat.S_ISREG(st.st_mode):
                    entries.append((max(st.st_atime, st.st_mtime), st.st_mtime, st.st_size, p))
        except OSError as exc:
            log.warning("cannot inspect cache dir %s: %s", self.dir, exc)
            return 0
        # The cap is on the whole folder, fetched material and index included, which is how
        # pitv_content measures it before it delivers. Counting the copies alone, PiTV judged
        # that a film still fitted, evicted nothing, and pitv_content went on refusing it.
        used = tree_bytes(self.dir)
        now = time.time()
        protected = self._protected
        evicted = 0
        for _, written, size, p in sorted(entries, key=lambda e: e[0]):
            if used + needed <= self.max_bytes and free > needed + HEADROOM_BYTES:
                break
            if p.name in protected or _is_part(p.name) or size == 0 or now - written < MIN_AGE_SECONDS:
                continue
            try:
                p.unlink()
            except OSError as exc:
                log.warning("could not evict %s: %s", p.name, exc)
                continue
            used -= size
            free += size
            evicted += 1
            log.info("evicted %s from cache", p.name)
        if evicted:
            self.invalidate()
        try:
            return shutil.disk_usage(self.dir).free
        except OSError:
            return 0


def touch_used(path: str) -> None:
    """Record that a cache copy went on air, for eviction order. Only the access time moves:
    the modification time stays pitv_content's delivery time. Setting an explicit time needs
    file ownership; where pitv_content owns the file the kernel's relatime update is the
    fallback, so a refusal is not worth more than a debug line."""
    try:
        st = os.stat(path)
        os.utime(path, ns=(time.time_ns(), st.st_mtime_ns))
    except OSError as exc:
        log.debug("could not mark %s as used: %s", path, exc)
