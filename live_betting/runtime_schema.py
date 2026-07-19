"""Versioned schema contract for monitor control and alert runtime state.

Runtime processes verify this contract.  Only the supervisor-owned database
preparation flow is allowed to install or migrate it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache


CURRENT_RUNTIME_SCHEMA_VERSION = 1
CONTROL_COMPONENT_NAMES = (
    "raybet_collector",
    "shadow_monitor",
    "vision_supervisor",
    "draft_publisher",
    "mail_worker",
)

_RUNTIME_VERSION_TABLE = """CREATE TABLE runtime_schema_version (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    contract_digest TEXT NOT NULL CHECK (length(contract_digest) = 64),
    installed_at TEXT NOT NULL
)"""

_OUTBOX_TABLE = """CREATE TABLE notification_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('filled', 'settled', 'monitor_alert', 'monitor_recovery')),
    channel TEXT NOT NULL DEFAULT 'email',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'leased', 'sent', 'dead_letter')),
    recipient TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    statistics_cutoff TEXT NOT NULL,
    template_version TEXT NOT NULL,
    lease_token TEXT,
    lease_until TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (order_key, event_type, channel)
)"""

_OUTBOX_INDEX = """CREATE INDEX idx_notification_outbox_due
    ON notification_outbox(status, next_attempt_at, lease_until)"""

_OUTBOX_TRIGGER = """CREATE TRIGGER notification_outbox_payload_immutable
BEFORE UPDATE ON notification_outbox
WHEN OLD.order_key IS NOT NEW.order_key
  OR OLD.event_type IS NOT NEW.event_type
  OR OLD.channel IS NOT NEW.channel
  OR OLD.payload_json IS NOT NEW.payload_json
  OR OLD.statistics_cutoff IS NOT NEW.statistics_cutoff
  OR OLD.template_version IS NOT NEW.template_version
  OR OLD.recipient IS NOT NEW.recipient
  OR OLD.message_id IS NOT NEW.message_id
BEGIN
    SELECT RAISE(ABORT, 'notification outbox payload is immutable');
END"""

_COMPONENT_VALUES = ", ".join(f"'{name}'" for name in CONTROL_COMPONENT_NAMES)
_CONTROL_TABLES = {
    "monitor_process_registry": f"""CREATE TABLE monitor_process_registry (
        component TEXT PRIMARY KEY CHECK (component IN ({_COMPONENT_VALUES})),
        pid INTEGER,
        command_hash TEXT NOT NULL,
        command_json TEXT NOT NULL,
        process_created_at REAL,
        started_at TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'stopped')),
        updated_at TEXT NOT NULL
    )""",
    "monitor_control_audit": f"""CREATE TABLE monitor_control_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        component TEXT NOT NULL CHECK (component IN ({_COMPONENT_VALUES})),
        action TEXT NOT NULL CHECK (action IN ('start', 'stop', 'restart')),
        result TEXT NOT NULL,
        ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
        pid INTEGER,
        command_hash TEXT,
        process_created_at REAL,
        client_host TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        response_json TEXT NOT NULL
    )""",
}

_CONTROL_TRIGGERS = {
    "monitor_control_audit_no_update": """CREATE TRIGGER monitor_control_audit_no_update
        BEFORE UPDATE ON monitor_control_audit
        BEGIN
            SELECT RAISE(ABORT, 'monitor control audit rows are immutable');
        END""",
    "monitor_control_audit_no_delete": """CREATE TRIGGER monitor_control_audit_no_delete
        BEFORE DELETE ON monitor_control_audit
        BEGIN
            SELECT RAISE(ABORT, 'monitor control audit rows cannot be deleted');
        END""",
}

_ALERT_TABLES = {
    "monitor_alert_candidates": """CREATE TABLE monitor_alert_candidates (
        dedupe_key TEXT PRIMARY KEY,
        first_detected_at TEXT NOT NULL,
        last_detected_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    "monitor_alert_incidents": """CREATE TABLE monitor_alert_incidents (
        incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL,
        episode INTEGER NOT NULL CHECK (episode > 0),
        category TEXT NOT NULL CHECK (category IN ('operational', 'paper_signal')),
        severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active', 'recovered')),
        first_detected_at TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        last_detected_at TEXT NOT NULL,
        recovered_at TEXT,
        acknowledged_at TEXT,
        acknowledged_by TEXT,
        source_json TEXT NOT NULL,
        occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
        UNIQUE (dedupe_key, episode)
    )""",
    "monitor_alert_audit": """CREATE TABLE monitor_alert_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL REFERENCES monitor_alert_incidents(incident_id),
        action TEXT NOT NULL CHECK (action IN ('opened', 'observed', 'acknowledged', 'recovered')),
        actor TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
}

_ALERT_INDEXES = {
    "idx_monitor_alert_active_key": """CREATE UNIQUE INDEX idx_monitor_alert_active_key
        ON monitor_alert_incidents(dedupe_key) WHERE status='active'""",
    "idx_monitor_alert_status_opened": """CREATE INDEX idx_monitor_alert_status_opened
        ON monitor_alert_incidents(status, opened_at DESC)""",
}

_ALERT_TRIGGERS = {
    "monitor_alert_audit_no_update": """CREATE TRIGGER monitor_alert_audit_no_update
        BEFORE UPDATE ON monitor_alert_audit
        BEGIN
            SELECT RAISE(ABORT, 'monitor alert audit rows are immutable');
        END""",
    "monitor_alert_audit_no_delete": """CREATE TRIGGER monitor_alert_audit_no_delete
        BEFORE DELETE ON monitor_alert_audit
        BEGIN
            SELECT RAISE(ABORT, 'monitor alert audit rows cannot be deleted');
        END""",
}

_TABLE_STATEMENTS = {
    "notification_outbox": _OUTBOX_TABLE,
    **_CONTROL_TABLES,
    **_ALERT_TABLES,
}
_INDEX_STATEMENTS = {
    "idx_notification_outbox_due": _OUTBOX_INDEX,
    **_ALERT_INDEXES,
}
_TRIGGER_STATEMENTS = {
    "notification_outbox_payload_immutable": _OUTBOX_TRIGGER,
    **_CONTROL_TRIGGERS,
    **_ALERT_TRIGGERS,
}
_RUNTIME_TABLE_NAMES = frozenset(
    {"runtime_schema_version"} | _TABLE_STATEMENTS.keys()
)
# The intelligence schema owns this compatibility index on the shared outbox.
# Its contract is verified by database_protocol, not by the runtime schema.
_EXTERNAL_RUNTIME_OBJECTS = frozenset(
    {("index", "idx_notification_due", "notification_outbox")}
)
_EXTERNAL_INDEX_STATEMENTS = {
    "idx_notification_due": """CREATE INDEX idx_notification_due
        ON notification_outbox(status, next_attempt_at, lease_until)""",
}
_EXPECTED_OBJECT_NAMES = frozenset(
    _RUNTIME_TABLE_NAMES
    | _INDEX_STATEMENTS.keys()
    | _TRIGGER_STATEMENTS.keys()
)
_MIGRATION_SUFFIX = "__runtime_schema_v1"
_SQL_TOKEN = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|"
    r"\[(?:\]\]|[^\]])*\]|[A-Za-z_][A-Za-z0-9_$]*|"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|"
    r"->>|->|<=|>=|<>|!=|==|\|\||[^\s]"
)
_TRIGGER_SCAN_TOKEN = re.compile(
    r"--[^\r\n]*(?:\r?\n|$)|/\*.*?(?:\*/|$)|"
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|"
    r"\[(?:\]\]|[^\]])*\]|[A-Za-z_][A-Za-z0-9_$]*|"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|"
    r"->>|->|<=|>=|<>|!=|==|\|\||[^\s]",
    re.DOTALL,
)
_BARE_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


@dataclass(frozen=True)
class RuntimeSchemaStatus:
    version: int
    contract_digest: str


def _normalize_sql(sql: object) -> str:
    tokens = _SQL_TOKEN.findall("" if sql is None else str(sql))
    return " ".join(
        token if token.startswith("'") else token.casefold()
        for token in tokens
    )


def _bare_sql_keyword(token: str) -> str | None:
    if _BARE_SQL_IDENTIFIER.fullmatch(token) is None:
        return None
    return token.casefold()


def _sql_identifier(token: str) -> str | None:
    if _BARE_SQL_IDENTIFIER.fullmatch(token) is not None:
        return token.casefold()
    if len(token) < 2:
        return None
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1].replace('""', '"').casefold()
    if token.startswith("`") and token.endswith("`"):
        return token[1:-1].replace("``", "`").casefold()
    if token.startswith("[") and token.endswith("]"):
        return token[1:-1].replace("]]", "]").casefold()
    return None


def _trigger_write_targets(sql: object) -> frozenset[str]:
    tokens = tuple(
        token
        for token in _TRIGGER_SCAN_TOKEN.findall("" if sql is None else str(sql))
        if not token.startswith(("'", "--", "/*"))
    )
    targets: set[str] = set()
    for index, token in enumerate(tokens):
        keyword = _bare_sql_keyword(token)
        target_index: int | None = None
        if keyword in {"insert", "update"}:
            target_index = index + 1
            if (
                target_index < len(tokens)
                and _bare_sql_keyword(tokens[target_index]) == "or"
            ):
                target_index += 2
            if keyword == "insert":
                if (
                    target_index >= len(tokens)
                    or _bare_sql_keyword(tokens[target_index]) != "into"
                ):
                    continue
                target_index += 1
        elif keyword == "replace":
            target_index = index + 1
            if (
                target_index < len(tokens)
                and _bare_sql_keyword(tokens[target_index]) == "into"
            ):
                target_index += 1
        elif keyword == "delete":
            target_index = index + 1
            if (
                target_index >= len(tokens)
                or _bare_sql_keyword(tokens[target_index]) != "from"
            ):
                continue
            target_index += 1
        if target_index is None or target_index >= len(tokens):
            continue
        target = _sql_identifier(tokens[target_index])
        if target is None:
            continue
        if target_index + 2 < len(tokens) and tokens[target_index + 1] == ".":
            qualified_target = _sql_identifier(tokens[target_index + 2])
            if qualified_target is not None:
                target = qualified_target
        targets.add(target)
    return frozenset(targets)


def _schema_objects(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, str, str]]:
    rows = [
        (False, *row)
        for row in connection.execute(
            """SELECT type, name, tbl_name, sql
                 FROM main.sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                  AND type IN ('table', 'index', 'trigger', 'view')"""
        )
    ]
    rows.extend(
        (True, *row)
        for row in connection.execute(
            """SELECT type, name, tbl_name, sql
                 FROM sqlite_temp_master
                WHERE name NOT LIKE 'sqlite_%'
                  AND type IN ('table', 'index', 'trigger', 'view')"""
        )
    )
    return {
        (f"temp.{name}" if is_temporary else str(name)): (
            str(object_type),
            str(table_name),
            _normalize_sql(sql),
        )
        for is_temporary, object_type, name, table_name, sql in rows
        if (
            (
                is_temporary
                or (str(object_type), str(name), str(table_name))
                not in _EXTERNAL_RUNTIME_OBJECTS
            )
            and (
                str(name) in _EXPECTED_OBJECT_NAMES
                or str(table_name) in _RUNTIME_TABLE_NAMES
                or (
                    str(object_type) == "trigger"
                    and bool(
                        _trigger_write_targets(sql) & _RUNTIME_TABLE_NAMES
                    )
                )
                or str(name).startswith("monitor_")
                or str(name).startswith("runtime_schema_")
                or str(name).endswith(_MIGRATION_SUFFIX)
            )
        )
    }


def _install_empty_schema(connection: sqlite3.Connection) -> None:
    connection.execute(_RUNTIME_VERSION_TABLE)
    for statement in _TABLE_STATEMENTS.values():
        connection.execute(statement)
    for statement in _INDEX_STATEMENTS.values():
        connection.execute(statement)
    for statement in _TRIGGER_STATEMENTS.values():
        connection.execute(statement)


@lru_cache(maxsize=1)
def _expected_objects() -> dict[str, tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        _install_empty_schema(connection)
        return _schema_objects(connection)
    finally:
        connection.close()


def _contract_digest() -> str:
    payload = json.dumps(
        sorted((name, *contract) for name, contract in _expected_objects().items()),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


RUNTIME_SCHEMA_CONTRACT_DIGEST = _contract_digest()


def runtime_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        """SELECT 1 FROM main.sqlite_master
             WHERE type='table' AND name='runtime_schema_version'"""
    ).fetchone()
    if row is None:
        return 0
    try:
        version_row = connection.execute(
            "SELECT MAX(version) FROM main.runtime_schema_version"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise RuntimeError("runtime schema version table is malformed") from error
    version = 0 if version_row is None or version_row[0] is None else int(version_row[0])
    if version > CURRENT_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError(
            f"runtime schema version {version} is newer than supported version "
            f"{CURRENT_RUNTIME_SCHEMA_VERSION}"
        )
    return version


def _contract_errors(connection: sqlite3.Connection) -> list[str]:
    expected = _expected_objects()
    actual = _schema_objects(connection)
    errors: list[str] = []
    missing = sorted(expected.keys() - actual.keys())
    if missing:
        errors.append("missing objects: " + ", ".join(missing))
    unexpected = sorted(actual.keys() - expected.keys())
    if unexpected:
        errors.append("unexpected objects: " + ", ".join(unexpected))
    mismatched = sorted(
        name
        for name in expected.keys() & actual.keys()
        if expected[name] != actual[name]
    )
    if mismatched:
        errors.append("mismatched objects: " + ", ".join(mismatched))
    return errors


def verify_runtime_schema(connection: sqlite3.Connection) -> RuntimeSchemaStatus:
    """Verify the runtime contract using SELECT/PRAGMA inspection only."""

    version = runtime_schema_version(connection)
    if version != CURRENT_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError(
            "runtime schema is not prepared: "
            f"version={version}/{CURRENT_RUNTIME_SCHEMA_VERSION}"
        )
    try:
        rows = list(connection.execute(
            """SELECT version, contract_digest
                 FROM main.runtime_schema_version ORDER BY version"""
        ))
    except sqlite3.DatabaseError as error:
        raise RuntimeError("runtime schema version table is malformed") from error
    if len(rows) != 1 or int(rows[0][0]) != CURRENT_RUNTIME_SCHEMA_VERSION:
        raise RuntimeError("runtime schema version history is invalid")
    recorded_digest = str(rows[0][1])
    if recorded_digest != RUNTIME_SCHEMA_CONTRACT_DIGEST:
        raise RuntimeError(
            "runtime schema contract digest mismatch for current version"
        )
    errors = _contract_errors(connection)
    if errors:
        raise RuntimeError("runtime schema contract failed: " + "; ".join(errors))
    return RuntimeSchemaStatus(version, recorded_digest)


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA main.table_xinfo("{table}")')
    )


@lru_cache(maxsize=1)
def _expected_columns() -> dict[str, tuple[str, ...]]:
    connection = sqlite3.connect(":memory:")
    try:
        _install_empty_schema(connection)
        return {
            table: _table_columns(connection, table)
            for table in _TABLE_STATEMENTS
        }
    finally:
        connection.close()


def _existing_table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table'"
        )
    }


def _migrate_unversioned_schema(connection: sqlite3.Connection) -> None:
    existing = _existing_table_names(connection)
    existing_objects = _schema_objects(connection)
    temporary = sorted(
        name for name in existing_objects if name.startswith("temp.")
    )
    if temporary:
        raise RuntimeError(
            "temporary runtime schema objects are not allowed: "
            + ", ".join(temporary)
        )
    external_objects = {
        (str(object_type), str(name), str(table_name))
        for object_type, name, table_name in connection.execute(
            """SELECT type, name, tbl_name FROM main.sqlite_master
                 WHERE type IN ('index', 'trigger')"""
        )
        if (str(object_type), str(name), str(table_name))
        in _EXTERNAL_RUNTIME_OBJECTS
    }
    unknown = sorted(
        name
        for name in existing
        if (
            name.startswith("monitor_")
            or name.startswith("runtime_schema_")
            or name.endswith(_MIGRATION_SUFFIX)
        )
        and name not in _TABLE_STATEMENTS
    )
    if unknown:
        raise RuntimeError(
            "unrecognized unversioned runtime schema objects: " + ", ".join(unknown)
        )

    expected_columns = _expected_columns()
    rebuild: set[str] = set()
    for table in _TABLE_STATEMENTS:
        if table not in existing:
            continue
        actual_columns = _table_columns(connection, table)
        if actual_columns != expected_columns[table]:
            raise RuntimeError(
                f"unversioned runtime table {table} has incompatible columns"
            )
        if existing_objects.get(table) != _expected_objects()[table]:
            rebuild.add(table)

    # Renaming the parent rewrites an otherwise-current child's foreign-key
    # target to the temporary name. Rebuild both sides in that case.
    if (
        "monitor_alert_incidents" in rebuild
        and "monitor_alert_audit" in existing
    ):
        rebuild.add("monitor_alert_audit")

    for name in (*_TRIGGER_STATEMENTS, *_INDEX_STATEMENTS):
        connection.execute(f'DROP {"TRIGGER" if name in _TRIGGER_STATEMENTS else "INDEX"} IF EXISTS "{name}"')

    # Children are renamed before their referenced parents.  The temporary
    # tables are dropped in the same child-first order after data is copied.
    rename_order = (
        "monitor_alert_audit",
        "monitor_alert_incidents",
        "monitor_alert_candidates",
        "monitor_control_audit",
        "monitor_process_registry",
        "notification_outbox",
    )
    migrated: list[str] = []
    for table in rename_order:
        if table not in rebuild:
            continue
        temporary = f"{table}{_MIGRATION_SUFFIX}"
        connection.execute(f'ALTER TABLE "{table}" RENAME TO "{temporary}"')
        migrated.append(table)

    for table, statement in _TABLE_STATEMENTS.items():
        if table not in existing or table in rebuild:
            connection.execute(statement)
    for table in _TABLE_STATEMENTS:
        if table not in migrated:
            continue
        columns = expected_columns[table]
        quoted = ", ".join(f'"{column}"' for column in columns)
        connection.execute(
            f'INSERT INTO "{table}" ({quoted}) '
            f'SELECT {quoted} FROM "{table}{_MIGRATION_SUFFIX}"'
        )
    for table in rename_order:
        if table in migrated:
            connection.execute(f'DROP TABLE "{table}{_MIGRATION_SUFFIX}"')

    for statement in _INDEX_STATEMENTS.values():
        connection.execute(statement)
    if (
        "notification_outbox" in rebuild
        and ("index", "idx_notification_due", "notification_outbox")
        in external_objects
    ):
        connection.execute(_EXTERNAL_INDEX_STATEMENTS["idx_notification_due"])
    for statement in _TRIGGER_STATEMENTS.values():
        connection.execute(statement)
    connection.execute(_RUNTIME_VERSION_TABLE)
    connection.execute(
        """INSERT INTO runtime_schema_version
           (version, contract_digest, installed_at) VALUES (?, ?, ?)""",
        (
            CURRENT_RUNTIME_SCHEMA_VERSION,
            RUNTIME_SCHEMA_CONTRACT_DIGEST,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def prepare_runtime_schema(
    connection: sqlite3.Connection,
    *,
    external_transaction: bool = False,
) -> RuntimeSchemaStatus:
    """Install or migrate runtime schema v0 atomically, then verify it.

    A database already stamped with the current version is never repaired in
    place: any contract drift is rejected and requires a new schema version.
    """

    version = runtime_schema_version(connection)
    if version == CURRENT_RUNTIME_SCHEMA_VERSION:
        return verify_runtime_schema(connection)
    version_table_exists = connection.execute(
        """SELECT 1 FROM main.sqlite_master
             WHERE type='table' AND name='runtime_schema_version'"""
    ).fetchone() is not None
    if version_table_exists:
        raise RuntimeError("runtime schema version history is empty or incomplete")
    if version != 0:
        raise RuntimeError(f"unsupported runtime schema migration from version {version}")
    if external_transaction and not connection.in_transaction:
        raise RuntimeError("external runtime schema transaction is not active")

    nested = connection.in_transaction
    savepoint = "runtime_schema_prepare"
    if not external_transaction:
        if nested:
            connection.execute(f"SAVEPOINT {savepoint}")
        else:
            connection.execute("BEGIN IMMEDIATE")
    try:
        _migrate_unversioned_schema(connection)
        status = verify_runtime_schema(connection)
    except BaseException:
        if not external_transaction:
            if nested:
                connection.execute(f"ROLLBACK TO {savepoint}")
                connection.execute(f"RELEASE {savepoint}")
            else:
                connection.rollback()
        raise
    else:
        if not external_transaction:
            if nested:
                connection.execute(f"RELEASE {savepoint}")
            else:
                connection.commit()
        return status


__all__ = [
    "CONTROL_COMPONENT_NAMES",
    "CURRENT_RUNTIME_SCHEMA_VERSION",
    "RUNTIME_SCHEMA_CONTRACT_DIGEST",
    "RuntimeSchemaStatus",
    "prepare_runtime_schema",
    "runtime_schema_version",
    "verify_runtime_schema",
]
