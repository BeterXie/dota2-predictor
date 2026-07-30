"""Supervise the local Dota shadow components on PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import text


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import build_engine, require_database_url  # noqa: E402
from event_intelligence.report import build_intelligence_report  # noqa: E402
from live_betting.browser_companion import PROTOCOL_VERSION  # noqa: E402
from live_betting.health import record_health  # noqa: E402
from live_betting.report import build_report  # noqa: E402
from live_betting.process_control import (  # noqa: E402
    MARKET_SOURCE_POLICY,
    terminate_subprocess_tree,
)
from live_betting.smtp_delivery import (  # noqa: E402
    SMTPConfig,
    SMTPConfigurationError,
)
from live_betting.storage import LiveBettingStore  # noqa: E402
from web.alerts import reconcile_alerts  # noqa: E402


COMPANION_HEALTH_URL = "http://127.0.0.1:8765/health"
SERVICE_LOCK_KEY = "dota2-predictor:shadow-service"
CHILD_RESTART_DELAYS_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
WORKER_MAX_AGE = {
    "raybet": timedelta(seconds=45),
    "shadow": timedelta(seconds=45),
    "mail": timedelta(seconds=90),
    "vision": timedelta(seconds=90),
    "strict_ingest": timedelta(seconds=90),
    "postmatch": timedelta(seconds=150),
    "draft_publisher": timedelta(minutes=15),
    "historical_rosh": timedelta(minutes=15),
}
WORKER_COMPONENTS = {
    "raybet": "raybet_worker",
    "shadow": "shadow_worker",
    "mail": "mail_worker",
    "vision": "vision_worker",
    "strict_ingest": "strict_ingest_worker",
    "postmatch": "postmatch_worker",
    "draft_publisher": "draft_publisher_worker",
    "historical_rosh": "historical_rosh_worker",
}


@dataclass
class _Child:
    process: subprocess.Popen[Any]
    stdout: Any
    stderr: Any
    failures: int = 0
    restart_at: float = 0.0


def _write_service_report(report_path: Path, result: Mapping[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f".{report_path.name}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)


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


def _database_health(connection: Any) -> tuple[str, str | None, dict[str, Any]]:
    row = connection.execute(
        """SELECT current_database(), pg_is_in_recovery(),
                  (SELECT COUNT(*)
                     FROM pg_constraint
                    WHERE contype='f' AND NOT convalidated)"""
    ).fetchone()
    if row is None:
        return "unhealthy", "database_probe_failed", {}
    details = {
        "backend": "postgresql",
        "database": str(row[0]),
        "in_recovery": bool(row[1]),
        "unvalidated_foreign_keys": int(row[2]),
    }
    if details["in_recovery"]:
        return "unhealthy", "database_is_read_only_replica", details
    if details["unvalidated_foreign_keys"]:
        return "unhealthy", "foreign_key_validation_incomplete", details
    return "healthy", None, details


def _record_component(
    connection: Any,
    component: str,
    status: str,
    now: datetime,
    *,
    reason: str | None,
    details: Mapping[str, Any],
    informational: bool = False,
) -> None:
    is_error = status in {"degraded", "unhealthy"} and not informational
    record_health(
        connection,
        component,
        status,
        heartbeat_at=now,
        success_at=now if status == "healthy" else None,
        error_at=now if is_error else None,
        error=reason if is_error else None,
        details={"source": "supervisor", **dict(details)},
    )


def _worker_health(
    connection: Any,
    component: str,
    now: datetime,
    active_components: set[str],
) -> tuple[str, dict[str, Any]]:
    worker = WORKER_COMPONENTS[component]
    row = connection.execute(
        "SELECT status, last_heartbeat_at, last_error, details_json "
        "FROM service_health WHERE component=?",
        (worker,),
    ).fetchone()
    if row is None:
        status = "starting" if component in active_components else "stopped"
        return status, {"worker_component": worker, "reason": "awaiting_heartbeat"}
    heartbeat = _parse_time(row["last_heartbeat_at"])
    if heartbeat is None:
        return "unhealthy", {"worker_component": worker, "reason": "invalid_heartbeat"}
    age = max(0.0, (now - heartbeat).total_seconds())
    limit = WORKER_MAX_AGE[component].total_seconds()
    try:
        worker_details = json.loads(str(row["details_json"] or "{}"))
    except (TypeError, ValueError):
        worker_details = {}
    if age > limit * 2:
        status, reason = "unhealthy", "heartbeat_expired"
    elif age > limit:
        status, reason = "degraded", "heartbeat_stale"
    else:
        status = str(row["status"])
        reason = str(row["last_error"] or f"worker_status_{status}")
    return status, {
        "worker_component": worker,
        "worker_heartbeat_at": heartbeat.isoformat(),
        "heartbeat_age_seconds": round(age, 3),
        "worker_details": worker_details,
        "reason": reason,
    }


def _probe_companion() -> Mapping[str, Any]:
    with urllib.request.urlopen(COMPANION_HEALTH_URL, timeout=1.0) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("companion health payload is invalid")
    return payload


def _companion_health(
    active: bool,
    probe: Callable[[], Mapping[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    if not active:
        return "stopped", "not_started_by_supervisor", {"required": False}
    try:
        payload = probe()
    except Exception as error:
        return "degraded", "companion_probe_failed", {
            "required": False,
            "error_type": type(error).__name__,
        }
    protocol = payload.get("protocol_version")
    if protocol != PROTOCOL_VERSION:
        return "degraded", "companion_protocol_mismatch", {
            "required": False,
            "protocol_version": protocol,
        }
    return "healthy", "companion_available", {
        "required": False,
        "protocol_version": protocol,
    }


def _service_capabilities(connection: Any) -> dict[str, dict[str, Any]]:
    statuses = {
        str(row["component"]): str(row["status"])
        for row in connection.execute(
            "SELECT component, status FROM service_health"
        ).fetchall()
    }
    return {
        "direct_market_collection": {
            "required": True,
            "status": statuses.get("raybet", "stopped"),
        },
        "vision": {
            "required": True,
            "status": statuses.get("vision", "stopped"),
        },
        "paper_decision": {
            "required": True,
            "status": statuses.get("shadow", "stopped"),
        },
        "browser_compare": {
            "required": False,
            "status": statuses.get("companion", "stopped"),
        },
    }


def service_once(
    database_url: str,
    report_path: Path | None = None,
    *,
    active_components: set[str] | None = None,
    companion_probe: Callable[[], Mapping[str, Any]] = _probe_companion,
    initialize_schema: bool = True,
    health_only: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    active = set(active_components or ())
    with LiveBettingStore(database_url) as store:
        if initialize_schema:
            store.init_schema()
        connection = store.connection
        database_status, database_reason, database_details = _database_health(
            connection
        )
        _record_component(
            connection,
            "database",
            database_status,
            now,
            reason=database_reason,
            details=database_details,
        )
        for component in WORKER_COMPONENTS:
            status, details = _worker_health(connection, component, now, active)
            _record_component(
                connection,
                component,
                status,
                now,
                reason=str(details.get("reason", "worker_unavailable")),
                details=details,
            )

        pending = int(
            connection.execute(
                "SELECT COUNT(*) FROM shadow_orders WHERE status='pending'"
            ).fetchone()[0]
        )
        try:
            SMTPConfig.from_environment()
            smtp_configured = True
        except SMTPConfigurationError:
            smtp_configured = False
        mail_row = connection.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status='dead_letter'"
        ).fetchone()
        _record_component(
            connection,
            "mail_delivery",
            "healthy" if smtp_configured and int(mail_row[0]) == 0 else "degraded",
            now,
            reason=None if smtp_configured else "configuration_missing",
            details={
                "smtp_configured": smtp_configured,
                "dead_letters": int(mail_row[0]),
            },
        )
        companion_status, companion_reason, companion_details = _companion_health(
            "companion" in active,
            companion_probe,
        )
        _record_component(
            connection,
            "companion",
            companion_status,
            now,
            reason=companion_reason,
            details=companion_details,
            informational="companion" not in active,
        )
        reconcile_alerts(connection, now=now)
        if health_only:
            return {"pending_orders": pending}
        result = {
            "market_source_policy": MARKET_SOURCE_POLICY,
            "capabilities": _service_capabilities(connection),
            "shadow": build_report(connection),
            "intelligence": build_intelligence_report(connection),
        }
    if report_path is not None:
        _write_service_report(report_path, result)
    return result


class _ReportWorker:
    def __init__(
        self,
        database_url: str,
        report_path: Path,
        *,
        report_interval: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database_url = database_url
        self.report_path = report_path
        self.report_interval = report_interval
        self._monotonic = monotonic
        self._thread: threading.Thread | None = None
        self._last_finished_at: float | None = None
        self.last_error: str | None = None

    def start_if_idle(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        if (
            self._last_finished_at is not None
            and self._monotonic() - self._last_finished_at < self.report_interval
        ):
            return False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            service_once(self.database_url, self.report_path)
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
        else:
            self.last_error = None
        finally:
            self._last_finished_at = self._monotonic()


def _data_paths() -> dict[str, Path]:
    root = ROOT / "data" / "live_betting"
    return {
        "raw": root / "raw-v2",
        "observations": root / "vision_observations",
        "evidence": root / "vision_evidence",
        "vision_logs": root / "watcher_logs",
        "managed_logs": root / "logs" / "managed",
        "source_archive": ROOT / "data" / "raw-sources",
        "coverage": ROOT / "data" / "reports" / "strict_event_coverage_latest.json",
    }


def _commands(args: argparse.Namespace) -> dict[str, list[str]]:
    paths = _data_paths()
    python = sys.executable
    commands: dict[str, list[str]] = {}
    if args.start_collector:
        commands["collector"] = [
            python,
            "-m",
            "live_betting.monitor",
            "--raw-dir",
            str(paths["raw"]),
            "--schema-prepared",
        ]
    if args.start_companion:
        commands["companion"] = [
            python,
            "-m",
            "live_betting.browser_companion",
            "--schema-prepared",
        ]
    if args.start_shadow:
        commands["shadow"] = [
            python,
            "-m",
            "live_betting.shadow_monitor",
            "--vision-jsonl",
            str(paths["observations"]),
            "--schema-prepared",
        ]
    if args.start_vision:
        commands["vision"] = [
            python,
            "scripts/supervise_raybet_streams.py",
            "--output-dir",
            str(paths["observations"]),
            "--evidence-dir",
            str(paths["evidence"]),
            "--log-dir",
            str(paths["vision_logs"]),
            "--schema-prepared",
        ]
    if args.start_mail:
        commands["mail"] = [
            python,
            "scripts/run_notification_worker.py",
            "--schema-prepared",
        ]
    if args.start_strict_ingest:
        commands["strict_ingest"] = [
            python,
            "scripts/run_strict_event_ingest.py",
            "--archive-root",
            str(paths["source_archive"]),
            "--coverage-report",
            str(paths["coverage"]),
            "--schema-prepared",
        ]
    if args.start_postmatch:
        commands["postmatch"] = [
            python,
            "-m",
            "live_betting.postmatch_monitor",
            "--all",
            "--archive-root",
            str(paths["source_archive"]),
            "--schema-prepared",
        ]
    if args.start_draft_publisher or args.start_shadow:
        deployment_key = args.draft_deployment_key
        if (
            not isinstance(deployment_key, str)
            or len(deployment_key) != 64
            or any(character not in "0123456789abcdef" for character in deployment_key)
        ):
            raise ValueError(
                "--draft-deployment-key must be a lowercase SHA-256 when the "
                "draft publisher is enabled"
            )
        commands["draft_publisher"] = [
            python,
            "-m",
            "live_betting.draft_publisher",
            "--deployment-key",
            deployment_key,
        ]
    if not args.once and not args.disable_historical_rosh:
        commands["historical_rosh"] = [
            python,
            "scripts/run_historical_rosh_worker.py",
            "--schema-prepared",
        ]
    return commands


def _spawn_child(
    name: str,
    command: list[str],
    database_url: str,
    log_dir: Path,
) -> _Child:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / f"{name}.stdout.log").open("a", encoding="utf-8")
    stderr = (log_dir / f"{name}.stderr.log").open("a", encoding="utf-8")
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except BaseException:
        stdout.close()
        stderr.close()
        raise
    return _Child(process=process, stdout=stdout, stderr=stderr)


def _reconcile_children(
    children: dict[str, _Child],
    commands: Mapping[str, list[str]],
    database_url: str,
    log_dir: Path,
) -> None:
    now = time.monotonic()
    for name in tuple(children):
        child = children[name]
        code = child.process.poll()
        if name not in commands:
            terminate_subprocess_tree(child.process)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()
            del children[name]
        elif code is not None and child.stdout is not None:
            child.stdout.close()
            child.stderr.close()
            failures = child.failures + 1
            delay = CHILD_RESTART_DELAYS_SECONDS[
                min(failures - 1, len(CHILD_RESTART_DELAYS_SECONDS) - 1)
            ]
            del children[name]
            replacement = _Child(
                process=child.process,
                stdout=None,
                stderr=None,
                failures=failures,
                restart_at=now + delay,
            )
            children[name] = replacement
    for name, command in commands.items():
        child = children.get(name)
        if child is not None and child.process.poll() is None:
            continue
        if child is not None and now < child.restart_at:
            continue
        failures = 0 if child is None else child.failures
        spawned = _spawn_child(name, command, database_url, log_dir)
        spawned.failures = failures
        children[name] = spawned


def _shutdown_children(children: dict[str, _Child]) -> None:
    for child in children.values():
        if child.process.poll() is None:
            terminate_subprocess_tree(child.process)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()
    children.clear()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--report", type=Path)
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
    parser.add_argument("--disable-historical-rosh", action="store_true")
    parser.add_argument("--draft-deployment-key")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    database_url = require_database_url(args.database_url)
    report = args.report or ROOT / "data" / "live_betting" / "service_report.json"
    try:
        commands = _commands(args)
    except ValueError as error:
        parser.error(str(error))

    with LiveBettingStore(database_url) as store:
        store.init_schema()

    engine = build_engine(database_url)
    lock_connection = engine.connect()
    acquired = bool(
        lock_connection.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
            {"key": SERVICE_LOCK_KEY},
        ).scalar_one()
    )
    if not acquired:
        lock_connection.close()
        engine.dispose()
        raise RuntimeError("another shadow service supervisor is already running")

    stopped = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    children: dict[str, _Child] = {}
    report_worker = _ReportWorker(database_url, report)
    try:
        while not stopped.is_set():
            _reconcile_children(
                children,
                commands,
                database_url,
                _data_paths()["managed_logs"],
            )
            result = service_once(
                database_url,
                report if args.once else None,
                active_components=set(commands),
                initialize_schema=False,
                health_only=not args.once,
            )
            if not args.once:
                report_worker.start_if_idle()
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "database": "postgresql",
                        "components": sorted(commands),
                        "pending": (
                            result["pending_orders"]
                            if not args.once
                            else result["shadow"]["orders"]["signals"]
                        ),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.once:
                break
            stopped.wait(args.interval)
    finally:
        report_worker.wait()
        _shutdown_children(children)
        lock_connection.execute(
            text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
            {"key": SERVICE_LOCK_KEY},
        )
        lock_connection.close()
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
