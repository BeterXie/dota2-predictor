"""Immutable STRATZ official R.O.S.H. request/profile contract.

This module intentionally contains the captured GraphQL documents.  The runtime
never reads the capture fixture; the fixture is only an independent verification
source for the constants and their hashes.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import rfc8785


PROFILE_ID = "stratz-rosh-web-2026-07-28-v1"
FORMULA_VERSION = "stratz-official-rosh/2026-07-28-v1"
V2_PROFILE_ID = "stratz-rosh-web-2026-07-28-v2"
V2_FORMULA_VERSION = "stratz-official-rosh/2026-07-28-v2"
ACTIVE_PROFILE_ID = V2_PROFILE_ID
UPSTREAM_BUNDLE_HASH = "9f11c70b970bab3de71f517c36551dca2cee143d176d86c649f3542a2fe90357"
SCORER_SOURCE_HASH = "c0f0ec77aa90468c4f741e133dac4a013ef8236ec6be3342a169adfbbe4d837c"
SERIALIZATION_VERSION = "rfc8785-jcs/v1"
PRESENTATION_ROUNDING = "js-number-to-fixed/1"
ENDPOINT = "https://api.stratz.com/graphql"
ALLOWED_BRACKETS = ("IMMORTAL",)
WEEK_SECONDS = 7 * 24 * 60 * 60
MAX_HEROES_META_SKIP = 25
V1_STATE = "frozen/unactivated/superseded-for-implementation"
V2_STATE = "active"

FROZEN_ARTIFACT_HASHES = MappingProxyType({
    "manifest.json": "b4c14b14ed283d78123aa5ed9724f56db7e2a055e393dcde0a14d620820fb0fb",
    "requests.json": "280f11b38a29c87751c4f36c74d95d4b89bf087f00b766331fbbe379f551971f",
    "responses.sanitized.json": "2afbe95c420676d34b87737138133443673a8d8c9e7d2bf10069712e799e70e7",
    "expected.json": "743b67ec2c5628934cea6834ee6832a951634179bdafcdbcdfa8b139d6d7305b",
    "page-assets.json": "ec5f08ca6c54779ee3a76a5f81401761d668908acc68d5b08d715b8e9634b70e",
    "upstream-bundle-7473.55187c1bd3991522.js": UPSTREAM_BUNDLE_HASH,
})


class ProfileError(ValueError):
    """Raised when an immutable profile or request identity is unsafe."""


def _decode(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _freeze(value: Any) -> Any:
    """Recursively freeze JSON-shaped values used in identity-bearing objects."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical_rows(rows: Sequence[Any], side: str) -> tuple[Mapping[str, Any], ...]:
    frozen: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProfileError(f"{side} draft entry must be an object")
        item = _freeze(row)
        position = item.get("position_id", item.get("positionId"))
        if isinstance(position, bool) or not isinstance(position, int):
            raise ProfileError(f"{side} draft entry has an invalid position_id")
        frozen.append(item)
    return tuple(sorted(frozen, key=lambda item: item.get("position_id", item.get("positionId"))))


# These are the exact UTF-8 query documents captured from the official page.
_QUERY_DOCUMENTS = MappingProxyType({
    "GetMatchPicksBans": _decode("cXVlcnkgR2V0TWF0Y2hQaWNrc0JhbnMoJG1hdGNoSWQ6IExvbmchKSB7CiAgbWF0Y2goaWQ6ICRtYXRjaElkKSB7CiAgICBpZAogICAgZ2FtZU1vZGUKICAgIHJlZ2lvbklkCiAgICBkdXJhdGlvblNlY29uZHMKICAgIGVuZERhdGVUaW1lCiAgICBsb2JieVR5cGUKICAgIGRpZFJhZGlhbnRXaW4KICAgIHJhZGlhbnRLaWxscwogICAgZGlyZUtpbGxzCiAgICBicmFja2V0CiAgICByYWRpYW50VGVhbSB7CiAgICAgIGlkCiAgICAgIG5hbWUKICAgICAgX190eXBlbmFtZQogICAgfQogICAgZGlyZVRlYW0gewogICAgICBpZAogICAgICBuYW1lCiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGxlYWd1ZSB7CiAgICAgIGlkCiAgICAgIGRpc3BsYXlOYW1lCiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIHBsYXllcnMgewogICAgICBoZXJvSWQKICAgICAgcG9zaXRpb24KICAgICAgX190eXBlbmFtZQogICAgfQogICAgcGlja0JhbnMgewogICAgICBoZXJvSWQKICAgICAgb3JkZXIKICAgICAgaXNQaWNrCiAgICAgIGlzUmFkaWFudAogICAgICBiYW5uZWRIZXJvSWQKICAgICAgd2FzQmFubmVkU3VjY2Vzc2Z1bGx5CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIF9fdHlwZW5hbWUKICB9Cn0="),
    "HeroesMetaPositions": _decode("cXVlcnkgSGVyb2VzTWV0YVBvc2l0aW9ucygkYnJhY2tldElkczogW1JhbmtCcmFja2V0XSwgJHRha2U6IEludCwgJHNraXA6IEludCwgJGhlcm9JZHM6IFtTaG9ydF0pIHsKICBoZXJvU3RhdHMgewogICAgaGVyb2VzUG9zXzE6IHdpbkRheSgKICAgICAgdGFrZTogJHRha2UKICAgICAgc2tpcDogJHNraXAKICAgICAgcG9zaXRpb25JZHM6IFtQT1NJVElPTl8xXQogICAgICBicmFja2V0SWRzOiAkYnJhY2tldElkcwogICAgICBoZXJvSWRzOiAkaGVyb0lkcwogICAgKSB7CiAgICAgIGhlcm9JZAogICAgICBtYXRjaENvdW50CiAgICAgIHdpbkNvdW50CiAgICAgIHRpbWVzdGFtcDogZGF5CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGhlcm9lc1Bvc18yOiB3aW5EYXkoCiAgICAgIHRha2U6ICR0YWtlCiAgICAgIHNraXA6ICRza2lwCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fMl0KICAgICAgYnJhY2tldElkczogJGJyYWNrZXRJZHMKICAgICAgaGVyb0lkczogJGhlcm9JZHMKICAgICkgewogICAgICBoZXJvSWQKICAgICAgbWF0Y2hDb3VudAogICAgICB3aW5Db3VudAogICAgICB0aW1lc3RhbXA6IGRheQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBoZXJvZXNQb3NfMzogd2luRGF5KAogICAgICB0YWtlOiAkdGFrZQogICAgICBza2lwOiAkc2tpcAogICAgICBwb3NpdGlvbklkczogW1BPU0lUSU9OXzNdCiAgICAgIGJyYWNrZXRJZHM6ICRicmFja2V0SWRzCiAgICAgIGhlcm9JZHM6ICRoZXJvSWRzCiAgICApIHsKICAgICAgaGVyb0lkCiAgICAgIG1hdGNoQ291bnQKICAgICAgd2luQ291bnQKICAgICAgdGltZXN0YW1wOiBkYXkKICAgICAgX190eXBlbmFtZQogICAgfQogICAgaGVyb2VzUG9zXzQ6IHdpbkRheSgKICAgICAgdGFrZTogJHRha2UKICAgICAgc2tpcDogJHNraXAKICAgICAgcG9zaXRpb25JZHM6IFtQT1NJVElPTl80XQogICAgICBicmFja2V0SWRzOiAkYnJhY2tldElkcwogICAgICBoZXJvSWRzOiAkaGVyb0lkcwogICAgKSB7CiAgICAgIGhlcm9JZAogICAgICBtYXRjaENvdW50CiAgICAgIHdpbkNvdW50CiAgICAgIHRpbWVzdGFtcDogZGF5CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGhlcm9lc1Bvc181OiB3aW5EYXkoCiAgICAgIHRha2U6ICR0YWtlCiAgICAgIHNraXA6ICRza2lwCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fNV0KICAgICAgYnJhY2tldElkczogJGJyYWNrZXRJZHMKICAgICAgaGVyb0lkczogJGhlcm9JZHMKICAgICkgewogICAgICBoZXJvSWQKICAgICAgbWF0Y2hDb3VudAogICAgICB3aW5Db3VudAogICAgICB0aW1lc3RhbXA6IGRheQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBoZXJvZXM6IHdpbkRheSgKICAgICAgdGFrZTogJHRha2UKICAgICAgc2tpcDogJHNraXAKICAgICAgYnJhY2tldElkczogJGJyYWNrZXRJZHMKICAgICAgaGVyb0lkczogJGhlcm9JZHMKICAgICkgewogICAgICBoZXJvSWQKICAgICAgbWF0Y2hDb3VudAogICAgICB3aW5Db3VudAogICAgICB0aW1lc3RhbXA6IGRheQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBfX3R5cGVuYW1lCiAgfQp9"),
    "GetMatchCountPreviousWeekDay": _decode("cXVlcnkgR2V0TWF0Y2hDb3VudFByZXZpb3VzV2Vla0RheSgkYnJhY2tldElkczogW1JhbmtCcmFja2V0XSkgewogIHN0cmF0eiB7CiAgICBwYWdlIHsKICAgICAgbWF0Y2hlcyB7CiAgICAgICAgbWF0Y2hlc1N0YXRzRGF5KHRha2U6IDMyLCBicmFja2V0SWRzOiAkYnJhY2tldElkcykgewogICAgICAgICAgZGF5CiAgICAgICAgICBtYXRjaENvdW50CiAgICAgICAgICBfX3R5cGVuYW1lCiAgICAgICAgfQogICAgICAgIG1hdGNoZXNTdGF0c1dlZWsodGFrZTogNCwgYnJhY2tldElkczogJGJyYWNrZXRJZHMpIHsKICAgICAgICAgIHdlZWsKICAgICAgICAgIG1hdGNoQ291bnQKICAgICAgICAgIF9fdHlwZW5hbWUKICAgICAgICB9CiAgICAgICAgX190eXBlbmFtZQogICAgICB9CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIF9fdHlwZW5hbWUKICB9Cn0="),
    "Synergy": _decode("cXVlcnkgU3luZXJneSgkYnJhY2tldEJhc2ljSWRzOiBbUmFua0JyYWNrZXRCYXNpY0VudW1dLCAkbWF0Y2hMaW1pdDogSW50LCAkdGFrZTogSW50LCAkaGVyb0lkczogW1Nob3J0XSkgewogIGhlcm9TdGF0cyB7CiAgICBtYXRjaFVwX1ByZXZfV2Vla18xOiBtYXRjaFVwKAogICAgICBicmFja2V0QmFzaWNJZHM6ICRicmFja2V0QmFzaWNJZHMKICAgICAgbWF0Y2hMaW1pdDogJG1hdGNoTGltaXQKICAgICAgdGFrZTogJHRha2UKICAgICAgd2VlazogMTc4NDQ4NTU0OAogICAgICBoZXJvSWRzOiAkaGVyb0lkcwogICAgKSB7CiAgICAgIGhlcm9JZAogICAgICB2cyB7CiAgICAgICAgaGVyb0lkMgogICAgICAgIHN5bmVyZ3kKICAgICAgICBtYXRjaENvdW50CiAgICAgICAgX190eXBlbmFtZQogICAgICB9CiAgICAgIHdpdGggewogICAgICAgIGhlcm9JZDIKICAgICAgICBzeW5lcmd5CiAgICAgICAgbWF0Y2hDb3VudAogICAgICAgIF9fdHlwZW5hbWUKICAgICAgfQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBtYXRjaFVwX1ByZXZfV2Vla18yOiBtYXRjaFVwKAogICAgICBicmFja2V0QmFzaWNJZHM6ICRicmFja2V0QmFzaWNJZHMKICAgICAgbWF0Y2hMaW1pdDogJG1hdGNoTGltaXQKICAgICAgdGFrZTogJHRha2UKICAgICAgd2VlazogMTc4Mzg4MDc0OAogICAgICBoZXJvSWRzOiAkaGVyb0lkcwogICAgKSB7CiAgICAgIGhlcm9JZAogICAgICB2cyB7CiAgICAgICAgaGVyb0lkMgogICAgICAgIHN5bmVyZ3kKICAgICAgICBtYXRjaENvdW50CiAgICAgICAgX190eXBlbmFtZQogICAgICB9CiAgICAgIHdpdGggewogICAgICAgIGhlcm9JZDIKICAgICAgICBzeW5lcmd5CiAgICAgICAgbWF0Y2hDb3VudAogICAgICAgIF9fdHlwZW5hbWUKICAgICAgfQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBtYXRjaFVwX1ByZXZfV2Vla18zOiBtYXRjaFVwKAogICAgICBicmFja2V0QmFzaWNJZHM6ICRicmFja2V0QmFzaWNJZHMKICAgICAgbWF0Y2hMaW1pdDogJG1hdGNoTGltaXQKICAgICAgdGFrZTogJHRha2UKICAgICAgd2VlazogMTc4MzI3NTk0OAogICAgICBoZXJvSWRzOiAkaGVyb0lkcwogICAgKSB7CiAgICAgIGhlcm9JZAogICAgICB2cyB7CiAgICAgICAgaGVyb0lkMgogICAgICAgIHN5bmVyZ3kKICAgICAgICBtYXRjaENvdW50CiAgICAgICAgX190eXBlbmFtZQogICAgICB9CiAgICAgIHdpdGggewogICAgICAgIGhlcm9JZDIKICAgICAgICBzeW5lcmd5CiAgICAgICAgbWF0Y2hDb3VudAogICAgICAgIF9fdHlwZW5hbWUKICAgICAgfQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBtYXRjaFVwX1ByZXZfV2Vla180OiBtYXRjaFVwKAogICAgICBicmFja2V0QmFzaWNJZHM6ICRicmFja2V0QmFzaWNJZHMKICAgICAgbWF0Y2hMaW1pdDogJG1hdGNoTGltaXQKICAgICAgdGFrZTogJHRha2UKICAgICAgd2VlazogMTc4MjY3MTE0OAogICAgICBoZXJvSWRzOiAkaGVyb0lkcwogICAgKSB7CiAgICAgIGhlcm9JZAogICAgICB2cyB7CiAgICAgICAgaGVyb0lkMgogICAgICAgIHN5bmVyZ3kKICAgICAgICBtYXRjaENvdW50CiAgICAgICAgX190eXBlbmFtZQogICAgICB9CiAgICAgIHdpdGggewogICAgICAgIGhlcm9JZDIKICAgICAgICBzeW5lcmd5CiAgICAgICAgbWF0Y2hDb3VudAogICAgICAgIF9fdHlwZW5hbWUKICAgICAgfQogICAgICBfX3R5cGVuYW1lCiAgICB9CiAgICBfX3R5cGVuYW1lCiAgfQp9"),
    "GetHeroStatsByTime": _decode("cXVlcnkgR2V0SGVyb1N0YXRzQnlUaW1lKCRicmFja2V0QmFzaWNJZHM6IFtSYW5rQnJhY2tldEJhc2ljRW51bV0sICR3ZWVrOiBMb25nKSB7CiAgaGVyb1N0YXRzIHsKICAgIGhlcm9TdGF0c0J5VGltZV8xOiBzdGF0cygKICAgICAgYnJhY2tldEJhc2ljSWRzOiAkYnJhY2tldEJhc2ljSWRzCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fMV0KICAgICAgZ3JvdXBCeVRpbWU6IHRydWUKICAgICAgbWluVGltZTogMjAKICAgICAgbWF4VGltZTogNjAKICAgICAgd2VlazogJHdlZWsKICAgICkgewogICAgICBoZXJvSWQKICAgICAgdGltZQogICAgICB3aW5Db3VudAogICAgICBtYXRjaENvdW50CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGhlcm9TdGF0c0J5VGltZV8yOiBzdGF0cygKICAgICAgYnJhY2tldEJhc2ljSWRzOiAkYnJhY2tldEJhc2ljSWRzCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fMl0KICAgICAgZ3JvdXBCeVRpbWU6IHRydWUKICAgICAgbWluVGltZTogMjAKICAgICAgbWF4VGltZTogNjAKICAgICAgd2VlazogJHdlZWsKICAgICkgewogICAgICBoZXJvSWQKICAgICAgdGltZQogICAgICB3aW5Db3VudAogICAgICBtYXRjaENvdW50CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGhlcm9TdGF0c0J5VGltZV8zOiBzdGF0cygKICAgICAgYnJhY2tldEJhc2ljSWRzOiAkYnJhY2tldEJhc2ljSWRzCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fM10KICAgICAgZ3JvdXBCeVRpbWU6IHRydWUKICAgICAgbWluVGltZTogMjAKICAgICAgbWF4VGltZTogNjAKICAgICAgd2VlazogJHdlZWsKICAgICkgewogICAgICBoZXJvSWQKICAgICAgdGltZQogICAgICB3aW5Db3VudAogICAgICBtYXRjaENvdW50CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGhlcm9TdGF0c0J5VGltZV80OiBzdGF0cygKICAgICAgYnJhY2tldEJhc2ljSWRzOiAkYnJhY2tldEJhc2ljSWRzCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fNF0KICAgICAgZ3JvdXBCeVRpbWU6IHRydWUKICAgICAgbWluVGltZTogMjAKICAgICAgbWF4VGltZTogNjAKICAgICAgd2VlazogJHdlZWsKICAgICkgewogICAgICBoZXJvSWQKICAgICAgdGltZQogICAgICB3aW5Db3VudAogICAgICBtYXRjaENvdW50CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIGhlcm9TdGF0c0J5VGltZV81OiBzdGF0cygKICAgICAgYnJhY2tldEJhc2ljSWRzOiAkYnJhY2tldEJhc2ljSWRzCiAgICAgIHBvc2l0aW9uSWRzOiBbUE9TSVRJT05fNV0KICAgICAgZ3JvdXBCeVRpbWU6IHRydWUKICAgICAgbWluVGltZTogMjAKICAgICAgbWF4VGltZTogNjAKICAgICAgd2VlazogJHdlZWsKICAgICkgewogICAgICBoZXJvSWQKICAgICAgdGltZQogICAgICB3aW5Db3VudAogICAgICBtYXRjaENvdW50CiAgICAgIF9fdHlwZW5hbWUKICAgIH0KICAgIF9fdHlwZW5hbWUKICB9Cn0="),
})

@dataclass(frozen=True)
class RoshParityProfile:
    rosh_profile_id: str
    formula_version: str
    request_profile_hash: str
    upstream_bundle_hash: str
    serialization_version: str
    scorer_source_hash: str = ""
    canonical_profile_hash: str = ""
    state: str = V1_STATE


@dataclass(frozen=True)
class RoshAnalysisInput:
    mode: str
    date_time: int
    bracket_ids: tuple[str, ...]
    match_id: int | None = None
    radiant: tuple[Mapping[str, Any], ...] = ()
    dire: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bracket_ids", tuple(self.bracket_ids))
        object.__setattr__(self, "radiant", _canonical_rows(self.radiant, "radiant"))
        object.__setattr__(self, "dire", _canonical_rows(self.dire, "dire"))

    @classmethod
    def from_value(cls, value: "RoshAnalysisInput | Mapping[str, Any]") -> "RoshAnalysisInput":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ProfileError("analysis input must be an object")
        def rows(name: str) -> tuple[Mapping[str, Any], ...]:
            raw = value.get(name, ())
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise ProfileError(f"{name} must be an array")
            rows: list[Mapping[str, Any]] = []
            for item in raw:
                if not isinstance(item, Mapping):
                    raise ProfileError(f"{name} draft entry must be an object")
                rows.append(dict(item))
            return tuple(rows)
        brackets = value.get("bracket_ids", value.get("bracketIds"))
        if not isinstance(brackets, Sequence) or isinstance(brackets, (str, bytes)):
            raise ProfileError("bracket_ids must be an array")
        return cls(
            mode=str(value.get("mode", "")),
            date_time=value.get("date_time", value.get("dateTime")),
            bracket_ids=tuple(str(item) for item in brackets),
            match_id=value.get("match_id", value.get("matchId")),
            radiant=rows("radiant"),
            dire=rows("dire"),
        )


@dataclass(frozen=True)
class RequestOperation:
    index: int
    operation_name: str
    query: str
    variables: Mapping[str, Any]
    query_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "variables", _freeze(self.variables))


@dataclass(frozen=True)
class RoshRequestPlan:
    profile: RoshParityProfile
    analysis_input: RoshAnalysisInput
    operations: tuple[RequestOperation, ...]
    elapsed_days: int
    metadata_mode: str
    current_day_shift: int
    week_anchors: tuple[int, ...]
    request_hash: str
    request_started_at: datetime | None = field(default=None, compare=False)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(_thaw(value))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def query_documents() -> Mapping[str, str]:
    return _QUERY_DOCUMENTS


def validate_draft(radiant: Sequence[Mapping[str, Any]], dire: Sequence[Mapping[str, Any]]) -> None:
    if len(radiant) != 5 or len(dire) != 5:
        raise ProfileError("each side must contain exactly five heroes")
    all_ids: list[int] = []
    for side, rows in (("radiant", radiant), ("dire", dire)):
        heroes: list[int] = []
        positions: list[int] = []
        for row in rows:
            try:
                hero_id = row["hero_id"] if "hero_id" in row else row["heroId"]
                position = row["position_id"] if "position_id" in row else row["positionId"]
            except (KeyError, TypeError) as exc:
                raise ProfileError(f"{side} draft entry is incomplete") from exc
            if isinstance(hero_id, bool) or not isinstance(hero_id, int) or hero_id <= 0:
                raise ProfileError("hero_id must be a positive integer")
            if isinstance(position, bool) or not isinstance(position, int) or position not in range(1, 6):
                raise ProfileError("position_id must be in 1..5")
            heroes.append(hero_id)
            positions.append(position)
        if len(set(heroes)) != 5 or len(set(positions)) != 5 or set(positions) != set(range(1, 6)):
            raise ProfileError(f"{side} heroes and positions must be unique and cover 1..5")
        all_ids.extend(heroes)
    if len(set(all_ids)) != 10:
        raise ProfileError("heroes may not be repeated across sides")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProfileError("request_started_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def utc_elapsed_days(date_time: int, now: datetime) -> int:
    if isinstance(date_time, bool) or not isinstance(date_time, int):
        raise ProfileError("date_time must be a Unix UTC integer")
    current = _utc(now).timestamp()
    elapsed = current - date_time
    if elapsed < 0:
        raise ProfileError("future date_time is not supported")
    return math.floor(elapsed / 86400)


def _profile_projection() -> dict[str, Any]:
    order = ("GetMatchPicksBans", "HeroesMetaPositions", "GetMatchCountPreviousWeekDay", "Synergy", "GetHeroStatsByTime", "GetHeroStatsByTime")
    variables = (
        {"matchId": "computed:match_id"},
        {"bracketIds": ["fixed:IMMORTAL"], "take": 7, "skip": "computed:elapsed_days", "heroIds": "mode_specific:variables_by_mode"},
        {"bracketIds": ["fixed:IMMORTAL"]},
        {"bracketBasicIds": "fixed:DIVINE_IMMORTAL", "matchLimit": 0, "take": 200, "heroIds": "mode_specific:variables_by_mode"},
        {"week": "computed:date_time", "bracketBasicIds": "omitted"},
        {"bracketBasicIds": "fixed:DIVINE_IMMORTAL", "week": "computed:date_time"},
    )
    operations = []
    for index, name in enumerate(order):
        query = _QUERY_DOCUMENTS[name]
        operations.append({
            "index": index,
            "operation_name": name,
            "query": query,
            "query_bytes_sha256": _sha256(query.encode("utf-8")),
            "variables": variables[index],
            "null_variables": [],
            "omitted_variables": [key for key, value in variables[index].items() if value == "omitted"],
        })
    return {
        "schema": "stratz-request-profile-artifact/v1",
        "profile_id": PROFILE_ID,
        "formula_version": FORMULA_VERSION,
        "endpoint": ENDPOINT,
        "serialization_version": SERIALIZATION_VERSION,
        "input_modes": ["historical_match", "explicit_draft"],
        "bracket_ids": ["IMMORTAL"],
        "operation_order": list(order),
        "operations": operations,
        "operation_sequences": {
            "historical_match": list(order),
            "explicit_draft": list(order[1:]),
        },
        "variables_by_mode": {
            "historical_match": [
                {"operation_name": name, "heroIds": "omitted" if name in {"HeroesMetaPositions", "Synergy"} else "not_applicable"}
                for name in order
            ],
            "explicit_draft": [
                {"operation_name": name, "heroIds": "computed:canonical_radiant_position_1_to_5_then_dire_position_1_to_5" if name in {"HeroesMetaPositions", "Synergy"} else "not_applicable"}
                for name in order[1:]
            ],
        },
        "planning": {
            "elapsed_day": "floor((utc_now - date_time) / 86400), full_24h, reject_future",
            "week_anchor": "date_time - (index + current_day_shift) * 604800",
            "current_day_shift": "1 when UTC calendar date(date_time) == UTC calendar date(now), else 0",
            "metadata_over_25_days": "fail_closed: exact weekly query is outside this frozen capture",
        },
        "fixed_variables": {"take": 7, "matchLimit": 0, "synergy_take": 200},
        "scorer_identity": {"status": "unactivated"},
    }


REQUEST_PROFILE_ARTIFACT = _freeze(_profile_projection())
REQUEST_PROFILE_HASH = _sha256(canonical_bytes(REQUEST_PROFILE_ARTIFACT))

def _v2_profile_projection() -> dict[str, Any]:
    projection = _thaw(REQUEST_PROFILE_ARTIFACT)
    projection["schema"] = "stratz-request-profile-artifact/v2"
    projection["profile_id"] = V2_PROFILE_ID
    projection["formula_version"] = V2_FORMULA_VERSION
    projection["request_hash_projection"] = [
        "index",
        "operation_name",
        "query",
        "query_sha256",
        "variables",
    ]
    projection["scorer_identity"] = {
        "status": V2_STATE,
        "sha256": SCORER_SOURCE_HASH,
    }
    return projection


V2_REQUEST_PROFILE_ARTIFACT = _freeze(_v2_profile_projection())
V2_REQUEST_PROFILE_HASH = _sha256(canonical_bytes(V2_REQUEST_PROFILE_ARTIFACT))


def _profile_identity_projection(profile: RoshParityProfile) -> dict[str, Any]:
    """Return the complete v2 identity, excluding its self-hash field."""
    return {
        "schema": "stratz-rosh-profile-identity/v2",
        "state": profile.state,
        "rosh_profile_id": profile.rosh_profile_id,
        "formula_version": profile.formula_version,
        "request_profile_hash": profile.request_profile_hash,
        "upstream_bundle_hash": profile.upstream_bundle_hash,
        "scorer_source_hash": profile.scorer_source_hash,
        "serialization_version": profile.serialization_version,
        "endpoint": ENDPOINT,
        "presentation_rounding": PRESENTATION_ROUNDING,
        "scorer_thresholds": {
            "position_reliability_count": 1000,
            "synergy_reliability_count": 100,
            "time_rank_fallback_count": 1000,
        },
        "captured_artifacts": dict(FROZEN_ARTIFACT_HASHES),
    }


_V2_WITHOUT_CANONICAL_HASH = RoshParityProfile(
    V2_PROFILE_ID,
    V2_FORMULA_VERSION,
    V2_REQUEST_PROFILE_HASH,
    UPSTREAM_BUNDLE_HASH,
    SERIALIZATION_VERSION,
    SCORER_SOURCE_HASH,
    "",
    V2_STATE,
)
CANONICAL_PROFILE_HASH = _sha256(
    canonical_bytes(_profile_identity_projection(_V2_WITHOUT_CANONICAL_HASH))
)

V1_PROFILE = RoshParityProfile(
    PROFILE_ID,
    FORMULA_VERSION,
    REQUEST_PROFILE_HASH,
    UPSTREAM_BUNDLE_HASH,
    SERIALIZATION_VERSION,
    state=V1_STATE,
)
V2_PROFILE = RoshParityProfile(
    V2_PROFILE_ID,
    V2_FORMULA_VERSION,
    V2_REQUEST_PROFILE_HASH,
    UPSTREAM_BUNDLE_HASH,
    SERIALIZATION_VERSION,
    SCORER_SOURCE_HASH,
    CANONICAL_PROFILE_HASH,
    V2_STATE,
)

PROFILE_REGISTRY = MappingProxyType({
    PROFILE_ID: V1_PROFILE,
    V2_PROFILE_ID: V2_PROFILE,
})


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_active_profile_identity(profile: RoshParityProfile) -> None:
    if not isinstance(profile, RoshParityProfile):
        raise ProfileError("profile must be a RoshParityProfile")
    registered = PROFILE_REGISTRY.get(V2_PROFILE_ID)
    if registered is None or profile != registered:
        raise ProfileError("profile is not the registered active v2 identity")
    if (
        profile.rosh_profile_id != V2_PROFILE_ID
        or profile.formula_version != V2_FORMULA_VERSION
    ):
        raise ProfileError("active v2 identity drift")
    if profile.state != V2_STATE:
        raise ProfileError("profile is not active")
    hashes = (
        profile.request_profile_hash,
        profile.upstream_bundle_hash,
        profile.scorer_source_hash,
        profile.canonical_profile_hash,
    )
    if not all(_valid_sha256(value) for value in hashes):
        raise ProfileError("active profile contains an incomplete hash")
    if profile.request_profile_hash != _sha256(canonical_bytes(V2_REQUEST_PROFILE_ARTIFACT)):
        raise ProfileError("request profile hash drift")
    scorer_path = Path(__file__).with_name("stratz_official_score.py")
    if profile.scorer_source_hash != _sha256(scorer_path.read_bytes()):
        raise ProfileError("scorer source hash drift")
    expected_canonical_hash = _sha256(canonical_bytes(_profile_identity_projection(profile)))
    if profile.canonical_profile_hash != expected_canonical_hash:
        raise ProfileError("canonical profile hash drift")
    if profile.upstream_bundle_hash != UPSTREAM_BUNDLE_HASH:
        raise ProfileError("upstream bundle hash drift")
    if profile.serialization_version != SERIALIZATION_VERSION:
        raise ProfileError("serialization identity drift")


def validate_active_profile(profile: RoshParityProfile) -> None:
    """Reject every identity except the complete, source-bound active v2."""
    _validate_active_profile_identity(profile)


def get_profile(profile_id: str | None = None) -> RoshParityProfile:
    requested_id = ACTIVE_PROFILE_ID if profile_id is None else profile_id
    profile = PROFILE_REGISTRY.get(requested_id)
    if profile is None or not profile.request_profile_hash:
        raise ProfileError("profile is missing or unregistered")
    if profile.rosh_profile_id != requested_id:
        raise ProfileError("profile registry key drift")
    if requested_id == V2_PROFILE_ID:
        _validate_active_profile_identity(profile)
        return profile
    if requested_id != PROFILE_ID or profile != V1_PROFILE:
        raise ProfileError("profile identity drift")
    if profile.request_profile_hash != _sha256(canonical_bytes(REQUEST_PROFILE_ARTIFACT)):
        raise ProfileError("request profile hash drift")
    return profile


def _make_operation(index: int, name: str, variables: Mapping[str, Any], query: str | None = None) -> RequestOperation:
    document = _QUERY_DOCUMENTS[name] if query is None else query
    return RequestOperation(index, name, document, MappingProxyType(dict(variables)), _sha256(document.encode("utf-8")))


def _synergy_query(anchors: Sequence[int]) -> str:
    if len(anchors) != 4 or len(set(anchors)) != 4:
        raise ProfileError("synergy requires four distinct week anchors")
    query = _QUERY_DOCUMENTS["Synergy"]
    cursor = 0
    pieces: list[str] = []
    for index, match in enumerate(re.finditer(r"(?<=week: )\d+", query)):
        pieces.append(query[cursor : match.start()])
        pieces.append(str(anchors[index]))
        cursor = match.end()
    pieces.append(query[cursor:])
    if len(pieces) != 9:
        raise ProfileError("unexpected synergy query shape")
    return "".join(pieces)


def build_official_request_plan(
    analysis_input: RoshAnalysisInput | Mapping[str, Any],
    profile: RoshParityProfile | None = None,
    *,
    request_started_at: datetime,
) -> RoshRequestPlan:
    profile = profile or get_profile()
    _validate_active_profile_identity(profile)
    value = RoshAnalysisInput.from_value(analysis_input)
    if value.mode not in {"historical_match", "explicit_draft"}:
        raise ProfileError("mode must be historical_match or explicit_draft")
    if isinstance(value.date_time, bool) or not isinstance(value.date_time, int) or value.date_time <= 0:
        raise ProfileError("date_time must be positive")
    if tuple(value.bracket_ids) != ALLOWED_BRACKETS:
        raise ProfileError("only IMMORTAL bracket is supported")
    if value.mode == "historical_match":
        if isinstance(value.match_id, bool) or not isinstance(value.match_id, int) or value.match_id <= 0:
            raise ProfileError("historical_match requires a positive match_id")
        if value.radiant or value.dire:
            raise ProfileError("historical_match cannot include draft rows")
    else:
        if value.match_id is not None:
            raise ProfileError("explicit_draft must not include match_id")
        validate_draft(value.radiant, value.dire)
    started = _utc(request_started_at)
    elapsed = utc_elapsed_days(value.date_time, started)
    if elapsed > MAX_HEROES_META_SKIP:
        raise ProfileError("date_time older than 25 days requires an unfrozen weekly query")
    metadata_mode = "daily"
    same_day = datetime.fromtimestamp(value.date_time, timezone.utc).date() == started.date()
    shift = 1 if same_day else 0
    anchors = tuple(value.date_time - (index + shift) * WEEK_SECONDS for index in range(4))
    hero_ids = [int(row.get("hero_id", row.get("heroId"))) for row in (*value.radiant, *value.dire)] if value.mode == "explicit_draft" else None
    operations: list[RequestOperation] = []
    index = 0
    if value.mode == "historical_match":
        operations.append(
            _make_operation(index, "GetMatchPicksBans", {"matchId": value.match_id})
        )
        index += 1
    meta_variables = {
        "bracketIds": ["IMMORTAL"],
        "take": 7,
        "skip": elapsed,
        **({"heroIds": hero_ids} if hero_ids is not None else {}),
    }
    operations.append(_make_operation(index, "HeroesMetaPositions", meta_variables))
    index += 1
    operations.append(
        _make_operation(
            index,
            "GetMatchCountPreviousWeekDay",
            {"bracketIds": ["IMMORTAL"]},
        )
    )
    index += 1
    synergy_variables = {
        "bracketBasicIds": "DIVINE_IMMORTAL",
        "matchLimit": 0,
        "take": 200,
        **({"heroIds": hero_ids} if hero_ids is not None else {}),
    }
    operations.append(
        _make_operation(
            index,
            "Synergy",
            synergy_variables,
            _synergy_query(anchors),
        )
    )
    index += 1
    operations.append(
        _make_operation(index, "GetHeroStatsByTime", {"week": value.date_time})
    )
    index += 1
    operations.append(
        _make_operation(
            index,
            "GetHeroStatsByTime",
            {
                "bracketBasicIds": "DIVINE_IMMORTAL",
                "week": value.date_time,
            },
        )
    )
    operation_tuple = tuple(operations)
    request_hash = _sha256(canonical_bytes(_request_projection(operation_tuple)))
    return RoshRequestPlan(
        profile,
        value,
        operation_tuple,
        elapsed,
        metadata_mode,
        shift,
        anchors,
        request_hash,
        started,
    )


def _request_projection(operations: Sequence[RequestOperation]) -> dict[str, Any]:
    return {
        "endpoint": ENDPOINT,
        "operations": [
            {
                "index": operation.index,
                "operation_name": operation.operation_name,
                "query": operation.query,
                "query_sha256": operation.query_sha256,
                "variables": dict(operation.variables),
            }
            for operation in operations
        ],
    }


def compute_request_hash(plan: RoshRequestPlan) -> str:
    return _sha256(canonical_bytes(_request_projection(plan.operations)))


def validate_canonical_request_plan(plan: RoshRequestPlan) -> None:
    """Rebuild and compare the complete active v2 request plan."""
    if not isinstance(plan, RoshRequestPlan):
        raise ProfileError("plan must be a RoshRequestPlan")
    validate_active_profile(plan.profile)
    if plan.request_started_at is None:
        raise ProfileError("plan is missing its frozen request_started_at")
    started = _utc(plan.request_started_at)
    rebuilt = build_official_request_plan(
        plan.analysis_input,
        profile=plan.profile,
        request_started_at=started,
    )
    if plan.request_started_at != started or plan != rebuilt:
        raise ProfileError("request plan drift")
    for operation in plan.operations:
        actual_query_hash = _sha256(operation.query.encode("utf-8"))
        if operation.query_sha256 != actual_query_hash:
            raise ProfileError("query hash drift")
    if plan.request_hash != compute_request_hash(plan):
        raise ProfileError("request hash drift")
