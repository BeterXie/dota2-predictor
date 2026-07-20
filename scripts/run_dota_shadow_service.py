"""Single-instance supervisor for local Dota shadow components."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.report import build_intelligence_report  # noqa: E402
from live_betting.browser_companion import PROTOCOL_VERSION  # noqa: E402
from live_betting.database_protocol import (  # noqa: E402
    prepare_database,
    verify_prepared_database,
)
from live_betting.health import record_health  # noqa: E402
from live_betting.report import build_report  # noqa: E402
from live_betting.service_coordination import (  # noqa: E402
    DatabaseFileIdentity,
    ProcessIdentity,
    SingleInstanceLock,
    TerminationResult,
    WriterScanResult,
    add_single_database_argument,
    bind_manager_child_authority,
    database_service_authority_lock_paths,
    manager_child_authority,
    manager_child_process_environment,
    managed_child_command,
    require_unique_database_file,
    scan_managed_writers,
    service_data_paths,
    terminate_subprocess_tree,
)
from live_betting.smtp_delivery import (  # noqa: E402
    SMTPConfig,
    SMTPConfigurationError,
)
from live_betting.storage import LiveBettingStore  # noqa: E402
from web.alerts import reconcile_alerts  # noqa: E402


WORKER_COMPONENTS = {
    "raybet": "raybet_worker",
    "shadow": "shadow_worker",
    "mail": "mail_worker",
    "vision": "vision_worker",
    "strict_ingest": "strict_ingest_worker",
    "postmatch": "postmatch_worker",
    "draft_publisher": "draft_publisher_worker",
}
ACTIVE_COMMANDS = {
    "raybet": "collector",
    "shadow": "shadow",
    "mail": "mail",
    "vision": "vision",
    "strict_ingest": "strict_ingest",
    "postmatch": "postmatch",
    "draft_publisher": "draft_publisher",
}
WORKER_MAX_AGE = {
    "raybet": timedelta(seconds=45),
    "shadow": timedelta(seconds=45),
    "mail": timedelta(seconds=90),
    "vision": timedelta(seconds=90),
    "strict_ingest": timedelta(seconds=90),
    "postmatch": timedelta(seconds=150),
    "draft_publisher": timedelta(minutes=15),
}
COLLECTOR_MAX_AGE = timedelta(seconds=60)
DATABASE_AUDIT_MAX_AGE = timedelta(minutes=15)
DATABASE_FAILURE_RECHECK = timedelta(seconds=60)
LOCK_CONTENTION_RETRY_MIN_SECONDS = 0.05
COMPANION_HEALTH_URL = "http://127.0.0.1:8765/health"
_DATABASE_HEALTH_CACHE: dict[
    str,
    tuple[datetime, tuple[str, str | None, dict[str, Any]]],
] = {}
_DATABASE_AUDIT_THREADS: dict[str, threading.Thread] = {}
_DATABASE_AUDIT_LOCK = threading.Lock()


def _transient_sqlite_lock_kind(error: sqlite3.OperationalError) -> str | None:
    code = getattr(error, "sqlite_errorcode", None)
    if type(code) is int:
        primary = code & 0xFF
        if primary == sqlite3.SQLITE_BUSY:
            return "SQLITE_BUSY"
        if primary == sqlite3.SQLITE_LOCKED:
            return "SQLITE_LOCKED"
        return None
    message = " ".join(str(error).split()).casefold()
    if message == "database is locked":
        return "SQLITE_BUSY"
    if message in {"database table is locked", "database schema is locked"}:
        return "SQLITE_LOCKED"
    return None


def _write_service_report(report_path: Path, result: Mapping[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)


def _generate_service_report(database: Path, report_path: Path) -> None:
    """Build a report on a connection owned by the calling thread."""
    with LiveBettingStore(database) as store:
        result = {
            "shadow": build_report(store.connection),
            "intelligence": build_intelligence_report(store.connection),
        }
    _write_service_report(report_path, result)


class _ReportWorker:
    """Run at most one report build without blocking supervisor heartbeats."""

    def __init__(
        self,
        database: Path,
        report_path: Path,
        *,
        report_interval: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.report_path = report_path
        self.report_interval = report_interval
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_finished_at: float | None = None
        self.last_error: str | None = None

    def start_if_idle(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if (
                self._last_finished_at is not None
                and self._monotonic() - self._last_finished_at
                < self.report_interval
            ):
                return False
            self._thread = threading.Thread(
                target=self._run,
                name="service-report",
                daemon=True,
            )
            self._thread.start()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        try:
            _generate_service_report(self.database, self.report_path)
        except Exception as error:
            message = " ".join(str(error).split())[:500]
            self.last_error = f"{type(error).__name__}: {message}"
            print(
                json.dumps({
                    "status": "report_error",
                    "error_type": type(error).__name__,
                    "error": message,
                }, ensure_ascii=False),
                file=sys.stderr,
                flush=True,
            )
        else:
            self.last_error = None
        finally:
            with self._lock:
                self._last_finished_at = self._monotonic()


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _worker_health(
    connection: Any,
    component: str,
    now: datetime,
    active_components: set[str],
) -> tuple[str, dict[str, Any]]:
    worker_component = WORKER_COMPONENTS[component]
    row = connection.execute(
        "SELECT * FROM service_health WHERE component=?",
        (worker_component,),
    ).fetchone()
    if row is None:
        return (
            "starting" if ACTIVE_COMMANDS[component] in active_components else "stopped",
            {"worker_component": worker_component, "reason": "awaiting_heartbeat"},
        )
    try:
        details = json.loads(str(row["details_json"]))
    except (TypeError, ValueError):
        details = {}
    heartbeat = _parse_time(row["last_heartbeat_at"])
    if heartbeat is None:
        return "unhealthy", {
            "worker_component": worker_component,
            "reason": "invalid_heartbeat",
        }
    age = max(0.0, (now - heartbeat).total_seconds())
    limit = WORKER_MAX_AGE[component].total_seconds()
    status = str(row["status"])
    if age > limit * 2:
        status = "unhealthy"
        reason = "heartbeat_expired"
    elif age > limit:
        status = "degraded"
        reason = "heartbeat_stale"
    elif status == "healthy":
        reason = "worker_heartbeat_fresh"
    else:
        reason = str(row["last_error"] or f"worker_status_{status}")
    return status, {
        "worker_component": worker_component,
        "worker_status": str(row["status"]),
        "worker_heartbeat_at": heartbeat.isoformat(),
        "heartbeat_age_seconds": round(age, 3),
        "worker_details": details,
        "reason": reason,
    }


def _database_health(connection: Any) -> tuple[str, str | None, dict[str, Any]]:
    integrity = [
        str(row[0])
        for row in connection.execute("PRAGMA quick_check").fetchall()
    ]
    foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
    details = {
        "integrity": "ok" if integrity == ["ok"] else integrity[:20],
        "foreign_key_issues": len(foreign_key_issues),
    }
    if integrity != ["ok"]:
        return "unhealthy", "integrity_check_failed", details
    if foreign_key_issues:
        return "unhealthy", "foreign_key_check_failed", details
    return "healthy", None, details


def _run_database_audit(database_key: str, database: Path) -> None:
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        try:
            connection.execute("PRAGMA busy_timeout=1000")
            result = _database_health(connection)
        finally:
            connection.close()
    except Exception as error:
        message = " ".join(str(error).split())[:500]
        result = (
            "unhealthy",
            "database_audit_failed",
            {
                "integrity": "unknown",
                "foreign_key_issues": None,
                "audit_error": f"{type(error).__name__}: {message}",
            },
        )
    checked_at = datetime.now(timezone.utc)
    with _DATABASE_AUDIT_LOCK:
        _DATABASE_HEALTH_CACHE[database_key] = (checked_at, result)
        if _DATABASE_AUDIT_THREADS.get(database_key) is threading.current_thread():
            del _DATABASE_AUDIT_THREADS[database_key]


def _start_database_audit(database_key: str, database: Path) -> None:
    with _DATABASE_AUDIT_LOCK:
        thread = _DATABASE_AUDIT_THREADS.get(database_key)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=_run_database_audit,
            args=(database_key, database),
            name="service-database-audit",
            daemon=True,
        )
        _DATABASE_AUDIT_THREADS[database_key] = thread
        thread.start()


def _periodic_database_health(
    connection: Any,
    now: datetime,
    *,
    background: bool = False,
) -> tuple[str, str | None, dict[str, Any]]:
    database_row = connection.execute(
        "PRAGMA database_list"
    ).fetchone()
    database_name = str(database_row[2]) if database_row and database_row[2] else ""
    database_key = database_name or str(id(connection))
    with _DATABASE_AUDIT_LOCK:
        cached = _DATABASE_HEALTH_CACHE.get(database_key)
    if cached is not None:
        checked_at, result = cached
        max_age = (
            DATABASE_AUDIT_MAX_AGE
            if result[0] == "healthy"
            else DATABASE_FAILURE_RECHECK
        )
        age = max(0.0, (now - checked_at).total_seconds())
        if age <= max_age.total_seconds():
            status, reason, details = result
            return status, reason, {
                **details,
                "audit_checked_at": checked_at.isoformat(),
                "audit_age_seconds": round(age, 3),
                "audit_cached": True,
            }
    if background and database_name:
        _start_database_audit(database_key, Path(database_name))
        if cached is not None:
            checked_at, result = cached
            status, reason, details = result
            age = max(0.0, (now - checked_at).total_seconds())
            return status, reason, {
                **details,
                "audit_checked_at": checked_at.isoformat(),
                "audit_age_seconds": round(age, 3),
                "audit_cached": True,
                "audit_stale": True,
                "audit_refreshing": True,
            }
        return "starting", "database_audit_pending", {
            "integrity": "pending",
            "foreign_key_issues": None,
            "audit_checked_at": None,
            "audit_age_seconds": None,
            "audit_cached": False,
            "audit_refreshing": True,
        }
    result = _database_health(connection)
    with _DATABASE_AUDIT_LOCK:
        _DATABASE_HEALTH_CACHE[database_key] = (now, result)
    status, reason, details = result
    return status, reason, {
        **details,
        "audit_checked_at": now.isoformat(),
        "audit_age_seconds": 0.0,
        "audit_cached": False,
    }


def _collector_health(
    connection: Any,
    worker_status: str,
    now: datetime,
    details: dict[str, Any],
) -> tuple[str, str]:
    if worker_status in {"stopped", "starting", "degraded", "unhealthy"}:
        return worker_status, str(details.get("reason", "worker_unavailable"))
    row = connection.execute(
        """SELECT last_success_at, last_error_at, last_error, gap_detected
             FROM collector_runs WHERE collector='raybet'"""
    ).fetchone()
    if row is None or not row["last_success_at"]:
        return "degraded", "no_successful_collection_yet"
    success_at = _parse_time(row["last_success_at"])
    if success_at is None:
        return "unhealthy", "invalid_collection_timestamp"
    age = max(0.0, (now - success_at).total_seconds())
    details["last_success_at"] = success_at.isoformat()
    details["collection_age_seconds"] = round(age, 3)
    if bool(row["gap_detected"]):
        return "degraded", "collection_gap_detected"
    error_at = _parse_time(row["last_error_at"])
    if error_at is not None and error_at >= success_at:
        return "degraded", "recent_collection_error"
    if age > COLLECTOR_MAX_AGE.total_seconds() * 2:
        return "unhealthy", "collection_stale"
    if age > COLLECTOR_MAX_AGE.total_seconds():
        return "degraded", "collection_delayed"
    return "healthy", "collection_fresh"


def _probe_companion() -> Mapping[str, Any]:
    with urllib.request.urlopen(COMPANION_HEALTH_URL, timeout=1.0) as response:
        if response.status != 200:
            raise RuntimeError(f"companion health returned HTTP {response.status}")
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError("companion health returned a non-object")
    return payload


def _companion_health(
    active: bool,
    probe: Callable[[], Mapping[str, Any]],
    *,
    initial: bool = False,
) -> tuple[str, str, dict[str, Any]]:
    if not active:
        return "stopped", "not_started_by_supervisor", {}
    try:
        payload = dict(probe())
    except Exception as error:
        return (
            "starting" if initial else "unhealthy",
            "awaiting_companion_health" if initial else "companion_unreachable",
            {
                "error_type": type(error).__name__,
            },
        )
    details = {
        "protocol_version": payload.get("protocol_version"),
        "service_state": payload.get("state"),
    }
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        return "unhealthy", "companion_protocol_mismatch", details
    if payload.get("state") != "ok":
        return "degraded", "companion_not_ready", details
    return "healthy", "companion_reachable", details


def _record_component(
    connection: Any,
    component: str,
    status: str,
    now: datetime,
    *,
    reason: str | None,
    details: dict[str, Any],
) -> None:
    is_error = status in {"degraded", "unhealthy", "stopped"}
    record_health(
        connection,
        component,
        status,
        heartbeat_at=now,
        success_at=now if status == "healthy" else None,
        error_at=now if is_error else None,
        error=reason if is_error else None,
        details={"source": "supervisor", **details},
    )


def service_once(
    database: Path,
    report_path: Path | None = None,
    *,
    active_components: set[str] | None = None,
    companion_probe: Callable[[], Mapping[str, Any]] = _probe_companion,
    initialize_schema: bool = True,
    health_only: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    active_components = set(active_components or ())
    with LiveBettingStore(database) as store:
        if initialize_schema:
            store.init_schema()
        connection = store.connection
        database_status, database_reason, database_details = _periodic_database_health(
            connection, now, background=health_only
        )
        _record_component(
            connection,
            "database",
            database_status,
            now,
            reason=database_reason,
            details=database_details,
        )
        raybet_worker_status, raybet_details = _worker_health(
            connection, "raybet", now, active_components
        )
        raybet_status, raybet_reason = _collector_health(
            connection, raybet_worker_status, now, raybet_details
        )
        _record_component(
            connection,
            "raybet",
            raybet_status,
            now,
            reason=raybet_reason,
            details=raybet_details,
        )

        shadow_status, shadow_details = _worker_health(
            connection, "shadow", now, active_components
        )
        pending = int(connection.execute(
            "SELECT COUNT(*) FROM shadow_orders WHERE status='pending'"
        ).fetchone()[0])
        shadow_details["pending_orders"] = pending
        _record_component(
            connection,
            "shadow",
            shadow_status,
            now,
            reason=str(shadow_details.get("reason", "worker_unavailable")),
            details=shadow_details,
        )

        try:
            SMTPConfig.from_environment()
            smtp_configured = True
        except SMTPConfigurationError:
            smtp_configured = False
        mail_worker_status, mail_details = _worker_health(
            connection, "mail", now, active_components
        )
        dead_letters = int(connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status='dead_letter'"
        ).fetchone()[0])
        pending_notifications = int(connection.execute(
            """SELECT COUNT(*) FROM notification_outbox
                WHERE status='pending' AND next_attempt_at IS NOT NULL
                  AND next_attempt_at<=?""",
            (now.isoformat(),),
        ).fetchone()[0])
        expired_leases = int(connection.execute(
            """SELECT COUNT(*) FROM notification_outbox
                WHERE status='leased' AND lease_until IS NOT NULL
                  AND lease_until<=?""",
            (now.isoformat(),),
        ).fetchone()[0])
        mail_details.update({
            "smtp_configured": smtp_configured,
            "dead_letters": dead_letters,
            "pending_due": pending_notifications,
            "expired_leases": expired_leases,
        })
        if not smtp_configured:
            mail_status, mail_error = "degraded", "configuration_missing"
        elif mail_worker_status == "unhealthy":
            mail_status = "unhealthy"
            mail_error = str(mail_details.get("reason", "worker_unavailable"))
        elif mail_worker_status == "healthy" and (
            dead_letters or pending_notifications or expired_leases
        ):
            mail_status, mail_error = "degraded", "notification_backlog_or_dead_letter"
        else:
            mail_status = mail_worker_status
            mail_error = str(mail_details.get("reason", "worker_unavailable"))
        _record_component(
            connection,
            "mail",
            mail_status,
            now,
            reason=mail_error,
            details=mail_details,
        )

        for component in ("vision", "strict_ingest", "postmatch", "draft_publisher"):
            status, details = _worker_health(
                connection, component, now, active_components
            )
            _record_component(
                connection,
                component,
                status,
                now,
                reason=str(details.get("reason", "worker_unavailable")),
                details=details,
            )

        previous_companion = connection.execute(
            "SELECT status FROM service_health WHERE component='companion'"
        ).fetchone()
        companion_status, companion_reason, companion_details = _companion_health(
            "companion" in active_components,
            companion_probe,
            initial=(
                previous_companion is None
                or str(previous_companion["status"]) == "stopped"
            ),
        )
        _record_component(
            connection,
            "companion",
            companion_status,
            now,
            reason=companion_reason,
            details=companion_details,
        )
        reconcile_alerts(connection, now=now)
        if health_only:
            return {"pending_orders": pending}
        report = build_report(connection)
        intelligence = build_intelligence_report(connection)
        result = {"shadow": report, "intelligence": intelligence}
        if report_path:
            _write_service_report(report_path, result)
        return result


def _commands(args: argparse.Namespace) -> dict[str, list[str]]:
    commands: dict[str, list[str]] = {}
    python = sys.executable
    paths = service_data_paths(args.database)
    database = str(paths.database)
    if args.start_collector:
        commands["collector"] = [
            python,
            "-m",
            "live_betting.monitor",
            "--database",
            database,
            "--raw-dir",
            str(paths.odds_raw_root),
            "--schema-prepared",
        ]
    if args.start_companion:
        commands["companion"] = [
            python,
            "-m",
            "live_betting.browser_companion",
            "--database",
            database,
            "--schema-prepared",
        ]
    if args.start_shadow:
        requested_vision = getattr(args, "vision_jsonl", None)
        if (
            requested_vision is not None
            and requested_vision.resolve() != paths.vision_observations
        ):
            raise ValueError(
                "--vision-jsonl must equal <database-dir>/live_betting/"
                "live_observations"
            )
        commands["shadow"] = [
            python,
            "scripts/run_comeback_shadow.py",
            "--database",
            database,
            "--vision-jsonl",
            str(paths.vision_observations),
            "--schema-prepared",
        ]
    if getattr(args, "start_vision", False):
        commands["vision"] = [
            python,
            "scripts/supervise_raybet_streams.py",
            "--database",
            database,
            "--output-dir",
            str(paths.vision_observations),
            "--evidence-dir",
            str(paths.vision_evidence),
            "--log-dir",
            str(paths.vision_logs),
            "--schema-prepared",
        ]
    if args.start_mail:
        commands["mail"] = [
            python,
            "scripts/run_notification_worker.py",
            "--database",
            database,
            "--schema-prepared",
        ]
    if args.start_strict_ingest:
        commands["strict_ingest"] = [
            python,
            "scripts/run_strict_event_ingest.py",
            "--database",
            database,
            "--archive-root",
            str(paths.source_archive_root),
            "--coverage-report",
            str(paths.strict_coverage_report),
            "--schema-prepared",
        ]
    if args.start_postmatch:
        commands["postmatch"] = [
            python,
            "scripts/run_postmatch_labeler.py",
            "--database",
            database,
            "--all",
            "--archive-root",
            str(paths.source_archive_root),
            "--schema-prepared",
        ]
    if getattr(args, "start_draft_publisher", False) or args.start_shadow:
        commands["draft_publisher"] = [
            python,
            "-m",
            "live_betting.draft_publisher",
            "--database",
            database,
            "--schema-prepared",
        ]
    return {
        name: managed_child_command(command)
        for name, command in commands.items()
    }


def _capture_subprocess_tree_identities(
    process_handle: Any,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    max_passes: int = 8,
) -> tuple[ProcessIdentity, ...]:
    if process_handle.poll() is not None:
        raise RuntimeError("direct child exited before subtree capture")
    root = process_factory(int(process_handle.pid))
    root_identity = ProcessIdentity(int(root.pid), float(root.create_time()))
    previous: tuple[ProcessIdentity, ...] | None = None
    for _ in range(max_passes):
        if process_handle.poll() is not None:
            raise RuntimeError("direct child exited during subtree capture")
        if float(root.create_time()) != root_identity.created_at:
            raise RuntimeError("direct child identity changed during subtree capture")
        identities = {root_identity}
        for child in root.children(recursive=True):
            if not child.is_running() or child.status() == psutil.STATUS_ZOMBIE:
                raise RuntimeError("descendant exited during subtree capture")
            identities.add(
                ProcessIdentity(int(child.pid), float(child.create_time()))
            )
        current = tuple(sorted(identities))
        if current == previous:
            if process_handle.poll() is not None:
                raise RuntimeError("direct child exited after subtree capture")
            return current
        previous = current
    raise RuntimeError("healthy child subtree did not stabilize")


def _healthy_subtree_identities(
    children: Mapping[str, Any],
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
) -> tuple[ProcessIdentity, ...]:
    identities: set[ProcessIdentity] = set()
    for process_handle in children.values():
        if process_handle.poll() is not None:
            continue
        identities.update(
            _capture_subprocess_tree_identities(
                process_handle,
                process_factory=process_factory,
            )
        )
    return tuple(sorted(identities))


def _replacement_authority_gate(
    children: Mapping[str, Any],
    database: Path,
    database_identity: DatabaseFileIdentity,
    *,
    process_factory: Callable[[int], Any] = psutil.Process,
    writer_scanner: Callable[..., WriterScanResult] | None = None,
) -> TerminationResult:
    writer_scanner = writer_scanner or scan_managed_writers
    try:
        require_unique_database_file(
            database,
            expected_identity=database_identity,
        )
        before = _healthy_subtree_identities(
            children,
            process_factory=process_factory,
        )
        scan = writer_scanner(database, allowed_identities=before)
        if scan.unverifiable_pids:
            return TerminationResult(
                False,
                "writer_scan_unverifiable:"
                + ",".join(str(pid) for pid in scan.unverifiable_pids),
            )
        if scan.conflicts:
            return TerminationResult(
                False,
                "orphan_writer_conflict:"
                + ",".join(str(item.pid) for item in scan.conflicts),
            )
        after = _healthy_subtree_identities(
            children,
            process_factory=process_factory,
        )
        require_unique_database_file(
            database,
            expected_identity=database_identity,
        )
    except Exception as error:
        return TerminationResult(
            False,
            f"replacement_authority_unverifiable:{type(error).__name__}:{error}",
        )
    if before != after:
        return TerminationResult(False, "healthy_subtree_changed_during_writer_gate")
    return TerminationResult(True)


def _shutdown_children_under_authority(
    children: Mapping[str, Any],
    database: Path,
    database_identity: DatabaseFileIdentity,
    *,
    terminator: Callable[..., TerminationResult] | None = None,
    writer_scanner: Callable[..., WriterScanResult] | None = None,
    retry_hook: Callable[[int, str], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> TerminationResult:
    terminator = terminator or terminate_subprocess_tree
    writer_scanner = writer_scanner or scan_managed_writers
    attempt = 0
    while True:
        attempt += 1
        termination_details: list[str] = []
        cleanup_details: list[str] = []
        for name, child in children.items():
            try:
                result = terminator(child)
            except Exception as error:
                termination_details.append(
                    f"termination_unproven:{name}:"
                    f"{type(error).__name__}:{error}"
                )
            else:
                if not result.ok:
                    termination_details.append(
                        f"termination_unproven:{name}:{result.detail}"
                    )
            try:
                _close_child_authority(child)
            except Exception as error:
                cleanup_details.append(
                    f"{name}:authority_cleanup:{type(error).__name__}:{error}"
                )
        authority_details: list[str] = list(cleanup_details)
        try:
            require_unique_database_file(
                database,
                expected_identity=database_identity,
            )
            scan = writer_scanner(database, allowed_identities=())
            if scan.unverifiable_pids:
                authority_details.append(
                    "writer_scan_unverifiable:"
                    + ",".join(str(pid) for pid in scan.unverifiable_pids)
                )
            if scan.conflicts:
                authority_details.append(
                    "writer_conflicts:"
                    + ",".join(str(item.pid) for item in scan.conflicts)
                )
            require_unique_database_file(
                database,
                expected_identity=database_identity,
            )
        except Exception as error:
            authority_details.append(
                f"database_authority_unverifiable:{type(error).__name__}:{error}"
            )
        if not termination_details and not authority_details:
            detail = f"shutdown_proven_after_attempt:{attempt}"
            return TerminationResult(True, detail)
        detail = ";".join(
            dict.fromkeys([*termination_details, *authority_details])
        )
        if retry_hook is not None and not retry_hook(attempt, detail):
            return TerminationResult(False, f"quarantined:{attempt}:{detail}")
        print(
            json.dumps(
                {
                    "status": "quarantined",
                    "attempt": attempt,
                    "detail": detail,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        sleeper(min(5.0, 0.25 * (2 ** min(attempt - 1, 5))))


_CHILD_AUTHORITY_ATTRIBUTE = "_dota2_manager_authority_context"
_CHILD_AUTHORITY_CLEANUP_ERROR_ATTRIBUTE = (
    "_dota2_manager_authority_cleanup_error"
)


class _ManagedChildHandle:
    """Keep a spawned process and its exact marker context inseparable."""

    def __init__(self, authority_context: Any) -> None:
        self._process_handle: Any | None = None
        setattr(self, _CHILD_AUTHORITY_ATTRIBUTE, authority_context)
        setattr(self, _CHILD_AUTHORITY_CLEANUP_ERROR_ATTRIBUTE, None)

    def bind(self, process_handle: Any) -> None:
        if self._process_handle is not None:
            raise RuntimeError("managed child process is already bound")
        self._process_handle = process_handle

    def poll(self) -> int | None:
        if self._process_handle is None:
            return 1
        return self._process_handle.poll()

    def __getattr__(self, name: str) -> Any:
        process_handle = self._process_handle
        if process_handle is None:
            raise AttributeError(name)
        return getattr(process_handle, name)


def _close_child_authority(process_handle: Any) -> None:
    try:
        namespace = vars(process_handle)
    except TypeError:
        return
    cleanup_error = namespace.get(_CHILD_AUTHORITY_CLEANUP_ERROR_ATTRIBUTE)
    if cleanup_error is not None:
        raise RuntimeError(
            "manager child authority cleanup is quarantined: "
            f"{cleanup_error}"
        )
    authority = namespace.get(_CHILD_AUTHORITY_ATTRIBUTE)
    if authority is None:
        return
    try:
        authority.__exit__(None, None, None)
    except BaseException as error:
        detail = f"{type(error).__name__}:{error}"
        namespace[_CHILD_AUTHORITY_CLEANUP_ERROR_ATTRIBUTE] = detail
        raise RuntimeError(
            f"manager child authority cleanup failed: {detail}"
        ) from error
    namespace[_CHILD_AUTHORITY_ATTRIBUTE] = None


def _child_authority_role(name: str) -> str:
    return "vision_supervisor" if name == "vision" else name


def _reconcile_managed_children(
    children: dict[str, Any],
    commands: Mapping[str, list[str]],
    database: Path,
    database_identity: DatabaseFileIdentity,
    *,
    authority_gate: Callable[..., TerminationResult] | None = None,
    popen_factory: Callable[..., Any] | None = None,
    process_factory: Callable[[int], Any] = psutil.Process,
) -> TerminationResult:
    pending = {
        name: command
        for name, command in commands.items()
        if children.get(name) is None or children[name].poll() is not None
    }
    if not pending:
        return TerminationResult(True)
    authority_gate = authority_gate or _replacement_authority_gate
    gate = authority_gate(children, database, database_identity)
    if not gate.ok:
        return gate
    popen_factory = popen_factory or subprocess.Popen
    for name, command in pending.items():
        previous = children.get(name)
        if previous is not None:
            try:
                _close_child_authority(previous)
            except Exception as error:
                return TerminationResult(
                    False,
                    f"child_authority_cleanup_failed:{name}:"
                    f"{type(error).__name__}:{error}",
                )
        authority_context = manager_child_authority(
            database,
            role=_child_authority_role(name),
            command=command,
            delegate_roles=("vision_watcher",) if name == "vision" else (),
            held_locks=database_service_authority_lock_paths(database),
        )
        managed_child = _ManagedChildHandle(authority_context)
        children[name] = managed_child
        process_handle: Any | None = None
        try:
            authority = authority_context.__enter__()
            process_handle = popen_factory(
                command,
                cwd=ROOT,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
                env=manager_child_process_environment(authority),
            )
            managed_child.bind(process_handle)
        except BaseException as spawn_error:
            if process_handle is not None:
                if managed_child._process_handle is None:
                    managed_child._process_handle = process_handle
                termination = terminate_subprocess_tree(
                    managed_child,
                    process_factory=process_factory,
                )
                if not termination.ok:
                    children[name] = managed_child
                    if not isinstance(spawn_error, Exception):
                        raise
                    return TerminationResult(
                        False,
                        f"child_spawn_failed:{name}:"
                        f"{type(spawn_error).__name__};"
                        f"termination_unproven:{name}:{termination.detail}",
                    )
            try:
                _close_child_authority(managed_child)
            except BaseException as cleanup_error:
                children[name] = managed_child
                if not isinstance(spawn_error, Exception):
                    raise spawn_error
                return TerminationResult(
                    False,
                    f"child_spawn_failed:{name}:"
                    f"{type(spawn_error).__name__};"
                    f"child_authority_cleanup_failed:{name}:"
                    f"{type(cleanup_error).__name__}:{cleanup_error}",
                )
            children.pop(name, None)
            raise
        try:
            bind_manager_child_authority(
                authority,
                process_handle,
                process_factory=process_factory,
            )
        except BaseException as bind_error:
            termination = terminate_subprocess_tree(
                managed_child,
                process_factory=process_factory,
            )
            if not termination.ok:
                if not isinstance(bind_error, Exception):
                    raise
                return TerminationResult(
                    False,
                    f"child_authority_bind_failed:{name}:"
                    f"{type(bind_error).__name__}:{bind_error};"
                    f"termination_unproven:{name}:{termination.detail}",
                )
            try:
                _close_child_authority(managed_child)
            except BaseException as cleanup_error:
                if not isinstance(bind_error, Exception):
                    raise bind_error
                return TerminationResult(
                    False,
                    f"child_authority_bind_failed:{name}:"
                    f"{type(bind_error).__name__}:{bind_error};"
                    f"child_authority_cleanup_failed:{name}:"
                    f"{type(cleanup_error).__name__}:{cleanup_error}",
                )
            children.pop(name, None)
            if not isinstance(bind_error, Exception):
                raise
            return TerminationResult(
                False,
                f"child_authority_bind_failed:{name}:"
                f"{type(bind_error).__name__}:{bind_error}",
            )
    return TerminationResult(True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(
        parser,
        default=ROOT / "data" / "dota2.db",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--start-collector", action="store_true")
    parser.add_argument("--start-companion", action="store_true")
    parser.add_argument("--start-shadow", action="store_true")
    parser.add_argument("--start-vision", action="store_true")
    parser.add_argument("--start-mail", action="store_true")
    parser.add_argument("--start-strict-ingest", action="store_true")
    parser.add_argument("--start-postmatch", action="store_true")
    parser.add_argument("--start-draft-publisher", action="store_true")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help=(
            "migration backup directory; defaults to <database-dir>/backups "
            "and may be placed on another volume"
        ),
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "verify current schema read-only, or take a backup before "
            "migration/repair"
        ),
    )
    parser.add_argument("--vision-jsonl", type=Path)
    args = parser.parse_args()
    args.database = args.database.resolve()
    if args.report is None:
        args.report = args.database.parent / "live_betting" / "service_report.json"
    authority_lock_paths = database_service_authority_lock_paths(args.database)
    additional_lock_path = args.lock.resolve() if args.lock is not None else None
    try:
        commands = _commands(args)
    except ValueError as error:
        parser.error(str(error))
    children: dict[str, Any] = {}
    report_worker: _ReportWorker | None = None
    initial_database_identity = require_unique_database_file(
        args.database,
        allow_missing=True,
    )
    database_identity: DatabaseFileIdentity | None = None
    with ExitStack() as locks:
        for authority_lock_path in authority_lock_paths:
            locks.enter_context(SingleInstanceLock(authority_lock_path))
        if (
            additional_lock_path is not None
            and additional_lock_path not in set(authority_lock_paths)
        ):
            locks.enter_context(SingleInstanceLock(additional_lock_path))
        try:
            locked_database_identity = require_unique_database_file(
                args.database,
                expected_identity=initial_database_identity,
                allow_missing=initial_database_identity is None,
            )
            if locked_database_identity != initial_database_identity:
                raise RuntimeError(
                    "database file identity changed before service lock"
                )
            writer_scan = scan_managed_writers(args.database)
            if writer_scan.unverifiable_pids:
                raise RuntimeError(
                    "managed writer scan could not verify PIDs: "
                    + ",".join(str(pid) for pid in writer_scan.unverifiable_pids)
                )
            if writer_scan.conflicts:
                raise RuntimeError(
                    "managed writers already target this database: "
                    + ",".join(
                        str(identity.pid) for identity in writer_scan.conflicts
                    )
                )
            preparation = (
                prepare_database(
                    args.database,
                    args.backup_dir or args.database.parent / "backups",
                    supervisor_process_lock_held=True,
                    odds_raw_root=(
                        args.database.resolve().parent / "live_betting" / "raw-v2"
                    ),
                )
                if args.migrate
                else verify_prepared_database(args.database)
            )
            database_identity = require_unique_database_file(
                args.database,
                expected_identity=locked_database_identity,
            )
            assert database_identity is not None
            report_worker = (
                None if args.once else _ReportWorker(args.database, args.report)
            )
            while True:
                try:
                    require_unique_database_file(
                        args.database,
                        expected_identity=database_identity,
                    )
                except Exception:
                    _shutdown_children_under_authority(
                        children,
                        args.database,
                        database_identity,
                    )
                    continue
                reconciliation = _reconcile_managed_children(
                    children,
                    commands,
                    args.database,
                    database_identity,
                )
                if not reconciliation.ok:
                    print(
                        json.dumps(
                            {
                                "status": "quarantined",
                                "detail": reconciliation.detail,
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    _shutdown_children_under_authority(
                        children,
                        args.database,
                        database_identity,
                    )
                    continue
                require_unique_database_file(
                    args.database,
                    expected_identity=database_identity,
                )
                try:
                    result = service_once(
                        args.database,
                        args.report if args.once else None,
                        active_components=set(commands),
                        initialize_schema=False,
                        health_only=report_worker is not None,
                    )
                except sqlite3.OperationalError as error:
                    lock_kind = _transient_sqlite_lock_kind(error)
                    if lock_kind is None:
                        raise
                    print(
                        json.dumps(
                            {
                                "status": "degraded",
                                "detail": "database_lock_contention",
                                "sqlite_result": lock_kind,
                            },
                            ensure_ascii=False,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(max(args.interval, LOCK_CONTENTION_RETRY_MIN_SECONDS))
                    continue
                if report_worker is not None:
                    report_worker.start_if_idle()
                print(json.dumps({"status": "ok", "components": list(commands),
                                  "pending": (
                                      result["pending_orders"]
                                      if report_worker is not None
                                      else result["shadow"]["orders"]["signals"]
                                  ),
                                  "backup": (
                                      str(preparation.backup)
                                      if preparation.backup is not None
                                      else None
                                  ),
                                  "schema_versions": {
                                      "live": preparation.live_schema_version,
                                      "intelligence": (
                                          preparation.intelligence_schema_version
                                      ),
                                      "runtime": preparation.runtime_schema_version,
                                  }},
                                 ensure_ascii=False))
                if args.once:
                    break
                time.sleep(args.interval)
        finally:
            if report_worker is not None:
                report_worker.wait()
            shutdown = (
                _shutdown_children_under_authority(
                    children,
                    args.database,
                    database_identity,
                )
                if database_identity is not None
                else TerminationResult(not children, "database_identity_unavailable")
            )
            if not shutdown.ok:
                raise RuntimeError(
                    f"supervisor child shutdown incomplete: {shutdown.detail}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
