"""Supervisor-owned schema preflight and online-backup protocol."""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from event_intelligence.storage import (
    CURRENT_SCHEMA_VERSION as INTELLIGENCE_SCHEMA_VERSION,
    IntelligenceStorage,
)
from fetch.db import Database
from shared.sqlite import connect

from .storage import CURRENT_SCHEMA_VERSION as LIVE_SCHEMA_VERSION
from .storage import LiveBettingStore


@dataclass(frozen=True)
class DatabasePreparation:
    database: Path
    backup: Path | None
    live_schema_version: int
    intelligence_schema_version: int


@dataclass(frozen=True)
class _ColumnContract:
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_position: int
    hidden: int


@dataclass(frozen=True)
class _SchemaContract:
    tables: dict[str, dict[str, _ColumnContract]]
    checks: dict[str, tuple[str, ...]]
    foreign_keys: dict[str, frozenset[tuple[str, str, str, str, str, str]]]
    indexes: dict[str, tuple[str, int, int, tuple[str, ...]]]
    triggers: dict[str, str]
    views: dict[str, str]


_SQL_TOKEN = re.compile(
    r"""
    '(?:''|[^'])*'
    | "(?:""|[^"])*"
    | `(?:``|[^`])*`
    | \[(?:\]\]|[^\]])*\]
    | [A-Za-z_][A-Za-z0-9_$]*
    | (?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?
    | ->>|->|<=|>=|<>|!=|==|\|\|
    | [^\s]
    """,
    re.VERBOSE,
)
# SQLite cannot add NOT NULL to populated tables without rebuilding them. These
# columns retain their additive-migration shape and pair nullability with the
# recorded lineage checks used by current readers.
_NULLABLE_MIGRATION_COLUMNS = frozenset(
    {
        ("draft_prediction_validations", "artifact_fingerprint"),
        ("draft_prediction_validations", "dependency_revision"),
        ("strict_derived_status", "benchmark_version"),
        ("strict_derived_status", "normalizer_version"),
        ("strict_derived_status", "profile_context_hash"),
    }
)
_DEFAULT_MIGRATION_COLUMNS = {
    ("match_players", "firstblood_claimed"): "0",
}


def _schema_version(
    connection: sqlite3.Connection,
    table: str,
    supported: int,
) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute(f"SELECT MAX(version) FROM {table}").fetchone()
    version = 0 if row is None or row[0] is None else int(row[0])
    if version > supported:
        raise RuntimeError(
            f"database {table} version {version} is newer than supported "
            f"version {supported}"
        )
    return version


def check_schema_versions(database: Path) -> tuple[int, int]:
    """Reject a future database before any migration or backup mutation."""

    database = database.resolve()
    if not database.exists() or database.stat().st_size == 0:
        return 0, 0
    connection = connect(database, read_only=True)
    try:
        live = _schema_version(connection, "live_schema_version", LIVE_SCHEMA_VERSION)
        intelligence = _schema_version(
            connection,
            "intelligence_schema_version",
            INTELLIGENCE_SCHEMA_VERSION,
        )
        return live, intelligence
    finally:
        connection.close()


def _sql_tokens(value: object) -> tuple[str, ...]:
    tokens = []
    for match in _SQL_TOKEN.finditer(str(value or "")):
        token = match.group(0)
        tokens.append(token if token.startswith("'") else token.casefold())
    return tuple(tokens)


def _strip_outer_parentheses(tokens: tuple[str, ...]) -> tuple[str, ...]:
    while len(tokens) >= 2 and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        closes_at_end = False
        for index, token in enumerate(tokens):
            if token == "(":
                depth += 1
            elif token == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = index == len(tokens) - 1
                    break
        if not closes_at_end:
            break
        tokens = tokens[1:-1]
    return tokens


def _normalize_sql(value: object) -> str:
    return " ".join(_sql_tokens(value))


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(_strip_outer_parentheses(_sql_tokens(value)))


def _canonical_check(
    table: str,
    tokens: tuple[str, ...],
) -> tuple[str, ...]:
    tokens = _strip_outer_parentheses(tokens)
    for migration_table, column in _NULLABLE_MIGRATION_COLUMNS:
        nullable_prefix = (column, "is", "null", "or")
        if table == migration_table and tokens[:4] == nullable_prefix:
            return _strip_outer_parentheses(tokens[4:])
    return tokens


def _check_contract(table: str, sql: object) -> tuple[str, ...]:
    tokens = _sql_tokens(sql)
    checks: list[str] = []
    index = 0
    while index < len(tokens):
        if tokens[index] != "check" or index + 1 >= len(tokens):
            index += 1
            continue
        if tokens[index + 1] != "(":
            index += 1
            continue
        depth = 1
        cursor = index + 2
        while cursor < len(tokens) and depth:
            if tokens[cursor] == "(":
                depth += 1
            elif tokens[cursor] == ")":
                depth -= 1
            cursor += 1
        if depth:
            raise RuntimeError(f"unbalanced CHECK constraint in table {table}")
        expression = _canonical_check(table, tokens[index + 2 : cursor - 1])
        checks.append(" ".join(expression))
        index = cursor
    return tuple(sorted(checks))


def _column_contract_matches(
    table: str,
    column: str,
    expected: _ColumnContract,
    actual: _ColumnContract,
) -> bool:
    if actual == expected:
        return True
    key = (table, column)
    if key in _NULLABLE_MIGRATION_COLUMNS:
        if actual == replace(expected, not_null=False):
            return True
    migrated_default = _DEFAULT_MIGRATION_COLUMNS.get(key)
    return migrated_default is not None and actual == replace(
        expected,
        default_sql=migrated_default,
    )


def _schema_contract(connection: sqlite3.Connection) -> _SchemaContract:
    objects = connection.execute(
        """SELECT type, name, tbl_name, sql
             FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
              AND type IN ('table', 'index', 'trigger', 'view')"""
    ).fetchall()
    tables: dict[str, dict[str, _ColumnContract]] = {}
    checks: dict[str, tuple[str, ...]] = {}
    foreign_keys: dict[
        str, frozenset[tuple[str, str, str, str, str, str]]
    ] = {}
    indexes: dict[str, tuple[str, int, int, tuple[str, ...]]] = {}
    triggers: dict[str, str] = {}
    views: dict[str, str] = {}
    for object_type, name, table_name, sql in objects:
        object_type = str(object_type)
        name = str(name)
        table_name = str(table_name)
        if object_type == "table":
            tables[name] = {
                str(row[1]): _ColumnContract(
                    declared_type=_normalize_sql(row[2]),
                    not_null=bool(row[3]),
                    default_sql=_normalize_default(row[4]),
                    primary_key_position=int(row[5]),
                    hidden=int(row[6]),
                )
                for row in connection.execute(f'PRAGMA table_xinfo("{name}")')
            }
            checks[name] = _check_contract(name, sql)
            foreign_keys[name] = frozenset(
                (
                    str(row[3]),
                    str(row[4]),
                    str(row[2]),
                    str(row[5]),
                    str(row[6]),
                    str(row[7]),
                )
                for row in connection.execute(f'PRAGMA foreign_key_list("{name}")')
            )
        elif object_type == "index":
            index_row = next(
                (
                    row
                    for row in connection.execute(
                        f'PRAGMA index_list("{table_name}")'
                    )
                    if str(row[1]) == name
                ),
                None,
            )
            if index_row is not None:
                indexes[name] = (
                    table_name,
                    int(index_row[2]),
                    int(index_row[4]),
                    tuple(
                        "" if row[2] is None else str(row[2])
                        for row in connection.execute(f'PRAGMA index_info("{name}")')
                    ),
                )
        elif object_type == "trigger":
            triggers[name] = _normalize_sql(sql)
        elif object_type == "view":
            views[name] = _normalize_sql(sql)
    return _SchemaContract(tables, checks, foreign_keys, indexes, triggers, views)


@lru_cache(maxsize=1)
def _expected_schema_contract() -> _SchemaContract:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        database = Path(":memory:")
        with LiveBettingStore(database, connection=connection) as live:
            live.init_schema()
        with IntelligenceStorage(database, connection=connection) as intelligence:
            intelligence.init_schema(seed_events=False)
            Database(connection=intelligence.connection).init_db()
        return _schema_contract(connection)
    finally:
        connection.close()


def _schema_contract_errors(
    expected: _SchemaContract,
    actual: _SchemaContract,
) -> list[str]:
    errors: list[str] = []
    missing_tables = sorted(expected.tables.keys() - actual.tables.keys())
    if missing_tables:
        errors.append("missing tables: " + ", ".join(missing_tables))
    for table in sorted(expected.tables.keys() & actual.tables.keys()):
        expected_columns = expected.tables[table]
        actual_columns = actual.tables[table]
        missing_columns = sorted(expected_columns.keys() - actual_columns.keys())
        if missing_columns:
            errors.append(
                f"{table} missing columns: " + ", ".join(missing_columns)
            )
        mismatched_columns = sorted(
            column
            for column in expected_columns.keys() & actual_columns.keys()
            if not _column_contract_matches(
                table,
                column,
                expected_columns[column],
                actual_columns[column],
            )
        )
        if mismatched_columns:
            errors.append(
                f"{table} column constraint mismatch: "
                + ", ".join(mismatched_columns)
            )
        if expected.checks.get(table, ()) != actual.checks.get(table, ()):
            errors.append(f"{table} check constraint mismatch")
        missing_foreign_keys = expected.foreign_keys.get(
            table, frozenset()
        ) - actual.foreign_keys.get(table, frozenset())
        if missing_foreign_keys:
            errors.append(f"{table} foreign-key contract mismatch")
    for label, expected_objects, actual_objects in (
        ("indexes", expected.indexes, actual.indexes),
        ("triggers", expected.triggers, actual.triggers),
        ("views", expected.views, actual.views),
    ):
        missing = sorted(expected_objects.keys() - actual_objects.keys())
        if missing:
            errors.append(f"missing {label}: " + ", ".join(missing))
        mismatched = sorted(
            name
            for name in expected_objects.keys() & actual_objects.keys()
            if expected_objects[name] != actual_objects[name]
        )
        if mismatched:
            errors.append(f"mismatched {label}: " + ", ".join(mismatched))
    return errors


def verify_prepared_database(database: Path) -> DatabasePreparation:
    """Verify a current service schema without taking a backup or mutating it."""

    database = database.resolve()
    live_version, intelligence_version = check_schema_versions(database)
    if (
        live_version != LIVE_SCHEMA_VERSION
        or intelligence_version != INTELLIGENCE_SCHEMA_VERSION
    ):
        raise RuntimeError(
            "database schema is not prepared: "
            f"live={live_version}/{LIVE_SCHEMA_VERSION}, "
            f"intelligence={intelligence_version}/{INTELLIGENCE_SCHEMA_VERSION}; "
            "run the supervisor with --migrate"
        )
    connection = connect(database, read_only=True)
    try:
        errors = _schema_contract_errors(
            _expected_schema_contract(),
            _schema_contract(connection),
        )
        if errors:
            visible = errors[:20]
            suffix = f"; plus {len(errors) - 20} more" if len(errors) > 20 else ""
            raise RuntimeError(
                "prepared database schema contract failed: "
                + "; ".join(visible)
                + suffix
            )
    finally:
        connection.close()
    return DatabasePreparation(
        database,
        None,
        live_version,
        intelligence_version,
    )


def _backup_connection(
    source: sqlite3.Connection,
    destination: Path,
) -> None:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"backup already exists: {destination}")
    page_count = int(source.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(source.execute("PRAGMA page_size").fetchone()[0])
    required_bytes = page_count * page_size
    available_bytes = shutil.disk_usage(destination.parent).free
    if available_bytes < required_bytes:
        raise RuntimeError(
            "insufficient free space for SQLite backup: "
            f"destination={destination}, required_bytes={required_bytes}, "
            f"available_bytes={available_bytes}; choose a backup directory "
            "on a volume with more free space"
        )
    target: sqlite3.Connection | None = None
    try:
        target = connect(destination)
        source.backup(target)
        target.commit()
        quick_check = target.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]) != "ok":
            raise RuntimeError("online backup failed SQLite quick_check")
    except BaseException:
        if target is not None:
            target.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()


def online_backup(database: Path, destination: Path) -> None:
    """Take a consistent SQLite snapshot, including committed WAL contents."""

    source = connect(database.resolve(), read_only=True)
    try:
        _backup_connection(source, destination)
    finally:
        source.close()


def _acquire_exclusive(connection: sqlite3.Connection, operation: str) -> None:
    mode = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
    if mode is None or str(mode[0]).casefold() != "exclusive":
        raise RuntimeError(f"failed to acquire exclusive {operation} mode")
    connection.execute("BEGIN EXCLUSIVE")
    connection.commit()


def _restore_connection(backup: Path, target: sqlite3.Connection) -> None:
    source = connect(backup, read_only=True)
    try:
        source.backup(target)
        target.commit()
        quick_check = target.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or str(quick_check[0]) != "ok":
            raise RuntimeError("restored database failed SQLite quick_check")
    finally:
        source.close()


def _restore_online_backup(backup: Path, database: Path) -> None:
    target = connect(database)
    try:
        _acquire_exclusive(target, "restore")
        _restore_connection(backup, target)
    finally:
        target.close()


def restore_database_backup(
    backup: Path,
    database: Path,
    *,
    safety_backup: Path,
) -> Path | None:
    """Restore through SQLite after preserving the current target online."""

    backup = backup.resolve()
    database = database.resolve()
    safety_backup = safety_backup.resolve()
    if len({backup, database, safety_backup}) != 3:
        raise ValueError("backup, database, and safety_backup must be distinct paths")
    if not backup.is_file():
        raise FileNotFoundError(f"backup does not exist: {backup}")
    backup_versions = check_schema_versions(backup)
    saved: Path | None = None
    target = connect(database)
    try:
        _acquire_exclusive(target, "restore")
        _schema_version(target, "live_schema_version", LIVE_SCHEMA_VERSION)
        _schema_version(
            target,
            "intelligence_schema_version",
            INTELLIGENCE_SCHEMA_VERSION,
        )
        target_has_schema = target.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') LIMIT 1"
        ).fetchone()
        if target_has_schema is not None:
            _backup_connection(target, safety_backup)
            saved = safety_backup
        _restore_connection(backup, target)
    finally:
        target.close()
    if check_schema_versions(database) != backup_versions:
        raise RuntimeError("restored schema versions do not match the source backup")
    return saved


def _backup_path(database: Path, backup_dir: Path, now: datetime) -> Path:
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return backup_dir.resolve() / f"{database.stem}-before-service-{timestamp}.db"


def prepare_database(
    database: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
) -> DatabasePreparation:
    """Check versions, snapshot existing data, then run additive migrations."""

    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    check_schema_versions(database)
    backup: Path | None = None
    migration: sqlite3.Connection | None = None
    migration_started = False
    exclusive_acquired = False
    created_fresh = False
    try:
        migration = connect(database, row_factory=sqlite3.Row)
        _acquire_exclusive(migration, "migration")
        exclusive_acquired = True
        _schema_version(migration, "live_schema_version", LIVE_SCHEMA_VERSION)
        _schema_version(
            migration,
            "intelligence_schema_version",
            INTELLIGENCE_SCHEMA_VERSION,
        )
        migration.execute("PRAGMA journal_mode=WAL")
        has_schema = migration.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') LIMIT 1"
        ).fetchone()
        created_fresh = has_schema is None
        if has_schema is not None:
            backup = _backup_path(
                database,
                backup_dir,
                now or datetime.now(timezone.utc),
            )
            _backup_connection(migration, backup)
        migration_started = True
        with LiveBettingStore(database, connection=migration) as live:
            live.init_schema()
        with IntelligenceStorage(database, connection=migration) as intelligence:
            intelligence.init_schema()
            Database(connection=intelligence.connection).init_db()
        if migration.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("migration connection disabled foreign-key enforcement")
        if migration.execute("PRAGMA busy_timeout").fetchone()[0] != 5_000:
            raise RuntimeError("migration connection lost the 5-second busy timeout")
        foreign_key_issue = migration.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_issue is not None:
            raise RuntimeError(
                "migrated database failed foreign-key check: "
                f"table={foreign_key_issue[0]} rowid={foreign_key_issue[1]}"
            )
    except BaseException:
        if migration is not None:
            migration.close()
        if backup is not None and migration_started:
            _restore_online_backup(backup, database)
        elif created_fresh and exclusive_acquired:
            for path in (
                database,
                Path(f"{database}-wal"),
                Path(f"{database}-shm"),
            ):
                path.unlink(missing_ok=True)
        raise
    else:
        assert migration is not None
        migration.close()

    try:
        verified = verify_prepared_database(database)
    except BaseException:
        if backup is not None:
            _restore_online_backup(backup, database)
        elif created_fresh:
            for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
                path.unlink(missing_ok=True)
        raise
    return DatabasePreparation(
        database,
        backup,
        verified.live_schema_version,
        verified.intelligence_schema_version,
    )


__all__ = [
    "DatabasePreparation",
    "check_schema_versions",
    "online_backup",
    "prepare_database",
    "restore_database_backup",
    "verify_prepared_database",
]
