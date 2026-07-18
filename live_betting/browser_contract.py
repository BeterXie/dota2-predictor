"""Strict localhost contract for sanitized RayBet browser events."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import rfc8785


SCHEMA_VERSION = 1
DOTA2_GAME_ID = 151
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_RAW_PAYLOAD_BYTES = 1024 * 1024
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
MATCH_RE = re.compile(r"^[0-9]{1,32}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
REASON_RE = re.compile(r"^[a-z0-9_]{1,64}$")
CAPTURE_REASONS = frozenset(
    {
        "binary_payload",
        "cycle",
        "diagnostic_untrusted",
        "invalid_candidate",
        "invalid_envelope",
        "invalid_manual_control",
        "max_array_items",
        "max_depth",
        "max_nodes",
        "max_object_keys",
        "max_string_bytes",
        "non_json_value",
        "payload_too_large",
        "raw_payload_too_large",
        "unknown_endpoint",
        "unknown_structure",
    }
)
RAYBET_ORIGINS = frozenset(
    {
        "https://ray086.com",
        "https://www.ray086.com",
        "https://cfinfo.365raylinks.com",
        "https://iminfo.esportsworldlink.com",
    }
)
RAYBET_VIDEO_HOSTS = frozenset(urlsplit(origin).hostname for origin in RAYBET_ORIGINS)
VIDEO_FIELDS = frozenset(
    {
        "state", "status", "playing", "paused", "live", "current_time", "currentTime",
        "duration", "position", "quality", "width", "height", "url", "src",
        "stream_url", "playback_url",
    }
)
VIDEO_PRIMARY_FIELDS = frozenset(
    {
        "state", "status", "playing", "paused", "live", "current_time", "currentTime",
        "duration", "url", "src", "stream_url", "playback_url",
    }
)
VIDEO_NUMERIC_FIELDS = frozenset(
    {"current_time", "currentTime", "duration", "position", "width", "height"}
)
VIDEO_STATE_VALUES = frozenset(
    {"buffering", "ended", "error", "idle", "loading", "paused", "playing", "ready", "stopped"}
)


class Transport(str, Enum):
    FETCH = "fetch"
    XHR = "xhr"
    WEBSOCKET = "websocket"
    PAGE_STATE = "page_state"


class EventType(str, Enum):
    MATCH_LIST = "match_list"
    ODDS = "odds"
    MARKET_UPDATE = "market_update"
    VIDEO = "video"
    MANUAL_CONTROL = "manual_control"
    UNKNOWN = "unknown"


def canonical_json(value: Any) -> bytes:
    """Return RFC 8785 canonical JSON shared with the extension."""
    return rfc8785.dumps(value)


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


_FORBIDDEN_KEY_PARTS = (
    "cookie",
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
    "user",
    "member",
    "account",
    "profile",
    "username",
    "phone",
    "email",
    "identity",
    "balance",
    "wallet",
    "currency",
    "deposit",
    "withdrawal",
    "rebate",
    "transaction",
    "device",
    "fingerprint",
    "advertising",
    "analytics",
    "persistentclient",
    "clientid",
    "visitorid",
    "browserid",
    "machineid",
    "installid",
    "betslip",
    "selectionslip",
    "stake",
    "potentialreturn",
    "order",
    "submit",
    "ticket",
    "requestheader",
    "responseheader",
    "requestbody",
    "formdata",
    "postbody",
)
_DANGEROUS_KEYS = frozenset({"__proto__", "prototype", "constructor"})


def find_forbidden_payload_key(value: Any) -> str | None:
    """Return the first forbidden payload key without retaining its value."""
    stack = [value]
    visited: set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
        if isinstance(current, dict):
            for key, child in current.items():
                if str(key).casefold() in _DANGEROUS_KEYS:
                    return str(key)
                normalized = _normalized_key(str(key))
                if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                    return str(key)
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return None


class BrowserEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: int
    event_id: str
    capture_session_id: str
    captured_at_utc: datetime
    page_origin: str
    page_path: str = Field(max_length=2048)
    source_path: str = Field(max_length=2048)
    transport: Transport
    event_type: EventType
    raybet_match_id: str | None = None
    game_id: int | None = None
    payload: dict[str, Any]
    payload_hash: str
    payload_bytes: int = Field(ge=0, le=2**31 - 1)
    capture_reason: str | None = None
    extension_version: str

    @field_validator("captured_at_utc", mode="before")
    @classmethod
    def parse_utc_timestamp(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("captured_at_utc must include a UTC offset")
        if value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("captured_at_utc must be UTC")
        return value.astimezone(timezone.utc)

    @field_validator("event_id", "payload_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not HASH_RE.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("capture_session_id")
    @classmethod
    def validate_session(cls, value: str) -> str:
        if not SESSION_RE.fullmatch(value):
            raise ValueError("must be a 128-bit lowercase hex value")
        return value

    @field_validator("raybet_match_id")
    @classmethod
    def validate_match_id(cls, value: str | None) -> str | None:
        if value is not None and not MATCH_RE.fullmatch(value):
            raise ValueError("invalid RayBet match id")
        return value

    @field_validator("page_origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            value not in RAYBET_ORIGINS
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("page_origin is not an allowed RayBet origin")
        return value

    @field_validator("page_path", "source_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not value.startswith("/") or "?" in value or "#" in value or "\x00" in value:
            raise ValueError("paths must exclude query strings and fragments")
        return value

    @field_validator("capture_reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is not None and (
            not REASON_RE.fullmatch(value) or value not in CAPTURE_REASONS
        ):
            raise ValueError("invalid capture reason")
        return value

    @field_validator("extension_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not VERSION_RE.fullmatch(value):
            raise ValueError("invalid extension version")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> "BrowserEvent":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        forbidden = find_forbidden_payload_key(self.payload)
        if forbidden is not None:
            raise ValueError("payload contains a forbidden field")
        encoded = canonical_json(self.payload)
        if len(encoded) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds 256 KiB")
        metadata_only = not self.payload and self.capture_reason is not None
        if not metadata_only and self.payload_bytes != len(encoded):
            raise ValueError("payload_bytes does not match canonical payload size")
        if self.capture_reason == "payload_too_large" and (
            self.payload_bytes <= MAX_PAYLOAD_BYTES or self.payload
        ):
            raise ValueError("payload_too_large must carry the full size and no payload")
        if self.capture_reason == "raw_payload_too_large" and (
            self.payload_bytes <= MAX_RAW_PAYLOAD_BYTES or self.payload
        ):
            raise ValueError("raw_payload_too_large must carry a lower-bound size")
        unverifiable_oversize_hash = (
            self.capture_reason == "payload_too_large" and not self.payload
        )
        if (
            not unverifiable_oversize_hash
            and hashlib.sha256(encoded).hexdigest() != self.payload_hash
        ):
            raise ValueError("payload hash mismatch")
        if self.game_id not in (None, DOTA2_GAME_ID):
            raise ValueError("only Dota 2 game_id=151 is accepted")
        if self.event_type is not EventType.UNKNOWN and self.game_id != DOTA2_GAME_ID:
            raise ValueError("recognized events require game_id=151")
        match_bound = {
            EventType.ODDS,
            EventType.MARKET_UPDATE,
            EventType.VIDEO,
            EventType.MANUAL_CONTROL,
        }
        if self.event_type in match_bound and self.raybet_match_id is None:
            raise ValueError("event type requires a RayBet match id")
        if self.event_type is EventType.UNKNOWN and self.payload:
            raise ValueError("unknown events must be metadata-only")
        if self.event_type is EventType.VIDEO:
            _validate_video_payload(self.payload)
        if (
            self.capture_reason in {"payload_too_large", "raw_payload_too_large"}
            and self.payload
        ):
            raise ValueError("oversized events must be metadata-only")
        if (
            self.event_type is EventType.MANUAL_CONTROL
            and self.capture_reason != "diagnostic_untrusted"
        ):
            raise ValueError("manual control data must remain diagnostic")
        return self


KNOWN_ENVELOPE_KEYS = frozenset(BrowserEvent.model_fields)


def find_forbidden_batch_key(value: Any) -> str | None:
    """Scan raw event dictionaries before model validation and database I/O."""
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        forbidden = find_forbidden_payload_key(payload)
        if forbidden is not None:
            return forbidden
        for key in item:
            if key not in KNOWN_ENVELOPE_KEYS:
                if str(key).casefold() in _DANGEROUS_KEYS:
                    return str(key)
                normalized = _normalized_key(str(key))
                if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                    return str(key)
    return None


def _validate_video_payload(payload: dict[str, Any]) -> None:
    result = payload.get("result")
    if not isinstance(result, dict) or isinstance(result, list):
        raise ValueError("video payload must contain a result object")
    if "odds" in result or "team" in result:
        raise ValueError("video payload cannot contain market or team arrays")
    if set(result) - VIDEO_FIELDS:
        raise ValueError("video payload contains an unsupported field")
    if not VIDEO_PRIMARY_FIELDS.intersection(result):
        raise ValueError("video payload lacks a playback marker")
    for key, value in result.items():
        if key in {"state", "status"}:
            if not isinstance(value, str) or value.casefold() not in VIDEO_STATE_VALUES:
                raise ValueError("video state is not allowlisted")
        elif key in {"playing", "paused", "live"}:
            if not isinstance(value, bool):
                raise ValueError("video boolean state is invalid")
        elif key in VIDEO_NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("video numeric state is invalid")
            if not math.isfinite(float(value)) or not 0 <= float(value) <= 86_400:
                raise ValueError("video numeric state is out of range")
        elif key == "quality":
            if not isinstance(value, str) or value.casefold() not in {
                "auto", "low", "medium", "high", "source"
            }:
                raise ValueError("video quality is not allowlisted")
        elif key in {"url", "src", "stream_url", "playback_url"}:
            if not isinstance(value, str):
                raise ValueError("video URL is invalid")
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in RAYBET_VIDEO_HOSTS
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (parsed.port not in {None, 443})
            ):
                raise ValueError("video URL must be public and sanitized")
