"""Validation for PUT /api/settings.

Settings are free-form JSON in the database, but several of them end up as mpv arguments,
file-system paths or an outbound URL. A stolen admin cookie must not be able to turn those
into command injection or a server-side request to the internet, so every key is checked
against the type of its default and the sensitive ones against a strict shape as well.
"""

from __future__ import annotations

import ipaddress
import math
import re
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ... import settings_schema
from ...db import DEFAULT_SETTINGS
from ...player.input import ACTIONS

HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# What an ALSA device name, a DRM connector, an mpv hwdec list or an aspect ratio may contain.
_DEVICE_TOKEN = re.compile(r"^[A-Za-z0-9_.:,/=+-]{0,200}$")
_TZ_NAME = re.compile(r"^[A-Za-z0-9_+/-]{1,64}$")
_PATH_KEYS = ("cache_dir", "acquire_dir")
_DEVICE_KEYS = ("audio_device", "drm_connector", "pi_hwdec", "display_aspect", "display_profile")
_HHMM_KEYS = ("day_start", "day_end", "kids_cutoff")
_PORTS = range(1, 65536)
# Stored with the settings but never read or written through the settings API.
SECRET_SETTINGS = frozenset({"admin_password_hash", "session_secret"})
# RFC 6598 shared address space: carrier-grade NAT, and the addresses Tailscale hands out,
# which is how the two apps would most likely reach each other on separate machines.
_SHARED_NET = ipaddress.ip_network("100.64.0.0/10")


class SettingError(ValueError):
    pass


def _check_path(key: str, value: Any) -> None:
    if not isinstance(value, str):
        raise SettingError(f"{key} must be a string")
    if value == "":
        return
    if "\0" in value or not value.startswith("/") or ".." in value.split("/"):
        raise SettingError(f"{key} must be an absolute path without '..'")


def _lan_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A bare LAN name ("pitv", "nas.local"); anything with a public-looking domain is out.
        return bool(re.match(r"^[A-Za-z0-9-]+(\.(local|lan|home|internal))?$", host))
    if ip.is_unspecified or ip.is_multicast:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or (ip.version == 4 and ip in _SHARED_NET)


def _check_tool_url(value: Any) -> None:
    if not isinstance(value, str):
        raise SettingError("content_tool_url must be a string")
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise SettingError("content_tool_url must be http(s)://host[:port] with no credentials")
    if u.path not in ("", "/") or u.query or u.fragment:
        raise SettingError("content_tool_url must be the base URL only")
    try:
        port = u.port    # raises for a non-numeric or out-of-range port
    except ValueError as exc:
        raise SettingError("content_tool_url port out of range") from exc
    if port is not None and port not in _PORTS:
        raise SettingError("content_tool_url port out of range")
    if not _lan_host(u.hostname):
        raise SettingError("content_tool_url must point at this machine or the local network")


def _check_number(key: str, value: Any, default: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingError(f"{key} must be a number")
    if not math.isfinite(value) or value < 0:
        raise SettingError(f"{key} must be a finite, non-negative number")
    if isinstance(default, int) and not isinstance(value, int) and value != int(value):
        raise SettingError(f"{key} must be a whole number")


def check_setting(key: str, value: Any) -> Any:
    """Validate one setting; returns the value to store. Raises SettingError with a reason."""
    if key not in DEFAULT_SETTINGS or key in SECRET_SETTINGS:
        raise SettingError(f"unknown setting {key}")
    default = DEFAULT_SETTINGS[key]
    choices = settings_schema.choice_values(key)
    if choices is not None and value not in choices:
        raise SettingError(f"{key} must be one of {', '.join(map(str, choices))}")
    if key in _PATH_KEYS:
        _check_path(key, value)
    elif key == "content_tool_url":
        _check_tool_url(value)
    elif key in _DEVICE_KEYS:
        if not isinstance(value, str) or not _DEVICE_TOKEN.match(value):
            raise SettingError(f"{key} contains characters that are not allowed")
    elif key == "timezone":
        if not isinstance(value, str) or not _TZ_NAME.match(value):
            raise SettingError("timezone must be an IANA zone name")
        try:
            ZoneInfo(value)
        except (KeyError, ValueError, OSError) as exc:
            raise SettingError(f"unknown timezone {value}") from exc
    elif key in _HHMM_KEYS:
        if not isinstance(value, str) or not HHMM.match(value):
            raise SettingError(f"{key} must be HH:MM")
    elif key == "keymap":
        if not isinstance(value, dict):
            raise SettingError("keymap must be an object of action -> [key names]")
        for action, keys in value.items():
            if action not in ACTIONS or not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
                raise SettingError(f"keymap: bad entry for {action!r}")
    elif key == "readiness_hours":
        if not isinstance(value, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in value):
            raise SettingError("readiness_hours must be a list of hours 0-23")
    elif key == "browse_roots":
        if not isinstance(value, list):
            raise SettingError("browse_roots must be a list of absolute paths")
        for root in value:
            _check_path(key, root)
            if not root:
                raise SettingError("browse_roots entries must not be empty")
    elif isinstance(default, bool):
        if not isinstance(value, bool):
            raise SettingError(f"{key} must be true or false")
    elif isinstance(default, (int, float)):
        _check_number(key, value, default)
        low, high = settings_schema.bounds(key) or (0, math.inf)
        if not low <= value <= high:
            raise SettingError(f"{key} must be between {low} and {high}")
    elif isinstance(default, str):
        if not isinstance(value, str) or len(value) > 500:
            raise SettingError(f"{key} must be a short string")
    elif isinstance(default, list):
        if not isinstance(value, list):
            raise SettingError(f"{key} must be a list")
    elif isinstance(default, dict) and not isinstance(value, dict):
        raise SettingError(f"{key} must be an object")
    return value
