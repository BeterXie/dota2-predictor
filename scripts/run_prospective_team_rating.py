"""Produce immutable prospective Team Rating P0 predictions for formal maps."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from event_intelligence.prospective_team_rating import (  # noqa: E402
    ProspectiveTeamRatingRepository,
    build_prospective_team_rating_seed,
    run_producer_once,
)
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from event_intelligence.team_rating import TeamRatingConfig  # noqa: E402


UTC = timezone.utc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _config(path: Path) -> TeamRatingConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("seed config must be a readable JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError("seed config must be a JSON object")
    return TeamRatingConfig(
        initial_rating=payload["initial_rating"],
        scale=payload["scale"],
        k_factor=payload["k_factor"],
        inactivity_half_life_days=payload["inactivity_half_life_days"],
        roster_carry_power=payload["roster_carry_power"],
        radiant_side_logit=payload["radiant_side_logit"],
        config_version=payload["config_version"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--match-id", type=_positive_int)
    parser.add_argument("--scan-start", type=_timestamp)
    parser.add_argument("--scan-end", type=_timestamp)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("dogfood-output") / "prospective-team-rating-artifacts",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--freeze-seed-config",
        type=Path,
        help="one-time frozen Team Rating config JSON used to create a seed",
    )
    parser.add_argument(
        "--seed-cutoff",
        type=_timestamp,
        help="historical state cutoff for --freeze-seed-config",
    )
    return parser


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.freeze_seed_config is None) != (args.seed_cutoff is None):
        parser.error("--freeze-seed-config and --seed-cutoff must be used together")
    now = datetime.now(UTC)
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema(seed_events=False)
        repository = ProspectiveTeamRatingRepository(storage.connection)
        seed_payload = None
        if args.freeze_seed_config is not None:
            if args.seed_cutoff > now:
                parser.error("--seed-cutoff cannot be in the future")
            source_results = repository.load_seed_results(
                training_cutoff=args.seed_cutoff,
                observed_at=now,
            )
            seed = build_prospective_team_rating_seed(
                config=_config(args.freeze_seed_config),
                source_results=source_results,
                seed_as_of=args.seed_cutoff,
                seed_training_cutoff=args.seed_cutoff,
                frozen_at=now,
            )
            inserted = repository.store_seed(seed, dry_run=args.dry_run)
            seed_payload = {
                "seed_hash": seed.seed_hash,
                "support": len(seed.source_manifest),
                "inserted": inserted,
                "dry_run": args.dry_run,
            }
        report = run_producer_once(
            repository,
            now=now,
            match_id=args.match_id,
            scan_start=args.scan_start,
            scan_end=args.scan_end,
            limit=args.limit,
            dry_run=args.dry_run,
            artifact_root=args.artifact_root,
        )
    payload = {"seed": seed_payload, "production": asdict(report)}
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    if args.json_output is not None:
        _write(args.json_output, encoded + "\n")
    print(encoded)
    return 0 if report.failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
