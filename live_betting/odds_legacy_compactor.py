"""Offline conversion of legacy per-observation odds rows to v2 storage.

The input database is never modified.  A consistent SQLite backup is converted
under an explicit destination root, validated against the input, and published
only through ``VACUUM INTO`` after every legacy observation is proven equivalent.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_intelligence.raw_archive import RawArchive, schema_fingerprint
from shared.sqlite import connect, execute_script

from .database_protocol import (
    CUTOVER_SAFETY_MARGIN_BYTES,
    online_backup,
    verify_prepared_database,
)
from .markets import legacy_normalized_state_hash_v1, normalized_state_hash
from .models import Market, OddsSnapshot
from .odds_response_authority import (
    ResponseStateOutcome,
    legacy_response_state_identity_v1,
    response_artifact_identity,
    response_state_identity,
    snapshot_derived_payload,
)
from .storage import CURRENT_SCHEMA_VERSION, SCHEMA_SQL


_MANIFEST_FORMAT = "legacy-odds-compaction-v1"
_WORK_DATABASE = "compaction-work.db"
_OUTPUT_DATABASE = "dota2-compacted.db"
_RAW_ROOT = Path("live_betting") / "raw-v2"
_MANIFEST = "compaction-manifest.json"
_SAFETY_MARGIN_BYTES = CUTOVER_SAFETY_MARGIN_BYTES
_COMMIT_BATCH_SIZE = 100
_LEGACY_COLUMNS = (
    "odds_id",
    "odds_group_id",
    "received_at",
    "price",
    "status",
    "market_type",
    "period",
    "side",
    "line",
    "outcome_key",
    "supported",
    "last_update",
    "raw_json",
)


@dataclass(frozen=True)
class CompactionResult:
    output_database: Path
    raw_root: Path
    source_sha256: str
    output_sha256: str
    observation_count: int
    outcome_count: int
    state_count: int
    artifact_count: int
    equivalence_sha256: str
    source_bytes: int
    output_bytes: int


@dataclass(frozen=True)
class _LegacyGroup:
    observation_key: str
    source: str
    source_event_id: str | None
    raybet_match_id: str
    observed_at: str
    normalized_state_hash: str
    timing_status: str
    processing_status: str
    normalized_change_count: int
    rows: tuple[sqlite3.Row, ...]


@dataclass(frozen=True)
class _GroupAuthority:
    normalized_state_hash: str
    original_legacy_normalized_state_hash: str
    state_hash: str
    state_outcomes: tuple[ResponseStateOutcome, ...]
    artifact_hash: str
    artifact_bytes: bytes
    artifact_payload: Any
    raw_outcomes: tuple[Mapping[str, Any], ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = _canonical_json(manifest) + b"\n"
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("compaction checkpoint manifest is invalid") from error
    if not isinstance(value, dict) or value.get("format") != _MANIFEST_FORMAT:
        raise RuntimeError("compaction checkpoint manifest has the wrong format")
    return value


def _contained_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("compaction path must be controlled and relative")
    result = (root / relative).resolve()
    try:
        result.relative_to(root)
    except ValueError as error:
        raise ValueError("compaction path escapes destination root") from error
    return result


def _require_distinct_roots(
    source_database: Path,
    source_raw_root: Path,
    destination_root: Path,
) -> None:
    for source in (source_database, source_raw_root):
        try:
            source.relative_to(destination_root)
        except ValueError:
            continue
        raise ValueError("source paths must be outside the compaction destination")
    if source_database == destination_root:
        raise ValueError("source database and destination must be distinct")


def _require_offline_checkpointed_source(database: Path) -> None:
    wal = Path(f"{database}-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError(
            "source database has a non-empty WAL; stop writers and checkpoint it first"
        )


def _registered_raw_artifact_count(database: Path) -> int:
    connection = connect(database, read_only=True)
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM odds_raw_artifacts").fetchone()[0]
        )
    finally:
        connection.close()


def _preflight_space(
    source_database: Path,
    destination_root: Path,
    *,
    resume: bool,
) -> None:
    connection = connect(source_database, read_only=True)
    try:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        registered_raw = 0
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odds_raw_artifacts'"
        ).fetchone():
            registered_raw = int(
                connection.execute(
                    "SELECT COALESCE(SUM(compressed_bytes), 0) FROM odds_raw_artifacts"
                ).fetchone()[0]
            )
    finally:
        connection.close()
    logical_bytes = page_count * page_size
    database_copies = 2 if resume else 3
    required = logical_bytes * database_copies + registered_raw + _SAFETY_MARGIN_BYTES
    available = shutil.disk_usage(destination_root).free
    if available < required:
        raise RuntimeError(
            "insufficient free space for offline odds compaction: "
            f"required_bytes={required}, available_bytes={available}, "
            f"destination={destination_root}"
        )


def _parse_aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("legacy transport time is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("legacy transport time must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _verify_artifact_file(
    path: Path,
    *,
    content_hash: str,
    uncompressed_bytes: int,
    compressed_bytes: int,
    fingerprint: str,
) -> None:
    if not path.is_file():
        raise RuntimeError(f"raw artifact is missing: {path}")
    if path.stat().st_size != compressed_bytes:
        raise RuntimeError(f"raw artifact compressed size mismatch: {path}")
    try:
        canonical = gzip.decompress(path.read_bytes())
        payload = json.loads(canonical)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"raw artifact is corrupt: {path}") from error
    if len(canonical) != uncompressed_bytes:
        raise RuntimeError(f"raw artifact byte count mismatch: {path}")
    if hashlib.sha256(canonical).hexdigest() != content_hash:
        raise RuntimeError(f"raw artifact hash mismatch: {path}")
    if schema_fingerprint(payload) != fingerprint:
        raise RuntimeError(f"raw artifact schema fingerprint mismatch: {path}")


def _copy_existing_artifacts(
    database: Path,
    source_root: Path,
    destination_root: Path,
) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    connection = connect(database, read_only=True, row_factory=sqlite3.Row)
    try:
        rows = connection.execute(
            """SELECT artifact_hash, storage_path, uncompressed_bytes,
                      compressed_bytes, schema_fingerprint
                 FROM odds_raw_artifacts ORDER BY artifact_hash"""
        )
        for row in rows:
            relative = Path(str(row["storage_path"]))
            source = _contained_path(source_root, relative)
            destination = _contained_path(destination_root, relative)
            expected = {
                "content_hash": str(row["artifact_hash"]),
                "uncompressed_bytes": int(row["uncompressed_bytes"]),
                "compressed_bytes": int(row["compressed_bytes"]),
                "fingerprint": str(row["schema_fingerprint"]),
            }
            _verify_artifact_file(source, **expected)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                _verify_artifact_file(destination, **expected)
                continue
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            try:
                shutil.copy2(source, temporary)
                _verify_artifact_file(temporary, **expected)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        connection.close()


def _iter_legacy_groups(connection: sqlite3.Connection) -> Iterator[_LegacyGroup]:
    cursor = connection.execute(
        """SELECT transport.observation_key, transport.source,
                  transport.source_event_id, transport.raybet_match_id,
                  transport.observed_at, transport.normalized_state_hash,
                  transport.timing_status, transport.processing_status,
                  transport.normalized_change_count,
                  legacy.odds_id, legacy.odds_group_id, legacy.received_at,
                  legacy.price, legacy.status, legacy.market_type, legacy.period,
                  legacy.side, legacy.line, legacy.outcome_key,
                  legacy.supported, legacy.last_update, legacy.raw_json
             FROM odds_transport_observations AS transport
             LEFT JOIN odds_response_outcomes AS legacy
               ON legacy.observation_key=transport.observation_key
            WHERE transport.response_state_hash IS NULL
              AND transport.response_artifact_hash IS NULL
            ORDER BY transport.observation_key, legacy.odds_id"""
    )
    current_key: str | None = None
    metadata: tuple[Any, ...] | None = None
    rows: list[sqlite3.Row] = []
    for row in cursor:
        key = str(row["observation_key"])
        if current_key is not None and key != current_key:
            assert metadata is not None
            yield _LegacyGroup(current_key, *metadata, tuple(rows))
            rows = []
        if key != current_key:
            current_key = key
            metadata = (
                str(row["source"]),
                row["source_event_id"],
                str(row["raybet_match_id"]),
                str(row["observed_at"]),
                str(row["normalized_state_hash"]),
                str(row["timing_status"]),
                str(row["processing_status"]),
                int(row["normalized_change_count"]),
            )
        if row["odds_id"] is not None:
            rows.append(row)
    if current_key is not None:
        assert metadata is not None
        yield _LegacyGroup(current_key, *metadata, tuple(rows))


def _legacy_group_by_key(
    connection: sqlite3.Connection,
    observation_key: str,
) -> _LegacyGroup:
    rows = connection.execute(
        """SELECT transport.observation_key, transport.source,
                  transport.source_event_id, transport.raybet_match_id,
                  transport.observed_at, transport.normalized_state_hash,
                  transport.timing_status, transport.processing_status,
                  transport.normalized_change_count,
                  legacy.odds_id, legacy.odds_group_id, legacy.received_at,
                  legacy.price, legacy.status, legacy.market_type, legacy.period,
                  legacy.side, legacy.line, legacy.outcome_key,
                  legacy.supported, legacy.last_update, legacy.raw_json
             FROM odds_transport_observations AS transport
             LEFT JOIN odds_response_outcomes AS legacy
               ON legacy.observation_key=transport.observation_key
            WHERE transport.observation_key=?
            ORDER BY legacy.odds_id""",
        (observation_key,),
    ).fetchall()
    if not rows:
        raise RuntimeError("legacy observation disappeared from source")
    first = rows[0]
    legacy_rows = tuple(row for row in rows if row["odds_id"] is not None)
    return _LegacyGroup(
        str(first["observation_key"]),
        str(first["source"]),
        first["source_event_id"],
        str(first["raybet_match_id"]),
        str(first["observed_at"]),
        str(first["normalized_state_hash"]),
        str(first["timing_status"]),
        str(first["processing_status"]),
        int(first["normalized_change_count"]),
        legacy_rows,
    )


def _group_authority(group: _LegacyGroup) -> _GroupAuthority:
    state_values: list[Sequence[Any]] = []
    raw_outcomes: list[Mapping[str, Any]] = []
    snapshots: list[OddsSnapshot] = []
    seen: set[str] = set()
    for row in group.rows:
        odds_id = str(row["odds_id"])
        if odds_id in seen:
            raise RuntimeError("legacy response contains a duplicate odds id")
        seen.add(odds_id)
        if str(row["received_at"]) != group.observed_at:
            raise RuntimeError("legacy outcome transport time mismatch")
        try:
            raw = json.loads(str(row["raw_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("legacy response outcome JSON is invalid") from error
        if not isinstance(raw, dict):
            raise RuntimeError("legacy response outcome JSON is not an object")
        raw_id = str(raw.get("odds_id") or raw.get("id") or "")
        if raw_id != odds_id:
            raise RuntimeError("legacy raw outcome id does not match its primary key")
        raw_outcomes.append(raw)
        snapshots.append(
            OddsSnapshot(
                raybet_match_id=group.raybet_match_id,
                odds_id=odds_id,
                odds_group_id=row["odds_group_id"],
                received_at=_parse_aware(group.observed_at),
                price=float(row["price"]),
                status=row["status"],
                market=Market(
                    str(row["market_type"]),
                    str(row["period"]),
                    row["side"],
                    row["line"],
                    str(row["outcome_key"]),
                    bool(row["supported"]),
                ),
                last_update=row["last_update"],
                raw=raw,
            )
        )
        state_values.append(
            (
                odds_id,
                row["odds_group_id"],
                row["price"],
                row["status"],
                str(row["market_type"]),
                str(row["period"]),
                row["side"],
                row["line"],
                str(row["outcome_key"]),
                int(row["supported"]),
                row["last_update"],
            )
        )
    if legacy_normalized_state_hash_v1(snapshots) != group.normalized_state_hash:
        raise RuntimeError("legacy normalized state hash does not match its outcomes")
    normalized_v2 = normalized_state_hash(snapshots)
    state_hash, state_outcomes, _ = response_state_identity(
        group.raybet_match_id,
        normalized_v2,
        state_values,
    )
    payload = snapshot_derived_payload(group.raybet_match_id, raw_outcomes)
    artifact_hash, artifact_bytes, sanitized = response_artifact_identity(payload)
    return _GroupAuthority(
        normalized_v2,
        group.normalized_state_hash,
        state_hash,
        state_outcomes,
        artifact_hash,
        artifact_bytes,
        sanitized,
        tuple(raw_outcomes),
    )


def _state_rows(
    connection: sqlite3.Connection,
    state_hash: str,
) -> tuple[ResponseStateOutcome, ...]:
    rows = connection.execute(
        """SELECT odds_id, odds_group_id, price, status, market_type, period,
                  side, line, outcome_key, supported, last_update
             FROM odds_response_state_outcomes
            WHERE response_state_hash=? ORDER BY odds_id""",
        (state_hash,),
    ).fetchall()
    return tuple(tuple(row) for row in rows)  # type: ignore[return-value]


def _persist_state(
    connection: sqlite3.Connection,
    group: _LegacyGroup,
    authority: _GroupAuthority,
) -> None:
    normalized_rows = connection.execute(
        """SELECT response_state_hash FROM odds_response_states
            WHERE raybet_match_id=? AND normalized_state_hash=?
              AND normalized_state_hash_version=2
            ORDER BY response_state_hash""",
        (group.raybet_match_id, authority.normalized_state_hash),
    ).fetchall()
    normalized_hashes = tuple(str(row[0]) for row in normalized_rows)
    if normalized_hashes and normalized_hashes != (authority.state_hash,):
        raise RuntimeError(
            "normalized state hash maps to a different response manifest"
        )
    existing = connection.execute(
        """SELECT raybet_match_id, normalized_state_hash,
                  normalized_state_hash_version,
                  original_legacy_normalized_state_hash, outcome_count
             FROM odds_response_states WHERE response_state_hash=?""",
        (authority.state_hash,),
    ).fetchone()
    if existing is not None:
        identity = (
            str(existing[0]),
            str(existing[1]),
            int(existing[2]),
            existing[3],
            int(existing[4]),
        )
        expected = (
            group.raybet_match_id,
            authority.normalized_state_hash,
            2,
            authority.original_legacy_normalized_state_hash,
            len(authority.state_outcomes),
        )
        if identity != expected or _state_rows(connection, authority.state_hash) != (
            authority.state_outcomes
        ):
            raise RuntimeError("response state hash or manifest collision")
        return
    connection.execute(
        """INSERT INTO odds_response_states
           (response_state_hash, raybet_match_id, normalized_state_hash,
            normalized_state_hash_version, original_legacy_normalized_state_hash,
            outcome_count) VALUES (?, ?, ?, 2, ?, ?)""",
        (
            authority.state_hash,
            group.raybet_match_id,
            authority.normalized_state_hash,
            authority.original_legacy_normalized_state_hash,
            len(authority.state_outcomes),
        ),
    )
    connection.executemany(
        """INSERT INTO odds_response_state_outcomes
           (response_state_hash, odds_id, odds_group_id, price, status,
            market_type, period, side, line, outcome_key, supported, last_update)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ((authority.state_hash, *row) for row in authority.state_outcomes),
    )


def _persist_artifact(
    connection: sqlite3.Connection,
    raw_archive: RawArchive,
    raw_root: Path,
    group: _LegacyGroup,
    authority: _GroupAuthority,
) -> None:
    existing = connection.execute(
        """SELECT source, storage_path, uncompressed_bytes, compressed_bytes,
                  schema_fingerprint
             FROM odds_raw_artifacts WHERE artifact_hash=?""",
        (authority.artifact_hash,),
    ).fetchone()
    if existing is not None:
        path = _contained_path(raw_root, Path(str(existing[1])))
        _verify_artifact_file(
            path,
            content_hash=authority.artifact_hash,
            uncompressed_bytes=int(existing[2]),
            compressed_bytes=int(existing[3]),
            fingerprint=str(existing[4]),
        )
        if str(existing[0]) != "raybet" or int(existing[2]) != len(
            authority.artifact_bytes
        ):
            raise RuntimeError("raw artifact hash or metadata collision")
        return

    receipt = raw_archive.archive_json(
        source="raybet",
        endpoint="https://raybet.local/v2/odds",
        request_identity=(
            "https://raybet.local/v2/odds?match_id=" + group.raybet_match_id
        ),
        payload_bytes=authority.artifact_bytes,
        observed_at=_parse_aware(group.observed_at),
        match_id=(
            int(group.raybet_match_id)
            if group.raybet_match_id.isdigit() and int(group.raybet_match_id) > 0
            else None
        ),
        status_code=None,
    )
    if receipt.content_sha256 != authority.artifact_hash:
        raise RuntimeError("raw archive changed the canonical artifact identity")
    relative = receipt.path.resolve().relative_to(raw_root)
    connection.execute(
        """INSERT INTO odds_raw_artifacts
           (artifact_hash, source, storage_path, uncompressed_bytes,
            compressed_bytes, schema_fingerprint)
           VALUES (?, 'raybet', ?, ?, ?, ?)""",
        (
            authority.artifact_hash,
            relative.as_posix(),
            receipt.byte_count,
            receipt.compressed_byte_count,
            receipt.schema_fingerprint,
        ),
    )


def _convert_group(
    connection: sqlite3.Connection,
    raw_archive: RawArchive,
    raw_root: Path,
    group: _LegacyGroup,
) -> None:
    if not connection.in_transaction:
        raise RuntimeError("response conversion requires an active batch transaction")
    authority = _group_authority(group)
    _persist_state(connection, group, authority)
    _persist_artifact(connection, raw_archive, raw_root, group, authority)
    updated = connection.execute(
        """UPDATE odds_transport_observations
              SET normalized_state_hash=?, normalized_state_hash_version=2,
                  original_legacy_normalized_state_hash=?,
                  response_state_hash=?, response_artifact_hash=?
            WHERE observation_key=?
              AND response_state_hash IS NULL
              AND response_artifact_hash IS NULL""",
        (
            authority.normalized_state_hash,
            authority.original_legacy_normalized_state_hash,
            authority.state_hash,
            authority.artifact_hash,
            group.observation_key,
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("legacy transport observation was not updated exactly once")


def _acquire_compaction_connection(database: Path) -> sqlite3.Connection:
    connection = connect(database, row_factory=sqlite3.Row)
    mode = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
    if mode is None or str(mode[0]).casefold() != "exclusive":
        connection.close()
        raise RuntimeError("failed to acquire exclusive compaction mode")
    connection.execute("BEGIN EXCLUSIVE")
    connection.commit()
    partial = int(
        connection.execute(
            """SELECT COUNT(*) FROM odds_transport_observations
                WHERE (response_state_hash IS NULL)
                   != (response_artifact_hash IS NULL)"""
        ).fetchone()[0]
    )
    if partial:
        connection.close()
        raise RuntimeError("transport observation has partial v2 storage references")
    connection.execute("DROP TRIGGER IF EXISTS odds_transport_observations_guard_update")
    connection.commit()
    return connection


def _group_equivalence_payload(
    group: _LegacyGroup,
    authority: _GroupAuthority,
) -> dict[str, Any]:
    return {
        "observation_key": group.observation_key,
        "transport": [
            group.source,
            group.source_event_id,
            group.raybet_match_id,
            group.observed_at,
            authority.normalized_state_hash,
            2,
            authority.original_legacy_normalized_state_hash,
            group.timing_status,
            group.processing_status,
            group.normalized_change_count,
        ],
        "state_outcomes": authority.state_outcomes,
        "raw_outcomes": authority.raw_outcomes,
    }


def _validate_equivalence(
    source_database: Path,
    converted_database: Path,
    raw_root: Path,
) -> tuple[int, int, str]:
    source = connect(source_database, read_only=True, row_factory=sqlite3.Row)
    converted = connect(converted_database, read_only=True, row_factory=sqlite3.Row)
    observation_count = 0
    outcome_count = 0
    root_digest = hashlib.sha256()
    try:
        source_keys = source.execute(
            """SELECT observation_key FROM odds_transport_observations
                WHERE response_state_hash IS NULL
                  AND response_artifact_hash IS NULL
                ORDER BY observation_key"""
        )
        for key_row in source_keys:
            group = _legacy_group_by_key(source, str(key_row[0]))
            authority = _group_authority(group)
            target = converted.execute(
                """SELECT source, source_event_id, raybet_match_id, observed_at,
                          normalized_state_hash, normalized_state_hash_version,
                          original_legacy_normalized_state_hash, timing_status,
                          processing_status, normalized_change_count,
                          response_state_hash, response_artifact_hash
                     FROM odds_transport_observations WHERE observation_key=?""",
                (group.observation_key,),
            ).fetchone()
            if target is None:
                raise RuntimeError("converted database is missing a transport observation")
            actual_transport = tuple(target[:10])
            expected_transport = (
                group.source,
                group.source_event_id,
                group.raybet_match_id,
                group.observed_at,
                authority.normalized_state_hash,
                2,
                authority.original_legacy_normalized_state_hash,
                group.timing_status,
                group.processing_status,
                group.normalized_change_count,
            )
            if actual_transport != expected_transport:
                raise RuntimeError("converted transport critical fields differ")
            if (str(target[10]), str(target[11])) != (
                authority.state_hash,
                authority.artifact_hash,
            ):
                raise RuntimeError("converted transport authority differs from legacy data")
            if _state_rows(converted, authority.state_hash) != authority.state_outcomes:
                raise RuntimeError("converted response members differ from legacy data")
            root_digest.update(
                hashlib.sha256(
                    _canonical_json(_group_equivalence_payload(group, authority))
                ).digest()
            )
            observation_count += 1
            outcome_count += len(group.rows)

        missing_refs = converted.execute(
            """SELECT COUNT(*) FROM odds_transport_observations AS transport
                 LEFT JOIN odds_response_states AS state
                  ON state.response_state_hash=transport.response_state_hash
                 AND state.raybet_match_id=transport.raybet_match_id
                 AND state.normalized_state_hash=transport.normalized_state_hash
                 AND state.normalized_state_hash_version=
                     transport.normalized_state_hash_version
                 AND state.original_legacy_normalized_state_hash
                     IS transport.original_legacy_normalized_state_hash
                LEFT JOIN odds_raw_artifacts AS artifact
                  ON artifact.artifact_hash=transport.response_artifact_hash
                WHERE transport.response_state_hash IS NULL
                   OR transport.response_artifact_hash IS NULL
                   OR state.response_state_hash IS NULL
                   OR artifact.artifact_hash IS NULL"""
        ).fetchone()[0]
        if int(missing_refs):
            raise RuntimeError("converted database has missing transport authorities")
        conflict = converted.execute(
            """SELECT raybet_match_id, normalized_state_hash,
                      normalized_state_hash_version
                 FROM odds_response_states
                WHERE normalized_state_hash_version=2
                GROUP BY raybet_match_id, normalized_state_hash,
                         normalized_state_hash_version
               HAVING COUNT(*) != 1 LIMIT 1"""
        ).fetchone()
        if conflict is not None:
            raise RuntimeError("normalized state hash has multiple response manifests")
        for state in converted.execute(
            """SELECT response_state_hash, raybet_match_id, normalized_state_hash,
                      normalized_state_hash_version, outcome_count
                 FROM odds_response_states
                ORDER BY response_state_hash"""
        ):
            rows = _state_rows(converted, str(state[0]))
            if int(state[3]) == 2:
                computed, ordered, _ = response_state_identity(
                    str(state[1]), str(state[2]), rows
                )
            else:
                computed, ordered, _ = legacy_response_state_identity_v1(
                    str(state[1]), str(state[2]), rows
                )
            if (
                computed != str(state[0])
                or ordered != rows
                or len(rows) != int(state[4])
            ):
                raise RuntimeError("persisted response state fails canonical verification")
        for artifact in converted.execute(
            """SELECT artifact_hash, storage_path, uncompressed_bytes,
                      compressed_bytes, schema_fingerprint
                 FROM odds_raw_artifacts ORDER BY artifact_hash"""
        ):
            _verify_artifact_file(
                _contained_path(raw_root, Path(str(artifact[1]))),
                content_hash=str(artifact[0]),
                uncompressed_bytes=int(artifact[2]),
                compressed_bytes=int(artifact[3]),
                fingerprint=str(artifact[4]),
            )
    finally:
        source.close()
        converted.close()
    return observation_count, outcome_count, root_digest.hexdigest()


def _restore_final_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN EXCLUSIVE")
    try:
        connection.execute("DROP VIEW IF EXISTS odds_response_outcomes_effective")
        connection.execute("DROP TABLE odds_response_outcomes")
        execute_script(connection, SCHEMA_SQL)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _vacuum_into(connection: sqlite3.Connection, output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"compaction output already exists: {output}")
    connection.execute("VACUUM INTO ?", (str(output),))


def _verify_sqlite_output(database: Path, raw_root: Path) -> None:
    verify_prepared_database(database, odds_raw_root=raw_root)
    connection = connect(database, read_only=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("compacted database failed integrity_check")
        foreign_key = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key is not None:
            raise RuntimeError(
                "compacted database failed foreign_key_check: "
                f"table={foreign_key[0]} rowid={foreign_key[1]}"
            )
        if int(connection.execute("SELECT COUNT(*) FROM odds_response_outcomes").fetchone()[0]):
            raise RuntimeError("compacted database retained legacy outcome rows")
        required = {
            "odds_response_outcomes_legacy_insert_disabled",
            "odds_transport_observations_guard_update",
            "odds_transport_observations_require_v2_state",
        }
        actual = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        if not required <= actual:
            raise RuntimeError("compacted database is missing final write guards")
        if connection.execute(
            """SELECT 1 FROM sqlite_master
                WHERE name LIKE 'odds_legacy_compaction_%' LIMIT 1"""
        ).fetchone():
            raise RuntimeError("compaction checkpoint objects leaked into final schema")
    finally:
        connection.close()


def _result_from_manifest(root: Path, manifest: Mapping[str, Any]) -> CompactionResult:
    return CompactionResult(
        output_database=_contained_path(root, Path(str(manifest["output_database"]))),
        raw_root=_contained_path(root, Path(str(manifest["raw_root"]))),
        source_sha256=str(manifest["source_sha256"]),
        output_sha256=str(manifest["output_sha256"]),
        observation_count=int(manifest["observation_count"]),
        outcome_count=int(manifest["outcome_count"]),
        state_count=int(manifest["state_count"]),
        artifact_count=int(manifest["artifact_count"]),
        equivalence_sha256=str(manifest["equivalence_sha256"]),
        source_bytes=int(manifest["source_bytes"]),
        output_bytes=int(manifest["output_bytes"]),
    )


def compact_legacy_odds(
    source_database: str | Path,
    source_raw_root: str | Path,
    destination_root: str | Path,
    *,
    resume: bool = False,
    _fail_after_observations: int | None = None,
) -> CompactionResult:
    """Convert one database prepared at the current live schema version."""

    source_database = Path(source_database).resolve()
    source_raw_root = Path(source_raw_root).resolve()
    destination_root = Path(destination_root).resolve()
    if not source_database.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_database}")
    if source_raw_root.exists() and not source_raw_root.is_dir():
        raise FileNotFoundError(f"source raw root is not a directory: {source_raw_root}")
    destination_root.mkdir(parents=True, exist_ok=True)
    _require_distinct_roots(source_database, source_raw_root, destination_root)
    _require_offline_checkpointed_source(source_database)
    verify_prepared_database(source_database, odds_raw_root=source_raw_root)
    if (
        not source_raw_root.exists()
        and _registered_raw_artifact_count(source_database) != 0
    ):
        raise RuntimeError("source raw root is missing for registered raw artifacts")

    work_database = _contained_path(destination_root, Path(_WORK_DATABASE))
    output_database = _contained_path(destination_root, Path(_OUTPUT_DATABASE))
    raw_root = _contained_path(destination_root, _RAW_ROOT)
    manifest_path = _contained_path(destination_root, Path(_MANIFEST))
    source_hash = _sha256_file(source_database)
    source_size = source_database.stat().st_size

    if manifest_path.exists():
        if not resume:
            raise FileExistsError(
                "compaction checkpoint already exists; pass resume=True to continue"
            )
        manifest = _read_manifest(manifest_path)
        expected_source = (
            str(source_database),
            source_hash,
            source_size,
            str(source_raw_root),
        )
        recorded_source = (
            manifest.get("source_database"),
            manifest.get("source_sha256"),
            manifest.get("source_bytes"),
            manifest.get("source_raw_root"),
        )
        if recorded_source != expected_source:
            raise RuntimeError("compaction checkpoint belongs to another source")
        if manifest.get("live_schema_version") != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "compaction checkpoint live schema version differs from current code"
            )
        if manifest.get("status") == "ready":
            result = _result_from_manifest(destination_root, manifest)
            _verify_sqlite_output(result.output_database, result.raw_root)
            if _sha256_file(result.output_database) != result.output_sha256:
                raise RuntimeError("published compaction output hash mismatch")
            return result
        if output_database.exists():
            if manifest.get("status") not in {"publishing", "failed"}:
                raise RuntimeError("unmanaged compaction output already exists")
            required = {
                "output_sha256",
                "output_bytes",
                "observation_count",
                "outcome_count",
                "state_count",
                "artifact_count",
                "equivalence_sha256",
            }
            if not required <= manifest.keys():
                raise RuntimeError("published compaction checkpoint is incomplete")
            recovered = _result_from_manifest(destination_root, manifest)
            _verify_sqlite_output(output_database, raw_root)
            if (
                output_database.stat().st_size != recovered.output_bytes
                or _sha256_file(output_database) != recovered.output_sha256
            ):
                raise RuntimeError("published compaction output does not match checkpoint")
            manifest["status"] = "ready"
            manifest.pop("error", None)
            _write_manifest(manifest_path, manifest)
            return recovered
        if not work_database.is_file():
            raise RuntimeError("compaction checkpoint work database is missing")
    else:
        if resume:
            raise FileNotFoundError("compaction checkpoint does not exist")
        occupied = [path for path in (work_database, output_database, raw_root) if path.exists()]
        if occupied:
            raise FileExistsError(
                "compaction destination contains unmanaged output: "
                + ", ".join(str(path) for path in occupied)
            )
        _preflight_space(source_database, destination_root, resume=False)
        online_backup(source_database, work_database)
        _copy_existing_artifacts(work_database, source_raw_root, raw_root)
        manifest = {
            "format": _MANIFEST_FORMAT,
            "status": "processing",
            "source_database": str(source_database),
            "source_raw_root": str(source_raw_root),
            "source_sha256": source_hash,
            "source_bytes": source_size,
            "live_schema_version": CURRENT_SCHEMA_VERSION,
            "work_database": _WORK_DATABASE,
            "raw_root": _RAW_ROOT.as_posix(),
            "output_database": _OUTPUT_DATABASE,
            "completed_observations": 0,
        }
        _write_manifest(manifest_path, manifest)

    _preflight_space(source_database, destination_root, resume=True)
    manifest["status"] = "processing"
    manifest.pop("error", None)
    _write_manifest(manifest_path, manifest)
    converted = 0
    connection: sqlite3.Connection | None = None
    temporary_output = output_database.with_name(f".{output_database.name}.vacuuming")
    try:
        connection = _acquire_compaction_connection(work_database)
        archive = RawArchive(raw_root)
        pending = 0
        for group in _iter_legacy_groups(connection):
            if not connection.in_transaction:
                connection.execute("BEGIN IMMEDIATE")
            _convert_group(connection, archive, raw_root, group)
            converted += 1
            pending += 1
            if pending == _COMMIT_BATCH_SIZE:
                connection.commit()
                manifest["completed_observations"] = int(
                    manifest.get("completed_observations", 0)
                ) + pending
                pending = 0
                _write_manifest(manifest_path, manifest)
            if (
                _fail_after_observations is not None
                and converted >= _fail_after_observations
            ):
                raise RuntimeError("injected compaction interruption")
        if connection.in_transaction:
            connection.commit()
        if pending:
            manifest["completed_observations"] = int(
                manifest.get("completed_observations", 0)
            ) + pending
            _write_manifest(manifest_path, manifest)

        # Exclusive locking mode intentionally prevents even a second reader.
        # Release it before the independent source-vs-output audit connection.
        connection.close()
        connection = None
        observation_count, outcome_count, equivalence_hash = _validate_equivalence(
            source_database, work_database, raw_root
        )
        manifest.update(
            {
                "status": "validated",
                "observation_count": observation_count,
                "outcome_count": outcome_count,
                "equivalence_sha256": equivalence_hash,
            }
        )
        _write_manifest(manifest_path, manifest)
        connection = _acquire_compaction_connection(work_database)
        _restore_final_schema(connection)
        connection.close()
        connection = None

        temporary_output.unlink(missing_ok=True)
        vacuum_connection = connect(work_database)
        try:
            _vacuum_into(vacuum_connection, temporary_output)
        finally:
            vacuum_connection.close()
        _verify_sqlite_output(temporary_output, raw_root)
        if _sha256_file(source_database) != source_hash:
            raise RuntimeError("source database changed during offline compaction")
        output_hash = _sha256_file(temporary_output)
        counts = connect(temporary_output, read_only=True)
        try:
            state_count = int(
                counts.execute("SELECT COUNT(*) FROM odds_response_states").fetchone()[0]
            )
            artifact_count = int(
                counts.execute("SELECT COUNT(*) FROM odds_raw_artifacts").fetchone()[0]
            )
        finally:
            counts.close()
        manifest.update(
            {
                "status": "publishing",
                "state_count": state_count,
                "artifact_count": artifact_count,
                "output_sha256": output_hash,
                "output_bytes": temporary_output.stat().st_size,
            }
        )
        _write_manifest(manifest_path, manifest)
        if output_database.exists():
            raise FileExistsError(f"compaction output already exists: {output_database}")
        os.replace(temporary_output, output_database)
        manifest["status"] = "ready"
        _write_manifest(manifest_path, manifest)
        return _result_from_manifest(destination_root, manifest)
    except BaseException as error:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        manifest["status"] = "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        _write_manifest(manifest_path, manifest)
        raise
    finally:
        temporary_output.unlink(missing_ok=True)


def result_json(result: CompactionResult) -> str:
    payload = asdict(result)
    payload["output_database"] = str(result.output_database)
    payload["raw_root"] = str(result.raw_root)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["CompactionResult", "compact_legacy_odds", "result_json"]
