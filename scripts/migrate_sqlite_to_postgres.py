"""One-time, fail-closed SQLite to PostgreSQL data importer."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.engine import Connection, Engine

from database.engine import build_engine, require_database_url


ROOT = Path(__file__).resolve().parents[1]
BATCH_SIZE = 500
CONTROL_TABLES = {
    "alembic_version",
    "intelligence_schema_version",
    "live_schema_version",
    "runtime_schema_version",
}
SINGLETON_TABLES = {
    "draft_authority_revisions",
    "draft_deployment_revisions",
    "draft_lineage_revisions",
}
SEEDED_APPEND_TABLES = {
    "draft_lineage_changes",
    "raybet_match_odds_activity",
    "research_result_labels",
}
CRITICAL_ID_COLUMNS = {
    "decision_key",
    "input_ref",
    "order_key",
    "evidence_ref",
    "message_id",
}
EXTRA_DEPENDENCIES: dict[str, set[str]] = {
    "prospective_draft_outcomes": {
        "map_results",
        "settlement_reconciliations",
        "settlement_result_evidence",
    },
    "research_result_labels": {
        "map_results",
        "settlement_reconciliations",
        "settlement_result_evidence",
    },
    "settlement_authority": {
        "map_results",
        "settlement_reconciliations",
        "shadow_map_attempts",
    },
    "settlements": {"settlement_authority"},
    "shadow_orders": {
        "shadow_map_attempts",
        "shadow_order_decision_lineage",
        "strategy_decisions",
    },
    "strategy_decisions": {
        "odds_transport_observations",
        "vision_draft_anchors",
        "vision_observations",
    },
}


@dataclass(frozen=True)
class ImportReport:
    dry_run: bool
    source_path: str
    source_readonly: bool
    target_revision: str | None
    planned_tables: tuple[str, ...]
    skipped_source_tables: tuple[str, ...]
    row_counts: dict[str, int]
    primary_key_ranges: dict[str, tuple[int | None, int | None]]
    critical_digests: dict[str, str]
    business_counts: dict[str, int]


def migrate_sqlite_to_postgres(
    sqlite_path: str | Path,
    database_url: str | None = None,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Import one SQLite database into a PostgreSQL database atomically."""

    source_path = Path(sqlite_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    target_url = require_database_url(database_url)
    source = _open_readonly_sqlite(source_path)
    engine: Engine | None = None
    try:
        source_tables = _source_tables(source)
        source_counts = {
            table: _sqlite_count(source, table) for table in source_tables
        }
        if dry_run:
            engine = build_engine(target_url)
            revision = _target_revision(engine)
            return ImportReport(
                dry_run=True,
                source_path=str(source_path),
                source_readonly=True,
                target_revision=revision,
                planned_tables=tuple(sorted(source_tables - CONTROL_TABLES)),
                skipped_source_tables=(),
                row_counts=source_counts,
                primary_key_ranges=_source_primary_key_ranges(
                    source, source_tables
                ),
                critical_digests=_source_critical_digests(
                    source, source_tables
                ),
                business_counts=_sqlite_business_counts(source, source_tables),
            )

        _upgrade_target(target_url)
        engine = build_engine(target_url)
        inspector = inspect(engine)
        target_tables = set(inspector.get_table_names())
        skipped_source = sorted(source_tables - target_tables - CONTROL_TABLES)
        nonempty_skipped = [
            table for table in skipped_source if source_counts[table] > 0
        ]
        if nonempty_skipped:
            raise RuntimeError(
                "source tables have no PostgreSQL schema: "
                + ", ".join(nonempty_skipped)
            )
        shared_tables = (
            source_tables & target_tables
        ) - CONTROL_TABLES
        order = _import_order(inspector, shared_tables)
        metadata = MetaData()
        reflected = {
            name: Table(name, metadata, autoload_with=engine)
            for name in shared_tables
        }
        _validate_source_columns(source, reflected)

        with engine.begin() as target:
            target.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            _truncate_target(target, inspector, target_tables)
            for table_name in order:
                _copy_table(
                    source,
                    target,
                    reflected[table_name],
                    conflict_tolerant=(
                        table_name in SEEDED_APPEND_TABLES
                        or table_name in SINGLETON_TABLES
                    ),
                )
            _reset_identity_sequences(target, inspect(target), target_tables)

        report = _validate_import(
            source,
            engine,
            source_path,
            shared_tables,
            tuple(order),
            tuple(skipped_source),
        )
        return report
    finally:
        source.close()
        if engine is not None:
            engine.dispose()


def _open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _upgrade_target(database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _target_revision(engine: Engine) -> str | None:
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _sqlite_count(connection: sqlite3.Connection, table: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_sqlite_identifier(table)}"
        ).fetchone()[0]
    )


def _sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_sqlite_identifier(table)})"
        ).fetchall()
    )


def _source_primary_key_columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_sqlite_identifier(table)})"
    ).fetchall()
    return tuple(
        str(row[1])
        for row in sorted(rows, key=lambda item: int(item[5]))
        if int(row[5]) > 0
    )


def _validate_source_columns(
    source: sqlite3.Connection,
    target_tables: Mapping[str, Table],
) -> None:
    for table_name, table in target_tables.items():
        source_columns = set(_source_columns(source, table_name))
        unknown = source_columns - set(table.columns.keys())
        if unknown:
            raise RuntimeError(
                f"{table_name} has columns missing from PostgreSQL: "
                + ", ".join(sorted(unknown))
            )


def _import_order(inspector: Any, tables: set[str]) -> list[str]:
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        for foreign_key in inspector.get_foreign_keys(table):
            referred = foreign_key.get("referred_table")
            if referred in tables and referred != table:
                dependencies[table].add(str(referred))
        dependencies[table].update(
            EXTRA_DEPENDENCIES.get(table, set()) & tables
        )
    dependencies.get("strict_live_map_mappings", set()).discard(
        "strict_live_automatic_evidence_approvals"
    )

    ordered: list[str] = []
    pending = {table: set(values) for table, values in dependencies.items()}
    while pending:
        ready = sorted(
            table for table, values in pending.items() if not values
        )
        if not ready:
            cycle = ", ".join(sorted(pending))
            raise RuntimeError(f"unresolved PostgreSQL import dependency: {cycle}")
        for table in ready:
            ordered.append(table)
            pending.pop(table)
        for values in pending.values():
            values.difference_update(ready)
    return ordered


def _truncate_target(
    connection: Connection,
    inspector: Any,
    target_tables: set[str],
) -> None:
    preserve = CONTROL_TABLES | SINGLETON_TABLES | SEEDED_APPEND_TABLES
    tables = sorted(target_tables - preserve)
    if not tables:
        return
    preparer = connection.dialect.identifier_preparer
    names = ", ".join(preparer.quote(table) for table in tables)
    connection.exec_driver_sql(
        f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"
    )


def _copy_table(
    source: sqlite3.Connection,
    target: Connection,
    table: Table,
    *,
    conflict_tolerant: bool,
) -> None:
    columns = _source_columns(source, table.name)
    if not columns:
        return
    quoted_columns = ", ".join(_sqlite_identifier(column) for column in columns)
    cursor = source.execute(
        f"SELECT {quoted_columns} FROM {_sqlite_identifier(table.name)}"
    )
    column_types = {column.name: column.type for column in table.columns}
    primary_key = [column.name for column in table.primary_key.columns]
    while rows := cursor.fetchmany(BATCH_SIZE):
        payload = [
            {
                column: _coerce_value(row[column], column_types[column])
                for column in columns
            }
            for row in rows
        ]
        if conflict_tolerant and primary_key:
            statement = postgresql_insert(table).values(payload)
            if table.name in SINGLETON_TABLES:
                update_values = {
                    column.name: getattr(statement.excluded, column.name)
                    for column in table.columns
                    if column.name not in primary_key
                }
                target.execute(
                    statement.on_conflict_do_update(
                        index_elements=primary_key,
                        set_=update_values,
                    )
                )
            else:
                target.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=primary_key
                    )
                )
        else:
            target.execute(table.insert(), payload)


def _coerce_value(value: Any, column_type: sa.types.TypeEngine[Any]) -> Any:
    if value is None:
        return None
    if isinstance(column_type, sa.Boolean):
        return bool(value)
    return value


def _reset_identity_sequences(
    connection: Connection,
    inspector: Any,
    tables: set[str],
) -> None:
    preparer = connection.dialect.identifier_preparer
    for table in sorted(tables):
        for column in inspector.get_columns(table):
            if column.get("identity") is None:
                continue
            table_name = preparer.quote(table)
            column_name = preparer.quote(str(column["name"]))
            sequence = connection.execute(
                text("SELECT pg_get_serial_sequence(:table, :column)"),
                {"table": table, "column": str(column["name"])},
            ).scalar_one_or_none()
            if sequence is None:
                continue
            maximum = connection.exec_driver_sql(
                f"SELECT MAX({column_name}) FROM {table_name}"
            ).scalar_one()
            connection.execute(
                text("SELECT setval(:sequence, :value, :called)"),
                {
                    "sequence": sequence,
                    "value": 1 if maximum is None else int(maximum),
                    "called": maximum is not None,
                },
            )


def _validate_import(
    source: sqlite3.Connection,
    engine: Engine,
    source_path: Path,
    tables: set[str],
    order: tuple[str, ...],
    skipped: tuple[str, ...],
) -> ImportReport:
    source_counts = {table: _sqlite_count(source, table) for table in tables}
    source_ranges = _source_primary_key_ranges(source, tables)
    source_digests = _source_critical_digests(source, tables)
    with engine.connect() as target:
        target_counts = {
            table: int(
                target.execute(
                    text(f'SELECT COUNT(*) FROM "{table}"')
                ).scalar_one()
            )
            for table in tables
        }
        if target_counts != source_counts:
            mismatch = {
                table: (source_counts[table], target_counts[table])
                for table in tables
                if source_counts[table] != target_counts[table]
            }
            raise RuntimeError(f"row-count validation failed: {mismatch}")
        target_ranges = _target_primary_key_ranges(target, source, tables)
        if target_ranges != source_ranges:
            raise RuntimeError("primary-key range validation failed")
        target_digests = _target_critical_digests(target, source, tables)
        if target_digests != source_digests:
            raise RuntimeError("critical identity/hash validation failed")
        business_counts = _postgres_business_counts(target, tables)
        revision = target.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    source_business_counts = _sqlite_business_counts(source, tables)
    if business_counts != source_business_counts:
        raise RuntimeError(
            "business-count validation failed: "
            f"source={source_business_counts}, target={business_counts}"
        )
    return ImportReport(
        dry_run=False,
        source_path=str(source_path),
        source_readonly=True,
        target_revision=str(revision),
        planned_tables=order,
        skipped_source_tables=skipped,
        row_counts=source_counts,
        primary_key_ranges=source_ranges,
        critical_digests=source_digests,
        business_counts=business_counts,
    )


def _source_primary_key_ranges(
    connection: sqlite3.Connection,
    tables: Iterable[str],
) -> dict[str, tuple[int | None, int | None]]:
    ranges: dict[str, tuple[int | None, int | None]] = {}
    for table in sorted(tables):
        primary_key = _source_primary_key_columns(connection, table)
        if len(primary_key) != 1:
            continue
        column = primary_key[0]
        declared = next(
            row
            for row in connection.execute(
                f"PRAGMA table_info({_sqlite_identifier(table)})"
            ).fetchall()
            if str(row[1]) == column
        )
        if "INT" not in str(declared[2]).upper():
            continue
        row = connection.execute(
            f"SELECT MIN({_sqlite_identifier(column)}), "
            f"MAX({_sqlite_identifier(column)}) "
            f"FROM {_sqlite_identifier(table)}"
        ).fetchone()
        ranges[table] = (
            None if row[0] is None else int(row[0]),
            None if row[1] is None else int(row[1]),
        )
    return ranges


def _target_primary_key_ranges(
    target: Connection,
    source: sqlite3.Connection,
    tables: Iterable[str],
) -> dict[str, tuple[int | None, int | None]]:
    ranges: dict[str, tuple[int | None, int | None]] = {}
    for table, _ in _source_primary_key_ranges(source, tables).items():
        column = _source_primary_key_columns(source, table)[0]
        row = target.execute(
            text(f'SELECT MIN("{column}"), MAX("{column}") FROM "{table}"')
        ).one()
        ranges[table] = (
            None if row[0] is None else int(row[0]),
            None if row[1] is None else int(row[1]),
        )
    return ranges


def _critical_columns(
    source: sqlite3.Connection,
    table: str,
) -> tuple[str, ...]:
    return tuple(
        column
        for column in _source_columns(source, table)
        if "hash" in column.lower() or column in CRITICAL_ID_COLUMNS
    )


def _source_critical_digests(
    connection: sqlite3.Connection,
    tables: Iterable[str],
) -> dict[str, str]:
    return {
        table: _sqlite_digest(connection, table, columns)
        for table in sorted(tables)
        if (columns := _critical_columns(connection, table))
    }


def _target_critical_digests(
    connection: Connection,
    source: sqlite3.Connection,
    tables: Iterable[str],
) -> dict[str, str]:
    return {
        table: _postgres_digest(connection, source, table, columns)
        for table in sorted(tables)
        if (columns := _critical_columns(source, table))
    }


def _sqlite_digest(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> str:
    quoted = ", ".join(_sqlite_identifier(column) for column in columns)
    order = _source_primary_key_columns(connection, table) or tuple(columns)
    order_sql = ", ".join(_sqlite_identifier(column) for column in order)
    rows = connection.execute(
        f"SELECT {quoted} FROM {_sqlite_identifier(table)} ORDER BY {order_sql}"
    )
    return _row_digest(tuple(row) for row in rows)


def _postgres_digest(
    connection: Connection,
    source: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> str:
    quoted = ", ".join(f'"{column}"' for column in columns)
    order = _source_primary_key_columns(source, table) or tuple(columns)
    order_sql = ", ".join(f'"{column}"' for column in order)
    rows = connection.execute(
        text(f'SELECT {quoted} FROM "{table}" ORDER BY {order_sql}')
    )
    return _row_digest(tuple(row) for row in rows)


def _row_digest(rows: Iterable[Sequence[Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        payload = json.dumps(
            [_json_value(value) for value in row],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, memoryview):
        return value.tobytes().hex()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _sqlite_business_counts(
    connection: sqlite3.Connection,
    tables: Iterable[str],
) -> dict[str, int]:
    available = set(tables)
    counts = {
        "strategy_decisions": _optional_sqlite_count(
            connection, available, "strategy_decisions"
        ),
        "shadow_orders": _optional_sqlite_count(
            connection, available, "shadow_orders"
        ),
        "settlements": _optional_sqlite_count(
            connection, available, "settlements"
        ),
        "active_alerts": _optional_sqlite_count(
            connection,
            available,
            "monitor_alert_incidents",
            "status='active'",
        ),
    }
    return counts


def _optional_sqlite_count(
    connection: sqlite3.Connection,
    available: set[str],
    table: str,
    predicate: str | None = None,
) -> int:
    if table not in available:
        return 0
    suffix = "" if predicate is None else f" WHERE {predicate}"
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {_sqlite_identifier(table)}{suffix}"
        ).fetchone()[0]
    )


def _postgres_business_counts(
    connection: Connection,
    tables: Iterable[str],
) -> dict[str, int]:
    available = set(tables)
    counts: dict[str, int] = {}
    for key, table, predicate in (
        ("strategy_decisions", "strategy_decisions", None),
        ("shadow_orders", "shadow_orders", None),
        ("settlements", "settlements", None),
        ("active_alerts", "monitor_alert_incidents", "status='active'"),
    ):
        if table not in available:
            counts[key] = 0
            continue
        suffix = "" if predicate is None else f" WHERE {predicate}"
        counts[key] = int(
            connection.execute(
                text(f'SELECT COUNT(*) FROM "{table}"{suffix}')
            ).scalar_one()
        )
    return counts


def _write_report(path: Path, report: ImportReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument(
        "--postgres",
        default=None,
        help="PostgreSQL URL; defaults to DATABASE_URL",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = migrate_sqlite_to_postgres(
        args.sqlite,
        args.postgres,
        dry_run=args.dry_run,
    )
    if args.report is not None:
        _write_report(args.report.resolve(), report)
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
