"""Supervise one visual watcher for every active RayBet match with video."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LIVE_BETTING_DATA = ROOT / "data" / "live_betting"
DEFAULT_OBSERVATION_DIR = LIVE_BETTING_DATA / "live_observations"

from shared.sqlite import connect as connect_sqlite  # noqa: E402
from live_betting.health import record_health  # noqa: E402
from live_betting.raybet_state import raybet_match_is_live  # noqa: E402

DEFAULT_EVIDENCE_DIR = LIVE_BETTING_DATA / "live_evidence"
DEFAULT_LOG_DIR = LIVE_BETTING_DATA / "watcher_logs"


def record_supervisor_health(
    database: Path,
    status: str,
    *,
    active_matches: int,
    error: str | None = None,
) -> None:
    connection = connect_sqlite(database)
    try:
        now = datetime.now(timezone.utc)
        record_health(
            connection,
            "vision_worker",
            status,
            heartbeat_at=now,
            success_at=now if status == "healthy" else None,
            error_at=now if error is not None else None,
            error=error,
            details={"active_watchers": active_matches},
        )
        connection.commit()
    finally:
        connection.close()


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
    children: dict[str, tuple[subprocess.Popen, object, object]],
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
    children: dict[str, tuple[subprocess.Popen, object, object]] = {}
    last_start: dict[str, float] = {}
    try:
        while True:
            try:
                active = set(active_matches(args.database))
                reap_children(children, active)
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
                    children[match_id] = (process, stdout, stderr)
                    last_start[match_id] = time.monotonic()
                record_supervisor_health(
                    args.database, "healthy", active_matches=len(active)
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
