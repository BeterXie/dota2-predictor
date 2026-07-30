"""Atomically accept exact RayBet-to-canonical mappings for one BO series."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.engine import require_database_url  # noqa: E402
from live_betting.storage import LiveBettingStore  # noqa: E402
from live_betting.strict_eligibility import (  # noqa: E402
    StrictMappingError,
    accept_strict_live_map_mapping,
    init_strict_live_eligibility_schema,
)


class EvidenceFileError(ValueError):
    """Raised when the evidence file cannot supply one JSON object."""


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_empty(value: str) -> str:
    parsed = value.strip()
    if not parsed:
        raise argparse.ArgumentTypeError("must be non-empty")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--raybet-match-id", type=_non_empty, required=True)
    parser.add_argument("--raybet-team-one-id", type=_positive_integer, required=True)
    parser.add_argument("--raybet-team-two-id", type=_positive_integer, required=True)
    parser.add_argument(
        "--canonical-team-one-id", type=_positive_integer, required=True
    )
    parser.add_argument(
        "--canonical-team-two-id", type=_positive_integer, required=True
    )
    parser.add_argument("--event-id", type=_non_empty, required=True)
    parser.add_argument("--source", type=_non_empty, required=True)
    parser.add_argument("--actor", type=_non_empty, required=True)
    parser.add_argument(
        "--map-number",
        action="append",
        type=_positive_integer,
        required=True,
        help="repeat once for each map in the BO series",
    )
    return parser


def load_evidence(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise EvidenceFileError("evidence_file_unreadable") from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceFileError("evidence_json_invalid") from error
    if not isinstance(value, dict) or not value:
        raise EvidenceFileError("evidence_must_be_non_empty_object")
    return value


def accept_batch(
    *,
    database_url: str,
    evidence: Mapping[str, Any],
    raybet_match_id: str,
    raybet_team_one_id: int,
    raybet_team_two_id: int,
    canonical_team_one_id: int,
    canonical_team_two_id: int,
    event_id: str,
    source: str,
    actor: str,
    map_numbers: Sequence[int],
) -> dict[str, object]:
    accepted_at = datetime.now(timezone.utc)
    with LiveBettingStore(database_url) as store:
        connection = store.connection
        init_strict_live_eligibility_schema(connection)
        with store.transaction():
            mappings = [
                accept_strict_live_map_mapping(
                    connection,
                    raybet_match_id=raybet_match_id,
                    map_number=map_number,
                    event_id=event_id,
                    team_one_id=raybet_team_one_id,
                    team_two_id=raybet_team_two_id,
                    canonical_team_one_id=canonical_team_one_id,
                    canonical_team_two_id=canonical_team_two_id,
                    source=source,
                    evidence=evidence,
                    accepted_by=actor,
                    accepted_at=accepted_at,
                )
                for map_number in map_numbers
            ]
    return {
        "status": "ok",
        "map_numbers": [mapping.map_number for mapping in mappings],
        "mapping_ids": [mapping.mapping_id for mapping in mappings],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = load_evidence(args.evidence)
        result = accept_batch(
            database_url=require_database_url(args.database_url),
            evidence=evidence,
            raybet_match_id=args.raybet_match_id,
            raybet_team_one_id=args.raybet_team_one_id,
            raybet_team_two_id=args.raybet_team_two_id,
            canonical_team_one_id=args.canonical_team_one_id,
            canonical_team_two_id=args.canonical_team_two_id,
            event_id=args.event_id,
            source=args.source,
            actor=args.actor,
            map_numbers=args.map_number,
        )
    except (EvidenceFileError, StrictMappingError) as error:
        print(
            json.dumps(
                {"status": "rejected", "reason": str(error)},
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
