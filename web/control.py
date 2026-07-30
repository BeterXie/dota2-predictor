"""Local process control backed by PostgreSQL audit state."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from database.session import DatabaseRow, PostgresSession
from live_betting.runtime_schema import CONTROL_COMPONENT_NAMES, verify_runtime_schema
from live_betting.process_control import (
    ProcessIdentity,
    command_comparison_key,
    terminate_process_tree,
    terminate_subprocess_tree,
)


@dataclass(frozen=True)
class ComponentSpec:
    label: str
    arguments: tuple[str, ...]


COMPONENTS: dict[str, ComponentSpec] = {
    "raybet_collector": ComponentSpec(
        "RayBet collector",
        (
            "-u",
            "-m",
            "live_betting.monitor",
            "--raw-dir",
            "{odds_raw_root}",
            "--interval",
            "6",
            "--list-interval",
            "30",
            "--schema-prepared",
        ),
    ),
    "shadow_monitor": ComponentSpec(
        "Shadow monitor",
        (
            "-u",
            "-m",
            "live_betting.shadow_monitor",
            "--vision-jsonl",
            "{vision_observations}",
            "--schema-prepared",
        ),
    ),
    "vision_supervisor": ComponentSpec(
        "Vision supervisor",
        (
            "-u",
            "scripts/supervise_raybet_streams.py",
            "--output-dir",
            "{vision_observations}",
            "--evidence-dir",
            "{vision_evidence}",
            "--log-dir",
            "{vision_logs}",
            "--schema-prepared",
        ),
    ),
    "draft_publisher": ComponentSpec(
        "Draft prediction publisher",
        (
            "-u",
            "-m",
            "live_betting.draft_publisher",
        ),
    ),
    "mail_worker": ComponentSpec(
        "Mail worker",
        (
            "-u",
            "scripts/run_notification_worker.py",
            "--schema-prepared",
        ),
    ),
}

if tuple(COMPONENTS) != CONTROL_COMPONENT_NAMES:
    raise RuntimeError("monitor control components drifted from runtime schema")

ACTIONS = {"start", "stop", "restart"}


class ControlService:
    def __init__(
        self,
        *,
        project_dir: Path,
        python_executable: str = sys.executable,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        process_factory: Callable[[int], Any] = psutil.Process,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.python_executable = python_executable
        self._popen = popen_factory
        self._process = process_factory
        self._owned: dict[str, tuple[Any, ProcessIdentity]] = {}
        self._lock = threading.RLock()

    def command_for(self, component: str) -> list[str]:
        root = self.project_dir / "data" / "live_betting"
        values = {
            "odds_raw_root": str(root / "raw-v2"),
            "vision_observations": str(root / "vision_observations"),
            "vision_evidence": str(root / "vision_evidence"),
            "vision_logs": str(root / "watcher_logs"),
        }
        return [
            self.python_executable,
            *(argument.format(**values) for argument in COMPONENTS[component].arguments),
        ]

    def statuses(self, connection: PostgresSession) -> list[dict[str, object]]:
        verify_runtime_schema(connection)
        with self._lock:
            return [
                self._status(connection, component, spec)
                for component, spec in COMPONENTS.items()
            ]

    def execute(
        self,
        connection: PostgresSession,
        *,
        component: str,
        action: str,
        request_id: str,
        client_host: str,
    ) -> dict[str, object]:
        if component not in COMPONENTS:
            raise KeyError(component)
        if action not in ACTIONS:
            raise ValueError(action)
        verify_runtime_schema(connection)
        with self._lock, connection.transaction():
            existing = connection.execute(
                "SELECT response_json FROM monitor_control_audit WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                payload = json.loads(str(existing[0]))
                if not isinstance(payload, dict):
                    raise RuntimeError("stored control response is invalid")
                return payload

            if action == "start":
                result = self._start(connection, component)
            elif action == "stop":
                result = self._stop(connection, component)
            else:
                stopped = self._stop(connection, component)
                result = self._start(connection, component) if stopped["ok"] else stopped
            response = {
                "component": component,
                "action": action,
                **result,
            }
            self._audit(
                connection,
                request_id=request_id,
                component=component,
                action=action,
                client_host=client_host,
                response=response,
            )
            return response

    def close(self) -> None:
        with self._lock:
            for component, (process, identity) in tuple(self._owned.items()):
                result = terminate_subprocess_tree(
                    process,
                    process_factory=self._process,
                )
                if not result.ok:
                    try:
                        running = self._process(identity.pid)
                    except (psutil.Error, OSError):
                        continue
                    terminate_process_tree(
                        running,
                        process_factory=self._process,
                        expected_root=identity,
                    )
                self._owned.pop(component, None)

    def shutdown(self, connection: PostgresSession, **_: object) -> None:
        with self._lock, connection.transaction():
            for component in tuple(self._owned):
                self._stop(connection, component)

    def _start(
        self,
        connection: PostgresSession,
        component: str,
    ) -> dict[str, object]:
        command = self.command_for(component)
        row = self._registry_row(connection, component)
        state, identity = self._inspect(row, command)
        if state == "running":
            return {
                "ok": True,
                "status": "running",
                "pid": identity.pid if identity else None,
                "detail": "already running",
            }
        process = self._popen(
            command,
            cwd=str(self.project_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            env=os.environ.copy(),
        )
        try:
            observed = self._process(int(process.pid))
            identity = ProcessIdentity(int(process.pid), float(observed.create_time()))
            if process.poll() is not None:
                raise RuntimeError("managed process exited during registration")
        except BaseException:
            terminate_subprocess_tree(process, process_factory=self._process)
            raise
        self._owned[component] = (process, identity)
        now = datetime.now(timezone.utc).isoformat()
        command_json = json.dumps(command, separators=(",", ":"))
        connection.execute(
            """INSERT INTO monitor_process_registry
               (component, pid, command_hash, command_json, process_created_at,
                started_at, status, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
               ON CONFLICT(component) DO UPDATE SET
                 pid=excluded.pid,
                 command_hash=excluded.command_hash,
                 command_json=excluded.command_json,
                 process_created_at=excluded.process_created_at,
                 started_at=excluded.started_at,
                 status='running',
                 updated_at=excluded.updated_at""",
            (
                component,
                identity.pid,
                self._command_hash(command),
                command_json,
                identity.created_at,
                now,
                now,
            ),
        )
        return {
            "ok": True,
            "status": "running",
            "pid": identity.pid,
            "detail": "started",
        }

    def _stop(
        self,
        connection: PostgresSession,
        component: str,
    ) -> dict[str, object]:
        command = self.command_for(component)
        row = self._registry_row(connection, component)
        state, identity = self._inspect(row, command)
        if state == "running" and identity is not None:
            owned = self._owned.get(component)
            if owned is not None and owned[1] == identity:
                result = terminate_subprocess_tree(
                    owned[0],
                    process_factory=self._process,
                )
            else:
                result = terminate_process_tree(
                    self._process(identity.pid),
                    process_factory=self._process,
                    expected_root=identity,
                )
            if not result.ok:
                return {
                    "ok": False,
                    "status": state,
                    "pid": identity.pid,
                    "detail": result.detail or "stop failed",
                }
        self._owned.pop(component, None)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """UPDATE monitor_process_registry
                  SET pid=NULL, process_created_at=NULL, status='stopped',
                      updated_at=?
                WHERE component=?""",
            (now, component),
        )
        return {"ok": True, "status": "stopped", "pid": None, "detail": "stopped"}

    def _status(
        self,
        connection: PostgresSession,
        component: str,
        spec: ComponentSpec,
    ) -> dict[str, object]:
        row = self._registry_row(connection, component)
        state, identity = self._inspect(row, self.command_for(component))
        return {
            "component": component,
            "label": spec.label,
            "status": state,
            "pid": identity.pid if identity is not None else None,
            "started_at": row["started_at"] if row is not None else None,
            "detail": None,
            "control_allowed": state not in {"identity_mismatch", "identity_unverifiable"},
        }

    def _inspect(
        self,
        row: DatabaseRow | None,
        expected_command: list[str],
    ) -> tuple[str, ProcessIdentity | None]:
        if row is None or row["status"] != "running" or row["pid"] is None:
            return "stopped", None
        try:
            identity = ProcessIdentity(
                int(row["pid"]),
                float(row["process_created_at"]),
            )
            process = self._process(identity.pid)
            if abs(float(process.create_time()) - identity.created_at) > 1e-3:
                return "identity_mismatch", None
            actual = list(process.cmdline())
        except psutil.NoSuchProcess:
            return "stopped", None
        except (psutil.Error, OSError, TypeError, ValueError):
            return "identity_unverifiable", None
        if command_comparison_key(actual) != command_comparison_key(expected_command):
            return "identity_mismatch", None
        return "running", identity

    @staticmethod
    def _registry_row(
        connection: PostgresSession,
        component: str,
    ) -> DatabaseRow | None:
        return connection.execute(
            "SELECT * FROM monitor_process_registry WHERE component=?",
            (component,),
        ).fetchone()

    @staticmethod
    def _command_hash(command: list[str]) -> str:
        payload = json.dumps(command_comparison_key(command), separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _audit(
        self,
        connection: PostgresSession,
        *,
        request_id: str,
        component: str,
        action: str,
        client_host: str,
        response: dict[str, object],
    ) -> None:
        row = self._registry_row(connection, component)
        connection.execute(
            """INSERT INTO monitor_control_audit
               (request_id, component, action, result, ok, pid, command_hash,
                process_created_at, client_host, requested_at, response_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id,
                component,
                action,
                str(response["detail"]),
                int(bool(response["ok"])),
                response.get("pid"),
                row["command_hash"] if row is not None else None,
                row["process_created_at"] if row is not None else None,
                client_host,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(response, separators=(",", ":"), default=str),
            ),
        )


__all__ = ["ACTIONS", "COMPONENTS", "ComponentSpec", "ControlService"]
