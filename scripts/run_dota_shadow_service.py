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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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
from live_betting.smtp_delivery import (  # noqa: E402
    SMTPConfig,
    SMTPConfigurationError,
)
from live_betting.storage import LiveBettingStore  # noqa: E402


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
COMPANION_HEALTH_URL = "http://127.0.0.1:8765/health"
_DATABASE_HEALTH_CACHE: dict[
    str,
    tuple[datetime, tuple[str, str | None, dict[str, Any]]],
] = {}
_DATABASE_AUDIT_THREADS: dict[str, threading.Thread] = {}
_DATABASE_AUDIT_LOCK = threading.Lock()


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


class SingleInstanceLock:
    """Cross-platform advisory lock held for the supervisor lifetime."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> "SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                self._handle.write(b"0")
                self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self._handle.close()
            self._handle = None
            raise RuntimeError(f"service lock is already held: {self.path}")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(str(os.getpid()).encode("ascii"))
        self._handle.flush()
        return self

    def __exit__(self, *args: object) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


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
    if args.start_collector:
        commands["collector"] = [
            python,
            "-m",
            "live_betting.monitor",
            "--database",
            str(args.database),
            "--schema-prepared",
        ]
    if args.start_companion:
        commands["companion"] = [
            python,
            "-m",
            "live_betting.browser_companion",
            "--database",
            str(args.database),
            "--schema-prepared",
        ]
    if args.start_shadow:
        if not args.vision_jsonl:
            raise ValueError("--vision-jsonl is required with --start-shadow")
        commands["shadow"] = [
            python,
            "scripts/run_comeback_shadow.py",
            "--database",
            str(args.database),
            "--vision-jsonl",
            str(args.vision_jsonl),
            "--schema-prepared",
        ]
    if getattr(args, "start_vision", False):
        commands["vision"] = [
            python,
            "scripts/supervise_raybet_streams.py",
            "--database",
            str(args.database),
            "--schema-prepared",
        ]
    if args.start_mail:
        commands["mail"] = [
            python,
            "scripts/run_notification_worker.py",
            "--database",
            str(args.database),
            "--schema-prepared",
        ]
    if args.start_strict_ingest:
        commands["strict_ingest"] = [
            python,
            "scripts/run_strict_event_ingest.py",
            "--database",
            str(args.database),
            "--schema-prepared",
        ]
    if args.start_postmatch:
        commands["postmatch"] = [
            python,
            "scripts/run_postmatch_labeler.py",
            "--database",
            str(args.database),
            "--all",
            "--schema-prepared",
        ]
    if getattr(args, "start_draft_publisher", False) or args.start_shadow:
        commands["draft_publisher"] = [
            python,
            "-m",
            "live_betting.draft_publisher",
            "--database",
            str(args.database),
            "--schema-prepared",
        ]
    return commands


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--report", type=Path, default=ROOT / "data" / "live_betting" / "service_report.json")
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
        help="take a verified backup and run additive schema migrations",
    )
    parser.add_argument("--vision-jsonl", type=Path)
    args = parser.parse_args()
    lock_path = args.lock or args.database.with_suffix(".service.lock")
    try:
        commands = _commands(args)
    except ValueError as error:
        parser.error(str(error))
    children: dict[str, subprocess.Popen[bytes]] = {}
    try:
        with SingleInstanceLock(lock_path):
            preparation = (
                prepare_database(
                    args.database,
                    args.backup_dir or args.database.parent / "backups",
                )
                if args.migrate
                else verify_prepared_database(args.database)
            )
            report_worker = (
                None if args.once else _ReportWorker(args.database, args.report)
            )
            while True:
                for name, command in commands.items():
                    child = children.get(name)
                    if child is None or child.poll() is not None:
                        children[name] = subprocess.Popen(
                            command,
                            cwd=ROOT,
                            creationflags=(
                                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                            ),
                        )
                result = service_once(
                    args.database,
                    args.report if args.once else None,
                    active_components=set(commands),
                    initialize_schema=False,
                    health_only=report_worker is not None,
                )
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
                                  }},
                                 ensure_ascii=False))
                if args.once:
                    break
                time.sleep(args.interval)
    finally:
        for child in children.values():
            if child.poll() is None:
                child.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
