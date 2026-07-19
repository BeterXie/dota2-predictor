from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from live_betting.database_protocol import verify_prepared_database
from live_betting.runtime_schema import (
    CONTROL_COMPONENT_NAMES,
    verify_runtime_schema,
)
from live_betting.service_coordination import (
    DatabaseFileIdentity,
    ProcessIdentity,
    SingleInstanceLock,
    TerminationResult,
    WriterScanResult,
    bind_manager_child_authority,
    command_comparison_key,
    database_service_authority_lock_paths,
    database_service_lock_path,
    manager_child_authority,
    manager_child_process_environment,
    managed_child_command,
    resolve_process_identity,
    require_unique_database_file,
    scan_managed_writers,
    service_data_paths,
    terminate_process_tree,
    terminate_subprocess_tree,
)

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
            "--raw-dir", "{odds_raw_root}", "--interval", "6",
            "--list-interval", "30", "--schema-prepared",
        ),
    ),
    "shadow_monitor": ComponentSpec(
        "Shadow monitor",
        (
            "-u", "-m", "live_betting.shadow_monitor", "--database", "{database}",
            "--vision-jsonl", "{vision_observations}", "--schema-prepared",
        ),
    ),
    "vision_supervisor": ComponentSpec(
        "Vision supervisor",
        (
            "-u", "scripts/supervise_raybet_streams.py", "--database", "{database}",
            "--output-dir", "{vision_observations}",
            "--evidence-dir", "{vision_evidence}",
            "--log-dir", "{vision_logs}", "--schema-prepared",
        ),
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
        (
            "-u", "scripts/run_notification_worker.py", "--database", "{database}",
            "--schema-prepared",
        ),
    ),
}

if tuple(COMPONENTS) != CONTROL_COMPONENT_NAMES:
    raise RuntimeError("monitor control components drifted from runtime schema")

ACTIONS = {"start", "stop", "restart"}
_SUPERVISOR_HEALTH_COMPONENTS = {
    "raybet_collector": "raybet_worker",
    "shadow_monitor": "shadow_worker",
    "vision_supervisor": "vision_worker",
    "draft_publisher": "draft_publisher_worker",
    "mail_worker": "mail_worker",
}


class ControlService:
    def __init__(
        self,
        *,
        project_dir: Path,
        python_executable: str = sys.executable,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        process_factory: Callable[[int], Any] = psutil.Process,
        health_provider: Callable[[sqlite3.Connection], list[dict[str, Any]]] = derive_health,
        service_lock_factory: Callable[[Path], Any] = SingleInstanceLock,
        database_verifier: Callable[..., Any] = verify_prepared_database,
        writer_scanner: Callable[..., WriterScanResult] = scan_managed_writers,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.python_executable = python_executable
        self._popen = popen_factory
        self._process = process_factory
        self._health_provider = health_provider
        self._service_lock_factory = service_lock_factory
        self._database_verifier = database_verifier
        self._writer_scanner = writer_scanner
        self._service_locks: dict[Path, Any] = {}
        self._database_identities: dict[Path, DatabaseFileIdentity] = {}
        self._owned_processes: dict[Path, dict[str, ProcessIdentity]] = {}
        self._child_authorities: dict[Path, dict[str, Any]] = {}
        self._unverified_processes: dict[
            Path, dict[int, tuple[str, Any]]
        ] = {}
        self._retained_service_locks: set[Path] = set()
        self._authority_release_failures: set[Path] = set()
        self._lock = threading.RLock()

    def command_for(self, component: str, database_path: Path) -> list[str]:
        spec = COMPONENTS[component]
        paths = service_data_paths(database_path)
        values = {
            "database": str(paths.database),
            "odds_raw_root": str(paths.odds_raw_root),
            "vision_observations": str(paths.vision_observations),
            "vision_evidence": str(paths.vision_evidence),
            "vision_logs": str(paths.vision_logs),
            "project": str(self.project_dir),
        }
        return managed_child_command(
            [
                self.python_executable,
                *(argument.format(**values) for argument in spec.arguments),
            ]
        )

    def statuses(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: Path,
    ) -> list[dict[str, object]]:
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
                externally_managed = (
                    state in {"missing", "stale", "stopped"}
                    and component in supervisor_health
                )
                unsafe_identity = state in {
                    "identity_mismatch",
                    "identity_unverifiable",
                }
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
                        "control_allowed": (
                            not externally_managed
                            and not unsafe_identity
                        ),
                    }
                )
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
        with self._lock:
            database = self._database_key(database_path)
            prelock_identity = require_unique_database_file(
                database,
                expected_identity=self._database_identities.get(database),
            )
            assert prelock_identity is not None
            if not self._acquire_service_lock(database):
                response = self._result(
                    False,
                    "service_lock_held",
                    detail="database service lock is held by another manager",
                )
                response.update({
                    "component": component,
                    "action": action,
                    "request_id": request_id,
                    "idempotent": False,
                })
                return response
            self._database_identities[database] = prelock_identity
            try:
                locked_identity = require_unique_database_file(
                    database,
                    expected_identity=prelock_identity,
                )
                assert locked_identity is not None
                self._database_identities[database] = locked_identity
                verify_runtime_schema(connection)
            except Exception:
                try:
                    self._release_service_lock_if_idle(connection, database)
                except Exception:
                    self._retained_service_locks.add(database)
                raise
            spawned_process: Any | None = None
            try:
                database = self._database_key(database_path)
                initial_identity = require_unique_database_file(
                    database,
                    expected_identity=self._database_identities.get(database),
                )
                assert initial_identity is not None
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
                        self._release_service_lock_if_idle(connection, database)
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
                    self._release_service_lock_if_idle(connection, database)
                    return response

                if not self._acquire_service_lock(database_path):
                    response = self._result(
                        False,
                        "service_lock_held",
                        detail="database service lock is held by another manager",
                    )
                else:
                    locked_identity = require_unique_database_file(
                        database,
                        expected_identity=initial_identity,
                    )
                    assert locked_identity is not None
                    self._database_identities[database] = locked_identity
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
                        response, spawned_process = self._start(
                            connection,
                            database_path,
                            component,
                            command,
                        )
                    elif action == "stop":
                        response = self._stop(
                            connection,
                            database_path,
                            component,
                            command,
                        )
                    else:
                        response, spawned_process = self._restart(
                            connection,
                            database_path,
                            component,
                            command,
                        )

                response.update(
                    {
                        "component": component,
                        "action": action,
                        "request_id": request_id,
                        "idempotent": False,
                        }
                    )
                require_unique_database_file(
                    database,
                    expected_identity=locked_identity,
                )
                self._record_audit(
                    connection,
                    response=response,
                    component=component,
                    action=action,
                    request_id=request_id,
                    client_host=client_host,
                )
                if self._must_retain_service_lock(response):
                    self._retained_service_locks.add(
                        self._database_key(database_path)
                    )
                else:
                    self._release_service_lock_if_idle(connection, database_path)
                connection.commit()
                return response
            except BaseException:
                connection.rollback()
                cleanup_ok = True
                if spawned_process is not None:
                    expected_identity = self._owned_processes.get(
                        self._database_key(database_path), {}
                    ).get(component)
                    cleanup_ok = self._terminate_process_tree(
                        spawned_process,
                        expected_root=expected_identity,
                    ).ok
                database = self._database_key(database_path)
                if cleanup_ok:
                    self._owned_processes.setdefault(
                        database, {}
                    ).pop(component, None)
                    unverified = self._unverified_processes.get(database, {})
                    if not any(
                        item_component == component
                        for item_component, _ in unverified.values()
                    ):
                        self._release_child_authority(database, component)
                if database in self._service_locks:
                    self._retained_service_locks.add(database)
                raise

    def close(self) -> None:
        """Release locks only when this manager owns no live child identity."""

        with self._lock:
            for database, components in tuple(self._owned_processes.items()):
                for identity in components.values():
                    alive, _, error = resolve_process_identity(
                        identity,
                        self._process,
                    )
                    if error is not None or alive:
                        raise RuntimeError(
                            "cannot close control manager with live or "
                            "unverifiable children"
                        )
                self._owned_processes.pop(database, None)
            for database, processes in tuple(self._unverified_processes.items()):
                for _, process_handle in processes.values():
                    try:
                        if process_handle.poll() is None:
                            raise RuntimeError(
                                "cannot close control manager with live or "
                                "unverifiable children"
                            )
                    except (AttributeError, OSError) as error:
                        raise RuntimeError(
                            "cannot close control manager with live or "
                            "unverifiable children"
                        ) from error
                self._unverified_processes.pop(database, None)
            for database in tuple(self._child_authorities):
                self._release_all_child_authorities(database)
            for database in tuple(self._service_locks):
                expected_identity = self._database_identities.get(database)
                if expected_identity is None:
                    raise RuntimeError(
                        "cannot close control manager without database identity"
                    )
                try:
                    require_unique_database_file(
                        database,
                        expected_identity=expected_identity,
                    )
                    scan = self._writer_scanner(database, allowed_identities=())
                    require_unique_database_file(
                        database,
                        expected_identity=expected_identity,
                    )
                except Exception as error:
                    raise RuntimeError(
                        "cannot close control manager without database authority"
                    ) from error
                if not scan.safe:
                    raise RuntimeError(
                        "cannot close control manager with live or "
                        "unverifiable children"
                    )
            for database in tuple(self._service_locks):
                self._release_service_lock(database)

    def shutdown(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: Path,
    ) -> None:
        """Stop every owned/registered tree before releasing database authority."""

        with self._lock:
            database = self._database_key(database_path)
            owned = self._owned_processes.setdefault(database, {})
            unverified = self._unverified_processes.setdefault(database, {})
            if (
                not owned
                and not unverified
                and not self._child_authorities.get(database)
                and database not in self._service_locks
                and not self._registered_process_requires_shutdown(
                    connection,
                    database,
                )
            ):
                self._owned_processes.pop(database, None)
                self._unverified_processes.pop(database, None)
                return
            targets: dict[str, tuple[ProcessIdentity, Any]] = {}
            errors: list[str] = []
            expected_database_identity = self._database_identities.get(database)
            try:
                current_database_identity = require_unique_database_file(
                    database,
                    expected_identity=expected_database_identity,
                )
            except Exception as error:
                current_database_identity = None
                errors.append(
                    f"database:identity_unverifiable:{type(error).__name__}:{error}"
                )
            if not self._acquire_service_lock(database):
                raise RuntimeError(
                    "control shutdown incomplete: database:service_lock_held"
                )
            if current_database_identity is not None:
                try:
                    locked_database_identity = require_unique_database_file(
                        database,
                        expected_identity=current_database_identity,
                    )
                except Exception as error:
                    errors.append(
                        "database:identity_changed_under_lock:"
                        f"{type(error).__name__}:{error}"
                    )
                else:
                    assert locked_database_identity is not None
                    self._database_identities[database] = locked_database_identity
            try:
                connection.execute("BEGIN IMMEDIATE")
                for component in COMPONENTS:
                    command = self.command_for(component, database)
                    row = connection.execute(
                        "SELECT * FROM monitor_process_registry WHERE component=?",
                        (component,),
                    ).fetchone()
                    state, process, detail = self._inspect(row, command)
                    if state == "running" and row is not None and process is not None:
                        identity = ProcessIdentity(
                            int(row["pid"]),
                            float(row["process_created_at"]),
                        )
                        owned[component] = identity
                        targets[component] = (identity, process)
                        continue
                    if state in {"identity_mismatch", "identity_unverifiable"}:
                        errors.append(f"{component}:{state}:{detail}")
                        continue
                    if state == "stale" and row is not None:
                        self._mark_stopped(connection, component)
                        self._release_child_authority(database, component)

                    identity = owned.get(component)
                    if identity is None:
                        continue
                    alive, owned_process, resolve_error = resolve_process_identity(
                        identity,
                        self._process,
                    )
                    if resolve_error is not None:
                        errors.append(
                            f"{component}:identity_unverifiable:{resolve_error}"
                        )
                    elif alive and owned_process is not None:
                        targets[component] = (identity, owned_process)
                    else:
                        owned.pop(component, None)
                        self._release_child_authority(database, component)

                has_authority = database in self._service_locks
                if (targets or unverified or errors or has_authority) and not has_authority:
                    if not self._acquire_service_lock(database):
                        errors.append("database:service_lock_held")
                    else:
                        has_authority = True

                if has_authority:
                    if current_database_identity is not None:
                        try:
                            locked_database_identity = require_unique_database_file(
                                database,
                                expected_identity=current_database_identity,
                            )
                        except Exception as error:
                            errors.append(
                                "database:identity_changed_under_lock:"
                                f"{type(error).__name__}:{error}"
                            )
                        else:
                            assert locked_database_identity is not None
                            self._database_identities[database] = (
                                locked_database_identity
                            )
                    for component, (identity, process) in targets.items():
                        termination = self._terminate_process_tree(
                            process,
                            expected_root=identity,
                        )
                        if not termination.ok:
                            errors.append(
                                f"{component}:termination_failed:{termination.detail}"
                            )
                            continue
                        self._mark_stopped(connection, component)
                        owned.pop(component, None)
                        self._release_child_authority(database, component)
                    for token, (component, process_handle) in tuple(
                        unverified.items()
                    ):
                        termination = terminate_subprocess_tree(
                            process_handle,
                            process_factory=self._process,
                        )
                        if not termination.ok:
                            errors.append(
                                f"{component}:unverified_termination_failed:"
                                f"{termination.detail}"
                            )
                            continue
                        unverified.pop(token, None)
                        self._release_child_authority(database, component)
                connection.commit()
            except Exception:
                connection.rollback()
                self._retained_service_locks.add(database)
                raise

            if errors:
                self._retained_service_locks.add(database)
                raise RuntimeError(
                    "control shutdown incomplete: " + ";".join(errors)
                )
            self._release_service_lock_if_idle(connection, database)
            if database in self._service_locks:
                self._retained_service_locks.add(database)
                raise RuntimeError(
                    "control shutdown incomplete: database authority remains"
                )
            if not owned:
                self._owned_processes.pop(database, None)
            if not unverified:
                self._unverified_processes.pop(database, None)

    @staticmethod
    def _must_retain_service_lock(response: dict[str, object]) -> bool:
        return str(response.get("result")) in {
            "identity_mismatch",
            "identity_unverifiable",
            "orphan_writer_conflict",
            "start_cleanup_failed",
            "stop_failed",
            "database_identity_changed",
            "unverified_child_present",
            "writer_scan_failed",
            "writer_scan_unverifiable",
        }

    def _database_key(self, database_path: Path) -> Path:
        return database_path.resolve()

    def _service_lock_path(self, database_path: Path) -> Path:
        return database_service_lock_path(self._database_key(database_path))

    def _acquire_service_lock(self, database_path: Path) -> bool:
        database = self._database_key(database_path)
        if database in self._service_locks:
            return True
        candidate = ExitStack()
        try:
            for lock_path in database_service_authority_lock_paths(database):
                candidate.enter_context(self._service_lock_factory(lock_path))
        except (OSError, RuntimeError):
            candidate.close()
            return False
        self._service_locks[database] = candidate
        return True

    def _release_service_lock(self, database_path: Path) -> None:
        database = self._database_key(database_path)
        if self._child_authorities.get(database):
            raise RuntimeError(
                "cannot release service lock while child authority is published"
            )
        if database in self._authority_release_failures:
            raise RuntimeError(
                "cannot release service lock after child authority cleanup failed"
            )
        held = self._service_locks.pop(database, None)
        if held is not None:
            held.__exit__(None, None, None)
        self._retained_service_locks.discard(database)
        self._database_identities.pop(database, None)

    def _release_service_lock_if_idle(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
    ) -> None:
        database = self._database_key(database_path)
        if database not in self._service_locks:
            return
        if self._unverified_processes.get(database):
            return
        for component in COMPONENTS:
            row = connection.execute(
                "SELECT * FROM monitor_process_registry WHERE component=?",
                (component,),
            ).fetchone()
            state, _, _ = self._inspect(row, self.command_for(component, database))
            if state in {
                "running",
                "identity_mismatch",
                "identity_unverifiable",
            }:
                return
        expected_identity = self._database_identities.get(database)
        if expected_identity is None:
            self._retained_service_locks.add(database)
            return
        try:
            require_unique_database_file(
                database,
                expected_identity=expected_identity,
            )
            scan = self._writer_scanner(database, allowed_identities=())
            require_unique_database_file(
                database,
                expected_identity=expected_identity,
            )
        except Exception:
            self._retained_service_locks.add(database)
            return
        if not scan.safe:
            return
        self._release_all_child_authorities(database)
        self._release_service_lock(database)

    def _release_child_authority(
        self,
        database_path: Path,
        component: str,
    ) -> None:
        database = self._database_key(database_path)
        by_component = self._child_authorities.get(database)
        if not by_component:
            return
        authority = by_component.get(component)
        if authority is not None:
            try:
                authority.__exit__(None, None, None)
            except BaseException:
                self._authority_release_failures.add(database)
                raise
            by_component.pop(component, None)
        if not by_component:
            self._child_authorities.pop(database, None)

    def _release_all_child_authorities(self, database_path: Path) -> None:
        database = self._database_key(database_path)
        for component in tuple(self._child_authorities.get(database, {})):
            self._release_child_authority(database, component)

    def _retain_unverified_process(
        self,
        database_path: Path,
        component: str,
        process_handle: Any,
    ) -> None:
        database = self._database_key(database_path)
        self._unverified_processes.setdefault(database, {})[id(process_handle)] = (
            component,
            process_handle,
        )

    def _forget_unverified_process(
        self,
        database_path: Path,
        process_handle: Any,
    ) -> None:
        database = self._database_key(database_path)
        processes = self._unverified_processes.get(database)
        if not processes:
            return
        processes.pop(id(process_handle), None)
        if not processes:
            self._unverified_processes.pop(database, None)

    def _release_spawn_quarantine(
        self,
        database_path: Path,
        component: str,
        process_handle: Any,
    ) -> None:
        self._release_child_authority(database_path, component)
        self._forget_unverified_process(database_path, process_handle)

    def _database_identity_error(
        self,
        database_path: Path,
    ) -> dict[str, object] | None:
        database = self._database_key(database_path)
        expected_identity = self._database_identities.get(database)
        if expected_identity is None:
            return self._result(
                False,
                "database_identity_changed",
                detail="database identity is unavailable",
            )
        try:
            require_unique_database_file(
                database,
                expected_identity=expected_identity,
            )
        except Exception as error:
            return self._result(
                False,
                "database_identity_changed",
                detail=f"{type(error).__name__}:{error}",
            )
        return None

    def _registered_process_requires_shutdown(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
    ) -> bool:
        try:
            rows = connection.execute(
                "SELECT * FROM monitor_process_registry"
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).casefold():
                return False
            raise
        for row in rows:
            component = str(row["component"])
            if component not in COMPONENTS:
                return True
            state, _, _ = self._inspect(
                row,
                self.command_for(component, database_path),
            )
            if state in {
                "running",
                "identity_mismatch",
                "identity_unverifiable",
            }:
                return True
        return False

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

    def _verified_registry_identities(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
    ) -> tuple[tuple[ProcessIdentity, ...], dict[str, object] | None]:
        database = self._database_key(database_path)
        identities: list[ProcessIdentity] = []
        owned = self._owned_processes.setdefault(database, {})
        for component in COMPONENTS:
            row = connection.execute(
                "SELECT * FROM monitor_process_registry WHERE component=?",
                (component,),
            ).fetchone()
            state, _, detail = self._inspect(
                row,
                self.command_for(component, database),
            )
            if state == "running" and row is not None:
                identity = ProcessIdentity(
                    int(row["pid"]),
                    float(row["process_created_at"]),
                )
                identities.append(identity)
                owned[component] = identity
            elif state in {"identity_mismatch", "identity_unverifiable"}:
                return (), self._result(False, state, row=row, detail=detail)
        return tuple(identities), None

    def _verify_spawn_authority(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
    ) -> dict[str, object] | None:
        paths = service_data_paths(database_path)
        try:
            self._database_verifier(
                paths.database,
                odds_raw_root=paths.odds_raw_root,
            )
        except Exception as error:
            return self._result(
                False,
                "database_verification_failed",
                detail=type(error).__name__,
            )
        allowed, error_response = self._verified_registry_identities(
            connection,
            paths.database,
        )
        if error_response is not None:
            return error_response
        try:
            scan = self._writer_scanner(
                paths.database,
                allowed_identities=allowed,
            )
        except Exception as error:
            return self._result(
                False,
                "writer_scan_failed",
                detail=type(error).__name__,
            )
        if scan.unverifiable_pids:
            return self._result(
                False,
                "writer_scan_unverifiable",
                detail=",".join(str(pid) for pid in scan.unverifiable_pids),
            )
        if scan.conflicts:
            return self._result(
                False,
                "orphan_writer_conflict",
                detail=",".join(str(item.pid) for item in scan.conflicts),
            )
        return None

    def _start(
        self,
        connection: sqlite3.Connection,
        database_path: Path,
        component: str,
        command: list[str],
    ) -> tuple[dict[str, object], Any | None]:
        row = connection.execute(
            "SELECT * FROM monitor_process_registry WHERE component=?",
            (component,),
        ).fetchone()
        state, _, detail = self._inspect(row, command)
        if state == "running":
            if row is not None:
                self._owned_processes.setdefault(
                    self._database_key(database_path), {}
                )[component] = ProcessIdentity(
                    int(row["pid"]),
                    float(row["process_created_at"]),
                )
            return self._result(True, "already_running", row=row), None
        if state in {"identity_mismatch", "identity_unverifiable"}:
            return self._result(False, state, row=row, detail=detail), None
        database = self._database_key(database_path)
        if self._unverified_processes.get(database):
            return (
                self._result(
                    False,
                    "unverified_child_present",
                    detail="a prior spawned process is not yet proven stopped",
                ),
                None,
            )
        authority_error = self._verify_spawn_authority(connection, database_path)
        if authority_error is not None:
            return authority_error, None
        identity_error = self._database_identity_error(database_path)
        if identity_error is not None:
            return identity_error, None
        if row is not None:
            self._mark_stopped(connection, component)
            self._release_child_authority(database, component)

        paths = service_data_paths(database_path)
        log_dir = paths.managed_logs
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / f"{component}.stdout.log"
        stderr_path = log_dir / f"{component}.stderr.log"
        stdout = stdout_path.open("a", encoding="utf-8")
        stderr = stderr_path.open("a", encoding="utf-8")
        authority_context = manager_child_authority(
            paths.database,
            role=component,
            command=command,
            delegate_roles=(
                ("vision_watcher",) if component == "vision_supervisor" else ()
            ),
        )
        authority: Any | None = None
        process_handle: Any | None = None
        spawn_error: BaseException | None = None
        try:
            self._child_authorities.setdefault(paths.database, {})[
                component
            ] = authority_context
            authority = authority_context.__enter__()
            child_environment = manager_child_process_environment(authority)
            process_handle = self._popen(
                command,
                cwd=str(self.project_dir.resolve()),
                stdout=stdout,
                stderr=stderr,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=child_environment,
            )
            self._retain_unverified_process(
                paths.database,
                component,
                process_handle,
            )
        except BaseException as error:
            spawn_error = error
        finally:
            for output in (stdout, stderr):
                try:
                    output.close()
                except BaseException as error:
                    if spawn_error is None:
                        spawn_error = error

        if spawn_error is not None:
            cleanup = (
                terminate_subprocess_tree(
                    process_handle,
                    process_factory=self._process,
                )
                if process_handle is not None
                else TerminationResult(True)
            )
            if cleanup.ok:
                try:
                    self._release_child_authority(paths.database, component)
                except BaseException as cleanup_error:
                    cleanup = TerminationResult(
                        False,
                        "authority_cleanup_failed:"
                        f"{type(cleanup_error).__name__}:{cleanup_error}",
                    )
                else:
                    if process_handle is not None:
                        self._forget_unverified_process(
                            paths.database,
                            process_handle,
                        )
            elif process_handle is not None:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            if not isinstance(spawn_error, Exception):
                raise spawn_error
            return (
                self._result(
                    False,
                    "start_failed" if cleanup.ok else "start_cleanup_failed",
                    detail=(
                        type(spawn_error).__name__
                        if cleanup.ok
                        else cleanup.detail
                    ),
                ),
                None,
            )

        assert authority is not None and process_handle is not None

        try:
            bound_identity = bind_manager_child_authority(
                authority,
                process_handle,
                process_factory=self._process,
            )
        except BaseException as error:
            cleanup = terminate_subprocess_tree(
                process_handle,
                process_factory=self._process,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            if not isinstance(error, Exception):
                raise
            return (
                self._result(
                    False,
                    (
                        "start_identity_mismatch"
                        if cleanup.ok
                        else "start_cleanup_failed"
                    ),
                    detail=(
                        f"manager_child_bind_failed:{type(error).__name__}:{error}"
                        if cleanup.ok
                        else f"manager_child_bind_failed:{cleanup.detail}"
                    ),
                ),
                None,
            )

        identity_error = self._database_identity_error(database_path)
        if identity_error is not None:
            cleanup = terminate_subprocess_tree(
                process_handle,
                process_factory=self._process,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            return (
                identity_error
                if cleanup.ok
                else self._result(
                    False,
                    "start_cleanup_failed",
                    detail=f"database_identity_changed:{cleanup.detail}",
                ),
                None,
            )
        try:
            process = self._process(int(process_handle.pid))
        except (
            psutil.Error,
            KeyError,
            AttributeError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            cleanup = terminate_subprocess_tree(
                process_handle,
                process_factory=self._process,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            return (
                self._result(
                    False,
                    "start_failed" if cleanup.ok else "start_cleanup_failed",
                    detail=(
                        type(error).__name__
                        if cleanup.ok
                        else f"{type(error).__name__}:{cleanup.detail}"
                    ),
                ),
                None,
            )
        created_at: float | None = None
        try:
            created_at = float(process.create_time())
            actual_command = list(process.cmdline())
        except (
            AttributeError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            psutil.Error,
        ) as error:
            expected_identity = (
                ProcessIdentity(int(process_handle.pid), created_at)
                if created_at is not None
                else None
            )
            cleanup = self._cleanup_spawned_process(
                process_handle,
                process=process,
                expected_root=expected_identity,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            return (
                self._result(
                    False,
                    "start_failed" if cleanup.ok else "start_cleanup_failed",
                    detail=(
                        type(error).__name__
                        if cleanup.ok
                        else f"{type(error).__name__}:{cleanup.detail}"
                    ),
                ),
                None,
            )

        assert created_at is not None
        spawned_identity = ProcessIdentity(int(process_handle.pid), created_at)
        if spawned_identity != bound_identity:
            cleanup = self._cleanup_spawned_process(
                process_handle,
                process=process,
                expected_root=bound_identity,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            return (
                self._result(
                    False,
                    (
                        "start_identity_mismatch"
                        if cleanup.ok
                        else "start_cleanup_failed"
                    ),
                    detail=(
                        "spawned identity changed after marker binding"
                        if cleanup.ok
                        else cleanup.detail
                    ),
                ),
                None,
            )
        expected_hash = _command_hash(command)
        if _command_hash(actual_command) != expected_hash:
            cleanup = self._cleanup_spawned_process(
                process_handle,
                process=process,
                expected_root=spawned_identity,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            return (
                self._result(
                    False,
                    (
                        "start_identity_mismatch"
                        if cleanup.ok
                        else "start_cleanup_failed"
                    ),
                    detail=(
                        "spawned command did not match allowlist"
                        if cleanup.ok
                        else cleanup.detail
                    ),
                ),
                None,
            )

        identity_error = self._database_identity_error(database_path)
        if identity_error is not None:
            cleanup = self._cleanup_spawned_process(
                process_handle,
                process=process,
                expected_root=spawned_identity,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            return (
                identity_error
                if cleanup.ok
                else self._result(
                    False,
                    "start_cleanup_failed",
                    detail=f"database_identity_changed:{cleanup.detail}",
                ),
                None,
            )

        try:
            now = _utc_now()
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
        except BaseException as error:
            cleanup = self._cleanup_spawned_process(
                process_handle,
                process=process,
                expected_root=spawned_identity,
            )
            if not cleanup.ok:
                self._retain_unverified_process(
                    paths.database,
                    component,
                    process_handle,
                )
            else:
                self._release_spawn_quarantine(
                    paths.database,
                    component,
                    process_handle,
                )
            if not isinstance(error, Exception):
                raise
            return (
                self._result(
                    False,
                    "registration_failed" if cleanup.ok else "start_cleanup_failed",
                    detail=type(error).__name__ if cleanup.ok else cleanup.detail,
                ),
                None,
            )
        self._owned_processes.setdefault(paths.database, {})[component] = spawned_identity
        self._forget_unverified_process(paths.database, process_handle)
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
        database_path: Path,
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
            self._owned_processes.setdefault(
                self._database_key(database_path), {}
            ).pop(component, None)
            self._release_child_authority(database_path, component)
            return self._result(True, "already_stopped", row=row)
        if state in {"identity_mismatch", "identity_unverifiable"} or process is None:
            return self._result(False, state, row=row, detail=detail)

        expected_identity = ProcessIdentity(
            int(row["pid"]),
            float(row["process_created_at"]),
        )
        self._owned_processes.setdefault(
            self._database_key(database_path), {}
        )[component] = expected_identity
        termination = self._terminate_process_tree(
            process,
            expected_root=expected_identity,
        )
        if not termination.ok:
            return self._result(
                False,
                "stop_failed",
                row=row,
                detail=termination.detail,
            )

        old_pid = int(row["pid"])
        self._mark_stopped(connection, component)
        self._owned_processes.setdefault(
            self._database_key(database_path), {}
        ).pop(component, None)
        self._release_child_authority(database_path, component)
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
        database_path: Path,
        component: str,
        command: list[str],
    ) -> tuple[dict[str, object], Any | None]:
        stopped = self._stop(connection, database_path, component, command)
        if not bool(stopped["ok"]):
            return stopped, None
        started, spawned_process = self._start(
            connection,
            database_path,
            component,
            command,
        )
        if bool(started["ok"]) and started["result"] == "started":
            started["result"] = "restarted"
        return started, spawned_process

    def _terminate_process_tree(
        self,
        process: Any,
        *,
        expected_root: ProcessIdentity | None = None,
    ) -> TerminationResult:
        return terminate_process_tree(
            process,
            process_factory=self._process,
            expected_root=expected_root,
        )

    def _cleanup_spawned_process(
        self,
        process_handle: Any,
        *,
        process: Any | None = None,
        expected_root: ProcessIdentity | None = None,
    ) -> TerminationResult:
        if process is not None:
            result = self._terminate_process_tree(
                process,
                expected_root=expected_root,
            )
            if result.ok:
                return result
        return terminate_subprocess_tree(
            process_handle,
            process_factory=self._process,
        )

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
            registered_created_at = float(row["process_created_at"])
        except (psutil.NoSuchProcess, KeyError):
            return "stale", None, "registered PID no longer exists"
        except (AttributeError, OSError, TypeError, ValueError, psutil.Error) as error:
            return (
                "identity_unverifiable",
                None,
                f"registered PID identity is unverifiable: {type(error).__name__}",
            )
        if actual_created_at != registered_created_at:
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
    payload = json.dumps(
        command_comparison_key(command),
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
