from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from live_betting.health import record_health
from live_betting.notifications import claim
from live_betting.smtp_delivery import SMTPConfig, build_message
from live_betting.storage import LiveBettingStore
from web.alerts import (
    acknowledge_alert,
    active_alerts,
    init_alert_schema,
    reconcile_alerts,
)


NOW = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)


class MonitorAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "alerts.db"
        self.store = LiveBettingStore(self.database)
        self.store.init_schema()
        init_alert_schema(self.store.connection)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_operational_alert_uses_grace_dedupes_and_recovers(self) -> None:
        record_health(
            self.store.connection,
            "raybet_worker",
            "degraded",
            heartbeat_at=NOW,
            error_at=NOW,
            error="timeout",
        )

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=30)
        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=29),
            grace_seconds=30,
        )
        self.assertEqual(active_alerts(self.store.connection), [])

        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=31),
            grace_seconds=30,
        )
        reconcile_alerts(
            self.store.connection,
            now=NOW + timedelta(seconds=95),
            grace_seconds=30,
        )
        alerts = active_alerts(self.store.connection)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["dedupe_key"], "operational:raybet_worker")
        self.assertEqual(alerts[0]["occurrence_count"], 2)

        recovered_at = NOW + timedelta(seconds=96)
        record_health(
            self.store.connection,
            "raybet_worker",
            "healthy",
            heartbeat_at=recovered_at,
            success_at=recovered_at,
        )
        reconcile_alerts(self.store.connection, now=recovered_at, grace_seconds=30)

        self.assertEqual(active_alerts(self.store.connection), [])
        self.assertEqual(
            [row[0] for row in self.store.connection.execute(
                "SELECT action FROM monitor_alert_audit ORDER BY audit_id"
            )],
            ["opened", "observed", "recovered"],
        )

    def test_missing_optional_smtp_does_not_remain_an_operational_alert(self) -> None:
        record_health(
            self.store.connection,
            "mail_worker",
            "unhealthy",
            heartbeat_at=NOW,
            error_at=NOW,
            error="smtp_authentication_failed",
        )
        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=0)
        self.assertEqual(
            active_alerts(self.store.connection)[0]["dedupe_key"],
            "operational:mail_worker",
        )

        later = NOW + timedelta(seconds=1)
        record_health(
            self.store.connection,
            "mail_worker",
            "degraded",
            heartbeat_at=later,
            error_at=later,
            error="configuration_missing",
        )
        reconcile_alerts(self.store.connection, now=later, grace_seconds=0)

        self.assertEqual(active_alerts(self.store.connection), [])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT status FROM monitor_alert_incidents WHERE dedupe_key=?",
                ("operational:mail_worker",),
            ).fetchone()[0],
            "recovered",
        )

    def test_paper_signal_opens_immediately_and_can_be_acknowledged(self) -> None:
        self.store.connection.execute(
            """INSERT INTO shadow_orders
               (order_key, raybet_match_id, odds_id, market_key, signaled_at,
                model_probability, market_probability, signal_price,
                signal_transport_key, signal_transport_at, expires_at,
                signal_odds_group_id, signal_outcome_key,
                signal_identity_verified, stake, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "order-alert", "match-1", "odds-1", "winner|map_1|team_one|",
                NOW.isoformat(), 0.58, 0.40, 2.5, "transport-1", NOW.isoformat(),
                (NOW + timedelta(seconds=15)).isoformat(), "group-1", "team_one",
                1, 1.0, "pending",
            ),
        )
        self.store.connection.commit()

        reconcile_alerts(self.store.connection, now=NOW, grace_seconds=30)
        alert = active_alerts(self.store.connection)[0]

        self.assertEqual(alert["category"], "paper_signal")
        self.assertTrue(
            acknowledge_alert(
                self.store.connection,
                incident_id=alert["incident_id"],
                actor="local-operator",
                acknowledged_at=NOW + timedelta(seconds=1),
            )
        )
        acknowledged = active_alerts(self.store.connection)[0]
        self.assertIsNotNone(acknowledged["acknowledged_at"])

    def test_email_outbox_uses_monitor_template_when_recipient_is_configured(self) -> None:
        record_health(
            self.store.connection,
            "vision_worker",
            "unhealthy",
            heartbeat_at=NOW,
            error_at=NOW,
            error="capture_failed",
        )
        reconcile_alerts(
            self.store.connection,
            now=NOW,
            grace_seconds=0,
            email_recipient="ops@example.com",
        )

        record = claim(self.store.connection, now=NOW)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.event_type, "monitor_alert")
        message = build_message(record, SMTPConfig("sender@example.com", "auth-code"))
        self.assertIn("监控告警", str(message["Subject"]))
        self.assertIn("capture_failed", message.get_content())

    def test_unacknowledged_alerts_sort_before_acknowledged_alerts(self) -> None:
        self.store.connection.executemany(
            """INSERT INTO monitor_alert_incidents
               (dedupe_key, episode, category, severity, title, body, status,
                first_detected_at, opened_at, last_detected_at,
                acknowledged_at, acknowledged_by, source_json, occurrence_count)
               VALUES (?, 1, 'operational', ?, ?, 'body', 'active',
                       ?, ?, ?, ?, ?, '{}', 1)""",
            (
                (
                    "acknowledged-critical", "critical", "acknowledged",
                    NOW.isoformat(), (NOW + timedelta(minutes=2)).isoformat(),
                    (NOW + timedelta(minutes=2)).isoformat(),
                    (NOW + timedelta(minutes=3)).isoformat(), "operator",
                ),
                (
                    "unacknowledged-warning", "warning", "unacknowledged",
                    NOW.isoformat(), NOW.isoformat(), NOW.isoformat(), None, None,
                ),
            ),
        )
        self.store.connection.commit()

        self.assertEqual(
            [alert["dedupe_key"] for alert in active_alerts(self.store.connection)],
            ["unacknowledged-warning", "acknowledged-critical"],
        )

    def test_current_outbox_schema_repairs_missing_artifacts(self) -> None:
        self.store.connection.execute(
            "DROP TRIGGER notification_outbox_payload_immutable"
        )
        self.store.connection.execute("DROP INDEX idx_notification_outbox_due")
        self.store.connection.commit()

        init_alert_schema(self.store.connection)

        objects = {
            (str(row[0]), str(row[1]))
            for row in self.store.connection.execute(
                """SELECT type, name FROM sqlite_master
                    WHERE name IN ('idx_notification_outbox_due',
                                   'notification_outbox_payload_immutable')"""
            )
        }
        self.assertEqual(
            objects,
            {
                ("index", "idx_notification_outbox_due"),
                ("trigger", "notification_outbox_payload_immutable"),
            },
        )

    def test_legacy_outbox_migration_is_atomic_and_retryable(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                """CREATE TABLE notification_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_key TEXT NOT NULL,
                    event_type TEXT NOT NULL
                        CHECK (event_type IN ('filled', 'settled')),
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
                )"""
            )
            connection.execute(
                """INSERT INTO notification_outbox
                   (outbox_id, order_key, event_type, channel, status, recipient,
                    message_id, payload_json, statistics_cutoff,
                    template_version, attempt_count, created_at, updated_at)
                   VALUES (7, 'order-7', 'filled', 'email', 'pending',
                           'ops@example.com', '<message-7@example.com>', '{}',
                           ?, 'legacy-v1', 0, ?, ?)""",
                (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
            )
            connection.commit()

            class FaultingConnection:
                def __init__(self, wrapped: sqlite3.Connection) -> None:
                    self.wrapped = wrapped

                def execute(
                    self, sql: str, parameters: tuple[object, ...] = ()
                ) -> sqlite3.Cursor:
                    if sql.lstrip().startswith(
                        "CREATE INDEX idx_notification_outbox_due"
                    ):
                        raise sqlite3.OperationalError("injected index failure")
                    return self.wrapped.execute(sql, parameters)

                def __getattr__(self, name: str):
                    return getattr(self.wrapped, name)

            with self.assertRaises(sqlite3.OperationalError):
                init_alert_schema(FaultingConnection(connection))  # type: ignore[arg-type]

            legacy_sql = str(connection.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='notification_outbox'"""
            ).fetchone()[0])
            self.assertNotIn("monitor_alert", legacy_sql)
            self.assertEqual(
                connection.execute(
                    "SELECT outbox_id FROM notification_outbox"
                ).fetchone()[0],
                7,
            )
            self.assertIsNone(connection.execute(
                """SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='notification_outbox_monitor_v1'"""
            ).fetchone())

            init_alert_schema(connection)

            upgraded_sql = str(connection.execute(
                """SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='notification_outbox'"""
            ).fetchone()[0])
            self.assertIn("monitor_alert", upgraded_sql)
            self.assertEqual(
                connection.execute(
                    "SELECT outbox_id FROM notification_outbox"
                ).fetchone()[0],
                7,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
