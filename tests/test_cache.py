"""Cache eviction: only PiTV's own `<media_id>_*` copies are ever deleted, oldest use first."""

from __future__ import annotations

import os
import time
from pathlib import Path

from pitv.player.cache import RUNNING_MARKER, STATUS_FILE, MediaCache, touch_used

OLD = time.time() - 5 * 3600   # past the two-hour protection for fresh deliveries


def _file(path: Path, size: int = 1000, when: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (when, when))
    return path


def test_make_room_evicts_only_unprotected_cache_copies(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    evictable = [_file(cache_dir / "1_old.mp4"), _file(cache_dir / "2_older.mkv")]
    kept = [
        _file(cache_dir / "3_manifest.mp4"),                       # in the current manifest
        _file(cache_dir / "4_fresh.mp4", when=time.time()),        # delivered under two hours ago
        _file(cache_dir / "5_writing.mp4.part"),                   # pitv_content still writing
        _file(cache_dir / "6_empty.mp4", size=0),                  # a failed write, not ours to judge
        _file(cache_dir / STATUS_FILE), _file(cache_dir / RUNNING_MARKER),
        _file(cache_dir / "notes.txt"), _file(cache_dir / "movie.mp4"),   # not cache copies at all
        _file(cache_dir / "acquired" / "Show" / "7_episode.mp4"),  # fetched material: lineup's job
        _file(cache_dir / "reports" / "r.json"), _file(cache_dir / "logs" / "pitv-content.log"),
    ]
    (cache_dir / "8_link.mp4").symlink_to(kept[6])
    cache = MediaCache(cache_dir, max_bytes=0)
    cache.protect({"3_manifest.mp4"})
    cache.make_room()
    assert not any(p.exists() for p in evictable)
    assert all(p.exists() for p in kept)
    assert (cache_dir / "8_link.mp4").is_symlink()


def test_make_room_evicts_least_recently_played_first(tmp_path):
    cache_dir = tmp_path / "cache"
    played, older, newer = (_file(cache_dir / f"{n}_film.mp4", when=OLD + n) for n in (1, 2, 3))
    touch_used(str(played))   # the oldest delivery went on air just now, so it goes last
    assert played.stat().st_mtime == OLD + 1   # delivery time is left alone
    cache = MediaCache(cache_dir, max_bytes=1500)
    cache.make_room()
    assert played.exists() and not older.exists() and not newer.exists()


def test_usage_counts_only_settled_copies(tmp_path):
    cache_dir = tmp_path / "cache"
    _file(cache_dir / "1_a.mp4")
    _file(cache_dir / "2_b.mp4.part")
    _file(cache_dir / STATUS_FILE)
    _file(cache_dir / "notes.txt")
    usage = MediaCache(cache_dir, max_bytes=10 ** 9).usage()
    assert usage["files"] == 1 and usage["used"] == 1000 and usage["tool_running"] is False
