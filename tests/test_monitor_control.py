from __future__ import annotations
# ruff: noqa: E402

import asyncio
import json
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import psutil
import pytest

service_coordination = pytest.importorskip(
    "live_betting.service_coordination",
    reason="legacy file-authority monitor control tests require the retired SQLite runtime",
)
from live_betting.service_coordination import (  # noqa: E402
    ProcessIdentity,
    SingleInstanceLock,
    WriterScanResult,
    database_service_authority_lock_paths,
    database_web_authority_lock_paths,
    managed_child_target,
    require_unique_database_file,
)
from live_betting.runtime_schema import prepare_runtime_schema
from web import queries
from web.app import _lifespan, app
from web.control import ControlService
from web.routers import control as control_router


class FakeProcess:
    def __init__(
        self,
        pid: int,
        command: list[str],
        created_at: float = 1_700_000_000.0,
    ) -> None:
        self.pid = pid
        self.command = command
        self.created_at = created_at
        self.running = True
        self.children_sequence: list[list[FakeProcess]] = [[]]
        self.children_calls = 0
        self.suspend_calls = 0
        self.resume_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.ignore_terminate = False
        self.ignore_kill = False
        self.suspend_error: BaseException | None = None
        self.resume_error: BaseException | None = None
        self.terminate_error: BaseException | None = None
        self.kill_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.identity_error: BaseException | None = None
        self.command_error: BaseException | None = None
        self.children_error: BaseException | None = None

    def create_time(self) -> float:
        if self.identity_error is not None:
            raise self.identity_error
        return self.created_at

    def cmdline(self) -> list[str]:
        if self.command_error is not None:
            raise self.command_error
        return list(self.command)

    def is_running(self) -> bool:
        return self.running

    def status(self) -> str:
        return "running" if self.running else "stopped"

    def children(self, recursive: bool = False) -> list[FakeProcess]:
        if self.children_error is not None:
            raise self.children_error
        index = min(self.children_calls, len(self.children_sequence) - 1)
        self.children_calls += 1
        return list(self.children_sequence[index])

    def suspend(self) -> None:
        self.suspend_calls += 1
        if self.suspend_error is not None:
            raise self.suspend_error

    def resume(self) -> None:
        self.resume_calls += 1
        if self.resume_error is not None:
            raise self.resume_error

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            raise self.terminate_error
        if not self.ignore_terminate:
            self.running = False

    def kill(self) -> None:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        if not self.ignore_kill:
            self.running = False

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_error is not None:
            raise self.wait_error
        if self.running:
            raise psutil.TimeoutExpired(timeout or 0, pid=self.pid)
        return 0


class FakePopen:
    def __init__(self, pid: int, process: FakeProcess | None = None) -> None:
        self.pid = pid
        self.process = process

    def poll(self) -> int | None:
        if self.process is None or self.process.running:
            return None
        return 0


class MonitorControlServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "control.db"
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row
        prepare_runtime_schema(self.connection)
        self.web_locks = [
            SingleInstanceLock(path)
            for path in database_web_authority_lock_paths(self.database)
        ]
        for lock in self.web_locks:
            lock.__enter__()
        self.processes: dict[int, FakeProcess] = {}
        self.popen_calls: list[tuple[list[str], dict[str, object]]] = []
        self.verifier_calls: list[tuple[Path, Path]] = []
        self.scanner_calls: list[tuple[Path, tuple[ProcessIdentity, ...]]] = []

        def popen(command: list[str], **kwargs: object) -> FakePopen:
            pid = 4100 + len(self.popen_calls)
            self.popen_calls.append((list(command), kwargs))
            self.processes[pid] = FakeProcess(pid, list(command))
            return FakePopen(pid, self.processes[pid])

        self.popen = popen
        self.service = self.make_service()

    def verify_database(self, database: Path, *, odds_raw_root: Path) -> None:
        self.verifier_calls.append((database, odds_raw_root))

    def scan_writers(
        self,
        database: Path,
        *,
        allowed_identities: tuple[ProcessIdentity, ...],
    ) -> WriterScanResult:
        self.scanner_calls.append((database, tuple(allowed_identities)))
        return WriterScanResult((), ())

    def make_service(self, **overrides: object) -> ControlService:
        options: dict[str, object] = {
            "project_dir": self.root,
            "python_executable": "python-test",
            "popen_factory": self.popen,
            "process_factory": self.processes.__getitem__,
            "database_verifier": self.verify_database,
            "writer_scanner": self.scan_writers,
        }
        options.update(overrides)
        return ControlService(**options)  # type: ignore[arg-type]

    def tearDown(self) -> None:
        for process in self.processes.values():
            process.running = False
        self.service.close()
        for lock in reversed(self.web_locks):
            lock.__exit__(None, None, None)
        self.connection.close()
        self.directory.cleanup()

    def execute(self, action: str, request_id: str) -> dict[str, object]:
        return self.service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action=action,
            request_id=request_id,
            client_host="127.0.0.1",
        )

    def test_start_is_allowlisted_and_request_id_is_idempotent(self) -> None:
        first = self.execute("start", "request-start-001")
        second = self.execute("start", "request-start-001")

        self.assertTrue(first["ok"])
        self.assertEqual(first["result"], "started")
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self.popen_calls), 1)
        environment = self.popen_calls[0][1]["env"]
        marker = json.loads(
            environment["DOTA2_MANAGER_CHILD_AUTHORITY_V1"]  # type: ignore[index]
        )
        bound = json.loads(
            Path(marker["marker_path"]).read_text(encoding="ascii")
        )
        self.assertEqual(bound["child_identity"]["pid"], first["pid"])

    def test_control_request_never_attempts_schema_ddl(self) -> None:
        schema_actions = {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
        }
        attempted: list[int] = []

        def authorizer(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action in schema_actions:
                attempted.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(authorizer)
        try:
            started = self.execute("start", "request-no-schema-ddl-start")
            stopped = self.execute("stop", "request-no-schema-ddl-stop")
        finally:
            self.connection.set_authorizer(None)

        self.assertTrue(started["ok"])
        self.assertTrue(stopped["ok"])
        self.assertEqual(attempted, [])

    def test_idempotent_stopped_replay_releases_newly_acquired_lock(self) -> None:
        self.execute("start", "request-idempotent-lock-start")
        stopped = self.execute("stop", "request-idempotent-lock-stop")

        replayed = self.execute("stop", "request-idempotent-lock-stop")

        self.assertTrue(stopped["ok"])
        self.assertTrue(replayed["idempotent"])
        self.assertEqual(replayed["result"], "stopped")
        with SingleInstanceLock(self.database.with_suffix(".service.lock")):
            pass
        command = self.popen_calls[0][0]
        target = managed_child_target(command)
        self.assertIsNotNone(target)
        self.assertEqual(
            target[:4],
            ["python-test", "-u", "-m", "live_betting.monitor"],
        )
        self.assertIn(str(self.database.resolve()), command)
        raw_index = command.index("--raw-dir")
        self.assertEqual(
            command[raw_index + 1],
            str(self.database.resolve().parent / "live_betting" / "raw-v2"),
        )
        self.assertIn("--schema-prepared", command)
        self.assertEqual(
            self.verifier_calls,
            [
                (
                    self.database.resolve(),
                    self.database.resolve().parent / "live_betting" / "raw-v2",
                )
            ],
        )

    def test_web_launcher_real_child_uses_root_authority_without_double_lock(
        self,
    ) -> None:
        ready = self.root / "web-child-ready.txt"
        probe = (
            "import sys,time; from pathlib import Path; "
            "from live_betting.service_coordination import "
            "database_writer_authority; "
            "db=Path(sys.argv[sys.argv.index('--database')+1]); "
            "authority=database_writer_authority(db); authority.__enter__(); "
            "Path(sys.argv[-1]).write_text('ready', encoding='ascii'); "
            "time.sleep(30)"
        )
        command = [
            sys.executable,
            "-c",
            probe,
            "--database",
            str(self.database.resolve()),
            str(ready),
        ]
        service = self.make_service(
            project_dir=Path(__file__).resolve().parents[1],
            popen_factory=subprocess.Popen,
            process_factory=psutil.Process,
        )
        service.command_for = lambda _component, _database: list(command)  # type: ignore[method-assign]

        try:
            started = service.execute(
                self.connection,
                database_path=self.database,
                component="raybet_collector",
                action="start",
                request_id="request-real-web-child-start",
                client_host="127.0.0.1",
            )
            self.assertTrue(started["ok"], started)
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "managed child did not enter authority")
            for lock_path in (
                self.database.with_suffix(".service.lock"),
                self.database.with_suffix(".web.lock"),
            ):
                with self.assertRaisesRegex(RuntimeError, "already held"):
                    with SingleInstanceLock(lock_path):
                        pass

            stopped = service.execute(
                self.connection,
                database_path=self.database,
                component="raybet_collector",
                action="stop",
                request_id="request-real-web-child-stop",
                client_host="127.0.0.1",
            )
            self.assertTrue(stopped["ok"], stopped)
        finally:
            try:
                service.shutdown(self.connection, database_path=self.database)
            finally:
                service.close()
        self.assertEqual(
            list(self.root.glob(".*.manager-child-authority.*.json")),
            [],
        )

    def test_statuses_does_not_commit_the_callers_transaction(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute(
            """INSERT INTO monitor_alert_candidates
               (dedupe_key, first_detected_at, last_detected_at, payload_json)
               VALUES ('transaction-probe', '2026-07-18T00:00:00Z',
                       '2026-07-18T00:00:00Z', '{}')"""
        )

        self.service.statuses(
            self.connection,
            database_path=self.database,
        )

        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM monitor_alert_candidates"
            ).fetchone()[0],
            0,
        )

    def test_runtime_verification_failure_releases_idle_service_lock(self) -> None:
        self.connection.execute("DROP TRIGGER monitor_control_audit_no_update")
        self.connection.commit()

        with self.assertRaisesRegex(RuntimeError, "missing objects"):
            self.execute("start", "request-runtime-schema-failure")

        self.assertEqual(self.popen_calls, [])
        with SingleInstanceLock(self.database.with_suffix(".service.lock")):
            pass

    def test_all_component_paths_follow_candidate_database_parent(self) -> None:
        candidate = self.root / "candidate-volume" / "restore" / "dota2.db"
        live_root = candidate.resolve().parent / "live_betting"
        commands = {
            component: self.service.command_for(component, candidate)
            for component in (
                "raybet_collector",
                "shadow_monitor",
                "vision_supervisor",
                "draft_publisher",
                "mail_worker",
            )
        }

        for command in commands.values():
            self.assertIn(str(candidate.resolve()), command)
            self.assertIn("--schema-prepared", command)
        self.assertIn(str(live_root / "raw-v2"), commands["raybet_collector"])
        self.assertIn(
            str(live_root / "live_observations"),
            commands["shadow_monitor"],
        )
        vision = commands["vision_supervisor"]
        self.assertIn(str(live_root / "live_observations"), vision)
        self.assertIn(str(live_root / "live_evidence"), vision)
        self.assertIn(str(live_root / "watcher_logs"), vision)
        self.assertFalse((self.root / "data").exists())

    def test_candidate_start_writes_logs_only_beside_candidate_database(self) -> None:
        candidate = self.root / "candidate-volume" / "restore" / "dota2.db"
        candidate.parent.mkdir(parents=True)
        sqlite3.connect(candidate).close()
        service = self.make_service()

        with ExitStack() as locks:
            for lock_path in database_web_authority_lock_paths(candidate):
                locks.enter_context(SingleInstanceLock(lock_path))
            started = service.execute(
                self.connection,
                database_path=candidate,
                component="vision_supervisor",
                action="start",
                request_id="request-candidate-path-start",
                client_host="127.0.0.1",
            )

            self.assertTrue(started["ok"])
            managed_logs = (
                candidate.resolve().parent / "live_betting" / "logs" / "managed"
            )
            self.assertTrue(
                (managed_logs / "vision_supervisor.stdout.log").is_file()
            )
            self.assertTrue(
                (managed_logs / "vision_supervisor.stderr.log").is_file()
            )
            self.assertFalse((self.root / "data").exists())
            stopped = service.execute(
                self.connection,
                database_path=candidate,
                component="vision_supervisor",
                action="stop",
                request_id="request-candidate-path-stop",
                client_host="127.0.0.1",
            )
            self.assertTrue(stopped["ok"])
        service.close()

    def test_prepared_verifier_failure_spawns_nothing_and_preserves_registry(
        self,
    ) -> None:
        def fail_verification(*_: object, **__: object) -> None:
            raise RuntimeError("candidate is not prepared")

        service = self.make_service(database_verifier=fail_verification)
        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-verifier-failed",
            client_host="127.0.0.1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "database_verification_failed")
        self.assertEqual(self.popen_calls, [])
        self.assertIsNone(
            self.connection.execute(
                "SELECT * FROM monitor_process_registry "
                "WHERE component='raybet_collector'"
            ).fetchone()
        )
        service.close()

    def test_prepared_verifier_runs_while_manager_lock_is_held(self) -> None:
        verified_under_lock: list[bool] = []

        def verify(*_: object, **__: object) -> None:
            with self.assertRaisesRegex(RuntimeError, "already held"):
                with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                    pass
            verified_under_lock.append(True)

        service = self.make_service(database_verifier=verify)
        started = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-verifier-lock",
            client_host="127.0.0.1",
        )

        self.assertTrue(started["ok"])
        self.assertEqual(verified_under_lock, [True])
        stopped = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="stop",
            request_id="request-verifier-lock-stop",
            client_host="127.0.0.1",
        )
        self.assertTrue(stopped["ok"])
        service.close()

    def test_external_supervisor_lock_fails_closed_before_spawn(self) -> None:
        lock_path = self.database.with_suffix(".service.lock")
        with SingleInstanceLock(lock_path):
            result = self.execute("start", "request-external-lock")

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "service_lock_held")
        self.assertEqual(self.popen_calls, [])

    def test_web_service_upgrade_fails_fast_and_preserves_web_authority(self) -> None:
        with ExitStack() as supervisor:
            for lock_path in database_service_authority_lock_paths(self.database):
                supervisor.enter_context(SingleInstanceLock(lock_path))
            blocked = self.execute("start", "request-upgrade-blocked")

        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["result"], "service_lock_held")
        self.assertEqual(self.popen_calls, [])
        for lock_path in database_web_authority_lock_paths(self.database):
            with self.assertRaisesRegex(RuntimeError, "already held"):
                with SingleInstanceLock(lock_path):
                    pass

        started = self.execute("start", "request-upgrade-after-release")
        self.assertTrue(started["ok"])

    def test_control_action_rejects_hardlinked_database(self) -> None:
        alias = self.root / "control-alias.db"
        os.link(self.database, alias)

        with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
            self.execute("start", "request-hardlink-start")

        self.assertEqual(self.popen_calls, [])

    def test_identity_change_under_lock_retains_database_authority(self) -> None:
        identity = require_unique_database_file(self.database)
        assert identity is not None

        with (
            patch(
                "web.control.require_unique_database_file",
                side_effect=[identity, RuntimeError("identity changed")],
            ),
            self.assertRaisesRegex(RuntimeError, "identity changed"),
        ):
            self.execute("start", "request-identity-race")

        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass
        self.service.close()

    def test_writer_scan_database_alias_blocks_spawn(self) -> None:
        alias = self.root / "control-runtime-alias.db"

        def add_alias(
            database: Path,
            *,
            allowed_identities: tuple[ProcessIdentity, ...],
        ) -> WriterScanResult:
            self.assertEqual(database, self.database.resolve())
            self.assertEqual(allowed_identities, ())
            os.link(database, alias)
            return WriterScanResult((), ())

        service = self.make_service(writer_scanner=add_alias)
        try:
            with self.assertRaisesRegex(RuntimeError, "exactly one hard link"):
                service.execute(
                    self.connection,
                    database_path=self.database,
                    component="raybet_collector",
                    action="start",
                    request_id="request-runtime-alias",
                    client_host="127.0.0.1",
                )
            self.assertEqual(self.popen_calls, [])
        finally:
            alias.unlink(missing_ok=True)
            service._writer_scanner = self.scan_writers
            service.close()

    def test_restart_retains_service_lock_until_last_managed_writer_stops(self) -> None:
        started = self.execute("start", "request-lock-start")
        first = self.processes[int(started["pid"])]

        restarted = self.execute("restart", "request-lock-restart")

        self.assertTrue(restarted["ok"])
        self.assertEqual(restarted["result"], "restarted")
        self.assertEqual(first.terminate_calls, 1)
        self.assertEqual(len(self.popen_calls), 2)
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass

        stopped = self.execute("stop", "request-lock-stop")
        self.assertTrue(stopped["ok"])
        with SingleInstanceLock(self.database.with_suffix(".service.lock")):
            pass

    def test_second_control_service_cannot_stop_first_managers_process(self) -> None:
        started = self.execute("start", "request-first-manager-start")
        process = self.processes[int(started["pid"])]
        second = self.make_service()

        stopped = second.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="stop",
            request_id="request-second-manager-stop",
            client_host="127.0.0.1",
        )

        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["result"], "service_lock_held")
        self.assertEqual(process.terminate_calls, 0)
        second.close()
        self.assertTrue(self.execute("stop", "request-first-manager-stop")["ok"])

    def test_idle_control_shutdown_is_read_only_under_supervisor_lock(self) -> None:
        service = self.make_service()

        with SingleInstanceLock(self.database.with_suffix(".service.lock")):
            service.shutdown(self.connection, database_path=self.database)

        service.close()

    def test_close_refuses_to_release_lock_for_live_child(self) -> None:
        self.execute("start", "request-close-live-start")

        with self.assertRaisesRegex(RuntimeError, "live or unverifiable"):
            self.service.close()
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass

        self.assertTrue(self.execute("stop", "request-close-live-stop")["ok"])

    def test_identity_access_denied_is_fail_closed_without_registry_change(self) -> None:
        started = self.execute("start", "request-access-start")
        process = self.processes[int(started["pid"])]
        process.command_error = psutil.AccessDenied(process.pid)

        status = self.service.statuses(
            self.connection,
            database_path=self.database,
        )[0]
        stopped = self.execute("stop", "request-access-stop")
        row = self.connection.execute(
            "SELECT pid, status FROM monitor_process_registry "
            "WHERE component='raybet_collector'"
        ).fetchone()

        self.assertEqual(status["status"], "identity_unverifiable")
        self.assertFalse(status["control_allowed"])
        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["result"], "identity_unverifiable")
        self.assertEqual(tuple(row), (process.pid, "running"))
        self.assertEqual(process.terminate_calls, 0)
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass

        process.command_error = None
        self.assertTrue(self.execute("stop", "request-access-retry")["ok"])

    def test_stop_captures_replenished_descendant_at_fixed_point(self) -> None:
        started = self.execute("start", "request-descendant-start")
        parent = self.processes[int(started["pid"])]
        child = FakeProcess(8301, ["python-test", "watch_raybet_stream.py"])
        self.processes[child.pid] = child
        parent.children_sequence = [[], [child], [child]]

        stopped = self.execute("stop", "request-descendant-stop")

        self.assertTrue(stopped["ok"])
        self.assertEqual(stopped["result"], "stopped")
        self.assertEqual(parent.suspend_calls, 1)
        self.assertEqual(child.suspend_calls, 1)
        self.assertEqual(child.terminate_calls, 1)
        self.assertFalse(child.running)

    def test_capture_failure_resumes_only_successfully_suspended_identities(self) -> None:
        started = self.execute("start", "request-suspend-failure-start")
        parent = self.processes[int(started["pid"])]
        child = FakeProcess(8303, ["python-test", "watch_raybet_stream.py"])
        child.suspend_error = psutil.AccessDenied(child.pid)
        self.processes[child.pid] = child
        parent.children_sequence = [[child]]

        stopped = self.execute("stop", "request-suspend-failure-stop")

        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["result"], "stop_failed")
        self.assertIn("suspend_failed:AccessDenied", str(stopped["detail"]))
        self.assertEqual(parent.resume_calls, 1)
        self.assertEqual(child.resume_calls, 0)
        self.assertEqual(parent.terminate_calls, 0)
        self.assertEqual(child.terminate_calls, 0)

    def test_stop_failure_keeps_registry_and_lock_for_live_descendant(self) -> None:
        started = self.execute("start", "request-live-descendant-start")
        parent = self.processes[int(started["pid"])]
        child = FakeProcess(8302, ["python-test", "watch_raybet_stream.py"])
        child.ignore_terminate = True
        child.ignore_kill = True
        self.processes[child.pid] = child
        parent.children_sequence = [[child], [child]]

        stopped = self.execute("stop", "request-live-descendant-stop")
        row = self.connection.execute(
            "SELECT pid, status FROM monitor_process_registry "
            "WHERE component='raybet_collector'"
        ).fetchone()

        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["result"], "stop_failed")
        self.assertIn("process_still_alive:8302", str(stopped["detail"]))
        self.assertEqual(tuple(row), (parent.pid, "running"))
        self.assertEqual(child.resume_calls, 1)
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass

    def test_restart_does_not_spawn_when_kill_or_verification_fails(self) -> None:
        started = self.execute("start", "request-restart-failure-start")
        process = self.processes[int(started["pid"])]
        process.ignore_terminate = True
        process.kill_error = psutil.AccessDenied(process.pid)

        restarted = self.execute("restart", "request-restart-failure")

        self.assertFalse(restarted["ok"])
        self.assertEqual(restarted["result"], "stop_failed")
        self.assertIn("kill_failed", str(restarted["detail"]))
        self.assertEqual(len(self.popen_calls), 1)
        row = self.connection.execute(
            "SELECT pid, status FROM monitor_process_registry "
            "WHERE component='raybet_collector'"
        ).fetchone()
        self.assertEqual(tuple(row), (process.pid, "running"))
        self.assertEqual(process.resume_calls, 1)

    def test_shutdown_stops_all_registered_trees_and_releases_authority(self) -> None:
        first = self.execute("start", "request-shutdown-first")
        second = self.service.execute(
            self.connection,
            database_path=self.database,
            component="vision_supervisor",
            action="start",
            request_id="request-shutdown-second",
            client_host="127.0.0.1",
        )

        self.service.shutdown(self.connection, database_path=self.database)

        self.assertFalse(self.processes[int(first["pid"])].running)
        self.assertFalse(self.processes[int(second["pid"])].running)
        rows = self.connection.execute(
            "SELECT status, pid FROM monitor_process_registry ORDER BY component"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(tuple(row) == ("stopped", None) for row in rows))
        with SingleInstanceLock(self.database.with_suffix(".service.lock")):
            pass

    def test_shutdown_continues_other_trees_and_retains_lock_on_failure(self) -> None:
        first = self.execute("start", "request-shutdown-failure-first")
        second = self.service.execute(
            self.connection,
            database_path=self.database,
            component="vision_supervisor",
            action="start",
            request_id="request-shutdown-failure-second",
            client_host="127.0.0.1",
        )
        failed = self.processes[int(first["pid"])]
        failed.ignore_terminate = True
        failed.ignore_kill = True

        with self.assertRaisesRegex(RuntimeError, "control shutdown incomplete"):
            self.service.shutdown(self.connection, database_path=self.database)

        self.assertTrue(failed.running)
        self.assertEqual(failed.resume_calls, 1)
        self.assertFalse(self.processes[int(second["pid"])].running)
        rows = {
            str(row["component"]): tuple(row)
            for row in self.connection.execute(
                "SELECT component, status, pid FROM monitor_process_registry"
            )
        }
        self.assertEqual(
            rows["raybet_collector"],
            ("raybet_collector", "running", failed.pid),
        )
        self.assertEqual(
            rows["vision_supervisor"],
            ("vision_supervisor", "stopped", None),
        )
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass

        failed.ignore_kill = False
        self.service.shutdown(self.connection, database_path=self.database)

    def test_pid_reuse_before_suspend_never_touches_replacement_process(self) -> None:
        started = self.execute("start", "request-pid-reuse-start")
        process = self.processes[int(started["pid"])]
        identity_calls = 0

        def reused_identity() -> float:
            nonlocal identity_calls
            identity_calls += 1
            return (
                process.created_at
                if identity_calls <= 2
                else process.created_at + 0.005
            )

        process.create_time = reused_identity  # type: ignore[method-assign]

        stopped = self.execute("stop", "request-pid-reuse-stop")

        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["result"], "stop_failed")
        self.assertEqual(
            stopped["detail"],
            "root_identity_changed_before_suspend",
        )
        self.assertEqual(process.suspend_calls, 0)
        self.assertEqual(process.terminate_calls, 0)
        row = self.connection.execute(
            "SELECT pid, status FROM monitor_process_registry "
            "WHERE component='raybet_collector'"
        ).fetchone()
        self.assertEqual(tuple(row), (process.pid, "running"))

    def test_registry_rejects_five_millisecond_create_time_change(self) -> None:
        started = self.execute("start", "request-ctime-exact-start")
        process = self.processes[int(started["pid"])]
        process.created_at += 0.005

        status = self.service.statuses(
            self.connection,
            database_path=self.database,
        )
        collector = next(
            item for item in status if item["component"] == "raybet_collector"
        )

        self.assertEqual(collector["status"], "identity_mismatch")

    def test_registry_missing_orphan_blocks_spawn_and_holds_manager_lock(self) -> None:
        scan_result = WriterScanResult((ProcessIdentity(9901, 123.0),), ())

        def scan(
            _: Path,
            *,
            allowed_identities: tuple[ProcessIdentity, ...],
        ) -> WriterScanResult:
            self.assertEqual(allowed_identities, ())
            return scan_result

        service = self.make_service(writer_scanner=scan)
        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-orphan-start",
            client_host="127.0.0.1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "orphan_writer_conflict")
        self.assertEqual(self.popen_calls, [])
        self.assertIsNone(
            self.connection.execute(
                "SELECT * FROM monitor_process_registry "
                "WHERE component='raybet_collector'"
            ).fetchone()
        )
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass
        with self.assertRaisesRegex(RuntimeError, "live or unverifiable"):
            service.close()

        service._writer_scanner = self.scan_writers
        service.close()

    def test_orphan_scan_allows_only_verified_registry_identity(self) -> None:
        first = self.execute("start", "request-allowlist-first")
        identity = ProcessIdentity(
            int(first["pid"]),
            float(first["process_created_at"]),
        )

        second = self.service.execute(
            self.connection,
            database_path=self.database,
            component="vision_supervisor",
            action="start",
            request_id="request-allowlist-second",
            client_host="127.0.0.1",
        )

        self.assertTrue(second["ok"])
        self.assertEqual(self.scanner_calls[-1][1], (identity,))
        self.assertTrue(
            self.service.execute(
                self.connection,
                database_path=self.database,
                component="vision_supervisor",
                action="stop",
                request_id="request-allowlist-stop-second",
                client_host="127.0.0.1",
            )["ok"]
        )
        self.assertTrue(self.execute("stop", "request-allowlist-stop-first")["ok"])

    def test_request_id_cannot_be_reused_for_another_action(self) -> None:
        started = self.execute("start", "request-reused-action")
        process = self.processes[int(started["pid"])]

        conflict = self.execute("stop", "request-reused-action")

        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["result"], "request_id_conflict")
        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM monitor_control_audit WHERE request_id=?",
                ("request-reused-action",),
            ).fetchone()[0],
            1,
        )

    def test_stop_validates_process_identity_before_termination(self) -> None:
        started = self.execute("start", "request-start-002")
        process = self.processes[int(started["pid"])]
        process.command = ["python-test", "unrelated.py"]

        stopped = self.execute("stop", "request-stop-002")

        self.assertFalse(stopped["ok"])
        self.assertEqual(stopped["result"], "identity_mismatch")
        self.assertEqual(process.terminate_calls, 0)

    def test_start_identity_mismatch_reaps_spawned_process(self) -> None:
        spawned: list[FakeProcess] = []

        def popen(command: list[str], **_: object) -> FakePopen:
            process = FakeProcess(5100, ["python-test", "unrelated.py"])
            self.processes[process.pid] = process
            spawned.append(process)
            return FakePopen(process.pid)

        service = self.make_service(popen_factory=popen)

        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-start-mismatch",
            client_host="127.0.0.1",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "start_identity_mismatch")
        self.assertEqual(spawned[0].terminate_calls, 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT pid FROM monitor_process_registry WHERE component='raybet_collector'"
            ).fetchone()
        )

    def test_popen_identity_failure_is_immediately_cleaned_up(self) -> None:
        factory_calls = 0

        def transient_factory(pid: int) -> FakeProcess:
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 1:
                raise psutil.AccessDenied(pid)
            return self.processes[pid]

        service = self.make_service(process_factory=transient_factory)

        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-transient-identity-failure",
            client_host="127.0.0.1",
        )

        process = next(iter(self.processes.values()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "start_identity_mismatch")
        self.assertFalse(process.running)
        self.assertEqual(process.terminate_calls, 1)
        service.close()

    def test_popen_value_error_is_immediately_cleaned_up(self) -> None:
        factory_calls = 0

        def malformed_factory(pid: int) -> FakeProcess:
            nonlocal factory_calls
            factory_calls += 1
            if factory_calls == 1:
                raise ValueError("malformed process identity")
            return self.processes[pid]

        service = self.make_service(process_factory=malformed_factory)

        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-value-error-identity",
            client_host="127.0.0.1",
        )

        process = next(iter(self.processes.values()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "start_identity_mismatch")
        self.assertFalse(process.running)
        self.assertEqual(process.terminate_calls, 1)
        service.close()

    def test_create_time_no_such_process_still_uses_popen_cleanup(self) -> None:
        identity_reads = 0

        def popen(command: list[str], **_: object) -> FakePopen:
            process = FakeProcess(5200, command)

            def create_time() -> float:
                nonlocal identity_reads
                identity_reads += 1
                if identity_reads == 1:
                    raise psutil.NoSuchProcess(process.pid)
                return process.created_at

            process.create_time = create_time  # type: ignore[method-assign]
            self.processes[process.pid] = process
            return FakePopen(process.pid, process)

        service = self.make_service(popen_factory=popen)

        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-create-time-no-such-process",
            client_host="127.0.0.1",
        )

        process = self.processes[5200]
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "start_identity_mismatch")
        self.assertFalse(process.running)
        self.assertEqual(process.terminate_calls, 1)
        service.close()

    def test_unverified_popen_is_retained_for_shutdown_recovery(self) -> None:
        def inaccessible_factory(pid: int) -> FakeProcess:
            raise psutil.AccessDenied(pid)

        service = self.make_service(process_factory=inaccessible_factory)
        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-persistent-identity-failure",
            client_host="127.0.0.1",
        )

        process = next(iter(self.processes.values()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "start_cleanup_failed")
        self.assertTrue(process.running)
        blocked = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-persistent-identity-retry",
            client_host="127.0.0.1",
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["result"], "unverified_child_present")
        self.assertEqual(len(self.popen_calls), 1)
        with self.assertRaisesRegex(RuntimeError, "already held"):
            with SingleInstanceLock(self.database.with_suffix(".service.lock")):
                pass

        service._process = self.processes.__getitem__
        service.shutdown(self.connection, database_path=self.database)
        self.assertFalse(process.running)
        with SingleInstanceLock(self.database.with_suffix(".service.lock")):
            pass
        service.close()

    def test_bind_keyboard_interrupt_terminates_and_cleans_authority(self) -> None:
        with patch(
            "web.control.bind_manager_child_authority",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.execute("start", "request-bind-keyboard-interrupt")

        process = next(iter(self.processes.values()))
        self.assertFalse(process.running)
        self.assertEqual(process.terminate_calls, 1)
        database = self.database.resolve()
        self.assertFalse(self.service._unverified_processes.get(database))
        self.assertFalse(self.service._child_authorities.get(database))

    def test_registration_system_exit_terminates_and_cleans_authority(self) -> None:
        with patch("web.control._utc_now", side_effect=SystemExit(17)):
            with self.assertRaisesRegex(SystemExit, "17"):
                self.execute("start", "request-registration-system-exit")

        process = next(iter(self.processes.values()))
        self.assertFalse(process.running)
        self.assertEqual(process.terminate_calls, 1)
        database = self.database.resolve()
        self.assertFalse(self.service._unverified_processes.get(database))
        self.assertFalse(self.service._child_authorities.get(database))

    def test_bind_interrupt_with_unproven_termination_stays_quarantined(self) -> None:
        with (
            patch(
                "web.control.bind_manager_child_authority",
                side_effect=KeyboardInterrupt(),
            ),
            patch(
                "web.control.terminate_subprocess_tree",
                return_value=type("Result", (), {"ok": False, "detail": "still alive"})(),
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.execute("start", "request-bind-interrupt-quarantine")

        database = self.database.resolve()
        self.assertTrue(self.service._unverified_processes.get(database))
        self.assertTrue(self.service._child_authorities.get(database))

    def test_registration_failure_reaps_process_and_is_audited(self) -> None:
        def reject_process_registration(
            action: int,
            arg1: str | None,
            _arg2: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if (
                action == sqlite3.SQLITE_INSERT
                and arg1 == "monitor_process_registry"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.connection.set_authorizer(reject_process_registration)
        try:
            result = self.execute("start", "request-start-registry-failure")
        finally:
            self.connection.set_authorizer(None)
        process = next(iter(self.processes.values()))

        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "registration_failed")
        self.assertEqual(process.terminate_calls, 1)
        audit = self.connection.execute(
            "SELECT result, ok FROM monitor_control_audit WHERE request_id=?",
            ("request-start-registry-failure",),
        ).fetchone()
        self.assertEqual(tuple(audit), ("registration_failed", 0))

    def test_concurrent_services_only_spawn_component_once(self) -> None:
        prepare_runtime_schema(self.connection)
        processes: dict[int, FakeProcess] = {}
        popen_calls: list[int] = []
        services: list[ControlService] = []
        calls_lock = threading.Lock()
        first_spawned = threading.Event()
        second_spawned = threading.Event()

        def popen(command: list[str], **_: object) -> FakePopen:
            with calls_lock:
                pid = 7000 + len(popen_calls)
                popen_calls.append(pid)
                process = FakeProcess(pid, list(command))
                processes[pid] = process
                is_first = len(popen_calls) == 1
            if is_first:
                first_spawned.set()
                self.assertFalse(second_spawned.wait(timeout=0.5))
            else:
                second_spawned.set()
            return FakePopen(pid)

        def run(request_id: str) -> dict[str, object]:
            connection = sqlite3.connect(self.database, timeout=5)
            connection.row_factory = sqlite3.Row
            service = self.make_service(
                popen_factory=popen,
                process_factory=processes.__getitem__,
            )
            with calls_lock:
                services.append(service)
            try:
                return service.execute(
                    connection,
                    database_path=self.database,
                    component="raybet_collector",
                    action="start",
                    request_id=request_id,
                    client_host="127.0.0.1",
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run, "request-concurrent-1")
            self.assertTrue(first_spawned.wait(timeout=2))
            second = executor.submit(run, "request-concurrent-2")
            results = [first.result(timeout=5), second.result(timeout=5)]

        self.assertEqual(popen_calls, [7000])
        self.assertEqual(
            sorted(str(result["result"]) for result in results),
            ["service_lock_held", "started"],
        )
        for process in processes.values():
            process.running = False
        for service in services:
            service.close()

    def test_audit_rows_are_append_only(self) -> None:
        prepare_runtime_schema(self.connection)
        self.execute("start", "request-start-003")

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE monitor_control_audit SET result='changed' WHERE request_id=?",
                ("request-start-003",),
            )
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM monitor_control_audit WHERE request_id=?",
                ("request-start-003",),
            )

    def test_supervisor_heartbeat_disables_duplicate_component_control(self) -> None:
        health = [
            {
                "component": "raybet_worker",
                "status": "healthy",
                "freshness": "fresh",
                "last_heartbeat_at": "2026-07-16T00:00:00+00:00",
            }
        ]
        service = self.make_service(health_provider=lambda _: health)

        status = service.statuses(
            self.connection,
            database_path=self.database,
        )[0]
        result = service.execute(
            self.connection,
            database_path=self.database,
            component="raybet_collector",
            action="start",
            request_id="request-supervisor-managed",
            client_host="127.0.0.1",
        )

        self.assertEqual(status["status"], "running")
        self.assertFalse(status["control_allowed"])
        self.assertEqual(status["detail"], "managed by unified supervisor")
        self.assertFalse(result["ok"])
        self.assertEqual(result["result"], "externally_managed")
        self.assertEqual(self.popen_calls, [])


class StubControlService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.shutdown_calls: list[Path] = []
        self.close_calls = 0

    def statuses(self, connection: sqlite3.Connection, *, database_path: Path) -> list[dict[str, object]]:
        return [
            {
                "component": "raybet_collector",
                "status": "stopped",
                "pid": None,
                "control_allowed": True,
            }
        ]

    def execute(self, connection: sqlite3.Connection, **kwargs: object) -> dict[str, object]:
        call = {key: str(value) for key, value in kwargs.items()}
        self.calls.append(call)
        return {
            "ok": True,
            "component": call["component"],
            "action": call["action"],
            "result": "started",
            "pid": 4321,
            "request_id": call["request_id"],
            "idempotent": False,
        }

    def shutdown(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: Path,
    ) -> None:
        self.shutdown_calls.append(database_path)

    def close(self) -> None:
        self.close_calls += 1


class MonitorControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "api.db"
        sqlite3.connect(self.database).close()
        self.previous_path = queries.DB_PATH
        queries.init_db(str(self.database))
        self.stub = StubControlService()
        self.service_patch = patch.object(control_router, "control_service", self.stub)
        self.service_patch.start()
        control_router.control_sessions.clear()

    def tearDown(self) -> None:
        self.service_patch.stop()
        control_router.control_sessions.clear()
        queries.init_db(self.previous_path)
        self.directory.cleanup()

    def test_session_and_mutation_require_loopback_and_csrf(self) -> None:
        async def scenario() -> None:
            remote_transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 50000))
            async with httpx.AsyncClient(transport=remote_transport, base_url="http://127.0.0.1") as remote:
                response = await remote.get("/api/monitor/control/session")
                self.assertEqual(response.status_code, 403)

            local_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=local_transport, base_url="http://127.0.0.1") as client:
                session = await client.get("/api/monitor/control/session")
                self.assertEqual(session.status_code, 200)
                token = session.json()["csrf_token"]

                components_without_csrf = await client.get(
                    "/api/monitor/control/components"
                )
                self.assertEqual(components_without_csrf.status_code, 403)
                components = await client.get(
                    "/api/monitor/control/components",
                    headers={"X-Monitor-CSRF": token},
                )
                self.assertEqual(components.status_code, 200)

                missing = await client.post(
                    "/api/monitor/control/raybet_collector/start",
                    json={"request_id": "request-api-001"},
                )
                self.assertEqual(missing.status_code, 403)

                accepted = await client.post(
                    "/api/monitor/control/raybet_collector/start",
                    headers={"X-Monitor-CSRF": token},
                    json={"request_id": "request-api-001"},
                )
                self.assertEqual(accepted.status_code, 200)
                self.assertEqual(len(self.stub.calls), 1)

        asyncio.run(scenario())

    def test_fastapi_lifespan_shuts_down_control_service_and_sessions(self) -> None:
        session_id, csrf_token, _ = control_router.control_sessions.issue()

        async def scenario() -> None:
            async with _lifespan(app):
                self.assertTrue(
                    control_router.control_sessions.valid(session_id, csrf_token)
                )

        asyncio.run(scenario())

        self.assertEqual(self.stub.shutdown_calls, [Path(queries.DB_PATH)])
        self.assertEqual(self.stub.close_calls, 1)
        self.assertFalse(control_router.control_sessions.valid(session_id, csrf_token))

    def test_fetch_shutdown_failure_still_runs_control_shutdown(self) -> None:
        async def scenario() -> None:
            async with _lifespan(app):
                pass

        with (
            patch(
                "web.app._shutdown_fetch_process",
                side_effect=RuntimeError("fetch cleanup failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "fetch cleanup failed"),
        ):
            asyncio.run(scenario())

        self.assertEqual(self.stub.shutdown_calls, [Path(queries.DB_PATH)])
        self.assertEqual(self.stub.close_calls, 1)

    def test_session_rejects_dns_rebinding_host_and_cross_origin(self) -> None:
        async def scenario() -> None:
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://attacker.example:8000",
            ) as rebound:
                malicious_host = await rebound.get("/api/monitor/control/session")

            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
            ) as local:
                malicious_origin = await local.get(
                    "/api/monitor/control/session",
                    headers={"Origin": "http://attacker.example:8000"},
                )
                same_origin = await local.get(
                    "/api/monitor/control/session",
                    headers={"Origin": "http://127.0.0.1:8000"},
                )

            self.assertEqual(malicious_host.status_code, 403)
            self.assertEqual(malicious_origin.status_code, 403)
            self.assertEqual(same_origin.status_code, 200)

        asyncio.run(scenario())

    def test_api_rejects_command_text_and_unknown_components(self) -> None:
        async def scenario() -> None:
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                token = (await client.get("/api/monitor/control/session")).json()["csrf_token"]
                headers = {"X-Monitor-CSRF": token}
                command = await client.post(
                    "/api/monitor/control/raybet_collector/start",
                    headers=headers,
                    json={"request_id": "request-api-002", "command": "calc.exe"},
                )
                unknown = await client.post(
                    "/api/monitor/control/not-allowed/start",
                    headers=headers,
                    json={"request_id": "request-api-003"},
                )

            self.assertEqual(command.status_code, 422)
            self.assertEqual(unknown.status_code, 404)
            self.assertEqual(self.stub.calls, [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
