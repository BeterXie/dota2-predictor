"""Fetch hero-vs-hero matchup data from OpenDota and store in the database.

Uses the OpenDota Explorer SQL API to query the full public dataset, which
provides ~11x more matchup data than the /heroes/{id}/matchups endpoint.

Usage:
    python -m fetch.fetch_matchups [--force] [--source explorer|matchups]
"""

import argparse
import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

import yaml

from .client import OpenDotaClient
from .db import Database
from live_betting.service_coordination import (
    add_single_database_argument,
    database_writer_authority,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_db_path(cfg: dict) -> str:
    raw: str = cfg.get("database", "../data/dota2.db")
    if os.path.isabs(raw):
        return raw
    return str((Path(__file__).parent / raw).resolve())


_HERO_MATCHUP_SQL = """SELECT
    p2.hero_id as vs_hero_id,
    COUNT(*) as games_played,
    SUM(CASE
        WHEN (p1.player_slot < 128 AND m.radiant_win)
          OR (p1.player_slot >= 128 AND NOT m.radiant_win)
        THEN 1 ELSE 0
    END) as wins
FROM matches m
JOIN player_matches p1 ON m.match_id = p1.match_id
JOIN player_matches p2 ON m.match_id = p2.match_id
WHERE p1.hero_id = {hero_id}
  AND p2.hero_id != {hero_id}
  AND ((p1.player_slot < 128) != (p2.player_slot < 128))
GROUP BY p2.hero_id
ORDER BY p2.hero_id"""


def _parse_explorer_response(data: dict) -> list[dict]:
    """Convert Explorer API rows into the format expected by db.insert_hero_matchups."""
    return [
        {
            "hero_id": int(row["vs_hero_id"]),
            "games_played": int(row["games_played"]),
            "wins": int(row["wins"]),
        }
        for row in data.get("rows", [])
    ]


async def fetch_matchups_explorer(
    client: OpenDotaClient,
    db: Database,
    force: bool = False,
) -> tuple[int, int]:
    """Fetch hero matchup data using the Explorer SQL API (full dataset).

    One SQL query per hero, each returning ~126 opponent rows with game counts
    from the public dataset (typically 500+ games per opponent pair).
    """
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    heroes = conn.execute(
        "SELECT hero_id, localized_name FROM heroes ORDER BY hero_id"
    ).fetchall()

    if not heroes:
        logger.error("No heroes in database. Run fetch first.")
        return 0, 0

    fetched = 0
    skipped = 0
    total = len(heroes)

    for i, row in enumerate(heroes):
        hero_id = row["hero_id"]
        name = row["localized_name"]

        if not force:
            existing = conn.execute(
                "SELECT COUNT(*) as cnt FROM hero_matchups WHERE hero_id = ?",
                (hero_id,),
            ).fetchone()
            if existing and existing["cnt"] > 0:
                skipped += 1
                logger.debug(
                    "[%d/%d] Hero %d (%s): already have %d matchups, skipping.",
                    i + 1, total, hero_id, name, existing["cnt"],
                )
                continue

        logger.info(
            "[%d/%d] Fetching matchups for hero %d (%s) via Explorer SQL...",
            i + 1, total, hero_id, name,
        )
        try:
            sql = _HERO_MATCHUP_SQL.format(hero_id=hero_id)
            encoded = quote(sql, safe="")
            data = await client._request(f"/api/explorer?sql={encoded}")
            matchups = _parse_explorer_response(data)
        except Exception:
            logger.exception(
                "Failed to fetch matchups for hero %d (%s)", hero_id, name
            )
            continue

        if not matchups:
            logger.warning("Hero %d (%s): no matchup data returned.", hero_id, name)
            continue

        try:
            db.insert_hero_matchups(hero_id, matchups)
        except Exception:
            logger.exception(
                "Failed to insert matchups for hero %d (%s)", hero_id, name
            )
            continue

        fetched += 1

    return fetched, skipped


async def fetch_matchups_endpoint(
    client: OpenDotaClient,
    db: Database,
    force: bool = False,
) -> tuple[int, int]:
    """Fetch hero matchup data using the /heroes/{id}/matchups endpoint.

    Faster but returns ~10x less data than the Explorer approach (~44 games
    per opponent pair vs ~500).
    """
    conn = db.connect()
    conn.row_factory = sqlite3.Row
    heroes = conn.execute(
        "SELECT hero_id, localized_name FROM heroes ORDER BY hero_id"
    ).fetchall()

    if not heroes:
        logger.error("No heroes in database. Run fetch first.")
        return 0, 0

    fetched = 0
    skipped = 0
    total = len(heroes)

    for i, row in enumerate(heroes):
        hero_id = row["hero_id"]
        name = row["localized_name"]

        if not force:
            existing = conn.execute(
                "SELECT COUNT(*) as cnt FROM hero_matchups WHERE hero_id = ?",
                (hero_id,),
            ).fetchone()
            if existing and existing["cnt"] > 0:
                skipped += 1
                logger.debug(
                    "[%d/%d] Hero %d (%s): already have %d matchups, skipping.",
                    i + 1, total, hero_id, name, existing["cnt"],
                )
                continue

        logger.info(
            "[%d/%d] Fetching matchups for hero %d (%s) via endpoint...",
            i + 1, total, hero_id, name,
        )
        try:
            matchups = await client.get_hero_matchups(hero_id)
        except Exception:
            logger.exception(
                "Failed to fetch matchups for hero %d (%s)", hero_id, name
            )
            continue

        try:
            db.insert_hero_matchups(hero_id, matchups)
        except Exception:
            logger.exception(
                "Failed to insert matchups for hero %d (%s)", hero_id, name
            )
            continue

        fetched += 1

    return fetched, skipped


async def run(
    cfg: dict,
    force: bool,
    source: str,
    database_path: str | Path | None = None,
) -> None:
    db_path = str(Path(database_path).resolve()) if database_path else resolve_db_path(cfg)
    db = Database(db_path)
    db.connect()
    db.init_db()

    rate_limit = int(os.environ.get("OPENDOTA_RATE_LIMIT", "50"))
    client = OpenDotaClient(rate_limit=rate_limit)

    try:
        if source == "explorer":
            fetched, skipped = await fetch_matchups_explorer(client, db, force)
        else:
            fetched, skipped = await fetch_matchups_endpoint(client, db, force)

        logger.info(
            "Done: %d heroes' matchups fetched, %d skipped (already present).",
            fetched, skipped,
        )
    finally:
        await client.close()
        db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Fetch hero matchup data from OpenDota"
    )
    add_single_database_argument(parser)
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch even if already in DB"
    )
    parser.add_argument(
        "--source",
        choices=["explorer", "matchups"],
        default="explorer",
        help="Data source: explorer (full SQL dataset, ~500 games/pair) or "
        "matchups (endpoint, ~44 games/pair). Default: explorer.",
    )
    args = parser.parse_args()

    cfg = load_config()
    database = Path(args.database or resolve_db_path(cfg)).resolve()
    with database_writer_authority(database):
        asyncio.run(run(cfg, args.force, args.source, database))


if __name__ == "__main__":
    main()
