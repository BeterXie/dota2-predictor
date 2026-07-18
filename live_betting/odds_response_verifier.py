"""Read-only verification for persisted odds response authority."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_intelligence.raw_archive import (
    canonical_json_value_bytes,
    schema_fingerprint,
)

from .markets import snapshots_from_payload, snapshot_state_outcome
from .odds_response_authority import (
    LEGACY_NORMALIZED_STATE_HASH_VERSION,
    NORMALIZED_STATE_HASH_VERSION,
    SNAPSHOT_DERIVED_FORMAT,
    ResponseStateOutcome,
    canonical_state_outcomes,
    legacy_normalized_state_identity_v1,
    legacy_response_state_identity_v1,
    normalized_state_identity,
    response_state_identity,
)


@dataclass(frozen=True)
class OddsResponseAuthorityVerification:
    state_count: int
    transport_count: int
    artifact_count: int
    legacy_transport_count: int


@dataclass(frozen=True)
class _StateAuthority:
    raybet_match_id: str
    normalized_state_hash: str
    normalized_state_hash_version: int
    original_legacy_normalized_state_hash: str | None
    outcomes: tuple[ResponseStateOutcome, ...]


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _state_groups(
    connection: sqlite3.Connection,
) -> Iterator[tuple[tuple[Any, ...], tuple[Sequence[Any], ...]]]:
    cursor = connection.execute(
        """SELECT state.response_state_hash, state.raybet_match_id,
                  state.normalized_state_hash,
                  state.normalized_state_hash_version,
                  state.original_legacy_normalized_state_hash,
                  state.outcome_count,
                  outcome.odds_id, outcome.odds_group_id, outcome.price,
                  outcome.status, outcome.market_type, outcome.period,
                  outcome.side, outcome.line, outcome.outcome_key,
                  outcome.supported, outcome.last_update
             FROM odds_response_states AS state
             LEFT JOIN odds_response_state_outcomes AS outcome
               ON outcome.response_state_hash=state.response_state_hash
            ORDER BY state.response_state_hash, outcome.odds_id"""
    )
    current_hash: str | None = None
    metadata: tuple[Any, ...] | None = None
    outcomes: list[Sequence[Any]] = []
    for row in cursor:
        state_hash = str(row[0])
        if current_hash is not None and state_hash != current_hash:
            assert metadata is not None
            yield metadata, tuple(outcomes)
            outcomes = []
        if state_hash != current_hash:
            current_hash = state_hash
            metadata = tuple(row[:6])
        if row[6] is not None:
            outcomes.append(tuple(row[6:17]))
    if current_hash is not None:
        assert metadata is not None
        yield metadata, tuple(outcomes)


def _state_authority(
    metadata: Sequence[Any],
    raw_outcomes: Sequence[Sequence[Any]],
) -> _StateAuthority:
    (
        raw_state_hash,
        raw_match_id,
        raw_normalized_hash,
        raw_version,
        raw_original_legacy_hash,
        raw_outcome_count,
    ) = metadata
    state_hash = str(raw_state_hash)
    match_id = str(raw_match_id)
    normalized_hash = str(raw_normalized_hash)
    version = int(raw_version)
    original_legacy_hash = (
        None if raw_original_legacy_hash is None else str(raw_original_legacy_hash)
    )
    outcomes = canonical_state_outcomes(raw_outcomes)
    if len(outcomes) != int(raw_outcome_count):
        raise RuntimeError(f"odds response state outcome count mismatch: {state_hash}")
    if version == NORMALIZED_STATE_HASH_VERSION:
        computed_normalized, _, _ = normalized_state_identity(outcomes)
        if computed_normalized != normalized_hash:
            raise RuntimeError(
                f"odds response state v2 normalized hash mismatch: {state_hash}"
            )
        computed_state, _, _ = response_state_identity(
            match_id, normalized_hash, outcomes
        )
        if original_legacy_hash is not None:
            computed_legacy, _, _ = legacy_normalized_state_identity_v1(outcomes)
            if computed_legacy != original_legacy_hash:
                raise RuntimeError(
                    f"odds response state preserved v1 hash mismatch: {state_hash}"
                )
    elif version == LEGACY_NORMALIZED_STATE_HASH_VERSION:
        if original_legacy_hash is not None:
            raise RuntimeError(
                f"legacy odds response state has a duplicate preserved hash: {state_hash}"
            )
        computed_normalized, _, _ = legacy_normalized_state_identity_v1(outcomes)
        if computed_normalized != normalized_hash:
            raise RuntimeError(
                f"odds response state v1 normalized hash mismatch: {state_hash}"
            )
        computed_state, _, _ = legacy_response_state_identity_v1(
            match_id, normalized_hash, outcomes
        )
    else:
        raise RuntimeError(f"odds response state hash version is invalid: {state_hash}")
    if computed_state != state_hash:
        raise RuntimeError(f"odds response state hash mismatch: {state_hash}")
    return _StateAuthority(
        match_id,
        normalized_hash,
        version,
        original_legacy_hash,
        outcomes,
    )


def _verify_states(connection: sqlite3.Connection) -> int:
    state_count = 0
    for metadata, raw_outcomes in _state_groups(connection):
        _state_authority(metadata, raw_outcomes)
        state_count += 1
    duplicate = connection.execute(
        """SELECT 1
             FROM odds_response_states
            WHERE normalized_state_hash_version=?
            GROUP BY raybet_match_id, normalized_state_hash
           HAVING COUNT(*)>1
            LIMIT 1""",
        (NORMALIZED_STATE_HASH_VERSION,),
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError(
            "v2 normalized state hash maps to multiple response manifests"
        )
    return state_count


def _transport_groups(
    connection: sqlite3.Connection,
) -> Iterator[tuple[tuple[Any, ...], tuple[tuple[Any, ...], ...]]]:
    cursor = connection.execute(
        """SELECT transport.observation_key, transport.raybet_match_id,
                  transport.observed_at, transport.normalized_state_hash,
                  transport.normalized_state_hash_version,
                  transport.original_legacy_normalized_state_hash,
                  transport.response_state_hash,
                  transport.response_artifact_hash,
                  state.response_state_hash, state.raybet_match_id,
                  state.normalized_state_hash,
                  state.normalized_state_hash_version,
                  state.original_legacy_normalized_state_hash,
                  legacy.raybet_match_id, legacy.received_at, legacy.odds_id,
                  legacy.odds_group_id, legacy.price, legacy.status,
                  legacy.market_type, legacy.period, legacy.side, legacy.line,
                  legacy.outcome_key, legacy.supported, legacy.last_update,
                  legacy.raw_json
             FROM odds_transport_observations AS transport
             LEFT JOIN odds_response_states AS state
               ON state.response_state_hash=transport.response_state_hash
             LEFT JOIN odds_response_outcomes AS legacy
               ON legacy.observation_key=transport.observation_key
              AND transport.response_state_hash IS NULL
              AND transport.response_artifact_hash IS NULL
            ORDER BY transport.observation_key, legacy.odds_id"""
    )
    current_key: str | None = None
    metadata: tuple[Any, ...] | None = None
    legacy_rows: list[tuple[Any, ...]] = []
    for row in cursor:
        observation_key = str(row[0])
        if current_key is not None and observation_key != current_key:
            assert metadata is not None
            yield metadata, tuple(legacy_rows)
            legacy_rows = []
        if observation_key != current_key:
            current_key = observation_key
            metadata = tuple(row[:13])
        if row[13] is not None:
            legacy_rows.append(tuple(row[13:27]))
    if current_key is not None:
        assert metadata is not None
        yield metadata, tuple(legacy_rows)


def _verify_legacy_transport(
    observation_key: str,
    match_id: str,
    observed_at: str,
    normalized_hash: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    outcomes: list[Sequence[Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        row_match_id, received_at = str(row[0]), str(row[1])
        if row_match_id != match_id or received_at != observed_at:
            raise RuntimeError(
                f"legacy odds transport outcome binding mismatch: {observation_key}"
            )
        odds_id = str(row[2])
        if odds_id in seen_ids:
            raise RuntimeError(f"legacy odds transport has duplicate members: {observation_key}")
        seen_ids.add(odds_id)
        try:
            raw = json.loads(str(row[13]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"legacy odds transport raw member is invalid: {observation_key}"
            ) from error
        if not isinstance(raw, dict) or str(
            raw.get("odds_id") or raw.get("id") or ""
        ) != odds_id:
            raise RuntimeError(
                f"legacy odds transport raw member id mismatch: {observation_key}"
            )
        outcomes.append(tuple(row[2:13]))
    computed, _, _ = legacy_normalized_state_identity_v1(outcomes)
    if computed != normalized_hash:
        raise RuntimeError(f"legacy odds transport normalized hash mismatch: {observation_key}")


def _verify_transports(
    connection: sqlite3.Connection,
) -> tuple[int, int]:
    transport_count = 0
    legacy_count = 0
    for row, legacy_rows in _transport_groups(connection):
        transport_count += 1
        observation_key = str(row[0])
        match_id = str(row[1])
        observed_at = str(row[2])
        normalized_hash = str(row[3])
        version = int(row[4])
        original_legacy_hash = None if row[5] is None else str(row[5])
        state_hash = None if row[6] is None else str(row[6])
        artifact_hash = None if row[7] is None else str(row[7])
        if (state_hash is None) != (artifact_hash is None):
            raise RuntimeError(
                f"odds transport has partial response authority: {observation_key}"
            )
        if state_hash is None:
            legacy_count += 1
            if (
                version != LEGACY_NORMALIZED_STATE_HASH_VERSION
                or original_legacy_hash is not None
            ):
                raise RuntimeError(
                    f"legacy odds transport hash version is invalid: {observation_key}"
                )
            _verify_legacy_transport(
                observation_key,
                match_id,
                observed_at,
                normalized_hash,
                legacy_rows,
            )
            continue
        if row[8] is None:
            raise RuntimeError(f"odds transport state is missing: {observation_key}")
        if (
            str(row[9]) != match_id
            or str(row[10]) != normalized_hash
            or int(row[11]) != version
            or (None if row[12] is None else str(row[12]))
            != original_legacy_hash
        ):
            raise RuntimeError(f"odds transport state binding mismatch: {observation_key}")
    return transport_count, legacy_count


def _artifact_state_groups(
    connection: sqlite3.Connection,
) -> Iterator[tuple[str, _StateAuthority]]:
    # SQLite may spill this distinct-pair grouping to its temporary store; the
    # Python verifier retains only one state manifest at a time.
    cursor = connection.execute(
        """WITH artifact_states AS (
               SELECT response_artifact_hash AS artifact_hash,
                      response_state_hash AS state_hash,
                      MIN(observation_key) AS observation_key
                 FROM odds_transport_observations
                WHERE response_artifact_hash IS NOT NULL
                  AND response_state_hash IS NOT NULL
                GROUP BY response_artifact_hash, response_state_hash
           )
           SELECT mapping.artifact_hash, mapping.state_hash,
                  mapping.observation_key, state.response_state_hash,
                  state.raybet_match_id, state.normalized_state_hash,
                  state.normalized_state_hash_version,
                  state.original_legacy_normalized_state_hash,
                  state.outcome_count, outcome.odds_id,
                  outcome.odds_group_id, outcome.price, outcome.status,
                  outcome.market_type, outcome.period, outcome.side,
                  outcome.line, outcome.outcome_key, outcome.supported,
                  outcome.last_update
             FROM artifact_states AS mapping
             LEFT JOIN odds_response_states AS state
               ON state.response_state_hash=mapping.state_hash
             LEFT JOIN odds_response_state_outcomes AS outcome
               ON outcome.response_state_hash=state.response_state_hash
            ORDER BY mapping.artifact_hash, mapping.state_hash,
                     outcome.odds_id"""
    )
    current: tuple[str, str] | None = None
    metadata: tuple[Any, ...] | None = None
    outcomes: list[tuple[Any, ...]] = []
    for row in cursor:
        if row[3] is None:
            raise RuntimeError(f"odds transport state is missing: {row[2]}")
        key = (str(row[0]), str(row[1]))
        if current is not None and key != current:
            assert metadata is not None
            yield current[0], _state_authority(metadata, tuple(outcomes))
            outcomes = []
        if key != current:
            current = key
            metadata = tuple(row[3:9])
        if row[9] is not None:
            outcomes.append(tuple(row[9:20]))
    if current is not None:
        assert metadata is not None
        yield current[0], _state_authority(metadata, tuple(outcomes))


def _artifact_path(root: Path, artifact_hash: str, storage_path: str) -> Path:
    relative = Path(storage_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"odds raw artifact path is unsafe: {artifact_hash}")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f"odds raw artifact path escapes root: {artifact_hash}") from error
    if path.name != f"{artifact_hash}.json.gz" or path.is_symlink() or not path.is_file():
        raise RuntimeError(f"odds raw artifact is missing or unsafe: {artifact_hash}")
    return path


def _verify_snapshot_derived_artifact(
    payload: dict[str, Any],
    states: Sequence[_StateAuthority],
    artifact_hash: str,
) -> None:
    result = payload.get("result")
    members = result.get("odds") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not isinstance(members, list):
        raise RuntimeError(f"legacy odds raw artifact envelope is invalid: {artifact_hash}")
    match_id = str(result.get("id") or "")
    by_id: dict[str, dict[str, Any]] = {}
    for member in members:
        if not isinstance(member, dict):
            raise RuntimeError(f"legacy odds raw artifact member is invalid: {artifact_hash}")
        odds_id = str(member.get("odds_id") or member.get("id") or "")
        if not odds_id or odds_id in by_id:
            raise RuntimeError(f"legacy odds raw artifact member id is invalid: {artifact_hash}")
        by_id[odds_id] = member
    for state in states:
        if (
            state.normalized_state_hash_version != NORMALIZED_STATE_HASH_VERSION
            or state.original_legacy_normalized_state_hash is None
            or match_id != state.raybet_match_id
            or set(by_id) != {row[0] for row in state.outcomes}
        ):
            raise RuntimeError(
                f"legacy odds raw artifact authority binding mismatch: {artifact_hash}"
            )
        for outcome in state.outcomes:
            member = by_id[outcome[0]]
            try:
                price = float(member.get("odds"))
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    f"legacy odds raw artifact price is invalid: {artifact_hash}"
                ) from error
            if not math.isfinite(price) or price != outcome[2]:
                raise RuntimeError(
                    f"legacy odds raw artifact v1 membership mismatch: {artifact_hash}"
                )
            status = member.get("status")
            normalized_status = None if status is None else str(status)
            last_update = str(member.get("last_update") or "") or None
            if normalized_status != outcome[3] or last_update != outcome[10]:
                raise RuntimeError(
                    f"legacy odds raw artifact v1 membership mismatch: {artifact_hash}"
                )


def _verify_full_artifact(
    payload: dict[str, Any],
    states: Sequence[_StateAuthority],
    artifact_hash: str,
) -> None:
    result = payload.get("result")
    members = result.get("odds") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not isinstance(members, list):
        raise RuntimeError(f"odds raw response envelope is invalid: {artifact_hash}")
    received_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    snapshots = snapshots_from_payload(payload, received_at=received_at)
    if len(snapshots) != len(members):
        raise RuntimeError(f"odds raw response contains unparsed members: {artifact_hash}")
    outcomes = canonical_state_outcomes(snapshot_state_outcome(row) for row in snapshots)
    match_id = str(result.get("id") or "")
    for state in states:
        if match_id != state.raybet_match_id or outcomes != state.outcomes:
            raise RuntimeError(f"odds raw response state membership mismatch: {artifact_hash}")


def _verify_artifacts(
    connection: sqlite3.Connection,
    raw_archive_root: Path,
) -> int:
    state_groups = iter(_artifact_state_groups(connection))
    pending = next(state_groups, None)
    artifact_count = 0
    for row in connection.execute(
        """SELECT artifact_hash, storage_path, uncompressed_bytes,
                  compressed_bytes, schema_fingerprint
             FROM odds_raw_artifacts ORDER BY artifact_hash"""
    ):
        artifact_count += 1
        artifact_hash = str(row[0])
        if pending is not None and pending[0] < artifact_hash:
            raise RuntimeError(
                f"odds transport raw artifact is missing: {pending[0]}"
            )
        path = _artifact_path(raw_archive_root, artifact_hash, str(row[1]))
        compressed = path.read_bytes()
        if len(compressed) != int(row[3]):
            raise RuntimeError(f"odds raw artifact compressed size mismatch: {artifact_hash}")
        try:
            canonical = gzip.decompress(compressed)
            payload = json.loads(canonical.decode("utf-8"))
        except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"odds raw artifact is corrupt: {artifact_hash}") from error
        if (
            len(canonical) != int(row[2])
            or hashlib.sha256(canonical).hexdigest() != artifact_hash
            or canonical_json_value_bytes(payload) != canonical
            or schema_fingerprint(payload) != str(row[4])
        ):
            raise RuntimeError(f"odds raw artifact metadata mismatch: {artifact_hash}")
        while pending is not None and pending[0] == artifact_hash:
            if not isinstance(payload, dict):
                raise RuntimeError(f"odds raw response is not an object: {artifact_hash}")
            state = pending[1]
            if payload.get("artifact_version") == SNAPSHOT_DERIVED_FORMAT:
                _verify_snapshot_derived_artifact(payload, (state,), artifact_hash)
            else:
                _verify_full_artifact(payload, (state,), artifact_hash)
            pending = next(state_groups, None)
    if pending is not None:
        raise RuntimeError(f"odds transport raw artifact is missing: {pending[0]}")
    return artifact_count


def verify_odds_response_authority(
    connection: sqlite3.Connection,
    raw_archive_root: str | Path,
) -> OddsResponseAuthorityVerification:
    """Recompute every persisted state, transport binding, and raw artifact."""

    required = (
        "odds_response_states",
        "odds_response_state_outcomes",
        "odds_transport_observations",
        "odds_response_outcomes",
        "odds_raw_artifacts",
    )
    missing = [table for table in required if not _table_exists(connection, table)]
    if missing:
        raise RuntimeError("odds response authority schema is missing: " + ", ".join(missing))
    root = Path(raw_archive_root).resolve()
    state_count = _verify_states(connection)
    transport_count, legacy_count = _verify_transports(connection)
    artifact_count = _verify_artifacts(connection, root)
    return OddsResponseAuthorityVerification(
        state_count, transport_count, artifact_count, legacy_count
    )


__all__ = [
    "OddsResponseAuthorityVerification",
    "verify_odds_response_authority",
]
