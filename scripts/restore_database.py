"""Restore a SQLite backup after taking a mandatory safety snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_offline_authority,
)
from live_betting.database_protocol import restore_database_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--safety-backup", type=Path, required=True)
    args = parser.parse_args()

    with database_offline_authority(
        args.database,
        allow_missing=not args.database.resolve().exists(),
        allow_replacement=True,
    ):
        saved = restore_database_backup(
            args.backup,
            args.database,
            safety_backup=args.safety_backup,
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "database": str(args.database.resolve()),
                "safety_backup": str(saved) if saved is not None else None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
