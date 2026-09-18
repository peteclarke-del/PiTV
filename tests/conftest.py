"""Shared set-up: a fake library on disk plus the library index pitv_content would publish for
it, imported into a fresh database. Durations come from the index, so no test needs ffprobe."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from pitv import db as dbm
from pitv.catalogue import import_and_place
from pitv.config import Config
from pitv.devtools import build_fake_library

# Encoded clips are kept between tests and between runs: the suite makes the same few dozen
# lengths every time, and rendering them is nearly all of its running time. The suffix is
# bumped whenever the clip itself changes (see devtools.test_signal), so a stale set is never
# reused. Set PITV_TEST_TEMPLATES to put the folder somewhere else.
TEMPLATES = Path(os.environ.get("PITV_TEST_TEMPLATES") or tempfile.gettempdir()) / "pitv-test-clips-v1"


def make_library(root: Path, max_episodes: int) -> dict[str, Any]:
    """Build the files and index under `root`, import the index, return cfg, conn and lib."""
    lib = build_fake_library(root / "lib", max_episodes_per_show=max_episodes, templates=TEMPLATES)
    cfg = Config(data_dir=root / "data", run_dir=root / "run")
    os.environ.pop("PITV_DB", None)
    cfg.ensure_dirs()
    conn = dbm.connect(cfg.db_path)
    dbm.init_db(conn)
    import_and_place(conn, lib["index"], "fake library")
    return {"cfg": cfg, "conn": conn, "lib": lib}
