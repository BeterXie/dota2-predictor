from __future__ import annotations

import os
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from event_intelligence.storage import (
    CURRENT_SCHEMA_VERSION as INTELLIGENCE_SCHEMA_VERSION,
)
from live_betting.health import read_health, record_health
from live_betting.storage import CURRENT_SCHEMA_VERSION as LIVE_SCHEMA_VERSION
from live_betting.storage import LiveBettingStore
from scripts.run_dota_shadow_service import (
    _DATABASE_HEALTH_CACHE,
    SingleInstanceLock,
    _commands,
    _companion_health,
    _database_health,
    _periodic_database_health,
    main,
    service_once,
)


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
        self.assertEqual(
            connection.execute.call_args_list[0].args[0], "PRAGMA quick_check"
        )

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
            self.assertFalse(report.with_name(f".{report.name}.tmp").exists())
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

    def test_missing_smtp_overrides_stale_optional_mail_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            stale = datetime.now(timezone.utc) - timedelta(days=1)
            with LiveBettingStore(database) as store:
                store.init_schema()
                record_health(
                    store.connection,
                    "mail_worker",
                    "degraded",
                    heartbeat_at=stale,
                    error_at=stale,
                    error="configuration_missing",
                    details={"source": "worker"},
                )

            with patch.dict(os.environ, {}, clear=True):
                service_once(database)

            with LiveBettingStore(database) as store:
                mail = next(
                    row for row in read_health(store.connection)
                    if row["component"] == "mail"
                )
            self.assertEqual(mail["status"], "degraded")
            self.assertEqual(mail["last_error"], "configuration_missing")
            self.assertFalse(mail["details"]["smtp_configured"])

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

    def test_supervisor_commands_include_vision_strict_ingest_and_postmatch(self) -> None:
        database = Path("service.db")
        commands = _commands(Namespace(
            database=database,
            start_collector=False,
            start_companion=True,
            start_shadow=False,
            start_vision=True,
            start_mail=False,
            start_strict_ingest=True,
            start_postmatch=True,
            vision_jsonl=None,
        ))
        self.assertEqual(
            set(commands), {"companion", "vision", "strict_ingest", "postmatch"}
        )
        self.assertIn("scripts/supervise_raybet_streams.py", commands["vision"])
        self.assertIn("scripts/run_strict_event_ingest.py", commands["strict_ingest"])
        self.assertIn("scripts/run_postmatch_labeler.py", commands["postmatch"])
        self.assertIn("--all", commands["postmatch"])

    def test_all_managed_workers_skip_redundant_schema_preparation(self) -> None:
        commands = _commands(Namespace(
            database=Path("service.db"),
            start_collector=True,
            start_companion=True,
            start_shadow=True,
            start_vision=True,
            start_mail=True,
            start_strict_ingest=True,
            start_postmatch=True,
            vision_jsonl=Path("vision.jsonl"),
        ))

        self.assertEqual(
            set(commands),
            {
                "collector",
                "companion",
                "shadow",
                "vision",
                "mail",
                "strict_ingest",
                "postmatch",
            },
        )
        for command in commands.values():
            self.assertIn("--schema-prepared", command)

    def test_supervisor_aggregates_new_worker_and_companion_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            now = datetime.now(timezone.utc)
            with LiveBettingStore(database) as store:
                store.init_schema()
                for component in (
                    "vision_worker",
                    "strict_ingest_worker",
                    "postmatch_worker",
                ):
                    record_health(
                        store.connection,
                        component,
                        "healthy",
                        heartbeat_at=now,
                        success_at=now,
                        details={"source": "worker"},
                    )
            service_once(
                database,
                active_components={
                    "vision",
                    "strict_ingest",
                    "postmatch",
                    "companion",
                },
                companion_probe=lambda: {"protocol_version": 1, "state": "ok"},
            )
            with LiveBettingStore(database) as store:
                statuses = {
                    row["component"]: row["status"]
                    for row in read_health(store.connection)
                }
            self.assertEqual(statuses["vision"], "healthy")
            self.assertEqual(statuses["strict_ingest"], "healthy")
            self.assertEqual(statuses["postmatch"], "healthy")
            self.assertEqual(statuses["companion"], "healthy")

    def test_companion_probe_has_one_startup_grace_cycle(self) -> None:
        def unavailable() -> dict[str, object]:
            raise OSError("not listening")

        self.assertEqual(
            _companion_health(True, unavailable, initial=True)[:2],
            ("starting", "awaiting_companion_health"),
        )
        self.assertEqual(
            _companion_health(True, unavailable, initial=False)[:2],
            ("unhealthy", "companion_unreachable"),
        )

    def test_database_preparation_finishes_before_any_child_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events: list[str] = []

            class Child:
                def poll(self) -> None:
                    return None

                def terminate(self) -> None:
                    events.append("terminate")

            def prepare(*args: object, **kwargs: object) -> Mock:
                events.append("prepare")
                return Mock(
                    backup=None,
                    live_schema_version=LIVE_SCHEMA_VERSION,
                    intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                )

            def spawn(*args: object, **kwargs: object) -> Child:
                events.append("spawn")
                return Child()

            def run_once(*args: object, **kwargs: object) -> dict[str, object]:
                events.append(
                    "service-no-init"
                    if kwargs.get("initialize_schema") is False
                    else "service"
                )
                return {"shadow": {"orders": {"signals": 0}}}

            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(root / "service.db"),
                "--lock",
                str(root / "service.lock"),
                "--once",
                "--migrate",
                "--start-companion",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.prepare_database",
                    side_effect=prepare,
                ),
                patch(
                    "scripts.run_dota_shadow_service.subprocess.Popen",
                    side_effect=spawn,
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    side_effect=run_once,
                ),
            ):
                self.assertEqual(main(), 0)

            self.assertLess(events.index("prepare"), events.index("spawn"))
            self.assertLess(events.index("spawn"), events.index("service-no-init"))

    def test_routine_supervisor_start_verifies_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events: list[str] = []

            class Child:
                def poll(self) -> None:
                    return None

                def terminate(self) -> None:
                    events.append("terminate")

            def verify(*args: object, **kwargs: object) -> Mock:
                events.append("verify")
                return Mock(
                    backup=None,
                    live_schema_version=LIVE_SCHEMA_VERSION,
                    intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                )

            def spawn(*args: object, **kwargs: object) -> Child:
                events.append("spawn")
                return Child()

            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(root / "service.db"),
                "--lock",
                str(root / "service.lock"),
                "--once",
                "--start-companion",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    side_effect=verify,
                ),
                patch(
                    "scripts.run_dota_shadow_service.prepare_database"
                ) as migrate,
                patch(
                    "scripts.run_dota_shadow_service.subprocess.Popen",
                    side_effect=spawn,
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    return_value={"shadow": {"orders": {"signals": 0}}},
                ),
            ):
                self.assertEqual(main(), 0)

            migrate.assert_not_called()
            self.assertLess(events.index("verify"), events.index("spawn"))

    def test_expensive_database_audit_is_cached_between_service_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            with LiveBettingStore(database) as store:
                store.init_schema()
                _DATABASE_HEALTH_CACHE.clear()
                with patch(
                    "scripts.run_dota_shadow_service._database_health",
                    wraps=_database_health,
                ) as audit:
                    first = _periodic_database_health(store.connection, NOW)
                    second = _periodic_database_health(
                        store.connection, NOW + timedelta(minutes=1)
                    )

            self.assertEqual(audit.call_count, 1)
            self.assertFalse(first[2]["audit_cached"])
            self.assertTrue(second[2]["audit_cached"])


if __name__ == "__main__":
    unittest.main()
