from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from .monitoring import derive_health


@dataclass(frozen=True)
class ComponentSpec:
    label: str
    arguments: tuple[str, ...]


COMPONENTS: dict[str, ComponentSpec] = {
    "raybet_collector": ComponentSpec(
        "RayBet collector",
        (
            "-u", "-m", "live_betting.monitor", "--database", "{database}",
            "--raw-dir", "{project}/data/live_betting/raw", "--interval", "6",
            "--list-interval", "30",
        ),
    ),
    "shadow_monitor": ComponentSpec(
        "Shadow monitor",
        (
            "-u", "-m", "live_betting.shadow_monitor", "--database", "{database}",
            "--vision-jsonl", "{project}/data/live_betting/live_observations",
        ),
    ),
    "vision_supervisor": ComponentSpec(
        "Vision supervisor",
        ("-u", "scripts/supervise_raybet_streams.py", "--database", "{database}"),
    ),
    "draft_publisher": ComponentSpec(
        "Draft prediction publisher",
        (
            "-u", "-m", "live_betting.draft_publisher", "--database",
            "{database}", "--schema-prepared",
        ),
    ),
    "mail_worker": ComponentSpec(
        "Mail worker",
        ("-u", "scripts/run_notification_worker.py", "--database", "{database}"),
    ),
}

ACTIONS = {"start", "stop", "restart"}
_SUPERVISOR_HEALTH_COMPONENTS = {
    "raybet_collector": "raybet_worker",
    "shadow_monitor": "shadow_worker",
    "vision_supervisor": "vision_worker",
    "draft_publisher": "draft_publisher_worker",
    "mail_worker": "mail_worker",
}


def initialize_control_schema(connection: sqlite3.Connection) -> None:
    component_values = ", ".join(f"'{name}'" for name in COMPONENTS)
    table_statements = {
        "monitor_process_registry": f"""CREATE TABLE monitor_process_registry (
            component TEXT PRIMARY KEY CHECK (component IN ({component_values})),
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
            component TEXT NOT NULL CHECK (component IN ({component_values})),
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
    trigger_statements = (
        """CREATE TRIGGER monitor_control_audit_no_update
        BEFORE UPDATE ON monitor_control_audit
        BEGIN
            SELECT RAISE(ABORT, 'monitor control audit rows are immutable');
        END""",
        """CREATE TRIGGER monitor_control_audit_no_delete
        BEFORE DELETE ON monitor_control_audit
        BEGIN
            SELECT RAISE(ABORT, 'monitor control audit rows cannot be deleted');
        END""",
    )
    existing = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute(
            """SELECT name, sql FROM sqlite_master
                 WHERE type='table' AND name IN
                       ('monitor_process_registry', 'monitor_control_audit')"""
        ).fetchall()
    }
    needs_rebuild = any(
        name in existing and "'draft_publisher'" not in existing[name]
        for name in table_statements
    )
    if not needs_rebuild:
        for name, statement in table_statements.items():
            if name not in existing:
                connection.execute(statement)
        installed_triggers = {
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master WHERE type='trigger'
                     AND name IN ('monitor_control_audit_no_update',
                                  'monitor_control_audit_no_delete')"""
            ).fetchall()
        }
        for statement, name in zip(
            trigger_statements,
            ("monitor_control_audit_no_update", "monitor_control_audit_no_delete"),
            strict=True,
        ):
            if name not in installed_triggers:
                connection.execute(statement)
        connection.commit()
        return

    legacy_suffix = "__component_migration"
    connection.execute("BEGIN IMMEDIATE")
    try:
        for trigger in (
            "monitor_control_audit_no_update",
            "monitor_control_audit_no_delete",
        ):
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        migrated: list[str] = []
        for name in table_statements:
            legacy = f"{name}{legacy_suffix}"
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (legacy,),
            ).fetchone() is not None:
                raise RuntimeError(f"unfinished control schema migration: {legacy}")
            if name in existing:
                connection.execute(f'ALTER TABLE "{name}" RENAME TO "{legacy}"')
                migrated.append(name)
            connection.execute(table_statements[name])
        if "monitor_process_registry" in migrated:
            connection.execute(
                """INSERT INTO monitor_process_registry
                   (component, pid, command_hash, command_json,
                    process_created_at, started_at, status, updated_at)
                   SELECT component, pid, command_hash, command_json,
                          process_created_at, started_at, status, updated_at
                     FROM monitor_process_registry__component_migration"""
            )
        if "monitor_control_audit" in migrated:
            connection.execute(
                """INSERT INTO monitor_control_audit
                   (audit_id, request_id, component, action, result, ok, pid,
                    command_hash, process_created_at, client_host, requested_at,
                    response_json)
                   SELECT audit_id, request_id, component, action, result, ok, pid,
                          command_hash, process_created_at, client_host, requested_at,
                          response_json
                     FROM monitor_control_audit__component_migration"""
            )
        for statement in trigger_statements:
            connection.execute(statement)
        for name in migrated:
            connection.execute(f'DROP TABLE "{name}{legacy_suffix}"')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


class ControlService:
    def __init__(
        self,
        *,
        project_dir: Path,
        python_executable: str = sys.executable,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        process_factory: Callable[[int], Any] = psutil.Process,
        health_provider: Callable[[sqlite3.Connection], list[dict[str, Any]]] = derive_health,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.python_executable = python_executable
        self._popen = popen_factory
        self._process = process_factory
        self._health_provider = health_provider
        self._lock = threading.RLock()

    def command_for(self, component: str, database_path: Path) -> list[str]:
        spec = COMPONENTS[component]
        values = {
            "database": str(database_path.resolve()),
            "project": str(self.project_dir),
        }
        return [self.python_executable, *(argument.format(**values) for argument in spec.arguments)]

    def statuses(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: Path,
    ) -> list[dict[str, object]]:
        initialize_control_schema(connection)
        results: list[dict[str, object]] = []
        with self._lock:
            supervisor_health = self._supervisor_health(connection)
            for component, spec in COMPONENTS.items():
                command = self.command_for(component, database_path)
                row = connection.execute(
                    "SELECT * FROM monitor_process_registry WHERE component=?",
                    (component,),
                ).fetchone()
                state, _, detail = self._inspect(row, command)
                if state == "stale" and row is not None:
                    self._mark_stopped(connection, component)
                externally_managed = (
                    state in {"missing", "stale", "stopped"}
                    and component in supervisor_health
                )
                results.append(
                    {
                        "component": component,
                        "label": spec.label,
                        "status": (
                            "running"
                            if externally_managed
                            else "stopped"
                            if state in {"missing", "stale", "stopped"}
                            else state
                        ),
                        "pid": (
                            None
                            if externally_managed
                            else int(row["pid"])
                            if row is not None and row["pid"] is not None
                            else None
                        ),
                        "started_at": (
                            supervisor_health[component].get("last_heartbeat_at")
                            if externally_managed
                            else row["started_at"]
                            if row is not None
                            else None
                        ),
                        "detail": (
                            "managed by unified supervisor"
                            if externally_managed
                            else detail
                        ),
                        "control_allowed": not externally_managed,
                    }
                )
            connection.commit()
        return results

    def execute(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: Path,
        component: str,
        action: str,
        request_id: str,
        client_host: str,
    ) -> dict[str, object]:
        if component not in COMPONENTS:
            raise KeyError(component)
        if action not in ACTIONS:
            raise ValueError(action)
        initialize_control_schema(connection)
        with self._lock:
            spawned_process: Any | None = None
            try:
                # The database write lock coordinates independent web processes too.
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """SELECT component, action, response_json
                         FROM monitor_control_audit WHERE request_id=?""",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["component"]) != component
                        or str(existing["action"]) != action
                    ):
                        connection.commit()
                        return {
                            "ok": False,
                            "result": "request_id_conflict",
                            "pid": None,
                            "command_hash": None,
                            "process_created_at": None,
                            "detail": "request_id is already bound to another control action",
                            "component": component,
                            "action": action,
                            "request_id": request_id,
                            "idempotent": False,
                        }
                    response = json.loads(str(existing["response_json"]))
                    response["idempotent"] = True
                    connection.commit()
                    return response

                command = self.command_for(component, database_path)
                row = connection.execute(
                    "SELECT * FROM monitor_process_registry WHERE component=?",
                    (component,),
                ).fetchone()
                state, _, _ = self._inspect(row, command)
                externally_managed = (
                    state in {"missing", "stale", "stopped"}
                    and component in self._supervisor_health(connection)
                )
                if externally_managed:
                    if state == "stale" and row is not None:
                        self._mark_stopped(connection, component)
                    response = self._result(
                        False,
                        "externally_managed",
                        detail="component is managed by the unified supervisor",
                    )
                elif action == "start":
                    response, spawned_process = self._start(connection, component, command)
                elif action == "stop":
                    response = self._stop(connection, component, command)
                else:
                    response, spawned_process = self._restart(connection, component, command)

                response.update(
                    {
                        "component": component,
                        "action": action,
                        "request_id": request_id,
                        "idempotent": False,
                    }
                )
                self._record_audit(
                    connection,
                    response=response,
                    component=component,
                    action=action,
                    request_id=request_id,
                    client_host=client_host,
                )
                connection.commit()
                return response
            except Exception:
                connection.rollback()
                if spawned_process is not None:
                    self._terminate_process_tree(spawned_process)
                raise

    def _supervisor_health(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        by_component = {
            str(item.get("component")): item
            for item in self._health_provider(connection)
        }
        result: dict[str, dict[str, Any]] = {}
        for component, health_component in _SUPERVISOR_HEALTH_COMPONENTS.items():
            health = by_component.get(health_component)
            if health is None:
                continue
            if health.get("status") not in {"healthy", "degraded"}:
                continue
            if health.get("freshness") not in {"fresh", "delayed"}:
                continue
            result[component] = health
        return result

    def _start(
        self,
        connection: sqlite3.Connection,
        component: str,
        command: list[str],
    ) -> tuple[dict[str, object], Any | None]:
        row = connection.execute(
            "SELECT * FROM monitor_process_registry WHERE component=?",
            (component,),
        ).fetchone()
        state, process, detail = self._inspect(row, command)
        if state == "running":
            return self._result(True, "already_running", row=row), None
        if state == "identity_mismatch":
            return self._result(False, state, row=row, detail=detail), None
        if row is not None:
            self._mark_stopped(connection, component)

        log_dir = self.project_dir / "data" / "live_betting" / "logs" / "managed"
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{component}.stdout.log"
        stderr_path = log_dir / f"{component}.stderr.log"
        stdout = stdout_path.open("a", encoding="utf-8")
        stderr = stderr_path.open("a", encoding="utf-8")
        try:
            process_handle = self._popen(
                command,
                cwd=str(self.project_dir),
                stdout=stdout,
                stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        finally:
            stdout.close()
            stderr.close()

        try:
            process = self._process(int(process_handle.pid))
            created_at = float(process.create_time())
            actual_command = list(process.cmdline())
        except (psutil.Error, KeyError, OSError) as error:
            self._terminate_process_tree(process_handle)
            return self._result(False, "start_failed", detail=type(error).__name__), None

        expected_hash = _command_hash(command)
        if _command_hash(actual_command) != expected_hash:
            self._terminate_process_tree(process)
            return (
                self._result(
                    False,
                    "start_identity_mismatch",
                    detail="spawned command did not match allowlist",
                ),
                None,
            )

        now = _utc_now()
        try:
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
                    int(process_handle.pid),
                    expected_hash,
                    json.dumps(command, ensure_ascii=True, separators=(",", ":")),
                    created_at,
                    now,
                    now,
                ),
            )
        except sqlite3.Error as error:
            self._terminate_process_tree(process)
            return self._result(False, "registration_failed", detail=type(error).__name__), None
        return (
            self._result(
                True,
                "started",
                pid=int(process_handle.pid),
                command_hash=expected_hash,
                process_created_at=created_at,
            ),
            process,
        )

    def _stop(
        self,
        connection: sqlite3.Connection,
        component: str,
        command: list[str],
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM monitor_process_registry WHERE component=?",
            (component,),
        ).fetchone()
        state, process, detail = self._inspect(row, command)
        if state in {"missing", "stopped", "stale"}:
            if row is not None:
                self._mark_stopped(connection, component)
            return self._result(True, "already_stopped", row=row)
        if state == "identity_mismatch" or process is None:
            return self._result(False, "identity_mismatch", row=row, detail=detail)

        self._terminate_process_tree(process)

        old_pid = int(row["pid"])
        self._mark_stopped(connection, component)
        return self._result(
            True,
            "stopped",
            pid=old_pid,
            command_hash=str(row["command_hash"]),
            process_created_at=float(row["process_created_at"]),
        )

    def _restart(
        self,
        connection: sqlite3.Connection,
        component: str,
        command: list[str],
    ) -> tuple[dict[str, object], Any | None]:
        stopped = self._stop(connection, component, command)
        if not bool(stopped["ok"]):
            return stopped, None
        started, spawned_process = self._start(connection, component, command)
        if bool(started["ok"]) and started["result"] == "started":
            started["result"] = "restarted"
        return started, spawned_process

    @staticmethod
    def _terminate_process_tree(process: Any) -> None:
        try:
            children = list(process.children(recursive=True))
        except (AttributeError, psutil.Error, OSError):
            children = []
        targets = [*children, process]
        for target in targets:
            try:
                target.terminate()
            except (AttributeError, psutil.Error, OSError):
                continue
        for target in targets:
            try:
                target.wait(timeout=8)
            except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
                try:
                    target.kill()
                    target.wait(timeout=3)
                except (AttributeError, psutil.Error, OSError, subprocess.SubprocessError):
                    continue
            except (AttributeError, psutil.Error, OSError, subprocess.SubprocessError):
                continue

    def _inspect(
        self,
        row: sqlite3.Row | None,
        expected_command: list[str],
    ) -> tuple[str, Any | None, str | None]:
        if row is None:
            return "missing", None, None
        if str(row["status"]) != "running" or row["pid"] is None:
            return "stopped", None, None
        try:
            process = self._process(int(row["pid"]))
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return "stale", None, "registered process is no longer running"
            actual_created_at = float(process.create_time())
            actual_hash = _command_hash(list(process.cmdline()))
        except (psutil.NoSuchProcess, KeyError, OSError):
            return "stale", None, "registered PID no longer exists"
        if abs(actual_created_at - float(row["process_created_at"])) > 0.01:
            return "identity_mismatch", None, "PID creation time changed"
        expected_hash = _command_hash(expected_command)
        if str(row["command_hash"]) != expected_hash or actual_hash != expected_hash:
            return "identity_mismatch", None, "PID command does not match allowlist"
        return "running", process, None

    @staticmethod
    def _mark_stopped(connection: sqlite3.Connection, component: str) -> None:
        connection.execute(
            """UPDATE monitor_process_registry
                  SET pid=NULL, process_created_at=NULL, status='stopped', updated_at=?
                WHERE component=?""",
            (_utc_now(), component),
        )

    @staticmethod
    def _result(
        ok: bool,
        result: str,
        *,
        row: sqlite3.Row | None = None,
        pid: int | None = None,
        command_hash: str | None = None,
        process_created_at: float | None = None,
        detail: str | None = None,
    ) -> dict[str, object]:
        if row is not None:
            pid = int(row["pid"]) if row["pid"] is not None else pid
            command_hash = str(row["command_hash"])
            process_created_at = (
                float(row["process_created_at"])
                if row["process_created_at"] is not None
                else process_created_at
            )
        return {
            "ok": ok,
            "result": result,
            "pid": pid,
            "command_hash": command_hash,
            "process_created_at": process_created_at,
            "detail": detail,
        }

    @staticmethod
    def _record_audit(
        connection: sqlite3.Connection,
        *,
        response: dict[str, object],
        component: str,
        action: str,
        request_id: str,
        client_host: str,
    ) -> None:
        payload = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
        connection.execute(
            """INSERT INTO monitor_control_audit
               (request_id, component, action, result, ok, pid, command_hash,
                process_created_at, client_host, requested_at, response_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id,
                component,
                action,
                str(response["result"]),
                int(bool(response["ok"])),
                response.get("pid"),
                response.get("command_hash"),
                response.get("process_created_at"),
                client_host,
                _utc_now(),
                payload,
            ),
        )


def _command_hash(command: list[str]) -> str:
    normalized = [os.path.normcase(item) if os.name == "nt" else item for item in command]
    payload = json.dumps(normalized, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
