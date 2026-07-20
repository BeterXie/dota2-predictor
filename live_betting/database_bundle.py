"""Self-contained, relocatable SQLite and raw-artifact backup bundles."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from event_intelligence.raw_archive import schema_fingerprint
from event_intelligence.raw_registry import (
    raw_source_relocation_id,
    relocate_raw_source_artifacts,
)
from shared.sqlite import connect

from .database_protocol import (
    CUTOVER_SAFETY_MARGIN_BYTES,
    immutable_checkpoint_reader,
    online_backup,
    sqlite_sidecar_state,
    verify_prepared_database,
)
from .runtime_schema import prepare_runtime_schema
from .hash_authority import (
    file_hash_authority_scope,
    hash_authority_scope_covers,
    invalidate_hashed_paths,
    read_hashed_file,
    rebind_hashed_paths,
)
from .service_coordination import (
    DatabaseFileIdentity,
    DirectoryIdentity,
    SingleInstanceLock,
    capture_directory_identity,
    database_authority_lock_paths,
    database_local_authority_lock_paths,
    database_offline_authority,
    fsync_directory,
    require_directory_identity,
    require_unique_database_file,
)
from .vision_frame_registry import (
    relocate_vision_frame_artifacts,
    verify_vision_frame_registry,
    vision_frame_relocation_id,
)


_BUNDLE_FORMAT = "dota2-database-bundle-v1"
_DATABASE_DIRECTORY = "database"
_DATABASE_FILE = "database.sqlite"
_BUNDLE_DATABASE_PATH = Path(_DATABASE_DIRECTORY) / _DATABASE_FILE
_MANIFEST_FILE = "manifest.json"
_RESTORE_MANIFEST_FILE = "restore-manifest.json"
_STAGING_MANIFEST_FILE = "staging-manifest.json"
_STAGING_FORMAT = "dota2-database-bundle-staging-v1"
_RESTORE_STAGING_FORMAT = "dota2-database-bundle-restore-staging-v1"
_SPACE_MARGIN_BYTES = CUTOVER_SAFETY_MARGIN_BYTES
_SOURCE_TREE_POLICY_VERSION = "runtime-only-v1"
_RUNTIME_ONLY_PREFIXES = (
    "data/",
    "dogfood-output/",
    "dist/",
    "build/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
)
_RUNTIME_ONLY_EXACT = frozenset(
    {
        "web/frontend/tsconfig.app.tsbuildinfo",
        "web/frontend/tsconfig.node.tsbuildinfo",
    }
)
_RUNTIME_ONLY_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")
_FILE_AUTHORITY_FIELDS = ("device", "inode", "bytes", "sha256")
_PROVENANCE_FIELDS = (
    "source_tree_head",
    "source_tree_clean",
    "source_tree_policy_version",
    "source_tree_runtime_dirty_paths",
)


@dataclass(frozen=True)
class BundleResult:
    bundle_directory: Path
    database_sha256: str
    artifact_count: int
    total_bytes: int


@dataclass(frozen=True)
class RestoreResult:
    restore_directory: Path
    database: Path
    odds_raw_root: Path
    source_raw_root: Path
    vision_frame_root: Path
    restored_database_sha256: str


def _sha256_file(path: Path) -> str:
    return read_hashed_file(path, label="bundle file").sha256


def _operation_lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.database-bundle.operation.lock"


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


def _rename_and_fsync(
    source: Path,
    destination: Path,
    *,
    recursive: bool = False,
) -> None:
    source_parent = capture_directory_identity(
        source.parent,
        label="rename source parent",
    )
    destination_parent = capture_directory_identity(
        destination.parent,
        label="rename destination parent",
    )
    try:
        source.rename(destination)
    except BaseException:
        invalidate_hashed_paths(source, destination, recursive=recursive)
        raise
    _fsync_bound_directories(source_parent, destination_parent)
    rebind_hashed_paths(source, destination, recursive=recursive)


def _unlink_and_fsync(path: Path, *, missing_ok: bool = False) -> bool:
    parent = capture_directory_identity(path.parent, label="unlink parent")
    invalidate_hashed_paths(path, recursive=path.is_dir())
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    _fsync_bound_directories(parent)
    return True


def _rmdir_and_fsync(path: Path) -> None:
    parent = capture_directory_identity(path.parent, label="rmdir parent")
    invalidate_hashed_paths(path, recursive=True)
    path.rmdir()
    _fsync_bound_directories(parent)


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
    expected = (
        int(record["device"]),
        int(record["inode"]),
        int(record["bytes"]),
        int(record["mtime_ns"]),
    )
    if (
        snapshot.device,
        snapshot.inode,
        snapshot.bytes,
        snapshot.mtime_ns,
    ) != expected:
        raise RuntimeError(f"{label} authority changed before quarantine")
    quarantine = path.with_name(
        f".{path.name}.quarantine.{os.getpid()}.{uuid.uuid4().hex}"
    )
    _replace_and_fsync(path, quarantine)
    moved = read_hashed_file(
        quarantine,
        label=f"quarantined {label}",
        include_payload=False,
    )
    if (
        moved.device != snapshot.device
        or moved.inode != snapshot.inode
        or moved.mode != snapshot.mode
        or moved.bytes != snapshot.bytes
        or moved.mtime_ns != snapshot.mtime_ns
        or moved.sha256 != snapshot.sha256
    ):
        raise RuntimeError(f"quarantined {label} authority changed")
    repeated = read_hashed_file(
        quarantine,
        label=f"quarantined {label}",
        include_payload=False,
    )
    if repeated != moved:
        raise RuntimeError(f"quarantined {label} changed before deletion")
    _unlink_and_fsync(quarantine)


def _database_identity_payload(identity: DatabaseFileIdentity) -> dict[str, Any]:
    return {
        "resolved_path": str(identity.resolved_path),
        "device": identity.device,
        "inode": identity.inode,
    }


def _unique_file_authority(
    path: Path,
    *,
    label: str = "file authority",
    database: bool = False,
) -> dict[str, Any]:
    """Hash one unaliased file while fencing replacement and active mutation."""

    snapshot = read_hashed_file(
        path,
        label=label,
        include_payload=False,
        database=database,
    )
    return {
        "resolved_path": str(snapshot.resolved_path),
        "device": snapshot.device,
        "inode": snapshot.inode,
        "bytes": snapshot.bytes,
        "sha256": snapshot.sha256,
    }


def _require_file_authority(
    path: Path,
    expected: object,
    *,
    allow_relocated_path: bool = False,
    label: str,
    database: bool = False,
) -> dict[str, Any]:
    if not isinstance(expected, Mapping):
        raise RuntimeError(f"{label} file authority is missing or invalid")
    current = _unique_file_authority(path, label=label, database=database)
    comparable_fields = _FILE_AUTHORITY_FIELDS + (
        () if allow_relocated_path else ("resolved_path",)
    )
    if any(current.get(field) != expected.get(field) for field in comparable_fields):
        raise RuntimeError(f"{label} file authority changed")
    return current


def _prepare_runtime_database(database: Path) -> None:
    invalidate_hashed_paths(database)
    connection = connect(database)
    try:
        journal_mode_row = connection.execute("PRAGMA journal_mode").fetchone()
        if journal_mode_row is None:
            raise RuntimeError("runtime schema preparation journal mode is unavailable")
        journal_mode = str(journal_mode_row[0]).casefold()
        connection.execute("BEGIN IMMEDIATE")
        prepare_runtime_schema(connection, external_transaction=True)
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        expected_checkpoint = (0, 0, 0) if journal_mode == "wal" else (0, -1, -1)
        if (
            checkpoint is None
            or tuple(int(value) for value in checkpoint) != expected_checkpoint
        ):
            raise RuntimeError("runtime schema preparation left an unsafe WAL")
        if journal_mode == "wal":
            rollback_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            if (
                rollback_mode is None
                or str(rollback_mode[0]).casefold() != "delete"
            ):
                raise RuntimeError("runtime schema preparation could not disable WAL")
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
        invalidate_hashed_paths(database)
    _require_transaction_free_database(
        database,
        label="runtime-prepared database",
    )


def _require_checkpointed_source(database: Path) -> None:
    _require_transaction_free_database(database, label="source database")


def _require_transaction_free_database(
    database: Path,
    *,
    label: str,
) -> dict[str, dict[str, int | bool | str | None]]:
    """Reject durable SQLite state that is not in the main database file."""

    state = sqlite_sidecar_state(database)
    unsafe = [
        f"{Path(str(state[name]['path'])).name}:{int(state[name]['bytes'])}"
        for name in ("wal", "journal")
        if int(state[name]["bytes"])
    ]
    if unsafe:
        raise RuntimeError(
            f"{label} has non-empty SQLite sidecars; checkpoint it first: "
            + ",".join(unsafe)
        )
    return state


def _unique_database_authority(path: Path, *, label: str) -> dict[str, Any]:
    _require_transaction_free_database(path, label=label)
    authority = _unique_file_authority(path, label=label, database=True)
    _require_transaction_free_database(path, label=label)
    return authority


def _require_database_file_authority(
    path: Path,
    expected: object,
    *,
    allow_relocated_path: bool = False,
    label: str,
) -> dict[str, Any]:
    _require_transaction_free_database(path, label=label)
    authority = _require_file_authority(
        path,
        expected,
        allow_relocated_path=allow_relocated_path,
        label=label,
        database=True,
    )
    _require_transaction_free_database(path, label=label)
    return authority


def _clear_quiescent_sqlite_sidecars(database: Path) -> None:
    state = _require_transaction_free_database(
        database,
        label="database",
    )
    for name in ("wal", "shm", "journal"):
        if state[name]["exists"]:
            invalidate_hashed_paths(database)
            _quarantine_sqlite_sidecar(
                state[name],
                label=f"database {name} sidecar",
            )
            invalidate_hashed_paths(database)
    completed = sqlite_sidecar_state(database)
    if any(bool(record["exists"]) for record in completed.values()):
        raise RuntimeError(f"database SQLite sidecars could not be cleared: {database}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_json_file_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value) + b"\n").hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _canonical_json(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    previous = (
        _unique_file_authority(path)
        if path.exists() or path.is_symlink()
        else None
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_authority = _unique_file_authority(temporary)
        if temporary_authority["bytes"] != len(encoded) or temporary_authority[
            "sha256"
        ] != hashlib.sha256(encoded).hexdigest():
            raise RuntimeError("temporary JSON authority changed before publication")
        if previous is not None:
            _require_file_authority(
                path,
                previous,
                label="JSON destination",
            )
        elif path.exists() or path.is_symlink():
            raise RuntimeError("JSON destination appeared before publication")
        _replace_and_fsync(temporary, path)
        published = _unique_file_authority(path)
        if published["bytes"] != len(encoded) or published["sha256"] != temporary_authority[
            "sha256"
        ]:
            raise RuntimeError("published JSON authority changed")
    finally:
        _unlink_and_fsync(temporary, missing_ok=True)


def _read_json_with_authority(
    path: Path,
    *,
    label: str,
) -> tuple[Any, dict[str, Any]]:
    snapshot = read_hashed_file(path, label=label, include_payload=True)
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
        raise RuntimeError(f"{label} is missing or invalid") from error
    _require_file_authority(path, authority, label=label)
    return value, authority


def _unlink_json_with_authority(
    path: Path,
    expected: Mapping[str, Any],
    *,
    label: str,
) -> None:
    value, authority = _read_json_with_authority(path, label=label)
    if value != expected:
        raise RuntimeError(f"{label} differs before deletion")
    _require_file_authority(path, authority, label=label)
    _unlink_and_fsync(path)
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"{label} could not be removed")


def _read_manifest(bundle_root: Path) -> dict[str, Any]:
    path = bundle_root / _MANIFEST_FILE
    value, _ = _read_json_with_authority(
        path,
        label="backup bundle manifest",
    )
    if not isinstance(value, dict) or value.get("format") != _BUNDLE_FORMAT:
        raise RuntimeError("backup bundle manifest has the wrong format")
    return value


def _read_staging_manifest(staging: Path) -> dict[str, Any]:
    path = staging / _STAGING_MANIFEST_FILE
    value, _ = _read_json_with_authority(
        path,
        label="backup bundle staging manifest",
    )
    if not isinstance(value, dict) or value.get("format") != _STAGING_FORMAT:
        raise RuntimeError("backup bundle staging manifest has the wrong format")
    return value


def _read_restore_staging_manifest(staging: Path) -> dict[str, Any]:
    path = staging / _STAGING_MANIFEST_FILE
    value, _ = _read_json_with_authority(
        path,
        label="restore staging manifest",
    )
    if not isinstance(value, dict) or value.get("format") != _RESTORE_STAGING_FORMAT:
        raise RuntimeError("restore staging manifest has the wrong format")
    return value


def _staging_directory(target: Path) -> Path:
    return target.parent / f".{target.name}.staging"


def _restore_staging_directory(target: Path) -> Path:
    return target.parent / f".{target.name}.restore-staging"


def _controlled_path(root: Path, relative: str | Path) -> Path:
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("bundle path must be controlled and relative")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("bundle path escapes its controlled root") from error
    return path


def _bundle_staging_database_path(
    staging: Path,
    checkpoint: Mapping[str, Any],
) -> Path:
    recorded = checkpoint.get("database_path")
    if recorded is None:
        relative = Path(_DATABASE_FILE)
    elif recorded == _BUNDLE_DATABASE_PATH.as_posix():
        relative = _BUNDLE_DATABASE_PATH
    else:
        raise RuntimeError("bundle staging database path is not controlled")
    return _controlled_path(staging, relative)


def _restore_staging_database_path(
    staging: Path,
    checkpoint: Mapping[str, Any],
    database_name: str,
) -> Path:
    current = Path(_DATABASE_DIRECTORY) / database_name
    recorded = checkpoint.get("staging_database_path")
    if recorded is None:
        relative = Path(database_name)
    elif recorded == current.as_posix():
        relative = current
    else:
        raise RuntimeError("restore staging database path is not controlled")
    return _controlled_path(staging, relative)


def _inside_any(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _schema_manifest(connection: sqlite3.Connection) -> dict[str, Any]:
    objects = [
        tuple(row)
        for row in connection.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name"""
        )
    ]
    versions: dict[str, int] = {}
    for table in (
        "live_schema_version",
        "intelligence_schema_version",
        "runtime_schema_version",
    ):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is not None:
            value = connection.execute(f"SELECT MAX(version) FROM {table}").fetchone()[
                0
            ]
            versions[table] = 0 if value is None else int(value)
    return {
        "sha256": hashlib.sha256(_canonical_json(objects)).hexdigest(),
        "object_count": len(objects),
        "versions": versions,
    }


def _git_commit() -> str:
    repository = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("cannot determine the source git commit") from error
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise RuntimeError("source git commit is invalid")
    return commit


def _git_status_porcelain() -> tuple[str, ...]:
    repository = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("cannot determine source worktree status") from error
    return tuple(line for line in completed.stdout.splitlines() if line.strip())


def _status_paths(raw: str) -> tuple[str, ...]:
    """Return every path represented by one porcelain status record.

    Rename records contain both the old and new path.  Treating only the
    destination as dirty would allow a source file to be renamed into an
    allowed runtime directory while evading the source-tree guard.
    """

    path_field = raw[3:] if len(raw) >= 3 else ""
    paths = path_field.rsplit(" -> ", 1) if " -> " in path_field else [path_field]
    normalized: list[str] = []
    for path in paths:
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            path = path[1:-1]
        path = path.replace("\\", "/").casefold()
        if path:
            normalized.append(path)
    return tuple(normalized)


def _runtime_only_path(path: str) -> bool:
    normalized = path.casefold()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in _RUNTIME_ONLY_EXACT:
        return True
    if normalized.endswith(_RUNTIME_ONLY_SUFFIXES):
        return True
    return any(
        normalized == prefix[:-1] or normalized.startswith(prefix)
        for prefix in _RUNTIME_ONLY_PREFIXES
    )


def _source_tree_provenance() -> dict[str, Any]:
    head = _git_commit()
    dirty_paths = tuple(
        sorted(
            {path for line in _git_status_porcelain() for path in _status_paths(line)}
        )
    )
    disallowed = tuple(path for path in dirty_paths if not _runtime_only_path(path))
    if disallowed:
        visible = ", ".join(disallowed[:20])
        suffix = f"; plus {len(disallowed) - 20} more" if len(disallowed) > 20 else ""
        raise RuntimeError(
            "source worktree contains non-runtime changes: " + visible + suffix
        )
    return {
        "source_tree_clean": True,
        "source_tree_policy_version": _SOURCE_TREE_POLICY_VERSION,
        "source_tree_head": head,
        "source_tree_runtime_dirty_paths": list(dirty_paths),
    }


def _provenance_from(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value.get(field) for field in _PROVENANCE_FIELDS}


def _require_current_provenance(expected: Mapping[str, Any]) -> dict[str, Any]:
    current = _source_tree_provenance()
    if _provenance_from(current) != _provenance_from(expected):
        raise RuntimeError("source worktree provenance changed during bundle operation")
    return current


def _require_git_commit_ancestor(ancestor: str, descendant: str) -> None:
    for label, commit in (("checkpoint", ancestor), ("current", descendant)):
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise RuntimeError(f"{label} git commit is invalid")
    repository = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("cannot verify checkpoint git ancestry") from error
    if completed.returncode == 0:
        return
    if completed.returncode == 1:
        raise RuntimeError("checkpoint git commit is not an ancestor of current HEAD")
    raise RuntimeError("cannot verify checkpoint git ancestry")


def _provenance_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "git_commit": value.get("git_commit"),
        **_provenance_from(value),
    }


def _valid_provenance_snapshot(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    expected_keys = {"git_commit", *_PROVENANCE_FIELDS}
    if set(value) != expected_keys:
        return False
    head = value.get("source_tree_head")
    dirty_paths = value.get("source_tree_runtime_dirty_paths")
    return bool(
        re.fullmatch(r"[0-9a-f]{40,64}", str(value.get("git_commit", "")))
        and value.get("git_commit") == head
        and value.get("source_tree_clean") is True
        and value.get("source_tree_policy_version") == _SOURCE_TREE_POLICY_VERSION
        and isinstance(dirty_paths, list)
        and all(
            isinstance(path, str) and _runtime_only_path(path)
            for path in dirty_paths
        )
    )


def _require_provenance_recovery_audit(
    value: object,
    current: Mapping[str, Any],
) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "from",
        "to",
        "adopted_at",
    }:
        raise RuntimeError("backup bundle provenance recovery audit is invalid")
    before = value.get("from")
    after = value.get("to")
    if (
        not _valid_provenance_snapshot(before)
        or not _valid_provenance_snapshot(after)
        or before == after
        or after != _provenance_snapshot(current)
    ):
        raise RuntimeError("backup bundle provenance recovery audit is invalid")
    adopted_at = value.get("adopted_at")
    try:
        parsed = datetime.fromisoformat(str(adopted_at))
    except ValueError as error:
        raise RuntimeError(
            "backup bundle provenance recovery timestamp is invalid"
        ) from error
    if not isinstance(adopted_at, str) or parsed.utcoffset() is None:
        raise RuntimeError("backup bundle provenance recovery timestamp is invalid")


def _adopt_snapshot_pending_provenance(
    staging: Path,
    checkpoint: Mapping[str, Any],
    binding: Mapping[str, Any],
    expected_old_head: str,
) -> dict[str, Any]:
    if checkpoint.get("status") != "snapshot_pending":
        raise RuntimeError(
            "bundle provenance adoption requires a snapshot_pending checkpoint"
        )
    if "provenance_recovery" in checkpoint:
        raise RuntimeError("bundle checkpoint provenance was already adopted")
    old_provenance = _provenance_snapshot(checkpoint)
    current_provenance = _provenance_snapshot(binding)
    if not _valid_provenance_snapshot(old_provenance):
        raise RuntimeError("bundle checkpoint provenance is invalid")
    if not _valid_provenance_snapshot(current_provenance):
        raise RuntimeError("current bundle provenance is invalid")
    old_head = str(old_provenance["git_commit"])
    current_head = str(current_provenance["git_commit"])
    if expected_old_head != old_head:
        raise RuntimeError("confirmed resume git commit differs from checkpoint")
    if old_head == current_head:
        raise RuntimeError("bundle checkpoint already uses the current git commit")
    for key in (
        "target",
        "source_database",
        "odds_raw_root",
        "allowed_source_roots",
    ):
        if checkpoint.get(key) != binding.get(key):
            raise RuntimeError("backup bundle staging checkpoint binding mismatch")
    _require_git_commit_ancestor(old_head, current_head)
    updated = dict(checkpoint)
    updated.update(binding)
    updated["provenance_recovery"] = {
        "from": old_provenance,
        "to": current_provenance,
        "adopted_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(staging / _STAGING_MANIFEST_FILE, updated)
    return updated


def _artifact_bundle_path(
    registry: str,
    key: str,
    content_hash: str,
    source: str,
    storage_path: str,
) -> str:
    if registry == "odds_raw_artifacts":
        relative = Path(storage_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("odds raw artifact path is not controlled and relative")
        return (Path("raw") / "odds" / relative).as_posix()
    if registry == "raw_source_artifacts":
        if not source or not re.fullmatch(r"[a-z0-9_-]+", source):
            raise RuntimeError("source raw artifact has an invalid source")
        return (
            Path("raw")
            / "sources"
            / source
            / content_hash[:2]
            / f"{content_hash}.json.gz"
        ).as_posix()
    if registry == "vision_frame_artifacts":
        if key != f"vision-frame:sha256:{content_hash}":
            raise RuntimeError("vision frame artifact identity is invalid")
        return (
            Path("vision") / "sha256" / content_hash[:2] / f"{content_hash}.jpg"
        ).as_posix()
    raise RuntimeError(f"unsupported raw artifact registry: {registry}:{key}")


def _database_artifact_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    odds_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odds_raw_artifacts'"
    ).fetchone()
    if odds_exists is not None:
        for row in connection.execute(
            """SELECT artifact_hash, source, storage_path, uncompressed_bytes,
                      compressed_bytes, schema_fingerprint
                 FROM odds_raw_artifacts ORDER BY artifact_hash"""
        ):
            key = str(row[0])
            records.append(
                {
                    "registry": "odds_raw_artifacts",
                    "key": key,
                    "content_sha256": key,
                    "source": str(row[1]),
                    "database_storage_path": str(row[2]),
                    "uncompressed_bytes": int(row[3]),
                    "compressed_bytes": int(row[4]),
                    "schema_fingerprint": str(row[5]),
                    "bundle_path": _artifact_bundle_path(
                        "odds_raw_artifacts", key, key, str(row[1]), str(row[2])
                    ),
                }
            )
    source_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_source_artifacts'"
    ).fetchone()
    if source_exists is not None:
        for row in connection.execute(
            """SELECT artifact_id, content_hash, source, storage_path,
                      uncompressed_bytes, compressed_bytes, schema_fingerprint
                 FROM raw_source_artifacts ORDER BY artifact_id"""
        ):
            records.append(
                {
                    "registry": "raw_source_artifacts",
                    "key": str(row[0]),
                    "content_sha256": str(row[1]),
                    "source": str(row[2]),
                    "database_storage_path": str(row[3]),
                    "uncompressed_bytes": int(row[4]),
                    "compressed_bytes": int(row[5]),
                    "schema_fingerprint": str(row[6]),
                    "bundle_path": _artifact_bundle_path(
                        "raw_source_artifacts",
                        str(row[0]),
                        str(row[1]),
                        str(row[2]),
                        str(row[3]),
                    ),
                }
            )
    vision_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='vision_frame_artifacts'"
    ).fetchone()
    if vision_exists is not None:
        verify_vision_frame_registry(connection, require_active_files=False)
        for row in connection.execute(
            """SELECT frame_ref, content_sha256, byte_length, storage_path
                 FROM active_vision_frame_artifacts ORDER BY frame_ref"""
        ):
            records.append(
                {
                    "registry": "vision_frame_artifacts",
                    "key": str(row[0]),
                    "content_sha256": str(row[1]),
                    "source": "vision_frame",
                    "database_storage_path": str(row[3]),
                    "uncompressed_bytes": int(row[2]),
                    "compressed_bytes": int(row[2]),
                    "schema_fingerprint": "image/jpeg",
                    "bundle_path": _artifact_bundle_path(
                        "vision_frame_artifacts",
                        str(row[0]),
                        str(row[1]),
                        "vision_frame",
                        str(row[3]),
                    ),
                }
            )
    records.sort(key=lambda row: (str(row["registry"]), str(row["key"])))
    return records


def _verify_artifact(path: Path, record: Mapping[str, Any]) -> None:
    snapshot = read_hashed_file(
        path,
        label="bundle raw artifact",
        include_payload=True,
    )
    assert snapshot.payload is not None
    compressed = snapshot.payload
    authority = {
        "resolved_path": str(snapshot.resolved_path),
        "device": snapshot.device,
        "inode": snapshot.inode,
        "bytes": snapshot.bytes,
        "sha256": snapshot.sha256,
    }
    if snapshot.bytes != int(record["compressed_bytes"]):
        raise RuntimeError(f"bundle raw artifact compressed size mismatch: {path}")
    if snapshot.sha256 != str(record["file_sha256"]):
        raise RuntimeError(f"bundle raw artifact file hash mismatch: {path}")
    if record["registry"] == "vision_frame_artifacts":
        if (
            len(compressed) != int(record["uncompressed_bytes"])
            or hashlib.sha256(compressed).hexdigest() != str(record["content_sha256"])
            or str(record["schema_fingerprint"]) != "image/jpeg"
            or path.name.casefold() != f"{record['content_sha256']}.jpg"
        ):
            raise RuntimeError(f"bundle vision frame metadata mismatch: {path}")
        _require_file_authority(path, authority, label="bundle vision artifact")
        return
    try:
        canonical = gzip.decompress(compressed)
        payload = json.loads(canonical)
    except (OSError, EOFError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"bundle raw artifact is corrupt: {path}") from error
    if len(canonical) != int(record["uncompressed_bytes"]):
        raise RuntimeError(f"bundle raw artifact byte count mismatch: {path}")
    if hashlib.sha256(canonical).hexdigest() != str(record["content_sha256"]):
        raise RuntimeError(f"bundle raw artifact content hash mismatch: {path}")
    if schema_fingerprint(payload) != str(record["schema_fingerprint"]):
        raise RuntimeError(f"bundle raw artifact schema mismatch: {path}")
    _require_file_authority(path, authority, label="bundle raw artifact")


def _source_artifact_path(
    record: Mapping[str, Any],
    *,
    database: Path,
    odds_raw_root: Path,
    allowed_roots: tuple[Path, ...],
) -> Path:
    stored = Path(str(record["database_storage_path"]))
    if record["registry"] == "odds_raw_artifacts":
        path = _controlled_path(odds_raw_root, stored)
    else:
        path = (
            stored.resolve()
            if stored.is_absolute()
            else (database.parent / stored).resolve()
        )
        if not _inside_any(path, allowed_roots):
            raise RuntimeError(f"source raw artifact escapes allowed roots: {path}")
    return path


def _registered_odds_artifact_count(
    database: Path,
    *,
    immutable_locks: Iterable[Path],
) -> int:
    with immutable_checkpoint_reader(
        database,
        label="source database",
        required_locks=immutable_locks,
    ) as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odds_raw_artifacts'"
        ).fetchone()
        if exists is None:
            return 0
        return int(
            connection.execute("SELECT COUNT(*) FROM odds_raw_artifacts").fetchone()[0]
        )


def _required_bundle_bytes(
    database: Path,
    *,
    immutable_locks: Iterable[Path],
) -> int:
    with immutable_checkpoint_reader(
        database,
        label="source database",
        required_locks=immutable_locks,
    ) as connection:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        artifact_bytes = sum(
            int(record["compressed_bytes"])
            for record in _database_artifact_rows(connection)
        )
    return page_count * page_size + artifact_bytes + _SPACE_MARGIN_BYTES


def _require_space(root: Path, required: int, operation: str) -> None:
    available = shutil.disk_usage(root).free
    if available < required:
        raise RuntimeError(
            f"insufficient free space to {operation}: "
            f"required_bytes={required}, available_bytes={available}"
        )


def _copy_artifact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"bundle artifact path collision: {destination}")
    temporary = destination.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if os.path.samefile(source, temporary):
            raise RuntimeError("backup bundle artifact must be an independent copy")
        _replace_and_fsync(temporary, destination)
    finally:
        _unlink_and_fsync(temporary, missing_ok=True)


def _publish_directory(staging: Path, target: Path) -> None:
    _rename_and_fsync(staging, target, recursive=True)


def _bundle_result(bundle_root: Path, manifest: Mapping[str, Any]) -> BundleResult:
    total_bytes = sum(
        path.stat().st_size for path in bundle_root.rglob("*") if path.is_file()
    )
    return BundleResult(
        bundle_root,
        str(manifest["database"]["sha256"]),
        int(manifest["artifact_count"]),
        total_bytes,
    )


def _create_binding_matches(
    value: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> bool:
    return all(value.get(key) == expected for key, expected in binding.items())


def _finish_published_bundle(
    bundle_root: Path,
    checkpoint: Mapping[str, Any],
    binding: Mapping[str, Any],
    database: Path,
) -> BundleResult:
    if checkpoint.get("status") != "publishing":
        raise RuntimeError("published bundle checkpoint is not publishing")
    if not _create_binding_matches(checkpoint, binding):
        raise RuntimeError("published bundle checkpoint binding mismatch")
    _require_database_file_authority(
        database,
        checkpoint.get("source_database_identity"),
        label="source database",
    )
    _require_current_provenance(checkpoint)
    manifest = verify_database_bundle(
        bundle_root,
        _allow_staging_checkpoint=True,
    )
    if not _create_binding_matches(manifest, binding):
        raise RuntimeError("published bundle manifest binding mismatch")
    _require_database_file_authority(
        _controlled_path(bundle_root, str(manifest["database"]["path"])),
        checkpoint.get("snapshot_file_identity"),
        allow_relocated_path=True,
        label="published bundle database",
    )
    _unlink_json_with_authority(
        bundle_root / _STAGING_MANIFEST_FILE,
        checkpoint,
        label="published bundle staging manifest",
    )
    return _bundle_result(bundle_root, manifest)


def create_database_bundle(
    database: str | Path,
    odds_raw_root: str | Path,
    bundle_directory: str | Path,
    *,
    allowed_source_roots: Iterable[str | Path] = (),
    git_commit: str | None = None,
    resume: bool = False,
    adopt_resume_from_git_commit: str | None = None,
) -> BundleResult:
    """Snapshot a database and copy only artifacts registered by that snapshot."""

    database_path = Path(database).resolve()
    bundle_path = Path(bundle_directory).resolve()
    operation_lock = _operation_lock_path(bundle_path)
    with ExitStack() as locks:
        locks.enter_context(
            database_offline_authority(
                database_path,
                lock_factory=SingleInstanceLock,
            )
        )
        _require_checkpointed_source(database_path)
        locks.enter_context(SingleInstanceLock(operation_lock))
        locks.enter_context(
            file_hash_authority_scope(
                required_locks=(
                    *database_authority_lock_paths(database_path),
                    operation_lock,
                )
            )
        )
        result = _create_database_bundle_locked(
            database_path,
            Path(odds_raw_root).resolve(),
            bundle_path,
            allowed_source_roots=allowed_source_roots,
            git_commit=git_commit,
            resume=resume,
            adopt_resume_from_git_commit=adopt_resume_from_git_commit,
        )
        _require_checkpointed_source(database_path)
        return result


def _create_database_bundle_locked(
    database: Path,
    odds_raw_root: Path,
    bundle_directory: Path,
    *,
    allowed_source_roots: Iterable[str | Path],
    git_commit: str | None,
    resume: bool,
    adopt_resume_from_git_commit: str | None,
) -> BundleResult:
    if adopt_resume_from_git_commit is not None and not resume:
        raise ValueError("resume provenance adoption requires resume=True")
    if not database.is_file():
        raise FileNotFoundError(f"database does not exist: {database}")
    if bundle_directory.exists() and not resume:
        raise FileExistsError(f"bundle destination already exists: {bundle_directory}")
    try:
        database.relative_to(bundle_directory)
    except ValueError:
        pass
    else:
        raise ValueError("database must be outside the bundle destination")

    operation_lock = _operation_lock_path(bundle_directory)
    source_locks = (
        *database_authority_lock_paths(database),
        operation_lock,
    )
    roots = tuple(
        dict.fromkeys(
            [
                database.parent,
                odds_raw_root,
                *(Path(root).resolve() for root in allowed_source_roots),
            ]
        )
    )
    provenance = _source_tree_provenance()
    head = str(provenance["source_tree_head"])
    if git_commit is not None and git_commit != head:
        raise ValueError("git commit must equal the current source HEAD")
    static_binding = {
        "target": str(bundle_directory),
        "source_database": str(database),
        "odds_raw_root": str(odds_raw_root),
        "allowed_source_roots": sorted(str(root) for root in roots),
        "git_commit": head,
        **provenance,
    }
    staging = _staging_directory(bundle_directory)
    if bundle_directory.exists():
        if adopt_resume_from_git_commit is not None:
            raise RuntimeError(
                "bundle provenance adoption requires a snapshot_pending checkpoint"
            )
        if staging.exists():
            raise RuntimeError("bundle target and staging both exist")
        if bundle_directory.is_symlink() or not bundle_directory.is_dir():
            raise RuntimeError(f"published bundle path is unsafe: {bundle_directory}")
        if (bundle_directory / _STAGING_MANIFEST_FILE).exists():
            checkpoint = _read_staging_manifest(bundle_directory)
            return _finish_published_bundle(
                bundle_directory,
                checkpoint,
                static_binding,
                database,
            )
        manifest = verify_database_bundle(bundle_directory)
        if not _create_binding_matches(manifest, static_binding):
            raise RuntimeError("completed bundle manifest binding mismatch")
        _require_database_file_authority(
            database,
            manifest.get("source_database_identity"),
            label="source database",
        )
        _require_current_provenance(manifest)
        return _bundle_result(bundle_directory, manifest)

    database_identity = _unique_database_authority(
        database,
        label="source database",
    )
    odds_artifact_count = _registered_odds_artifact_count(
        database,
        immutable_locks=source_locks,
    )
    if odds_raw_root.exists() and not odds_raw_root.is_dir():
        raise FileNotFoundError(f"odds raw root does not exist: {odds_raw_root}")
    if not odds_raw_root.exists() and odds_artifact_count:
        raise FileNotFoundError(
            f"odds raw root is missing for registered raw artifacts: {odds_raw_root}"
        )
    verify_prepared_database(
        database,
        odds_raw_root=odds_raw_root,
        core_only=True,
        immutable_locks=source_locks,
    )

    bundle_directory.parent.mkdir(parents=True, exist_ok=True)
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError(f"backup bundle staging path is unsafe: {staging}")
        if not resume:
            raise FileExistsError(
                "backup bundle staging checkpoint already exists; pass resume=True "
                "to continue"
            )
        checkpoint = _read_staging_manifest(staging)
        _require_database_file_authority(
            database,
            checkpoint.get("source_database_identity"),
            label="source database",
        )
        if adopt_resume_from_git_commit is not None:
            checkpoint = _adopt_snapshot_pending_provenance(
                staging,
                checkpoint,
                static_binding,
                adopt_resume_from_git_commit,
            )
        if not _create_binding_matches(checkpoint, static_binding):
            raise RuntimeError("backup bundle staging checkpoint binding mismatch")
    else:
        if resume:
            raise FileNotFoundError("backup bundle staging checkpoint does not exist")
        _require_space(
            bundle_directory.parent,
            _required_bundle_bytes(database, immutable_locks=source_locks),
            "create backup bundle",
        )
        staging.mkdir()
        checkpoint = {
            "format": _STAGING_FORMAT,
            "status": "snapshot_pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_path": _BUNDLE_DATABASE_PATH.as_posix(),
            **static_binding,
            "source_database_identity": database_identity,
        }
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)

    snapshot = _bundle_staging_database_path(staging, checkpoint)
    status = checkpoint.get("status")
    if snapshot.parent != staging:
        if snapshot.parent.exists():
            if snapshot.parent.is_symlink() or not snapshot.parent.is_dir():
                raise RuntimeError("bundle staging database directory is unsafe")
        elif status == "snapshot_pending":
            snapshot.parent.mkdir()
            fsync_directory(staging)
        else:
            raise RuntimeError("bundle staging database directory is missing")
    if status == "snapshot_pending":
        if snapshot.exists():
            if snapshot.is_dir() or snapshot.is_symlink():
                raise RuntimeError("backup bundle staging database is unsafe")
            _unlink_and_fsync(snapshot)
        _require_database_file_authority(
            database,
            checkpoint.get("source_database_identity"),
            label="source database",
        )
        _require_checkpointed_source(database)
        invalidate_hashed_paths(snapshot)
        online_backup(
            database,
            snapshot,
            immutable_locks=source_locks,
        )
        invalidate_hashed_paths(snapshot)
        _require_checkpointed_source(database)
        _prepare_runtime_database(snapshot)
        _clear_quiescent_sqlite_sidecars(snapshot)
        _require_database_file_authority(
            database,
            checkpoint.get("source_database_identity"),
            label="source database",
        )
        verify_prepared_database(
            snapshot,
            odds_raw_root=odds_raw_root,
            immutable_locks=(operation_lock,),
        )
        snapshot_identity = _unique_database_authority(
            snapshot,
            label="bundle staging database",
        )
        with immutable_checkpoint_reader(
            snapshot,
            label="bundle staging database",
            required_locks=(operation_lock,),
        ) as connection:
            schema = _schema_manifest(connection)
        checkpoint.update(
            {
                "status": "copying",
                "database_bytes": snapshot_identity["bytes"],
                "database_sha256": snapshot_identity["sha256"],
                "snapshot_file_identity": snapshot_identity,
                "schema": schema,
            }
        )
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "copying"

    if status == "copying":
        _require_database_file_authority(
            snapshot,
            checkpoint.get("snapshot_file_identity"),
            label="bundle staging database",
        )
        verify_prepared_database(
            snapshot,
            odds_raw_root=odds_raw_root,
            immutable_locks=(operation_lock,),
        )
        with immutable_checkpoint_reader(
            snapshot,
            label="bundle staging database",
            required_locks=(operation_lock,),
        ) as connection:
            schema = _schema_manifest(connection)
            records = _database_artifact_rows(connection)
        if schema != checkpoint.get("schema"):
            raise RuntimeError("backup bundle staging schema differs from checkpoint")
        artifact_bytes = sum(int(record["compressed_bytes"]) for record in records)
        missing_bytes = sum(
            int(record["compressed_bytes"])
            for record in records
            if not _controlled_path(staging, str(record["bundle_path"])).exists()
        )
        _require_space(
            staging.parent,
            missing_bytes + _SPACE_MARGIN_BYTES,
            "resume backup artifact copies",
        )
        seen_paths: dict[str, str] = {}
        for record in records:
            source = _source_artifact_path(
                record,
                database=database,
                odds_raw_root=odds_raw_root,
                allowed_roots=roots,
            )
            source_record = dict(record)
            source_record["file_sha256"] = (
                _sha256_file(source) if source.is_file() else ""
            )
            _verify_artifact(source, source_record)
            bundle_path = str(record["bundle_path"])
            prior = seen_paths.get(bundle_path)
            if prior is not None and prior != str(record["content_sha256"]):
                raise RuntimeError("two database artifacts collide in the bundle path")
            seen_paths[bundle_path] = str(record["content_sha256"])
            destination = _controlled_path(staging, bundle_path)
            if not destination.exists():
                _copy_artifact(source, destination)
            record["file_sha256"] = source_record["file_sha256"]
            _verify_artifact(destination, record)

        manifest: dict[str, Any] = {
            "format": _BUNDLE_FORMAT,
            "created_at": checkpoint["created_at"],
            **static_binding,
            "publication_target": str(bundle_directory),
            "source_database_identity": checkpoint["source_database_identity"],
            "database": {
                "path": snapshot.relative_to(staging).as_posix(),
                "bytes": checkpoint["database_bytes"],
                "sha256": checkpoint["database_sha256"],
            },
            "schema": schema,
            "artifacts": records,
            "artifact_count": len(records),
            "artifact_bytes": artifact_bytes,
        }
        if "provenance_recovery" in checkpoint:
            manifest["provenance_recovery"] = checkpoint["provenance_recovery"]
        _write_json(staging / _MANIFEST_FILE, manifest)
        _verify_database_bundle(
            staging,
            _allow_staging_checkpoint=True,
            _held_operation_lock=operation_lock,
        )
        checkpoint["status"] = "ready"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "ready"

    if status == "ready":
        manifest = _verify_database_bundle(
            staging,
            _allow_staging_checkpoint=True,
            _held_operation_lock=operation_lock,
        )
        if not _create_binding_matches(manifest, static_binding):
            raise RuntimeError("ready backup staging binding mismatch")
        _require_database_file_authority(
            snapshot,
            checkpoint.get("snapshot_file_identity"),
            label="bundle staging database",
        )
        _require_database_file_authority(
            database,
            checkpoint.get("source_database_identity"),
            label="source database",
        )
        _require_current_provenance(checkpoint)
        checkpoint["status"] = "publishing"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "publishing"

    if status != "publishing":
        raise RuntimeError(f"backup bundle staging status is invalid: {status}")
    _verify_database_bundle(
        staging,
        _allow_staging_checkpoint=True,
        _held_operation_lock=operation_lock,
    )
    _require_database_file_authority(
        snapshot,
        checkpoint.get("snapshot_file_identity"),
        label="bundle staging database",
    )
    _require_database_file_authority(
        database,
        checkpoint.get("source_database_identity"),
        label="source database",
    )
    _require_current_provenance(checkpoint)
    _publish_directory(staging, bundle_directory)
    return _finish_published_bundle(
        bundle_directory,
        checkpoint,
        static_binding,
        database,
    )


def verify_database_bundle(
    bundle_directory: str | Path,
    *,
    _allow_staging_checkpoint: bool = False,
) -> dict[str, Any]:
    bundle_root = Path(bundle_directory).resolve()
    if not bundle_root.is_dir():
        raise FileNotFoundError(f"backup bundle does not exist: {bundle_root}")
    operation_lock = _operation_lock_path(bundle_root)
    if hash_authority_scope_covers((operation_lock,)):
        return _verify_database_bundle(
            bundle_root,
            _allow_staging_checkpoint=_allow_staging_checkpoint,
            _held_operation_lock=operation_lock,
        )
    with SingleInstanceLock(operation_lock):
        with file_hash_authority_scope(required_locks=(operation_lock,)):
            return _verify_database_bundle(
                bundle_root,
                _allow_staging_checkpoint=_allow_staging_checkpoint,
                _held_operation_lock=operation_lock,
            )


def _verify_database_bundle(
    bundle_directory: str | Path,
    *,
    _allow_staging_checkpoint: bool = False,
    _held_operation_lock: Path,
) -> dict[str, Any]:
    """Verify the database, manifest, and every registered artifact."""

    bundle_root = Path(bundle_directory).resolve()
    if not bundle_root.is_dir():
        raise FileNotFoundError(f"backup bundle does not exist: {bundle_root}")
    if (
        not _allow_staging_checkpoint
        and (bundle_root / _STAGING_MANIFEST_FILE).exists()
    ):
        raise RuntimeError("backup bundle publication is incomplete")
    manifest, manifest_identity = _read_json_with_authority(
        bundle_root / _MANIFEST_FILE,
        label="backup bundle manifest",
    )
    if not isinstance(manifest, dict) or manifest.get("format") != _BUNDLE_FORMAT:
        raise RuntimeError("backup bundle manifest has the wrong format")
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(manifest.get("git_commit", ""))):
        raise RuntimeError("backup bundle git commit is invalid")
    if manifest.get("source_tree_clean") is not True:
        raise RuntimeError("backup bundle source tree is not clean")
    if manifest.get("source_tree_policy_version") != _SOURCE_TREE_POLICY_VERSION:
        raise RuntimeError("backup bundle source tree policy is invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(manifest.get("source_tree_head", ""))):
        raise RuntimeError("backup bundle source tree head is invalid")
    if manifest.get("git_commit") != manifest.get("source_tree_head"):
        raise RuntimeError("backup bundle git commit differs from source HEAD")
    runtime_dirty_paths = manifest.get("source_tree_runtime_dirty_paths")
    if not isinstance(runtime_dirty_paths, list) or any(
        not isinstance(path, str) or not _runtime_only_path(path)
        for path in runtime_dirty_paths
    ):
        raise RuntimeError("backup bundle runtime dirty path policy is invalid")
    if "provenance_recovery" in manifest:
        _require_provenance_recovery_audit(
            manifest["provenance_recovery"],
            manifest,
        )
    source_identity = manifest.get("source_database_identity")
    if (
        not isinstance(source_identity, dict)
        or not isinstance(source_identity.get("resolved_path"), str)
        or not isinstance(source_identity.get("device"), int)
        or not isinstance(source_identity.get("inode"), int)
        or int(source_identity.get("inode", 0)) <= 0
        or not isinstance(source_identity.get("bytes"), int)
        or int(source_identity.get("bytes", -1)) < 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(source_identity.get("sha256", "")))
    ):
        raise RuntimeError("backup bundle source database identity is invalid")
    if not isinstance(manifest.get("source_database"), str):
        raise RuntimeError("backup bundle source database path is invalid")
    if not isinstance(manifest.get("odds_raw_root"), str):
        raise RuntimeError("backup bundle odds raw root is invalid")
    allowed_roots = manifest.get("allowed_source_roots")
    if not isinstance(allowed_roots, list) or any(
        not isinstance(root, str) for root in allowed_roots
    ):
        raise RuntimeError("backup bundle allowed source roots are invalid")
    database_meta = manifest.get("database")
    if not isinstance(database_meta, dict):
        raise RuntimeError("backup bundle database manifest is invalid")
    database = _controlled_path(bundle_root, str(database_meta.get("path", "")))
    database_identity = _unique_database_authority(
        database,
        label="backup bundle database",
    )
    if database_identity["bytes"] != int(database_meta.get("bytes", -1)):
        raise RuntimeError("backup bundle database size mismatch")
    if database_identity["sha256"] != str(database_meta.get("sha256", "")):
        raise RuntimeError("backup bundle database hash mismatch")
    verify_prepared_database(
        database,
        odds_raw_root=bundle_root / "raw" / "odds",
        verify_vision_frames=False,
        immutable_locks=(_held_operation_lock,),
    )
    with immutable_checkpoint_reader(
        database,
        label="backup bundle database",
        required_locks=(_held_operation_lock,),
    ) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("backup bundle database failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("backup bundle database failed foreign_key_check")
        if _schema_manifest(connection) != manifest.get("schema"):
            raise RuntimeError("backup bundle database schema manifest mismatch")
        database_records = _database_artifact_rows(connection)

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise RuntimeError("backup bundle artifact manifest is invalid")
    if int(manifest.get("artifact_count", -1)) != len(artifacts):
        raise RuntimeError("backup bundle artifact count mismatch")
    if int(manifest.get("artifact_bytes", -1)) != sum(
        int(item.get("compressed_bytes", -1))
        for item in artifacts
        if isinstance(item, dict)
    ):
        raise RuntimeError("backup bundle artifact byte count mismatch")
    manifest_keys: set[tuple[str, str]] = set()
    manifest_paths: set[str] = set()
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            raise RuntimeError("backup bundle artifact entry is invalid")
        key = (str(item.get("registry", "")), str(item.get("key", "")))
        path = str(item.get("bundle_path", ""))
        if key in manifest_keys or path in manifest_paths:
            raise RuntimeError("backup bundle contains duplicate artifact entries")
        manifest_keys.add(key)
        manifest_paths.add(path)
        by_key[key] = item
        _verify_artifact(_controlled_path(bundle_root, path), item)

    expected_keys = {
        (str(record["registry"]), str(record["key"])) for record in database_records
    }
    if manifest_keys != expected_keys:
        raise RuntimeError("backup bundle artifact set differs from its database")
    for record in database_records:
        key = (str(record["registry"]), str(record["key"]))
        item = by_key[key]
        for field in (
            "content_sha256",
            "source",
            "database_storage_path",
            "uncompressed_bytes",
            "compressed_bytes",
            "schema_fingerprint",
            "bundle_path",
        ):
            if item.get(field) != record[field]:
                raise RuntimeError(
                    f"backup artifact metadata differs from database: {key[0]}:{key[1]}"
                )
    actual_artifacts: set[str] = set()
    for directory in (bundle_root / "raw", bundle_root / "vision"):
        if directory.exists():
            actual_artifacts.update(
                path.relative_to(bundle_root).as_posix()
                for path in directory.rglob("*")
                if path.is_file()
            )
    if actual_artifacts != manifest_paths:
        raise RuntimeError(
            "backup bundle artifact tree has missing or unregistered files"
        )
    _require_database_file_authority(
        database,
        database_identity,
        label="backup bundle database",
    )
    _require_file_authority(
        bundle_root / _MANIFEST_FILE,
        manifest_identity,
        label="backup bundle manifest",
    )
    return manifest


def _relocate_raw_source_paths(
    database: Path,
    replacements: Mapping[str, str],
    incoming_files: Mapping[str, str],
    *,
    final_root: Path,
    staging_root: Path,
) -> tuple[str, ...]:
    if not replacements:
        return ()
    invalidate_hashed_paths(database)
    connection = connect(database)
    try:
        return relocate_raw_source_artifacts(
            connection,
            replacements,
            allowed_new_roots=[final_root],
            incoming_files=incoming_files,
            allowed_incoming_roots=[staging_root],
            reason="database bundle restore",
            actor="live_betting.database_bundle",
            relocated_at=datetime.now(timezone.utc),
        )
    finally:
        connection.close()
        invalidate_hashed_paths(database)


def _relocate_vision_frame_paths(
    database: Path,
    replacements: Mapping[str, str],
    incoming_files: Mapping[str, str],
    *,
    final_root: Path,
    staging_root: Path,
) -> tuple[str, ...]:
    if not replacements:
        return ()
    invalidate_hashed_paths(database)
    connection = connect(database)
    try:
        return relocate_vision_frame_artifacts(
            connection,
            replacements,
            allowed_new_roots=[final_root],
            incoming_files=incoming_files,
            allowed_incoming_roots=[staging_root],
            reason="database bundle restore",
            actor="live_betting.database_bundle",
            relocated_at=datetime.now(timezone.utc),
        )
    finally:
        connection.close()
        invalidate_hashed_paths(database)


def _restore_layout(
    bundle_root: Path,
    restore_root: Path,
    staging: Path,
    manifest: Mapping[str, Any],
    database_name: str,
    *,
    staging_database: Path | None = None,
) -> tuple[
    Path,
    Path,
    Path,
    Path,
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    list[tuple[Path, Path, Mapping[str, Any] | None]],
]:
    restored_database = (
        staging / database_name if staging_database is None else staging_database
    )
    odds_root = staging / "live_betting" / "raw-v2"
    source_root = staging / "raw-sources"
    vision_root = staging / "live_betting" / "live_evidence"
    bundle_database = _controlled_path(bundle_root, str(manifest["database"]["path"]))
    copies: list[tuple[Path, Path, Mapping[str, Any] | None]] = [
        (bundle_database, restored_database, None)
    ]
    replacements: dict[str, str] = {}
    relocation_inputs: dict[str, str] = {}
    vision_replacements: dict[str, str] = {}
    vision_relocation_inputs: dict[str, str] = {}
    for record in manifest["artifacts"]:
        source = _controlled_path(bundle_root, str(record["bundle_path"]))
        if record["registry"] == "odds_raw_artifacts":
            destination = _controlled_path(
                odds_root, str(record["database_storage_path"])
            )
        elif record["registry"] == "raw_source_artifacts":
            relative = Path(str(record["bundle_path"])).relative_to(
                Path("raw") / "sources"
            )
            destination = _controlled_path(source_root, relative)
            artifact_id = str(record["key"])
            replacements[artifact_id] = str(
                _controlled_path(restore_root / "raw-sources", relative)
            )
            relocation_inputs[artifact_id] = str(destination)
        elif record["registry"] == "vision_frame_artifacts":
            relative = Path(str(record["bundle_path"])).relative_to("vision")
            destination = _controlled_path(vision_root, relative)
            frame_ref = str(record["key"])
            vision_replacements[frame_ref] = str(
                _controlled_path(
                    restore_root / "live_betting" / "live_evidence",
                    relative,
                )
            )
            vision_relocation_inputs[frame_ref] = str(destination)
        else:
            raise RuntimeError("restore manifest contains an unknown registry")
        copies.append((source, destination, record))
    return (
        restored_database,
        odds_root,
        source_root,
        vision_root,
        replacements,
        relocation_inputs,
        vision_replacements,
        vision_relocation_inputs,
        copies,
    )


def _verify_restore_copy(
    source: Path,
    destination: Path,
    record: Mapping[str, Any] | None,
    database_meta: Mapping[str, Any],
    *,
    require_database_hash: bool = True,
) -> None:
    if record is None:
        source_identity = _unique_database_authority(
            source,
            label="bundle source database",
        )
        destination_identity = _unique_database_authority(
            destination,
            label="restore staging database",
        )
    else:
        source_identity = _unique_file_authority(source)
        destination_identity = _unique_file_authority(destination)
    if os.path.samefile(source, destination):
        raise RuntimeError("restore staging copy must be independent from bundle")
    if record is None:
        if (
            source_identity["bytes"] != int(database_meta["bytes"])
            or source_identity["sha256"] != str(database_meta["sha256"])
        ):
            raise RuntimeError("bundle source database differs from manifest")
        if require_database_hash and (
            destination_identity["bytes"] != int(database_meta["bytes"])
            or destination_identity["sha256"] != str(database_meta["sha256"])
        ):
            raise RuntimeError("restore staging database differs from bundle")
    else:
        if (
            source_identity["bytes"] != int(record["compressed_bytes"])
            or source_identity["sha256"] != str(record["file_sha256"])
        ):
            raise RuntimeError("bundle source artifact differs from manifest")
        _verify_artifact(destination, record)
    if record is None:
        _require_database_file_authority(
            source,
            source_identity,
            label="bundle source database",
        )
        _require_database_file_authority(
            destination,
            destination_identity,
            label="restore staging database",
        )
    else:
        _require_file_authority(source, source_identity, label="bundle source")
        _require_file_authority(
            destination,
            destination_identity,
            label="restore staging copy",
        )


def _restore_relocation_ids(
    database: Path,
    replacements: Mapping[str, str],
    *,
    immutable_locks: Iterable[Path],
) -> tuple[str, ...]:
    if not replacements:
        return ()
    _require_transaction_free_database(database, label="restored database")
    with immutable_checkpoint_reader(
        database,
        label="restored database relocation audit",
        required_locks=immutable_locks,
    ) as connection:
        ids: list[str] = []
        for artifact_id, expected_path in sorted(replacements.items()):
            row = connection.execute(
                """SELECT artifact.storage_path, artifact.content_hash,
                          artifact.source, artifact.uncompressed_bytes,
                          artifact.compressed_bytes, artifact.schema_fingerprint,
                          relocation.relocation_id,
                          relocation.relocation_sequence,
                          relocation.content_hash, relocation.source,
                          relocation.old_storage_path, relocation.new_storage_path,
                          relocation.uncompressed_bytes,
                          relocation.compressed_bytes,
                          relocation.schema_fingerprint, relocation.reason,
                          relocation.actor, relocation.relocated_at
                     FROM raw_source_artifacts AS artifact
                     JOIN raw_source_artifact_relocations AS relocation
                       ON relocation.artifact_id=artifact.artifact_id
                    WHERE artifact.artifact_id=?
                    ORDER BY relocation.relocation_sequence DESC LIMIT 1""",
                (artifact_id,),
            ).fetchone()
            if (
                row is None
                or str(row[0]) != expected_path
                or str(row[11]) != expected_path
                or tuple(row[1:6]) != tuple(row[8:10]) + tuple(row[12:15])
                or str(row[15]) != "database bundle restore"
                or str(row[16]) != "live_betting.database_bundle"
            ):
                raise RuntimeError(
                    "restored source artifact relocation audit is missing"
                )
            payload = {
                "artifact_id": artifact_id,
                "content_hash": str(row[8]),
                "source": str(row[9]),
                "old_storage_path": str(row[10]),
                "new_storage_path": str(row[11]),
                "uncompressed_bytes": int(row[12]),
                "compressed_bytes": int(row[13]),
                "schema_fingerprint": str(row[14]),
                "reason": str(row[15]),
                "actor": str(row[16]),
                "relocated_at": str(row[17]),
                "relocation_sequence": int(row[7]),
            }
            if raw_source_relocation_id(payload) != str(row[6]):
                raise RuntimeError("restored source artifact relocation id is invalid")
            ids.append(str(row[6]))
        result = tuple(ids)
    _require_transaction_free_database(database, label="restored database")
    return result


def _restore_vision_relocation_ids(
    database: Path,
    replacements: Mapping[str, str],
    *,
    immutable_locks: Iterable[Path],
) -> tuple[str, ...]:
    if not replacements:
        return ()
    _require_transaction_free_database(database, label="restored database")
    with immutable_checkpoint_reader(
        database,
        label="restored database vision relocation audit",
        required_locks=immutable_locks,
    ) as connection:
        verify_vision_frame_registry(connection, require_active_files=False)
        ids: list[str] = []
        for frame_ref, expected_path in sorted(replacements.items()):
            row = connection.execute(
                """SELECT artifact.content_sha256, artifact.byte_length,
                          relocation.relocation_id,
                          relocation.relocation_sequence,
                          relocation.content_sha256, relocation.byte_length,
                          relocation.old_storage_path,
                          relocation.new_storage_path, relocation.reason,
                          relocation.actor, relocation.relocated_at
                     FROM vision_frame_artifacts AS artifact
                     JOIN vision_frame_artifact_relocations AS relocation
                       ON relocation.frame_ref=artifact.frame_ref
                    WHERE artifact.frame_ref=?
                    ORDER BY relocation.relocation_sequence DESC LIMIT 1""",
                (frame_ref,),
            ).fetchone()
            if (
                row is None
                or str(row[7]) != expected_path
                or str(row[0]) != str(row[4])
                or int(row[1]) != int(row[5])
                or str(row[8]) != "database bundle restore"
                or str(row[9]) != "live_betting.database_bundle"
            ):
                raise RuntimeError("restored vision frame relocation audit is missing")
            payload = {
                "frame_ref": frame_ref,
                "content_sha256": str(row[4]),
                "byte_length": int(row[5]),
                "old_storage_path": str(row[6]),
                "new_storage_path": str(row[7]),
                "reason": str(row[8]),
                "actor": str(row[9]),
                "relocated_at": str(row[10]),
                "relocation_sequence": int(row[3]),
            }
            if vision_frame_relocation_id(payload) != str(row[2]):
                raise RuntimeError("restored vision frame relocation id is invalid")
            ids.append(str(row[2]))
        result = tuple(ids)
    _require_transaction_free_database(database, label="restored database")
    return result


def _verify_restored_database(
    database: Path,
    odds_root: Path,
    replacements: Mapping[str, str],
    vision_replacements: Mapping[str, str],
    *,
    verify_vision_files: bool,
    immutable_locks: Iterable[Path],
) -> str:
    _require_transaction_free_database(database, label="restored database")
    verify_prepared_database(
        database,
        odds_raw_root=odds_root,
        verify_vision_frames=verify_vision_files,
        immutable_locks=immutable_locks,
    )
    with immutable_checkpoint_reader(
        database,
        label="restored database verification",
        required_locks=immutable_locks,
    ) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("restored database failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("restored database failed foreign_key_check")
        for artifact_id, expected_path in replacements.items():
            row = connection.execute(
                "SELECT storage_path FROM raw_source_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None or str(row[0]) != expected_path:
                raise RuntimeError("restored source artifact path was not relocated")
        for frame_ref, expected_path in vision_replacements.items():
            row = connection.execute(
                """SELECT storage_path FROM active_vision_frame_artifacts
                    WHERE frame_ref=?""",
                (frame_ref,),
            ).fetchone()
            if row is None or str(row[0]) != expected_path:
                raise RuntimeError("restored vision frame path was not relocated")
    authority = _unique_database_authority(
        database,
        label="restored database",
    )
    return str(authority["sha256"])


def _physical_file_authority(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value.get(field) for field in _FILE_AUTHORITY_FIELDS}


def _restore_binding_matches(
    value: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> bool:
    return all(value.get(key) == expected for key, expected in binding.items())


def _restore_manifest_payload(
    *,
    bundle_manifest_hash: str,
    bundle_manifest: Mapping[str, Any],
    restored_hash: str,
    restore_root: Path,
    database_name: str,
    relocation_ids: tuple[str, ...],
    vision_relocation_ids: tuple[str, ...],
    bundle_manifest_identity: Mapping[str, Any],
    bundle_database_identity: Mapping[str, Any],
    restored_database_identity: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "format": "dota2-database-bundle-restore-v1",
        "bundle_manifest_sha256": bundle_manifest_hash,
        "bundle_manifest_identity": dict(bundle_manifest_identity),
        "bundle_database_sha256": bundle_manifest["database"]["sha256"],
        "bundle_database_identity": dict(bundle_database_identity),
        "restored_database_sha256": restored_hash,
        "restored_database_identity": _physical_file_authority(
            restored_database_identity
        ),
        "restore_target": str(restore_root),
        "database": database_name,
        "artifact_count": bundle_manifest["artifact_count"],
        "raw_source_relocation_ids": list(relocation_ids),
        "vision_frame_relocation_ids": list(vision_relocation_ids),
    }


def _require_completed_restore_database_binding(
    result: RestoreResult,
    restore_root: Path,
    database_name: str,
) -> tuple[dict[str, Any], dict[str, Any], DatabaseFileIdentity]:
    manifest_path = restore_root / _RESTORE_MANIFEST_FILE
    manifest, manifest_identity = _read_json_with_authority(
        manifest_path,
        label="completed restore manifest",
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "dota2-database-bundle-restore-v1"
        or manifest.get("restore_target") != str(restore_root)
        or manifest.get("database") != database_name
        or manifest.get("restored_database_sha256")
        != result.restored_database_sha256
    ):
        raise RuntimeError("completed restore manifest binding differs")
    expected_database = manifest.get("restored_database_identity")
    if (
        not isinstance(expected_database, Mapping)
        or expected_database.get("sha256") != result.restored_database_sha256
    ):
        raise RuntimeError("completed restore database authority differs")
    completed_database = _require_database_file_authority(
        result.database,
        expected_database,
        allow_relocated_path=True,
        label="final restored database",
    )
    database_identity = DatabaseFileIdentity(
        result.database.resolve(),
        int(completed_database["device"]),
        int(completed_database["inode"]),
    )
    _require_file_authority(
        manifest_path,
        manifest_identity,
        label="completed restore manifest",
    )
    return manifest, manifest_identity, database_identity


def _require_completed_restore_authority(
    result: RestoreResult,
    restore_root: Path,
    database_name: str,
    expected_manifest: Mapping[str, Any],
) -> tuple[DatabaseFileIdentity, dict[str, Any]]:
    (
        manifest,
        manifest_identity,
        database_identity,
    ) = _require_completed_restore_database_binding(
        result,
        restore_root,
        database_name,
    )
    if manifest != expected_manifest:
        raise RuntimeError("completed restore manifest binding differs")
    if manifest_identity["sha256"] != _canonical_json_file_sha256(
        expected_manifest
    ):
        raise RuntimeError("completed restore manifest is not canonical")
    _require_file_authority(
        restore_root / _RESTORE_MANIFEST_FILE,
        manifest_identity,
        label="completed restore manifest",
    )
    return (
        database_identity,
        manifest_identity,
    )


def _bind_published_database_identity(
    database: Path,
    expected: object,
    callback: Callable[[DatabaseFileIdentity], None],
    *,
    label: str,
) -> dict[str, Any]:
    authority = _require_database_file_authority(
        database,
        expected,
        allow_relocated_path=True,
        label=label,
    )
    identity = DatabaseFileIdentity(
        database.resolve(),
        int(authority["device"]),
        int(authority["inode"]),
    )
    callback(identity)
    return authority


def _replace_database_and_bind_identity(
    source: Path,
    destination: Path,
    expected: object,
    callback: Callable[[DatabaseFileIdentity], None],
) -> None:
    source_parent = capture_directory_identity(
        source.parent,
        label="database publish source parent",
    )
    destination_parent = capture_directory_identity(
        destination.parent,
        label="database publish destination parent",
    )
    staged = _require_database_file_authority(
        source,
        expected,
        label="restore staging database before publication",
    )
    published_identity = DatabaseFileIdentity(
        destination.resolve(),
        int(staged["device"]),
        int(staged["inode"]),
    )
    try:
        os.replace(source, destination)
    except BaseException:
        try:
            require_unique_database_file(
                destination,
                expected_identity=published_identity,
            )
        except Exception:
            pass
        else:
            callback(published_identity)
        invalidate_hashed_paths(source, destination)
        raise
    callback(published_identity)
    _fsync_bound_directories(source_parent, destination_parent)
    rebind_hashed_paths(source, destination)
    _bind_published_database_identity(
        destination,
        expected,
        callback,
        label="published restore database",
    )


def _finish_published_restore(
    bundle_root: Path,
    restore_root: Path,
    database_name: str,
    manifest: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    database_published: Callable[[DatabaseFileIdentity], None],
    restore_root_identity: DirectoryIdentity,
    immutable_locks: Iterable[Path],
) -> RestoreResult:
    require_directory_identity(restore_root_identity, label="restore root")
    if checkpoint.get("status") != "publishing":
        raise RuntimeError("published restore checkpoint is not publishing")
    if not _restore_binding_matches(checkpoint, binding):
        raise RuntimeError("published restore checkpoint binding mismatch")
    final_database = restore_root / database_name
    _bind_published_database_identity(
        final_database,
        checkpoint.get("restored_database_identity"),
        database_published,
        label="published restore database",
    )
    _require_file_authority(
        bundle_root / _MANIFEST_FILE,
        checkpoint.get("bundle_manifest_identity"),
        label="bundle manifest",
    )
    _require_database_file_authority(
        _controlled_path(bundle_root, str(manifest["database"]["path"])),
        checkpoint.get("bundle_database_identity"),
        label="bundle database",
    )
    (
        restored_database,
        odds_root,
        source_root,
        vision_root,
        replacements,
        _,
        vision_replacements,
        _,
        copies,
    ) = _restore_layout(
        bundle_root,
        restore_root,
        restore_root,
        manifest,
        database_name,
    )
    for source, destination, record in copies:
        _verify_restore_copy(
            source,
            destination,
            record,
            manifest["database"],
            require_database_hash=False,
        )
    relocation_ids = _restore_relocation_ids(
        restored_database,
        replacements,
        immutable_locks=immutable_locks,
    )
    vision_relocation_ids = _restore_vision_relocation_ids(
        restored_database,
        vision_replacements,
        immutable_locks=immutable_locks,
    )
    if list(relocation_ids) != checkpoint.get("raw_source_relocation_ids", []):
        raise RuntimeError("published restore relocation audit differs from checkpoint")
    if list(vision_relocation_ids) != checkpoint.get("vision_frame_relocation_ids", []):
        raise RuntimeError("published vision relocation audit differs from checkpoint")
    final_hash = _verify_restored_database(
        restored_database,
        odds_root,
        replacements,
        vision_replacements,
        verify_vision_files=True,
        immutable_locks=immutable_locks,
    )
    if final_hash != checkpoint.get("restored_database_sha256"):
        raise RuntimeError("published restore database differs from checkpoint")
    restored_identity = _require_database_file_authority(
        restored_database,
        checkpoint.get("restored_database_identity"),
        allow_relocated_path=True,
        label="published restore database",
    )
    restore_manifest_path = restore_root / _RESTORE_MANIFEST_FILE
    _require_file_authority(
        restore_manifest_path,
        checkpoint.get("restore_manifest_identity"),
        allow_relocated_path=True,
        label="published restore manifest",
    )
    restore_manifest, restore_manifest_identity = _read_json_with_authority(
        restore_manifest_path,
        label="published restore manifest",
    )
    expected_restore_manifest = _restore_manifest_payload(
        bundle_manifest_hash=str(binding["bundle_manifest_sha256"]),
        bundle_manifest=manifest,
        restored_hash=final_hash,
        restore_root=restore_root,
        database_name=database_name,
        relocation_ids=relocation_ids,
        vision_relocation_ids=vision_relocation_ids,
        bundle_manifest_identity=checkpoint["bundle_manifest_identity"],
        bundle_database_identity=checkpoint["bundle_database_identity"],
        restored_database_identity=restored_identity,
    )
    if restore_manifest != expected_restore_manifest:
        raise RuntimeError(
            "published restore manifest differs from checkpoint authority"
        )
    if restore_manifest_identity["sha256"] != _canonical_json_file_sha256(
        expected_restore_manifest
    ):
        raise RuntimeError("published restore manifest is not canonical")
    _require_file_authority(
        restore_manifest_path,
        checkpoint.get("restore_manifest_identity"),
        allow_relocated_path=True,
        label="published restore manifest",
    )
    _require_file_authority(
        bundle_root / _MANIFEST_FILE,
        checkpoint.get("bundle_manifest_identity"),
        label="bundle manifest",
    )
    _require_database_file_authority(
        _controlled_path(bundle_root, str(manifest["database"]["path"])),
        checkpoint.get("bundle_database_identity"),
        label="bundle database",
    )
    _unlink_and_fsync(
        restore_root / _STAGING_MANIFEST_FILE,
        missing_ok=True,
    )
    result = RestoreResult(
        restore_root,
        restored_database,
        odds_root,
        source_root,
        vision_root,
        final_hash,
    )
    require_directory_identity(restore_root_identity, label="restore root")
    return result


def _restore_root_allowed_paths(
    bundle_root: Path,
    restore_root: Path,
    database_name: str,
    manifest: Mapping[str, Any],
) -> tuple[set[Path], set[Path]]:
    final_database = restore_root / database_name
    files = {
        path.resolve()
        for path in database_local_authority_lock_paths(final_database)
    }
    files.update(path.with_name(f"{path.name}.owner") for path in tuple(files))
    files.add((restore_root / _RESTORE_MANIFEST_FILE).resolve())
    for suffix in ("-wal", "-shm", "-journal"):
        files.add(Path(f"{final_database}{suffix}").resolve())
    (
        _,
        _,
        _,
        _,
        _,
        _,
        _,
        _,
        copies,
    ) = _restore_layout(
        bundle_root,
        restore_root,
        restore_root,
        manifest,
        database_name,
    )
    files.update(destination.resolve() for _, destination, _ in copies)
    directories = {restore_root.resolve()}
    for path in files:
        parent = path.parent
        while parent != restore_root.parent:
            directories.add(parent)
            if parent == restore_root:
                break
            parent = parent.parent
    return files, directories


def _require_restore_root_inventory(
    bundle_root: Path,
    restore_root: Path,
    database_name: str,
    manifest: Mapping[str, Any],
) -> None:
    allowed_files, allowed_directories = _restore_root_allowed_paths(
        bundle_root,
        restore_root,
        database_name,
        manifest,
    )
    for path in restore_root.rglob("*"):
        resolved = path.resolve()
        if path.is_symlink():
            raise RuntimeError(f"restore target contains a symlink: {path}")
        if path.is_dir():
            if resolved not in allowed_directories:
                raise RuntimeError(f"restore target contains an unknown directory: {path}")
        elif resolved not in allowed_files:
            raise RuntimeError(f"restore target contains an unknown file: {path}")


def _verify_published_dependencies(
    bundle_root: Path,
    restore_root: Path,
    database_name: str,
    manifest: Mapping[str, Any],
) -> None:
    *_, copies = _restore_layout(
        bundle_root,
        restore_root,
        restore_root,
        manifest,
        database_name,
    )
    for source, destination, record in copies:
        if record is None:
            continue
        _verify_restore_copy(source, destination, record, manifest["database"])


def _publish_restore_items(
    bundle_root: Path,
    restore_root: Path,
    staging: Path,
    database_name: str,
    manifest: Mapping[str, Any],
    checkpoint: dict[str, Any],
    *,
    phase_hook: Callable[[str], None] | None,
    database_published: Callable[[DatabaseFileIdentity], None],
    restore_root_identity: DirectoryIdentity,
) -> None:
    require_directory_identity(restore_root_identity, label="restore root")
    published = list(checkpoint.get("published_items", []))
    restore_manifest_identity = checkpoint.get("restore_manifest_identity")
    if not isinstance(restore_manifest_identity, Mapping):
        raise RuntimeError("restore manifest authority is missing before publication")
    if len(published) != len(set(published)) or any(
        item not in {"live_betting", "raw-sources", _RESTORE_MANIFEST_FILE}
        for item in published
    ):
        raise RuntimeError("restore publication checkpoint item list is invalid")
    for item in ("live_betting", "raw-sources", _RESTORE_MANIFEST_FILE):
        require_directory_identity(restore_root_identity, label="restore root")
        source = staging / item
        destination = restore_root / item
        if item in published:
            if source.exists() or not destination.exists():
                raise RuntimeError(f"published restore item state differs: {item}")
            if item == _RESTORE_MANIFEST_FILE:
                _require_file_authority(
                    destination,
                    restore_manifest_identity,
                    allow_relocated_path=True,
                    label="published restore manifest",
                )
            continue
        if not source.exists():
            if destination.exists():
                if item == _RESTORE_MANIFEST_FILE:
                    _require_file_authority(
                        destination,
                        restore_manifest_identity,
                        allow_relocated_path=True,
                        label="recovered published restore manifest",
                    )
                published.append(item)
                checkpoint["published_items"] = published
                _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
                continue
            if item != _RESTORE_MANIFEST_FILE:
                continue
            raise RuntimeError("restore manifest is missing before publication")
        if destination.exists():
            raise RuntimeError(f"restore publication target already exists: {item}")
        if item == _RESTORE_MANIFEST_FILE:
            _require_file_authority(
                source,
                restore_manifest_identity,
                label="restore manifest before publication",
            )
        _rename_and_fsync(
            source,
            destination,
            recursive=source.is_dir(),
        )
        if phase_hook is not None:
            phase_hook(f"published:{item}:renamed")
        if item == _RESTORE_MANIFEST_FILE:
            _require_file_authority(
                destination,
                restore_manifest_identity,
                allow_relocated_path=True,
                label="published restore manifest",
            )
        published.append(item)
        checkpoint["published_items"] = published
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        require_directory_identity(restore_root_identity, label="restore root")

    _require_restore_root_inventory(
        bundle_root,
        restore_root,
        database_name,
        manifest,
    )
    _verify_published_dependencies(
        bundle_root,
        restore_root,
        database_name,
        manifest,
    )
    staging_database = _restore_staging_database_path(
        staging,
        checkpoint,
        database_name,
    )
    final_database = restore_root / database_name
    _clear_quiescent_sqlite_sidecars(staging_database)
    expected_identity = checkpoint.get("restored_database_identity")
    if checkpoint.get("database_published") is True:
        if staging_database.exists() or not final_database.exists():
            raise RuntimeError("published restore database state differs")
        _bind_published_database_identity(
            final_database,
            expected_identity,
            database_published,
            label="published restore database",
        )
    elif final_database.exists():
        if staging_database.exists():
            raise RuntimeError("restore database exists in staging and target")
        _bind_published_database_identity(
            final_database,
            expected_identity,
            database_published,
            label="recovered published restore database",
        )
        checkpoint["database_published"] = True
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
    else:
        _replace_database_and_bind_identity(
            staging_database,
            final_database,
            expected_identity,
            database_published,
        )
        if phase_hook is not None:
            phase_hook("published:database:renamed")
        checkpoint["database_published"] = True
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
    staging_database_parent = staging_database.parent
    if staging_database_parent != staging and (
        staging_database_parent.exists() or staging_database_parent.is_symlink()
    ):
        if (
            staging_database_parent.is_symlink()
            or not staging_database_parent.is_dir()
        ):
            raise RuntimeError("restore staging database directory is unsafe")
        if any(staging_database_parent.iterdir()):
            raise RuntimeError("restore staging database directory is not empty")
        _rmdir_and_fsync(staging_database_parent)
    require_directory_identity(restore_root_identity, label="restore root")


def restore_database_bundle(
    bundle_directory: str | Path,
    restore_directory: str | Path,
    *,
    database_name: str = "dota2.db",
    resume: bool = False,
    _phase_hook: Callable[[str], None] | None = None,
) -> RestoreResult:
    bundle_root = Path(bundle_directory).resolve()
    restore_root = Path(restore_directory).resolve()
    if Path(database_name).name != database_name or not database_name:
        raise ValueError("restore database name must be one file name")
    try:
        restore_root.relative_to(bundle_root)
    except ValueError:
        pass
    else:
        raise ValueError("restore destination must be outside the bundle")
    bundle_operation_lock = _operation_lock_path(bundle_root)
    restore_operation_lock = _operation_lock_path(restore_root)
    with ExitStack() as operation:
        operation.enter_context(SingleInstanceLock(bundle_operation_lock))
        operation.enter_context(SingleInstanceLock(restore_operation_lock))
        if restore_root.exists():
            if restore_root.is_symlink() or not restore_root.is_dir():
                raise RuntimeError(f"restore path is unsafe: {restore_root}")
        else:
            restore_root.mkdir(parents=True)
            fsync_directory(restore_root.parent)
        restore_root_identity = capture_directory_identity(
            restore_root,
            label="restore root",
        )
        staging = _restore_staging_directory(restore_root)
        if staging.is_dir() and (staging / _STAGING_MANIFEST_FILE).is_file():
            checkpoint = _read_restore_staging_manifest(staging)
            if (
                checkpoint.get("target") != str(restore_root)
                or checkpoint.get("database_name") != database_name
            ):
                raise RuntimeError("restore staging checkpoint binding mismatch")
        require_directory_identity(restore_root_identity, label="restore root")
        final_database = restore_root / database_name
        immutable_locks = (
            bundle_operation_lock,
            restore_operation_lock,
            *database_authority_lock_paths(final_database),
        )
        completed_identity: DatabaseFileIdentity | None = None

        def final_replacement_identity() -> DatabaseFileIdentity | None:
            return completed_identity

        def bind_final_identity(identity: DatabaseFileIdentity) -> None:
            nonlocal completed_identity
            if completed_identity is not None and completed_identity != identity:
                raise RuntimeError("published restore database identity changed")
            completed_identity = identity

        with (
            database_offline_authority(
                final_database,
                allow_missing=True,
                allow_replacement=True,
                replacement_identity_getter=final_replacement_identity,
                expected_root_identity=restore_root_identity,
                lock_factory=SingleInstanceLock,
            ),
            file_hash_authority_scope(
                required_locks=immutable_locks
            ),
        ):
            result = _restore_database_bundle_locked(
                bundle_root,
                restore_root,
                database_name=database_name,
                resume=resume,
                _phase_hook=_phase_hook,
                _database_published=bind_final_identity,
                _restore_root_identity=restore_root_identity,
                _immutable_locks=immutable_locks,
            )
            _require_completed_restore_database_binding(
                result,
                restore_root,
                database_name,
            )
            manifest = verify_database_bundle(bundle_root)
            bundle_manifest_identity = _unique_file_authority(
                bundle_root / _MANIFEST_FILE
            )
            bundle_database_identity = _unique_database_authority(
                _controlled_path(bundle_root, str(manifest["database"]["path"])),
                label="bundle database",
            )
            if bundle_manifest_identity["sha256"] != _canonical_json_file_sha256(
                manifest
            ):
                raise RuntimeError("bundle manifest changed after final verification")
            if (
                bundle_database_identity["bytes"]
                != int(manifest["database"]["bytes"])
                or bundle_database_identity["sha256"]
                != str(manifest["database"]["sha256"])
            ):
                raise RuntimeError("bundle database changed after final verification")
            restored_database_identity = _unique_database_authority(
                result.database,
                label="final restored database",
            )
            restore_layout = _restore_layout(
                bundle_root,
                restore_root,
                restore_root,
                manifest,
                database_name,
            )
            expected_completed_manifest = _restore_manifest_payload(
                bundle_manifest_hash=str(bundle_manifest_identity["sha256"]),
                bundle_manifest=manifest,
                restored_hash=result.restored_database_sha256,
                restore_root=restore_root,
                database_name=database_name,
                relocation_ids=_restore_relocation_ids(
                    result.database,
                    restore_layout[4],
                    immutable_locks=immutable_locks,
                ),
                vision_relocation_ids=_restore_vision_relocation_ids(
                    result.database,
                    restore_layout[6],
                    immutable_locks=immutable_locks,
                ),
                bundle_manifest_identity=bundle_manifest_identity,
                bundle_database_identity=bundle_database_identity,
                restored_database_identity=restored_database_identity,
            )
            (
                reviewed_identity,
                completed_manifest_identity,
            ) = _require_completed_restore_authority(
                result,
                restore_root,
                database_name,
                expected_completed_manifest,
            )
            bind_final_identity(reviewed_identity)
            if _phase_hook is not None:
                _phase_hook("completed:authority-reviewed")
            _require_file_authority(
                restore_root / _RESTORE_MANIFEST_FILE,
                completed_manifest_identity,
                label="completed restore manifest",
            )
            require_unique_database_file(
                result.database,
                expected_identity=completed_identity,
            )
            return result


def _restore_database_bundle_locked(
    bundle_directory: str | Path,
    restore_directory: str | Path,
    *,
    database_name: str = "dota2.db",
    resume: bool = False,
    _phase_hook: Callable[[str], None] | None = None,
    _database_published: Callable[[DatabaseFileIdentity], None],
    _restore_root_identity: DirectoryIdentity,
    _immutable_locks: Iterable[Path],
) -> RestoreResult:
    """Restore through one target-bound, resumable staging checkpoint."""

    bundle_root = Path(bundle_directory).resolve()
    restore_root = Path(restore_directory).resolve()
    if Path(database_name).name != database_name or not database_name:
        raise ValueError("restore database name must be one file name")
    if not restore_root.is_dir() or restore_root.is_symlink():
        raise RuntimeError(f"restore destination is unsafe: {restore_root}")
    require_directory_identity(_restore_root_identity, label="restore root")
    try:
        restore_root.relative_to(bundle_root)
    except ValueError:
        pass
    else:
        raise ValueError("restore destination must be outside the bundle")
    manifest = verify_database_bundle(bundle_root)
    bundle_manifest_identity = _unique_file_authority(bundle_root / _MANIFEST_FILE)
    bundle_manifest_hash = str(bundle_manifest_identity["sha256"])
    if bundle_manifest_hash != _canonical_json_file_sha256(manifest):
        raise RuntimeError("bundle manifest changed after verification")
    bundle_database = _controlled_path(
        bundle_root,
        str(manifest["database"]["path"]),
    )
    bundle_database_identity = _unique_database_authority(
        bundle_database,
        label="bundle database",
    )
    if (
        bundle_database_identity["bytes"] != int(manifest["database"]["bytes"])
        or bundle_database_identity["sha256"]
        != str(manifest["database"]["sha256"])
    ):
        raise RuntimeError("bundle database changed after verification")
    staging = _restore_staging_directory(restore_root)
    binding = {
        "bundle_root": str(bundle_root),
        "bundle_manifest_sha256": bundle_manifest_hash,
        "bundle_manifest_identity": bundle_manifest_identity,
        "bundle_database_identity": bundle_database_identity,
        "target": str(restore_root),
        "database_name": database_name,
    }
    _require_restore_root_inventory(
        bundle_root,
        restore_root,
        database_name,
        manifest,
    )
    final_database = restore_root / database_name
    completed_manifest_path = restore_root / _RESTORE_MANIFEST_FILE
    if (
        staging.is_dir()
        and not any(staging.iterdir())
        and final_database.is_file()
        and completed_manifest_path.is_file()
    ):
        if not resume:
            raise FileExistsError(f"restore destination already exists: {restore_root}")
        _rmdir_and_fsync(staging)
    if not staging.exists() and (
        final_database.exists() or completed_manifest_path.exists()
    ):
        if not final_database.exists() or not completed_manifest_path.exists():
            raise RuntimeError("published restore is partial and has no checkpoint")
        if not resume:
            raise FileExistsError(f"restore destination already exists: {restore_root}")
        completed, completed_manifest_identity = _read_json_with_authority(
            completed_manifest_path,
            label="completed restore manifest",
        )
        if not isinstance(completed, Mapping):
            raise RuntimeError("completed restore manifest is invalid")
        checkpoint = {
            "format": _RESTORE_STAGING_FORMAT,
            "status": "publishing",
            **binding,
            "raw_source_relocation_ids": completed.get("raw_source_relocation_ids"),
            "vision_frame_relocation_ids": completed.get(
                "vision_frame_relocation_ids"
            ),
            "restored_database_sha256": completed.get("restored_database_sha256"),
            "restored_database_identity": completed.get(
                "restored_database_identity"
            ),
            "restore_manifest_identity": completed_manifest_identity,
        }
        _clear_quiescent_sqlite_sidecars(final_database)
        result = _finish_published_restore(
            bundle_root,
            restore_root,
            database_name,
            manifest,
            checkpoint,
            binding,
            database_published=_database_published,
            restore_root_identity=_restore_root_identity,
            immutable_locks=_immutable_locks,
        )
        _clear_quiescent_sqlite_sidecars(final_database)
        return result

    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError(f"restore staging path is unsafe: {staging}")
        if not resume:
            raise FileExistsError(
                "restore staging checkpoint already exists; pass resume=True to continue"
            )
        checkpoint = _read_restore_staging_manifest(staging)
        if not _restore_binding_matches(checkpoint, binding):
            raise RuntimeError("restore staging checkpoint binding mismatch")
    else:
        if resume:
            raise FileNotFoundError("restore staging checkpoint does not exist")
        _require_space(
            staging.parent,
            int(manifest["database"]["bytes"])
            + int(manifest["artifact_bytes"])
            + _SPACE_MARGIN_BYTES,
            "restore backup bundle",
        )
        staging.mkdir()
        checkpoint = {
            "format": _RESTORE_STAGING_FORMAT,
            "status": "copying",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "staging_database_path": (
                Path(_DATABASE_DIRECTORY) / database_name
            ).as_posix(),
            "published_items": [],
            "database_published": False,
            **binding,
        }
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)

    staging_database = _restore_staging_database_path(
        staging,
        checkpoint,
        database_name,
    )
    (
        restored_database,
        odds_root,
        source_root,
        vision_root,
        replacements,
        relocation_inputs,
        vision_replacements,
        vision_relocation_inputs,
        copies,
    ) = _restore_layout(
        bundle_root,
        restore_root,
        staging,
        manifest,
        database_name,
        staging_database=staging_database,
    )
    status = checkpoint.get("status")
    if status == "copying":
        missing_bytes = sum(
            (
                int(manifest["database"]["bytes"])
                if record is None
                else int(record["compressed_bytes"])
            )
            for _, destination, record in copies
            if not destination.exists()
        )
        _require_space(
            staging.parent,
            missing_bytes + _SPACE_MARGIN_BYTES,
            "resume restore copies",
        )
        for source, destination, record in copies:
            if destination.exists():
                _verify_restore_copy(source, destination, record, manifest["database"])
                continue
            _copy_artifact(source, destination)
            _verify_restore_copy(source, destination, record, manifest["database"])
        copied_database_identity = _unique_database_authority(
            restored_database,
            label="copied restore database",
        )
        checkpoint["copied_database_identity"] = copied_database_identity
        checkpoint["status"] = "preparing"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "preparing"

    if status == "preparing":
        for source, destination, record in copies:
            _verify_restore_copy(
                source,
                destination,
                record,
                manifest["database"],
            )
        _require_database_file_authority(
            restored_database,
            checkpoint.get("copied_database_identity"),
            label="copied restore database",
        )
        _clear_quiescent_sqlite_sidecars(restored_database)
        _require_database_file_authority(
            restored_database,
            checkpoint.get("copied_database_identity"),
            label="prepared restore database",
        )
        verify_prepared_database(
            restored_database,
            odds_raw_root=odds_root,
            verify_vision_frames=False,
            immutable_locks=_immutable_locks,
        )
        checkpoint["status"] = "relocating"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "relocating"

    if status in {"relocating", "verifying", "ready"}:
        for source, destination, record in copies:
            _verify_restore_copy(
                source,
                destination,
                record,
                manifest["database"],
                require_database_hash=False,
            )

    if status == "relocating":
        if replacements:
            with immutable_checkpoint_reader(
                restored_database,
                label="restore source artifact registry",
                required_locks=_immutable_locks,
            ) as connection:
                current_paths = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        """SELECT artifact_id, storage_path
                             FROM raw_source_artifacts
                            WHERE artifact_id IN ({})""".format(
                            ",".join("?" for _ in replacements)
                        ),
                        tuple(replacements),
                    )
                }
            if set(current_paths) != set(replacements):
                raise RuntimeError("restore source artifact registry is incomplete")
            relocated = [
                artifact_id
                for artifact_id, expected in replacements.items()
                if current_paths[artifact_id] == expected
            ]
            if relocated and len(relocated) != len(replacements):
                raise RuntimeError("restore source artifact relocation is partial")
            if not relocated:
                _relocate_raw_source_paths(
                    restored_database,
                    replacements,
                    relocation_inputs,
                    final_root=restore_root / "raw-sources",
                    staging_root=source_root,
                )
        if vision_replacements:
            with immutable_checkpoint_reader(
                restored_database,
                label="restore vision frame registry",
                required_locks=_immutable_locks,
            ) as connection:
                current_vision_paths = {
                    str(row[0]): str(row[1])
                    for row in connection.execute(
                        """SELECT frame_ref, storage_path
                             FROM active_vision_frame_artifacts
                            WHERE frame_ref IN ({})""".format(
                            ",".join("?" for _ in vision_replacements)
                        ),
                        tuple(vision_replacements),
                    )
                }
            if set(current_vision_paths) != set(vision_replacements):
                raise RuntimeError("restore vision frame registry is incomplete")
            relocated_vision = [
                frame_ref
                for frame_ref, expected in vision_replacements.items()
                if current_vision_paths[frame_ref] == expected
            ]
            if relocated_vision and len(relocated_vision) != len(vision_replacements):
                raise RuntimeError("restore vision frame relocation is partial")
            if not relocated_vision:
                _relocate_vision_frame_paths(
                    restored_database,
                    vision_replacements,
                    vision_relocation_inputs,
                    final_root=restore_root / "live_betting" / "live_evidence",
                    staging_root=vision_root,
                )
        _clear_quiescent_sqlite_sidecars(restored_database)
        relocation_ids = _restore_relocation_ids(
            restored_database,
            replacements,
            immutable_locks=_immutable_locks,
        )
        vision_relocation_ids = _restore_vision_relocation_ids(
            restored_database,
            vision_replacements,
            immutable_locks=_immutable_locks,
        )
        checkpoint["raw_source_relocation_ids"] = list(relocation_ids)
        checkpoint["vision_frame_relocation_ids"] = list(vision_relocation_ids)
        checkpoint["status"] = "verifying"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "verifying"

    if status == "verifying":
        relocation_ids = _restore_relocation_ids(
            restored_database,
            replacements,
            immutable_locks=_immutable_locks,
        )
        vision_relocation_ids = _restore_vision_relocation_ids(
            restored_database,
            vision_replacements,
            immutable_locks=_immutable_locks,
        )
        if list(relocation_ids) != checkpoint.get("raw_source_relocation_ids", []):
            raise RuntimeError("restore relocation audit differs from checkpoint")
        if list(vision_relocation_ids) != checkpoint.get(
            "vision_frame_relocation_ids", []
        ):
            raise RuntimeError(
                "restore vision relocation audit differs from checkpoint"
            )
        restored_hash = _verify_restored_database(
            restored_database,
            odds_root,
            replacements,
            vision_replacements,
            verify_vision_files=False,
            immutable_locks=_immutable_locks,
        )
        restored_database_identity = _unique_database_authority(
            restored_database,
            label="restored database",
        )
        restore_manifest = _restore_manifest_payload(
            bundle_manifest_hash=bundle_manifest_hash,
            bundle_manifest=manifest,
            restored_hash=restored_hash,
            restore_root=restore_root,
            database_name=database_name,
            relocation_ids=relocation_ids,
            vision_relocation_ids=vision_relocation_ids,
            bundle_manifest_identity=bundle_manifest_identity,
            bundle_database_identity=bundle_database_identity,
            restored_database_identity=restored_database_identity,
        )
        _write_json(staging / _RESTORE_MANIFEST_FILE, restore_manifest)
        restore_manifest_identity = _unique_file_authority(
            staging / _RESTORE_MANIFEST_FILE
        )
        checkpoint["restored_database_sha256"] = restored_hash
        checkpoint["restored_database_identity"] = restored_database_identity
        checkpoint["restore_manifest_identity"] = restore_manifest_identity
        checkpoint["status"] = "ready"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "ready"

    if status == "ready":
        relocation_ids = _restore_relocation_ids(
            restored_database,
            replacements,
            immutable_locks=_immutable_locks,
        )
        vision_relocation_ids = _restore_vision_relocation_ids(
            restored_database,
            vision_replacements,
            immutable_locks=_immutable_locks,
        )
        if list(relocation_ids) != checkpoint.get("raw_source_relocation_ids", []):
            raise RuntimeError("ready restore relocation audit differs from checkpoint")
        if list(vision_relocation_ids) != checkpoint.get(
            "vision_frame_relocation_ids", []
        ):
            raise RuntimeError("ready vision relocation audit differs from checkpoint")
        restored_hash = _verify_restored_database(
            restored_database,
            odds_root,
            replacements,
            vision_replacements,
            verify_vision_files=False,
            immutable_locks=_immutable_locks,
        )
        if restored_hash != checkpoint.get("restored_database_sha256"):
            raise RuntimeError("ready restore database differs from checkpoint")
        restored_identity = _require_database_file_authority(
            restored_database,
            checkpoint.get("restored_database_identity"),
            label="ready restore database",
        )
        restore_manifest_path = staging / _RESTORE_MANIFEST_FILE
        restore_manifest, restore_manifest_identity = _read_json_with_authority(
            restore_manifest_path,
            label="ready restore manifest",
        )
        expected_restore_manifest = _restore_manifest_payload(
            bundle_manifest_hash=bundle_manifest_hash,
            bundle_manifest=manifest,
            restored_hash=restored_hash,
            restore_root=restore_root,
            database_name=database_name,
            relocation_ids=relocation_ids,
            vision_relocation_ids=vision_relocation_ids,
            bundle_manifest_identity=bundle_manifest_identity,
            bundle_database_identity=bundle_database_identity,
            restored_database_identity=restored_identity,
        )
        if restore_manifest != expected_restore_manifest:
            raise RuntimeError(
                "ready restore manifest differs from checkpoint authority"
            )
        if restore_manifest_identity["sha256"] != _canonical_json_file_sha256(
            expected_restore_manifest
        ):
            raise RuntimeError("ready restore manifest is not canonical")
        if not isinstance(checkpoint.get("restore_manifest_identity"), Mapping):
            checkpoint["restore_manifest_identity"] = restore_manifest_identity
            _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        _require_file_authority(
            restore_manifest_path,
            checkpoint.get("restore_manifest_identity"),
            label="ready restore manifest",
        )
        _require_file_authority(
            bundle_root / _MANIFEST_FILE,
            checkpoint.get("bundle_manifest_identity"),
            label="bundle manifest",
        )
        _require_database_file_authority(
            bundle_database,
            checkpoint.get("bundle_database_identity"),
            label="bundle database",
        )
        checkpoint["status"] = "publishing"
        checkpoint.setdefault("published_items", [])
        checkpoint.setdefault("database_published", False)
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "publishing"

    if status not in {"publishing", "published_ready"}:
        raise RuntimeError(f"restore staging status is invalid: {status}")
    _require_file_authority(
        bundle_root / _MANIFEST_FILE,
        checkpoint.get("bundle_manifest_identity"),
        label="bundle manifest",
    )
    _require_database_file_authority(
        bundle_database,
        checkpoint.get("bundle_database_identity"),
        label="bundle database",
    )
    if status == "publishing":
        _publish_restore_items(
            bundle_root,
            restore_root,
            staging,
            database_name,
            manifest,
            checkpoint,
            phase_hook=_phase_hook,
            database_published=_database_published,
            restore_root_identity=_restore_root_identity,
        )
    verification_checkpoint = dict(checkpoint)
    verification_checkpoint["status"] = "publishing"
    result = _finish_published_restore(
        bundle_root,
        restore_root,
        database_name,
        manifest,
        verification_checkpoint,
        binding,
        database_published=_database_published,
        restore_root_identity=_restore_root_identity,
        immutable_locks=_immutable_locks,
    )
    _clear_quiescent_sqlite_sidecars(restore_root / database_name)
    if status == "publishing":
        checkpoint["status"] = "published_ready"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        if _phase_hook is not None:
            _phase_hook("published:ready")
    leftovers = sorted(
        path.name
        for path in staging.iterdir()
        if path.name != _STAGING_MANIFEST_FILE
    )
    if leftovers:
        raise RuntimeError(
            "restore staging contains unknown files after publication: "
            + ",".join(leftovers)
        )
    _unlink_json_with_authority(
        staging / _STAGING_MANIFEST_FILE,
        checkpoint,
        label="restore staging manifest",
    )
    if _phase_hook is not None:
        _phase_hook("published:checkpoint-cleared")
    _rmdir_and_fsync(staging)
    return result


def bundle_result_json(result: BundleResult | RestoreResult) -> str:
    payload = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in asdict(result).items()
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = [
    "BundleResult",
    "RestoreResult",
    "bundle_result_json",
    "create_database_bundle",
    "restore_database_bundle",
    "verify_database_bundle",
]
