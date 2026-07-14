"""Fetch hero meta statistics from OpenDota API and store in the database.

Pulls three data sources:
  1. /api/heroStats       — pro/pub/turbo pick/win rates for all heroes
  2. /api/heroes/{id}/durations — win rate by game duration (early vs late game)
  3. /api/benchmarks?hero_id={id} — performance benchmarks (GPM, XPM, damage, etc.)

Usage:
    python -m fetch.hero_meta [--force]
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

import yaml

from .client import OpenDotaClient
from .db import Database

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"

# Metrics from /api/benchmarks we care about for draft profile analysis
BENCHMARK_METRICS = [
    "gold_per_min",
    "xp_per_min",
    "kills_per_min",
    "last_hits_per_min",
    "hero_damage_per_min",
    "hero_healing_per_min",
    "tower_damage",
]


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_db_path(cfg: dict) -> str:
    raw: str = cfg.get("database", "../data/dota2.db")
    if os.path.isabs(raw):
        return raw
    return str((Path(__file__).parent / raw).resolve())


def compute_win_rate_curve(durations: list[dict]) -> dict[str, float]:
    """Compute early/mid/late game win rates from duration-binned data.

    Returns dict with early_wr, mid_wr, late_wr, and scaling_score.
    Duration bins are in seconds from the API.
    """
    bins = {}
    for d in durations:
        if d["games_played"] >= 10:
            mins = d["duration_bin"] // 60
            wr = d["wins"] / d["games_played"]
            if mins not in bins or d["games_played"] > 100:
                bins[mins] = wr

    def wr_in_range(lo, hi):
        ws = [wr for m, wr in bins.items() if lo <= m <= hi]
        return sum(ws) / len(ws) if ws else 0.5

    early = wr_in_range(15, 25)
    mid = wr_in_range(25, 40)
    late = wr_in_range(40, 60)

    return {
        "early_wr": round(early, 4),
        "mid_wr": round(mid, 4),
        "late_wr": round(late, 4),
        "scaling_score": round(late - early, 4),
    }


async def fetch_hero_stats(client: OpenDotaClient, db: Database) -> int:
    """Fetch and store hero general stats from /api/heroStats."""
    logger.info("Fetching /api/heroStats...")
    data = await client.get_hero_stats()
    if not data:
        logger.warning("No hero stats returned.")
        return 0

    # Update heroes table with pro/pub stats
    db.update_hero_stats(data)

    # Also compute and store overall win rate for each hero
    conn = db.connect()
    for hs in data:
        total_pick = (hs.get("1_pick", 0) + hs.get("2_pick", 0) + hs.get("3_pick", 0) +
                      hs.get("4_pick", 0) + hs.get("5_pick", 0) + hs.get("6_pick", 0) +
                      hs.get("7_pick", 0))
        total_win = (hs.get("1_win", 0) + hs.get("2_win", 0) + hs.get("3_win", 0) +
                     hs.get("4_win", 0) + hs.get("5_win", 0) + hs.get("6_win", 0) +
                     hs.get("7_win", 0))
        wr = total_win / total_pick if total_pick > 0 else 0.0
        conn.execute("UPDATE heroes SET win_rate = ? WHERE hero_id = ?", (wr, hs["id"]))
    conn.commit()

    logger.info("Updated %d heroes with stats from /api/heroStats.", len(data))
    return len(data)


async def fetch_hero_durations(
    client: OpenDotaClient, db: Database, hero_ids: list[int], force: bool = False
) -> tuple[int, int]:
    """Fetch and store hero win rate by game duration."""
    conn = db.connect()
    fetched = 0
    skipped = 0

    for i, hero_id in enumerate(hero_ids):
        if not force:
            existing = conn.execute(
                "SELECT COUNT(*) as cnt FROM hero_duration_stats WHERE hero_id = ?",
                (hero_id,),
            ).fetchone()
            if existing and existing[0] > 0:
                skipped += 1
                continue

        logger.info("[%d/%d] Fetching durations for hero %d...", i + 1, len(hero_ids), hero_id)
        try:
            data = await client._request(f"/api/heroes/{hero_id}/durations")
        except Exception:
            logger.exception("Failed to fetch durations for hero %d", hero_id)
            continue

        durations = [
            {"duration_min": d["duration_bin"] // 60, "games_played": d["games_played"],
             "wins": d["wins"]}
            for d in data
        ]
        db.insert_hero_duration_stats(hero_id, durations)
        fetched += 1

    return fetched, skipped


async def fetch_hero_benchmarks(
    client: OpenDotaClient, db: Database, hero_ids: list[int], force: bool = False
) -> tuple[int, int]:
    """Fetch and store hero performance benchmarks."""
    conn = db.connect()
    fetched = 0
    skipped = 0

    for i, hero_id in enumerate(hero_ids):
        if not force:
            existing = conn.execute(
                "SELECT COUNT(*) as cnt FROM hero_benchmarks WHERE hero_id = ?",
                (hero_id,),
            ).fetchone()
            if existing and existing[0] > 0:
                skipped += 1
                continue

        logger.info("[%d/%d] Fetching benchmarks for hero %d...", i + 1, len(hero_ids), hero_id)
        try:
            data = await client._request(f"/api/benchmarks?hero_id={hero_id}")
        except Exception:
            logger.exception("Failed to fetch benchmarks for hero %d", hero_id)
            continue

        if "result" in data:
            filtered = {m: data["result"][m] for m in BENCHMARK_METRICS if m in data["result"]}
            db.insert_hero_benchmarks(hero_id, filtered)
        fetched += 1

    return fetched, skipped


async def compute_scaling_scores(db: Database, hero_ids: list[int]) -> None:
    """Compute and store early/mid/late WR and scaling_score into a summary table."""
    conn = db.connect()

    # Ensure the summary columns exist in heroes table
    summary_cols = [
        "ALTER TABLE heroes ADD COLUMN early_wr REAL DEFAULT 0.5",
        "ALTER TABLE heroes ADD COLUMN mid_wr REAL DEFAULT 0.5",
        "ALTER TABLE heroes ADD COLUMN late_wr REAL DEFAULT 0.5",
        "ALTER TABLE heroes ADD COLUMN scaling_score REAL DEFAULT 0.0",
    ]
    for sql in summary_cols:
        try:
            conn.execute(sql)
        except Exception:
            pass

    for hero_id in hero_ids:
        rows = conn.execute(
            "SELECT duration_min, games_played, wins FROM hero_duration_stats WHERE hero_id = ?",
            (hero_id,),
        ).fetchall()
        if not rows:
            continue

        durations = [{"duration_bin": r[0] * 60, "games_played": r[1], "wins": r[2]} for r in rows]
        curve = compute_win_rate_curve(durations)

        conn.execute(
            "UPDATE heroes SET early_wr=?, mid_wr=?, late_wr=?, scaling_score=? WHERE hero_id=?",
            (curve["early_wr"], curve["mid_wr"], curve["late_wr"], curve["scaling_score"], hero_id),
        )

    conn.commit()
    logger.info("Computed scaling scores for %d heroes.", len(hero_ids))


async def run(cfg: dict, force: bool) -> None:
    db_path = resolve_db_path(cfg)
    db = Database(db_path)
    db.connect()
    db.init_db()

    rate_limit = int(os.environ.get("OPENDOTA_RATE_LIMIT", "50"))
    client = OpenDotaClient(rate_limit=rate_limit)

    try:
        # Step 1: Fetch hero stats (single API call for all heroes)
        await fetch_hero_stats(client, db)

        # Step 2: Get hero list from DB
        conn = db.connect()
        hero_rows = conn.execute("SELECT hero_id FROM heroes ORDER BY hero_id").fetchall()
        hero_ids = [r[0] for r in hero_rows]
        logger.info("Found %d heroes in database.", len(hero_ids))

        # Step 3: Fetch durations per hero
        d_fetched, d_skipped = await fetch_hero_durations(client, db, hero_ids, force)
        logger.info("Duration stats: %d fetched, %d skipped.", d_fetched, d_skipped)

        # Step 4: Fetch benchmarks per hero
        b_fetched, b_skipped = await fetch_hero_benchmarks(client, db, hero_ids, force)
        logger.info("Benchmarks: %d fetched, %d skipped.", b_fetched, b_skipped)

        # Step 5: Compute scaling scores
        await compute_scaling_scores(db, hero_ids)

        logger.info("Hero meta fetch complete.")

        # Summary
        stats = conn.execute(
            """SELECT COUNT(DISTINCT hero_id) as heroes,
                      COUNT(*) as total_durations,
                      COUNT(DISTINCT hero_id) as benchmarked_heroes
               FROM hero_duration_stats"""
        ).fetchone()
        bench_count = conn.execute(
            "SELECT COUNT(DISTINCT hero_id) FROM hero_benchmarks"
        ).fetchone()[0]
        logger.info(
            "Summary: %d heroes with duration data, %d with benchmarks.",
            stats[0], bench_count,
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
        description="Fetch hero meta statistics from OpenDota API"
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch even if already in DB"
    )
    args = parser.parse_args()

    cfg = load_config()
    asyncio.run(run(cfg, args.force))


if __name__ == "__main__":
    main()
