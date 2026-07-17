"""Supervise one visual watcher for every active RayBet match with video."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LIVE_BETTING_DATA = ROOT / "data" / "live_betting"
DEFAULT_OBSERVATION_DIR = LIVE_BETTING_DATA / "live_observations"

from shared.sqlite import connect as connect_sqlite  # noqa: E402
from live_betting.health import record_health  # noqa: E402
from live_betting.raybet_state import raybet_match_is_live  # noqa: E402
from live_betting.vision_retention import prune_vision_evidence  # noqa: E402

DEFAULT_EVIDENCE_DIR = LIVE_BETTING_DATA / "live_evidence"
DEFAULT_LOG_DIR = LIVE_BETTING_DATA / "watcher_logs"
OUTPUT_MAX_AGE = timedelta(seconds=90)
WATCHER_STARTUP_GRACE = timedelta(seconds=90)
RETENTION_INTERVAL_SECONDS = 60 * 60

Child = tuple[subprocess.Popen, object, object]
OutputSignature = tuple[int, int]


def record_supervisor_health(
    database: Path,
    status: str,
    *,
    active_matches: int,
    error: str | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    connection = connect_sqlite(database)
    try:
        now = datetime.now(timezone.utc)
        health_details = {"active_watchers": active_matches}
        health_details.update(details or {})
        record_health(
            connection,
            "vision_worker",
            status,
            heartbeat_at=now,
            success_at=now if status == "healthy" else None,
            error_at=now if error is not None else None,
            error=error,
            details=health_details,
        )
        connection.commit()
    finally:
        connection.close()


def _output_signature(path: Path) -> OutputSignature | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def supervisor_health(
    desired: set[str],
    children: Mapping[str, Child],
    output_dir: Path,
    *,
    started_at: Mapping[str, datetime] | None = None,
    output_baselines: Mapping[str, OutputSignature | None] | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, object], str | None]:
    """Describe whether desired watchers are alive and producing fresh output."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started_at = started_at or {}
    output_baselines = output_baselines or {}
    running: set[str] = set()
    producing: set[str] = set()
    watcher_details: dict[str, dict[str, object]] = {}
    stale: set[str] = set()

    for match_id in sorted(desired):
        child = children.get(match_id)
        is_running = child is not None and child[0].poll() is None
        if is_running:
            running.add(match_id)

        path = output_dir / f"{match_id}.jsonl"
        signature = _output_signature(path)
        baseline_known = match_id in output_baselines
        has_current_output = signature is not None and (
            not baseline_known or signature != output_baselines[match_id]
        )
        output_at = (
            datetime.fromtimestamp(signature[1] / 1_000_000_000, timezone.utc)
            if signature is not None
            else None
        )
        output_age = (
            max(0.0, (now - output_at).total_seconds())
            if output_at is not None
            else None
        )
        is_producing = bool(
            is_running
            and has_current_output
            and output_age is not None
            and output_age <= OUTPUT_MAX_AGE.total_seconds()
        )
        if is_producing:
            producing.add(match_id)

        started = started_at.get(match_id)
        startup_age = (
            max(0.0, (now - started.astimezone(timezone.utc)).total_seconds())
            if started is not None
            else None
        )
        if not is_running:
            state = "desired"
            reason = "watcher_not_running"
        elif is_producing:
            state = "producing"
            reason = "fresh_output"
        elif startup_age is not None and startup_age <= WATCHER_STARTUP_GRACE.total_seconds():
            state = "running"
            reason = "awaiting_first_output"
        else:
            state = "running"
            reason = "output_stale" if has_current_output else "no_current_output"
            stale.add(match_id)
        watcher_details[match_id] = {
            "state": state,
            "running": is_running,
            "producing": is_producing,
            "reason": reason,
            "output_updated_at": output_at.isoformat() if output_at else None,
            "output_age_seconds": round(output_age, 3) if output_age is not None else None,
        }

    missing = desired - running
    waiting = running - producing - stale
    if not desired or producing == desired:
        status = "healthy"
        reason = "idle" if not desired else "all_watchers_producing"
        error = None
    elif missing:
        status = "unhealthy"
        reason = "watchers_not_running"
        error = f"watchers not running: {','.join(sorted(missing))}"
    elif stale:
        status = "unhealthy"
        reason = "watchers_not_producing"
        error = f"watchers not producing fresh output: {','.join(sorted(stale))}"
    elif waiting == desired:
        status = "starting"
        reason = "awaiting_first_output"
        error = None
    else:
        status = "degraded"
        reason = "watchers_starting"
        error = None

    details: dict[str, object] = {
        "desired_watchers": len(desired),
        "running_watchers": len(running),
        "producing_watchers": len(producing),
        "desired_match_ids": sorted(desired),
        "running_match_ids": sorted(running),
        "producing_match_ids": sorted(producing),
        "reason": reason,
        "watchers": watcher_details,
    }
    return status, details, error


def active_matches(database: Path, *, now: datetime | None = None) -> list[str]:
    connection = connect_sqlite(database, read_only=True)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                """SELECT raybet_match_id, status, updated_at
                     FROM raybet_matches
                    WHERE live_url IS NOT NULL AND live_url != ''"""
            )
            if raybet_match_is_live(row[1], row[2], now=now)
        ]
    finally:
        connection.close()


def run_evidence_retention(
    database: Path,
    evidence_dir: Path,
    active_match_ids: set[str],
) -> dict[str, object]:
    """Apply the fixed policy while excluding every currently active match."""
    return prune_vision_evidence(
        database,
        evidence_dir,
        excluded_match_ids=active_match_ids,
        dry_run=False,
    ).as_dict()


def watcher_command(
    database: Path,
    match_id: str,
    output_dir: Path,
    evidence_dir: Path,
) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts" / "watch_raybet_stream.py"),
        "--match-id",
        match_id,
        "--database",
        str(database),
        "--output",
        str(output_dir / f"{match_id}.jsonl"),
        "--evidence-dir",
        str(evidence_dir / match_id),
        "--interval",
        "1",
        "--evidence-interval",
        "30",
        "--refresh-url",
    ]


def reap_children(
    children: dict[str, Child],
    active: set[str],
) -> None:
    for match_id, (process, stdout, stderr) in list(children.items()):
        if match_id not in active and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if match_id not in active or process.poll() is not None:
            stdout.close()
            stderr.close()
            children.pop(match_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OBSERVATION_DIR)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    children: dict[str, Child] = {}
    last_start: dict[str, float] = {}
    started_at: dict[str, datetime] = {}
    output_baselines: dict[str, OutputSignature | None] = {}
    last_retention_at: float | None = None
    retention_details: dict[str, object] = {"status": "pending"}
    try:
        while True:
            try:
                active = set(active_matches(args.database))
                reap_children(children, active)
                for match_id in set(started_at) - set(children):
                    started_at.pop(match_id, None)
                    output_baselines.pop(match_id, None)
                for match_id in active:
                    if (
                        match_id in children
                        or time.monotonic() - last_start.get(match_id, 0) < 30
                    ):
                        continue
                    stdout = (args.log_dir / f"{match_id}.stdout.log").open(
                        "a", encoding="utf-8"
                    )
                    stderr = (args.log_dir / f"{match_id}.stderr.log").open(
                        "a", encoding="utf-8"
                    )
                    output_baselines[match_id] = _output_signature(
                        args.output_dir / f"{match_id}.jsonl"
                    )
                    try:
                        process = subprocess.Popen(
                            watcher_command(
                                args.database,
                                match_id,
                                args.output_dir,
                                args.evidence_dir,
                            ),
                            cwd=ROOT,
                            stdout=stdout,
                            stderr=stderr,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except BaseException:
                        stdout.close()
                        stderr.close()
                        output_baselines.pop(match_id, None)
                        raise
                    children[match_id] = (process, stdout, stderr)
                    last_start[match_id] = time.monotonic()
                    started_at[match_id] = datetime.now(timezone.utc)
                monotonic_now = time.monotonic()
                if (
                    last_retention_at is None
                    or monotonic_now - last_retention_at >= RETENTION_INTERVAL_SECONDS
                ):
                    last_retention_at = monotonic_now
                    try:
                        retention_details = run_evidence_retention(
                            args.database, args.evidence_dir, active
                        )
                    except Exception as retention_error:
                        retention_details = {
                            "status": "error",
                            "error_type": type(retention_error).__name__,
                        }
                status, details, error = supervisor_health(
                    active,
                    children,
                    args.output_dir,
                    started_at=started_at,
                    output_baselines=output_baselines,
                )
                details["evidence_retention"] = retention_details
                if retention_details.get("status") == "error":
                    if status == "healthy":
                        status = "degraded"
                    error = error or "vision evidence retention failed"
                record_supervisor_health(
                    args.database,
                    status,
                    active_matches=int(details["running_watchers"]),
                    error=error,
                    details=details,
                )
            except Exception as error:
                try:
                    record_supervisor_health(
                        args.database,
                        "unhealthy",
                        active_matches=len(children),
                        error=f"{type(error).__name__}: {error}",
                    )
                except Exception:
                    pass
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        reap_children(children, set())
        try:
            record_supervisor_health(
                args.database, "stopped", active_matches=0
            )
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
