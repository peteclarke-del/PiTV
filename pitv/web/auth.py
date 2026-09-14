"""Single-password admin login with a signed session cookie."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..db import get_setting, set_setting, tx

COOKIE = "pitv_session"
SESSION_SECONDS = 30 * 86400
_attempts: dict[str, list[float]] = {}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def secret_key(conn: sqlite3.Connection) -> str:
    key = get_setting(conn, "session_secret")
    if not key:
        key = secrets.token_hex(32)
        with tx(conn):
            set_setting(conn, "session_secret", key)
    return key


def serializer(conn: sqlite3.Connection) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(conn), salt="pitv-session")


def password_is_set(conn: sqlite3.Connection) -> bool:
    return bool(get_setting(conn, "admin_password_hash"))


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < 300]
    _attempts[ip] = hits
    return len(hits) >= 8


def record_attempt(ip: str) -> None:
    _attempts.setdefault(ip, []).append(time.time())


def is_admin(request: Request, conn: sqlite3.Connection) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        data = serializer(conn).loads(token, max_age=SESSION_SECONDS)
    except BadSignature:
        return False
    return bool(data.get("admin"))


def require_admin(request: Request, conn: sqlite3.Connection) -> None:
    if not password_is_set(conn):
        return  # first run: admin is open until a password is set
    if not is_admin(request, conn):
        raise HTTPException(status_code=401, detail="Admin login required")


def make_session(conn: sqlite3.Connection) -> str:
    return serializer(conn).dumps({"admin": True, "t": int(time.time())})
