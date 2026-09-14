"""Where missing programmes can come from.

Only sources the owner is entitled to use are supported: the Internet Archive's public
collections (public-domain and freely licensed material, of which there is a great deal of
1980s television advertising and some programmes) and explicit URLs the owner supplies
(direct media links, or pages handled by yt-dlp when it is installed). There is no torrent
or usenet support and none is planned.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mpg", ".mpeg", ".ogv", ".mov", ".m4v", ".webm")
_SXXEYY = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
Progress = Callable[[str, float], None]


@dataclass
class Candidate:
    provider: str
    ref: str            # archive: identifier/filename ; url: the URL
    title: str
    year: int | None
    size: int | None
    detail: str = ""


def _get_json(url: str, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "PiTV/0.1 (+https://github.com/peteclarke-del/PiTV)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


# --- Internet Archive ----------------------------------------------------------------------

def archive_search(query: str, year: int | None = None, rows: int = 15) -> list[Candidate]:
    q = f'({query}) AND mediatype:(movies)'
    if year:
        q += f" AND year:({year - 1} OR {year} OR {year + 1})"
    params = urllib.parse.urlencode({"q": q, "fl[]": ["identifier", "title", "year", "item_size", "description"],
                                     "rows": rows, "output": "json", "sort[]": "downloads desc"}, doseq=True)
    data = _get_json(f"https://archive.org/advancedsearch.php?{params}")
    out = []
    for doc in data.get("response", {}).get("docs", []):
        y = None
        try:
            y = int(str(doc.get("year", ""))[:4])
        except ValueError:
            y = None
        desc = doc.get("description")
        if isinstance(desc, list):
            desc = " ".join(desc)
        out.append(Candidate("archive", doc["identifier"], doc.get("title") or doc["identifier"], y,
                             doc.get("item_size"), (desc or "")[:200]))
    return out


def archive_files(identifier: str) -> list[dict[str, Any]]:
    meta = _get_json(f"https://archive.org/metadata/{identifier}")
    files = []
    for f in meta.get("files", []):
        name = f.get("name", "")
        if name.lower().endswith(VIDEO_EXT):
            try:
                size = int(f.get("size") or 0)
            except ValueError:
                size = 0
            files.append({"name": name, "size": size, "format": f.get("format"),
                          "url": f"https://archive.org/download/{identifier}/{urllib.parse.quote(name)}"})
    files.sort(key=lambda f: (-(1 if f["name"].lower().endswith(".mp4") else 0), -f["size"]))
    return files


def archive_pick_file(identifier: str, season: int | None = None, episode: int | None = None) -> dict[str, Any] | None:
    files = archive_files(identifier)
    if not files:
        return None
    if season is not None and episode is not None:
        for f in files:
            m = _SXXEYY.search(f["name"])
            if m and int(m.group(1)) == season and int(m.group(2)) == episode:
                return f
        return None
    return files[0]


# --- downloading -----------------------------------------------------------------------------

def download_url(url: str, dest: Path, progress: Progress, expected_size: int | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "PiTV/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
        total = int(r.headers.get("Content-Length") or expected_size or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                progress(f"downloading {done // (1024*1024)} / {total // (1024*1024)} MB", done / total)
    tmp.rename(dest)
    return dest


def download_with_ytdlp(url: str, dest_stem: Path, progress: Progress) -> Path:
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        raise RuntimeError("yt-dlp is not installed (apt install yt-dlp) and the URL is not a direct media link")
    dest_stem.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ytdlp, "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4", "--no-playlist",
           "--newline", "-o", f"{dest_stem}.%(ext)s", url]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout
    for line in proc.stdout:
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            progress(line.strip()[:80], float(m.group(1)) / 100)
    if proc.wait() != 0:
        raise RuntimeError("yt-dlp failed")
    for cand in dest_stem.parent.glob(dest_stem.name + ".*"):
        if cand.suffix.lower() in VIDEO_EXT:
            return cand
    raise RuntimeError("yt-dlp produced no video file")


def is_direct_media(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(VIDEO_EXT)
