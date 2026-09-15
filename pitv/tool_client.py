"""HTTP client for pitv_content's local API (docs/CONTENT_CONTRACT.md).

Used by the web admin (proxy, run now, catalogue options) and by the player's maintenance
thread (library index import), so it lives outside the web package. It never raises for a
network or protocol failure: callers get a status and an error document and carry on.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .db import DEFAULT_SETTINGS

# The library index is the largest answer: a few MB for a big NAS. Anything far beyond that is
# not pitv_content, and reading it would only cost the Pi memory.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """pitv_content never redirects. Following one would let whatever answers on
    content_tool_url send PiTV's requests, and the admin's proxied ones, to any address,
    around the rule that keeps that URL on this machine or the LAN. Returning None makes the
    3xx arrive as an HTTPError, which `request` reports."""

    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


# No environment proxies either: the API is local, and a proxy would see every request.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def base_url(settings: dict[str, Any]) -> str:
    return settings.get("content_tool_url") or DEFAULT_SETTINGS["content_tool_url"]


def _offline(message: str) -> tuple[int, dict[str, Any]]:
    return 503, {"error": message, "offline": True}


def _body(response: Any) -> bytes | None:
    """The response body, or None when it is larger than MAX_RESPONSE_BYTES."""
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    return None if len(raw) > MAX_RESPONSE_BYTES else raw


def request(base: str, method: str, path: str, query: str = "", body: dict[str, Any] | None = None,
            timeout: float = 15) -> tuple[int, Any]:
    """One JSON request, as (HTTP status, decoded body). An unreachable or silent service, or
    anything that is not JSON (an unrelated service on that port answering with an HTML page),
    means the tool is not there: 503 with `offline`. A redirect or an oversized answer is a 502.
    The URL must be http(s); the setting is validated when saved, and this keeps a hand-edited
    database from reaching file:// URLs."""
    if urlsplit(base).scheme not in ("http", "https"):
        return _offline(f"pitv_content API URL {base!r} is not http(s)")
    url = f"{base.rstrip('/')}/api/{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                code, raw = r.status, _body(r)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                return 502, {"error": f"pitv_content API at {base} answered with a redirect (HTTP {exc.code}),"
                                      f" which PiTV does not follow"}
            code, raw = exc.code, _body(exc)
    except (OSError, http.client.HTTPException, ValueError) as exc:   # URLError and timeouts are OSErrors
        return _offline(f"pitv_content API unavailable at {base}: {exc}")
    if raw is None:
        return 502, {"error": f"pitv_content API at {base} sent more than {MAX_RESPONSE_BYTES} bytes"}
    text = raw.decode("utf-8", errors="replace")
    try:
        return code, (json.loads(text) if text.strip() else {})
    except ValueError:
        return _offline(f"pitv_content API not found at {base} (got HTTP {code}, non-JSON response)")
