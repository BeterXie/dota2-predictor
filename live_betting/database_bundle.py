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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_intelligence.raw_archive import schema_fingerprint
from event_intelligence.raw_registry import (
    raw_source_relocation_id,
    relocate_raw_source_artifacts,
)
from shared.sqlite import connect

from .database_protocol import online_backup, verify_prepared_database
from .vision_frame_registry import (
    relocate_vision_frame_artifacts,
    verify_vision_frame_registry,
    vision_frame_relocation_id,
)


_BUNDLE_FORMAT = "dota2-database-bundle-v1"
_DATABASE_FILE = "database.sqlite"
_MANIFEST_FILE = "manifest.json"
_RESTORE_MANIFEST_FILE = "restore-manifest.json"
_STAGING_MANIFEST_FILE = "staging-manifest.json"
_STAGING_FORMAT = "dota2-database-bundle-staging-v1"
_RESTORE_STAGING_FORMAT = "dota2-database-bundle-restore-staging-v1"
_SPACE_MARGIN_BYTES = 1024 * 1024


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


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _canonical_json(value) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_manifest(bundle_root: Path) -> dict[str, Any]:
    path = bundle_root / _MANIFEST_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("backup bundle manifest is missing or invalid") from error
    if not isinstance(value, dict) or value.get("format") != _BUNDLE_FORMAT:
        raise RuntimeError("backup bundle manifest has the wrong format")
    return value


def _read_staging_manifest(staging: Path) -> dict[str, Any]:
    path = staging / _STAGING_MANIFEST_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("backup bundle staging manifest is missing or invalid") from error
    if not isinstance(value, dict) or value.get("format") != _STAGING_FORMAT:
        raise RuntimeError("backup bundle staging manifest has the wrong format")
    return value


def _read_restore_staging_manifest(staging: Path) -> dict[str, Any]:
    path = staging / _STAGING_MANIFEST_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("restore staging manifest is missing or invalid") from error
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
    for table in ("live_schema_version", "intelligence_schema_version"):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is not None:
            value = connection.execute(f"SELECT MAX(version) FROM {table}").fetchone()[0]
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
            Path("vision")
            / "sha256"
            / content_hash[:2]
            / f"{content_hash}.jpg"
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
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        metadata = None
    if (
        path.is_symlink()
        or not path.is_file()
        or metadata is None
        or int(metadata.st_nlink) != 1
    ):
        raise RuntimeError(f"bundle raw artifact is missing or unsafe: {path}")
    compressed = path.read_bytes()
    if len(compressed) != int(record["compressed_bytes"]):
        raise RuntimeError(f"bundle raw artifact compressed size mismatch: {path}")
    if _sha256_file(path) != str(record["file_sha256"]):
        raise RuntimeError(f"bundle raw artifact file hash mismatch: {path}")
    if record["registry"] == "vision_frame_artifacts":
        if (
            len(compressed) != int(record["uncompressed_bytes"])
            or hashlib.sha256(compressed).hexdigest()
            != str(record["content_sha256"])
            or str(record["schema_fingerprint"]) != "image/jpeg"
            or path.name.casefold()
            != f"{record['content_sha256']}.jpg"
        ):
            raise RuntimeError(f"bundle vision frame metadata mismatch: {path}")
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
        path = stored.resolve() if stored.is_absolute() else (database.parent / stored).resolve()
        if not _inside_any(path, allowed_roots):
            raise RuntimeError(f"source raw artifact escapes allowed roots: {path}")
    return path


def _required_bundle_bytes(database: Path) -> int:
    connection = connect(database, read_only=True)
    try:
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        artifact_bytes = sum(
            int(record["compressed_bytes"])
            for record in _database_artifact_rows(connection)
        )
    finally:
        connection.close()
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
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def create_database_bundle(
    database: str | Path,
    odds_raw_root: str | Path,
    bundle_directory: str | Path,
    *,
    allowed_source_roots: Iterable[str | Path] = (),
    git_commit: str | None = None,
    resume: bool = False,
) -> BundleResult:
    """Snapshot a database and copy only artifacts registered by that snapshot."""

    database = Path(database).resolve()
    odds_raw_root = Path(odds_raw_root).resolve()
    bundle_directory = Path(bundle_directory).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"database does not exist: {database}")
    if not odds_raw_root.is_dir():
        raise FileNotFoundError(f"odds raw root does not exist: {odds_raw_root}")
    if bundle_directory.exists():
        raise FileExistsError(f"bundle destination already exists: {bundle_directory}")
    try:
        database.relative_to(bundle_directory)
    except ValueError:
        pass
    else:
        raise ValueError("database must be outside the bundle destination")

    bundle_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_directory(bundle_directory)
    roots = tuple(
        dict.fromkeys(
            [
                database.parent,
                odds_raw_root,
                *(Path(root).resolve() for root in allowed_source_roots),
            ]
        )
    )
    effective_git_commit = git_commit or _git_commit()
    binding = {
        "target": str(bundle_directory),
        "source_database": str(database),
        "odds_raw_root": str(odds_raw_root),
        "allowed_source_roots": sorted(str(root) for root in roots),
        "git_commit": effective_git_commit,
    }

    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError(f"backup bundle staging path is unsafe: {staging}")
        if not resume:
            raise FileExistsError(
                "backup bundle staging checkpoint already exists; pass resume=True "
                "to continue"
            )
        checkpoint_path = staging / _STAGING_MANIFEST_FILE
        if not checkpoint_path.exists():
            completed = _read_manifest(staging)
            if completed.get("publication_target") != str(bundle_directory):
                raise RuntimeError("completed backup staging belongs to another target")
            verify_database_bundle(staging)
            total_bytes = sum(
                path.stat().st_size for path in staging.rglob("*") if path.is_file()
            )
            staging.rename(bundle_directory)
            return BundleResult(
                bundle_directory,
                str(completed["database"]["sha256"]),
                int(completed["artifact_count"]),
                total_bytes,
            )
        checkpoint = _read_staging_manifest(staging)
        recorded_binding = {
            key: checkpoint.get(key) for key in binding
        }
        if recorded_binding != binding:
            raise RuntimeError("backup bundle staging checkpoint binding mismatch")
    else:
        if resume:
            raise FileNotFoundError("backup bundle staging checkpoint does not exist")
        _require_space(
            bundle_directory.parent,
            _required_bundle_bytes(database),
            "create backup bundle",
        )
        staging.mkdir()
        checkpoint = {
            "format": _STAGING_FORMAT,
            "status": "snapshot_pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            **binding,
        }
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)

    snapshot = staging / _DATABASE_FILE
    status = checkpoint.get("status")
    if status == "snapshot_pending":
        if snapshot.exists():
            if snapshot.is_dir() or snapshot.is_symlink():
                raise RuntimeError("backup bundle staging database is unsafe")
            snapshot.unlink()
        online_backup(database, snapshot)
        verify_prepared_database(snapshot, odds_raw_root=odds_raw_root)
        connection = connect(snapshot, read_only=True)
        try:
            schema = _schema_manifest(connection)
        finally:
            connection.close()
        checkpoint.update(
            {
                "status": "copying",
                "database_bytes": snapshot.stat().st_size,
                "database_sha256": _sha256_file(snapshot),
                "schema": schema,
            }
        )
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "copying"

    if status == "copying":
        if (
            snapshot.is_symlink()
            or not snapshot.is_file()
            or snapshot.stat().st_size != int(checkpoint.get("database_bytes", -1))
            or _sha256_file(snapshot) != checkpoint.get("database_sha256")
        ):
            raise RuntimeError("backup bundle staging database differs from checkpoint")
        verify_prepared_database(snapshot, odds_raw_root=odds_raw_root)
        connection = connect(snapshot, read_only=True)
        try:
            schema = _schema_manifest(connection)
            records = _database_artifact_rows(connection)
        finally:
            connection.close()
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
            source_record["file_sha256"] = _sha256_file(source) if source.is_file() else ""
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
            "git_commit": effective_git_commit,
            "publication_target": str(bundle_directory),
            "database": {
                "path": _DATABASE_FILE,
                "bytes": snapshot.stat().st_size,
                "sha256": _sha256_file(snapshot),
            },
            "schema": schema,
            "artifacts": records,
            "artifact_count": len(records),
            "artifact_bytes": artifact_bytes,
        }
        _write_json(staging / _MANIFEST_FILE, manifest)
        verify_database_bundle(staging)
        checkpoint["status"] = "ready"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "ready"

    if status != "ready":
        raise RuntimeError(f"backup bundle staging status is invalid: {status}")
    manifest = verify_database_bundle(staging)
    if manifest.get("publication_target") != str(bundle_directory):
        raise RuntimeError("ready backup staging belongs to another target")
    staging.rename(bundle_directory)
    (bundle_directory / _STAGING_MANIFEST_FILE).unlink()
    total_bytes = sum(
        path.stat().st_size
        for path in bundle_directory.rglob("*")
        if path.is_file()
    )
    return BundleResult(
        bundle_directory,
        str(manifest["database"]["sha256"]),
        int(manifest["artifact_count"]),
        total_bytes,
    )


def verify_database_bundle(bundle_directory: str | Path) -> dict[str, Any]:
    """Verify the database, manifest, and every registered artifact."""

    bundle_root = Path(bundle_directory).resolve()
    if not bundle_root.is_dir():
        raise FileNotFoundError(f"backup bundle does not exist: {bundle_root}")
    manifest = _read_manifest(bundle_root)
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(manifest.get("git_commit", ""))):
        raise RuntimeError("backup bundle git commit is invalid")
    database_meta = manifest.get("database")
    if not isinstance(database_meta, dict):
        raise RuntimeError("backup bundle database manifest is invalid")
    database = _controlled_path(bundle_root, str(database_meta.get("path", "")))
    if database.is_symlink() or not database.is_file():
        raise RuntimeError("backup bundle database is missing or unsafe")
    if database.stat().st_size != int(database_meta.get("bytes", -1)):
        raise RuntimeError("backup bundle database size mismatch")
    if _sha256_file(database) != str(database_meta.get("sha256", "")):
        raise RuntimeError("backup bundle database hash mismatch")
    verify_prepared_database(
        database,
        odds_raw_root=bundle_root / "raw" / "odds",
        verify_vision_frames=False,
    )
    connection = connect(database, read_only=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("backup bundle database failed integrity_check")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("backup bundle database failed foreign_key_check")
        if _schema_manifest(connection) != manifest.get("schema"):
            raise RuntimeError("backup bundle database schema manifest mismatch")
        database_records = _database_artifact_rows(connection)
    finally:
        connection.close()

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


def _restore_layout(
    bundle_root: Path,
    restore_root: Path,
    staging: Path,
    manifest: Mapping[str, Any],
    database_name: str,
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
    restored_database = staging / database_name
    odds_root = staging / "live_betting" / "raw-v2"
    source_root = staging / "raw-sources"
    vision_root = staging / "live_betting" / "live_evidence"
    bundle_database = _controlled_path(
        bundle_root, str(manifest["database"]["path"])
    )
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
    if destination.is_symlink() or not destination.is_file():
        raise RuntimeError(f"restore staging copy is missing or unsafe: {destination}")
    if os.path.samefile(source, destination):
        raise RuntimeError("restore staging copy must be independent from bundle")
    if record is None:
        if not require_database_hash:
            return
        if (
            destination.stat().st_size != int(database_meta["bytes"])
            or _sha256_file(destination) != str(database_meta["sha256"])
        ):
            raise RuntimeError("restore staging database differs from bundle")
        return
    _verify_artifact(destination, record)


def _restore_relocation_ids(
    database: Path,
    replacements: Mapping[str, str],
) -> tuple[str, ...]:
    if not replacements:
        return ()
    connection = connect(database, read_only=True)
    try:
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
                raise RuntimeError("restored source artifact relocation audit is missing")
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
        return tuple(ids)
    finally:
        connection.close()


def _restore_vision_relocation_ids(
    database: Path,
    replacements: Mapping[str, str],
) -> tuple[str, ...]:
    if not replacements:
        return ()
    connection = connect(database, read_only=True)
    try:
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
        return tuple(ids)
    finally:
        connection.close()


def _verify_restored_database(
    database: Path,
    odds_root: Path,
    replacements: Mapping[str, str],
    vision_replacements: Mapping[str, str],
    *,
    verify_vision_files: bool,
) -> str:
    verify_prepared_database(
        database,
        odds_raw_root=odds_root,
        verify_vision_frames=verify_vision_files,
    )
    connection = connect(database, read_only=True)
    try:
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
    finally:
        connection.close()
    return _sha256_file(database)


def restore_database_bundle(
    bundle_directory: str | Path,
    restore_directory: str | Path,
    *,
    database_name: str = "dota2.db",
    resume: bool = False,
) -> RestoreResult:
    """Restore through one target-bound, resumable staging checkpoint."""

    bundle_root = Path(bundle_directory).resolve()
    restore_root = Path(restore_directory).resolve()
    if Path(database_name).name != database_name or not database_name:
        raise ValueError("restore database name must be one file name")
    if restore_root.exists():
        raise FileExistsError(f"restore destination already exists: {restore_root}")
    try:
        restore_root.relative_to(bundle_root)
    except ValueError:
        pass
    else:
        raise ValueError("restore destination must be outside the bundle")
    manifest = verify_database_bundle(bundle_root)
    bundle_manifest_hash = _sha256_file(bundle_root / _MANIFEST_FILE)
    staging = _restore_staging_directory(restore_root)
    staging.parent.mkdir(parents=True, exist_ok=True)
    binding = {
        "bundle_root": str(bundle_root),
        "bundle_manifest_sha256": bundle_manifest_hash,
        "target": str(restore_root),
        "database_name": database_name,
    }
    if staging.exists():
        if staging.is_symlink() or not staging.is_dir():
            raise RuntimeError(f"restore staging path is unsafe: {staging}")
        if not resume:
            raise FileExistsError(
                "restore staging checkpoint already exists; pass resume=True to continue"
            )
        checkpoint = _read_restore_staging_manifest(staging)
        if {key: checkpoint.get(key) for key in binding} != binding:
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
            **binding,
        }
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)

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
                _verify_restore_copy(
                    source, destination, record, manifest["database"]
                )
                continue
            _copy_artifact(source, destination)
            _verify_restore_copy(source, destination, record, manifest["database"])
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
            connection = connect(restored_database, read_only=True)
            try:
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
            finally:
                connection.close()
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
            connection = connect(restored_database, read_only=True)
            try:
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
            finally:
                connection.close()
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
        relocation_ids = _restore_relocation_ids(restored_database, replacements)
        vision_relocation_ids = _restore_vision_relocation_ids(
            restored_database, vision_replacements
        )
        checkpoint["raw_source_relocation_ids"] = list(relocation_ids)
        checkpoint["vision_frame_relocation_ids"] = list(vision_relocation_ids)
        checkpoint["status"] = "verifying"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "verifying"

    if status == "verifying":
        relocation_ids = _restore_relocation_ids(restored_database, replacements)
        vision_relocation_ids = _restore_vision_relocation_ids(
            restored_database, vision_replacements
        )
        if list(relocation_ids) != checkpoint.get("raw_source_relocation_ids", []):
            raise RuntimeError("restore relocation audit differs from checkpoint")
        if list(vision_relocation_ids) != checkpoint.get(
            "vision_frame_relocation_ids", []
        ):
            raise RuntimeError("restore vision relocation audit differs from checkpoint")
        restored_hash = _verify_restored_database(
            restored_database,
            odds_root,
            replacements,
            vision_replacements,
            verify_vision_files=False,
        )
        restore_manifest = {
            "format": "dota2-database-bundle-restore-v1",
            "bundle_manifest_sha256": bundle_manifest_hash,
            "bundle_database_sha256": manifest["database"]["sha256"],
            "restored_database_sha256": restored_hash,
            "restore_target": str(restore_root),
            "database": database_name,
            "artifact_count": manifest["artifact_count"],
            "raw_source_relocation_ids": list(relocation_ids),
            "vision_frame_relocation_ids": list(vision_relocation_ids),
        }
        _write_json(staging / _RESTORE_MANIFEST_FILE, restore_manifest)
        checkpoint["restored_database_sha256"] = restored_hash
        checkpoint["status"] = "ready"
        _write_json(staging / _STAGING_MANIFEST_FILE, checkpoint)
        status = "ready"

    if status != "ready":
        raise RuntimeError(f"restore staging status is invalid: {status}")
    relocation_ids = _restore_relocation_ids(restored_database, replacements)
    vision_relocation_ids = _restore_vision_relocation_ids(
        restored_database, vision_replacements
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
    )
    if restored_hash != checkpoint.get("restored_database_sha256"):
        raise RuntimeError("ready restore database differs from checkpoint")
    try:
        restore_manifest = json.loads(
            (staging / _RESTORE_MANIFEST_FILE).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("ready restore manifest is missing or invalid") from error
    expected_restore_identity = {
        "format": "dota2-database-bundle-restore-v1",
        "bundle_manifest_sha256": bundle_manifest_hash,
        "bundle_database_sha256": manifest["database"]["sha256"],
        "restored_database_sha256": restored_hash,
        "restore_target": str(restore_root),
        "database": database_name,
        "artifact_count": manifest["artifact_count"],
        "raw_source_relocation_ids": list(relocation_ids),
        "vision_frame_relocation_ids": list(vision_relocation_ids),
    }
    if restore_manifest != expected_restore_identity:
        raise RuntimeError("ready restore manifest differs from checkpoint authority")
    staging.rename(restore_root)
    (restore_root / _STAGING_MANIFEST_FILE).unlink()
    final_hash = _verify_restored_database(
        restore_root / database_name,
        restore_root / "live_betting" / "raw-v2",
        replacements,
        vision_replacements,
        verify_vision_files=True,
    )
    if final_hash != restored_hash:
        raise RuntimeError("published restore database differs from staging")
    return RestoreResult(
        restore_root,
        restore_root / database_name,
        restore_root / "live_betting" / "raw-v2",
        restore_root / "raw-sources",
        restore_root / "live_betting" / "live_evidence",
        restored_hash,
    )


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
