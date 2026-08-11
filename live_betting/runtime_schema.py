"""PostgreSQL runtime-schema verification for monitor control and alerts."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from database.session import PostgresSession


ALEMBIC_HEAD = "20260807_0035"
CURRENT_RUNTIME_SCHEMA_VERSION = 1
RUNTIME_SCHEMA_CONTRACT_DIGEST = (
    "eb58ed6794cd39cdf4b9947a9132f2c2683cb20c769770586e3ca5c9f093beb9"
)
CONTROL_COMPONENT_NAMES = (
    "raybet_collector",
    "vision_supervisor",
)

_REQUIRED_TABLES = frozenset(
    {
        "runtime_schema_version",
        "monitor_process_registry",
        "monitor_control_audit",
        "monitor_alert_candidates",
        "live_draft_prospective_predictions",
        "live_draft_prospective_settlements",
        "map_decision_checkpoints",
        "map_decision_checkpoint_settlements",
        "monitor_alert_incidents",
        "monitor_alert_audit",
    }
)
_REQUIRED_INDEXES = frozenset(
    {
        "idx_monitor_alert_active_key",
        "idx_monitor_alert_status_opened",
        "ix_map_decision_checkpoints_map_time",
    }
)
_REQUIRED_TRIGGERS = frozenset(
    {
        "monitor_control_audit_no_update",
        "monitor_control_audit_no_delete",
        "monitor_alert_audit_no_update",
        "monitor_alert_audit_no_delete",
        "map_decision_checkpoints_append_only",
        "map_decision_checkpoint_settlements_append_only",
        "map_decision_checkpoint_settlements_insert_guard",
    }
)


@dataclass(frozen=True)
class RuntimeSchemaStatus:
    version: int
    contract_digest: str


def runtime_schema_version(connection: PostgresSession) -> int:
    """Return the installed PostgreSQL runtime schema version."""

    try:
        relation = connection.execute(
            "SELECT to_regclass(current_schema() || '.runtime_schema_version')"
        ).fetchone()
        if relation is None or relation[0] is None:
            return 0
        row = connection.execute(
            "SELECT MAX(version) FROM runtime_schema_version"
        ).fetchone()
    except SQLAlchemyError as error:
        raise RuntimeError("runtime schema version table is malformed") from error
    version = 0 if row is None or row[0] is None else int(row[0])
    if version > CURRENT_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError(
            f"runtime schema version {version} is newer than supported version "
            f"{CURRENT_RUNTIME_SCHEMA_VERSION}"
        )
    return version


def _schema_objects(
    connection: PostgresSession,
) -> tuple[set[str], set[str], set[str]]:
    tables = {
        str(row[0])
        for row in connection.execute(
            """SELECT table_name
                 FROM information_schema.tables
                WHERE table_schema=current_schema()
                  AND table_type='BASE TABLE'"""
        )
    }
    indexes = {
        str(row[0])
        for row in connection.execute(
            """SELECT indexname FROM pg_indexes
                WHERE schemaname=current_schema()"""
        )
    }
    triggers = {
        str(row[0])
        for row in connection.execute(
            """SELECT trigger.trigger_name
                 FROM information_schema.triggers AS trigger
                WHERE trigger.trigger_schema=current_schema()"""
        )
    }
    return tables, indexes, triggers


def _reject_temporary_shadowing(connection: PostgresSession) -> None:
    temporary = {
        str(row[0])
        for row in connection.execute(
            """SELECT relation.relname
                 FROM pg_class AS relation
                WHERE relation.relnamespace=pg_my_temp_schema()
                  AND relation.relname = ANY(CAST(? AS text[]))""",
            (list(_REQUIRED_TABLES),),
        )
    }
    if temporary:
        raise RuntimeError(
            "temporary runtime schema objects are not allowed: "
            + ", ".join(sorted(temporary))
        )


def verify_runtime_schema(connection: PostgresSession) -> RuntimeSchemaStatus:
    """Verify the Alembic-managed PostgreSQL runtime contract read-only."""

    _reject_temporary_shadowing(connection)
    revision = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
    actual_revision = None if revision is None else str(revision[0])
    if actual_revision != ALEMBIC_HEAD:
        raise RuntimeError(
            f"PostgreSQL schema revision {actual_revision!r} is not {ALEMBIC_HEAD}"
        )
    version = runtime_schema_version(connection)
    if version != CURRENT_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError(
            "runtime schema is not prepared: "
            f"version={version}/{CURRENT_RUNTIME_SCHEMA_VERSION}"
        )
    rows = connection.execute(
        """SELECT version, contract_digest
             FROM runtime_schema_version ORDER BY version"""
    ).fetchall()
    if len(rows) != 1 or int(rows[0][0]) != CURRENT_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError("runtime schema version history is invalid")
    recorded_digest = str(rows[0][1])
    if recorded_digest != RUNTIME_SCHEMA_CONTRACT_DIGEST:
        raise RuntimeError(
            "runtime schema contract digest mismatch for current version"
        )
    tables, indexes, triggers = _schema_objects(connection)
    missing = sorted(
        (_REQUIRED_TABLES - tables)
        | (_REQUIRED_INDEXES - indexes)
        | (_REQUIRED_TRIGGERS - triggers)
    )
    if missing:
        raise RuntimeError("runtime schema contract failed: missing objects: " + ", ".join(missing))
    return RuntimeSchemaStatus(version, recorded_digest)


def prepare_runtime_schema(
    connection: PostgresSession,
    *,
    external_transaction: bool = False,
) -> RuntimeSchemaStatus:
    """Verify the runtime schema; Alembic is the only schema writer."""

    if external_transaction and not connection.in_transaction:
        raise RuntimeError("external runtime schema transaction is not active")
    return verify_runtime_schema(connection)


__all__ = [
    "CONTROL_COMPONENT_NAMES",
    "CURRENT_RUNTIME_SCHEMA_VERSION",
    "RUNTIME_SCHEMA_CONTRACT_DIGEST",
    "RuntimeSchemaStatus",
    "prepare_runtime_schema",
    "runtime_schema_version",
    "verify_runtime_schema",
]
