"""Strict localhost contract for sanitized RayBet browser events."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import rfc8785


SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 256 * 1024
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
MATCH_RE = re.compile(r"^[0-9]{1,32}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
REASON_RE = re.compile(r"^[a-z0-9_]{1,64}$")
RAYBET_ORIGINS = frozenset(
    {
        "https://ray086.com",
        "https://www.ray086.com",
        "https://cfinfo.365raylinks.com",
        "https://iminfo.esportsworldlink.com",
    }
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
        if value is not None and not REASON_RE.fullmatch(value):
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
        unverifiable_oversize_hash = (
            self.capture_reason == "payload_too_large" and not self.payload
        )
        if (
            not unverifiable_oversize_hash
            and hashlib.sha256(encoded).hexdigest() != self.payload_hash
        ):
            raise ValueError("payload hash mismatch")
        if self.game_id not in (None, 151):
            raise ValueError("only Dota 2 game_id=151 is accepted")
        if self.event_type is not EventType.UNKNOWN and self.game_id != 151:
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
