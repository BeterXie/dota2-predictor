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
import secrets
import shutil
import sqlite3
import stat
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_intelligence.raw_archive import RawArchive, schema_fingerprint
from shared.sqlite import connect, execute_script

from .database_protocol import (
    CUTOVER_SAFETY_MARGIN_BYTES,
    immutable_checkpoint_reader,
    online_backup,
    sqlite_sidecar_state,
    vacuum_into_immutable_checkpoint,
    verify_prepared_database,
)
from .hash_authority import (
    file_hash_authority_scope,
    invalidate_hashed_paths,
    read_hashed_file,
    rebind_hashed_paths,
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
from .service_coordination import (
    DatabaseFileIdentity,
    DirectoryIdentity,
    SingleInstanceLock,
    WriterScanResult,
    capture_directory_identity,
    database_authority_lock_paths,
    fsync_directory,
    require_directory_identity,
    require_unique_database_file,
    scan_managed_writers,
)
from .storage import CURRENT_SCHEMA_VERSION, SCHEMA_SQL


_MANIFEST_FORMAT = "legacy-odds-compaction-v1"
_WORK_ROOT = Path(".compaction-work")
_WORK_DATABASE = "compaction-work.db"
_INITIALIZING_WORK_DATABASE = ".compaction-work.db.initializing"
_WORK_DATABASE_PATH = _WORK_ROOT / _WORK_DATABASE
_INITIALIZING_WORK_DATABASE_PATH = _WORK_ROOT / _INITIALIZING_WORK_DATABASE
_OUTPUT_DATABASE = "dota2-compacted.db"
_RAW_ROOT = Path("live_betting") / "raw-v2"
_MANIFEST = "compaction-manifest.json"
_DESTINATION_LOCK = ".compaction.lock"
_SAFETY_MARGIN_BYTES = CUTOVER_SAFETY_MARGIN_BYTES
_COMMIT_BATCH_SIZE = 100
_PROGRESS_BATCH_SIZE = 500
_VERIFIED_HASH_CACHE_SIZE = 4096
_WORK_DATABASE_AUTHORITY = "work_database_authority"
_PROCESSING_MUTATED_TABLES = frozenset(
    {
        "odds_transport_observations",
        "odds_response_states",
        "odds_response_state_outcomes",
        "odds_raw_artifacts",
    }
)
_TRANSPORT_CONVERSION_COLUMNS = frozenset(
    {
        "normalized_state_hash",
        "normalized_state_hash_version",
        "original_legacy_normalized_state_hash",
        "response_state_hash",
        "response_artifact_hash",
    }
)
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
    state_manifest_bytes: bytes
    artifact_hash: str
    artifact_bytes: bytes
    artifact_payload: Any
    raw_outcomes: tuple[Mapping[str, Any], ...]


class _BoundedFingerprintCache:
    def __init__(self, max_entries: int = _VERIFIED_HASH_CACHE_SIZE) -> None:
        if max_entries <= 0:
            raise ValueError("hash cache size must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()

    def get(self, value: str) -> str | None:
        fingerprint = self._entries.get(value)
        if fingerprint is not None:
            self._entries.move_to_end(value)
        return fingerprint

    def add(self, value: str, fingerprint: str) -> None:
        self._entries[value] = fingerprint
        self._entries.move_to_end(value)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class _WorkDatabaseAuthorityError(RuntimeError):
    """The resumable work file no longer matches its durable checkpoint."""


@dataclass
class _ManifestAuthority:
    value: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _secondary_fingerprint(value: bytes) -> str:
    return hashlib.sha512(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return read_hashed_file(
        path,
        label="compaction database",
        database=True,
    ).sha256


@contextmanager
def _checkpoint_reader(
    database: Path,
    *,
    label: str,
    required_locks: Sequence[Path],
    row_factory: type[sqlite3.Row] | None = None,
) -> Iterator[sqlite3.Connection]:
    if required_locks:
        with immutable_checkpoint_reader(
            database,
            label=label,
            required_locks=required_locks,
            row_factory=row_factory,
        ) as connection:
            yield connection
        return
    connection = connect(database, read_only=True, row_factory=row_factory)
    try:
        yield connection
    finally:
        connection.close()


def _fsync_bound_directories(*identities: DirectoryIdentity) -> None:
    seen: set[tuple[int, int]] = set()
    for identity in identities:
        require_directory_identity(identity, label="mutation parent")
        key = (identity.device, identity.inode)
        if key not in seen:
            fsync_directory(identity.path)
            seen.add(key)


def _replace_and_fsync(source: Path, destination: Path) -> None:
    source_parent = capture_directory_identity(
        source.parent,
        label="rename source parent",
    )
    destination_parent = capture_directory_identity(
        destination.parent,
        label="rename destination parent",
    )
    try:
        os.replace(source, destination)
    except BaseException:
        invalidate_hashed_paths(source, destination)
        raise
    _fsync_bound_directories(source_parent, destination_parent)
    rebind_hashed_paths(source, destination)


def _unlink_and_fsync(path: Path, *, missing_ok: bool = False) -> bool:
    parent = capture_directory_identity(path.parent, label="unlink parent")
    invalidate_hashed_paths(path)
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    _fsync_bound_directories(parent)
    return True


def _quarantine_verified_file(
    path: Path,
    *,
    expected_device: int,
    expected_inode: int,
    expected_bytes: int,
    expected_sha256: str,
    label: str,
    database: bool = False,
) -> None:
    quarantine = path.with_name(
        f".{path.name}.quarantine.{os.getpid()}.{secrets.token_hex(16)}"
    )
    if quarantine.exists() or quarantine.is_symlink():
        raise RuntimeError(f"{label} quarantine path already exists")
    _replace_and_fsync(path, quarantine)
    try:
        snapshot = read_hashed_file(
            quarantine,
            label=f"quarantined {label}",
            include_payload=False,
            database=database,
        )
    except BaseException:
        # The captured object is intentionally retained when its authority is unclear.
        raise
    if (
        snapshot.device != expected_device
        or snapshot.inode != expected_inode
        or snapshot.bytes != expected_bytes
        or snapshot.sha256 != expected_sha256
    ):
        raise RuntimeError(f"quarantined {label} authority changed")
    repeated = read_hashed_file(
        quarantine,
        label=f"quarantined {label}",
        include_payload=False,
        database=database,
    )
    if repeated != snapshot:
        raise RuntimeError(f"quarantined {label} authority changed before deletion")
    _unlink_and_fsync(quarantine)


def _quarantine_sqlite_sidecar(
    record: Mapping[str, int | bool | str | None],
    *,
    label: str,
) -> None:
    path = Path(str(record["path"]))
    snapshot = read_hashed_file(
        path,
        label=label,
        include_payload=False,
    )
    if (
        snapshot.device,
        snapshot.inode,
        snapshot.bytes,
        snapshot.mtime_ns,
    ) != (
        int(record["device"]),
        int(record["inode"]),
        int(record["bytes"]),
        int(record["mtime_ns"]),
    ):
        raise RuntimeError(f"{label} authority changed before quarantine")
    _quarantine_verified_file(
        path,
        expected_device=snapshot.device,
        expected_inode=snapshot.inode,
        expected_bytes=snapshot.bytes,
        expected_sha256=snapshot.sha256,
        label=label,
    )


def _require_transaction_free_database(
    database: Path,
    *,
    label: str,
) -> dict[str, dict[str, int | bool | str | None]]:
    state = sqlite_sidecar_state(database)
    nonempty = [
        Path(str(state[name]["path"]))
        for name in ("wal", "journal")
        if int(state[name]["bytes"])
    ]
    if nonempty:
        raise RuntimeError(
            f"{label} has non-empty transactional sidecars: "
            + ", ".join(str(path) for path in nonempty)
        )
    return state


def _clear_quiescent_sqlite_sidecars(database: Path, *, label: str) -> None:
    state = _require_transaction_free_database(database, label=label)
    for name in ("wal", "shm", "journal"):
        if state[name]["exists"]:
            invalidate_hashed_paths(database)
            _quarantine_sqlite_sidecar(
                state[name],
                label=f"{label} {name} sidecar",
            )
            invalidate_hashed_paths(database)
    completed = sqlite_sidecar_state(database)
    if any(bool(record["exists"]) for record in completed.values()):
        raise RuntimeError(f"{label} SQLite sidecars could not be cleared")


def _require_stable_work_database(database: Path) -> None:
    _require_transaction_free_database(
        database,
        label="compaction work database",
    )


def _capture_work_database_authority(
    database: Path,
    *,
    expected_identity: DatabaseFileIdentity | None = None,
    hash_phase: str | None,
    authority_path: Path | None = None,
) -> tuple[DatabaseFileIdentity, dict[str, Any]]:
    """Capture one stable work-file identity, with a hash only when durable."""

    try:
        identity = require_unique_database_file(
            database,
            expected_identity=expected_identity,
        )
        assert identity is not None
        if hash_phase is not None:
            _require_stable_work_database(database)
        before = database.stat()
        digest = _sha256_file(database) if hash_phase is not None else None
        require_unique_database_file(database, expected_identity=identity)
        if hash_phase is not None:
            _require_stable_work_database(database)
        after = database.stat()
        if hash_phase is not None and _sha256_file(database) != digest:
            raise RuntimeError("work database hash changed during authority capture")
    except (OSError, RuntimeError) as error:
        raise _WorkDatabaseAuthorityError(
            f"compaction work database authority is invalid: {error}"
        ) from error
    if int(before.st_size) != int(after.st_size):
        raise _WorkDatabaseAuthorityError(
            "compaction work database size changed during authority capture"
        )
    return identity, {
        "resolved_path": str(
            identity.resolved_path
            if authority_path is None
            else authority_path.resolve()
        ),
        "device": identity.device,
        "inode": identity.inode,
        "bytes": int(after.st_size),
        "sha256": digest,
        "hash_phase": hash_phase,
    }


def _recorded_work_database_authority(
    database: Path,
    manifest: Mapping[str, Any],
) -> tuple[DatabaseFileIdentity, Mapping[str, Any]]:
    raw = manifest.get(_WORK_DATABASE_AUTHORITY)
    if not isinstance(raw, Mapping):
        raise _WorkDatabaseAuthorityError(
            "compaction checkpoint is missing work database authority"
        )
    resolved = database.resolve()
    if raw.get("resolved_path") != str(resolved):
        raise _WorkDatabaseAuthorityError(
            "compaction work database resolved path differs from checkpoint"
        )
    device = raw.get("device")
    inode = raw.get("inode")
    size = raw.get("bytes")
    if (
        type(device) is not int
        or type(inode) is not int
        or int(inode) <= 0
        or type(size) is not int
        or int(size) < 0
    ):
        raise _WorkDatabaseAuthorityError(
            "compaction checkpoint work database identity is invalid"
        )
    digest = raw.get("sha256")
    hash_phase = raw.get("hash_phase")
    if digest is None:
        if hash_phase is not None:
            raise _WorkDatabaseAuthorityError(
                "compaction checkpoint work hash phase is invalid"
            )
    elif (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(hash_phase, str)
        or not hash_phase
    ):
        raise _WorkDatabaseAuthorityError(
            "compaction checkpoint work database hash is invalid"
        )
    return DatabaseFileIdentity(resolved, int(device), int(inode)), raw


def _require_work_database_authority(
    database: Path,
    manifest: Mapping[str, Any],
    *,
    verify_available_hash: bool = True,
    authority_path: Path | None = None,
) -> DatabaseFileIdentity:
    """Fence the work path; verify bytes/hash when the phase made them durable."""

    recorded_identity, recorded = _recorded_work_database_authority(
        database if authority_path is None else authority_path,
        manifest,
    )
    expected = DatabaseFileIdentity(
        database.resolve(),
        recorded_identity.device,
        recorded_identity.inode,
    )
    try:
        require_unique_database_file(database, expected_identity=expected)
        digest = recorded.get("sha256")
        if verify_available_hash and digest is not None:
            _require_stable_work_database(database)
            if database.stat().st_size != int(recorded["bytes"]):
                raise RuntimeError("work database size differs from checkpoint")
            if _sha256_file(database) != digest:
                raise RuntimeError("work database hash differs from checkpoint")
            require_unique_database_file(database, expected_identity=expected)
            _require_stable_work_database(database)
            if database.stat().st_size != int(recorded["bytes"]):
                raise RuntimeError(
                    "work database size changed during checkpoint verification"
                )
            if _sha256_file(database) != digest:
                raise RuntimeError(
                    "work database hash changed during checkpoint verification"
                )
    except (OSError, RuntimeError) as error:
        raise _WorkDatabaseAuthorityError(
            f"compaction work database authority changed: {error}"
        ) from error
    return expected


def _remove_ready_work_database(
    database: Path,
    manifest: Mapping[str, Any],
) -> bool:
    if _WORK_DATABASE_AUTHORITY not in manifest:
        return False
    if database.is_symlink():
        raise _WorkDatabaseAuthorityError(
            "ready compaction work database must not be a symlink"
        )
    if not database.exists():
        missing_main_sidecars = sqlite_sidecar_state(database)
        if any(
            bool(record["exists"])
            for record in missing_main_sidecars.values()
        ):
            raise _WorkDatabaseAuthorityError(
                "ready compaction work database is missing while sidecars remain"
            )
        return True
    identity = _require_work_database_authority(
        database,
        manifest,
    )
    sidecars = sqlite_sidecar_state(database)
    if any(int(record["bytes"]) for record in sidecars.values()):
        raise _WorkDatabaseAuthorityError(
            "ready compaction work database has non-empty SQLite sidecars"
        )
    sidecar_paths = [
        _require_recorded_sidecar_authority(record)
        for record in sidecars.values()
    ]
    for record, path in zip(sidecars.values(), sidecar_paths, strict=True):
        if path is not None:
            _require_recorded_sidecar_authority(record)
            _quarantine_verified_file(
                path,
                expected_device=int(record["device"]),
                expected_inode=int(record["inode"]),
                expected_bytes=int(record["bytes"]),
                expected_sha256=hashlib.sha256(b"").hexdigest(),
                label="ready compaction work sidecar",
            )
    if database.is_symlink():
        raise _WorkDatabaseAuthorityError(
            "ready compaction work database became a symlink"
        )
    completed_identity = _require_work_database_authority(database, manifest)
    if completed_identity != identity:
        raise _WorkDatabaseAuthorityError(
            "ready compaction work database identity changed during cleanup"
        )
    remaining_sidecars = sqlite_sidecar_state(database)
    if any(bool(record["exists"]) for record in remaining_sidecars.values()):
        raise _WorkDatabaseAuthorityError(
            "ready compaction work database sidecars reappeared during cleanup"
        )
    _, recorded = _recorded_work_database_authority(database, manifest)
    recorded_hash = recorded.get("sha256")
    if not isinstance(recorded_hash, str) or len(recorded_hash) != 64:
        raise _WorkDatabaseAuthorityError(
            "ready compaction work database hash authority is missing"
        )
    _quarantine_verified_file(
        database,
        expected_device=identity.device,
        expected_inode=identity.inode,
        expected_bytes=int(recorded["bytes"]),
        expected_sha256=recorded_hash,
        label="ready compaction work database",
        database=True,
    )
    if database.is_symlink() or database.exists():
        raise RuntimeError("ready compaction work database could not be removed")
    completed = sqlite_sidecar_state(database)
    if any(bool(record["exists"]) for record in completed.values()):
        raise RuntimeError("ready compaction work sidecars could not be removed")
    return True


def _require_recorded_sidecar_authority(
    record: Mapping[str, int | bool | str | None],
) -> Path | None:
    path = Path(str(record["path"]))
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if bool(record["exists"]):
            raise _WorkDatabaseAuthorityError(
                "ready compaction work sidecar disappeared during cleanup"
            )
        return None
    if not bool(record["exists"]):
        raise _WorkDatabaseAuthorityError(
            "ready compaction work sidecar appeared during cleanup"
        )
    if not stat.S_ISREG(metadata.st_mode) or int(metadata.st_nlink) != 1:
        raise _WorkDatabaseAuthorityError(
            "ready compaction work sidecar authority is unsafe"
        )
    expected = (
        int(record["device"]),
        int(record["inode"]),
        int(record["mtime_ns"]),
        int(record["bytes"]),
    )
    actual = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mtime_ns),
        int(metadata.st_size),
    )
    if actual != expected:
        raise _WorkDatabaseAuthorityError(
            "ready compaction work sidecar authority changed during cleanup"
        )
    return path


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _capture_manifest_authority(path: Path) -> dict[str, Any]:
    snapshot = read_hashed_file(
        path,
        label="compaction manifest",
        include_payload=False,
    )
    return {
        "resolved_path": str(snapshot.resolved_path),
        "device": snapshot.device,
        "inode": snapshot.inode,
        "bytes": snapshot.bytes,
        "sha256": snapshot.sha256,
    }


def _require_manifest_authority(
    path: Path,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    current = _capture_manifest_authority(path)
    if any(current.get(field) != expected.get(field) for field in (
        "resolved_path",
        "device",
        "inode",
        "bytes",
        "sha256",
    )):
        raise RuntimeError("compaction manifest file authority changed")
    return current


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = _canonical_json(manifest) + b"\n"
    previous: dict[str, Any] | None = None
    if path.exists() or path.is_symlink():
        previous = _capture_manifest_authority(path)
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_authority = _capture_manifest_authority(temporary)
        if temporary_authority["bytes"] != len(encoded) or temporary_authority[
            "sha256"
        ] != hashlib.sha256(encoded).hexdigest():
            raise RuntimeError("compaction manifest temporary file authority changed")
        if previous is not None:
            _require_manifest_authority(path, previous)
        elif path.exists() or path.is_symlink():
            raise RuntimeError("compaction manifest appeared before publication")
        _replace_and_fsync(temporary, path)
        current = _capture_manifest_authority(path)
        if current["bytes"] != len(encoded) or current["sha256"] != temporary_authority[
            "sha256"
        ]:
            raise RuntimeError("compaction manifest authority changed after publication")
    finally:
        _unlink_and_fsync(temporary, missing_ok=True)


def _checkpoint_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    status: str | None = None,
    phase: str | None = None,
    _authority: _ManifestAuthority | None = None,
    **values: Any,
) -> dict[str, Any]:
    if _authority is not None and _authority.value is not None:
        _require_manifest_authority(path, _authority.value)
    if status is not None:
        manifest["status"] = status
    if phase is not None:
        manifest["phase"] = phase
        manifest["phase_started_at"] = _utc_now()
    manifest.update(values)
    manifest["heartbeat_at"] = _utc_now()
    _write_manifest(path, manifest)
    refreshed = _capture_manifest_authority(path)
    if _authority is not None:
        _authority.value = refreshed
    return refreshed


def _read_manifest_with_authority(
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = read_hashed_file(
        path,
        label="compaction manifest",
        include_payload=True,
    )
    authority = {
        "resolved_path": str(snapshot.resolved_path),
        "device": snapshot.device,
        "inode": snapshot.inode,
        "bytes": snapshot.bytes,
        "sha256": snapshot.sha256,
    }
    assert snapshot.payload is not None
    try:
        value = json.loads(snapshot.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("compaction checkpoint manifest is invalid") from error
    _require_manifest_authority(path, authority)
    if not isinstance(value, dict) or value.get("format") != _MANIFEST_FORMAT:
        raise RuntimeError("compaction checkpoint manifest has the wrong format")
    return value, authority


def _read_manifest(path: Path) -> dict[str, Any]:
    value, _ = _read_manifest_with_authority(path)
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
            pass
        else:
            raise ValueError("source paths must be outside the compaction destination")
        try:
            destination_root.relative_to(source)
        except ValueError:
            continue
        raise ValueError("compaction destination must be outside source paths")
    if source_database == destination_root:
        raise ValueError("source database and destination must be distinct")


def _require_offline_checkpointed_source(database: Path) -> None:
    _require_transaction_free_database(database, label="source database")


def _registered_raw_artifact_count(
    database: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> int:
    with _checkpoint_reader(
        database,
        label="compaction source raw artifact count",
        required_locks=required_locks,
    ) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM odds_raw_artifacts").fetchone()[0]
        )


def _preflight_space(
    source_database: Path,
    destination_root: Path,
    *,
    work_database: Path,
    output_database: Path,
    raw_root: Path,
    required_locks: Sequence[Path] = (),
) -> None:
    with _checkpoint_reader(
        source_database,
        label="compaction preflight source",
        required_locks=required_locks,
    ) as connection:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        registered_raw = 0
        present_source_raw = 0
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odds_raw_artifacts'"
        ).fetchone():
            for storage_path, compressed_bytes in connection.execute(
                "SELECT storage_path, compressed_bytes FROM odds_raw_artifacts"
            ):
                expected_bytes = int(compressed_bytes)
                registered_raw += expected_bytes
                destination = _contained_path(raw_root, Path(str(storage_path)))
                if destination.is_file() and destination.stat().st_size == expected_bytes:
                    present_source_raw += expected_bytes

    logical_bytes = page_count * page_size
    present_generated_raw = 0
    work_allocated = 0
    remaining_legacy = 1
    if work_database.is_file():
        work_allocated = max(work_database.stat().st_size, logical_bytes)
        with (
            _checkpoint_reader(
                source_database,
                label="compaction preflight source artifacts",
                required_locks=required_locks,
            ) as source,
            _checkpoint_reader(
                work_database,
                label="compaction preflight sealed work database",
                required_locks=required_locks,
            ) as work,
        ):
            remaining_legacy = int(
                work.execute(
                    """SELECT COUNT(*) FROM odds_transport_observations
                         WHERE response_state_hash IS NULL
                           AND response_artifact_hash IS NULL"""
                ).fetchone()[0]
            )
            source_hashes = iter(
                source.execute(
                    "SELECT artifact_hash FROM odds_raw_artifacts ORDER BY artifact_hash"
                )
            )
            source_row = next(source_hashes, None)
            source_hash = None if source_row is None else str(source_row[0])
            for artifact_hash, storage_path, compressed_bytes in work.execute(
                """SELECT artifact_hash, storage_path, compressed_bytes
                     FROM odds_raw_artifacts ORDER BY artifact_hash"""
            ):
                work_hash = str(artifact_hash)
                while source_hash is not None and source_hash < work_hash:
                    source_row = next(source_hashes, None)
                    source_hash = None if source_row is None else str(source_row[0])
                if source_hash == work_hash:
                    continue
                expected_bytes = int(compressed_bytes)
                destination = _contained_path(raw_root, Path(str(storage_path)))
                if destination.is_file() and destination.stat().st_size == expected_bytes:
                    present_generated_raw += expected_bytes

    # While legacy and v2 rows coexist, conversion can consume another source-sized
    # extent.  VACUUM may then need one extent as large as that high-water mark.
    # On resume, completed conversion removes the extra growth reserve.
    conversion_growth = logical_bytes if remaining_legacy else 0
    work_upper_bound = max(work_allocated, logical_bytes) + conversion_growth
    missing_work = max(0, work_upper_bound - work_allocated)
    missing_output = 0 if output_database.is_file() else work_upper_bound
    missing_source_raw = registered_raw - present_source_raw
    remaining_generated_raw = (
        max(0, logical_bytes - present_generated_raw)
        if remaining_legacy
        else 0
    )
    required = (
        missing_work
        + missing_output
        + missing_source_raw
        + remaining_generated_raw
        + _SAFETY_MARGIN_BYTES
    )
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
) -> bytes:
    snapshot = read_hashed_file(
        path,
        label="raw artifact",
        include_payload=True,
    )
    assert snapshot.payload is not None
    if snapshot.bytes != compressed_bytes:
        raise RuntimeError(f"raw artifact compressed size mismatch: {path}")
    try:
        canonical = gzip.decompress(snapshot.payload)
        payload = json.loads(canonical)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"raw artifact is corrupt: {path}") from error
    if len(canonical) != uncompressed_bytes:
        raise RuntimeError(f"raw artifact byte count mismatch: {path}")
    if hashlib.sha256(canonical).hexdigest() != content_hash:
        raise RuntimeError(f"raw artifact hash mismatch: {path}")
    if schema_fingerprint(payload) != fingerprint:
        raise RuntimeError(f"raw artifact schema fingerprint mismatch: {path}")
    confirmed = read_hashed_file(
        path,
        label="raw artifact",
        include_payload=False,
    )
    if any(
        getattr(confirmed, field) != getattr(snapshot, field)
        for field in (
            "resolved_path",
            "device",
            "inode",
            "mode",
            "bytes",
            "mtime_ns",
            "sha256",
        )
    ):
        raise RuntimeError(f"raw artifact authority changed: {path}")
    return canonical


def _copy_existing_artifacts(
    database: Path,
    source_root: Path,
    destination_root: Path,
    *,
    progress: Callable[[int], None] | None = None,
    required_locks: Sequence[Path] = (),
) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    with _checkpoint_reader(
        database,
        label="initialized compaction work database",
        required_locks=required_locks,
        row_factory=sqlite3.Row,
    ) as connection:
        rows = connection.execute(
            """SELECT artifact_hash, storage_path, uncompressed_bytes,
                      compressed_bytes, schema_fingerprint
                 FROM odds_raw_artifacts ORDER BY artifact_hash"""
        )
        copied = 0
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
            source_canonical = _verify_artifact_file(source, **expected)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if _verify_artifact_file(destination, **expected) != source_canonical:
                    raise RuntimeError(
                        "source and copied raw artifacts differ byte-for-byte"
                    )
            else:
                temporary = destination.with_name(
                    f".{destination.name}.{os.getpid()}.tmp"
                )
                try:
                    shutil.copy2(source, temporary)
                    if _verify_artifact_file(temporary, **expected) != source_canonical:
                        raise RuntimeError("copied raw artifact differs from source")
                    _replace_and_fsync(temporary, destination)
                finally:
                    _unlink_and_fsync(temporary, missing_ok=True)
            copied += 1
            if progress is not None and copied % _PROGRESS_BATCH_SIZE == 0:
                progress(copied)
        if progress is not None:
            progress(copied)


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


def _group_authority(group: _LegacyGroup) -> _GroupAuthority:
    state_values: list[Sequence[Any]] = []
    raw_outcomes: list[Mapping[str, Any]] = []
    snapshots: list[OddsSnapshot] = []
    seen: set[str] = set()
    observed_at = _parse_aware(group.observed_at)
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
                received_at=observed_at,
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
    state_hash, state_outcomes, state_manifest = response_state_identity(
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
        state_manifest,
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
    verified_states: _BoundedFingerprintCache,
) -> None:
    fingerprint = _secondary_fingerprint(authority.state_manifest_bytes)
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
        cached = verified_states.get(authority.state_hash)
        if cached is not None and cached != fingerprint:
            raise RuntimeError("response state hash maps to different manifests")
        rows_match = True
        if cached is None:
            rows_match = (
                _state_rows(connection, authority.state_hash)
                == authority.state_outcomes
            )
            if rows_match:
                verified_states.add(authority.state_hash, fingerprint)
        if identity != expected or not rows_match:
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
    verified_states.add(authority.state_hash, fingerprint)


def _persist_artifact(
    connection: sqlite3.Connection,
    raw_archive: RawArchive,
    raw_root: Path,
    group: _LegacyGroup,
    authority: _GroupAuthority,
    verified_artifacts: _BoundedFingerprintCache,
) -> None:
    fingerprint = _secondary_fingerprint(authority.artifact_bytes)
    existing = connection.execute(
        """SELECT source, storage_path, uncompressed_bytes, compressed_bytes,
                  schema_fingerprint
             FROM odds_raw_artifacts WHERE artifact_hash=?""",
        (authority.artifact_hash,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != "raybet" or int(existing[2]) != len(
            authority.artifact_bytes
        ):
            raise RuntimeError("raw artifact hash or metadata collision")
        cached = verified_artifacts.get(authority.artifact_hash)
        if cached is not None and cached != fingerprint:
            raise RuntimeError("raw artifact hash maps to different payloads")
        if cached is None:
            path = _contained_path(raw_root, Path(str(existing[1])))
            canonical = _verify_artifact_file(
                path,
                content_hash=authority.artifact_hash,
                uncompressed_bytes=int(existing[2]),
                compressed_bytes=int(existing[3]),
                fingerprint=str(existing[4]),
            )
            if canonical != authority.artifact_bytes:
                raise RuntimeError("raw artifact hash maps to different payloads")
            verified_artifacts.add(authority.artifact_hash, fingerprint)
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
    verified_artifacts.add(authority.artifact_hash, fingerprint)


def _convert_group(
    connection: sqlite3.Connection,
    raw_archive: RawArchive,
    raw_root: Path,
    group: _LegacyGroup,
    verified_states: _BoundedFingerprintCache,
    verified_artifacts: _BoundedFingerprintCache,
) -> None:
    if not connection.in_transaction:
        raise RuntimeError("response conversion requires an active batch transaction")
    authority = _group_authority(group)
    _persist_state(connection, group, authority, verified_states)
    _persist_artifact(
        connection,
        raw_archive,
        raw_root,
        group,
        authority,
        verified_artifacts,
    )
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


def _acquire_compaction_connection(
    database: Path,
    *,
    authority_check: Callable[[], None] | None = None,
) -> sqlite3.Connection:
    check = authority_check or (lambda: None)
    check()
    connection = connect(database, row_factory=sqlite3.Row)
    try:
        check()
        mode = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
        if mode is None or str(mode[0]).casefold() != "exclusive":
            raise RuntimeError("failed to acquire exclusive compaction mode")
        check()
        connection.execute("BEGIN EXCLUSIVE")
        connection.commit()
        check()
        partial = int(
            connection.execute(
                """SELECT COUNT(*) FROM odds_transport_observations
                    WHERE (response_state_hash IS NULL)
                       != (response_artifact_hash IS NULL)"""
            ).fetchone()[0]
        )
        if partial:
            raise RuntimeError(
                "transport observation has partial v2 storage references"
            )
        check()
        connection.execute(
            "DROP TRIGGER IF EXISTS odds_transport_observations_guard_update"
        )
        connection.commit()
        check()
        return connection
    except BaseException:
        connection.close()
        raise


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
    *,
    progress: Callable[[int], None] | None = None,
    required_locks: Sequence[Path] = (),
) -> tuple[int, int, str]:
    observation_count = 0
    outcome_count = 0
    root_digest = hashlib.sha256()
    verified_states = _BoundedFingerprintCache()
    with (
        _checkpoint_reader(
            source_database,
            label="compaction equivalence source",
            required_locks=required_locks,
            row_factory=sqlite3.Row,
        ) as source,
        _checkpoint_reader(
            converted_database,
            label="compaction equivalence sealed database",
            required_locks=required_locks,
            row_factory=sqlite3.Row,
        ) as converted,
    ):
        for group in _iter_legacy_groups(source):
            authority = _group_authority(group)
            state_fingerprint = _secondary_fingerprint(
                authority.state_manifest_bytes
            )
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
            cached_state = verified_states.get(authority.state_hash)
            if cached_state is not None and cached_state != state_fingerprint:
                raise RuntimeError(
                    "legacy state hash maps to different canonical manifests"
                )
            if cached_state is None:
                if (
                    _state_rows(converted, authority.state_hash)
                    != authority.state_outcomes
                ):
                    raise RuntimeError(
                        "converted response members differ from legacy data"
                    )
                verified_states.add(authority.state_hash, state_fingerprint)
            root_digest.update(
                hashlib.sha256(
                    _canonical_json(_group_equivalence_payload(group, authority))
                ).digest()
            )
            observation_count += 1
            outcome_count += len(group.rows)
            if (
                progress is not None
                and observation_count % _PROGRESS_BATCH_SIZE == 0
            ):
                progress(observation_count)

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
        if progress is not None:
            progress(observation_count)
    return observation_count, outcome_count, root_digest.hexdigest()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _digest_value(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", value.hex()]
    raise RuntimeError(f"unsupported SQLite value type: {type(value).__name__}")


def _table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    quoted = _quoted_identifier(table)
    return tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_xinfo({quoted})")
        if int(row[6]) == 0
    )


def _table_content_digest(
    connection: sqlite3.Connection,
    table: str,
    *,
    columns: Sequence[str] | None = None,
) -> tuple[int, str]:
    selected = tuple(columns or _table_columns(connection, table))
    if not selected:
        raise RuntimeError(f"table has no digestible columns: {table}")
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    without_rowid = (
        table_sql is not None
        and table_sql[0] is not None
        and "WITHOUT ROWID" in str(table_sql[0]).upper()
    )
    digest_columns = selected
    if without_rowid:
        primary_key = tuple(
            str(row[1])
            for row in sorted(
                (
                    row
                    for row in connection.execute(
                        f"PRAGMA table_xinfo({_quoted_identifier(table)})"
                    )
                    if int(row[5]) > 0
                ),
                key=lambda row: int(row[5]),
            )
        )
        if not primary_key:
            raise RuntimeError(f"WITHOUT ROWID table has no primary key: {table}")
        projection = ", ".join(
            _quoted_identifier(column) for column in selected
        )
        order = ", ".join(
            _quoted_identifier(column) for column in primary_key
        )
    else:
        declared = {column.casefold() for column in _table_columns(connection, table)}
        rowid = next(
            (
                candidate
                for candidate in ("_rowid_", "rowid", "oid")
                if candidate not in declared
            ),
            None,
        )
        if rowid is None:
            raise RuntimeError(f"rowid table shadows every rowid alias: {table}")
        projection = rowid + ", " + ", ".join(
            _quoted_identifier(column) for column in selected
        )
        order = rowid
        digest_columns = (rowid, *selected)
    sql = f"SELECT {projection} FROM {_quoted_identifier(table)}"
    sql += " ORDER BY " + order
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(sql):
        if len(row) != len(digest_columns):
            raise RuntimeError(f"table digest projection drifted: {table}")
        digest.update(_canonical_json([_digest_value(value) for value in row]))
        count += 1
    return count, digest.hexdigest()


def _processing_schema_contract(
    connection: sqlite3.Connection,
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            """SELECT type, name, tbl_name, sql
                 FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                  AND name != 'odds_transport_observations_guard_update'
                ORDER BY type, name"""
        )
    )


def _verify_processing_resume(
    source_database: Path,
    work_database: Path,
    raw_root: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> None:
    """Prove a hash-unavailable converting checkpoint before reopening RW."""

    with (
        _checkpoint_reader(
            source_database,
            label="processing resume source",
            required_locks=required_locks,
            row_factory=sqlite3.Row,
        ) as source,
        _checkpoint_reader(
            work_database,
            label="processing resume sealed work database",
            required_locks=required_locks,
            row_factory=sqlite3.Row,
        ) as work,
    ):
        integrity = work.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("processing work database failed integrity_check")
        if work.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("processing work database failed foreign_key_check")
        if _processing_schema_contract(source) != _processing_schema_contract(work):
            raise RuntimeError("processing work database changed preserved schema")

        source_tables = {
            str(row[0])
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        }
        work_tables = {
            str(row[0])
            for row in work.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not str(row[0]).startswith("sqlite_")
        }
        if source_tables != work_tables:
            raise RuntimeError("processing work database table set differs from source")
        for table in sorted(source_tables - _PROCESSING_MUTATED_TABLES):
            if _table_columns(source, table) != _table_columns(work, table):
                raise RuntimeError(
                    f"processing work database columns differ for table {table}"
                )
            if _table_content_digest(source, table) != _table_content_digest(
                work, table
            ):
                raise RuntimeError(
                    f"processing work database changed preserved table {table}"
                )

        transport_columns = _table_columns(source, "odds_transport_observations")
        if transport_columns != _table_columns(work, "odds_transport_observations"):
            raise RuntimeError("processing transport columns differ from source")
        preserved_transport = tuple(
            column
            for column in transport_columns
            if column not in _TRANSPORT_CONVERSION_COLUMNS
        )
        if _table_content_digest(
            source,
            "odds_transport_observations",
            columns=preserved_transport,
        ) != _table_content_digest(
            work,
            "odds_transport_observations",
            columns=preserved_transport,
        ):
            raise RuntimeError("processing transport preserved fields differ from source")
        preexisting_authority_columns = tuple(
            column
            for column in transport_columns
            if column in _TRANSPORT_CONVERSION_COLUMNS
        )
        preexisting_projection = ", ".join(
            _quoted_identifier(column)
            for column in ("observation_key", *preexisting_authority_columns)
        )
        for source_row in source.execute(
            f"SELECT {preexisting_projection} "
            "FROM odds_transport_observations "
            "WHERE response_state_hash IS NOT NULL "
            "OR response_artifact_hash IS NOT NULL "
            "ORDER BY observation_key"
        ):
            target = work.execute(
                "SELECT "
                + ", ".join(
                    _quoted_identifier(column)
                    for column in preexisting_authority_columns
                )
                + " FROM odds_transport_observations WHERE observation_key=?",
                (source_row[0],),
            ).fetchone()
            if target is None or tuple(target) != tuple(source_row[1:]):
                raise RuntimeError(
                    "processing preexisting transport authority differs from source"
                )

        expected_states = {
            str(row[0])
            for row in source.execute(
                "SELECT response_state_hash FROM odds_response_states"
            )
        }
        expected_artifacts = {
            str(row[0])
            for row in source.execute(
                "SELECT artifact_hash FROM odds_raw_artifacts"
            )
        }
        verified_states = _BoundedFingerprintCache()
        verified_artifacts = _BoundedFingerprintCache()
        for group in _iter_legacy_groups(source):
            source_refs = source.execute(
                """SELECT normalized_state_hash,
                          normalized_state_hash_version,
                          original_legacy_normalized_state_hash,
                          response_state_hash, response_artifact_hash
                     FROM odds_transport_observations
                    WHERE observation_key=?""",
                (group.observation_key,),
            ).fetchone()
            target = work.execute(
                """SELECT source, source_event_id, raybet_match_id, observed_at,
                          normalized_state_hash, normalized_state_hash_version,
                          original_legacy_normalized_state_hash, timing_status,
                          processing_status, normalized_change_count,
                          response_state_hash, response_artifact_hash
                     FROM odds_transport_observations WHERE observation_key=?""",
                (group.observation_key,),
            ).fetchone()
            if source_refs is None or target is None:
                raise RuntimeError(
                    "processing work database is missing a transport observation"
                )
            state_ref = target[10]
            artifact_ref = target[11]
            if (state_ref is None) != (artifact_ref is None):
                raise RuntimeError(
                    "processing transport has partial authority references"
                )
            if state_ref is None:
                if tuple(target[4:7]) + (target[10], target[11]) != tuple(
                    source_refs
                ):
                    raise RuntimeError(
                        "unconverted processing transport differs from source"
                    )
                continue

            authority = _group_authority(group)
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
                authority.state_hash,
                authority.artifact_hash,
            )
            if tuple(target) != expected_transport:
                raise RuntimeError(
                    "converted processing transport differs from legacy authority"
                )
            fingerprint = _secondary_fingerprint(authority.state_manifest_bytes)
            cached = verified_states.get(authority.state_hash)
            if cached is not None and cached != fingerprint:
                raise RuntimeError(
                    "processing state hash maps to different canonical manifests"
                )
            if cached is None:
                if _state_rows(work, authority.state_hash) != authority.state_outcomes:
                    raise RuntimeError(
                        "processing response members differ from legacy data"
                    )
                verified_states.add(authority.state_hash, fingerprint)
            artifact_fingerprint = _secondary_fingerprint(
                authority.artifact_bytes
            )
            cached_artifact = verified_artifacts.get(authority.artifact_hash)
            if (
                cached_artifact is not None
                and cached_artifact != artifact_fingerprint
            ):
                raise RuntimeError(
                    "processing artifact hash maps to different canonical payloads"
                )
            if cached_artifact is None:
                artifact = work.execute(
                    """SELECT source, storage_path, uncompressed_bytes,
                              compressed_bytes, schema_fingerprint
                         FROM odds_raw_artifacts WHERE artifact_hash=?""",
                    (authority.artifact_hash,),
                ).fetchone()
                source_artifact = source.execute(
                    """SELECT source, storage_path, uncompressed_bytes,
                              compressed_bytes, schema_fingerprint
                         FROM odds_raw_artifacts WHERE artifact_hash=?""",
                    (authority.artifact_hash,),
                ).fetchone()
                expected_path = (
                    str(source_artifact[1])
                    if source_artifact is not None
                    else (
                        Path("raybet")
                        / authority.artifact_hash[:2]
                        / f"{authority.artifact_hash}.json.gz"
                    ).as_posix()
                )
                if (
                    artifact is None
                    or str(artifact[0]) != "raybet"
                    or str(artifact[1]) != expected_path
                    or int(artifact[2]) != len(authority.artifact_bytes)
                    or str(artifact[4])
                    != schema_fingerprint(authority.artifact_payload)
                ):
                    raise RuntimeError(
                        "processing raw artifact metadata differs from authority"
                    )
                if source_artifact is not None and tuple(artifact) != tuple(
                    source_artifact
                ):
                    raise RuntimeError(
                        "processing source artifact metadata changed"
                    )
                canonical = _verify_artifact_file(
                    _contained_path(raw_root, Path(str(artifact[1]))),
                    content_hash=authority.artifact_hash,
                    uncompressed_bytes=int(artifact[2]),
                    compressed_bytes=int(artifact[3]),
                    fingerprint=str(artifact[4]),
                )
                if canonical != authority.artifact_bytes:
                    raise RuntimeError(
                        "processing raw artifact differs from canonical payload"
                    )
                verified_artifacts.add(
                    authority.artifact_hash,
                    artifact_fingerprint,
                )
            expected_states.add(authority.state_hash)
            expected_artifacts.add(authority.artifact_hash)

        actual_states = {
            str(row[0])
            for row in work.execute(
                "SELECT response_state_hash FROM odds_response_states"
            )
        }
        actual_artifacts = {
            str(row[0])
            for row in work.execute("SELECT artifact_hash FROM odds_raw_artifacts")
        }
        if actual_states != expected_states:
            raise RuntimeError("processing response state set differs from authority")
        if actual_artifacts != expected_artifacts:
            raise RuntimeError("processing raw artifact set differs from authority")

        for state in work.execute(
            """SELECT response_state_hash, raybet_match_id, normalized_state_hash,
                      normalized_state_hash_version, outcome_count
                 FROM odds_response_states ORDER BY response_state_hash"""
        ):
            rows = _state_rows(work, str(state[0]))
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
                raise RuntimeError(
                    "processing response state fails canonical verification"
                )
        for artifact in work.execute(
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
def _restore_final_schema(
    connection: sqlite3.Connection,
    *,
    authority_check: Callable[[], None] | None = None,
) -> None:
    check = authority_check or (lambda: None)
    check()
    connection.execute("BEGIN EXCLUSIVE")
    try:
        check()
        connection.execute("DROP VIEW IF EXISTS odds_response_outcomes_effective")
        connection.execute("DROP TABLE odds_response_outcomes")
        execute_script(connection, SCHEMA_SQL)
        connection.commit()
        check()
    except BaseException:
        connection.rollback()
        raise


def _verify_sqlite_output(
    database: Path,
    raw_root: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> None:
    _require_transaction_free_database(database, label="compacted database")
    verify_prepared_database(
        database,
        odds_raw_root=raw_root,
        core_only=True,
        immutable_locks=required_locks or None,
    )
    with _checkpoint_reader(
        database,
        label="compacted database verification",
        required_locks=required_locks,
    ) as connection:
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
    _require_transaction_free_database(database, label="compacted database")


def _verify_final_schema_resume(
    source_database: Path,
    work_database: Path,
    raw_root: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> None:
    """Accept either atomic side of a crash around final-schema commit."""

    try:
        _verify_sqlite_output(
            work_database,
            raw_root,
            required_locks=required_locks,
        )
    except Exception as final_schema_error:
        try:
            _verify_processing_resume(
                source_database,
                work_database,
                raw_root,
                required_locks=required_locks,
            )
        except Exception as processing_error:
            raise RuntimeError(
                "final-schema resume matches neither durable schema state: "
                f"final={type(final_schema_error).__name__}:{final_schema_error}; "
                f"processing={type(processing_error).__name__}:{processing_error}"
            ) from processing_error
    else:
        _validate_equivalence(
            source_database,
            work_database,
            raw_root,
            required_locks=required_locks,
        )


def _verify_published_output(
    database: Path,
    raw_root: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
    required_locks: Sequence[Path] = (),
) -> DatabaseFileIdentity:
    _clear_quiescent_sqlite_sidecars(
        database,
        label="published compaction output",
    )
    identity = require_unique_database_file(database)
    assert identity is not None
    if database.stat().st_size != expected_bytes:
        raise RuntimeError("published compaction output size mismatch")
    _verify_sqlite_output(
        database,
        raw_root,
        required_locks=required_locks,
    )
    require_unique_database_file(database, expected_identity=identity)
    if _sha256_file(database) != expected_sha256:
        raise RuntimeError("published compaction output hash mismatch")
    require_unique_database_file(database, expected_identity=identity)
    _clear_quiescent_sqlite_sidecars(
        database,
        label="published compaction output",
    )
    require_unique_database_file(database, expected_identity=identity)
    if database.stat().st_size != expected_bytes:
        raise RuntimeError("published compaction output size changed during verification")
    return identity


def _require_manifest_layout(manifest: Mapping[str, Any]) -> None:
    expected = {
        "output_database": _OUTPUT_DATABASE,
        "raw_root": _RAW_ROOT.as_posix(),
    }
    for field, controlled_path in expected.items():
        if manifest.get(field) != controlled_path:
            raise RuntimeError(
                f"compaction checkpoint {field} is not the controlled path"
            )
    if manifest.get("work_database") not in {
        _WORK_DATABASE,
        _WORK_DATABASE_PATH.as_posix(),
    }:
        raise RuntimeError(
            "compaction checkpoint work_database is not the controlled path"
        )


def _manifest_nonnegative_int(manifest: Mapping[str, Any], field: str) -> int:
    value = manifest.get(field)
    if type(value) is not int or value < 0:
        raise RuntimeError(f"compaction checkpoint {field} is invalid")
    return value


def _result_from_manifest(root: Path, manifest: Mapping[str, Any]) -> CompactionResult:
    _require_manifest_layout(manifest)
    return CompactionResult(
        output_database=_contained_path(root, Path(str(manifest["output_database"]))),
        raw_root=_contained_path(root, Path(str(manifest["raw_root"]))),
        source_sha256=str(manifest["source_sha256"]),
        output_sha256=str(manifest["output_sha256"]),
        observation_count=_manifest_nonnegative_int(manifest, "observation_count"),
        outcome_count=_manifest_nonnegative_int(manifest, "outcome_count"),
        state_count=_manifest_nonnegative_int(manifest, "state_count"),
        artifact_count=_manifest_nonnegative_int(manifest, "artifact_count"),
        equivalence_sha256=str(manifest["equivalence_sha256"]),
        source_bytes=_manifest_nonnegative_int(manifest, "source_bytes"),
        output_bytes=_manifest_nonnegative_int(manifest, "output_bytes"),
    )


def _verify_manifest_result_authority(
    source_database: Path,
    result: CompactionResult,
    *,
    required_locks: Sequence[Path] = (),
) -> None:
    observation_count, outcome_count, equivalence_sha256 = _validate_equivalence(
        source_database,
        result.output_database,
        result.raw_root,
        required_locks=required_locks,
    )
    if (
        observation_count != result.observation_count
        or outcome_count != result.outcome_count
        or equivalence_sha256 != result.equivalence_sha256
    ):
        raise RuntimeError(
            "compaction checkpoint equivalence differs from published output"
        )
    with _checkpoint_reader(
        result.output_database,
        label="published compaction result",
        required_locks=required_locks,
    ) as connection:
        state_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM odds_response_states"
            ).fetchone()[0]
        )
        artifact_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM odds_raw_artifacts"
            ).fetchone()[0]
        )
    if state_count != result.state_count or artifact_count != result.artifact_count:
        raise RuntimeError(
            "compaction checkpoint counts differ from published output"
        )


def _legacy_observation_count(
    database: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> int:
    with _checkpoint_reader(
        database,
        label="legacy observation count",
        required_locks=required_locks,
    ) as connection:
        return int(
            connection.execute(
                """SELECT COUNT(*) FROM odds_transport_observations
                    WHERE response_state_hash IS NULL
                      AND response_artifact_hash IS NULL"""
            ).fetchone()[0]
        )


def _completed_legacy_observation_count(
    source_total: int,
    work_database: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> int:
    pending = _legacy_observation_count(
        work_database,
        required_locks=required_locks,
    )
    if pending > source_total:
        raise RuntimeError("compaction work database has unexpected legacy observations")
    return source_total - pending


def _verify_initialized_work_database(
    database: Path,
    *,
    required_locks: Sequence[Path] = (),
) -> None:
    with _checkpoint_reader(
        database,
        label="initialized compaction work database",
        required_locks=required_locks,
    ) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]) != "ok":
            raise RuntimeError("initialized compaction work database failed quick_check")


def _writer_scan_error(scan: WriterScanResult) -> str | None:
    if scan.unverifiable_pids:
        return "managed writer scan could not verify PIDs: " + ",".join(
            str(pid) for pid in scan.unverifiable_pids
        )
    if scan.conflicts:
        return "managed writers already target this database: " + ",".join(
            str(identity.pid) for identity in scan.conflicts
        )
    return None


def compact_legacy_odds(
    source_database: str | Path,
    source_raw_root: str | Path,
    destination_root: str | Path,
    *,
    resume: bool = False,
    _fail_after_observations: int | None = None,
    _phase_hook: Callable[[str], None] | None = None,
    _writer_scanner: Callable[[Path], WriterScanResult] | None = None,
    _lock_factory: Callable[[Path], Any] = SingleInstanceLock,
) -> CompactionResult:
    """Convert one database while excluding every service and compactor peer."""

    source = Path(source_database).resolve()
    source_raw = Path(source_raw_root).resolve()
    destination = Path(destination_root).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if source_raw.exists() and not source_raw.is_dir():
        raise FileNotFoundError(f"source raw root is not a directory: {source_raw}")
    _require_distinct_roots(source, source_raw, destination)
    source_identity = require_unique_database_file(source)
    assert source_identity is not None
    output_database = (destination / _OUTPUT_DATABASE).resolve()
    output_identity = require_unique_database_file(
        output_database,
        allow_missing=True,
    )
    writer_scanner = _writer_scanner

    def offline_scan(path: Path) -> WriterScanResult:
        if writer_scanner is not None:
            return writer_scanner(path)
        return scan_managed_writers(path, mode="offline")

    with ExitStack() as locks:
        source_locks = database_authority_lock_paths(source)
        output_locks = database_authority_lock_paths(output_database)
        destination_lock = destination / _DESTINATION_LOCK
        for lock_path in source_locks:
            locks.enter_context(_lock_factory(lock_path))
        require_unique_database_file(source, expected_identity=source_identity)
        scan_error = _writer_scan_error(offline_scan(source))
        if scan_error is not None:
            raise RuntimeError(scan_error)
        _require_offline_checkpointed_source(source)
        for lock_path in output_locks:
            locks.enter_context(_lock_factory(lock_path))
        locks.enter_context(_lock_factory(destination_lock))
        require_unique_database_file(source, expected_identity=source_identity)
        require_unique_database_file(
            output_database,
            expected_identity=output_identity,
            allow_missing=output_identity is None,
        )
        scan_error = _writer_scan_error(offline_scan(output_database))
        if scan_error is not None:
            raise RuntimeError(scan_error)
        destination.mkdir(parents=True, exist_ok=True)
        if _lock_factory is SingleInstanceLock:
            locks.enter_context(
                file_hash_authority_scope(
                    required_locks=(
                        *source_locks,
                        *output_locks,
                        destination_lock,
                    )
                )
            )
        return _compact_legacy_odds_locked(
            source,
            source_raw,
            destination,
            source_identity=source_identity,
            resume=resume,
            _fail_after_observations=_fail_after_observations,
            _phase_hook=_phase_hook,
            required_locks=(
                *source_locks,
                *output_locks,
                destination_lock,
            ),
        )


def _compact_legacy_odds_locked(
    source_database: Path,
    source_raw_root: Path,
    destination_root: Path,
    *,
    source_identity: DatabaseFileIdentity,
    resume: bool,
    _fail_after_observations: int | None,
    _phase_hook: Callable[[str], None] | None,
    required_locks: Sequence[Path],
) -> CompactionResult:
    """Convert one prepared database with both authority locks already held."""

    if not source_database.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_database}")
    if source_raw_root.exists() and not source_raw_root.is_dir():
        raise FileNotFoundError(f"source raw root is not a directory: {source_raw_root}")
    _require_distinct_roots(source_database, source_raw_root, destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)
    _require_offline_checkpointed_source(source_database)
    verify_prepared_database(
        source_database,
        odds_raw_root=source_raw_root,
        core_only=True,
        immutable_locks=required_locks,
    )
    _require_offline_checkpointed_source(source_database)
    if (
        not source_raw_root.exists()
        and _registered_raw_artifact_count(
            source_database,
            required_locks=required_locks,
        )
        != 0
    ):
        raise RuntimeError("source raw root is missing for registered raw artifacts")

    work_database = _contained_path(destination_root, _WORK_DATABASE_PATH)
    initializing_work_database = _contained_path(
        destination_root, _INITIALIZING_WORK_DATABASE_PATH
    )
    work_root = work_database.parent
    output_database = _contained_path(destination_root, Path(_OUTPUT_DATABASE))
    raw_root = _contained_path(destination_root, _RAW_ROOT)
    manifest_path = _contained_path(destination_root, Path(_MANIFEST))
    temporary_output = output_database.with_name(f".{output_database.name}.vacuuming")
    _require_offline_checkpointed_source(source_database)
    source_hash = _sha256_file(source_database)
    _require_offline_checkpointed_source(source_database)
    source_size = source_database.stat().st_size
    source_legacy_observations = _legacy_observation_count(
        source_database,
        required_locks=required_locks,
    )
    _require_offline_checkpointed_source(source_database)
    work_identity: DatabaseFileIdentity | None = None
    manifest_authority = _ManifestAuthority()

    def checkpoint(
        *,
        status: str | None = None,
        phase: str | None = None,
        **values: Any,
    ) -> dict[str, Any]:
        return _checkpoint_manifest(
            manifest_path,
            manifest,
            status=status,
            phase=phase,
            _authority=manifest_authority,
            **values,
        )

    def finish_ready_cleanup() -> None:
        if manifest_authority.value is not None:
            _require_manifest_authority(manifest_path, manifest_authority.value)
        removed = _remove_ready_work_database(work_database, manifest)
        if removed:
            if "cleanup_pending" in manifest or "cleanup_pending_reason" in manifest:
                manifest.pop("cleanup_pending", None)
                manifest.pop("cleanup_pending_reason", None)
                checkpoint(status="ready", phase="ready")
        else:
            checkpoint(
                status="ready",
                phase="ready",
                cleanup_pending=True,
                cleanup_pending_reason="work_database_authority_missing",
            )
        if manifest_authority.value is not None:
            _require_manifest_authority(manifest_path, manifest_authority.value)

    def notify(phase: str) -> None:
        if manifest_authority.value is not None:
            _require_manifest_authority(manifest_path, manifest_authority.value)
        if _phase_hook is not None:
            _phase_hook(phase)
        if manifest_authority.value is not None:
            _require_manifest_authority(manifest_path, manifest_authority.value)

    if manifest_path.exists() or manifest_path.is_symlink():
        if not resume:
            raise FileExistsError(
                "compaction checkpoint already exists; pass resume=True to continue"
            )
        manifest, manifest_authority.value = _read_manifest_with_authority(
            manifest_path
        )
        recorded_work_path = manifest.get("work_database")
        if recorded_work_path == _WORK_DATABASE:
            work_database = _contained_path(destination_root, Path(_WORK_DATABASE))
            initializing_work_database = _contained_path(
                destination_root,
                Path(_INITIALIZING_WORK_DATABASE),
            )
            work_root = destination_root
        elif recorded_work_path != _WORK_DATABASE_PATH.as_posix():
            raise RuntimeError(
                "compaction checkpoint work_database is not the controlled path"
            )
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
        recorded_legacy_count = manifest.get("source_legacy_observations")
        if (
            recorded_legacy_count is not None
            and int(recorded_legacy_count) != source_legacy_observations
        ):
            raise RuntimeError(
                "compaction checkpoint legacy observation count differs from source"
            )
        manifest["source_legacy_observations"] = source_legacy_observations
        recorded_phase = str(manifest.get("phase") or "")
        if manifest.get("status") == "ready":
            if initializing_work_database.exists() or initializing_work_database.is_symlink():
                raise RuntimeError(
                    "ready compaction retained an initializing work database"
                )
            result = _result_from_manifest(destination_root, manifest)
            _verify_published_output(
                result.output_database,
                result.raw_root,
                expected_bytes=result.output_bytes,
                expected_sha256=result.output_sha256,
                required_locks=required_locks,
            )
            _verify_manifest_result_authority(
                source_database,
                result,
                required_locks=required_locks,
            )
            require_unique_database_file(
                source_database,
                expected_identity=source_identity,
            )
            _require_offline_checkpointed_source(source_database)
            if _sha256_file(source_database) != source_hash:
                raise RuntimeError("source database changed after compaction publication")
            _require_offline_checkpointed_source(source_database)
            _require_manifest_authority(manifest_path, manifest_authority.value)
            finish_ready_cleanup()
            return result
        if recorded_phase == "publishing_work":
            temporary_exists = (
                initializing_work_database.exists()
                or initializing_work_database.is_symlink()
            )
            published_exists = work_database.exists() or work_database.is_symlink()
            if temporary_exists == published_exists:
                raise _WorkDatabaseAuthorityError(
                    "work publication must have exactly one authority file"
                )
            if temporary_exists:
                _require_work_database_authority(
                    initializing_work_database,
                    manifest,
                    authority_path=work_database,
                )
                _replace_and_fsync(initializing_work_database, work_database)
                notify("work_database_replaced")
            work_identity = _require_work_database_authority(work_database, manifest)
            checkpoint(
                status="initializing",
                phase="copying_existing_artifacts",
                copied_existing_artifacts=0,
            )
        if work_database.exists():
            work_identity = _require_work_database_authority(
                work_database,
                manifest,
            )
            recorded_work = manifest[_WORK_DATABASE_AUTHORITY]
            if recorded_work.get("sha256") is None:
                mutable_phase = str(manifest.get("phase") or "")
                if mutable_phase == "converting":
                    _verify_processing_resume(
                        source_database,
                        work_database,
                        raw_root,
                        required_locks=required_locks,
                    )
                elif mutable_phase == "final_schema":
                    _verify_final_schema_resume(
                        source_database,
                        work_database,
                        raw_root,
                        required_locks=required_locks,
                    )
                else:
                    raise _WorkDatabaseAuthorityError(
                        "hash-unavailable work database is not in a resumable phase"
                    )
                require_unique_database_file(
                    work_database,
                    expected_identity=work_identity,
                )
        elif _WORK_DATABASE_AUTHORITY in manifest:
            raise _WorkDatabaseAuthorityError(
                "checkpoint-bound compaction work database is missing"
            )
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
            _verify_published_output(
                output_database,
                raw_root,
                expected_bytes=recovered.output_bytes,
                expected_sha256=recovered.output_sha256,
                required_locks=required_locks,
            )
            _verify_manifest_result_authority(
                source_database,
                recovered,
                required_locks=required_locks,
            )
            require_unique_database_file(
                source_database,
                expected_identity=source_identity,
            )
            _require_offline_checkpointed_source(source_database)
            if _sha256_file(source_database) != source_hash:
                raise RuntimeError("source database changed after compaction publication")
            _require_offline_checkpointed_source(source_database)
            manifest.pop("error", None)
            checkpoint(
                status="ready",
                phase="ready",
            )
            finish_ready_cleanup()
            return recovered
    else:
        if resume:
            raise FileNotFoundError("compaction checkpoint does not exist")
        occupied = [
            path
            for path in (
                work_root,
                _contained_path(destination_root, Path(_WORK_DATABASE)),
                _contained_path(
                    destination_root,
                    Path(_INITIALIZING_WORK_DATABASE),
                ),
                work_database,
                initializing_work_database,
                output_database,
                temporary_output,
                raw_root,
            )
            if path.exists()
        ]
        if occupied:
            raise FileExistsError(
                "compaction destination contains unmanaged output: "
                + ", ".join(str(path) for path in occupied)
            )
        _preflight_space(
            source_database,
            destination_root,
            work_database=work_database,
            output_database=output_database,
            raw_root=raw_root,
            required_locks=required_locks,
        )
        manifest = {
            "format": _MANIFEST_FORMAT,
            "source_database": str(source_database),
            "source_raw_root": str(source_raw_root),
            "source_sha256": source_hash,
            "source_bytes": source_size,
            "source_legacy_observations": source_legacy_observations,
            "live_schema_version": CURRENT_SCHEMA_VERSION,
            "work_database": _WORK_DATABASE_PATH.as_posix(),
            "raw_root": _RAW_ROOT.as_posix(),
            "output_database": _OUTPUT_DATABASE,
            "completed_observations": 0,
            "validated_observations": 0,
            "created_at": _utc_now(),
        }
        checkpoint(
            status="initializing",
            phase="initializing_backup",
        )
        notify("initializing_manifest_written")

    if work_root != destination_root:
        if work_root.exists():
            if work_root.is_symlink() or not work_root.is_dir():
                raise RuntimeError(f"compaction work root is unsafe: {work_root}")
        else:
            work_root.mkdir()
            fsync_directory(work_root.parent)

    connection: sqlite3.Connection | None = None
    try:
        initialization_phases = {
            "initializing_backup",
            "copying_existing_artifacts",
        }
        recorded_phase = str(manifest.get("phase") or "")
        work_published_now = False
        if initializing_work_database.exists() and work_database.exists():
            raise RuntimeError(
                "compaction initialization has both temporary and published work databases"
            )
        if not work_database.is_file():
            if recorded_phase not in initialization_phases:
                raise RuntimeError("compaction checkpoint work database is missing")
            _unlink_and_fsync(initializing_work_database, missing_ok=True)
        _unlink_and_fsync(temporary_output, missing_ok=True)
        if resume:
            _preflight_space(
                source_database,
                destination_root,
                work_database=work_database,
                output_database=output_database,
                raw_root=raw_root,
                required_locks=required_locks,
            )
        if not work_database.is_file():
            checkpoint(
                status="initializing",
                phase="initializing_backup",
            )
            notify("initializing_backup_started")
            invalidate_hashed_paths(initializing_work_database)
            online_backup(
                source_database,
                initializing_work_database,
                immutable_locks=required_locks,
            )
            invalidate_hashed_paths(initializing_work_database)
            _, pending_work_authority = _capture_work_database_authority(
                initializing_work_database,
                hash_phase="work_publish_pending",
                authority_path=work_database,
            )
            checkpoint(
                status="initializing",
                phase="publishing_work",
                **{_WORK_DATABASE_AUTHORITY: pending_work_authority},
            )
            notify("work_database_authority_checkpointed")
            _require_work_database_authority(
                initializing_work_database,
                manifest,
                authority_path=work_database,
            )
            _replace_and_fsync(initializing_work_database, work_database)
            work_published_now = True
            notify("work_database_replaced")
            work_identity = _require_work_database_authority(
                work_database,
                manifest,
            )

        if recorded_phase in initialization_phases or manifest.get("status") == "initializing":
            if work_identity is None:
                work_identity = _require_work_database_authority(
                    work_database,
                    manifest,
                )
            if not work_published_now:
                _verify_initialized_work_database(
                    work_database,
                    required_locks=required_locks,
                )
                _require_work_database_authority(work_database, manifest)
            checkpoint(
                status="initializing",
                phase="copying_existing_artifacts",
                copied_existing_artifacts=0,
            )

            def copy_progress(count: int) -> None:
                checkpoint(
                    status="initializing",
                    copied_existing_artifacts=count,
                )

            _copy_existing_artifacts(
                work_database,
                source_raw_root,
                raw_root,
                progress=copy_progress,
                required_locks=required_locks,
            )
            notify("initialization_completed")

        _preflight_space(
            source_database,
            destination_root,
            work_database=work_database,
            output_database=output_database,
            raw_root=raw_root,
            required_locks=required_locks,
        )
        completed = _completed_legacy_observation_count(
            source_legacy_observations,
            work_database,
            required_locks=required_locks,
        )
        work_identity = _require_work_database_authority(work_database, manifest)
        manifest.pop("error", None)
        checkpoint(
            status="processing",
            phase="converting",
            completed_observations=completed,
            validated_observations=0,
        )
        notify("conversion_started")

        work_identity, work_authority = _capture_work_database_authority(
            work_database,
            expected_identity=work_identity,
            hash_phase=None,
        )
        checkpoint(
            status="processing",
            **{_WORK_DATABASE_AUTHORITY: work_authority},
        )

        def require_mutable_work_identity() -> None:
            _require_work_database_authority(
                work_database,
                manifest,
                verify_available_hash=False,
            )

        converted = 0
        invalidate_hashed_paths(work_database)
        connection = _acquire_compaction_connection(
            work_database,
            authority_check=require_mutable_work_identity,
        )
        archive = RawArchive(raw_root, cache_paths=False)
        verified_states = _BoundedFingerprintCache()
        verified_artifacts = _BoundedFingerprintCache()
        pending = 0
        for group in _iter_legacy_groups(connection):
            if not connection.in_transaction:
                require_mutable_work_identity()
                connection.execute("BEGIN IMMEDIATE")
            _convert_group(
                connection,
                archive,
                raw_root,
                group,
                verified_states,
                verified_artifacts,
            )
            converted += 1
            pending += 1
            if pending == _COMMIT_BATCH_SIZE:
                connection.commit()
                require_mutable_work_identity()
                completed += pending
                pending = 0
                work_identity, work_authority = _capture_work_database_authority(
                    work_database,
                    expected_identity=work_identity,
                    hash_phase=None,
                )
                checkpoint(
                    status="processing",
                    completed_observations=completed,
                    **{_WORK_DATABASE_AUTHORITY: work_authority},
                )
            if (
                _fail_after_observations is not None
                and converted >= _fail_after_observations
            ):
                raise RuntimeError("injected compaction interruption")
        if connection.in_transaction:
            connection.commit()
            require_mutable_work_identity()
        if pending:
            completed += pending
            work_identity, work_authority = _capture_work_database_authority(
                work_database,
                expected_identity=work_identity,
                hash_phase=None,
            )
            checkpoint(
                status="processing",
                completed_observations=completed,
                **{_WORK_DATABASE_AUTHORITY: work_authority},
            )

        # Exclusive locking mode intentionally prevents even a second reader.
        # Release it before the independent source-vs-output audit connection.
        connection.close()
        connection = None
        work_identity, work_authority = _capture_work_database_authority(
            work_database,
            expected_identity=work_identity,
            hash_phase="validating",
        )
        completed = _completed_legacy_observation_count(
            source_legacy_observations,
            work_database,
            required_locks=required_locks,
        )
        checkpoint(
            status="processing",
            phase="validating",
            completed_observations=completed,
            validated_observations=0,
            **{_WORK_DATABASE_AUTHORITY: work_authority},
        )
        notify("validation_started")

        def validation_progress(count: int) -> None:
            checkpoint(
                status="processing",
                validated_observations=count,
            )

        observation_count, outcome_count, equivalence_hash = _validate_equivalence(
            source_database,
            work_database,
            raw_root,
            progress=validation_progress,
            required_locks=required_locks,
        )
        checkpoint(
            status="validated",
            phase="validated",
            observation_count=observation_count,
            outcome_count=outcome_count,
            equivalence_sha256=equivalence_hash,
            validated_observations=observation_count,
            **{_WORK_DATABASE_AUTHORITY: work_authority},
        )
        notify("validated_manifest_written")

        work_identity = _require_work_database_authority(work_database, manifest)
        checkpoint(
            status="validated",
            phase="final_schema",
        )
        work_identity, work_authority = _capture_work_database_authority(
            work_database,
            expected_identity=work_identity,
            hash_phase=None,
        )
        checkpoint(
            status="validated",
            **{_WORK_DATABASE_AUTHORITY: work_authority},
        )
        invalidate_hashed_paths(work_database)
        connection = _acquire_compaction_connection(
            work_database,
            authority_check=require_mutable_work_identity,
        )
        _restore_final_schema(
            connection,
            authority_check=require_mutable_work_identity,
        )
        connection.close()
        connection = None
        work_identity, work_authority = _capture_work_database_authority(
            work_database,
            expected_identity=work_identity,
            hash_phase="final_schema_complete",
        )
        checkpoint(
            status="validated",
            phase="final_schema_complete",
            **{_WORK_DATABASE_AUTHORITY: work_authority},
        )
        notify("final_schema_committed")

        _preflight_space(
            source_database,
            destination_root,
            work_database=work_database,
            output_database=output_database,
            raw_root=raw_root,
            required_locks=required_locks,
        )

        checkpoint(
            status="validated",
            phase="vacuuming",
        )
        _unlink_and_fsync(temporary_output, missing_ok=True)
        work_identity = _require_work_database_authority(work_database, manifest)
        invalidate_hashed_paths(temporary_output)
        try:
            vacuum_into_immutable_checkpoint(
                work_database,
                temporary_output,
                label="compaction work database vacuum source",
                required_locks=required_locks,
                authority_check=require_mutable_work_identity,
            )
        finally:
            invalidate_hashed_paths(temporary_output)
        _require_work_database_authority(work_database, manifest)
        notify("vacuum_completed")

        checkpoint(
            status="validated",
            phase="verifying_output",
        )
        _verify_sqlite_output(
            temporary_output,
            raw_root,
            required_locks=required_locks,
        )
        notify("output_verified")

        checkpoint(
            status="validated",
            phase="hashing",
        )
        require_unique_database_file(
            source_database,
            expected_identity=source_identity,
        )
        _require_offline_checkpointed_source(source_database)
        if _sha256_file(source_database) != source_hash:
            raise RuntimeError("source database changed during offline compaction")
        _require_offline_checkpointed_source(source_database)
        _require_transaction_free_database(
            temporary_output,
            label="compaction output candidate",
        )
        output_hash = _sha256_file(temporary_output)
        with _checkpoint_reader(
            temporary_output,
            label="compaction output counts",
            required_locks=required_locks,
        ) as counts:
            state_count = int(
                counts.execute("SELECT COUNT(*) FROM odds_response_states").fetchone()[0]
            )
            artifact_count = int(
                counts.execute("SELECT COUNT(*) FROM odds_raw_artifacts").fetchone()[0]
            )
        _require_transaction_free_database(
            temporary_output,
            label="compaction output candidate",
        )
        require_unique_database_file(
            source_database,
            expected_identity=source_identity,
        )
        _require_work_database_authority(work_database, manifest)
        checkpoint(
            status="publishing",
            phase="publishing",
            state_count=state_count,
            artifact_count=artifact_count,
            output_sha256=output_hash,
            output_bytes=temporary_output.stat().st_size,
        )
        notify("publishing_manifest_written")
        require_unique_database_file(
            source_database,
            expected_identity=source_identity,
        )
        if output_database.exists():
            raise FileExistsError(f"compaction output already exists: {output_database}")
        _clear_quiescent_sqlite_sidecars(
            temporary_output,
            label="compaction output candidate",
        )
        _replace_and_fsync(temporary_output, output_database)
        notify("output_replaced")
        _verify_published_output(
            output_database,
            raw_root,
            expected_bytes=int(manifest["output_bytes"]),
            expected_sha256=str(manifest["output_sha256"]),
            required_locks=required_locks,
        )
        notify("published_output_verified")
        require_unique_database_file(
            source_database,
            expected_identity=source_identity,
        )
        _require_offline_checkpointed_source(source_database)
        if _sha256_file(source_database) != source_hash:
            raise RuntimeError("source database changed after compaction publication")
        _require_offline_checkpointed_source(source_database)
        require_unique_database_file(
            source_database,
            expected_identity=source_identity,
        )
        _verify_published_output(
            output_database,
            raw_root,
            expected_bytes=int(manifest["output_bytes"]),
            expected_sha256=str(manifest["output_sha256"]),
            required_locks=required_locks,
        )
        checkpoint(
            status="ready",
            phase="ready",
        )
        result = _result_from_manifest(destination_root, manifest)
        finish_ready_cleanup()
        return result
    except BaseException as error:
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        if manifest.get("status") == "ready":
            raise
        if work_identity is not None and work_database.exists():
            try:
                _require_work_database_authority(work_database, manifest)
                work_identity, work_authority = _capture_work_database_authority(
                    work_database,
                    expected_identity=work_identity,
                    hash_phase=f"failed:{manifest.get('phase') or 'unknown'}",
                )
            except _WorkDatabaseAuthorityError as authority_error:
                manifest["work_database_authority_error"] = (
                    f"{type(authority_error).__name__}: {authority_error}"
                )
            else:
                manifest[_WORK_DATABASE_AUTHORITY] = work_authority
                manifest.pop("work_database_authority_error", None)
        manifest["error"] = f"{type(error).__name__}: {error}"
        checkpoint(status="failed")
        raise
    finally:
        _unlink_and_fsync(temporary_output, missing_ok=True)


def result_json(result: CompactionResult) -> str:
    payload = asdict(result)
    payload["output_database"] = str(result.output_database)
    payload["raw_root"] = str(result.raw_root)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["CompactionResult", "compact_legacy_odds", "result_json"]
