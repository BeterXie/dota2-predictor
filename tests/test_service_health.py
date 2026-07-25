from __future__ import annotations

import os
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from io import StringIO
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

import scripts.run_dota_shadow_service as service_module

from event_intelligence.storage import (
    CURRENT_SCHEMA_VERSION as INTELLIGENCE_SCHEMA_VERSION,
)
from live_betting.health import read_health, record_health
from live_betting.monitor import resolve_data_paths as resolve_collector_data_paths
from live_betting.runtime_schema import (
    CURRENT_RUNTIME_SCHEMA_VERSION,
    prepare_runtime_schema,
)
from live_betting.service_coordination import (
    DatabaseFileIdentity,
    ProcessIdentity,
    TerminationResult,
    WriterScanResult,
    database_service_authority_lock_paths,
    database_writer_authority,
    managed_child_target,
    require_unique_database_file,
    scan_managed_writers,
)
from live_betting.storage import CURRENT_SCHEMA_VERSION as LIVE_SCHEMA_VERSION
from live_betting.storage import LiveBettingStore
from scripts.run_dota_shadow_service import (
    _ChildRestartState,
    _DATABASE_AUDIT_THREADS,
    _DATABASE_HEALTH_CACHE,
    _ReportWorker,
    SingleInstanceLock,
    _commands,
    _companion_health,
    _capture_subprocess_tree_identities,
    _database_health,
    _periodic_database_health,
    _reconcile_managed_children,
    _replacement_authority_gate,
    _restart_sleep_seconds,
    _run_database_audit,
    _shutdown_children_under_authority,
    main,
    service_once,
)


NOW = datetime(2026, 7, 14, 2, 0, tzinfo=timezone.utc)
DRAFT_DEPLOYMENT_KEY = "a" * 64


class _MutableProcess:
    def __init__(
        self,
        pid: int,
        created_at: float,
        descendants: list["_MutableProcess"] | None = None,
    ) -> None:
        self.pid = pid
        self.created_at = created_at
        self.descendants = descendants or []
        self.running = True
        self.status_value = "running"

    def create_time(self) -> float:
        return self.created_at

    def children(self, recursive: bool = False) -> list["_MutableProcess"]:
        return list(self.descendants)

    def is_running(self) -> bool:
        return self.running

    def status(self) -> str:
        return self.status_value


def _terminate_fake_tree(process: object) -> TerminationResult:
    process.terminate()  # type: ignore[attr-defined]
    return TerminationResult(True)


class ServiceHealthTests(unittest.TestCase):
    def test_subtree_capture_rejects_five_millisecond_root_change(self) -> None:
        class Process:
            pid = 991_100

            def __init__(self) -> None:
                self.identity_reads = 0

            def create_time(self) -> float:
                self.identity_reads += 1
                return 10.0 if self.identity_reads == 1 else 10.005

            @staticmethod
            def children(recursive: bool = False) -> list[object]:
                return []

        process = Process()
        handle = Mock(pid=process.pid)
        handle.poll.return_value = None

        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            _capture_subprocess_tree_identities(
                handle,
                process_factory=lambda _pid: process,
            )

    def test_subtree_capture_retries_a_descendant_that_exits_mid_read(self) -> None:
        class Descendant:
            pid = 991_201

            @staticmethod
            def is_running() -> bool:
                raise psutil.NoSuchProcess(991_201)

        class Process:
            pid = 991_200

            def __init__(self) -> None:
                self.child_reads = 0

            @staticmethod
            def create_time() -> float:
                return 20.0

            def children(self, recursive: bool = False) -> list[object]:
                self.child_reads += 1
                return [Descendant()] if self.child_reads == 1 else []

        process = Process()
        handle = Mock(pid=process.pid)
        handle.poll.return_value = None

        self.assertEqual(
            _capture_subprocess_tree_identities(
                handle,
                process_factory=lambda _pid: process,
            ),
            (ProcessIdentity(process.pid, 20.0),),
        )

    def test_subtree_capture_retries_nonrunning_and_zombie_descendants(
        self,
    ) -> None:
        for mode in ("nonrunning", "zombie"):
            with self.subTest(mode=mode):
                class Descendant:
                    pid = 991_301

                    @staticmethod
                    def is_running() -> bool:
                        return mode != "nonrunning"

                    @staticmethod
                    def status() -> str:
                        return psutil.STATUS_ZOMBIE if mode == "zombie" else "running"

                    @staticmethod
                    def create_time() -> float:
                        return 31.0

                class Process:
                    pid = 991_300

                    def __init__(self) -> None:
                        self.child_reads = 0

                    @staticmethod
                    def create_time() -> float:
                        return 30.0

                    def children(self, recursive: bool = False) -> list[object]:
                        self.child_reads += 1
                        return [Descendant()] if self.child_reads == 1 else []

                process = Process()
                handle = Mock(pid=process.pid)
                handle.poll.return_value = None

                self.assertEqual(
                    _capture_subprocess_tree_identities(
                        handle,
                        process_factory=lambda _pid: process,
                    ),
                    (ProcessIdentity(process.pid, 30.0),),
                )

    def test_database_file_identity_rejects_hardlinks_and_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "authority.db"
            alias = Path(directory) / "authority-alias.db"
            database.write_bytes(b"first")
            identity = require_unique_database_file(database)
            self.assertIsNotNone(identity)

            os.link(database, alias)
            self.assertTrue(os.path.samefile(database, alias))
            self.assertNotEqual(
                database.with_suffix(".service.lock"),
                alias.with_suffix(".service.lock"),
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
                require_unique_database_file(database)
            alias.unlink()

            database.unlink()
            database.write_bytes(b"replacement")
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                require_unique_database_file(
                    database,
                    expected_identity=identity,
                )

    def test_database_file_identity_allows_only_explicit_missing_files(self) -> None:
        missing = Path("missing-authority.db")
        self.assertIsNone(
            require_unique_database_file(missing, allow_missing=True)
        )
        with self.assertRaises(FileNotFoundError):
            require_unique_database_file(missing)

    def test_collector_default_raw_root_follows_selected_database(self) -> None:
        database = Path("candidate") / "collector.db"

        args = resolve_collector_data_paths(Namespace(
            database=database,
            raw_dir=None,
        ))

        self.assertEqual(args.database, database.resolve())
        self.assertEqual(
            args.raw_dir,
            database.resolve().parent / "live_betting" / "raw-v2",
        )

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
                prepare_runtime_schema(store.connection)
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
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)
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
            self.assertEqual(result["market_source_policy"], "direct_primary")
            self.assertFalse(result["capabilities"]["browser_compare"]["required"])

    def test_unconfigured_companion_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)

            service_once(database, active_components=set())

            with LiveBettingStore(database) as store:
                companion = next(
                    row for row in read_health(store.connection)
                    if row["component"] == "companion"
                )
            self.assertEqual(companion["status"], "stopped")
            self.assertIsNone(companion["last_error"])
            self.assertIsNone(companion["last_error_at"])
            self.assertFalse(companion["details"]["configured"])
            self.assertEqual(
                companion["details"]["reason"], "not_started_by_supervisor"
            )
            self.assertEqual(companion["details"]["readiness_impact"], "none")
            self.assertEqual(
                companion["details"]["market_source_policy"], "direct_primary"
            )

    def test_health_only_cycle_skips_slow_report_builders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            report = Path(directory) / "report.json"
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("scripts.run_dota_shadow_service.build_report") as shadow,
                patch(
                    "scripts.run_dota_shadow_service.build_intelligence_report"
                ) as intelligence,
            ):
                result = service_once(database, report, health_only=True)

            shadow.assert_not_called()
            intelligence.assert_not_called()
            self.assertEqual(result, {"pending_orders": 0})
            self.assertFalse(report.exists())
            with LiveBettingStore(database) as store:
                self.assertTrue(read_health(store.connection))
            for thread in list(_DATABASE_AUDIT_THREADS.values()):
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def test_health_only_cycle_does_not_wait_for_database_audit(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_audit(*args: object) -> tuple[str, None, dict[str, object]]:
            started.set()
            self.assertTrue(release.wait(5))
            return "healthy", None, {
                "integrity": "ok",
                "foreign_key_issues": 0,
            }

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)
            _DATABASE_HEALTH_CACHE.clear()
            _DATABASE_AUDIT_THREADS.clear()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch(
                    "scripts.run_dota_shadow_service._database_health",
                    side_effect=slow_audit,
                ) as audit,
            ):
                thread = None
                try:
                    first = service_once(
                        database,
                        initialize_schema=False,
                        health_only=True,
                    )
                    self.assertTrue(started.wait(2))
                    second = service_once(
                        database,
                        initialize_schema=False,
                        health_only=True,
                    )
                    self.assertEqual(first, {"pending_orders": 0})
                    self.assertEqual(second, {"pending_orders": 0})
                    self.assertEqual(audit.call_count, 1)
                    with LiveBettingStore(database) as store:
                        database_health = next(
                            row for row in read_health(store.connection)
                            if row["component"] == "database"
                        )
                    self.assertEqual(database_health["status"], "starting")
                    self.assertEqual(
                        database_health["details"]["audit_checked_at"], None
                    )
                    thread = next(iter(_DATABASE_AUDIT_THREADS.values()))
                finally:
                    release.set()
                    for running in list(_DATABASE_AUDIT_THREADS.values()):
                        running.join(5)
                if thread is None:
                    self.fail("database audit thread was not started")
                self.assertFalse(thread.is_alive())

                service_once(
                    database,
                    initialize_schema=False,
                    health_only=True,
                )

            with LiveBettingStore(database) as store:
                database_health = next(
                    row for row in read_health(store.connection)
                    if row["component"] == "database"
                )
            self.assertEqual(database_health["status"], "healthy")
            self.assertTrue(database_health["details"]["audit_cached"])

    def test_report_worker_is_single_flight(self) -> None:
        started = threading.Event()
        release = threading.Event()
        clock = [0.0]

        def slow_report(*args: object) -> None:
            started.set()
            self.assertTrue(release.wait(2))

        worker = _ReportWorker(
            Path("service.db"),
            Path("report.json"),
            report_interval=300.0,
            monotonic=lambda: clock[0],
        )
        with patch(
            "scripts.run_dota_shadow_service._generate_service_report",
            side_effect=slow_report,
        ) as generate:
            self.assertTrue(worker.start_if_idle())
            self.assertTrue(started.wait(2))
            self.assertFalse(worker.start_if_idle())
            release.set()
            self.assertTrue(worker.wait(2))
            self.assertFalse(worker.start_if_idle())
            clock[0] = 299.0
            self.assertFalse(worker.start_if_idle())
            clock[0] = 300.0
            self.assertTrue(worker.start_if_idle())
            self.assertTrue(worker.wait(2))

        self.assertEqual(generate.call_count, 2)
        generate.assert_called_with(Path("service.db"), Path("report.json"))

    def test_database_audit_uses_read_only_connection_and_closes_on_failure(
        self,
    ) -> None:
        database = Path("audit.db").resolve()
        connection = Mock()
        _DATABASE_HEALTH_CACHE.clear()
        with (
            patch(
                "scripts.run_dota_shadow_service.sqlite3.connect",
                return_value=connection,
            ) as connect,
            patch(
                "scripts.run_dota_shadow_service._database_health",
                side_effect=RuntimeError("audit failed"),
            ),
        ):
            _run_database_audit(str(database), database)

        connect.assert_called_once_with(
            f"{database.as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        connection.execute.assert_called_once_with("PRAGMA busy_timeout=1000")
        connection.close.assert_called_once_with()
        cached = _DATABASE_HEALTH_CACHE[str(database)][1]
        self.assertEqual(cached[0], "unhealthy")
        self.assertEqual(cached[1], "database_audit_failed")

    def test_report_worker_failure_does_not_escape_and_can_retry(self) -> None:
        worker = _ReportWorker(
            Path("service.db"), Path("report.json"), report_interval=0.0
        )
        with (
            patch(
                "scripts.run_dota_shadow_service._generate_service_report",
                side_effect=RuntimeError("report failed"),
            ) as generate,
            patch("scripts.run_dota_shadow_service.sys.stderr"),
        ):
            self.assertTrue(worker.start_if_idle())
            self.assertTrue(worker.wait(2))
            self.assertEqual(worker.last_error, "RuntimeError: report failed")
            self.assertTrue(worker.start_if_idle())
            self.assertTrue(worker.wait(2))

        self.assertEqual(generate.call_count, 2)

    def test_missing_smtp_overrides_stale_optional_mail_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            stale = datetime.now(timezone.utc) - timedelta(days=1)
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)
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
                prepare_runtime_schema(store.connection)
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
                prepare_runtime_schema(store.connection)
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

    def test_stale_lock_text_never_blocks_and_probes_do_not_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.lock"
            path.write_bytes(b"stale-pid-text" * 20)

            with SingleInstanceLock(path):
                pass
            first = path.read_bytes()
            with SingleInstanceLock(path):
                pass
            second = path.read_bytes()

            self.assertEqual(len(first), 4096)
            self.assertEqual(len(second), 4096)
            first_owner = json.loads(first.rstrip(b" ").decode("ascii"))
            second_owner = json.loads(second.rstrip(b" ").decode("ascii"))
            self.assertEqual(first_owner["pid"], os.getpid())
            self.assertEqual(second_owner["pid"], os.getpid())
            self.assertNotEqual(first_owner["nonce"], second_owner["nonce"])

    def test_writer_scan_fails_closed_for_opaque_python_process(self) -> None:
        opaque = Mock(
            info={
                "pid": 4210,
                "name": "python.exe",
                "cmdline": None,
                "create_time": None,
            }
        )

        result = scan_managed_writers(
            Path("candidate.db"),
            process_iter=lambda _: [opaque],
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4210,))

    def test_writer_scan_drops_unverifiable_process_that_exited(self) -> None:
        opaque = Mock(
            info={
                "pid": 4218,
                "name": "python.exe",
                "cmdline": None,
                "create_time": 109.0,
            }
        )

        def exited(pid: int) -> object:
            raise psutil.NoSuchProcess(pid)

        result = scan_managed_writers(
            Path("candidate.db"),
            process_iter=lambda _: [opaque],
            process_factory=exited,
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, ())

    def test_writer_scan_keeps_living_unreadable_python_process(self) -> None:
        opaque = Mock(
            info={
                "pid": 4219,
                "name": "python.exe",
                "cmdline": None,
                "create_time": 110.0,
            }
        )
        factory_calls: list[int] = []

        def still_running(pid: int) -> object:
            factory_calls.append(pid)
            return opaque

        result = scan_managed_writers(
            Path("candidate.db"),
            process_iter=lambda _: [opaque],
            process_factory=still_running,
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4219,))
        self.assertEqual(factory_calls, [4219, 4219])

    def test_writer_scan_reclassifies_reused_pid(self) -> None:
        database = Path("candidate.db")
        opaque = Mock(
            info={
                "pid": 4223,
                "name": "python.exe",
                "cmdline": None,
                "create_time": 111.0,
            }
        )
        replacement = Mock(
            info={
                "pid": 4223,
                "name": "python.exe",
                "cmdline": [
                    "python",
                    "-m",
                    "live_betting.monitor",
                    "--database",
                    str(database.resolve()),
                ],
                "create_time": 112.0,
            }
        )

        result = scan_managed_writers(
            database,
            process_iter=lambda _: [opaque],
            process_factory=lambda _: replacement,
        )

        self.assertEqual(result.conflicts, (ProcessIdentity(4223, 112.0),))
        self.assertEqual(result.unverifiable_pids, ())

    def test_writer_scan_recovers_pid_name_and_classifies_access_denied(self) -> None:
        class ProtectedProcess:
            def __init__(self, pid: int, name: str) -> None:
                self.pid = pid
                self._name = name

            @property
            def info(self) -> dict[str, object]:
                raise psutil.AccessDenied(self.pid)

            def name(self) -> str:
                return self._name

            def exe(self) -> str:
                return self._name

            def cmdline(self) -> list[str]:
                raise psutil.AccessDenied(self.pid)

        protected_python = ProtectedProcess(4211, "python.exe")
        protected_system = ProtectedProcess(4212, "System")

        result = scan_managed_writers(
            Path("candidate.db"),
            process_iter=lambda _: [protected_python, protected_system],
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4211,))

    def test_writer_scan_keeps_opaque_user_process_but_skips_protected_system(
        self,
    ) -> None:
        class OpaqueProcess:
            def __init__(self, pid: int, parent_pid: int) -> None:
                self.pid = pid
                self.parent_pid = parent_pid

            @property
            def info(self) -> dict[str, object]:
                raise psutil.AccessDenied(self.pid)

            def name(self) -> str:
                raise psutil.AccessDenied(self.pid)

            def exe(self) -> str:
                raise psutil.AccessDenied(self.pid)

            def cmdline(self) -> list[str]:
                raise psutil.AccessDenied(self.pid)

            def ppid(self) -> int:
                return self.parent_pid

        opaque_user = OpaqueProcess(4214, 500)
        protected_system = OpaqueProcess(4215, 4)

        with patch("live_betting.service_coordination.os.name", "nt"):
            result = scan_managed_writers(
                Path("candidate.db"),
                process_iter=lambda _: [opaque_user, protected_system],
            )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4214,))

    def test_writer_scan_rejects_relative_database_without_guessing_cwd(self) -> None:
        relative = Mock(
            info={
                "pid": 4213,
                "name": "python.exe",
                "cmdline": [
                    "python",
                    "-m",
                    "live_betting.monitor",
                    "--database",
                    "relative.db",
                ],
                "create_time": 104.0,
            }
        )

        result = scan_managed_writers(
            Path("candidate.db"),
            process_iter=lambda _: [relative],
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4213,))

    def test_writer_scan_rejects_duplicate_database_arguments(self) -> None:
        database = Path("candidate.db")
        duplicate = Mock(
            info={
                "pid": 4217,
                "name": "python.exe",
                "cmdline": [
                    "python",
                    "-m",
                    "live_betting.monitor",
                    "--database",
                    str((Path("other.db")).resolve()),
                    "--database",
                    str(database.resolve()),
                ],
                "create_time": 108.0,
            }
        )

        result = scan_managed_writers(
            database,
            process_iter=lambda _: [duplicate],
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4217,))

    def test_writer_scan_rejects_hardlinked_expected_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "candidate.db"
            alias = Path(directory) / "candidate-alias.db"
            database.write_bytes(b"sqlite")
            os.link(database, alias)
            iterator_called = False

            def processes(_: object) -> list[object]:
                nonlocal iterator_called
                iterator_called = True
                return []

            with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
                scan_managed_writers(database, process_iter=processes)
            self.assertFalse(iterator_called)

    def test_writer_scan_rejects_database_appearing_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "appeared.db"

            def processes(_: object) -> list[object]:
                database.write_bytes(b"sqlite")
                return []

            with self.assertRaisesRegex(
                RuntimeError,
                "identity changed during writer scan",
            ):
                scan_managed_writers(database, process_iter=processes)

    def test_writer_scan_rejects_known_writer_without_explicit_database(self) -> None:
        missing_database = Mock(
            info={
                "pid": 4216,
                "name": "python.exe",
                "cmdline": [
                    "python",
                    "scripts/run_dota_shadow_service.py",
                    "--once",
                ],
                "create_time": 105.0,
            }
        )

        result = scan_managed_writers(
            Path("candidate.db"),
            process_iter=lambda _: [missing_database],
        )

        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.unverifiable_pids, (4216,))

    def test_external_writer_scan_matches_only_the_exact_database(self) -> None:
        database = Path("candidate") / "dota2.db"
        exact = Mock(
            info={
                "pid": 4201,
                "name": "python.exe",
                "create_time": 101.0,
                "cmdline": [
                    "python",
                    "-m",
                    "live_betting.monitor",
                    "--database",
                    str(database.resolve()),
                ],
            }
        )
        other_database = Mock(
            info={
                "pid": 4202,
                "name": "python.exe",
                "create_time": 102.0,
                "cmdline": [
                    "python",
                    "scripts/run_comeback_shadow.py",
                    "--database",
                    str((Path("other") / "dota2.db").resolve()),
                ],
            }
        )
        unrelated = Mock(
            info={
                "pid": 4203,
                "name": "python.exe",
                "create_time": 103.0,
                "cmdline": [
                    "python",
                    "-m",
                    "tools.readonly",
                    "--database",
                    str(database.resolve()),
                ],
            }
        )
        result = scan_managed_writers(
            database,
            process_iter=lambda _: [exact, other_database, unrelated],
        )
        self.assertEqual(result.conflicts, (ProcessIdentity(4201, 101.0),))
        self.assertEqual(result.unverifiable_pids, ())

    def test_writer_scan_recognizes_wrappers_but_ignores_read_only_web(self) -> None:
        database = Path("candidate") / "dota2.db"
        processes = [
            Mock(info={
                "pid": 4204,
                "name": "python.exe",
                "create_time": 106.0,
                "cmdline": [
                    "python",
                    "-m",
                    "scripts.run_comeback_shadow",
                    "--database",
                    str(database.resolve()),
                ],
            }),
            Mock(info={
                "pid": 4205,
                "name": "python.exe",
                "create_time": 107.0,
                "cmdline": [
                    "python",
                    "-m",
                    "web.main",
                    "--database",
                    str(database.resolve()),
                ],
            }),
        ]

        result = scan_managed_writers(
            database,
            process_iter=lambda _: processes,
        )

        self.assertEqual(result.conflicts, (ProcessIdentity(4204, 106.0),))
        self.assertEqual(result.unverifiable_pids, ())

    def test_supervisor_refuses_orphaned_writer_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(root / "candidate.db"),
                "--lock",
                str(root / "candidate.service.lock"),
                "--once",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers",
                    return_value=WriterScanResult(
                        (ProcessIdentity(4201, 101.0),),
                        (),
                    ),
                ),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database"
                ) as verify,
                patch(
                    "scripts.run_dota_shadow_service.subprocess.Popen"
                ) as spawn,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "managed writers already target this database: 4201",
                ):
                    main()

            verify.assert_not_called()
            spawn.assert_not_called()

    def test_supervisor_rejects_database_appearing_before_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "appeared.db"
            identity = DatabaseFileIdentity(
                database.resolve(),
                1,
                2,
            )
            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--once",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.require_unique_database_file",
                    side_effect=[None, identity],
                ),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers"
                ) as scan,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "identity changed before service lock",
                ):
                    main()

            scan.assert_not_called()

    def test_custom_lock_cannot_replace_standard_database_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "candidate.db"
            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--lock",
                str(root / "custom.lock"),
                "--once",
            ]
            with (
                SingleInstanceLock(database.with_suffix(".service.lock")),
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers"
                ) as scan,
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database"
                ) as verify,
                patch(
                    "scripts.run_dota_shadow_service.subprocess.Popen"
                ) as spawn,
            ):
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    main()

            scan.assert_not_called()
            verify.assert_not_called()
            spawn.assert_not_called()

    def test_supervisor_proves_child_shutdown_before_releasing_standard_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "service.db"
            database.touch()
            standard_lock = database.with_suffix(".service.lock")
            custom_lock = root / "custom.lock"
            lock_was_held: list[bool] = []

            class Child:
                def poll(self) -> None:
                    return None

            def terminate_tree(_: object) -> TerminationResult:
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(standard_lock):
                        pass
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(custom_lock):
                        pass
                lock_was_held.append(True)
                return TerminationResult(True)

            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--lock",
                str(custom_lock),
                "--once",
                "--start-companion",
            ]
            preparation = Mock(
                backup=None,
                live_schema_version=LIVE_SCHEMA_VERSION,
                intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
            )
            output = StringIO()
            with (
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", output),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers",
                    return_value=WriterScanResult((), ()),
                ),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    return_value=preparation,
                ),
                patch(
                    "scripts.run_dota_shadow_service.subprocess.Popen",
                    return_value=Child(),
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    return_value=ProcessIdentity(1, 1.0),
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    return_value={"shadow": {"orders": {"signals": 0}}},
                ),
                patch(
                    "scripts.run_dota_shadow_service.terminate_subprocess_tree",
                    side_effect=terminate_tree,
                ),
            ):
                self.assertEqual(main(), 0)

            self.assertEqual(lock_was_held, [True])
            payload = json.loads(output.getvalue())
            self.assertEqual(
                payload["schema_versions"],
                {
                    "live": LIVE_SCHEMA_VERSION,
                    "intelligence": INTELLIGENCE_SCHEMA_VERSION,
                    "runtime": CURRENT_RUNTIME_SCHEMA_VERSION,
                },
            )
            with SingleInstanceLock(standard_lock), SingleInstanceLock(custom_lock):
                pass

    def test_first_exited_child_gate_preserves_marker_for_live_writer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "replacement.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            exited = Mock()
            exited.poll.return_value = 1
            authority = Mock()
            authority.__exit__ = Mock(return_value=None)
            exited._dota2_manager_authority_context = authority
            exited._dota2_manager_authority_cleanup_error = None
            children = {"collector": exited}
            restart_states: dict[str, _ChildRestartState] = {}
            descendant = ProcessIdentity(9001, 11.0)
            allowed_scans: list[tuple[ProcessIdentity, ...]] = []

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                allowed_scans.append(allowed_identities)
                return WriterScanResult((descendant,), ())

            def gate(*args: object) -> TerminationResult:
                return _replacement_authority_gate(
                    *args,  # type: ignore[arg-type]
                    writer_scanner=scan,
                )
            spawn = Mock()

            result = _reconcile_managed_children(
                children,
                {"collector": ["python", "collector.py"]},
                database,
                identity,
                restart_states=restart_states,
                monotonic=lambda: 10.0,
                authority_gate=gate,
                popen_factory=spawn,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "orphan_writer_conflict:9001")
            self.assertIs(children["collector"], exited)
            self.assertEqual(allowed_scans, [()])
            authority.__exit__.assert_not_called()
            self.assertEqual(restart_states["collector"], _ChildRestartState())
            spawn.assert_not_called()

    def test_reconcile_allows_watcher_churn_before_publisher_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-watcher-churn.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            old_watcher = _MutableProcess(9012, 12.0)
            new_watcher = _MutableProcess(9013, 13.0)
            vision_root = _MutableProcess(9011, 11.0, [old_watcher])
            vision = Mock(pid=vision_root.pid)
            vision.poll.return_value = None
            publisher = Mock(pid=9021)
            publisher.poll.return_value = 1
            publisher_authority = Mock()
            publisher_authority.__exit__ = Mock(return_value=None)
            publisher._dota2_manager_authority_context = publisher_authority
            publisher._dota2_manager_authority_cleanup_error = None
            replacement = Mock(pid=9022)
            replacement.poll.return_value = None
            replacement_authority = Mock()
            replacement_authority.__enter__ = Mock(
                return_value={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}
            )
            replacement_authority.__exit__ = Mock(return_value=None)
            children = {"vision": vision, "draft_publisher": publisher}
            commands = {
                "vision": ["python", "vision.py"],
                "draft_publisher": ["python", "publisher.py"],
            }
            states = {
                "vision": _ChildRestartState(started_at=0.0),
                "draft_publisher": _ChildRestartState(started_at=0.0),
            }
            clock = [10.0]
            scan_count = 0
            events: list[str] = []

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                nonlocal scan_count
                scan_count += 1
                events.append(f"scan:{scan_count}")
                if scan_count == 1:
                    vision_root.descendants.clear()
                elif scan_count == 2:
                    vision_root.descendants.append(new_watcher)
                    return WriterScanResult(
                        (ProcessIdentity(new_watcher.pid, new_watcher.created_at),),
                        (),
                    )
                return WriterScanResult((), ())

            def process_factory(pid: int) -> object:
                if pid == vision_root.pid:
                    return vision_root
                if pid == new_watcher.pid:
                    return new_watcher
                raise psutil.NoSuchProcess(pid)

            def gate(*args: object) -> TerminationResult:
                return _replacement_authority_gate(
                    *args,  # type: ignore[arg-type]
                    process_factory=process_factory,
                    writer_scanner=scan,
                )

            def spawn(*_: object, **__: object) -> object:
                events.append("spawn")
                return replacement

            first = _reconcile_managed_children(
                children,
                commands,
                database,
                identity,
                restart_states=states,
                monotonic=lambda: clock[0],
                authority_gate=gate,
                popen_factory=spawn,
            )

            self.assertTrue(first.ok, first.detail)
            self.assertEqual(states["draft_publisher"].next_restart_at, 11.0)
            self.assertNotIn("spawn", events)

            clock[0] = 11.0
            with (
                patch(
                    "scripts.run_dota_shadow_service.manager_child_authority",
                    return_value=replacement_authority,
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    return_value=ProcessIdentity(replacement.pid, 14.0),
                ),
            ):
                restarted = _reconcile_managed_children(
                    children,
                    commands,
                    database,
                    identity,
                    restart_states=states,
                    monotonic=lambda: clock[0],
                    authority_gate=gate,
                    popen_factory=spawn,
                )

            self.assertTrue(restarted.ok, restarted.detail)
            self.assertIs(
                children["draft_publisher"]._process_handle,  # type: ignore[attr-defined]
                replacement,
            )
            self.assertEqual(events, ["scan:1", "scan:2", "scan:3", "scan:4", "spawn"])

    def test_exited_child_restarts_after_backoff_without_touching_peer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "isolated-restart.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            exited = Mock()
            exited.poll.return_value = 9
            peer = Mock()
            peer.poll.return_value = None
            replacement = Mock(pid=8801)
            replacement.poll.return_value = None
            children = {"collector": exited, "companion": peer}
            states = {
                "collector": _ChildRestartState(started_at=0.0),
                "companion": _ChildRestartState(started_at=10.0),
            }
            now = [10.0]
            spawn = Mock(return_value=replacement)
            authority = Mock()
            authority.__enter__ = Mock(
                return_value={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}
            )
            authority.__exit__ = Mock(return_value=None)

            first = _reconcile_managed_children(
                children,
                {"collector": ["python", "collector.py"]},
                database,
                identity,
                restart_states=states,
                monotonic=lambda: now[0],
                authority_gate=lambda *_: TerminationResult(True),
                popen_factory=spawn,
            )
            self.assertTrue(first.ok)
            self.assertIs(children["collector"], exited)
            self.assertIs(children["companion"], peer)
            self.assertEqual(states["collector"].next_restart_at, 11.0)
            spawn.assert_not_called()

            now[0] = 10.999
            deferred = _reconcile_managed_children(
                children,
                {"collector": ["python", "collector.py"]},
                database,
                identity,
                restart_states=states,
                monotonic=lambda: now[0],
                authority_gate=lambda *_: TerminationResult(True),
                popen_factory=spawn,
            )
            self.assertTrue(deferred.ok)
            spawn.assert_not_called()

            now[0] = 11.0
            with (
                patch(
                    "scripts.run_dota_shadow_service.manager_child_authority",
                    return_value=authority,
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    return_value=ProcessIdentity(8801, 11.0),
                ),
            ):
                restarted = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    restart_states=states,
                    monotonic=lambda: now[0],
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                )

            self.assertTrue(restarted.ok)
            self.assertIs(
                children["collector"]._process_handle,  # type: ignore[attr-defined]
                replacement,
            )
            self.assertIs(children["companion"], peer)
            spawn.assert_called_once()

    def test_expired_live_publisher_is_isolated_and_uses_existing_backoff(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-wedged.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            publisher = Mock(pid=8810)
            publisher.poll.return_value = None
            publisher_authority = Mock()
            publisher_authority.__exit__ = Mock(return_value=None)
            publisher._dota2_manager_authority_context = publisher_authority
            publisher._dota2_manager_authority_cleanup_error = None
            peer = Mock(pid=8811)
            peer.poll.return_value = None
            children = {"draft_publisher": publisher, "shadow": peer}
            states = {
                "draft_publisher": _ChildRestartState(started_at=0.0),
                "shadow": _ChildRestartState(started_at=0.0),
            }
            gate = Mock(return_value=TerminationResult(True))

            with (
                patch.object(
                    service_module,
                    "_capture_subprocess_tree_identities",
                    return_value=(ProcessIdentity(8810, 10.0),),
                ),
                patch.object(
                    service_module,
                    "_draft_publisher_heartbeat_expired",
                    return_value=True,
                ),
                patch.object(
                    service_module,
                    "terminate_subprocess_tree",
                    return_value=TerminationResult(True),
                ) as terminate,
            ):
                result = _reconcile_managed_children(
                    children,
                    {
                        "draft_publisher": ["python", "publisher.py"],
                        "shadow": ["python", "shadow.py"],
                    },
                    database,
                    identity,
                    restart_states=states,
                    monotonic=lambda: 120.0,
                    authority_gate=gate,
                )

            self.assertTrue(result.ok)
            self.assertNotIn("draft_publisher", children)
            self.assertIs(children["shadow"], peer)
            self.assertEqual(states["draft_publisher"].consecutive_failures, 1)
            self.assertEqual(states["draft_publisher"].next_restart_at, 121.0)
            terminate.assert_called_once_with(publisher, process_factory=psutil.Process)
            publisher_authority.__exit__.assert_called_once_with(None, None, None)
            gate.assert_called_once()
            peer.terminate.assert_not_called()

    def test_busy_health_probe_never_marks_publisher_wedged(self) -> None:
        with patch.object(
            service_module.sqlite3,
            "connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            self.assertIsNone(
                service_module._draft_publisher_heartbeat_expired(
                    Path("busy.db"),
                    expected_identities=(ProcessIdentity(8810, 10.0),),
                    child_runtime_seconds=120.0,
                    now=NOW,
                )
            )

    def test_publisher_wedge_probe_uses_hard_expiry_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-health.db"
            identity = ProcessIdentity(8812, 10.0)
            with LiveBettingStore(database) as store:
                store.init_schema()
                record_health(
                    store.connection,
                    "draft_publisher_worker",
                    "starting",
                    heartbeat_at=NOW - timedelta(minutes=31),
                    details={
                        "phase": "loading_history",
                        "process_pid": identity.pid,
                        "process_created_at": identity.created_at,
                        "process_generation": (
                            service_module._publisher_process_generation(identity)
                        ),
                    },
                )

            self.assertTrue(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(identity,),
                    child_runtime_seconds=31 * 60,
                    now=NOW,
                )
            )
            self.assertFalse(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(identity,),
                    child_runtime_seconds=29 * 60,
                    now=NOW - timedelta(minutes=2),
                )
            )

    def test_old_publisher_health_cannot_kill_new_generation_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-generation.db"
            old_identity = ProcessIdentity(8840, 10.0)
            new_identity = ProcessIdentity(8841, 20.0)
            with LiveBettingStore(database) as store:
                store.init_schema()
                record_health(
                    store.connection,
                    "draft_publisher_worker",
                    "starting",
                    heartbeat_at=NOW - timedelta(hours=1),
                    details={
                        "process_pid": old_identity.pid,
                        "process_created_at": old_identity.created_at,
                        "process_generation": (
                            service_module._publisher_process_generation(old_identity)
                        ),
                    },
                )

            self.assertFalse(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(new_identity,),
                    child_runtime_seconds=60.0,
                    now=NOW,
                )
            )
            self.assertTrue(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(new_identity,),
                    child_runtime_seconds=31 * 60,
                    now=NOW,
                )
            )

    def test_missing_or_invalid_health_requires_current_child_hard_timeout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-missing-health.db"
            identity = ProcessIdentity(8842, 30.0)
            with LiveBettingStore(database) as store:
                store.init_schema()

            self.assertFalse(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(identity,),
                    child_runtime_seconds=60.0,
                    now=NOW,
                )
            )
            self.assertTrue(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(identity,),
                    child_runtime_seconds=31 * 60,
                    now=NOW,
                )
            )

            with LiveBettingStore(database) as store:
                record_health(
                    store.connection,
                    "draft_publisher_worker",
                    "starting",
                    heartbeat_at=NOW,
                    details={"process_pid": "invalid"},
                )
                store.connection.execute(
                    "UPDATE service_health SET last_heartbeat_at='invalid' "
                    "WHERE component='draft_publisher_worker'"
                )
                store.connection.commit()
            self.assertFalse(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(identity,),
                    child_runtime_seconds=60.0,
                    now=NOW,
                )
            )
            self.assertTrue(
                service_module._draft_publisher_heartbeat_expired(
                    database,
                    expected_identities=(identity,),
                    child_runtime_seconds=31 * 60,
                    now=NOW,
                )
            )

    def test_publisher_startup_grace_prevents_stale_row_misfire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-startup-grace.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            publisher = Mock(pid=8820)
            publisher.poll.return_value = None
            children = {"draft_publisher": publisher}
            states = {"draft_publisher": _ChildRestartState(started_at=100.0)}

            with (
                patch.object(
                    service_module,
                    "_capture_subprocess_tree_identities",
                    return_value=(ProcessIdentity(8830, 10.0),),
                ),
                patch.object(
                    service_module,
                    "_draft_publisher_heartbeat_expired",
                    return_value=True,
                ) as probe,
                patch.object(service_module, "terminate_subprocess_tree") as terminate,
            ):
                result = _reconcile_managed_children(
                    children,
                    {"draft_publisher": ["python", "publisher.py"]},
                    database,
                    identity,
                    restart_states=states,
                    monotonic=lambda: 130.0,
                    authority_gate=lambda *_: TerminationResult(True),
                )

            self.assertTrue(result.ok)
            self.assertIs(children["draft_publisher"], publisher)
            probe.assert_not_called()
            terminate.assert_not_called()

    def test_unproven_wedged_termination_preserves_publisher_quarantine(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "publisher-quarantine.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            publisher = Mock(pid=8830)
            publisher.poll.return_value = None
            authority = Mock()
            authority.__exit__ = Mock(return_value=None)
            publisher._dota2_manager_authority_context = authority
            publisher._dota2_manager_authority_cleanup_error = None
            peer = Mock(pid=8831)
            peer.poll.return_value = None
            children = {"draft_publisher": publisher, "shadow": peer}
            states = {
                "draft_publisher": _ChildRestartState(started_at=0.0),
                "shadow": _ChildRestartState(started_at=0.0),
            }

            with (
                patch.object(
                    service_module,
                    "_capture_subprocess_tree_identities",
                    return_value=(ProcessIdentity(8830, 10.0),),
                ),
                patch.object(
                    service_module,
                    "_draft_publisher_heartbeat_expired",
                    return_value=True,
                ),
                patch.object(
                    service_module,
                    "terminate_subprocess_tree",
                    return_value=TerminationResult(False, "identity_changed"),
                ),
            ):
                result = _reconcile_managed_children(
                    children,
                    {
                        "draft_publisher": ["python", "publisher.py"],
                        "shadow": ["python", "shadow.py"],
                    },
                    database,
                    identity,
                    restart_states=states,
                    monotonic=lambda: 120.0,
                    authority_gate=lambda *_: TerminationResult(True),
                )

            self.assertFalse(result.ok)
            self.assertIn("termination_unproven", str(result.detail))
            self.assertIs(children["draft_publisher"], publisher)
            self.assertIs(children["shadow"], peer)
            self.assertEqual(states["draft_publisher"].consecutive_failures, 0)
            authority.__exit__.assert_not_called()

    def test_spawn_oserror_uses_deterministic_bounded_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "spawn-backoff.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            children: dict[str, object] = {}
            states: dict[str, _ChildRestartState] = {}
            now = [0.0]
            spawn = Mock(side_effect=OSError("spawn failed"))
            authorities: list[Mock] = []

            def authority_factory(*_: object, **__: object) -> Mock:
                authority = Mock()
                authority.__enter__ = Mock(
                    return_value={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}
                )
                authority.__exit__ = Mock(return_value=None)
                authorities.append(authority)
                return authority

            attempts = [0.0, 1.0, 3.0, 7.0, 15.0, 31.0, 61.0]
            expected_next = [1.0, 3.0, 7.0, 15.0, 31.0, 61.0, 91.0]
            with patch(
                "scripts.run_dota_shadow_service.manager_child_authority",
                side_effect=authority_factory,
            ):
                for attempted_at, retry_at in zip(attempts, expected_next):
                    now[0] = attempted_at
                    result = _reconcile_managed_children(
                        children,
                        {"companion": ["python", "companion.py"]},
                        database,
                        identity,
                        restart_states=states,
                        monotonic=lambda: now[0],
                        authority_gate=lambda *_: TerminationResult(True),
                        popen_factory=spawn,
                    )
                    self.assertTrue(result.ok)
                    self.assertEqual(
                        states["companion"].next_restart_at,
                        retry_at,
                    )
                    self.assertEqual(children, {})

            self.assertEqual(spawn.call_count, len(attempts))
            self.assertEqual(len(authorities), len(attempts))
            for authority in authorities:
                authority.__exit__.assert_called_once_with(None, None, None)

    def test_child_restart_failures_reset_after_sixty_stable_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "stable-reset.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            child = Mock()
            child.poll.return_value = None
            children = {"collector": child}
            states = {
                "collector": _ChildRestartState(
                    consecutive_failures=5,
                    started_at=0.0,
                )
            }
            now = [59.999]

            for current in (59.999, 60.0):
                now[0] = current
                result = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    restart_states=states,
                    monotonic=lambda: now[0],
                )
                self.assertTrue(result.ok)

            self.assertEqual(states["collector"].consecutive_failures, 0)
            child.poll.return_value = 4
            now[0] = 61.0
            failed = _reconcile_managed_children(
                children,
                {"collector": ["python", "collector.py"]},
                database,
                identity,
                restart_states=states,
                monotonic=lambda: now[0],
            )
            self.assertTrue(failed.ok)
            self.assertEqual(states["collector"].consecutive_failures, 1)
            self.assertEqual(states["collector"].next_restart_at, 62.0)

    def test_due_restart_skips_the_regular_supervisor_interval(self) -> None:
        state = _ChildRestartState(
            consecutive_failures=1,
            next_restart_at=10.0,
        )

        self.assertEqual(
            _restart_sleep_seconds(
                {"companion": state},
                15.0,
                monotonic=lambda: 10.0,
            ),
            0.0,
        )

    def test_successful_spawn_uses_post_bind_monotonic_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "slow-spawn.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            clock = [10.0]
            process = Mock(pid=8810)
            process.poll.return_value = None
            authority = Mock()
            authority.__enter__ = Mock(
                return_value={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}
            )
            authority.__exit__ = Mock(return_value=None)
            states: dict[str, _ChildRestartState] = {}

            def spawn(*_: object, **__: object) -> object:
                clock[0] = 20.0
                return process

            def bind(*_: object, **__: object) -> ProcessIdentity:
                clock[0] = 25.0
                return ProcessIdentity(8810, 25.0)

            with (
                patch(
                    "scripts.run_dota_shadow_service.manager_child_authority",
                    return_value=authority,
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    side_effect=bind,
                ),
            ):
                result = _reconcile_managed_children(
                    {},
                    {"companion": ["python", "companion.py"]},
                    database,
                    identity,
                    states,
                    monotonic=lambda: clock[0],
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                )

            self.assertTrue(result.ok)
            self.assertEqual(states["companion"].started_at, 25.0)

    def test_each_supervisor_child_gets_one_exact_live_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "exact-children.db"
            database.touch()
            live_root = database.parent / "live_betting"
            commands = _commands(Namespace(
                database=database,
                start_collector=True,
                start_companion=True,
                start_shadow=True,
                start_vision=True,
                start_mail=True,
                start_strict_ingest=True,
                start_postmatch=True,
                start_draft_publisher=True,
                draft_deployment_key=DRAFT_DEPLOYMENT_KEY,
                vision_jsonl=live_root / "live_observations",
            ))
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            captured: dict[str, tuple[int, dict[str, str]]] = {}
            fake_processes: dict[int, object] = {}
            next_pid = 95_000

            class Child:
                def __init__(self, pid: int) -> None:
                    self.pid = pid

                @staticmethod
                def poll() -> None:
                    return None

            def spawn(
                command: list[str],
                **kwargs: object,
            ) -> Child:
                nonlocal next_pid
                next_pid += 1
                name = next(
                    key for key, expected in commands.items() if expected == command
                )
                captured[name] = (
                    next_pid,
                    dict(kwargs["env"]),  # type: ignore[arg-type]
                )
                fake_processes[next_pid] = SimpleNamespace(
                    pid=next_pid,
                    create_time=lambda pid=next_pid: float(pid),
                    cmdline=lambda value=list(command): list(value),
                )
                return Child(next_pid)

            marker_paths: list[Path] = []
            root = psutil.Process(os.getpid())

            def spawned_process(pid: int) -> object:
                if pid == root.pid:
                    return root
                return fake_processes[pid]

            with ExitStack() as authority:
                for lock_path in database_service_authority_lock_paths(database):
                    authority.enter_context(SingleInstanceLock(lock_path))
                children: dict[str, object] = {}
                result = _reconcile_managed_children(
                    children,
                    commands,
                    database,
                    identity,
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                    process_factory=spawned_process,
                )
                self.assertTrue(result.ok)
                self.assertEqual(set(captured), set(commands))

                marker_values: set[str] = set()
                for name, command in commands.items():
                    child_pid, environment = captured[name]
                    marker = environment["DOTA2_MANAGER_CHILD_AUTHORITY_V1"]
                    payload = json.loads(marker)
                    marker_values.add(marker)
                    marker_path = Path(payload["marker_path"])
                    marker_paths.append(marker_path)
                    self.assertEqual(payload["command"], command)
                    self.assertEqual(
                        payload["role"],
                        "vision_supervisor" if name == "vision" else name,
                    )
                    self.assertEqual(
                        payload["delegate_roles"],
                        ["vision_watcher"] if name == "vision" else [],
                    )
                    bound_payload = json.loads(
                        marker_path.read_text(encoding="ascii")
                    )
                    self.assertEqual(
                        bound_payload["child_identity"],
                        {"pid": child_pid, "created_at": float(child_pid)},
                    )

                    child = fake_processes[child_pid]

                    def process_factory(
                        pid: int,
                        *,
                        current: object = child,
                    ) -> object:
                        if pid == root.pid:
                            return root
                        if pid == current.pid:
                            return current
                        raise KeyError(pid)

                    with database_writer_authority(
                        database,
                        environ={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": marker},
                        process_factory=process_factory,
                        parent_pid=root.pid,
                        current_pid=child_pid,
                    ):
                        pass

                self.assertEqual(len(marker_values), len(commands))
                shutdown = _shutdown_children_under_authority(
                    children,
                    database,
                    identity,
                    terminator=lambda _: TerminationResult(True),
                    writer_scanner=lambda _, **__: WriterScanResult((), ()),
                    retry_hook=lambda *_: False,
                    sleeper=lambda _: None,
                )
                self.assertTrue(shutdown.ok, shutdown.detail)

            self.assertTrue(all(not path.exists() for path in marker_paths))

    def test_supervisor_marker_context_covers_spawn_restart_and_shutdown(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "marker-lifecycle.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            events: list[str] = []

            class Authority:
                def __init__(self, name: str) -> None:
                    self.name = name

                def __enter__(self) -> dict[str, str]:
                    events.append(f"enter:{self.name}")
                    return {"DOTA2_MANAGER_CHILD_AUTHORITY_V1": self.name}

                def __exit__(self, *_: object) -> None:
                    events.append(f"exit:{self.name}")

            first_process = Mock()
            first_process.poll.return_value = None
            second_process = Mock()
            second_process.poll.return_value = None
            spawned = iter((first_process, second_process))

            def spawn(*_: object, **__: object) -> object:
                process = next(spawned)
                events.append(
                    "spawn:first" if process is first_process else "spawn:second"
                )
                return process

            def bind(_: object, process: object, **__: object) -> ProcessIdentity:
                events.append(
                    "bind:first" if process is first_process else "bind:second"
                )
                return ProcessIdentity(1 if process is first_process else 2, 1.0)

            children: dict[str, object] = {}
            restart_states: dict[str, _ChildRestartState] = {}
            now = [0.0]
            termination_attempts = 0

            def terminate(_: object) -> TerminationResult:
                nonlocal termination_attempts
                termination_attempts += 1
                return TerminationResult(
                    termination_attempts > 1,
                    "first exit proof failed",
                )

            with (
                patch(
                    "scripts.run_dota_shadow_service.manager_child_authority",
                    side_effect=(Authority("first"), Authority("second")),
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    side_effect=bind,
                ),
            ):
                first = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    restart_states=restart_states,
                    monotonic=lambda: now[0],
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                )
                self.assertTrue(first.ok)
                first_process.poll.return_value = 1
                now[0] = 1.0
                deferred = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    restart_states=restart_states,
                    monotonic=lambda: now[0],
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                )
                self.assertTrue(deferred.ok)
                now[0] = 2.0
                second = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    restart_states=restart_states,
                    monotonic=lambda: now[0],
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                )
                self.assertTrue(second.ok)

            shutdown = _shutdown_children_under_authority(
                children,
                database,
                identity,
                terminator=terminate,
                writer_scanner=lambda _, **__: WriterScanResult((), ()),
                retry_hook=lambda *_: True,
                sleeper=lambda _: None,
            )
            self.assertTrue(shutdown.ok, shutdown.detail)
            self.assertEqual(termination_attempts, 2)
            self.assertEqual(
                events,
                [
                    "enter:first",
                    "spawn:first",
                    "bind:first",
                    "exit:first",
                    "enter:second",
                    "spawn:second",
                    "bind:second",
                    "exit:second",
                ],
            )

    def test_supervisor_bind_base_exceptions_cleanup_before_propagation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bind-interrupt.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            for error in (KeyboardInterrupt(), SystemExit(9)):
                with self.subTest(error=type(error).__name__):
                    exited: list[bool] = []

                    class Authority:
                        def __enter__(self) -> dict[str, str]:
                            return {"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}

                        def __exit__(self, *_: object) -> None:
                            exited.append(True)

                    process = Mock(pid=7711)
                    process.poll.return_value = None
                    children: dict[str, object] = {}
                    with (
                        patch(
                            "scripts.run_dota_shadow_service.manager_child_authority",
                            return_value=Authority(),
                        ),
                        patch(
                            "scripts.run_dota_shadow_service.bind_manager_child_authority",
                            side_effect=error,
                        ),
                        patch(
                            "scripts.run_dota_shadow_service.terminate_subprocess_tree",
                            return_value=TerminationResult(True),
                        ) as terminate,
                    ):
                        with self.assertRaises(type(error)):
                            _reconcile_managed_children(
                                children,
                                {"collector": ["python", "collector.py"]},
                                database,
                                identity,
                                authority_gate=lambda *_: TerminationResult(True),
                                popen_factory=lambda *_args, **_kwargs: process,
                            )

                    terminate.assert_called_once()
                    self.assertEqual(exited, [True])
                    self.assertEqual(children, {})

    def test_supervisor_interrupt_keeps_unproven_child_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "bind-quarantine.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            class Authority:
                def __enter__(self) -> dict[str, str]:
                    return {"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}

                def __exit__(self, *_: object) -> None:
                    raise AssertionError(
                        "unproven child authority must remain published"
                    )

            process = Mock(pid=7712)
            process.poll.return_value = None
            children: dict[str, object] = {}
            with (
                patch(
                    "scripts.run_dota_shadow_service.manager_child_authority",
                    return_value=Authority(),
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    side_effect=KeyboardInterrupt(),
                ),
                patch(
                    "scripts.run_dota_shadow_service.terminate_subprocess_tree",
                    return_value=TerminationResult(False, "still alive"),
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    _reconcile_managed_children(
                        children,
                        {"collector": ["python", "collector.py"]},
                        database,
                        identity,
                        authority_gate=lambda *_: TerminationResult(True),
                        popen_factory=lambda *_args, **_kwargs: process,
                    )

            self.assertIn("collector", children)

    def test_clean_scan_cannot_release_unproven_child_termination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "unproven-exit.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            standard_lock = database.with_suffix(".service.lock")
            lock_checks: list[bool] = []

            def stop_quarantine(_: int, __: str) -> bool:
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(standard_lock):
                        pass
                lock_checks.append(True)
                return False

            with SingleInstanceLock(standard_lock):
                result = _shutdown_children_under_authority(
                    {"collector": object()},
                    database,
                    identity,
                    terminator=lambda _: TerminationResult(
                        False,
                        "identity unverifiable",
                    ),
                    writer_scanner=lambda _, **__: WriterScanResult((), ()),
                    retry_hook=stop_quarantine,
                    sleeper=lambda _: None,
                )

            self.assertFalse(result.ok)
            self.assertIn("termination_unproven:collector", result.detail)
            self.assertEqual(lock_checks, [True])

    def test_spawn_failure_closes_marker_and_schedules_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "spawn-failure.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            authority = Mock()
            authority.__enter__ = Mock(
                return_value={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}
            )
            authority.__exit__ = Mock(return_value=None)
            children: dict[str, object] = {}
            restart_states: dict[str, _ChildRestartState] = {}

            with patch(
                "scripts.run_dota_shadow_service.manager_child_authority",
                return_value=authority,
            ):
                result = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    restart_states=restart_states,
                    monotonic=lambda: 10.0,
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=Mock(side_effect=OSError("spawn failed")),
                )

            self.assertTrue(result.ok)
            authority.__exit__.assert_called_once_with(None, None, None)
            self.assertEqual(children, {})
            self.assertEqual(restart_states["collector"].next_restart_at, 11.0)

    def test_authority_enter_failure_is_fatal_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "authority-enter.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            authority = Mock()
            authority.__enter__ = Mock(side_effect=RuntimeError("invalid marker"))
            authority.__exit__ = Mock(return_value=None)
            children: dict[str, object] = {}
            spawn = Mock()

            with patch(
                "scripts.run_dota_shadow_service.manager_child_authority",
                return_value=authority,
            ):
                result = _reconcile_managed_children(
                    children,
                    {"companion": ["python", "companion.py"]},
                    database,
                    identity,
                    {},
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=spawn,
                )

            self.assertFalse(result.ok)
            self.assertIn("child_authority_enter_failed:companion", result.detail)
            authority.__exit__.assert_called_once_with(None, None, None)
            spawn.assert_not_called()
            self.assertEqual(children, {})

    def test_marker_cleanup_failure_stays_quarantined_while_lock_is_held(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "cleanup-quarantine.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            authority = Mock()
            authority.__enter__ = Mock(
                return_value={"DOTA2_MANAGER_CHILD_AUTHORITY_V1": "marker"}
            )
            authority.__exit__ = Mock(side_effect=RuntimeError("cannot unlink"))
            children: dict[str, object] = {}
            attempts: list[int] = []
            standard_lock = database.with_suffix(".service.lock")

            def retry(attempt: int, _: str) -> bool:
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(standard_lock):
                        pass
                attempts.append(attempt)
                return attempt < 2

            with (
                SingleInstanceLock(standard_lock),
                patch(
                    "scripts.run_dota_shadow_service.manager_child_authority",
                    return_value=authority,
                ),
            ):
                reconciliation = _reconcile_managed_children(
                    children,
                    {"collector": ["python", "collector.py"]},
                    database,
                    identity,
                    authority_gate=lambda *_: TerminationResult(True),
                    popen_factory=Mock(side_effect=OSError("spawn failed")),
                )
                self.assertFalse(reconciliation.ok)
                self.assertIn("child_authority_cleanup_failed", reconciliation.detail)
                quarantine = _shutdown_children_under_authority(
                    children,
                    database,
                    identity,
                    terminator=lambda _: TerminationResult(True),
                    writer_scanner=lambda _, **__: WriterScanResult((), ()),
                    retry_hook=retry,
                    sleeper=lambda _: None,
                )

            self.assertFalse(quarantine.ok)
            self.assertIn("authority cleanup is quarantined", quarantine.detail)
            self.assertEqual(attempts, [1, 2])
            authority.__exit__.assert_called_once_with(None, None, None)

    def test_replacement_gate_allows_only_stable_healthy_subtrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "healthy-tree.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            class Process:
                def __init__(
                    self,
                    pid: int,
                    created_at: float,
                    children: list["Process"] | None = None,
                ) -> None:
                    self.pid = pid
                    self.created_at = created_at
                    self._children = children or []

                def create_time(self) -> float:
                    return self.created_at

                def children(self, recursive: bool = False) -> list["Process"]:
                    return list(self._children)

                def is_running(self) -> bool:
                    return True

                def status(self) -> str:
                    return "running"

            descendant = Process(9102, 12.0)
            root_process = Process(9101, 11.0, [descendant])
            handle = Mock(pid=9101)
            handle.poll.return_value = None
            observed_allowed: list[tuple[ProcessIdentity, ...]] = []

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                observed_allowed.append(tuple(allowed_identities))
                return WriterScanResult((ProcessIdentity(9199, 19.0),), ())

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: root_process,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "orphan_writer_conflict:9199")
            self.assertEqual(
                observed_allowed,
                [(ProcessIdentity(9101, 11.0), ProcessIdentity(9102, 12.0))],
            )

    def test_replacement_gate_rescans_legal_descendant_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "sibling-startup.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            class Process:
                def __init__(self, pid: int, created_at: float) -> None:
                    self.pid = pid
                    self.created_at = created_at
                    self.descendants: list["Process"] = []

                def create_time(self) -> float:
                    return self.created_at

                def children(self, recursive: bool = False) -> list["Process"]:
                    return list(self.descendants)

                def is_running(self) -> bool:
                    return True

                def status(self) -> str:
                    return "running"

            root = Process(9201, 21.0)
            worker = Process(9202, 22.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            observed_allowed: list[tuple[ProcessIdentity, ...]] = []

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                observed_allowed.append(tuple(allowed_identities))
                if not root.descendants:
                    root.descendants.append(worker)
                    return WriterScanResult(
                        (ProcessIdentity(worker.pid, worker.created_at),),
                        (),
                    )
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: root,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok)
            self.assertEqual(
                observed_allowed,
                [
                    (ProcessIdentity(9201, 21.0),),
                    (
                        ProcessIdentity(9201, 21.0),
                        ProcessIdentity(9202, 22.0),
                    ),
                ],
            )

    def test_replacement_gate_rejects_unknown_managed_root_during_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "unknown-root.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            class Process:
                def __init__(self, pid: int, created_at: float) -> None:
                    self.pid = pid
                    self.created_at = created_at

                def create_time(self) -> float:
                    return self.created_at

                def children(self, recursive: bool = False) -> list[object]:
                    return []

                def is_running(self) -> bool:
                    return True

                def status(self) -> str:
                    return "running"

            roots = {9301: Process(9301, 31.0), 9302: Process(9302, 32.0)}
            first = Mock(pid=9301)
            first.poll.return_value = None
            unknown = Mock(pid=9302)
            unknown.poll.return_value = None
            children = {"collector": first}

            def scan(_: Path, **__: object) -> WriterScanResult:
                children["unknown"] = unknown
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                children,
                database,
                identity,
                process_factory=lambda pid: roots[pid],
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "healthy_roots_changed_during_writer_gate")

    def test_replacement_gate_rescans_a_direct_child_that_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "child-exit.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            process = Mock(pid=9401)
            process.create_time.return_value = 41.0
            process.children.return_value = []
            handle = Mock(pid=9401)
            handle.poll.return_value = None
            exited = False
            allowed_scans: list[tuple[ProcessIdentity, ...]] = []

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                nonlocal exited
                allowed_scans.append(tuple(allowed_identities))
                if not exited:
                    exited = True
                    handle.poll.return_value = 9
                return WriterScanResult((), ())

            def process_factory(_: int) -> object:
                if exited:
                    raise psutil.NoSuchProcess(handle.pid)
                return process

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=process_factory,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok, result.detail)
            self.assertEqual(
                allowed_scans,
                [(ProcessIdentity(9401, 41.0),), ()],
            )

    def test_replacement_gate_rejects_root_shrink_while_identity_is_alive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "live-root-shrink.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9411, 42.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None

            def scan(_: Path, **__: object) -> WriterScanResult:
                handle.poll.return_value = 1
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: root,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "healthy_roots_changed_during_writer_gate")

    def test_replacement_gate_rejects_live_root_demoted_to_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "demoted-root.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            first_root = _MutableProcess(9412, 42.1)
            second_root = _MutableProcess(9413, 42.2)
            first = Mock(pid=first_root.pid)
            first.poll.return_value = None
            second = Mock(pid=second_root.pid)
            second.poll.return_value = None
            children = {"collector": first, "vision": second}

            def scan(_: Path, **__: object) -> WriterScanResult:
                children.pop("collector")
                second_root.descendants.append(first_root)
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                children,
                database,
                identity,
                process_factory=lambda pid: (
                    first_root if pid == first_root.pid else second_root
                ),
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "healthy_roots_changed_during_writer_gate")

    def test_replacement_gate_allows_root_shrink_after_pid_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "reused-root-shrink.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9421, 43.0)
            reused = _MutableProcess(root.pid, 44.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            exited = False
            scans = 0

            def scan(_: Path, **__: object) -> WriterScanResult:
                nonlocal exited, scans
                scans += 1
                if scans == 1:
                    exited = True
                    handle.poll.return_value = 1
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: reused if exited else root,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok, result.detail)
            self.assertEqual(scans, 2)

    def test_replacement_gate_rejects_root_identity_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "root-replacement.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            process = Mock(pid=9501)
            process.create_time.return_value = 51.0
            process.children.return_value = []
            handle = Mock(pid=9501)
            handle.poll.return_value = None

            def scan(_: Path, **__: object) -> WriterScanResult:
                process.create_time.return_value = 52.0
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: process,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "healthy_roots_changed_during_writer_gate")

    def test_replacement_gate_rescans_a_descendant_that_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "descendant-toctou.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)

            class Process:
                def __init__(self, pid: int, created_at: float) -> None:
                    self.pid = pid
                    self.created_at = created_at
                    self.descendants: list["Process"] = []

                def create_time(self) -> float:
                    return self.created_at

                def children(self, recursive: bool = False) -> list["Process"]:
                    return list(self.descendants)

                def is_running(self) -> bool:
                    return True

                def status(self) -> str:
                    return "running"

            root = Process(9601, 61.0)
            worker = Process(9602, 62.0)
            root.descendants.append(worker)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            exited = False
            allowed_scans: list[tuple[ProcessIdentity, ...]] = []

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                nonlocal exited
                allowed_scans.append(tuple(allowed_identities))
                if not exited:
                    exited = True
                    root.descendants.clear()
                return WriterScanResult((), ())

            def process_factory(pid: int) -> object:
                if pid == root.pid:
                    return root
                raise psutil.NoSuchProcess(pid)

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=process_factory,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok, result.detail)
            self.assertEqual(
                allowed_scans,
                [
                    (
                        ProcessIdentity(9601, 61.0),
                        ProcessIdentity(9602, 62.0),
                    ),
                    (ProcessIdentity(9601, 61.0),),
                ],
            )

    def test_replacement_gate_reconciles_birth_conflict_from_real_scanner(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "real-scanner-birth.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9701, 71.0)
            worker = _MutableProcess(9702, 72.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            scanned = 0
            allowed_scans: list[tuple[ProcessIdentity, ...]] = []
            writer = Mock(info={
                "pid": worker.pid,
                "name": "python.exe",
                "create_time": worker.created_at,
                "cmdline": [
                    "python",
                    "scripts/run_postmatch_labeler.py",
                    "--database",
                    str(database.resolve()),
                ],
            })

            def scan(
                path: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                nonlocal scanned
                scanned += 1
                allowed_scans.append(tuple(allowed_identities))
                if scanned == 1:
                    root.descendants.append(worker)
                return scan_managed_writers(
                    path,
                    allowed_identities=allowed_identities,
                    process_iter=lambda _: [writer],
                )

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: root,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok, result.detail)
            self.assertEqual(scanned, 2)
            self.assertEqual(
                allowed_scans[1],
                (
                    ProcessIdentity(root.pid, root.created_at),
                    ProcessIdentity(worker.pid, worker.created_at),
                ),
            )

    def test_replacement_gate_rejects_external_conflict_during_descendant_birth(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "birth-external.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9711, 81.0)
            worker = _MutableProcess(9712, 82.0)
            external = ProcessIdentity(9713, 83.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None

            def scan(_: Path, **__: object) -> WriterScanResult:
                root.descendants.append(worker)
                return WriterScanResult(
                    (ProcessIdentity(worker.pid, worker.created_at), external),
                    (),
                )

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda _: root,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, f"orphan_writer_conflict:{external.pid}")

    def test_replacement_gate_rejects_added_descendant_that_detaches_alive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "descendant-detach.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9721, 91.0)
            worker = _MutableProcess(9722, 92.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            scans = 0

            def scan(_: Path, **__: object) -> WriterScanResult:
                nonlocal scans
                scans += 1
                if scans == 1:
                    root.descendants.append(worker)
                    return WriterScanResult(
                        (ProcessIdentity(worker.pid, worker.created_at),),
                        (),
                    )
                root.descendants.clear()
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=lambda pid: root if pid == root.pid else worker,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "healthy_subtree_changed_during_writer_gate")

    def test_replacement_gate_rejects_exited_root_with_live_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "orphan-descendant.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            descendant = _MutableProcess(9732, 102.0)
            root = _MutableProcess(9731, 101.0, [descendant])
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            exited = False

            def scan(_: Path, **__: object) -> WriterScanResult:
                nonlocal exited
                exited = True
                handle.poll.return_value = 1
                return WriterScanResult((), ())

            def process_factory(pid: int) -> object:
                if pid == root.pid:
                    if exited:
                        raise psutil.NoSuchProcess(pid)
                    return root
                return descendant

            result = _replacement_authority_gate(
                {"vision": handle},
                database,
                identity,
                process_factory=process_factory,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertEqual(result.detail, "healthy_subtree_changed_during_writer_gate")

    def test_replacement_gate_allows_descendant_pid_reuse_after_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "descendant-reuse.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            worker = _MutableProcess(9742, 112.0)
            reused = _MutableProcess(worker.pid, 113.0)
            root = _MutableProcess(9741, 111.0, [worker])
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            scans = 0

            def scan(_: Path, **__: object) -> WriterScanResult:
                nonlocal scans
                scans += 1
                if scans == 1:
                    root.descendants.clear()
                return WriterScanResult((), ())

            result = _replacement_authority_gate(
                {"vision": handle},
                database,
                identity,
                process_factory=lambda pid: root if pid == root.pid else reused,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok, result.detail)
            self.assertEqual(scans, 2)

    def test_replacement_gate_rejects_unverifiable_descendant_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "descendant-unverifiable.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            worker = _MutableProcess(9752, 122.0)
            root = _MutableProcess(9751, 121.0, [worker])
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None

            def scan(_: Path, **__: object) -> WriterScanResult:
                root.descendants.clear()
                return WriterScanResult((), ())

            def process_factory(pid: int) -> object:
                if pid == root.pid:
                    return root
                raise psutil.AccessDenied(pid)

            result = _replacement_authority_gate(
                {"vision": handle},
                database,
                identity,
                process_factory=process_factory,
                writer_scanner=scan,
            )

            self.assertFalse(result.ok)
            self.assertIn("replacement_authority_unverifiable", str(result.detail))
            self.assertIn(str(worker.pid), str(result.detail))

    def test_replacement_gate_allows_root_exit_during_current_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capture-root-exit.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9761, 131.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            exit_during_capture = False
            scans = 0

            def scan(_: Path, **__: object) -> WriterScanResult:
                nonlocal exit_during_capture, scans
                scans += 1
                if scans == 1:
                    exit_during_capture = True
                return WriterScanResult((), ())

            def process_factory(pid: int) -> object:
                if exit_during_capture:
                    handle.poll.return_value = 1
                    raise psutil.NoSuchProcess(pid)
                return root

            result = _replacement_authority_gate(
                {"collector": handle},
                database,
                identity,
                process_factory=process_factory,
                writer_scanner=scan,
            )

            self.assertTrue(result.ok, result.detail)
            self.assertEqual(scans, 2)

    def test_replacement_gate_bounds_continuous_legal_churn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "continuous-churn.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            root = _MutableProcess(9771, 141.0)
            handle = Mock(pid=root.pid)
            handle.poll.return_value = None
            next_pid = 9771

            def scan(_: Path, **__: object) -> WriterScanResult:
                nonlocal next_pid
                next_pid += 1
                worker = _MutableProcess(next_pid, float(next_pid))
                root.descendants[:] = [worker]
                return WriterScanResult(
                    (ProcessIdentity(worker.pid, worker.created_at),),
                    (),
                )

            def process_factory(pid: int) -> object:
                if pid == root.pid:
                    return root
                current = root.descendants[0]
                if pid == current.pid:
                    return current
                raise psutil.NoSuchProcess(pid)

            result = _replacement_authority_gate(
                {"vision": handle},
                database,
                identity,
                process_factory=process_factory,
                writer_scanner=scan,
                max_passes=3,
            )

            self.assertFalse(result.ok)
            self.assertEqual(
                result.detail,
                "healthy_subtree_did_not_stabilize_during_writer_gate",
            )

    def test_replacement_gate_allows_read_only_web_peer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "web-peer.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            web_peer = Mock(info={
                "pid": 9150,
                "name": "python.exe",
                "create_time": 15.0,
                "cmdline": [
                    "python",
                    "-m",
                    "web.main",
                    "--database",
                    str(database.resolve()),
                ],
            })

            result = _replacement_authority_gate(
                {},
                database,
                identity,
                writer_scanner=lambda path, **kwargs: scan_managed_writers(
                    path,
                    allowed_identities=kwargs["allowed_identities"],
                    process_iter=lambda _: [web_peer],
                ),
            )

            self.assertTrue(result.ok)

    def test_shutdown_quarantine_retries_while_standard_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "shutdown-retry.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            standard = database.with_suffix(".service.lock")
            termination_attempts = 0
            scan_attempts = 0
            retry_lock_checks: list[bool] = []

            def terminate(_: object) -> TerminationResult:
                nonlocal termination_attempts
                termination_attempts += 1
                return TerminationResult(termination_attempts > 1, "first failure")

            def scan(
                _: Path,
                *,
                allowed_identities: tuple[ProcessIdentity, ...],
            ) -> WriterScanResult:
                nonlocal scan_attempts
                self.assertEqual(allowed_identities, ())
                scan_attempts += 1
                return (
                    WriterScanResult((ProcessIdentity(9201, 20.0),), ())
                    if scan_attempts == 1
                    else WriterScanResult((), ())
                )

            def retry(_: int, __: str) -> bool:
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(standard):
                        pass
                retry_lock_checks.append(True)
                return True

            with SingleInstanceLock(standard):
                result = _shutdown_children_under_authority(
                    {"collector": object()},
                    database,
                    identity,
                    terminator=terminate,
                    writer_scanner=scan,
                    retry_hook=retry,
                    sleeper=lambda _: None,
                )

            self.assertTrue(result.ok)
            self.assertEqual(termination_attempts, 2)
            self.assertEqual(retry_lock_checks, [True])
            with SingleInstanceLock(standard):
                pass

    def test_shutdown_quarantine_has_bounded_persistent_failure_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "persistent-quarantine.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            attempts: list[int] = []

            result = _shutdown_children_under_authority(
                {"collector": object()},
                database,
                identity,
                terminator=lambda _: TerminationResult(False, "cannot terminate"),
                writer_scanner=lambda _, **__: WriterScanResult(
                    (ProcessIdentity(9301, 30.0),),
                    (),
                ),
                retry_hook=lambda attempt, _: (
                    attempts.append(attempt) is None and attempt < 2
                ),
                sleeper=lambda _: None,
            )

            self.assertFalse(result.ok)
            self.assertIn("quarantined:2", str(result.detail))
            self.assertEqual(attempts, [1, 2])

    def test_runtime_hardlink_quarantine_stops_children_and_blocks_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime-hardlink.db"
            alias = Path(directory) / "runtime-hardlink-alias.db"
            database.write_bytes(b"sqlite")
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            os.link(database, alias)
            terminated: list[bool] = []

            gate = _replacement_authority_gate(
                {},
                database,
                identity,
                writer_scanner=lambda _, **__: WriterScanResult((), ()),
            )
            self.assertFalse(gate.ok)
            self.assertIn("replacement_authority_unverifiable", str(gate.detail))

            result = _shutdown_children_under_authority(
                {"collector": object()},
                database,
                identity,
                terminator=lambda _: (
                    terminated.append(True) or TerminationResult(True)
                ),
                writer_scanner=lambda _, **__: WriterScanResult((), ()),
                retry_hook=lambda *_: False,
                sleeper=lambda _: None,
            )

            self.assertFalse(result.ok)
            self.assertIn("database_authority_unverifiable", str(result.detail))
            self.assertEqual(terminated, [True])

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
            set(commands),
            {
                "companion",
                "vision",
                "strict_ingest",
                "postmatch",
                "historical_rosh",
            },
        )
        self.assertIn(
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "supervise_raybet_streams.py"
            ),
            commands["vision"],
        )
        project_root = Path(__file__).resolve().parents[1]
        self.assertIn(
            str(project_root / "scripts" / "run_strict_event_ingest.py"),
            commands["strict_ingest"],
        )
        self.assertIn(
            str(project_root / "scripts" / "run_postmatch_labeler.py"),
            commands["postmatch"],
        )
        self.assertIn("--all", commands["postmatch"])

    def test_all_managed_workers_skip_redundant_schema_preparation(self) -> None:
        database = Path("service.db")
        live_root = database.resolve().parent / "live_betting"
        commands = _commands(Namespace(
            database=database,
            start_collector=True,
            start_companion=True,
            start_shadow=True,
            start_vision=True,
            start_mail=True,
            start_strict_ingest=True,
            start_postmatch=True,
            draft_deployment_key=DRAFT_DEPLOYMENT_KEY,
            vision_jsonl=live_root / "live_observations",
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
                "draft_publisher",
                "historical_rosh",
            },
        )
        for command in commands.values():
            self.assertIn("--schema-prepared", command)
            self.assertIn(str(database.resolve()), command)
        self.assertIn(str(live_root / "raw-v2"), commands["collector"])
        self.assertIn(str(live_root / "live_observations"), commands["shadow"])
        self.assertIn(str(live_root / "live_observations"), commands["vision"])
        self.assertIn(str(live_root / "live_evidence"), commands["vision"])
        self.assertIn(str(live_root / "watcher_logs"), commands["vision"])
        self.assertIn(
            str(database.resolve().parent / "raw-sources"),
            commands["strict_ingest"],
        )
        self.assertIn(
            str(
                database.resolve().parent
                / "reports"
                / "strict_event_coverage_latest.json"
            ),
            commands["strict_ingest"],
        )
        self.assertIn(
            str(database.resolve().parent / "raw-sources"),
            commands["postmatch"],
        )
        self.assertIn(
            str(
                Path(__file__).resolve().parents[1]
                / "scripts"
                / "run_historical_rosh_worker.py"
            ),
            commands["historical_rosh"],
        )
        publisher_target = managed_child_target(commands["draft_publisher"])
        self.assertIsNotNone(publisher_target)
        assert publisher_target is not None
        self.assertEqual(publisher_target.count("--deployment-key"), 1)
        key_index = publisher_target.index("--deployment-key")
        self.assertEqual(publisher_target[key_index + 1], DRAFT_DEPLOYMENT_KEY)

    def test_supervisor_requires_valid_external_draft_deployment_pin(self) -> None:
        base = dict(
            database=Path("service.db"),
            start_collector=False,
            start_companion=False,
            start_shadow=False,
            start_vision=False,
            start_mail=False,
            start_strict_ingest=False,
            start_postmatch=False,
            start_draft_publisher=True,
            vision_jsonl=None,
        )
        for value in (None, "a" * 63, "A" * 64):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "draft-deployment-key"):
                    _commands(Namespace(**base, draft_deployment_key=value))

        commands = _commands(
            Namespace(**base, draft_deployment_key=DRAFT_DEPLOYMENT_KEY)
        )
        target = managed_child_target(commands["draft_publisher"])
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(
            target[target.index("--deployment-key") + 1],
            DRAFT_DEPLOYMENT_KEY,
        )

    def test_supervisor_once_and_explicit_disable_skip_historical_rosh(self) -> None:
        base = dict(
            database=Path("service.db"),
            start_collector=False,
            start_companion=False,
            start_shadow=False,
            start_vision=False,
            start_mail=False,
            start_strict_ingest=False,
            start_postmatch=False,
            start_draft_publisher=False,
            vision_jsonl=None,
        )
        once = _commands(Namespace(**base, once=True))
        disabled = _commands(
            Namespace(**base, once=False, disable_historical_rosh=True)
        )

        self.assertNotIn("historical_rosh", once)
        self.assertNotIn("historical_rosh", disabled)

    def test_supervisor_rejects_cross_database_vision_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "must equal <database-dir>"):
            _commands(Namespace(
                database=Path("candidate") / "dota2.db",
                start_collector=False,
                start_companion=False,
                start_shadow=True,
                start_vision=False,
                start_mail=False,
                start_strict_ingest=False,
                start_postmatch=False,
                start_draft_publisher=False,
                vision_jsonl=Path("project-data") / "live_observations",
            ))

    def test_supervisor_aggregates_new_worker_and_companion_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            now = datetime.now(timezone.utc)
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)
                for component in (
                    "vision_worker",
                    "strict_ingest_worker",
                    "postmatch_worker",
                    "historical_rosh_worker",
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
                    "historical_rosh",
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
            self.assertEqual(statuses["historical_rosh"], "healthy")
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
            backup_dir = root / "external-volume-backups"
            (root / "service.db").touch()

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
                    runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
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
                "--backup-dir",
                str(backup_dir),
                "--start-companion",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.prepare_database",
                    side_effect=prepare,
                ) as migrate,
                patch(
                    "scripts.run_dota_shadow_service.subprocess.Popen",
                    side_effect=spawn,
                ),
                patch(
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    return_value=ProcessIdentity(1, 1.0),
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    side_effect=run_once,
                ),
                patch(
                    "scripts.run_dota_shadow_service.terminate_subprocess_tree",
                    side_effect=_terminate_fake_tree,
                ),
            ):
                self.assertEqual(main(), 0)

            self.assertLess(events.index("prepare"), events.index("spawn"))
            self.assertLess(events.index("spawn"), events.index("service-no-init"))
            self.assertEqual(
                migrate.call_args.args,
                (root / "service.db", backup_dir),
            )
            self.assertIs(
                migrate.call_args.kwargs["supervisor_process_lock_held"],
                True,
            )
            self.assertEqual(
                migrate.call_args.kwargs["odds_raw_root"],
                root / "live_betting" / "raw-v2",
            )

    def test_routine_supervisor_start_verifies_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events: list[str] = []
            (root / "service.db").touch()

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
                    runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
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
                    "scripts.run_dota_shadow_service.bind_manager_child_authority",
                    return_value=ProcessIdentity(1, 1.0),
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    return_value={"shadow": {"orders": {"signals": 0}}},
                ) as service,
                patch(
                    "scripts.run_dota_shadow_service.terminate_subprocess_tree",
                    side_effect=_terminate_fake_tree,
                ),
            ):
                self.assertEqual(main(), 0)

            migrate.assert_not_called()
            self.assertLess(events.index("verify"), events.index("spawn"))
            self.assertEqual(
                service.call_args.args[1],
                root / "live_betting" / "service_report.json",
            )
            self.assertFalse(service.call_args.kwargs["health_only"])

    def test_supervisor_retries_busy_and_locked_without_stopping_children(self) -> None:
        busy = sqlite3.OperationalError("wrapped busy")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        locked = sqlite3.OperationalError("database table is locked")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "service.db"
            database.touch()
            child = object()
            events: list[str] = []
            sleep_durations: list[float] = []
            service_calls = 0
            output = StringIO()
            errors = StringIO()
            preparation = Mock(
                backup=None,
                live_schema_version=LIVE_SCHEMA_VERSION,
                intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
            )

            def reconcile(children: dict[str, object], *_: object) -> TerminationResult:
                children.setdefault("companion", child)
                events.append("reconcile")
                return TerminationResult(True)

            def run_once(*_: object, **__: object) -> dict[str, object]:
                nonlocal service_calls
                service_calls += 1
                events.append(f"service-{service_calls}")
                if service_calls == 1:
                    raise busy
                if service_calls == 2:
                    raise locked
                return {"shadow": {"orders": {"signals": 0}}}

            def sleep(seconds: float) -> None:
                self.assertGreater(seconds, 0)
                self.assertNotIn("shutdown", events)
                sleep_durations.append(seconds)
                events.append("sleep")

            def shutdown(children: dict[str, object], *_: object) -> TerminationResult:
                self.assertEqual(children, {"companion": child})
                events.append("shutdown")
                return TerminationResult(True)

            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--once",
                "--interval",
                "0",
                "--start-companion",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", output),
                patch.object(sys, "stderr", errors),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers",
                    return_value=WriterScanResult((), ()),
                ),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    return_value=preparation,
                ),
                patch(
                    "scripts.run_dota_shadow_service._reconcile_managed_children",
                    side_effect=reconcile,
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    side_effect=run_once,
                ),
                patch(
                    "scripts.run_dota_shadow_service._shutdown_children_under_authority",
                    side_effect=shutdown,
                ),
                patch("scripts.run_dota_shadow_service.time.sleep", side_effect=sleep),
            ):
                self.assertEqual(main(), 0)

            self.assertEqual(
                events,
                [
                    "reconcile",
                    "service-1",
                    "sleep",
                    "reconcile",
                    "service-2",
                    "sleep",
                    "reconcile",
                    "service-3",
                    "shutdown",
                ],
            )
            self.assertEqual(sleep_durations, [0.05, 0.05])
            self.assertEqual(
                [json.loads(line) for line in errors.getvalue().splitlines()],
                [
                    {
                        "status": "degraded",
                        "detail": "database_lock_contention",
                        "sqlite_result": "SQLITE_BUSY",
                    },
                    {
                        "status": "degraded",
                        "detail": "database_lock_contention",
                        "sqlite_result": "SQLITE_LOCKED",
                    },
                ],
            )

    def test_supervisor_non_lock_operational_error_still_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "service.db"
            database.touch()
            child = object()
            events: list[str] = []
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_READONLY
            preparation = Mock(
                backup=None,
                live_schema_version=LIVE_SCHEMA_VERSION,
                intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
            )

            def reconcile(children: dict[str, object], *_: object) -> TerminationResult:
                children["companion"] = child
                return TerminationResult(True)

            def shutdown(children: dict[str, object], *_: object) -> TerminationResult:
                self.assertEqual(children, {"companion": child})
                events.append("shutdown")
                return TerminationResult(True)

            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--once",
                "--start-companion",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(sys, "stderr", StringIO()),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers",
                    return_value=WriterScanResult((), ()),
                ),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    return_value=preparation,
                ),
                patch(
                    "scripts.run_dota_shadow_service._reconcile_managed_children",
                    side_effect=reconcile,
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    side_effect=error,
                ),
                patch(
                    "scripts.run_dota_shadow_service._shutdown_children_under_authority",
                    side_effect=shutdown,
                ),
                patch("scripts.run_dota_shadow_service.time.sleep") as sleep,
            ):
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    main()

            self.assertIs(raised.exception, error)
            self.assertEqual(events, ["shutdown"])
            sleep.assert_not_called()

    def test_proven_authority_failure_exits_without_reconcile_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "authority-fatal.db"
            database.touch()
            standard_lock = database.with_suffix(".service.lock")
            preparation = Mock(
                backup=None,
                live_schema_version=LIVE_SCHEMA_VERSION,
                intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
            )
            reconcile = Mock(
                return_value=TerminationResult(
                    False,
                    "child_authority_bind_failed:companion:RuntimeError:bad marker",
                )
            )
            service = Mock()
            shutdown_calls = 0

            def shutdown(*_: object, **__: object) -> TerminationResult:
                nonlocal shutdown_calls
                shutdown_calls += 1
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(standard_lock):
                        pass
                return TerminationResult(True, "shutdown_proven_after_attempt:1")

            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--once",
                "--start-companion",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.object(sys, "stdout", StringIO()),
                patch.object(sys, "stderr", StringIO()),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers",
                    return_value=WriterScanResult((), ()),
                ),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    return_value=preparation,
                ),
                patch(
                    "scripts.run_dota_shadow_service._reconcile_managed_children",
                    reconcile,
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    service,
                ),
                patch(
                    "scripts.run_dota_shadow_service._shutdown_children_under_authority",
                    side_effect=shutdown,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "managed child authority failed",
                ):
                    main()

            reconcile.assert_called_once()
            service.assert_not_called()
            self.assertEqual(shutdown_calls, 1)

    def test_proven_database_authority_failure_never_reconciles_children(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "database-authority-fatal.db"
            database.touch()
            identity = require_unique_database_file(database)
            assert isinstance(identity, DatabaseFileIdentity)
            preparation = Mock(
                backup=None,
                live_schema_version=LIVE_SCHEMA_VERSION,
                intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
            )
            identity_checks = 0

            def check_identity(*_: object, **__: object) -> DatabaseFileIdentity:
                nonlocal identity_checks
                identity_checks += 1
                if identity_checks <= 3:
                    return identity
                raise RuntimeError("database file identity changed")

            shutdown = Mock(
                return_value=TerminationResult(
                    True,
                    "shutdown_proven_after_attempt:1",
                )
            )
            reconcile = Mock()
            service = Mock()
            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(database),
                "--once",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.require_unique_database_file",
                    side_effect=check_identity,
                ),
                patch(
                    "scripts.run_dota_shadow_service.scan_managed_writers",
                    return_value=WriterScanResult((), ()),
                ),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    return_value=preparation,
                ),
                patch(
                    "scripts.run_dota_shadow_service._reconcile_managed_children",
                    reconcile,
                ),
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    service,
                ),
                patch(
                    "scripts.run_dota_shadow_service._shutdown_children_under_authority",
                    shutdown,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "database authority failed",
                ):
                    main()

            shutdown.assert_called_once()
            reconcile.assert_not_called()
            service.assert_not_called()

    def test_recurring_supervisor_keeps_health_and_reporting_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reporter = Mock()
            (root / "service.db").touch()
            argv = [
                "run_dota_shadow_service.py",
                "--database",
                str(root / "service.db"),
                "--report",
                str(root / "service-report.json"),
                "--lock",
                str(root / "service.lock"),
            ]
            preparation = Mock(
                backup=None,
                live_schema_version=LIVE_SCHEMA_VERSION,
                intelligence_schema_version=INTELLIGENCE_SCHEMA_VERSION,
                runtime_schema_version=CURRENT_RUNTIME_SCHEMA_VERSION,
            )
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "scripts.run_dota_shadow_service.verify_prepared_database",
                    return_value=preparation,
                ),
                patch(
                    "scripts.run_dota_shadow_service._ReportWorker",
                    return_value=reporter,
                ) as worker,
                patch(
                    "scripts.run_dota_shadow_service.service_once",
                    return_value={"pending_orders": 0},
                ) as service,
                patch(
                    "scripts.run_dota_shadow_service.time.sleep",
                    side_effect=KeyboardInterrupt,
                ),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    main()

            worker.assert_called_once_with(
                root / "service.db", root / "service-report.json"
            )
            self.assertIsNone(service.call_args.args[1])
            self.assertTrue(service.call_args.kwargs["health_only"])
            reporter.start_if_idle.assert_called_once_with()
            reporter.wait.assert_called_once_with()

    def test_expensive_database_audit_is_cached_between_service_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "service.db"
            with LiveBettingStore(database) as store:
                store.init_schema()
                prepare_runtime_schema(store.connection)
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

    def test_failed_database_audit_remains_unhealthy_during_background_recheck(
        self,
    ) -> None:
        database = Path("failed-service.db").resolve()
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = (
            0,
            "main",
            str(database),
        )
        _DATABASE_HEALTH_CACHE.clear()
        _DATABASE_HEALTH_CACHE[str(database)] = (
            NOW,
            (
                "unhealthy",
                "integrity_check_failed",
                {"integrity": ["malformed"], "foreign_key_issues": 0},
            ),
        )

        with patch(
            "scripts.run_dota_shadow_service._start_database_audit"
        ) as start_audit:
            cached = _periodic_database_health(
                connection,
                NOW + timedelta(seconds=59),
                background=True,
            )
            refreshing = _periodic_database_health(
                connection,
                NOW + timedelta(seconds=61),
                background=True,
            )

        self.assertEqual(cached[:2], ("unhealthy", "integrity_check_failed"))
        self.assertTrue(cached[2]["audit_cached"])
        self.assertEqual(
            refreshing[:2], ("unhealthy", "integrity_check_failed")
        )
        self.assertTrue(refreshing[2]["audit_stale"])
        self.assertTrue(refreshing[2]["audit_refreshing"])
        start_audit.assert_called_once_with(str(database), database)


if __name__ == "__main__":
    unittest.main()
