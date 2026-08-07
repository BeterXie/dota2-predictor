"""Collect prospective R.O.S.H. evidence and immutable P0/P1 shadow rows."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Sequence


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


class _DryRunTransport:
    def fetch_legacy_lineup_batch(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dry-run must not access STRATZ")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _acceptance_limit(value: str) -> int:
    parsed = int(value)
    if not MIN_ACCEPTANCE_MAPS <= parsed <= MAX_ACCEPTANCE_MAPS:
        raise argparse.ArgumentTypeError("must be between 5 and 10")
    return parsed


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", help="PostgreSQL URL (default: DATABASE_URL)")
    parser.add_argument("--match-id", type=_positive_int)
    parser.add_argument("--scan-start", type=_timestamp)
    parser.add_argument("--scan-end", type=_timestamp)
    parser.add_argument("--limit", type=_positive_int, default=5)
    parser.add_argument(
        "--acceptance-limit",
        type=_acceptance_limit,
        default=MAX_ACCEPTANCE_MAPS,
        help="hard stop after 5-10 real future maps",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("dogfood-output") / "prospective-rosh-artifacts",
    )
    parser.add_argument("--json-output", type=Path)
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(UTC)
    transport = _DryRunTransport() if args.dry_run else StratzRoshClient()
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema(seed_events=False)
        report = run_collector_once(
            ProspectiveRoshCollectorRepository(storage.connection),
            transport,
            artifact_root=args.artifact_root,
            now=now,
            match_id=args.match_id,
            scan_start=args.scan_start,
            scan_end=args.scan_end,
            limit=args.limit,
            acceptance_limit=args.acceptance_limit,
            dry_run=args.dry_run,
        )
    encoded = json.dumps(asdict(report), ensure_ascii=True, sort_keys=True, default=str)
    if args.json_output is not None:
        _write(args.json_output, encoded + "\n")
    print(encoded)
    return 0 if report.terminal_failure == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
