"""Append-only, content-addressed milestone revocation ledger.

Threat model: the ledger directory, including its index, objects, seals, lock,
and transaction journal, is one attacker-controlled failure domain.  Integrity
therefore depends on an exact head anchor and pair-baseline manifest supplied by
the caller from a frozen/signed manifest, or from independently stored files
whose SHA-256 values are supplied separately.  Ledger-local copies are never
treated as trust anchors.  The implementation detects rollback, substitution,
non-canonical JSON, unsafe links, and pre-cutoff database/raw mutation; it does
not claim resistance after the independent anchor or baseline itself is forged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import rfc8785


BINDING_SCHEMA = "dota2-milestone-revocation-ledger-binding-v1"
INDEX_SCHEMA = "dota2-milestone-revocation-ledger-index-v1"
RECORD_SCHEMA = "dota2-milestone-revocation-record-v1"
ANCHOR_SCHEMA = "dota2-milestone-revocation-external-anchor-v1"
PAIR_BASELINE_SCHEMA = "dota2-milestone-revocation-pair-baseline-v1"
TRANSACTION_SCHEMA = "dota2-milestone-revocation-transaction-v1"
_ZERO_HASH = "0" * 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEAL_RE = re.compile(r"^(?P<sequence>[0-9]{20})-(?P<digest>[0-9a-f]{64})\.json$")
_CONFLICT_TYPES = frozenset({"mapping", "vision", "draft", "source", "settlement"})
_MILESTONE_ORDER = ("M1", "M2", "M3-C", "M3-E", "M4-C", "M4-E")
_ROLE_NAMES = frozenset(
    {
        "execution_owner",
        "independent_verifier",
        "production_db_operator",
        "m4_decision_owner",
        "m4_analysis_author",
    }
)
_AFFECTED_FIELDS = (
    "decision_keys",
    "order_keys",
    "settlement_keys",
    "sample_keys",
)
_LEDGER_FIXED_ENTRIES = {"index.jsonl", "objects", "seals", "ledger.lock"}


@dataclass(frozen=True)
class MilestoneRevocationConfig:
    """Complete caller-owned configuration for one verified ledger projection."""

    root: Path
    database_path: Path
    raw_root: Path
    expected_anchor: Mapping[str, object] | Path
    pair_manifest: bytes | Mapping[str, object] | Path
    expected_pair_manifest_hash: str
    expected_anchor_hash: str | None = None


class MilestoneRevocationIntegrityError(RuntimeError):
    """Raised when a configured ledger cannot be proven intact and paired."""


def canonical_bytes(value: Any) -> bytes:
    return rfc8785.dumps(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _strict_json(raw: bytes, label: str) -> Any:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise MilestoneRevocationIntegrityError(f"{label} JSON is invalid") from error
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError) as error:
        raise MilestoneRevocationIntegrityError(f"{label} JSON is invalid") from error
    if encoded != raw:
        raise MilestoneRevocationIntegrityError(f"{label} is not canonical")
    return value


def _require_regular_file(path: Path, label: str, *, links: int = 1) -> os.stat_result:
    if path.is_symlink():
        raise MilestoneRevocationIntegrityError(f"{label} is a symlink")
    try:
        metadata = path.stat()
    except OSError as error:
        raise MilestoneRevocationIntegrityError(f"{label} is missing or unsafe") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise MilestoneRevocationIntegrityError(f"{label} is not a regular file")
    if int(metadata.st_nlink) != links:
        raise MilestoneRevocationIntegrityError(f"{label} has an external hard link")
    return metadata


def _require_directory(path: Path, label: str) -> os.stat_result:
    if path.is_symlink():
        raise MilestoneRevocationIntegrityError(f"{label} is a symlink")
    try:
        metadata = path.stat()
    except OSError as error:
        raise MilestoneRevocationIntegrityError(f"{label} is missing or unsafe") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise MilestoneRevocationIntegrityError(f"{label} is not a directory")
    return metadata


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _external_bytes(
    value: bytes | Mapping[str, object] | Path,
    *,
    expected_hash: str | None,
    ledger_root: Path,
    label: str,
) -> bytes:
    if isinstance(value, Path):
        if expected_hash is None:
            raise MilestoneRevocationIntegrityError(f"{label} file requires expected hash")
        _require_hash(expected_hash, f"expected {label} hash")
        _require_regular_file(value, f"external {label}")
        if _is_within(value, ledger_root):
            raise MilestoneRevocationIntegrityError(
                f"external {label} must be independent of the ledger directory"
            )
        raw = value.read_bytes()
        if _sha256(raw) != expected_hash:
            raise MilestoneRevocationIntegrityError(f"external {label} hash mismatch")
        return raw
    if expected_hash is not None:
        raise MilestoneRevocationIntegrityError(
            f"expected {label} file hash is only valid with an independent file"
        )
    if isinstance(value, bytes):
        return value
    if isinstance(value, Mapping):
        return canonical_bytes(dict(value))
    raise MilestoneRevocationIntegrityError(f"external {label} is malformed")


def _json_scalar(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise MilestoneRevocationIntegrityError("database baseline contains non-finite data")
        return value
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    raise MilestoneRevocationIntegrityError("database baseline contains unsupported data")


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _database_baseline(database_path: Path) -> dict[str, object]:
    _path_identity(database_path, directory=False)
    uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("BEGIN")
        tables = connection.execute(
            """SELECT name, sql FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%'
                 ORDER BY name"""
        ).fetchall()
        output = []
        for name_value, sql_value in tables:
            name = str(name_value)
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_quoted(name)})")
            ]
            try:
                cutoff = connection.execute(
                    f"SELECT MAX(rowid) FROM {_quoted(name)}"
                ).fetchone()[0]
            except sqlite3.DatabaseError as error:
                raise MilestoneRevocationIntegrityError(
                    f"database baseline table lacks verifiable rowid: {name}"
                ) from error
            if cutoff is None:
                rows: list[list[object]] = []
            else:
                selected = ", ".join(_quoted(column) for column in columns)
                rows = [
                    [int(row[0]), *(_json_scalar(item) for item in row[1:])]
                    for row in connection.execute(
                        f"SELECT rowid, {selected} FROM {_quoted(name)} "
                        "WHERE rowid<=? ORDER BY rowid",
                        (int(cutoff),),
                    )
                ]
            output.append(
                {
                    "table": name,
                    "schema_sql_hash": _sha256(str(sql_value).encode("utf-8")),
                    "columns": columns,
                    "rowid_cutoff": None if cutoff is None else int(cutoff),
                    "row_count_at_cutoff": len(rows),
                    "logical_prefix_hash": _sha256(canonical_bytes(rows)),
                }
            )
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    return {"resolved_path": str(database_path.resolve(strict=True)), "tables": output}


def _raw_baseline(raw_root: Path) -> dict[str, object]:
    root = raw_root.resolve(strict=True)
    _require_directory(raw_root, "raw baseline root")
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir() and not path.is_symlink():
            continue
        _require_regular_file(path, "raw baseline file")
        raw = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "cutoff_bytes": len(raw),
                "sha256_through_cutoff": _sha256(raw),
            }
        )
    return {"resolved_path": str(root), "files": files}


def create_pair_baseline_manifest(
    database_path: Path,
    raw_root: Path,
    *,
    p0_baseline_evidence_identity: str,
) -> bytes:
    """Freeze a recomputable pre-cutoff logical baseline without hashing DB suffixes."""

    _require_hash(p0_baseline_evidence_identity, "P0 baseline evidence identity")
    return canonical_bytes(
        {
            "schema": PAIR_BASELINE_SCHEMA,
            "p0_baseline_evidence_identity": p0_baseline_evidence_identity,
            "database": _database_baseline(Path(database_path)),
            "raw_root": _raw_baseline(Path(raw_root)),
        }
    )


def _path_identity(path: Path, *, directory: bool) -> dict[str, object]:
    if path.is_symlink():
        raise MilestoneRevocationIntegrityError(f"paired path is a symlink: {path}")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(metadata.st_mode):
        kind = "directory" if directory else "regular file"
        raise MilestoneRevocationIntegrityError(f"paired path is not a {kind}: {resolved}")
    if not directory and int(metadata.st_nlink) != 1:
        raise MilestoneRevocationIntegrityError(
            f"paired database must have exactly one hard link: {resolved}"
        )
    device = int(metadata.st_dev)
    inode = int(metadata.st_ino)
    if inode <= 0:
        raise MilestoneRevocationIntegrityError(f"paired path identity unavailable: {resolved}")
    return {
        "resolved_path": str(resolved),
        # Windows file indexes routinely exceed RFC 8785's safe JSON integer
        # domain. Decimal strings preserve the exact stable identity.
        "device": str(device),
        "inode": str(inode),
    }


def _load_pair_manifest(
    value: bytes | Mapping[str, object] | Path,
    *,
    expected_hash: str,
    ledger_root: Path,
) -> tuple[Mapping[str, Any], bytes]:
    _require_hash(expected_hash, "expected pair manifest hash")
    raw = _external_bytes(
        value,
        expected_hash=expected_hash if isinstance(value, Path) else None,
        ledger_root=ledger_root,
        label="pair baseline manifest",
    )
    if _sha256(raw) != expected_hash:
        raise MilestoneRevocationIntegrityError("pair baseline manifest hash mismatch")
    manifest = _require_exact_fields(
        _strict_json(raw, "pair baseline manifest"),
        {"schema", "p0_baseline_evidence_identity", "database", "raw_root"},
        "pair baseline manifest",
    )
    if manifest["schema"] != PAIR_BASELINE_SCHEMA:
        raise MilestoneRevocationIntegrityError("unsupported pair baseline manifest schema")
    _require_hash(
        manifest["p0_baseline_evidence_identity"], "P0 baseline evidence identity"
    )
    return manifest, raw


def _verify_database_baseline(
    expected: object, database_path: Path
) -> None:
    value = _require_exact_fields(
        expected, {"resolved_path", "tables"}, "database baseline"
    )
    if value["resolved_path"] != str(database_path.resolve(strict=True)):
        raise MilestoneRevocationIntegrityError("database baseline path mismatch")
    if not isinstance(value["tables"], list):
        raise MilestoneRevocationIntegrityError("database baseline tables are malformed")
    uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        schema_rows = connection.execute(
            """SELECT name, sql FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
        ).fetchall()
        schema_by_name = {str(row[0]): str(row[1]) for row in schema_rows}
        actual_columns = {
            name: [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_quoted(name)})")
            ]
            for name in schema_by_name
        }
    finally:
        connection.close()
    for expected_table_value in value["tables"]:
        expected_table = _require_exact_fields(
            expected_table_value,
            {
                "table",
                "schema_sql_hash",
                "columns",
                "rowid_cutoff",
                "row_count_at_cutoff",
                "logical_prefix_hash",
            },
            "database baseline table",
        )
        name = expected_table["table"]
        if not isinstance(name, str) or name not in schema_by_name:
            raise MilestoneRevocationIntegrityError("database baseline table is missing")
        # Recompute using the frozen cutoff rather than the current MAX(rowid),
        # so legitimate suffix inserts do not invalidate the baseline.
        uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            columns = expected_table["columns"]
            if not isinstance(columns, list) or any(
                not isinstance(column, str) for column in columns
            ):
                raise MilestoneRevocationIntegrityError(
                    "database baseline columns are malformed"
                )
            cutoff = expected_table["rowid_cutoff"]
            if cutoff is not None and (type(cutoff) is not int or cutoff < 1):
                raise MilestoneRevocationIntegrityError(
                    "database baseline cutoff is malformed"
                )
            selected = ", ".join(_quoted(column) for column in columns)
            rows = [] if cutoff is None else [
                [int(row[0]), *(_json_scalar(item) for item in row[1:])]
                for row in connection.execute(
                    f"SELECT rowid, {selected} FROM {_quoted(name)} "
                    "WHERE rowid<=? ORDER BY rowid",
                    (cutoff,),
                )
            ]
        except sqlite3.DatabaseError as error:
            raise MilestoneRevocationIntegrityError(
                f"database baseline cannot be recomputed: {name}"
            ) from error
        finally:
            connection.close()
        if (
            _sha256(schema_by_name[name].encode("utf-8"))
            != expected_table["schema_sql_hash"]
            or actual_columns[name] != columns
            or len(rows) != expected_table["row_count_at_cutoff"]
            or _sha256(canonical_bytes(rows)) != expected_table["logical_prefix_hash"]
        ):
            raise MilestoneRevocationIntegrityError(
                f"database baseline prefix mismatch: {name}"
            )


def _verify_raw_baseline(expected: object, raw_root: Path) -> None:
    value = _require_exact_fields(expected, {"resolved_path", "files"}, "raw baseline")
    root = raw_root.resolve(strict=True)
    if value["resolved_path"] != str(root) or not isinstance(value["files"], list):
        raise MilestoneRevocationIntegrityError("raw baseline root is malformed")
    _require_directory(raw_root, "raw baseline root")
    seen: set[str] = set()
    for record_value in value["files"]:
        record = _require_exact_fields(
            record_value,
            {"path", "cutoff_bytes", "sha256_through_cutoff"},
            "raw baseline file",
        )
        relative = record["path"]
        cutoff = record["cutoff_bytes"]
        if (
            not isinstance(relative, str)
            or relative in seen
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or type(cutoff) is not int
            or cutoff < 0
        ):
            raise MilestoneRevocationIntegrityError("raw baseline file is malformed")
        seen.add(relative)
        _require_hash(record["sha256_through_cutoff"], "raw baseline prefix hash")
        path = root / Path(relative)
        current = root
        for part in Path(relative).parts[:-1]:
            current /= part
            if current.is_symlink():
                raise MilestoneRevocationIntegrityError(
                    "raw baseline path contains a symlink"
                )
        _require_regular_file(path, "raw baseline file")
        if not _is_within(path, root):
            raise MilestoneRevocationIntegrityError("raw baseline path escapes its root")
        with path.open("rb") as stream:
            prefix = stream.read(cutoff)
        if len(prefix) != cutoff or _sha256(prefix) != record["sha256_through_cutoff"]:
            raise MilestoneRevocationIntegrityError(
                f"raw baseline prefix mismatch: {relative}"
            )


def _verify_pair_manifest(
    manifest: Mapping[str, Any], database_path: Path, raw_root: Path
) -> None:
    _verify_database_baseline(manifest["database"], database_path)
    _verify_raw_baseline(manifest["raw_root"], raw_root)


def _require_connection_database(connection: Any, database_path: Path) -> None:
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except Exception as error:
        raise MilestoneRevocationIntegrityError(
            "configured ledger database connection identity is unavailable"
        ) from error
    main_paths = [str(row[2]) for row in rows if str(row[1]) == "main"]
    if len(main_paths) != 1 or not main_paths[0]:
        raise MilestoneRevocationIntegrityError(
            "configured ledger requires a file-backed main database"
        )
    if Path(main_paths[0]).resolve() != database_path.resolve():
        raise MilestoneRevocationIntegrityError(
            "configured ledger database path differs from the report connection"
        )


def _existing_keys(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    values: set[str],
) -> set[str]:
    output: set[str] = set()
    ordered = sorted(values)
    for offset in range(0, len(ordered), 500):
        chunk = ordered[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"SELECT {_quoted(column)} FROM {_quoted(table)} "
            f"WHERE {_quoted(column)} IN ({placeholders})",
            chunk,
        ).fetchall()
        output.update(str(row[0]) for row in rows)
    return output


def _verify_record_lineage(
    records: Sequence[Mapping[str, Any]],
    database_path: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Verify declared sample/settlement/order/decision edges against SQLite."""

    if not records:
        return
    owned = connection is None
    if connection is None:
        uri = database_path.resolve(strict=True).as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    try:
        decision_keys: set[str] = set()
        order_keys: set[str] = set()
        settlement_keys: set[str] = set()
        explicit: list[tuple[str, str, str]] = []
        for record in records:
            affected = record["affected"]
            decision_keys.update(str(key) for key in affected["decision_keys"])
            order_keys.update(str(key) for key in affected["order_keys"])
            settlement_keys.update(str(key) for key in affected["settlement_keys"])
            for lineage in affected["sample_lineage"]:
                settlement_key = str(lineage["settlement_key"])
                order_key = str(lineage["order_key"])
                decision_key = str(lineage["decision_key"])
                if settlement_key != order_key:
                    raise MilestoneRevocationIntegrityError(
                        "sample settlement/order lineage identity mismatch"
                    )
                settlement_keys.add(settlement_key)
                order_keys.add(order_key)
                decision_keys.add(decision_key)
                explicit.append((settlement_key, order_key, decision_key))
        if decision_keys and _existing_keys(
            connection,
            table="strategy_decisions",
            column="decision_key",
            values=decision_keys,
        ) != decision_keys:
            raise MilestoneRevocationIntegrityError(
                "affected decision lineage is not persisted"
            )
        if order_keys and _existing_keys(
            connection,
            table="shadow_orders",
            column="order_key",
            values=order_keys,
        ) != order_keys:
            raise MilestoneRevocationIntegrityError(
                "affected order lineage is not persisted"
            )
        if settlement_keys and _existing_keys(
            connection,
            table="settlements",
            column="order_key",
            values=settlement_keys,
        ) != settlement_keys:
            raise MilestoneRevocationIntegrityError(
                "affected settlement lineage is not persisted"
            )
        linked_orders = order_keys | settlement_keys
        if linked_orders:
            actual_lineage: set[tuple[str, str]] = set()
            ordered = sorted(linked_orders)
            for offset in range(0, len(ordered), 500):
                chunk = ordered[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                actual_lineage.update(
                    (str(row[0]), str(row[1]))
                    for row in connection.execute(
                        "SELECT order_key, decision_key "
                        "FROM shadow_order_decision_lineage "
                        f"WHERE order_key IN ({placeholders})",
                        chunk,
                    ).fetchall()
                )
            actual_orders = {order_key for order_key, _ in actual_lineage}
            if actual_orders != linked_orders:
                raise MilestoneRevocationIntegrityError(
                    "affected order/decision lineage is not persisted"
                )
            linked_decisions = {
                decision_key for _order_key, decision_key in actual_lineage
            }
            if _existing_keys(
                connection,
                table="strategy_decisions",
                column="decision_key",
                values=linked_decisions,
            ) != linked_decisions:
                raise MilestoneRevocationIntegrityError(
                    "affected order points to a missing strategy decision"
                )
            if any(
                (order_key, decision_key) not in actual_lineage
                for _settlement_key, order_key, decision_key in explicit
            ):
                raise MilestoneRevocationIntegrityError(
                    "sample/order/decision lineage does not match persisted lineage"
                )
    except sqlite3.Error as error:
        raise MilestoneRevocationIntegrityError(
            "affected sample/settlement/order/decision lineage is unverifiable"
        ) from error
    finally:
        if owned:
            connection.close()


def _binding_record(
    database_path: Path,
    raw_root: Path,
    *,
    pair_manifest_hash: str,
    p0_baseline_evidence_identity: str,
) -> dict[str, object]:
    return {
        "schema": BINDING_SCHEMA,
        "bound_at": _utc_now(),
        "database": _path_identity(database_path, directory=False),
        "raw_root": _path_identity(raw_root, directory=True),
        "pair_manifest_hash": pair_manifest_hash,
        "p0_baseline_evidence_identity": p0_baseline_evidence_identity,
    }


def _write_immutable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _require_regular_file(path, "immutable ledger path")
        if path.read_bytes() != content:
            raise MilestoneRevocationIntegrityError(
                f"immutable ledger path already contains different bytes: {path}"
            ) from None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _ledger_lock(root: Path) -> Iterator[None]:
    lock_path = root / "ledger.lock"
    _require_regular_file(lock_path, "ledger lock")
    with lock_path.open("r+b", buffering=0) as stream:
        _lock_stream(stream)
        try:
            _require_regular_file(lock_path, "ledger lock")
            yield
        finally:
            _unlock_stream(stream)


def _lock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _index_entry(
    *, sequence: int, previous_entry_hash: str, object_hash: str, object_type: str
) -> tuple[dict[str, object], bytes, str]:
    value = {
        "schema": INDEX_SCHEMA,
        "sequence": sequence,
        "previous_entry_hash": previous_entry_hash,
        "object_hash": object_hash,
        "object_type": object_type,
    }
    encoded = canonical_bytes(value)
    return value, encoded, _sha256(encoded)


def _append_index(root: Path, entry_bytes: bytes, entry_hash: str, sequence: int) -> None:
    seal = root / "seals" / f"{sequence:020d}-{entry_hash}.json"
    _write_immutable(seal, entry_bytes)
    index_path = root / "index.jsonl"
    with index_path.open("ab") as stream:
        stream.write(entry_bytes + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def initialize_milestone_revocation_ledger(
    root: Path,
    *,
    database_path: Path,
    raw_root: Path,
    pair_manifest: bytes | Mapping[str, object] | Path,
    expected_pair_manifest_hash: str,
    p0_baseline_evidence_identity: str,
) -> dict[str, object]:
    """Create a fresh ledger bound to one stable database/raw pair."""

    root = Path(root)
    if root.is_symlink() or (root.exists() and any(root.iterdir())):
        raise ValueError("milestone revocation ledger root must be fresh/empty")
    root.mkdir(parents=True, exist_ok=True)
    (root / "objects").mkdir()
    (root / "seals").mkdir()
    (root / "index.jsonl").touch(exist_ok=False)
    (root / "ledger.lock").write_bytes(b"\0")
    manifest, _ = _load_pair_manifest(
        pair_manifest,
        expected_hash=expected_pair_manifest_hash,
        ledger_root=root,
    )
    if manifest["p0_baseline_evidence_identity"] != p0_baseline_evidence_identity:
        raise MilestoneRevocationIntegrityError("P0 baseline evidence identity mismatch")
    _verify_pair_manifest(manifest, Path(database_path), Path(raw_root))
    with _ledger_lock(root):
        binding = _binding_record(
            Path(database_path),
            Path(raw_root),
            pair_manifest_hash=expected_pair_manifest_hash,
            p0_baseline_evidence_identity=p0_baseline_evidence_identity,
        )
        object_bytes = canonical_bytes(binding)
        object_hash = _sha256(object_bytes)
        _write_immutable(root / "objects" / f"{object_hash}.json", object_bytes)
        _, entry_bytes, entry_hash = _index_entry(
            sequence=1,
            previous_entry_hash=_ZERO_HASH,
            object_hash=object_hash,
            object_type="binding",
        )
        _append_index(root, entry_bytes, entry_hash, 1)
        _fsync_directory(root)
    return _anchor_for(
        binding=binding,
        head_hash=entry_hash,
        sequence=1,
        genesis_entry_hash=entry_hash,
    )


def required_revoked_milestones(conflict_type: str) -> tuple[str, ...]:
    if conflict_type not in _CONFLICT_TYPES:
        raise ValueError("unsupported milestone revocation conflict type")
    if conflict_type == "settlement":
        return _MILESTONE_ORDER[1:]
    return _MILESTONE_ORDER


def _require_exact_fields(
    value: object, fields: set[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise MilestoneRevocationIntegrityError(f"{label} fields are malformed")
    return value


def _require_hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MilestoneRevocationIntegrityError(f"{label} must be a SHA-256 digest")


def _validate_anchor(value: object) -> dict[str, object]:
    anchor = _require_exact_fields(
        value,
        {
            "schema",
            "ledger_id",
            "head_entry_hash",
            "sequence",
            "minimum_sequence",
            "genesis_entry_hash",
            "pair_manifest_hash",
            "p0_baseline_evidence_identity",
        },
        "external anchor",
    )
    for field in (
        "ledger_id",
        "head_entry_hash",
        "genesis_entry_hash",
        "pair_manifest_hash",
        "p0_baseline_evidence_identity",
    ):
        _require_hash(anchor[field], f"external anchor {field}")
    sequence = anchor["sequence"]
    minimum = anchor["minimum_sequence"]
    if (
        anchor["schema"] != ANCHOR_SCHEMA
        or type(sequence) is not int
        or sequence < 1
        or minimum != sequence
    ):
        raise MilestoneRevocationIntegrityError("external anchor is malformed")
    return dict(anchor)


def _anchor_for(
    *,
    binding: Mapping[str, Any],
    head_hash: str,
    sequence: int,
    genesis_entry_hash: str,
) -> dict[str, object]:
    return {
        "schema": ANCHOR_SCHEMA,
        "ledger_id": _sha256(canonical_bytes(binding)),
        "head_entry_hash": head_hash,
        "sequence": sequence,
        "minimum_sequence": sequence,
        "genesis_entry_hash": genesis_entry_hash,
        "pair_manifest_hash": binding["pair_manifest_hash"],
        "p0_baseline_evidence_identity": binding[
            "p0_baseline_evidence_identity"
        ],
    }


def _load_anchor(
    value: Mapping[str, object] | Path,
    *,
    expected_hash: str | None,
    ledger_root: Path,
) -> dict[str, object]:
    raw = _external_bytes(
        value,
        expected_hash=expected_hash,
        ledger_root=ledger_root,
        label="anchor",
    )
    parsed = _strict_json(raw, "external anchor")
    return _validate_anchor(parsed)


def _signature(value: object, label: str) -> Mapping[str, Any]:
    signature = _require_exact_fields(
        value, {"name", "account", "role", "signed_at"}, label
    )
    if not all(isinstance(signature[field], str) for field in ("name", "account", "role")):
        raise MilestoneRevocationIntegrityError(f"{label} identity fields are malformed")
    if signature["signed_at"] is not None and not isinstance(signature["signed_at"], str):
        raise MilestoneRevocationIntegrityError(f"{label} signed_at is malformed")
    return signature


def _validate_record_structure(record: object) -> Mapping[str, Any]:
    value = _require_exact_fields(
        record,
        {
            "schema",
            "original_record",
            "workspace_evidence",
            "conflict",
            "affected",
            "revoked_milestones",
            "governance",
            "disposition",
        },
        "milestone revocation record",
    )
    if value["schema"] != RECORD_SCHEMA:
        raise MilestoneRevocationIntegrityError("unsupported milestone revocation schema")
    original = _require_exact_fields(
        value["original_record"],
        {"record_id", "record_type", "milestone", "evaluation_result", "recorded_at"},
        "original record identity",
    )
    _require_hash(original["record_id"], "original record identity")
    if original["record_type"] not in {"acceptance", "readiness", "promotion"}:
        raise MilestoneRevocationIntegrityError("original record type is invalid")
    if original["milestone"] not in _MILESTONE_ORDER:
        raise MilestoneRevocationIntegrityError("original record milestone is invalid")
    if original["evaluation_result"] not in {"passed", "failed"}:
        raise MilestoneRevocationIntegrityError("original evaluation result is invalid")
    if _parse_utc(original["recorded_at"]) is None:
        raise MilestoneRevocationIntegrityError("original record timestamp is invalid")

    evidence = _require_exact_fields(
        value["workspace_evidence"],
        {
            "workspace_manifest_hash",
            "evidence_manifest_hash",
            "cohort_hash",
            "report_hash",
            "spec_hash",
        },
        "workspace/evidence binding",
    )
    for field, digest in evidence.items():
        _require_hash(digest, str(field))

    conflict = _require_exact_fields(
        value["conflict"],
        {"type", "authority_evidence_refs", "discovered_at", "effective_at"},
        "conflict",
    )
    if conflict["type"] not in _CONFLICT_TYPES:
        raise MilestoneRevocationIntegrityError("conflict type is invalid")
    refs = conflict["authority_evidence_refs"]
    if (
        not isinstance(refs, list)
        or not refs
        or len(set(refs)) != len(refs)
        or any(
            not isinstance(ref, str)
            or not ref.strip()
            or len(ref) > 2048
            or any(ord(character) < 32 for character in ref)
            for ref in refs
        )
    ):
        raise MilestoneRevocationIntegrityError("authority evidence refs are invalid")
    for field in ("discovered_at", "effective_at"):
        if conflict[field] is not None and not isinstance(conflict[field], str):
            raise MilestoneRevocationIntegrityError(f"conflict {field} is malformed")

    affected = _require_exact_fields(
        value["affected"], set(_AFFECTED_FIELDS) | {"sample_lineage"}, "affected identities"
    )
    affected_count = 0
    for field in _AFFECTED_FIELDS:
        keys = affected[field]
        if (
            not isinstance(keys, list)
            or len(keys) > 10_000
            or len(set(keys)) != len(keys)
            or any(not isinstance(key, str) or not key.strip() or len(key) > 512 for key in keys)
        ):
            raise MilestoneRevocationIntegrityError(f"affected {field} are invalid")
        affected_count += len(keys)
    lineage = affected["sample_lineage"]
    if not isinstance(lineage, list) or len(lineage) > 10_000:
        raise MilestoneRevocationIntegrityError("affected sample lineage is invalid")
    lineage_samples: set[str] = set()
    lineage_tuples: set[tuple[str, str, str, str]] = set()
    for item in lineage:
        entry = _require_exact_fields(
            item,
            {"sample_key", "settlement_key", "order_key", "decision_key"},
            "affected sample lineage entry",
        )
        values = tuple(entry[field] for field in (
            "sample_key", "settlement_key", "order_key", "decision_key"
        ))
        if any(
            not isinstance(key, str) or not key.strip() or len(key) > 512
            for key in values
        ) or values in lineage_tuples:
            raise MilestoneRevocationIntegrityError("affected sample lineage is invalid")
        lineage_tuples.add(values)
        lineage_samples.add(str(entry["sample_key"]))
    if lineage_samples != set(affected["sample_keys"]):
        raise MilestoneRevocationIntegrityError(
            "every affected sample requires explicit settlement/order/decision lineage"
        )
    if affected_count == 0:
        raise MilestoneRevocationIntegrityError("revocation must affect at least one identity")

    milestones = value["revoked_milestones"]
    expected_milestones = list(required_revoked_milestones(str(conflict["type"])))
    if milestones != expected_milestones:
        raise MilestoneRevocationIntegrityError(
            "revoked milestones do not contain the required dependency closure"
        )

    governance = _require_exact_fields(
        value["governance"],
        {"initiator", "independent_verifier", "approvers"},
        "governance",
    )
    _signature(governance["initiator"], "initiator")
    _signature(governance["independent_verifier"], "independent verifier")
    approvers = governance["approvers"]
    if not isinstance(approvers, list) or len(approvers) > 16:
        raise MilestoneRevocationIntegrityError("governance approvers are malformed")
    for index, approver in enumerate(approvers):
        _signature(approver, f"approver {index}")

    disposition = _require_exact_fields(
        value["disposition"], {"status", "reason", "decided_at"}, "disposition"
    )
    if disposition["status"] not in {"active", "review_required"}:
        raise MilestoneRevocationIntegrityError("disposition status is invalid")
    if not isinstance(disposition["reason"], str):
        raise MilestoneRevocationIntegrityError("disposition reason is malformed")
    if disposition["decided_at"] is not None and not isinstance(
        disposition["decided_at"], str
    ):
        raise MilestoneRevocationIntegrityError("disposition timestamp is malformed")
    return value


def _validate_binding(value: object) -> Mapping[str, Any]:
    binding = _require_exact_fields(
        value,
        {
            "schema",
            "bound_at",
            "database",
            "raw_root",
            "pair_manifest_hash",
            "p0_baseline_evidence_identity",
        },
        "ledger binding",
    )
    if binding["schema"] != BINDING_SCHEMA or _parse_utc(binding["bound_at"]) is None:
        raise MilestoneRevocationIntegrityError("ledger genesis binding is invalid")
    for label in ("database", "raw_root"):
        identity = _require_exact_fields(
            binding[label], {"resolved_path", "device", "inode"}, f"{label} identity"
        )
        if (
            not isinstance(identity["resolved_path"], str)
            or not Path(identity["resolved_path"]).is_absolute()
            or not isinstance(identity["device"], str)
            or not identity["device"].isdigit()
            or not isinstance(identity["inode"], str)
            or not identity["inode"].isdigit()
            or int(identity["inode"]) <= 0
        ):
            raise MilestoneRevocationIntegrityError(f"{label} identity is malformed")
    _require_hash(binding["pair_manifest_hash"], "binding pair manifest hash")
    _require_hash(
        binding["p0_baseline_evidence_identity"], "binding P0 baseline evidence identity"
    )
    return binding


def _governance_review_reasons(record: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    original = record["original_record"]
    conflict = record["conflict"]
    governance = record["governance"]
    disposition = record["disposition"]
    initiator = governance["initiator"]
    verifier = governance["independent_verifier"]
    approvers = governance["approvers"]
    discovered = _parse_utc(conflict["discovered_at"])
    effective = _parse_utc(conflict["effective_at"])
    decided = _parse_utc(disposition["decided_at"])
    recorded = _parse_utc(original["recorded_at"])

    if disposition["status"] != "active":
        reasons.append("disposition_not_active")
    if not str(disposition["reason"]).strip():
        reasons.append("disposition_reason_missing")
    if original["evaluation_result"] != "passed":
        reasons.append("original_record_not_passed")
    if discovered is None:
        reasons.append("discovered_at_missing_or_invalid")
    if effective is None:
        reasons.append("effective_at_missing_or_invalid")
    if decided is None:
        reasons.append("disposition_timestamp_missing_or_invalid")
    if discovered is not None and recorded is not None and discovered <= recorded:
        reasons.append("conflict_not_post_record")
    if effective is not None and recorded is not None and effective < recorded:
        reasons.append("effective_at_precedes_original_record")
    if effective is not None and discovered is not None and effective < discovered:
        reasons.append("effective_at_precedes_conflict_discovery")
    if decided is not None and recorded is not None and decided < recorded:
        reasons.append("disposition_precedes_original_record")
    if decided is not None and discovered is not None and decided < discovered:
        reasons.append("disposition_precedes_conflict_discovery")
    if decided is not None and effective is not None and decided < effective:
        reasons.append("disposition_precedes_effective_at")

    for label, signature in (("initiator", initiator), ("independent_verifier", verifier)):
        if not str(signature["name"]).strip() or not str(signature["account"]).strip():
            reasons.append(f"{label}_not_named")
        if signature["role"] not in _ROLE_NAMES:
            reasons.append(f"{label}_role_invalid")
        signed = _parse_utc(signature["signed_at"])
        if signed is None:
            reasons.append(f"{label}_timestamp_missing_or_invalid")
        else:
            if recorded is not None and signed < recorded:
                reasons.append(f"{label}_signature_precedes_original_record")
            if discovered is not None and signed < discovered:
                reasons.append(f"{label}_signature_precedes_conflict_discovery")
            if effective is not None and signed < effective:
                reasons.append(f"{label}_signature_precedes_effective_at")
            if decided is not None and signed > decided:
                reasons.append(f"{label}_signature_follows_disposition")
    if verifier["role"] != "independent_verifier":
        reasons.append("independent_verifier_role_required")
    if (
        str(initiator["name"]).strip()
        and str(initiator["name"]).casefold() == str(verifier["name"]).strip().casefold()
    ) or (
        str(initiator["account"]).strip()
        and str(initiator["account"]).casefold()
        == str(verifier["account"]).strip().casefold()
    ):
        reasons.append("separation_of_duties_violated")

    named_approvers = []
    for approver in approvers:
        signed = _parse_utc(approver["signed_at"])
        if signed is not None:
            if recorded is not None and signed < recorded:
                reasons.append("approver_signature_precedes_original_record")
            if discovered is not None and signed < discovered:
                reasons.append("approver_signature_precedes_conflict_discovery")
            if effective is not None and signed < effective:
                reasons.append("approver_signature_precedes_effective_at")
            if decided is not None and signed > decided:
                reasons.append("approver_signature_follows_disposition")
        if (
            str(approver["name"]).strip()
            and str(approver["account"]).strip()
            and approver["role"] in _ROLE_NAMES
            and _parse_utc(approver["signed_at"]) is not None
        ):
            named_approvers.append(approver)
    verifier_approved = any(
        approver["role"] == "independent_verifier"
        and str(approver["name"]).strip().casefold()
        == str(verifier["name"]).strip().casefold()
        and str(approver["account"]).strip().casefold()
        == str(verifier["account"]).strip().casefold()
        for approver in named_approvers
    )
    if not verifier_approved:
        reasons.append("independent_verifier_approval_missing")
    return list(dict.fromkeys(reasons))


def _conflict_fingerprint(record: Mapping[str, Any]) -> str:
    return _sha256(
        canonical_bytes(
            {
                "original_record_id": record["original_record"]["record_id"],
                "conflict_type": record["conflict"]["type"],
                "authority_evidence_refs": sorted(
                    record["conflict"]["authority_evidence_refs"]
                ),
                "affected": {
                    field: sorted(record["affected"][field])
                    for field in _AFFECTED_FIELDS
                },
            }
        )
    )


def _read_ledger(
    root: Path,
) -> tuple[
    Mapping[str, Any],
    list[tuple[str, Mapping[str, Any]]],
    str,
    int,
    str,
]:
    candidate_root = Path(root)
    if candidate_root.is_symlink():
        raise MilestoneRevocationIntegrityError("ledger root is missing or unsafe")
    root = candidate_root.resolve(strict=True)
    if not root.is_dir():
        raise MilestoneRevocationIntegrityError("ledger root is missing or unsafe")
    actual_entries = {path.name for path in root.iterdir()}
    allowed_entries = _LEDGER_FIXED_ENTRIES | {"transaction.json", "transaction.returned"}
    if not _LEDGER_FIXED_ENTRIES.issubset(actual_entries) or not actual_entries <= allowed_entries:
        raise MilestoneRevocationIntegrityError("ledger root contains unexpected entries")
    object_root = root / "objects"
    seal_root = root / "seals"
    index_path = root / "index.jsonl"
    if (
        object_root.is_symlink()
        or seal_root.is_symlink()
        or index_path.is_symlink()
        or not object_root.is_dir()
        or not seal_root.is_dir()
        or not index_path.is_file()
    ):
        raise MilestoneRevocationIntegrityError("ledger layout is missing or unsafe")
    _require_regular_file(index_path, "ledger index")
    _require_regular_file(root / "ledger.lock", "ledger lock")
    raw_index = index_path.read_bytes()
    if not raw_index or not raw_index.endswith(b"\n"):
        raise MilestoneRevocationIntegrityError("ledger index is empty or truncated")
    lines = raw_index.splitlines()
    previous = _ZERO_HASH
    referenced: set[str] = set()
    records: list[tuple[str, Mapping[str, Any]]] = []
    binding: Mapping[str, Any] | None = None
    expected_seals: set[str] = set()
    conflict_fingerprints: set[str] = set()
    original_identities: dict[str, dict[str, object]] = {}
    genesis_entry_hash: str | None = None
    for expected_sequence, raw_entry in enumerate(lines, start=1):
        entry = _strict_json(raw_entry, "ledger index entry")
        entry = _require_exact_fields(
            entry,
            {"schema", "sequence", "previous_entry_hash", "object_hash", "object_type"},
            "ledger index entry",
        )
        if (
            entry["schema"] != INDEX_SCHEMA
            or entry["sequence"] != expected_sequence
            or entry["previous_entry_hash"] != previous
            or entry["object_type"] not in {"binding", "revocation"}
        ):
            raise MilestoneRevocationIntegrityError("ledger index sequence/hash chain is invalid")
        object_hash = entry["object_hash"]
        _require_hash(object_hash, "ledger object hash")
        if object_hash in referenced:
            raise MilestoneRevocationIntegrityError("ledger contains a duplicate object reference")
        referenced.add(str(object_hash))
        entry_hash = _sha256(raw_entry)
        if expected_sequence == 1:
            genesis_entry_hash = entry_hash
        expected_seals.add(f"{expected_sequence:020d}-{entry_hash}.json")
        previous = entry_hash
        object_path = object_root / f"{object_hash}.json"
        _require_regular_file(object_path, "referenced ledger object")
        object_bytes = object_path.read_bytes()
        if _sha256(object_bytes) != object_hash:
            raise MilestoneRevocationIntegrityError("ledger object hash mismatch")
        value = _strict_json(object_bytes, "ledger object")
        if expected_sequence == 1:
            binding = _validate_binding(value)
            if entry["object_type"] != "binding":
                raise MilestoneRevocationIntegrityError("ledger genesis binding is invalid")
        else:
            if entry["object_type"] != "revocation":
                raise MilestoneRevocationIntegrityError("non-genesis binding is forbidden")
            record = _validate_record_structure(value)
            fingerprint = _conflict_fingerprint(record)
            if fingerprint in conflict_fingerprints:
                raise MilestoneRevocationIntegrityError("duplicate revocation conflict is forbidden")
            conflict_fingerprints.add(fingerprint)
            original = dict(record["original_record"])
            original_id = str(original["record_id"])
            prior_original = original_identities.setdefault(original_id, original)
            if prior_original != original:
                raise MilestoneRevocationIntegrityError(
                    "original milestone record identity conflicts with an earlier binding"
                )
            records.append((str(object_hash), record))
    assert binding is not None and genesis_entry_hash is not None

    actual_objects = {path.name for path in object_root.iterdir()}
    if actual_objects != {f"{digest}.json" for digest in referenced}:
        raise MilestoneRevocationIntegrityError("ledger contains missing or unreferenced objects")
    actual_seals = {path.name for path in seal_root.iterdir()}
    if actual_seals != expected_seals:
        raise MilestoneRevocationIntegrityError("ledger index seals prove deletion or truncation")
    for name in actual_seals:
        match = _SEAL_RE.fullmatch(name)
        if match is None:
            raise MilestoneRevocationIntegrityError("ledger contains malformed seal name")
        sequence = int(match.group("sequence"))
        seal_path = seal_root / name
        _require_regular_file(seal_path, "ledger seal")
        seal_bytes = seal_path.read_bytes()
        if seal_bytes != lines[sequence - 1] or _sha256(seal_bytes) != match.group("digest"):
            raise MilestoneRevocationIntegrityError("ledger index seal mismatch")
    return binding, records, previous, len(lines), genesis_entry_hash


def _write_transaction(root: Path, transaction: Mapping[str, object]) -> bytes:
    raw = canonical_bytes(dict(transaction))
    _write_immutable(root / "transaction.json", raw)
    _fsync_directory(root)
    return raw


def _read_transaction(root: Path) -> tuple[Mapping[str, Any], bytes] | None:
    path = root / "transaction.json"
    if not path.exists():
        if (root / "transaction.returned").exists():
            raise MilestoneRevocationIntegrityError("ledger transaction receipt is orphaned")
        return None
    _require_regular_file(path, "ledger transaction journal")
    raw = path.read_bytes()
    value = _require_exact_fields(
        _strict_json(raw, "ledger transaction journal"),
        {
            "schema",
            "old_anchor",
            "new_anchor",
            "old_index_size",
            "object_hash",
            "object_bytes_hex",
            "seal_name",
            "entry_bytes_hex",
            "sequence",
        },
        "ledger transaction journal",
    )
    if value["schema"] != TRANSACTION_SCHEMA:
        raise MilestoneRevocationIntegrityError("ledger transaction schema is invalid")
    old_anchor = _validate_anchor(value["old_anchor"])
    new_anchor = _validate_anchor(value["new_anchor"])
    if (
        type(value["old_index_size"]) is not int
        or value["old_index_size"] < 1
        or type(value["sequence"]) is not int
        or value["sequence"] != old_anchor["sequence"] + 1
        or new_anchor["sequence"] != value["sequence"]
    ):
        raise MilestoneRevocationIntegrityError("ledger transaction sequence is invalid")
    _require_hash(value["object_hash"], "transaction object hash")
    for field in ("object_bytes_hex", "entry_bytes_hex", "seal_name"):
        if not isinstance(value[field], str):
            raise MilestoneRevocationIntegrityError("ledger transaction fields are malformed")
    try:
        object_bytes = bytes.fromhex(value["object_bytes_hex"])
        entry_bytes = bytes.fromhex(value["entry_bytes_hex"])
    except ValueError as error:
        raise MilestoneRevocationIntegrityError("ledger transaction bytes are malformed") from error
    if (
        _sha256(object_bytes) != value["object_hash"]
        or _sha256(entry_bytes) != new_anchor["head_entry_hash"]
        or value["seal_name"]
        != f"{value['sequence']:020d}-{new_anchor['head_entry_hash']}.json"
    ):
        raise MilestoneRevocationIntegrityError("ledger transaction hashes are invalid")
    return value, raw


def _remove_transaction(root: Path) -> None:
    for name in ("transaction.returned", "transaction.json"):
        path = root / name
        if path.exists():
            _require_regular_file(path, f"ledger {name}")
            path.unlink()
    _fsync_directory(root)


def _rollback_transaction(root: Path, transaction: Mapping[str, Any]) -> None:
    index_path = root / "index.jsonl"
    _require_regular_file(index_path, "ledger index")
    old_size = int(transaction["old_index_size"])
    current = index_path.read_bytes()
    entry_bytes = bytes.fromhex(str(transaction["entry_bytes_hex"])) + b"\n"
    if len(current) < old_size or current[old_size:] not in {b"", entry_bytes}:
        raise MilestoneRevocationIntegrityError("ledger transaction index is unrecoverable")
    if len(current) != old_size:
        with index_path.open("r+b") as stream:
            stream.truncate(old_size)
            stream.flush()
            os.fsync(stream.fileno())
    for path in (
        root / "seals" / str(transaction["seal_name"]),
        root / "objects" / f"{transaction['object_hash']}.json",
    ):
        if path.exists():
            _require_regular_file(path, "ledger transaction artifact")
            path.unlink()
    _remove_transaction(root)


def _complete_transaction(root: Path, transaction: Mapping[str, Any]) -> None:
    object_bytes = bytes.fromhex(str(transaction["object_bytes_hex"]))
    entry_bytes = bytes.fromhex(str(transaction["entry_bytes_hex"]))
    object_path = root / "objects" / f"{transaction['object_hash']}.json"
    seal_path = root / "seals" / str(transaction["seal_name"])
    _write_immutable(object_path, object_bytes)
    _write_immutable(seal_path, entry_bytes)
    index_path = root / "index.jsonl"
    _require_regular_file(index_path, "ledger index")
    old_size = int(transaction["old_index_size"])
    current = index_path.read_bytes()
    suffix = entry_bytes + b"\n"
    if len(current) == old_size:
        with index_path.open("ab") as stream:
            stream.write(suffix)
            stream.flush()
            os.fsync(stream.fileno())
    elif len(current) != old_size + len(suffix) or current[old_size:] != suffix:
        raise MilestoneRevocationIntegrityError("ledger transaction index is unrecoverable")


def _recover_transaction(root: Path, expected_anchor: Mapping[str, object]) -> None:
    loaded = _read_transaction(root)
    if loaded is None:
        return
    transaction, raw = loaded
    old_anchor = dict(transaction["old_anchor"])
    new_anchor = dict(transaction["new_anchor"])
    returned_path = root / "transaction.returned"
    returned = returned_path.exists()
    if returned:
        _require_regular_file(returned_path, "ledger transaction receipt")
        if returned_path.read_bytes() != _sha256(raw).encode("ascii"):
            raise MilestoneRevocationIntegrityError("ledger transaction receipt is invalid")
    if dict(expected_anchor) == new_anchor:
        if returned:
            binding, _, head_hash, entry_count, genesis_hash = _read_ledger(root)
            actual = _anchor_for(
                binding=binding,
                head_hash=head_hash,
                sequence=entry_count,
                genesis_entry_hash=genesis_hash,
            )
            if actual != new_anchor:
                raise MilestoneRevocationIntegrityError(
                    "returned ledger transaction does not match its external anchor"
                )
        else:
            _complete_transaction(root, transaction)
        _remove_transaction(root)
        return
    if dict(expected_anchor) == old_anchor and not returned:
        _rollback_transaction(root, transaction)
        return
    raise MilestoneRevocationIntegrityError("stale external anchor for ledger transaction")


def _verify_pair(
    binding: Mapping[str, Any], database_path: Path, raw_root: Path
) -> None:
    expected_database = _path_identity(database_path, directory=False)
    expected_raw = _path_identity(raw_root, directory=True)
    if binding["database"] != expected_database or binding["raw_root"] != expected_raw:
        raise MilestoneRevocationIntegrityError(
            "milestone revocation ledger database/raw pair identity mismatch"
        )


def _configured_projection(
    binding: Mapping[str, Any],
    records: Sequence[tuple[str, Mapping[str, Any]]],
    head_hash: str,
    entry_count: int,
) -> dict[str, object]:
    projected_records = []
    isolated = {field: set() for field in _AFFECTED_FIELDS}
    revoked: set[str] = set()
    review_required: set[str] = set()
    for record_id, record in records:
        reasons = _governance_review_reasons(record)
        active = not reasons
        for field in _AFFECTED_FIELDS:
            isolated[field].update(record["affected"][field])
        for lineage in record["affected"]["sample_lineage"]:
            isolated["settlement_keys"].add(lineage["settlement_key"])
            isolated["order_keys"].add(lineage["order_key"])
            isolated["decision_keys"].add(lineage["decision_key"])
        target = revoked if active else review_required
        target.update(record["revoked_milestones"])
        projected_records.append(
            {
                "record_id": record_id,
                "conflict_fingerprint": _conflict_fingerprint(record),
                "original_record": dict(record["original_record"]),
                "evaluation_result": record["original_record"]["evaluation_result"],
                "governance_status": "revoked" if active else "review_required",
                "revocation_status": "active" if active else "review_required",
                "review_reasons": reasons,
                "conflict": dict(record["conflict"]),
                "affected": dict(record["affected"]),
                "revoked_milestones": list(record["revoked_milestones"]),
                "workspace_evidence": dict(record["workspace_evidence"]),
                "governance": dict(record["governance"]),
                "disposition": dict(record["disposition"]),
            }
        )
    has_review = bool(review_required)
    return {
        "status": "revoked" if revoked else "review_required" if has_review else "active",
        "governance_status": "revoked" if revoked else "active",
        "ledger_integrity": {
            "status": "verified",
            "entry_count": entry_count,
            "revocation_record_count": len(records),
            "head_entry_hash": head_hash,
        },
        "pair_identity": {
            "database": dict(binding["database"]),
            "raw_root": dict(binding["raw_root"]),
            "pair_manifest_hash": binding["pair_manifest_hash"],
            "p0_baseline_evidence_identity": binding[
                "p0_baseline_evidence_identity"
            ],
        },
        "records": projected_records,
        "isolated_keys": {
            field: sorted(values) for field, values in isolated.items()
        },
        "revoked_milestones": [item for item in _MILESTONE_ORDER if item in revoked],
        "review_required_milestones": [
            item for item in _MILESTONE_ORDER if item in review_required
        ],
        "requires_new_cutoff_manifest_report_record": bool(records),
    }


def not_configured_milestone_revocation_projection() -> dict[str, object]:
    return {
        "status": "not_configured",
        "governance_status": "active",
        "ledger_integrity": {"status": "not_configured"},
        "pair_identity": None,
        "records": [],
        "isolated_keys": {field: [] for field in _AFFECTED_FIELDS},
        "revoked_milestones": [],
        "review_required_milestones": [],
        "requires_new_cutoff_manifest_report_record": False,
    }


def load_milestone_revocation_projection(
    root: Path | None = None,
    *,
    database_path: Path | None = None,
    raw_root: Path | None = None,
    connection: Any | None = None,
    expected_anchor: Mapping[str, object] | Path | None = None,
    expected_anchor_hash: str | None = None,
    pair_manifest: bytes | Mapping[str, object] | Path | None = None,
    expected_pair_manifest_hash: str | None = None,
    config: MilestoneRevocationConfig | None = None,
) -> dict[str, object]:
    """Verify and project a configured ledger; never guess a production path."""

    supplied = {
        "root": root,
        "database_path": database_path,
        "raw_root": raw_root,
        "expected_anchor": expected_anchor,
        "pair_manifest": pair_manifest,
        "expected_pair_manifest_hash": expected_pair_manifest_hash,
    }
    if config is not None:
        if any(value is not None for value in supplied.values()) or expected_anchor_hash is not None:
            raise ValueError("revocation config cannot be combined with individual fields")
        root = config.root
        database_path = config.database_path
        raw_root = config.raw_root
        expected_anchor = config.expected_anchor
        expected_anchor_hash = config.expected_anchor_hash
        pair_manifest = config.pair_manifest
        expected_pair_manifest_hash = config.expected_pair_manifest_hash
    if root is None:
        if any(value is not None for key, value in supplied.items() if key != "root"):
            raise ValueError("revocation fields cannot be supplied without a ledger")
        return not_configured_milestone_revocation_projection()
    if (
        database_path is None
        or raw_root is None
        or expected_anchor is None
        or pair_manifest is None
        or expected_pair_manifest_hash is None
    ):
        raise MilestoneRevocationIntegrityError(
            "configured ledger requires explicit database/raw pair, external anchor, "
            "and pair baseline manifest"
        )
    root = Path(root)
    if root.is_symlink():
        raise MilestoneRevocationIntegrityError("ledger root is missing or unsafe")
    database_path = Path(database_path)
    raw_root = Path(raw_root)
    if connection is not None:
        _require_connection_database(connection, database_path)
    anchor = _load_anchor(
        expected_anchor,
        expected_hash=expected_anchor_hash,
        ledger_root=root,
    )
    manifest, _ = _load_pair_manifest(
        pair_manifest,
        expected_hash=expected_pair_manifest_hash,
        ledger_root=root,
    )
    with _ledger_lock(root):
        _recover_transaction(root, anchor)
        binding, records, head_hash, entry_count, genesis_hash = _read_ledger(root)
        _verify_pair(binding, database_path, raw_root)
        if (
            binding["pair_manifest_hash"] != expected_pair_manifest_hash
            or binding["p0_baseline_evidence_identity"]
            != manifest["p0_baseline_evidence_identity"]
        ):
            raise MilestoneRevocationIntegrityError("ledger pair baseline identity mismatch")
        actual_anchor = _anchor_for(
            binding=binding,
            head_hash=head_hash,
            sequence=entry_count,
            genesis_entry_hash=genesis_hash,
        )
        if actual_anchor != anchor:
            raise MilestoneRevocationIntegrityError(
                "ledger head does not match the external anchor"
            )
        _verify_pair_manifest(manifest, database_path, raw_root)
        _verify_record_lineage(
            [record for _, record in records],
            database_path,
            connection=connection,
        )
        return _configured_projection(binding, records, head_hash, entry_count)


def append_milestone_revocation(
    root: Path,
    record: Mapping[str, Any],
    *,
    database_path: Path,
    raw_root: Path,
    expected_anchor: Mapping[str, object] | Path,
    pair_manifest: bytes | Mapping[str, object] | Path,
    expected_pair_manifest_hash: str,
    expected_anchor_hash: str | None = None,
    _crash_at: str | None = None,
) -> dict[str, object]:
    """CAS-append one record and return the new caller-owned external anchor."""

    value = _validate_record_structure(record)
    root = Path(root)
    anchor = _load_anchor(
        expected_anchor,
        expected_hash=expected_anchor_hash,
        ledger_root=root,
    )
    manifest, _ = _load_pair_manifest(
        pair_manifest,
        expected_hash=expected_pair_manifest_hash,
        ledger_root=root,
    )
    with _ledger_lock(root):
        _recover_transaction(root, anchor)
        binding, records, head_hash, entry_count, genesis_hash = _read_ledger(root)
        _verify_pair(binding, Path(database_path), Path(raw_root))
        actual_anchor = _anchor_for(
            binding=binding,
            head_hash=head_hash,
            sequence=entry_count,
            genesis_entry_hash=genesis_hash,
        )
        if actual_anchor != anchor:
            raise MilestoneRevocationIntegrityError("stale external anchor for append")
        if (
            binding["pair_manifest_hash"] != expected_pair_manifest_hash
            or binding["p0_baseline_evidence_identity"]
            != manifest["p0_baseline_evidence_identity"]
        ):
            raise MilestoneRevocationIntegrityError("ledger pair baseline identity mismatch")
        _verify_pair_manifest(manifest, Path(database_path), Path(raw_root))
        _verify_record_lineage(
            [*(existing for _, existing in records), value],
            Path(database_path),
        )
        fingerprint = _conflict_fingerprint(value)
        if any(_conflict_fingerprint(existing) == fingerprint for _, existing in records):
            raise MilestoneRevocationIntegrityError("duplicate revocation conflict is forbidden")
        for _, existing in records:
            if (
                existing["original_record"]["record_id"]
                == value["original_record"]["record_id"]
                and existing["original_record"] != value["original_record"]
            ):
                raise MilestoneRevocationIntegrityError(
                    "original milestone record identity conflicts with an earlier binding"
                )
        object_bytes = canonical_bytes(value)
        object_hash = _sha256(object_bytes)
        object_path = root / "objects" / f"{object_hash}.json"
        if object_path.exists():
            raise MilestoneRevocationIntegrityError("duplicate revocation object is forbidden")
        sequence = entry_count + 1
        _, entry_bytes, entry_hash = _index_entry(
            sequence=sequence,
            previous_entry_hash=head_hash,
            object_hash=object_hash,
            object_type="revocation",
        )
        new_anchor = _anchor_for(
            binding=binding,
            head_hash=entry_hash,
            sequence=sequence,
            genesis_entry_hash=genesis_hash,
        )
        transaction = {
            "schema": TRANSACTION_SCHEMA,
            "old_anchor": anchor,
            "new_anchor": new_anchor,
            "old_index_size": (root / "index.jsonl").stat().st_size,
            "object_hash": object_hash,
            "object_bytes_hex": object_bytes.hex(),
            "seal_name": f"{sequence:020d}-{entry_hash}.json",
            "entry_bytes_hex": entry_bytes.hex(),
            "sequence": sequence,
        }
        transaction_raw = _write_transaction(root, transaction)
        _write_immutable(object_path, object_bytes)
        if _crash_at == "object_only":
            raise RuntimeError("simulated append crash: object_only")
        _write_immutable(root / "seals" / str(transaction["seal_name"]), entry_bytes)
        if _crash_at == "seal_only":
            raise RuntimeError("simulated append crash: seal_only")
        _append_index(root, entry_bytes, entry_hash, sequence)
        if _crash_at == "index_only":
            raise RuntimeError("simulated append crash: index_only")
        _write_immutable(
            root / "transaction.returned", _sha256(transaction_raw).encode("ascii")
        )
        _fsync_directory(root)
        return new_anchor


__all__ = [
    "MilestoneRevocationIntegrityError",
    "MilestoneRevocationConfig",
    "PAIR_BASELINE_SCHEMA",
    "RECORD_SCHEMA",
    "append_milestone_revocation",
    "canonical_bytes",
    "create_pair_baseline_manifest",
    "initialize_milestone_revocation_ledger",
    "load_milestone_revocation_projection",
    "not_configured_milestone_revocation_projection",
    "required_revoked_milestones",
]
