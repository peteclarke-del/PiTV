"""HTTP client for pitv_content's local API (docs/CONTENT_CONTRACT.md).

Used by the web admin (proxy, run now, catalogue options) and by the player's maintenance
thread (library index import), so it lives outside the web package.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .db import DEFAULT_SETTINGS


def base_url(settings: dict[str, Any]) -> str:
    return settings.get("content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]


def request(base: str, method: str, path: str, query: str = "", body: dict[str, Any] | None = None,
            timeout: float = 15) -> tuple[int, Any]:
    """One JSON request. Anything that is not JSON (an unrelated service on that port answering
    with an HTML page) means the tool is not there: 503 with `offline`."""
    url = f"{base.rstrip('/')}/api/{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return 503, {"error": f"pitv_content API unavailable at {base}: {exc}", "offline": True}
    text = raw.decode("utf-8", errors="replace")
    try:
        return code, (json.loads(text) if text.strip() else {})
    except ValueError:
        return 503, {"error": f"pitv_content API not found at {base} (got HTTP {code}, non-JSON response)",
                     "offline": True}
