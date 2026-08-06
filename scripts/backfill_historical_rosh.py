"""Backfill Rosh lineup scores for approved T1/T2 historical maps."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from event_intelligence.historical_rosh_backfill import (  # noqa: E402
    backfill_historical_rosh_scores,
    existing_historical_rosh_score_for_identity,
    historical_rosh_score_is_complete,
    persist_historical_rosh_score,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from live_betting.stratz_rosh_client import StratzRoshClient  # noqa: E402


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--match-id", type=_positive_int, help="one formal match ID")
    parser.add_argument("--limit", type=_positive_int, help="maximum maps to process")
    parser.add_argument(
        "--pure-only",
        action="store_true",
        help="skip current STRATZ player-hero profile requests",
    )
    parser.add_argument("--max-attempts", type=_positive_int, default=3)
    parser.add_argument(
        "--initial-backoff-seconds", type=_non_negative_float, default=1.0
    )
    parser.add_argument("--throttle-seconds", type=_non_negative_float, default=0.25)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = require_database_url(args.database_url)
    with IntelligenceStorage(database_url) as storage:
        storage.init_schema()

    include_player_adjustment = not args.pure_only

    def persist_score(
        _read_storage: object,
        match_id: int,
        identity: dict[str, Sequence[int]],
        score: object,
        created_at: object,
    ) -> bool:
        with IntelligenceStorage(database_url) as writer:
            existing = existing_historical_rosh_score_for_identity(
                writer.connection,
                match_id=match_id,
                formula_version=score.formula_version,
                identity=identity,
            )
            if historical_rosh_score_is_complete(
                existing,
                include_current_player_adjustment=include_player_adjustment,
            ):
                return False
            return persist_historical_rosh_score(
                writer,
                match_id,
                identity,
                score,
                created_at,
            )

    with IntelligenceStorage(database_url) as read_storage:
        report = backfill_historical_rosh_scores(
            read_storage,
            StratzRoshClient(),
            match_id=args.match_id,
            limit=args.limit,
            include_current_player_adjustment=include_player_adjustment,
            persist_score=persist_score,
            max_attempts=args.max_attempts,
            initial_backoff_seconds=args.initial_backoff_seconds,
            throttle_seconds=args.throttle_seconds,
        )
    print(json.dumps({"database": "postgresql", **asdict(report)}, sort_keys=True))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
