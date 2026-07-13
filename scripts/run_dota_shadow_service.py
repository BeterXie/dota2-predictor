"""Single-instance supervisor for local Dota shadow components."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_intelligence.report import build_intelligence_report  # noqa: E402
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
}
ACTIVE_COMMANDS = {"raybet": "collector", "shadow": "shadow", "mail": "mail"}
WORKER_MAX_AGE = {
    "raybet": timedelta(seconds=45),
    "shadow": timedelta(seconds=45),
    "mail": timedelta(seconds=90),
}
COLLECTOR_MAX_AGE = timedelta(seconds=60)


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
        for row in connection.execute("PRAGMA integrity_check").fetchall()
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
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    active_components = set(active_components or ())
    with LiveBettingStore(database) as store:
        store.init_schema()
        connection = store.connection
        database_status, database_reason, database_details = _database_health(connection)
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
        if mail_worker_status == "unhealthy":
            mail_status = "unhealthy"
            mail_error = str(mail_details.get("reason", "worker_unavailable"))
        elif not smtp_configured:
            mail_status, mail_error = "degraded", "configuration_missing"
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
        report = build_report(connection)
        intelligence = build_intelligence_report(connection)
        result = {"shadow": report, "intelligence": intelligence}
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
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
        ]
    if args.start_companion:
        commands["companion"] = [
            python,
            "-m",
            "live_betting.browser_companion",
            "--database",
            str(args.database),
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
        ]
    if args.start_mail:
        commands["mail"] = [
            python,
            "scripts/run_notification_worker.py",
            "--database",
            str(args.database),
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
    parser.add_argument("--start-mail", action="store_true")
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
                    args.report,
                    active_components=set(commands),
                )
                print(json.dumps({"status": "ok", "components": list(commands),
                                  "pending": result["shadow"]["orders"]["signals"]},
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
