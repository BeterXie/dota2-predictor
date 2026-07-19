from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from live_betting.database_protocol import prepare_database, verify_prepared_database
from live_betting.runtime_schema import (
    CURRENT_RUNTIME_SCHEMA_VERSION,
    RUNTIME_SCHEMA_CONTRACT_DIGEST,
    prepare_runtime_schema,
    runtime_schema_version,
    verify_runtime_schema,
)


def _legacy_runtime_schema(connection: sqlite3.Connection) -> None:
    legacy_components = (
        "'raybet_collector', 'shadow_monitor', 'vision_supervisor', 'mail_worker'"
    )
    connection.executescript(
        f"""
        CREATE TABLE notification_outbox (
            outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_key TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('filled', 'settled')),
            channel TEXT NOT NULL DEFAULT 'email',
            status TEXT NOT NULL DEFAULT 'pending',
            recipient TEXT NOT NULL,
            message_id TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            statistics_cutoff TEXT NOT NULL,
            template_version TEXT NOT NULL,
            lease_token TEXT,
            lease_until TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (order_key, event_type, channel)
        );
        CREATE TABLE monitor_process_registry (
            component TEXT PRIMARY KEY CHECK (component IN ({legacy_components})),
            pid INTEGER,
            command_hash TEXT NOT NULL,
            command_json TEXT NOT NULL,
            process_created_at REAL,
            started_at TEXT,
            status TEXT NOT NULL CHECK (status IN ('running', 'stopped')),
            updated_at TEXT NOT NULL
        );
        CREATE TABLE monitor_control_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            component TEXT NOT NULL CHECK (component IN ({legacy_components})),
            action TEXT NOT NULL CHECK (action IN ('start', 'stop', 'restart')),
            result TEXT NOT NULL,
            ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
            pid INTEGER,
            command_hash TEXT,
            process_created_at REAL,
            client_host TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            response_json TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """INSERT INTO notification_outbox
           (outbox_id, order_key, event_type, channel, status, recipient,
            message_id, payload_json, statistics_cutoff, template_version,
            attempt_count, created_at, updated_at)
           VALUES (7, 'order-7', 'filled', 'email', 'pending',
                   'ops@example.com', 'message-7', '{}', '2026-07-18T00:00:00Z',
                   'legacy-v1', 0, '2026-07-18T00:00:00Z',
                   '2026-07-18T00:00:00Z')"""
    )
    connection.execute(
        """INSERT INTO monitor_process_registry
           (component, pid, command_hash, command_json, process_created_at,
            started_at, status, updated_at)
           VALUES ('raybet_collector', 42, 'hash', '[]', 1.0,
                   '2026-07-18T00:00:00Z', 'running',
                   '2026-07-18T00:00:00Z')"""
    )
    connection.execute(
        """INSERT INTO monitor_control_audit
           (audit_id, request_id, component, action, result, ok, pid,
            command_hash, process_created_at, client_host, requested_at,
            response_json)
           VALUES (9, 'request-9', 'raybet_collector', 'start', 'started', 1,
                   42, 'hash', 1.0, '127.0.0.1',
                   '2026-07-18T00:00:00Z', '{}')"""
    )
    connection.commit()


def test_legacy_runtime_schema_migrates_without_losing_rows() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        _legacy_runtime_schema(connection)

        status = prepare_runtime_schema(connection)

        assert status.version == CURRENT_RUNTIME_SCHEMA_VERSION
        assert status.contract_digest == RUNTIME_SCHEMA_CONTRACT_DIGEST
        assert connection.execute(
            "SELECT outbox_id FROM notification_outbox"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT pid FROM monitor_process_registry"
        ).fetchone()[0] == 42
        assert connection.execute(
            "SELECT audit_id FROM monitor_control_audit"
        ).fetchone()[0] == 9
        assert "monitor_alert" in str(connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='notification_outbox'"
        ).fetchone()[0])
        assert "draft_publisher" in str(connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='monitor_process_registry'"
        ).fetchone()[0])
        assert verify_runtime_schema(connection) == status
    finally:
        connection.close()


def test_external_runtime_preparation_participates_in_caller_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN IMMEDIATE")
        prepare_runtime_schema(connection, external_transaction=True)
        connection.rollback()

        assert runtime_schema_version(connection) == 0
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='monitor_alert_incidents'"
        ).fetchone() is None

        connection.execute("BEGIN IMMEDIATE")
        prepare_runtime_schema(connection, external_transaction=True)
        connection.commit()
        assert verify_runtime_schema(connection).version == (
            CURRENT_RUNTIME_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_current_version_contract_drift_is_rejected_not_repaired() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        connection.execute("DROP TRIGGER monitor_alert_audit_no_update")
        connection.commit()

        with pytest.raises(RuntimeError, match="missing objects"):
            prepare_runtime_schema(connection)

        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='monitor_alert_audit_no_update'"
        ).fetchone() is None
    finally:
        connection.close()


def test_current_version_rejects_renamed_object_attached_to_runtime_table() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        connection.execute(
            """CREATE TRIGGER unrelated_registration_guard
               BEFORE INSERT ON monitor_process_registry
               BEGIN
                   SELECT RAISE(ABORT, 'blocked');
               END"""
        )
        connection.commit()

        with pytest.raises(
            RuntimeError,
            match="unexpected objects: unrelated_registration_guard",
        ):
            verify_runtime_schema(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("trigger_name", "operation"),
    (
        (
            "arbitrary_runtime_insert",
            """INSERT OR IGNORE INTO \"monitor_alert_candidates\"
               (dedupe_key, first_detected_at, last_detected_at, payload_json)
               VALUES ('cross-table', 'now', 'now', '{}')""",
        ),
        (
            "arbitrary_runtime_update",
            """UPDATE /* runtime target */ [monitor_process_registry]
               SET status='stopped'""",
        ),
        (
            "arbitrary_runtime_delete",
            "DELETE FROM `notification_outbox`",
        ),
        (
            "arbitrary_runtime_replace",
            """REPLACE INTO monitor_alert_candidates
               (dedupe_key, first_detected_at, last_detected_at, payload_json)
               VALUES ('cross-table', 'now', 'now', '{}')""",
        ),
    ),
)
def test_current_version_rejects_cross_table_runtime_writes(
    trigger_name: str,
    operation: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE unrelated_events (event_id INTEGER)")
        prepare_runtime_schema(connection)
        connection.execute(
            f"""CREATE TRIGGER {trigger_name}
                AFTER INSERT ON unrelated_events
                BEGIN
                    {operation};
                END"""
        )
        connection.commit()

        with pytest.raises(
            RuntimeError,
            match=rf"unexpected objects: {trigger_name}",
        ):
            verify_runtime_schema(connection)
    finally:
        connection.close()


def test_current_version_allows_unrelated_persistent_trigger() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE unrelated_events (event_id INTEGER);
            CREATE TABLE unrelated_audit (event_id INTEGER);
            """
        )
        prepare_runtime_schema(connection)
        connection.executescript(
            """
            CREATE TRIGGER unrelated_audit_insert
            AFTER INSERT ON unrelated_events
            BEGIN
                -- DELETE FROM notification_outbox is documentation only.
                SELECT 'UPDATE monitor_process_registry SET status=''stopped''';
                INSERT INTO unrelated_audit (event_id) VALUES (NEW.event_id);
            END;
            """
        )

        assert verify_runtime_schema(connection).version == (
            CURRENT_RUNTIME_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_current_version_rejects_temp_cross_table_runtime_write() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE unrelated_events (event_id INTEGER)")
        prepare_runtime_schema(connection)
        connection.execute(
            """CREATE TEMP TRIGGER arbitrary_temp_runtime_write
               AFTER INSERT ON main.unrelated_events
               BEGIN
                   UPDATE monitor_alert_incidents
                      SET occurrence_count=occurrence_count + 1;
               END"""
        )

        with pytest.raises(
            RuntimeError,
            match=r"unexpected objects: temp\.arbitrary_temp_runtime_write",
        ):
            verify_runtime_schema(connection)
    finally:
        connection.close()


def test_current_version_rejects_temporary_runtime_schema_shadow() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        connection.execute(
            """CREATE TEMP TABLE runtime_schema_version (
                   version INTEGER,
                   contract_digest TEXT
               )"""
        )
        connection.execute(
            "INSERT INTO temp.runtime_schema_version VALUES (999, 'bad')"
        )

        with pytest.raises(
            RuntimeError,
            match=r"unexpected objects: temp\.runtime_schema_version",
        ):
            verify_runtime_schema(connection)
    finally:
        connection.close()


def test_shared_intelligence_outbox_index_is_not_runtime_owned() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        connection.execute(
            """CREATE INDEX idx_notification_due
               ON notification_outbox(status, next_attempt_at, lease_until)"""
        )
        connection.commit()

        assert verify_runtime_schema(connection).version == (
            CURRENT_RUNTIME_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_legacy_outbox_rebuild_preserves_shared_intelligence_index() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        _legacy_runtime_schema(connection)
        connection.execute(
            """CREATE INDEX idx_notification_due
               ON notification_outbox(status, next_attempt_at, lease_until)"""
        )
        connection.commit()

        prepare_runtime_schema(connection)

        indexes = {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                     WHERE type='index' AND tbl_name='notification_outbox'"""
            )
        }
        assert "idx_notification_due" in indexes
        assert "idx_notification_outbox_due" in indexes
        assert verify_runtime_schema(connection).version == (
            CURRENT_RUNTIME_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_parent_table_rebuild_keeps_alert_audit_foreign_key_canonical() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.executescript(
            """
            CREATE TABLE monitor_alert_incidents (
                incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL,
                episode INTEGER NOT NULL CHECK (episode > 0),
                category TEXT NOT NULL,
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
            );
            CREATE TABLE monitor_alert_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL
                    REFERENCES monitor_alert_incidents(incident_id),
                action TEXT NOT NULL
                    CHECK (action IN ('opened', 'observed', 'acknowledged', 'recovered')),
                actor TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO monitor_alert_incidents
                (incident_id, dedupe_key, episode, category, severity, title,
                 body, status, first_detected_at, opened_at, last_detected_at,
                 source_json)
            VALUES
                (4, 'operational:test', 1, 'operational', 'warning', 'title',
                 'body', 'active', '2026-07-18T00:00:00Z',
                 '2026-07-18T00:00:00Z', '2026-07-18T00:00:00Z', '{}');
            INSERT INTO monitor_alert_audit
                (audit_id, incident_id, action, actor, detail, created_at)
            VALUES
                (8, 4, 'opened', 'system', 'opened', '2026-07-18T00:00:00Z');
            """
        )

        prepare_runtime_schema(connection)

        assert connection.execute(
            "SELECT incident_id FROM monitor_alert_audit WHERE audit_id=8"
        ).fetchone()[0] == 4
        foreign_keys = connection.execute(
            'PRAGMA foreign_key_list("monitor_alert_audit")'
        ).fetchall()
        assert [str(row[2]) for row in foreign_keys] == [
            "monitor_alert_incidents"
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert verify_runtime_schema(connection).version == (
            CURRENT_RUNTIME_SCHEMA_VERSION
        )
    finally:
        connection.close()


def test_current_version_digest_drift_is_rejected() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        connection.execute(
            "UPDATE runtime_schema_version SET contract_digest=?",
            ("0" * 64,),
        )
        connection.commit()

        with pytest.raises(RuntimeError, match="digest mismatch"):
            verify_runtime_schema(connection)
        with pytest.raises(RuntimeError, match="digest mismatch"):
            prepare_runtime_schema(connection)
    finally:
        connection.close()


def test_future_runtime_version_is_rejected() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        connection.execute(
            "UPDATE runtime_schema_version SET version=?",
            (CURRENT_RUNTIME_SCHEMA_VERSION + 1,),
        )
        connection.commit()

        with pytest.raises(RuntimeError, match="newer than supported"):
            runtime_schema_version(connection)
    finally:
        connection.close()


def test_runtime_verifier_never_attempts_schema_or_data_writes() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        prepare_runtime_schema(connection)
        prohibited = {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
        }
        attempted: list[int] = []

        def authorizer(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action in prohibited:
                attempted.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        assert verify_runtime_schema(connection).version == (
            CURRENT_RUNTIME_SCHEMA_VERSION
        )
        assert attempted == []
    finally:
        connection.close()


def test_database_verifier_defaults_to_runtime_and_core_only_is_explicit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prepared.db"
    prepared = prepare_database(database, tmp_path / "backups")
    assert prepared.runtime_schema_version == CURRENT_RUNTIME_SCHEMA_VERSION

    connection = sqlite3.connect(database)
    try:
        connection.execute("DROP TRIGGER monitor_alert_audit_no_update")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="runtime schema failed"):
        verify_prepared_database(database)

    core = verify_prepared_database(database, core_only=True)
    assert core.runtime_schema_version == 0
