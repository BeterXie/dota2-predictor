"""Canonical identities for exact RayBet response storage.

This module is deliberately independent of SQLite.  Online writers, offline
compaction, and audit code must use these functions so a response has one
identity regardless of which path persisted it.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, TypeAlias

from .sanitize import sanitize_raybet_payload


MAX_RESPONSE_ARTIFACT_BYTES = 16 * 1024 * 1024
LEGACY_NORMALIZED_STATE_HASH_VERSION = 1
NORMALIZED_STATE_HASH_VERSION = 2
LEGACY_NORMALIZED_STATE_FORMAT = "normalized-odds-state-v1"
NORMALIZED_STATE_FORMAT = "normalized-odds-state-v2"
RESPONSE_STATE_FORMAT = "odds-response-state-v2"
SNAPSHOT_DERIVED_FORMAT = "snapshot-derived-v1"

ResponseStateOutcome: TypeAlias = tuple[
    str,
    str | None,
    float,
    str | None,
    str,
    str,
    str | None,
    float | None,
    str,
    int,
    str | None,
]


class ResponseArtifactLimitError(ValueError):
    """A sanitized response exceeds the bounded raw-artifact size."""


def _content_hash(domain: str, payload: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + payload).hexdigest()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_state_outcomes(
    outcomes: Iterable[Sequence[Any]],
) -> tuple[ResponseStateOutcome, ...]:
    """Validate and order the exact semantic members of one response."""

    canonical: list[ResponseStateOutcome] = []
    seen_odds_ids: set[str] = set()
    for raw in outcomes:
        if len(raw) != 11:
            raise ValueError("response state outcome must have 11 fields")
        odds_id = raw[0]
        if not isinstance(odds_id, str) or not odds_id:
            raise ValueError("response state odds id must be non-empty text")
        if odds_id in seen_odds_ids:
            raise ValueError("duplicate odds id in one response")
        seen_odds_ids.add(odds_id)

        price = raw[2]
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError("response outcome price must be numeric")
        price = float(price)
        if not math.isfinite(price):
            raise ValueError("response outcome price must be finite")
        line = raw[7]
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, (int, float)):
                raise ValueError("response outcome line must be numeric or null")
            line = float(line)
            if not math.isfinite(line):
                raise ValueError("response outcome line must be finite")
        supported = raw[9]
        if isinstance(supported, bool) or supported not in (0, 1):
            raise ValueError("response outcome supported must be 0 or 1")
        for index, label in ((4, "market type"), (5, "period")):
            if not isinstance(raw[index], str) or not raw[index]:
                raise ValueError(f"response outcome {label} must be non-empty text")
        if not isinstance(raw[8], str) or (supported == 1 and not raw[8]):
            raise ValueError(
                "supported response outcome key must be non-empty text"
            )
        for index, label in (
            (1, "odds group id"),
            (3, "status"),
            (6, "side"),
            (10, "last update"),
        ):
            if raw[index] is not None and not isinstance(raw[index], str):
                raise ValueError(f"response outcome {label} must be text or null")

        canonical.append(
            (
                odds_id,
                raw[1],
                price,
                raw[3],
                raw[4],
                raw[5],
                raw[6],
                line,
                raw[8],
                int(supported),
                raw[10],
            )
        )
    canonical.sort(key=lambda row: row[0])
    return tuple(canonical)


def normalized_state_identity(
    outcomes: Iterable[Sequence[Any]],
) -> tuple[str, tuple[ResponseStateOutcome, ...], bytes]:
    """Return the v2 semantic hash, ordered members, and canonical manifest."""

    ordered = canonical_state_outcomes(outcomes)
    manifest = json.dumps(
        {
            "format": NORMALIZED_STATE_FORMAT,
            "outcomes": ordered,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(manifest), ordered, manifest


def legacy_normalized_state_identity_v1(
    outcomes: Iterable[Sequence[Any]],
) -> tuple[str, tuple[ResponseStateOutcome, ...], bytes]:
    """Reproduce the historical subset hash without presenting it as v2."""

    ordered = canonical_state_outcomes(outcomes)
    legacy = sorted(
        (row[0], row[2], str(row[3]), row[10])
        for row in ordered
    )
    manifest = json.dumps(
        legacy,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(manifest), ordered, manifest


def response_state_identity(
    raybet_match_id: str,
    normalized_state_hash: str,
    outcomes: Iterable[Sequence[Any]],
) -> tuple[str, tuple[ResponseStateOutcome, ...], bytes]:
    """Return the content hash, ordered members, and canonical manifest bytes."""

    if not isinstance(raybet_match_id, str) or not raybet_match_id:
        raise ValueError("RayBet match id must be non-empty text")
    if (
        not isinstance(normalized_state_hash, str)
        or len(normalized_state_hash) != 64
    ):
        raise ValueError("normalized state hash must be 64 characters")
    computed_normalized_hash, ordered, _ = normalized_state_identity(outcomes)
    if computed_normalized_hash != normalized_state_hash:
        raise ValueError(
            "normalized state hash does not match the canonical v2 manifest"
        )
    manifest = json.dumps(
        {
            "format": RESPONSE_STATE_FORMAT,
            "raybet_match_id": raybet_match_id,
            "normalized_state_hash": normalized_state_hash,
            "outcomes": ordered,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _content_hash(RESPONSE_STATE_FORMAT, manifest), ordered, manifest


def legacy_response_state_identity_v1(
    raybet_match_id: str,
    normalized_state_hash: str,
    outcomes: Iterable[Sequence[Any]],
) -> tuple[str, tuple[ResponseStateOutcome, ...], bytes]:
    """Reproduce an existing response-state identity that contains a v1 hash."""

    computed_legacy_hash, ordered, _ = legacy_normalized_state_identity_v1(outcomes)
    if computed_legacy_hash != normalized_state_hash:
        raise ValueError(
            "legacy normalized state hash does not match the canonical v1 manifest"
        )
    if not isinstance(raybet_match_id, str) or not raybet_match_id:
        raise ValueError("RayBet match id must be non-empty text")
    manifest = json.dumps(
        {
            "format": RESPONSE_STATE_FORMAT,
            "raybet_match_id": raybet_match_id,
            "normalized_state_hash": normalized_state_hash,
            "outcomes": ordered,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _content_hash(RESPONSE_STATE_FORMAT, manifest), ordered, manifest


def snapshot_derived_payload(
    raybet_match_id: str,
    raw_outcomes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the partial raw envelope retained for a legacy observation."""

    if not isinstance(raybet_match_id, str) or not raybet_match_id:
        raise ValueError("RayBet match id must be non-empty text")
    members: list[Mapping[str, Any]] = []
    for raw in raw_outcomes:
        if not isinstance(raw, Mapping):
            raise ValueError("snapshot-derived outcome must be an object")
        members.append(raw)
    return {
        "artifact_version": SNAPSHOT_DERIVED_FORMAT,
        "result": {"id": raybet_match_id, "odds": members},
    }


def response_artifact_identity(payload: Any) -> tuple[str, bytes, Any]:
    """Sanitize and hash one exact response artifact."""

    sanitized = sanitize_raybet_payload(payload)
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_ARTIFACT_BYTES:
        raise ResponseArtifactLimitError(
            "response artifact exceeds storage limit"
        )
    return hashlib.sha256(encoded).hexdigest(), encoded, sanitized


__all__ = [
    "LEGACY_NORMALIZED_STATE_FORMAT",
    "LEGACY_NORMALIZED_STATE_HASH_VERSION",
    "MAX_RESPONSE_ARTIFACT_BYTES",
    "NORMALIZED_STATE_FORMAT",
    "NORMALIZED_STATE_HASH_VERSION",
    "RESPONSE_STATE_FORMAT",
    "ResponseArtifactLimitError",
    "ResponseStateOutcome",
    "canonical_state_outcomes",
    "legacy_normalized_state_identity_v1",
    "legacy_response_state_identity_v1",
    "normalized_state_identity",
    "response_artifact_identity",
    "response_state_identity",
    "snapshot_derived_payload",
]
