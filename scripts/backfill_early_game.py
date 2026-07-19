"""Backfill early-game stats for existing matches.

Re-fetches match detail from OpenDota to extract time-series data:
  - gold_t, lh_t, xp_t → 10-min snapshots
  - kills_log, killed_by → early KDA
  - obs_log, sen_log → early ward placements
  - lane_efficiency, lane_role, is_roaming, kda

Usage:
    python scripts/backfill_early_game.py [--limit N]
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetch.client import OpenDotaClient
from fetch.parser import parse_players
from fetch.db import Database
from live_betting.service_coordination import (
    add_single_database_argument,
    database_writer_authority,
)
from shared.sqlite import connect as connect_sqlite

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "fetch" / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_db_path(cfg: dict) -> str:
    raw: str = cfg.get("database", "../data/dota2.db")
    if os.path.isabs(raw):
        return raw
    # Config is relative to fetch/ directory
    config_dir = CONFIG_PATH.parent
    return str((config_dir / raw).resolve())


def get_pending_matches(db_path: str, limit: int | None = None) -> list[int]:
    """Get match IDs that need early-game backfill."""
    conn = connect_sqlite(db_path, read_only=True)
    rows = conn.execute(
        """SELECT match_id FROM matches
           WHERE EXISTS (SELECT 1 FROM match_players mp WHERE mp.match_id = matches.match_id)
           ORDER BY match_id"""
    ).fetchall()
    conn.close()

    match_ids = [r[0] for r in rows]
    logger.info("Found %d total matches.", len(match_ids))

    if limit:
        match_ids = match_ids[:limit]
        logger.info("Limited to %d matches.", limit)

    return match_ids


def update_player_early_game(db_path: str, match_id: int, players: list[dict]) -> int:
    """Update existing match_players rows with early-game fields."""
    conn = connect_sqlite(db_path)
    updated = 0
    for p in players:
        conn.execute(
            """UPDATE match_players SET
                gold_10min = ?, lh_10min = ?, xp_10min = ?,
                kills_10min = ?, deaths_10min = ?, assists_10min = ?,
                obs_placed_10min = ?, sen_placed_10min = ?,
                lane_efficiency = ?, lane_role = ?, is_roaming = ?, kda = ?,
                observer_kills_10min = ?, sentry_kills_10min = ?
               WHERE match_id = ? AND player_slot = ?""",
            (
                p.get("gold_10min"), p.get("lh_10min"), p.get("xp_10min"),
                p.get("kills_10min"), p.get("deaths_10min"), p.get("assists_10min"),
                p.get("obs_placed_10min"), p.get("sen_placed_10min"),
                p.get("lane_efficiency"), p.get("lane_role"), p.get("is_roaming"), p.get("kda"),
                p.get("observer_kills_10min"), p.get("sentry_kills_10min"),
                match_id, p["player_slot"],
            ),
        )
        updated += conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    conn.close()
    return updated


async def backfill(
    cfg: dict,
    limit: int | None = None,
    database_path: str | Path | None = None,
) -> None:
    db_path = str(Path(database_path).resolve()) if database_path else resolve_db_path(cfg)
    db = Database(db_path)
    db.connect()
    db.init_db()

    match_ids = get_pending_matches(db_path, limit)
    if not match_ids:
        logger.info("No matches to backfill.")
        return

    rate_limit = int(os.environ.get("OPENDOTA_RATE_LIMIT", "5"))
    client = OpenDotaClient(rate_limit=rate_limit)

    success = 0
    fail = 0
    total = len(match_ids)

    for i, mid in enumerate(match_ids):
        logger.info("[%d/%d] Fetching match %d...", i + 1, total, mid)
        try:
            data = await client.get_match(mid)
            if not data or "players" not in data:
                logger.warning("Match %d: no player data in response.", mid)
                fail += 1
                continue

            players = parse_players(data)
            updated = update_player_early_game(db_path, mid, players)

            # Summary of what we got
            sample = players[0] if players else {}
            has_10min = sample.get("gold_10min") is not None
            has_wards = sample.get("obs_placed_10min", 0) > 0 or sample.get("sen_placed_10min", 0) > 0
            logger.info(
                "Match %d: updated %d players (10min_data=%s, wards=%s).",
                mid, updated, has_10min, has_wards,
            )
            success += 1

        except Exception:
            logger.exception("Failed to process match %d.", mid)
            fail += 1

    await client.close()
    db.close()

    logger.info("Backfill complete: %d success, %d failed.", success, fail)

    # Stats
    conn = connect_sqlite(db_path, read_only=True)
    total_players = conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0]
    with_10min = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE gold_10min IS NOT NULL"
    ).fetchone()[0]
    with_wards = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE obs_placed_10min > 0 OR sen_placed_10min > 0"
    ).fetchone()[0]
    logger.info(
        "DB stats: %d/%d players have 10-min data, %d have ward data.",
        with_10min, total_players, with_wards,
    )
    conn.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Backfill early-game stats from OpenDota match details"
    )
    add_single_database_argument(parser)
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of matches to process")
    args = parser.parse_args()

    cfg = load_config()
    database = Path(args.database or resolve_db_path(cfg)).resolve()
    with database_writer_authority(database):
        asyncio.run(backfill(cfg, args.limit, database))


if __name__ == "__main__":
    main()
