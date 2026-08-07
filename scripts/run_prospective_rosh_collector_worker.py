"""Periodically collect bounded prospective R.O.S.H. operational shadows."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import threading
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from event_intelligence.prospective_rosh_collector import (  # noqa: E402
    MAX_ACCEPTANCE_MAPS,
    MIN_ACCEPTANCE_MAPS,
    ProspectiveRoshCollectorRepository,
    run_collector_once,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from live_betting.stratz_rosh_client import StratzRoshClient  # noqa: E402


UTC = timezone.utc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _acceptance_limit(value: str) -> int:
    parsed = int(value)
    if not MIN_ACCEPTANCE_MAPS <= parsed <= MAX_ACCEPTANCE_MAPS:
        raise argparse.ArgumentTypeError("must be between 5 and 10")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--batch-size", type=_positive_int, default=1)
    parser.add_argument("--poll-seconds", type=_positive_float, default=30.0)
    parser.add_argument(
        "--acceptance-limit",
        type=_acceptance_limit,
        default=MAX_ACCEPTANCE_MAPS,
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("dogfood-output") / "prospective-rosh-artifacts",
    )
    parser.add_argument("--schema-prepared", action="store_true")
    return parser


def run_worker(
    database_url: str,
    *,
    batch_size: int = 1,
    poll_seconds: float = 30.0,
    acceptance_limit: int = MAX_ACCEPTANCE_MAPS,
    artifact_root: Path,
    stop_event: threading.Event | None = None,
    clock: Callable[[], datetime] | None = None,
    transport: StratzRoshClient | None = None,
) -> int:
    configured = require_database_url(database_url)
    stopper = stop_event or threading.Event()
    now = clock or (lambda: datetime.now(UTC))
    client = transport or StratzRoshClient(stop_requested=stopper.is_set)
    while not stopper.is_set():
        observed = now().astimezone(UTC)
        try:
            with IntelligenceStorage(configured) as storage:
                report = run_collector_once(
                    ProspectiveRoshCollectorRepository(storage.connection),
                    client,
                    artifact_root=artifact_root,
                    now=observed,
                    limit=batch_size,
                    acceptance_limit=acceptance_limit,
                )
            print(json.dumps(asdict(report), sort_keys=True, default=str), flush=True)
            if report.acceptance_stopped:
                return 0
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "degraded",
                        "error": type(error).__name__,
                        "at": observed.isoformat(),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if stopper.wait(poll_seconds):
            break
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = require_database_url(args.database_url)
    if not args.schema_prepared:
        with IntelligenceStorage(database_url) as storage:
            storage.init_schema(seed_events=False)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    return run_worker(
        database_url,
        batch_size=args.batch_size,
        poll_seconds=args.poll_seconds,
        acceptance_limit=args.acceptance_limit,
        artifact_root=args.artifact_root,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    raise SystemExit(main())
