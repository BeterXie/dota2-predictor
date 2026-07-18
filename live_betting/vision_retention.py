"""Bounded, lineage-aware retention for local vision evidence frames."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from shared.sqlite import connect as connect_sqlite

from .vision_frame_registry import (
    retire_vision_frame_artifact,
    verify_vision_frame_registry,
)


DEFAULT_RETENTION_TTL = timedelta(days=7)
DEFAULT_MAX_UNPROTECTED_PER_MATCH = 2_000
MIN_CAPACITY_DELETION_AGE = timedelta(hours=1)
AUDIT_GAME_BUCKET_SECONDS = 10 * 60
AUDIT_WALL_BUCKET_SECONDS = 60 * 60
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg"})
_REQUIRED_TABLES = frozenset(
    {
        "vision_observations",
        "vision_frame_artifacts",
        "vision_frame_artifact_relocations",
        "vision_frame_artifact_retirements",
        "vision_observation_invalidations",
        "vision_draft_anchors",
        "vision_draft_conflicts",
        "strategy_decisions",
        "prospective_draft_curves",
        "prospective_draft_landmarks",
        "research_live_predictions",
        "shadow_orders",
        "shadow_map_attempts",
        "shadow_order_decision_lineage",
        "settlements",
    }
)


class RetentionSafetyError(RuntimeError):
    """Raised before deletion when retention lineage cannot be trusted."""


@dataclass(frozen=True)
class EvidenceFile:
    path: Path
    relative_path: Path
    match_scope: str
    size: int
    mtime_ns: int
    device: int
    inode: int


@dataclass(frozen=True)
class VisionRetentionResult:
    dry_run: bool
    scanned_files: int
    scanned_bytes: int
    protected_reference_files: int
    protected_audit_files: int
    protected_active_files: int
    retained_unprotected_files: int
    planned_deletions: tuple[Path, ...]
    planned_bytes: int
    deleted_files: int
    deleted_bytes: int
    unsafe_paths: int
    delete_errors: int
    ttl_seconds: int
    capacity_grace_seconds: int
    max_unprotected_per_match: int

    def as_dict(self) -> dict[str, object]:
        return {
            "status": (
                "error"
                if self.unsafe_paths or self.delete_errors
                else "dry_run" if self.dry_run else "ok"
            ),
            "dry_run": self.dry_run,
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "protected_reference_files": self.protected_reference_files,
            "protected_audit_files": self.protected_audit_files,
            "protected_active_files": self.protected_active_files,
            "retained_unprotected_files": self.retained_unprotected_files,
            "planned_deletions": len(self.planned_deletions),
            "planned_bytes": self.planned_bytes,
            "deleted_files": self.deleted_files,
            "deleted_bytes": self.deleted_bytes,
            "unsafe_paths": self.unsafe_paths,
            "delete_errors": self.delete_errors,
            "ttl_seconds": self.ttl_seconds,
            "capacity_grace_seconds": self.capacity_grace_seconds,
            "max_unprotected_per_match": self.max_unprotected_per_match,
        }


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(reparse_flag and attributes & reparse_flag)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def _resolve_root(root: Path) -> Path:
    if _is_link(root):
        raise RetentionSafetyError("evidence root cannot be a link or junction")
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise RetentionSafetyError("evidence root is unavailable") from error
    if not resolved.is_dir():
        raise RetentionSafetyError("evidence root must be a directory")
    return resolved


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _reference_key(
    value: object,
    root: Path,
    frame_paths: dict[str, str] | None = None,
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if frame_paths is not None and value in frame_paths:
        return frame_paths[value]
    try:
        raw = Path(value)
        candidate = raw if raw.is_absolute() else root / raw
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.suffix.casefold() not in _IMAGE_SUFFIXES or not _inside_root(
        resolved, root
    ):
        return None
    return _path_key(resolved)


def _scan_files(root: Path) -> tuple[list[EvidenceFile], int]:
    files: list[EvidenceFile] = []
    unsafe = 0
    for directory, names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        safe_names: list[str] = []
        for name in names:
            child = directory_path / name
            if _is_link(child):
                unsafe += 1
            else:
                safe_names.append(name)
        names[:] = safe_names
        for filename in filenames:
            raw = directory_path / filename
            if raw.suffix.casefold() not in _IMAGE_SUFFIXES:
                continue
            if _is_link(raw):
                unsafe += 1
                continue
            try:
                resolved = raw.resolve(strict=True)
                metadata = raw.stat(follow_symlinks=False)
                relative = resolved.relative_to(root)
            except (OSError, ValueError):
                unsafe += 1
                continue
            if not stat.S_ISREG(metadata.st_mode):
                unsafe += 1
                continue
            scope = relative.parts[0] if len(relative.parts) > 1 else "__root__"
            files.append(
                EvidenceFile(
                    path=resolved,
                    relative_path=relative,
                    match_scope=scope,
                    size=int(metadata.st_size),
                    mtime_ns=int(metadata.st_mtime_ns),
                    device=int(metadata.st_dev),
                    inode=int(metadata.st_ino),
                )
            )
    return files, unsafe


def _require_tables(connection: sqlite3.Connection) -> None:
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(_REQUIRED_TABLES - present)
    if missing:
        raise RetentionSafetyError(
            f"retention lineage schema is incomplete: {','.join(missing)}"
        )


def _registered_frame_paths(
    connection: sqlite3.Connection,
    root: Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, tuple[str, int]]]:
    try:
        verify_vision_frame_registry(connection, require_active_files=True)
    except RuntimeError as error:
        raise RetentionSafetyError("vision frame registry is unverifiable") from error
    by_ref: dict[str, str] = {}
    by_path: dict[str, str] = {}
    identities: dict[str, tuple[str, int]] = {}
    for row in connection.execute(
        """SELECT frame_ref, storage_path, content_sha256, byte_length
             FROM active_vision_frame_artifacts ORDER BY frame_ref"""
    ):
        try:
            path = Path(str(row[1])).resolve(strict=True)
        except OSError as error:
            raise RetentionSafetyError(
                "registered vision frame is unavailable"
            ) from error
        if not _inside_root(path, root):
            continue
        key = _path_key(path)
        prior = by_path.get(key)
        if prior is not None and prior != str(row[0]):
            raise RetentionSafetyError(
                "multiple vision frame identities share one storage path"
            )
        by_ref[str(row[0])] = key
        by_path[key] = str(row[0])
        identities[str(row[0])] = (str(row[2]), int(row[3]))
    return by_ref, by_path, identities


def _json_source_refs(raw: object) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except (RecursionError, TypeError, ValueError) as error:
        raise RetentionSafetyError("retention lineage JSON is invalid") from error
    refs: list[str] = []
    stack = [value]
    nodes = 0
    while stack:
        current = stack.pop()
        nodes += 1
        if nodes > 100_000:
            raise RetentionSafetyError("retention lineage JSON is too large")
        if isinstance(current, dict):
            for key, child in current.items():
                if key in {"source_frame_ref", "vision_ref"} and isinstance(
                    child, str
                ):
                    refs.append(child)
                else:
                    stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return tuple(refs)


def _add_reference_keys(
    output: set[str],
    values: Iterable[object],
    root: Path,
    frame_paths: dict[str, str],
) -> int:
    before = len(output)
    for value in values:
        key = _reference_key(value, root, frame_paths)
        if key is not None:
            output.add(key)
    return len(output) - before


def _causal_observation_refs(
    connection: sqlite3.Connection,
    *,
    match_id: str,
    map_number: int,
    cutoff: str,
    radiant_json: str | None = None,
    dire_json: str | None = None,
    window_seconds: int = 30,
) -> tuple[str, ...]:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    filters = [
        "raybet_match_id=?",
        "map_number=?",
        "julianday(captured_at)<=julianday(?)",
        "julianday(captured_at)>=julianday(?) - (? / 86400.0)",
    ]
    parameters: list[object] = [
        match_id,
        map_number,
        cutoff,
        cutoff,
        window_seconds,
    ]
    if radiant_json is not None and dire_json is not None:
        filters.extend(
            [
                "confirmed=1",
                "screen_state='game'",
                "radiant_hero_ids=?",
                "dire_hero_ids=?",
            ]
        )
        parameters.extend([radiant_json, dire_json])
    rows = connection.execute(
        f"""SELECT captured_at, source_frame_ref
               FROM vision_observations
              WHERE {' AND '.join(filters)}
              ORDER BY julianday(captured_at) DESC, captured_at DESC""",
        parameters,
    ).fetchall()
    if rows:
        return tuple(str(row["source_frame_ref"]) for row in rows)
    return tuple(
        str(row[0])
        for row in connection.execute(
            """SELECT source_frame_ref FROM vision_observations
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        )
    )


def _referenced_keys(
    connection: sqlite3.Connection,
    root: Path,
    frame_paths: dict[str, str],
    frame_identities: dict[str, tuple[str, int]],
) -> set[str]:
    protected: set[str] = set()
    direct_rows = connection.execute(
        """SELECT source_frame_ref FROM vision_observation_invalidations
           UNION SELECT source_frame_ref FROM vision_draft_conflicts
           UNION SELECT source_frame_ref FROM vision_draft_anchors
           UNION SELECT team_side_source_frame_ref FROM vision_draft_anchors
                 WHERE team_side_source_frame_ref IS NOT NULL
           UNION SELECT evidence_ref FROM settlements"""
    ).fetchall()
    _add_reference_keys(
        protected, (row[0] for row in direct_rows), root, frame_paths
    )
    for table in ("strategy_decisions", "shadow_orders"):
        for row in connection.execute(
            f"""SELECT vision_source_frame_ref,
                       vision_source_frame_sha256,
                       vision_source_frame_bytes
                  FROM {table}
                 WHERE vision_source_frame_ref IS NOT NULL"""
        ):
            frame_ref = str(row[0])
            identity = frame_identities.get(frame_ref)
            if (
                identity is not None
                and row[1] is not None
                and row[2] is not None
                and identity == (str(row[1]), int(row[2]))
            ):
                protected.add(frame_paths[frame_ref])
    curve_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(prospective_draft_curves)")
    }
    if "anchor_source_frame_ref" in curve_columns:
        _add_reference_keys(
            protected,
            (
                row[0]
                for row in connection.execute(
                    """SELECT anchor_source_frame_ref
                         FROM prospective_draft_curves
                        WHERE anchor_source_frame_ref IS NOT NULL"""
                )
            ),
            root,
            frame_paths,
        )

    for row in connection.execute(
        """SELECT decision_key, raybet_match_id, map_number, decided_at,
                  contributions_json
             FROM strategy_decisions"""
    ):
        refs = _json_source_refs(row["contributions_json"])
        added = _add_reference_keys(protected, refs, root, frame_paths)
        if added == 0:
            fallback = _causal_observation_refs(
                connection,
                match_id=str(row["raybet_match_id"]),
                map_number=int(row["map_number"]),
                cutoff=str(row["decided_at"]),
            )
            _add_reference_keys(protected, fallback, root, frame_paths)

    for row in connection.execute(
        """SELECT prediction.raybet_match_id, prediction.map_number,
                  prediction.observed_at, prediction.radiant_hero_ids_json,
                  prediction.dire_hero_ids_json
             FROM research_live_predictions AS prediction"""
    ):
        refs = _causal_observation_refs(
            connection,
            match_id=str(row["raybet_match_id"]),
            map_number=int(row["map_number"]),
            cutoff=str(row["observed_at"]),
            radiant_json=str(row["radiant_hero_ids_json"]),
            dire_json=str(row["dire_hero_ids_json"]),
        )
        _add_reference_keys(protected, refs, root, frame_paths)

    for row in connection.execute(
        """SELECT curve.raybet_match_id, curve.map_number,
                  curve.first_usable_at, landmark.input_refs_json
             FROM prospective_draft_landmarks AS landmark
             JOIN prospective_draft_curves AS curve
               ON curve.curve_key=landmark.curve_key"""
    ):
        refs = _json_source_refs(row["input_refs_json"])
        added = _add_reference_keys(protected, refs, root, frame_paths)
        if added == 0:
            fallback = _causal_observation_refs(
                connection,
                match_id=str(row["raybet_match_id"]),
                map_number=int(row["map_number"]),
                cutoff=str(row["first_usable_at"]),
            )
            _add_reference_keys(protected, fallback, root, frame_paths)

    for row in connection.execute(
        """SELECT orders.raybet_match_id, attempt.map_number,
                  orders.signal_transport_at
             FROM shadow_orders AS orders
             JOIN shadow_map_attempts AS attempt
               ON attempt.order_key=orders.order_key"""
    ):
        fallback = _causal_observation_refs(
            connection,
            match_id=str(row["raybet_match_id"]),
            map_number=int(row["map_number"]),
            cutoff=str(row["signal_transport_at"]),
        )
        _add_reference_keys(protected, fallback, root, frame_paths)
    return protected


def _parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _observation_metadata(
    connection: sqlite3.Connection,
    root: Path,
    available: set[str],
    frame_paths: dict[str, str],
) -> tuple[set[str], dict[str, datetime], set[str]]:
    audit: dict[tuple[object, ...], tuple[datetime, str]] = {}
    captured_by_path: dict[str, datetime] = {}
    invalid_times: set[str] = set()
    rows = connection.execute(
        """SELECT raybet_match_id, map_number, captured_at,
                  game_clock_seconds, screen_state, source_frame_ref
             FROM vision_observations
            ORDER BY julianday(captured_at), captured_at, source_frame_ref"""
    )
    for row in rows:
        key = _reference_key(row["source_frame_ref"], root, frame_paths)
        if key is None or key not in available:
            continue
        captured = _parse_utc(row["captured_at"])
        if captured is None:
            invalid_times.add(key)
            continue
        captured_by_path.setdefault(key, captured)
        clock = row["game_clock_seconds"]
        map_number = row["map_number"]
        if (
            map_number is not None
            and type(clock) is int
            and int(clock) >= 0
        ):
            bucket: tuple[object, ...] = (
                str(row["raybet_match_id"]),
                int(map_number),
                "game",
                int(clock) // AUDIT_GAME_BUCKET_SECONDS,
            )
        else:
            bucket = (
                str(row["raybet_match_id"]),
                map_number,
                str(row["screen_state"]),
                int(captured.timestamp()) // AUDIT_WALL_BUCKET_SECONDS,
            )
        current = audit.get(bucket)
        if current is None or (captured, key) < current:
            audit[bucket] = (captured, key)
    return {value[1] for value in audit.values()}, captured_by_path, invalid_times


def _safe_to_delete(candidate: EvidenceFile, root: Path) -> bool:
    if _is_link(candidate.path):
        return False
    try:
        if root.resolve(strict=True) != root:
            return False
        resolved = candidate.path.resolve(strict=True)
        metadata = candidate.path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        resolved == candidate.path
        and _inside_root(resolved, root)
        and stat.S_ISREG(metadata.st_mode)
        and int(metadata.st_dev) == candidate.device
        and int(metadata.st_ino) == candidate.inode
        and int(metadata.st_size) == candidate.size
        and int(metadata.st_mtime_ns) == candidate.mtime_ns
    )


def prune_vision_evidence(
    database: Path,
    evidence_root: Path,
    *,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_RETENTION_TTL,
    max_unprotected_per_match: int = DEFAULT_MAX_UNPROTECTED_PER_MATCH,
    excluded_match_ids: Iterable[str] = (),
    dry_run: bool = True,
) -> VisionRetentionResult:
    """Plan or apply evidence cleanup while preserving immutable lineage."""
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    if max_unprotected_per_match < 0:
        raise ValueError("max_unprotected_per_match cannot be negative")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    root = _resolve_root(Path(evidence_root))
    files, unsafe = _scan_files(root)
    available = {_path_key(item.path) for item in files}
    connection = connect_sqlite(
        Path(database), read_only=True, row_factory=sqlite3.Row
    )
    try:
        _require_tables(connection)
        frame_paths, registered_by_path, frame_identities = _registered_frame_paths(
            connection, root
        )
        referenced = _referenced_keys(
            connection, root, frame_paths, frame_identities
        )
        audit, captured_by_path, invalid_times = _observation_metadata(
            connection, root, available, frame_paths
        )
    finally:
        connection.close()
    referenced.update(invalid_times)

    excluded = {str(value) for value in excluded_match_ids}
    protected_reference = 0
    protected_audit = 0
    protected_active = 0
    retained = 0
    unprotected: dict[str, list[tuple[datetime, EvidenceFile]]] = {}
    for item in files:
        key = _path_key(item.path)
        if key in referenced:
            protected_reference += 1
            continue
        if key in audit:
            protected_audit += 1
            continue
        if item.match_scope in excluded:
            protected_active += 1
            continue
        captured = captured_by_path.get(key) or datetime.fromtimestamp(
            item.mtime_ns / 1_000_000_000, timezone.utc
        )
        unprotected.setdefault(item.match_scope, []).append((captured, item))

    cutoff = current - ttl
    capacity_cutoff = current - MIN_CAPACITY_DELETION_AGE
    planned: list[EvidenceFile] = []
    for rows in unprotected.values():
        rows.sort(key=lambda value: (value[0], str(value[1].relative_path)), reverse=True)
        for index, (captured, item) in enumerate(rows):
            expired = captured <= cutoff
            over_capacity = (
                index >= max_unprotected_per_match
                and captured <= capacity_cutoff
            )
            if expired or over_capacity:
                planned.append(item)
            else:
                retained += 1
    planned.sort(key=lambda item: str(item.relative_path))

    deleted_files = 0
    deleted_bytes = 0
    delete_errors = 0
    if not dry_run and unsafe == 0:
        write_connection: sqlite3.Connection | None = None
        try:
            for item in planned:
                if not _safe_to_delete(item, root):
                    unsafe += 1
                    continue
                frame_ref = registered_by_path.get(_path_key(item.path))
                if frame_ref is not None:
                    if write_connection is None:
                        write_connection = connect_sqlite(
                            Path(database), row_factory=sqlite3.Row
                        )
                    try:
                        retire_vision_frame_artifact(
                            write_connection,
                            frame_ref,
                            reason="vision evidence retention",
                            actor="live_betting.vision_retention",
                            retired_at=current,
                        )
                    except (RuntimeError, sqlite3.Error, ValueError):
                        unsafe += 1
                        continue
                    if not _safe_to_delete(item, root):
                        unsafe += 1
                        continue
                try:
                    item.path.unlink()
                except OSError:
                    delete_errors += 1
                else:
                    deleted_files += 1
                    deleted_bytes += item.size
        finally:
            if write_connection is not None:
                write_connection.close()

    return VisionRetentionResult(
        dry_run=dry_run,
        scanned_files=len(files),
        scanned_bytes=sum(item.size for item in files),
        protected_reference_files=protected_reference,
        protected_audit_files=protected_audit,
        protected_active_files=protected_active,
        retained_unprotected_files=retained,
        planned_deletions=tuple(item.path for item in planned),
        planned_bytes=sum(item.size for item in planned),
        deleted_files=deleted_files,
        deleted_bytes=deleted_bytes,
        unsafe_paths=unsafe,
        delete_errors=delete_errors,
        ttl_seconds=int(ttl.total_seconds()),
        capacity_grace_seconds=int(MIN_CAPACITY_DELETION_AGE.total_seconds()),
        max_unprotected_per_match=max_unprotected_per_match,
    )


__all__ = [
    "AUDIT_GAME_BUCKET_SECONDS",
    "DEFAULT_MAX_UNPROTECTED_PER_MATCH",
    "DEFAULT_RETENTION_TTL",
    "MIN_CAPACITY_DELETION_AGE",
    "RetentionSafetyError",
    "VisionRetentionResult",
    "prune_vision_evidence",
]
