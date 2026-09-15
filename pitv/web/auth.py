"""Single-password admin login with a signed session cookie."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request, Response
from itsdangerous import BadData, URLSafeTimedSerializer

from ..db import get_setting, set_setting, tx

COOKIE = "pitv_session"
SESSION_SECONDS = 30 * 86400
PBKDF2_ITERATIONS = 600_000     # OWASP's current figure for PBKDF2-HMAC-SHA256
_LEGACY_ITERATIONS = 200_000    # hashes written before the count was stored alongside them
LOGIN_WINDOW_SECONDS = 300
LOGIN_ATTEMPTS = 8
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def _parse(stored: str) -> tuple[int, bytes, str] | None:
    parts = stored.split("$")
    try:
        if len(parts) == 4 and parts[0] == "pbkdf2_sha256":
            return int(parts[1]), bytes.fromhex(parts[2]), parts[3]
        if len(parts) == 3 and parts[0] == "pbkdf2":
            return _LEGACY_ITERATIONS, bytes.fromhex(parts[1]), parts[2]
    except ValueError:
        return None
    return None


def verify_password(password: str, stored: str | None) -> bool:
    parsed = _parse(stored or "")
    if parsed is None:
        return False
    iterations, salt, digest_hex = parsed
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(digest.hex(), digest_hex)


def needs_rehash(stored: str | None) -> bool:
    """True for a hash made with fewer iterations than we use now; upgraded on the next login."""
    parsed = _parse(stored or "")
    return parsed is not None and parsed[0] < PBKDF2_ITERATIONS


def secret_key(conn: sqlite3.Connection) -> str:
    key = get_setting(conn, "session_secret")
    if not key:
        with tx(conn):
            # Re-read inside the write transaction so two first requests cannot mint two keys.
            key = get_setting(conn, "session_secret")
            if not key:
                key = secrets.token_hex(32)
                set_setting(conn, "session_secret", key)
    return key


def rotate_secret(conn: sqlite3.Connection) -> None:
    """Sessions are stateless signed cookies, so a new signing key is the only way to revoke
    them. Called on a password change: whoever held a session under the old password loses it."""
    set_setting(conn, "session_secret", secrets.token_hex(32))


def serializer(conn: sqlite3.Connection) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(conn), salt="pitv-session")


def password_is_set(conn: sqlite3.Connection) -> bool:
    return bool(get_setting(conn, "admin_password_hash"))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def login_allowed(ip: str) -> bool:
    """Count one login attempt against the address; False once it has used its allowance.

    Checking and recording happen under one lock and before the password is verified, so a
    burst of parallel requests cannot all pass the check and then each cost a PBKDF2 run."""
    now = time.time()
    with _attempts_lock:
        # Forget addresses that have gone quiet so the table cannot grow without bound.
        for stale in [k for k, v in _attempts.items() if now - v[-1] >= LOGIN_WINDOW_SECONDS]:
            del _attempts[stale]
        hits = [t for t in _attempts.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        if len(hits) >= LOGIN_ATTEMPTS:
            _attempts[ip] = hits
            return False
        _attempts[ip] = [*hits, now]
        return True


def forget_attempts(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)


def is_admin(request: Request, conn: sqlite3.Connection) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        data = serializer(conn).loads(token, max_age=SESSION_SECONDS)
    except BadData:
        return False
    return isinstance(data, dict) and bool(data.get("admin"))


def has_admin(request: Request, conn: sqlite3.Connection) -> bool:
    """Admin rights: a valid session, or first run before any password exists."""
    return not password_is_set(conn) or is_admin(request, conn)


def require_admin(request: Request, conn: sqlite3.Connection) -> None:
    if not has_admin(request, conn):
        raise HTTPException(status_code=401, detail="Admin login required")


def make_session(conn: sqlite3.Connection) -> str:
    return serializer(conn).dumps({"admin": True, "t": int(time.time())})


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def set_session_cookie(request: Request, response: Response, conn: sqlite3.Connection) -> None:
    """HttpOnly and SameSite=Lax always; Secure whenever the request came over TLS (a reverse
    proxy in front of the Pi) so the browser never sends the session back in clear."""
    response.set_cookie(COOKIE, make_session(conn), max_age=SESSION_SECONDS, httponly=True,
                        samesite="lax", secure=_is_https(request))


# --- pitv_content's token ---------------------------------------------------------------------

CONTENT_TOKEN_FILE = "content-token"


def content_token(data_dir: Path, rotate: bool = False) -> str:
    """The token pitv_content presents on the manifest, report and make-room endpoints, which it
    calls without a browser session. It lives in a file only the service user can read, beside
    the database: pitv_content on the same Pi runs as that user and reads it there; on another
    machine the admin copies it across from the Content page."""
    path = data_dir / CONTENT_TOKEN_FILE
    if not rotate:
        try:
            token = path.read_text().strip()
        except OSError:
            token = ""
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(f".{CONTENT_TOKEN_FILE}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(tmp, 0o600)          # O_CREAT's mode does not apply to a file left by a crash
    os.replace(tmp, path)
    return token


def is_content_client(request: Request) -> bool:
    """Whether the request carries pitv_content's token as a bearer credential."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    token = getattr(request.app.state, "content_token", "")
    return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(value.strip(), token)
