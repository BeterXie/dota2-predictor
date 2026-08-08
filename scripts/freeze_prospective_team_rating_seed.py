"""Freeze the accepted Team Rating state used by live prospective predictions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from database.session import PostgresSession  # noqa: E402
from event_intelligence.prospective_team_rating import (  # noqa: E402
    ProspectiveTeamRatingRepository,
    build_prospective_team_rating_seed,
)
from event_intelligence.raw_archive import canonical_json_bytes  # noqa: E402
from event_intelligence.storage import IntelligenceStorage  # noqa: E402
from event_intelligence.team_rating import TeamRatingConfig  # noqa: E402


UTC = timezone.utc
ACCEPTED_CONFIG_PATH = (
    ROOT / "event_intelligence" / "resources" / "team_rating_accepted_config_v1.json"
)
ACCEPTED_CONFIGURATION_HASH = (
    "b527319ab1035d6cae6550820cd0854b467f845537d033909b4f2e45e706c19a"
)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def load_accepted_config() -> TeamRatingConfig:
    try:
        payload = json.loads(ACCEPTED_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("accepted Team Rating config is unavailable") from error
    if not isinstance(payload, dict):
        raise ValueError("accepted Team Rating config must be an object")
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if digest != ACCEPTED_CONFIGURATION_HASH:
        raise ValueError("accepted Team Rating config hash mismatch")
    return TeamRatingConfig(**payload)


def freeze_seed(
    connection: PostgresSession,
    *,
    seed_cutoff: datetime,
    frozen_at: datetime,
    dry_run: bool = False,
) -> dict[str, object]:
    cutoff = _utc(seed_cutoff, "seed_cutoff")
    frozen = _utc(frozen_at, "frozen_at")
    if cutoff > frozen:
        raise ValueError("seed cutoff cannot follow frozen_at")
    repository = ProspectiveTeamRatingRepository(connection)
    source_results = repository.load_seed_results(
        training_cutoff=cutoff,
        observed_at=frozen,
    )
    seed = build_prospective_team_rating_seed(
        config=load_accepted_config(),
        source_results=source_results,
        seed_as_of=cutoff,
        seed_training_cutoff=cutoff,
        frozen_at=frozen,
    )
    inserted = repository.store_seed(seed, dry_run=dry_run)
    return {
        "status": "dry_run" if dry_run else "stored" if inserted else "unchanged",
        "seed_hash": seed.seed_hash,
        "configuration_hash": seed.configuration_hash,
        "support": len(seed.source_manifest),
        "inserted": inserted,
        "dry_run": dry_run,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--seed-cutoff", required=True, type=_timestamp)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    frozen_at = datetime.now(UTC)
    if args.seed_cutoff > frozen_at:
        parser.error("--seed-cutoff cannot be in the future")
    with IntelligenceStorage(require_database_url(args.database_url)) as storage:
        storage.init_schema(seed_events=False)
        result = freeze_seed(
            storage.connection,
            seed_cutoff=args.seed_cutoff,
            frozen_at=frozen_at,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
