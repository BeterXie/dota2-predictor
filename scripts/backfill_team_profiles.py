"""Backfill recent detailed OpenDota matches for selected team IDs."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live_betting.service_coordination import (  # noqa: E402
    add_single_database_argument,
    database_writer_authority,
)
from fetch.client import OpenDotaClient  # noqa: E402
from fetch.db import Database  # noqa: E402


async def backfill(database: Path, team_ids: list[int], limit: int, rate_limit: int) -> dict:
    db = Database(str(database))
    db.connect()
    db.init_db()
    client = OpenDotaClient(rate_limit=rate_limit)
    fetched = skipped = failed = 0
    try:
        candidates: dict[int, int] = {}
        for team_id in team_ids:
            for row in await client.get_team_matches(team_id):
                if row.get("match_id") is not None:
                    candidates[int(row["match_id"])] = int(row.get("start_time") or 0)
        match_ids = [match_id for match_id, _ in sorted(
            candidates.items(), key=lambda item: item[1], reverse=True
        )[:limit]]
        for match_id in match_ids:
            if db.is_fetched(match_id):
                skipped += 1
                continue
            try:
                db.insert_match(await client.get_match(match_id))
                fetched += 1
            except Exception:
                logging.exception("failed to backfill match %d", match_id)
                failed += 1
    finally:
        await client.close()
        db.close()
    return {"fetched": fetched, "skipped": skipped, "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_single_database_argument(parser, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--team-id", type=int, action="append", required=True)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--rate-limit", type=int, default=50)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with database_writer_authority(args.database):
        result = asyncio.run(
            backfill(args.database, args.team_id, args.limit, args.rate_limit)
        )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
