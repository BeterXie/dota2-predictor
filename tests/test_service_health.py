from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from live_betting.health import read_health, record_health
from scripts.run_dota_shadow_service import (
    SingleInstanceLock,
    _database_health,
    service_once,
)
from live_betting.storage import LiveBettingStore


NOW = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)


class ServiceHealthTests(unittest.TestCase):
    def test_database_integrity_failure_is_unhealthy(self) -> None:
        connection = Mock()
        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=[("page 7 is malformed",)])),
            Mock(fetchall=Mock(return_value=[])),
        ]
        status, reason, details = _database_health(connection)
        self.assertEqual(status, "unhealthy")
        self.assertEqual(reason, "integrity_check_failed")
        self.assertEqual(details["integrity"], ["page 7 is malformed"])

        connection.execute.side_effect = [
            Mock(fetchall=Mock(return_value=[("ok",)])),
            Mock(fetchall=Mock(return_value=[("orders", 1, "parent", 0)])),
        ]
        status, reason, details = _database_health(connection)
        self.assertEqual(status, "unhealthy")
        self.assertEqual(reason, "foreign_key_check_failed")
        self.assertEqual(details["foreign_key_issues"], 1)

    def test_health_upsert_preserves_last_success_and_sanitizes_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.db"
            with LiveBettingStore(path) as store:
                store.init_schema()
                record_health(
                    store.connection,
                    "collector",
                    "healthy",
                    heartbeat_at=NOW,
                    success_at=NOW,
                    details={"source": "direct"},
                )
                record_health(
                    store.connection,
                    "collector",
                    "degraded",
                    heartbeat_at=NOW,
                    error="line1\nline2",
                )
                rows = read_health(store.connection)
            self.assertEqual(rows[0]["status"], "degraded")
            self.assertEqual(rows[0]["last_error"], "line1 line2")
            self.assertEqual(rows[0]["last_success_at"], NOW.isoformat())

    def test_service_once_reports_mail_unhealthy_without_stopping_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            report = Path(directory) / "report.json"
            with patch.dict(os.environ, {}, clear=True):
                result = service_once(database, report)
            self.assertTrue(report.exists())
            self.assertIn("strict_scope", result["shadow"])
            with LiveBettingStore(database) as store:
                statuses = {
                    row["component"]: row["status"]
                    for row in read_health(store.connection)
                }
            self.assertEqual(statuses["database"], "healthy")
            self.assertEqual(statuses["mail"], "degraded")
            self.assertEqual(statuses["shadow"], "stopped")
            self.assertEqual(statuses["raybet"], "stopped")

    def test_fresh_worker_heartbeats_are_required_for_healthy_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            now = datetime.now(timezone.utc)
            with LiveBettingStore(database) as store:
                store.init_schema()
                for component in ("raybet_worker", "shadow_worker", "mail_worker"):
                    record_health(
                        store.connection,
                        component,
                        "healthy",
                        heartbeat_at=now,
                        success_at=now,
                        details={"source": "worker"},
                    )
                store.record_collector("raybet", success_at=now)
            environment = {
                "DOTA2_SMTP_SENDER": "sender@qq.com",
                "DOTA2_SMTP_AUTH_CODE": "test-only",
            }
            with patch.dict(os.environ, environment, clear=True):
                service_once(
                    database,
                    active_components={"collector", "shadow", "mail"},
                )
            with LiveBettingStore(database) as store:
                statuses = {
                    row["component"]: row["status"]
                    for row in read_health(store.connection)
                }
            self.assertEqual(statuses["raybet"], "healthy")
            self.assertEqual(statuses["shadow"], "healthy")
            self.assertEqual(statuses["mail"], "healthy")

    def test_stale_worker_heartbeat_is_not_refreshed_by_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            stale = datetime.now(timezone.utc) - timedelta(minutes=5)
            with LiveBettingStore(database) as store:
                store.init_schema()
                record_health(
                    store.connection,
                    "shadow_worker",
                    "healthy",
                    heartbeat_at=stale,
                    success_at=stale,
                    details={"source": "worker"},
                )
            with patch.dict(os.environ, {}, clear=True):
                service_once(database, active_components={"shadow"})
            with LiveBettingStore(database) as store:
                statuses = {
                    row["component"]: row["status"]
                    for row in read_health(store.connection)
                }
            self.assertEqual(statuses["shadow"], "unhealthy")

    def test_single_instance_lock_rejects_second_holder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.lock"
            with SingleInstanceLock(path):
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(path):
                        pass


if __name__ == "__main__":
    unittest.main()
