"""Dry-run or apply the bounded retention policy for vision JPG evidence."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.vision_retention import (  # noqa: E402
    DEFAULT_MAX_UNPROTECTED_PER_MATCH,
    DEFAULT_RETENTION_TTL,
    RetentionSafetyError,
    prune_vision_evidence,
)


DEFAULT_EVIDENCE_DIR = ROOT / "data" / "live_betting" / "live_evidence"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=int(DEFAULT_RETENTION_TTL.total_seconds() // 86_400),
    )
    parser.add_argument(
        "--max-unprotected-per-match",
        type=int,
        default=DEFAULT_MAX_UNPROTECTED_PER_MATCH,
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="apply the plan; omission is always a read-only dry run",
    )
    args = parser.parse_args(argv)
    if args.ttl_days <= 0:
        parser.error("--ttl-days must be positive")
    if args.max_unprotected_per_match < 0:
        parser.error("--max-unprotected-per-match cannot be negative")
    try:
        result = prune_vision_evidence(
            args.database,
            args.evidence_dir,
            ttl=timedelta(days=args.ttl_days),
            max_unprotected_per_match=args.max_unprotected_per_match,
            dry_run=not args.delete,
        )
    except (OSError, sqlite3.Error, RetentionSafetyError, ValueError) as error:
        print(json.dumps({
            "status": "error",
            "error_type": type(error).__name__,
            "dry_run": not args.delete,
        }, sort_keys=True))
        return 2
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 1 if result.unsafe_paths or result.delete_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
