"""Defensive redaction for RayBet payloads before local persistence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


_SENSITIVE_KEY_PARTS = (
    "authorization",
    "bearer",
    "token",
    "secret",
    "session",
    "csrf",
    "apikey",
    "accesskey",
    "privatekey",
    "password",
    "passwd",
    "credential",
    "signature",
    "authkey",
    "streamkey",
    "hlskey",
    "jwt",
    "cookie",
)
_URL_KEY_PARTS = (
    "url",
    "uri",
    "href",
    "src",
    "stream",
    "playback",
    "playlist",
)
_QUERY_SECRET_RE = re.compile(
    r"([?&])(?:token|auth[_-]?key|signature|sig|expires|exp|authorization)="
    r"[^&#\s]+",
    re.IGNORECASE,
)
_DROP = object()


class RayBetPayloadSanitizationError(ValueError):
    """Raised when a provider payload cannot be safely copied."""


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _is_url_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _URL_KEY_PARTS)


def sanitize_public_url(value: object) -> str | None:
    """Keep only a public URL's scheme, authority and path."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc or parsed.hostname is None:
        if text.startswith("/"):
            return urlunsplit(("", "", parsed.path, "", ""))
        return None
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, "", ""))


def _sanitize_string(value: str, *, key: str) -> str | object:
    if _is_url_key(key) or re.match(r"^(?:https?|wss?|rtmp):", value, re.I):
        sanitized = sanitize_public_url(value)
        if sanitized is not None:
            return sanitized
        if _is_url_key(key):
            return _DROP
    cleaned = _QUERY_SECRET_RE.sub(r"\1", value)
    cleaned = cleaned.replace("?&", "?").replace("&&", "&")
    return cleaned.rstrip("?&")


def sanitize_raybet_payload(
    value: Any, *, max_depth: int = 64, max_nodes: int = 100_000
) -> Any:
    """Return a JSON-shaped copy without credentials or signed URL material."""
    seen: set[int] = set()
    nodes = 0

    def walk(item: Any, key: str, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise RayBetPayloadSanitizationError("payload node limit exceeded")
        if depth > max_depth:
            raise RayBetPayloadSanitizationError("payload depth limit exceeded")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            return _sanitize_string(item, key=key)
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                raise RayBetPayloadSanitizationError("payload cycle detected")
            seen.add(identity)
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                child_key = str(raw_key)
                if _is_sensitive_key(child_key):
                    continue
                sanitized = walk(child, child_key, depth + 1)
                if sanitized is not _DROP:
                    result[child_key] = sanitized
            seen.remove(identity)
            return result
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen:
                raise RayBetPayloadSanitizationError("payload cycle detected")
            seen.add(identity)
            result = [walk(child, key, depth + 1) for child in item]
            seen.remove(identity)
            return [None if child is _DROP else child for child in result]
        raise RayBetPayloadSanitizationError(
            f"unsupported payload value: {type(item).__name__}"
        )

    return walk(value, "", 0)


__all__ = [
    "RayBetPayloadSanitizationError",
    "sanitize_public_url",
    "sanitize_raybet_payload",
]
