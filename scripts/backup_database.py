"""Create a consistent operator backup of the project SQLite database."""

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
from live_betting.database_protocol import (  # noqa: E402
    check_schema_versions,
    online_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with database_offline_authority(args.database):
        versions = check_schema_versions(args.database)
        online_backup(args.database, args.output)
        if check_schema_versions(args.output) != versions:
            raise RuntimeError("backup schema versions changed during snapshot")
    print(
        json.dumps(
            {
                "status": "ok",
                "backup": str(args.output.resolve()),
                "live_schema_version": versions[0],
                "intelligence_schema_version": versions[1],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
