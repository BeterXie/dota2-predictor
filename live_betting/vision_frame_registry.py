"""Content-addressed identity and relocation for vision evidence frames."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VISION_FRAME_REF_PREFIX = "vision-frame:sha256:"


@dataclass(frozen=True)
class VisionFrameReceipt:
    frame_ref: str
    content_sha256: str
    byte_length: int
    storage_path: Path


def vision_frame_ref(content_sha256: str) -> str:
    digest = _digest(content_sha256)
    return f"{VISION_FRAME_REF_PREFIX}{digest}"


def _digest(value: object) -> str:
    normalized = str(value).strip()
    if (
        len(normalized) != 64
        or normalized != normalized.casefold()
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError("vision frame SHA-256 must be 64 lowercase hex characters")
    return normalized


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


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and int(left.st_nlink) == 1
        and int(right.st_nlink) == 1
        and int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
        and int(left.st_size) == int(right.st_size)
    )


def inspect_vision_frame(path: str | Path) -> VisionFrameReceipt:
    """Hash one regular file while rejecting links and path replacement."""

    candidate = Path(path)
    if _is_link(candidate):
        raise RuntimeError(f"vision frame path is unsafe: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
        before = candidate.stat(follow_symlinks=False)
        digest = hashlib.sha256()
        byte_length = 0
        with candidate.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not _same_file(before, opened):
                raise RuntimeError("vision frame changed before verification")
            while chunk := handle.read(1024 * 1024):
                byte_length += len(chunk)
                digest.update(chunk)
            after_read = os.fstat(handle.fileno())
        after_path = candidate.stat(follow_symlinks=False)
    except OSError as error:
        raise RuntimeError(f"vision frame is unavailable: {candidate}") from error
    if (
        not _same_file(opened, after_read)
        or not _same_file(after_read, after_path)
        or byte_length <= 0
        or byte_length != int(after_read.st_size)
    ):
        raise RuntimeError("vision frame changed during verification")
    content_sha256 = digest.hexdigest()
    return VisionFrameReceipt(
        frame_ref=vision_frame_ref(content_sha256),
        content_sha256=content_sha256,
        byte_length=byte_length,
        storage_path=resolved,
    )


def verify_vision_frame_receipt(receipt: VisionFrameReceipt) -> VisionFrameReceipt:
    """Recompute a caller receipt and require content-addressed identity."""

    expected_hash = _digest(receipt.content_sha256)
    if receipt.frame_ref != vision_frame_ref(expected_hash):
        raise RuntimeError("vision frame reference does not match its SHA-256")
    if (
        isinstance(receipt.byte_length, bool)
        or not isinstance(receipt.byte_length, int)
        or receipt.byte_length <= 0
    ):
        raise RuntimeError("vision frame byte length is invalid")
    actual = inspect_vision_frame(receipt.storage_path)
    if (
        actual.frame_ref != receipt.frame_ref
        or actual.content_sha256 != expected_hash
        or actual.byte_length != receipt.byte_length
    ):
        raise RuntimeError("vision frame receipt differs from its file")
    expected_name = f"{expected_hash}.jpg"
    if actual.storage_path.name.casefold() != expected_name:
        raise RuntimeError("vision frame path is not content-addressed")
    return actual


def publish_vision_frame_bytes(
    root: str | Path,
    encoded_frame: bytes,
) -> VisionFrameReceipt:
    """Atomically publish encoded bytes under their content hash."""

    if not isinstance(encoded_frame, bytes) or not encoded_frame:
        raise ValueError("encoded vision frame must be non-empty bytes")
    content_sha256 = hashlib.sha256(encoded_frame).hexdigest()
    root_path = Path(root)
    if root_path.exists() and _is_link(root_path):
        raise RuntimeError("vision frame root cannot be a link or junction")
    destination = (
        root_path / "sha256" / content_sha256[:2] / f"{content_sha256}.jpg"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _is_link(destination.parent):
        raise RuntimeError("vision frame content directory is unsafe")
    expected = VisionFrameReceipt(
        frame_ref=vision_frame_ref(content_sha256),
        content_sha256=content_sha256,
        byte_length=len(encoded_frame),
        storage_path=destination.resolve(),
    )
    if destination.exists():
        return verify_vision_frame_receipt(expected)
    temporary = destination.with_name(f".{uuid.uuid4().hex}.tmp")
    published = False
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded_frame)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            pass
        else:
            published = True
    finally:
        temporary.unlink(missing_ok=True)
    try:
        return verify_vision_frame_receipt(expected)
    except BaseException:
        if published:
            destination.unlink(missing_ok=True)
        raise


def _row(connection: sqlite3.Connection, frame_ref: str) -> tuple[Any, ...]:
    row = connection.execute(
        """SELECT frame_ref, content_sha256, byte_length, storage_path,
                  registered_at
             FROM vision_frame_artifacts WHERE frame_ref=?""",
        (frame_ref,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"vision frame is not registered: {frame_ref}")
    return tuple(row)


def _canonical_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def vision_frame_relocation_id(payload: Mapping[str, Any]) -> str:
    return _canonical_id(payload)


def vision_frame_retirement_id(payload: Mapping[str, Any]) -> str:
    return _canonical_id(payload)


def _effective_storage_path(
    connection: sqlite3.Connection,
    frame_ref: str,
) -> tuple[tuple[Any, ...], Path]:
    artifact = _row(connection, frame_ref)
    current = str(artifact[3])
    relocations = connection.execute(
        """SELECT relocation_id, relocation_sequence, content_sha256,
                  byte_length, old_storage_path, new_storage_path, reason,
                  actor, relocated_at
             FROM vision_frame_artifact_relocations
            WHERE frame_ref=? ORDER BY relocation_sequence""",
        (frame_ref,),
    ).fetchall()
    for expected_sequence, raw in enumerate(relocations, start=1):
        row = tuple(raw)
        payload = {
            "frame_ref": frame_ref,
            "content_sha256": str(row[2]),
            "byte_length": int(row[3]),
            "old_storage_path": str(row[4]),
            "new_storage_path": str(row[5]),
            "reason": str(row[6]),
            "actor": str(row[7]),
            "relocated_at": str(row[8]),
            "relocation_sequence": int(row[1]),
        }
        if (
            int(row[1]) != expected_sequence
            or str(row[2]) != str(artifact[1])
            or int(row[3]) != int(artifact[2])
            or str(row[4]) != current
            or str(row[0]) != vision_frame_relocation_id(payload)
        ):
            raise RuntimeError("vision frame relocation audit is invalid")
        current = str(row[5])
    return artifact, Path(current)


def _retired(connection: sqlite3.Connection, frame_ref: str) -> bool:
    row = connection.execute(
        """SELECT retirement_id, content_sha256, byte_length, storage_path,
                  reason, actor, retired_at
             FROM vision_frame_artifact_retirements WHERE frame_ref=?""",
        (frame_ref,),
    ).fetchone()
    if row is None:
        return False
    artifact, effective = _effective_storage_path(connection, frame_ref)
    values = tuple(row)
    payload = {
        "frame_ref": frame_ref,
        "content_sha256": str(values[1]),
        "byte_length": int(values[2]),
        "storage_path": str(values[3]),
        "reason": str(values[4]),
        "actor": str(values[5]),
        "retired_at": str(values[6]),
    }
    if (
        str(values[0]) != vision_frame_retirement_id(payload)
        or str(values[1]) != str(artifact[1])
        or int(values[2]) != int(artifact[2])
        or Path(str(values[3])) != effective
    ):
        raise RuntimeError("vision frame retirement audit is invalid")
    return True


def register_vision_frame_artifact(
    connection: sqlite3.Connection,
    receipt: VisionFrameReceipt,
    *,
    registered_at: datetime,
) -> bool:
    """Register one verified immutable frame identity."""

    actual = verify_vision_frame_receipt(receipt)
    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        raise ValueError("vision frame registration time must be timezone-aware")
    values = (
        actual.frame_ref,
        actual.content_sha256,
        actual.byte_length,
        str(actual.storage_path),
        registered_at.astimezone(timezone.utc).isoformat(),
    )
    cursor = connection.execute(
        """INSERT OR IGNORE INTO vision_frame_artifacts
           (frame_ref, content_sha256, byte_length, storage_path, registered_at)
           VALUES (?, ?, ?, ?, ?)""",
        values,
    )
    existing = _row(connection, actual.frame_ref)
    if tuple(existing[:4]) != values[:4]:
        raise RuntimeError("vision frame registry identity conflict")
    if _retired(connection, actual.frame_ref):
        raise RuntimeError("retired vision frame cannot be registered again")
    return cursor.rowcount == 1


def verify_registered_vision_frame(
    connection: sqlite3.Connection,
    frame_ref: str,
    *,
    expected_sha256: str | None = None,
    expected_bytes: int | None = None,
) -> VisionFrameReceipt:
    """Verify registry/audit metadata and the current physical frame."""

    artifact, path = _effective_storage_path(connection, frame_ref)
    if _retired(connection, frame_ref):
        raise RuntimeError("vision frame is retired")
    receipt = VisionFrameReceipt(
        frame_ref=str(artifact[0]),
        content_sha256=str(artifact[1]),
        byte_length=int(artifact[2]),
        storage_path=path,
    )
    if expected_sha256 is not None and _digest(expected_sha256) != receipt.content_sha256:
        raise RuntimeError("vision frame bound SHA-256 differs from registry")
    if expected_bytes is not None and int(expected_bytes) != receipt.byte_length:
        raise RuntimeError("vision frame bound byte length differs from registry")
    return verify_vision_frame_receipt(receipt)


def verify_bound_order_vision_frame(
    connection: sqlite3.Connection,
    order_key: str,
) -> VisionFrameReceipt:
    """Reverify the exact frame identity shared by an order and its decision."""

    row = connection.execute(
        """SELECT orders.vision_source_frame_ref,
                  orders.vision_source_frame_sha256,
                  orders.vision_source_frame_bytes,
                  decision.vision_source_frame_ref,
                  decision.vision_source_frame_sha256,
                  decision.vision_source_frame_bytes
             FROM shadow_orders AS orders
             JOIN shadow_order_decision_lineage AS lineage
               ON lineage.order_key=orders.order_key
             JOIN strategy_decisions AS decision
               ON decision.decision_key=lineage.decision_key
            WHERE orders.order_key=?""",
        (order_key,),
    ).fetchone()
    if row is None or any(value is None for value in row):
        raise RuntimeError("order vision frame authority is missing")
    values = tuple(row)
    if values[:3] != values[3:]:
        raise RuntimeError("order vision frame authority differs from decision")
    return verify_registered_vision_frame(
        connection,
        str(values[0]),
        expected_sha256=str(values[1]),
        expected_bytes=int(values[2]),
    )


def verify_vision_frame_registry(
    connection: sqlite3.Connection,
    *,
    require_active_files: bool = True,
) -> int:
    """Validate every audit chain and, by default, every active frame file."""

    rows = connection.execute(
        """SELECT frame_ref, content_sha256, byte_length
             FROM vision_frame_artifacts ORDER BY frame_ref"""
    ).fetchall()
    active = 0
    for raw in rows:
        frame_ref, content_sha256, byte_length = tuple(raw)
        _effective_storage_path(connection, str(frame_ref))
        if _retired(connection, str(frame_ref)):
            continue
        active += 1
        if require_active_files:
            verify_registered_vision_frame(
                connection,
                str(frame_ref),
                expected_sha256=str(content_sha256),
                expected_bytes=int(byte_length),
            )
    return active


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _controlled_path(
    value: str | Path,
    roots: tuple[Path, ...],
    *,
    require_file: bool,
) -> Path:
    candidate = Path(value)
    if candidate.exists() and _is_link(candidate):
        raise RuntimeError(f"vision frame relocation path is unsafe: {candidate}")
    resolved = candidate.resolve()
    if not any(_inside(resolved, root) for root in roots):
        raise RuntimeError(f"vision frame relocation path escapes roots: {resolved}")
    if require_file and not resolved.is_file():
        raise RuntimeError(f"vision frame relocation input is missing: {resolved}")
    return resolved


def relocate_vision_frame_artifacts(
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
    """Append verified path relocations without mutating frame identity."""

    if connection.in_transaction:
        raise RuntimeError("vision frame relocation requires an idle connection")
    if relocated_at.tzinfo is None or relocated_at.utcoffset() is None:
        raise ValueError("vision frame relocation time must be timezone-aware")
    reason = reason.strip()
    actor = actor.strip()
    if not reason or not actor or len(reason) > 256 or len(actor) > 256:
        raise ValueError("vision frame relocation reason and actor are required")
    roots = tuple(dict.fromkeys(Path(root).resolve() for root in allowed_new_roots))
    incoming_roots = tuple(
        dict.fromkeys(
            Path(root).resolve()
            for root in (
                allowed_new_roots
                if allowed_incoming_roots is None
                else allowed_incoming_roots
            )
        )
    )
    if not roots or not incoming_roots:
        raise ValueError("vision frame relocation roots are required")
    if incoming_files is not None and set(incoming_files) != set(replacements):
        raise ValueError("vision frame relocation inputs do not match replacements")
    planned: list[tuple[str, str, int, Path, Path, int]] = []
    destinations: set[Path] = set()
    for frame_ref, replacement in sorted(replacements.items()):
        artifact, current = _effective_storage_path(connection, frame_ref)
        if _retired(connection, frame_ref):
            raise RuntimeError("retired vision frame cannot be relocated")
        destination = _controlled_path(replacement, roots, require_file=False)
        incoming = _controlled_path(
            replacement if incoming_files is None else incoming_files[frame_ref],
            incoming_roots,
            require_file=True,
        )
        if destination in destinations:
            raise RuntimeError("vision frame relocation destination collision")
        destinations.add(destination)
        if destination == current:
            continue
        receipt = VisionFrameReceipt(
            frame_ref=frame_ref,
            content_sha256=str(artifact[1]),
            byte_length=int(artifact[2]),
            storage_path=incoming,
        )
        verify_vision_frame_receipt(receipt)
        sequence = int(
            connection.execute(
                """SELECT COALESCE(MAX(relocation_sequence), 0) + 1
                     FROM vision_frame_artifact_relocations WHERE frame_ref=?""",
                (frame_ref,),
            ).fetchone()[0]
        )
        planned.append(
            (frame_ref, str(artifact[1]), int(artifact[2]), current, destination, sequence)
        )
    when = relocated_at.astimezone(timezone.utc).isoformat()
    relocation_ids: list[str] = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        for frame_ref, digest, size, old, new, sequence in planned:
            current_artifact, current_path = _effective_storage_path(
                connection, frame_ref
            )
            if (
                str(current_artifact[1]) != digest
                or int(current_artifact[2]) != size
                or current_path != old
            ):
                raise RuntimeError("vision frame changed during relocation")
            payload = {
                "frame_ref": frame_ref,
                "content_sha256": digest,
                "byte_length": size,
                "old_storage_path": str(old),
                "new_storage_path": str(new),
                "reason": reason,
                "actor": actor,
                "relocated_at": when,
                "relocation_sequence": sequence,
            }
            relocation_id = vision_frame_relocation_id(payload)
            connection.execute(
                """INSERT INTO vision_frame_artifact_relocations
                   (relocation_id, relocation_sequence, frame_ref, content_sha256,
                    byte_length, old_storage_path, new_storage_path, reason,
                    actor, relocated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    relocation_id,
                    sequence,
                    frame_ref,
                    digest,
                    size,
                    str(old),
                    str(new),
                    reason,
                    actor,
                    when,
                ),
            )
            relocation_ids.append(relocation_id)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return tuple(relocation_ids)


def retire_vision_frame_artifact(
    connection: sqlite3.Connection,
    frame_ref: str,
    *,
    reason: str,
    actor: str,
    retired_at: datetime,
) -> str:
    """Retire one verified unprotected frame before physical deletion."""

    if connection.in_transaction:
        raise RuntimeError("vision frame retirement requires an idle connection")
    if retired_at.tzinfo is None or retired_at.utcoffset() is None:
        raise ValueError("vision frame retirement time must be timezone-aware")
    reason = reason.strip()
    actor = actor.strip()
    if not reason or not actor or len(reason) > 256 or len(actor) > 256:
        raise ValueError("vision frame retirement reason and actor are required")
    receipt = verify_registered_vision_frame(connection, frame_ref)
    when = retired_at.astimezone(timezone.utc).isoformat()
    payload = {
        "frame_ref": receipt.frame_ref,
        "content_sha256": receipt.content_sha256,
        "byte_length": receipt.byte_length,
        "storage_path": str(receipt.storage_path),
        "reason": reason,
        "actor": actor,
        "retired_at": when,
    }
    retirement_id = vision_frame_retirement_id(payload)
    connection.execute("BEGIN IMMEDIATE")
    try:
        verify_registered_vision_frame(
            connection,
            frame_ref,
            expected_sha256=receipt.content_sha256,
            expected_bytes=receipt.byte_length,
        )
        connection.execute(
            """INSERT INTO vision_frame_artifact_retirements
               (retirement_id, frame_ref, content_sha256, byte_length,
                storage_path, reason, actor, retired_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                retirement_id,
                frame_ref,
                receipt.content_sha256,
                receipt.byte_length,
                str(receipt.storage_path),
                reason,
                actor,
                when,
            ),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return retirement_id


__all__ = [
    "VISION_FRAME_REF_PREFIX",
    "VisionFrameReceipt",
    "inspect_vision_frame",
    "publish_vision_frame_bytes",
    "register_vision_frame_artifact",
    "relocate_vision_frame_artifacts",
    "retire_vision_frame_artifact",
    "verify_bound_order_vision_frame",
    "verify_registered_vision_frame",
    "verify_vision_frame_registry",
    "verify_vision_frame_receipt",
    "vision_frame_ref",
    "vision_frame_relocation_id",
    "vision_frame_retirement_id",
]
