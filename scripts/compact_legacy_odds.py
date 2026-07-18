"""Compact a database at the current live schema into an immutable output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.odds_legacy_compactor import (  # noqa: E402
    compact_legacy_odds,
    result_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a matching failed checkpoint under destination-root",
    )
    args = parser.parse_args()
    result = compact_legacy_odds(
        args.database,
        args.raw_root,
        args.destination_root,
        resume=args.resume,
    )
    print(result_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
