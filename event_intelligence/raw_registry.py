"""Strict authority and audited relocation for event raw artifacts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .raw_archive import verify_raw_artifact_file


_ARTIFACT_COLUMNS = (
    "artifact_id",
    "content_hash",
    "source",
    "storage_path",
    "uncompressed_bytes",
    "compressed_bytes",
    "schema_fingerprint",
)


def _artifact_row(
    connection: sqlite3.Connection,
    artifact_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """SELECT artifact_id, content_hash, source, storage_path,
                  uncompressed_bytes, compressed_bytes, schema_fingerprint
             FROM raw_source_artifacts WHERE artifact_id=?""",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"raw source artifact is missing: {artifact_id}")
    return dict(zip(_ARTIFACT_COLUMNS, tuple(row), strict=True))


def verify_registered_raw_source_artifact(
    connection: sqlite3.Connection,
    artifact_id: str,
) -> Path:
    """Verify the registry metadata against its current physical artifact."""

    row = _artifact_row(connection, artifact_id)
    path = Path(str(row["storage_path"]))
    verify_raw_artifact_file(
        path,
        content_hash=str(row["content_hash"]),
        uncompressed_bytes=int(row["uncompressed_bytes"]),
        compressed_bytes=int(row["compressed_bytes"]),
        expected_schema_fingerprint=str(row["schema_fingerprint"]),
    )
    return path.resolve()


def _controlled_path(
    path: str | Path,
    roots: tuple[Path, ...],
    *,
    require_file: bool,
) -> Path:
    candidate = Path(path)
    if candidate.is_symlink():
        raise RuntimeError(f"raw source relocation path is unsafe: {candidate}")
    resolved = candidate.resolve()
    if not any(_inside(resolved, root) for root in roots):
        raise RuntimeError(f"raw source relocation path escapes allowed roots: {resolved}")
    if require_file and not resolved.is_file():
        raise RuntimeError(f"raw source relocation artifact is missing: {resolved}")
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ValueError(f"raw source relocation {field} must be 1-256 characters")
    return normalized


def raw_source_relocation_id(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def relocate_raw_source_artifacts(
    connection: sqlite3.Connection,
    replacements: Mapping[str, str | Path],
    *,
    allowed_new_roots: Iterable[str | Path],
    incoming_files: Mapping[str, str | Path] | None = None,
    allowed_incoming_roots: Iterable[str | Path] | None = None,
    reason: str,
    actor: str,
    relocated_at: datetime,
) -> tuple[str, ...]:
    """Relocate registered files with one append-only audit per path change."""

    if connection.in_transaction:
        raise RuntimeError("raw source relocation requires an idle connection")
    if relocated_at.tzinfo is None or relocated_at.utcoffset() is None:
        raise ValueError("raw source relocation time must be timezone-aware")
    when = relocated_at.astimezone(timezone.utc).isoformat()
    reason = _text(reason, "reason")
    actor = _text(actor, "actor")
    root_inputs = tuple(allowed_new_roots)
    roots = tuple(dict.fromkeys(Path(root).resolve() for root in root_inputs))
    if not roots:
        raise ValueError("raw source relocation requires an allowed destination root")
    incoming_roots = tuple(
        dict.fromkeys(
            Path(root).resolve()
            for root in (
                root_inputs
                if allowed_incoming_roots is None
                else allowed_incoming_roots
            )
        )
    )
    if not incoming_roots:
        raise ValueError("raw source relocation requires an allowed incoming root")
    if incoming_files is not None and set(incoming_files) != set(replacements):
        raise ValueError("raw source relocation incoming files do not match replacements")

    planned: list[tuple[dict[str, Any], Path, Path]] = []
    seen_destinations: set[Path] = set()
    for artifact_id, replacement in sorted(replacements.items()):
        authority = _artifact_row(connection, artifact_id)
        destination = _controlled_path(replacement, roots, require_file=False)
        incoming = _controlled_path(
            (
                replacement
                if incoming_files is None
                else incoming_files[artifact_id]
            ),
            incoming_roots,
            require_file=True,
        )
        if destination in seen_destinations:
            raise RuntimeError("raw source relocations contain a destination collision")
        seen_destinations.add(destination)
        if str(destination) == str(authority["storage_path"]):
            continue
        verify_raw_artifact_file(
            incoming,
            content_hash=str(authority["content_hash"]),
            uncompressed_bytes=int(authority["uncompressed_bytes"]),
            compressed_bytes=int(authority["compressed_bytes"]),
            expected_schema_fingerprint=str(authority["schema_fingerprint"]),
        )
        planned.append((authority, destination, incoming))

    relocation_ids: list[str] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        for authority, destination, incoming in planned:
            artifact_id = str(authority["artifact_id"])
            current = _artifact_row(connection, artifact_id)
            if current != authority:
                raise RuntimeError(
                    f"raw source artifact changed during relocation: {artifact_id}"
                )
            verify_raw_artifact_file(
                incoming,
                content_hash=str(current["content_hash"]),
                uncompressed_bytes=int(current["uncompressed_bytes"]),
                compressed_bytes=int(current["compressed_bytes"]),
                expected_schema_fingerprint=str(current["schema_fingerprint"]),
            )
            sequence = int(
                connection.execute(
                    """SELECT COALESCE(MAX(relocation_sequence), 0) + 1
                         FROM raw_source_artifact_relocations
                        WHERE artifact_id=?""",
                    (artifact_id,),
                ).fetchone()[0]
            )
            payload = {
                "artifact_id": artifact_id,
                "content_hash": str(current["content_hash"]),
                "source": str(current["source"]),
                "old_storage_path": str(current["storage_path"]),
                "new_storage_path": str(destination),
                "uncompressed_bytes": int(current["uncompressed_bytes"]),
                "compressed_bytes": int(current["compressed_bytes"]),
                "schema_fingerprint": str(current["schema_fingerprint"]),
                "reason": reason,
                "actor": actor,
                "relocated_at": when,
                "relocation_sequence": sequence,
            }
            relocation_id = raw_source_relocation_id(payload)
            connection.execute(
                """INSERT INTO raw_source_artifact_relocations
                   (relocation_id, relocation_sequence, artifact_id, content_hash,
                    source, old_storage_path, new_storage_path, uncompressed_bytes,
                    compressed_bytes, schema_fingerprint, reason, actor, relocated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    relocation_id,
                    sequence,
                    artifact_id,
                    payload["content_hash"],
                    payload["source"],
                    payload["old_storage_path"],
                    payload["new_storage_path"],
                    payload["uncompressed_bytes"],
                    payload["compressed_bytes"],
                    payload["schema_fingerprint"],
                    reason,
                    actor,
                    when,
                ),
            )
            updated = connection.execute(
                "UPDATE raw_source_artifacts SET storage_path=? WHERE artifact_id=?",
                (str(destination), artifact_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"raw source artifact vanished: {artifact_id}")
            relocation_ids.append(relocation_id)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return tuple(relocation_ids)


__all__ = [
    "raw_source_relocation_id",
    "relocate_raw_source_artifacts",
    "verify_registered_raw_source_artifact",
]
