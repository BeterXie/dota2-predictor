"""Entry point for the data fetcher module.

Usage:
    python -m fetch.main [--force] [--match-id X]
"""

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml

from .client import OpenDotaClient
from .postgres_store import CoreMatchStore
from database.engine import advisory_lock

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg


async def discover_matches(
    client: OpenDotaClient,
    cfg: dict,
) -> set[int]:
    """Query OpenDota for match IDs from configured leagues and teams."""
    match_ids: set[int] = set()

    start_ts = None
    end_ts = None
    dr = cfg.get("date_range", {})
    if dr.get("start"):
        start_ts = int(datetime.fromisoformat(dr["start"]).replace(
            tzinfo=timezone.utc).timestamp())
    if dr.get("end"):
        end_ts = int(datetime.fromisoformat(dr["end"]).replace(
            tzinfo=timezone.utc).timestamp())

    for league_id in cfg.get("leagues") or []:
        logger.info("Discovering matches for league %d...", league_id)
        try:
            matches = await client.get_league_matches(league_id)
        except Exception:
            logger.exception("Failed to fetch matches for league %d", league_id)
            continue
        for m in matches:
            mid = m.get("match_id")
            if mid is None:
                continue
            if start_ts and m.get("start_time", 0) < start_ts:
                continue
            if end_ts and m.get("start_time", 0) > end_ts:
                continue
            match_ids.add(mid)
        logger.info("League %d: found %d matches in range.", league_id, len(matches))

    for team_id in cfg.get("teams") or []:
        logger.info("Discovering matches for team %d...", team_id)
        try:
            matches = await client.get_team_matches(team_id)
        except Exception:
            logger.exception("Failed to fetch matches for team %d", team_id)
            continue
        for m in matches:
            mid = m.get("match_id")
            if mid is None:
                continue
            if start_ts and m.get("start_time", 0) < start_ts:
                continue
            if end_ts and m.get("start_time", 0) > end_ts:
                continue
            match_ids.add(mid)
        logger.info("Team %d: found %d matches in range.", team_id, len(matches))

    for mid in cfg.get("specific_matches") or []:
        match_ids.add(mid)

    return match_ids


async def fetch_heroes(client: OpenDotaClient, db: CoreMatchStore) -> None:
    logger.info("Fetching hero list...")
    heroes = await client.get_heroes()
    db.insert_heroes(heroes)


async def fetch_matches(
    client: OpenDotaClient,
    db: CoreMatchStore,
    match_ids: set[int],
    force: bool,
) -> tuple[int, int]:
    """Fetch match details and insert into DB. Returns (fetched, skipped)."""
    todo = sorted(match_ids)
    fetched = 0
    skipped = 0

    for i, mid in enumerate(todo):
        if not force and db.is_fetched(mid):
            skipped += 1
            logger.debug("Match %d already fetched, skipping.", mid)
            continue

        logger.info("[%d/%d] Fetching match %d...", i + 1, len(todo), mid)
        try:
            match = await client.get_match(mid)
        except Exception:
            logger.exception("Failed to fetch match %d", mid)
            continue

        try:
            db.insert_match(match)
        except Exception:
            logger.exception("Failed to insert match %d", mid)
            continue

        fetched += 1

    return fetched, skipped


async def run(
    cfg: dict,
    force: bool,
    single_match_id: int | None,
    database_url: str | None = None,
) -> None:
    db = CoreMatchStore(database_url)

    rate_limit = int(os.environ.get("OPENDOTA_RATE_LIMIT", "50"))
    client = OpenDotaClient(rate_limit=rate_limit)

    try:
        with advisory_lock(db.engine, "fetch.main"):
            if db.hero_count() == 0:
                await fetch_heroes(client, db)

            if single_match_id is not None:
                match_ids = {single_match_id}
            else:
                match_ids = await discover_matches(client, cfg)

            if not match_ids:
                logger.warning("No matches discovered. Check config.yaml leagues/teams.")
                return

            logger.info("Discovered %d total match IDs to process.", len(match_ids))
            fetched, skipped = await fetch_matches(client, db, match_ids, force)
            logger.info(
                "Done: %d fetched, %d skipped (already present).",
                fetched,
                skipped,
            )
    finally:
        await client.close()
        db.close()


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Fetch Dota 2 match data from OpenDota")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if already in DB")
    parser.add_argument("--match-id", type=int, default=None, help="Fetch a single match by ID")
    parser.add_argument(
        "--database-url",
        action="append",
        default=None,
        help="PostgreSQL URL; defaults to DATABASE_URL",
    )
    args = parser.parse_args(argv)
    if args.database_url is not None and len(args.database_url) != 1:
        parser.error("--database-url may only be specified once")
    database_url = None if args.database_url is None else args.database_url[0]

    cfg = load_config()
    asyncio.run(run(cfg, args.force, args.match_id, database_url))


if __name__ == "__main__":
    main()
