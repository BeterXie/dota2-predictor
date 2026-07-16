from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from web import queries
from web.app import app
from web.control import ControlService, initialize_control_schema
from web.routers import control as control_router


class FakeProcess:
    def __init__(self, pid: int, command: list[str], created_at: float = 1_700_000_000.0) -> None:
        self.pid = pid
        self.command = command
        self.created_at = created_at
        self.terminate_calls = 0
        self.kill_calls = 0

    def create_time(self) -> float:
        return self.created_at

    def cmdline(self) -> list[str]:
        return list(self.command)

    def is_running(self) -> bool:
        return True

    def status(self) -> str:
        return "running"

    def children(self, recursive: bool = False) -> list[FakeProcess]:
        return []

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        return 0


class FakePopen:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class MonitorControlServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "control.db"
        self.connection = sqlite3.connect(self.database)
        self.connection.row_factory = sqlite3.Row
        self.processes: dict[int, FakeProcess] = {}
        self.popen_calls: list[tuple[list[str], dict[str, object]]] = []

        def popen(command: list[str], **kwargs: object) -> FakePopen:
            pid = 4100 + len(self.popen_calls)
            self.popen_calls.append((list(command), kwargs))
            self.processes[pid] = FakeProcess(pid, list(command))
            return FakePopen(pid)

        self.service = ControlService(
            project_dir=self.root,
            python_executable="python-test",
            popen_factory=popen,
            process_factory=self.processes.__getitem__,
        )

    def tearDown(self) -> None:
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
        command = self.popen_calls[0][0]
        self.assertEqual(command[:4], ["python-test", "-u", "-m", "live_betting.monitor"])
        self.assertIn(str(self.database.resolve()), command)

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

        service = ControlService(
            project_dir=self.root,
            python_executable="python-test",
            popen_factory=popen,
            process_factory=self.processes.__getitem__,
        )

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

    def test_registration_failure_reaps_process_and_is_audited(self) -> None:
        initialize_control_schema(self.connection)
        self.connection.execute(
            """CREATE TRIGGER reject_process_registration
               BEFORE INSERT ON monitor_process_registry
               BEGIN
                   SELECT RAISE(ABORT, 'registration rejected');
               END"""
        )
        self.connection.commit()

        result = self.execute("start", "request-start-registry-failure")
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
        initialize_control_schema(self.connection)
        processes: dict[int, FakeProcess] = {}
        popen_calls: list[int] = []
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
            service = ControlService(
                project_dir=self.root,
                python_executable="python-test",
                popen_factory=popen,
                process_factory=processes.__getitem__,
            )
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
            ["already_running", "started"],
        )

    def test_audit_rows_are_append_only(self) -> None:
        initialize_control_schema(self.connection)
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
        service = ControlService(
            project_dir=self.root,
            python_executable="python-test",
            popen_factory=self.service._popen,
            process_factory=self.processes.__getitem__,
            health_provider=lambda _: health,
        )

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


class MonitorControlApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "api.db"
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
