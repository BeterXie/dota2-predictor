"""Fetch hero-vs-hero matchup data from Stratz GraphQL API.

Uses the heroVsHeroMatchup query which returns pre-computed advantage/disadvantage
data with synergy scores. Sample sizes are ~13x larger than OpenDota Explorer.

Requires curl_cffi for TLS fingerprinting to bypass Cloudflare.

Usage:
    python -m fetch.fetch_stratz_matchups [--force] [--token TOKEN]
"""

import argparse
import logging
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import yaml
from curl_cffi import requests as cffi_requests

from .db import Database
from live_betting.service_coordination import (
    add_single_database_argument,
    database_writer_authority,
)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"

MATCHUP_QUERY = """
query HeroMatchups($heroId: Short!) {
  heroStats {
    heroVsHeroMatchup(heroId: $heroId) {
      advantage {
        heroId
        vs {
          heroId2
          matchCount
          winCount
          synergy
          winsAverage
        }
      }
      disadvantage {
        heroId
        vs {
          heroId2
          matchCount
          winCount
          synergy
          winsAverage
        }
      }
    }
  }
}
"""


def resolve_stratz_token(environment: Mapping[str, str] | None = None) -> str | None:
    """Resolve the preferred STRATZ credential with legacy fallback."""
    env = os.environ if environment is None else environment
    primary = str(env.get("STRATZ_API_TOKEN", "")).strip()
    if primary:
        return primary
    legacy = str(env.get("STRATZ_TOKEN", "")).strip()
    return legacy or None


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve_db_path(cfg: dict) -> str:
    raw: str = cfg.get("database", "../data/dota2.db")
    if os.path.isabs(raw):
        return raw
    return str((Path(__file__).parent / raw).resolve())


def _flatten_matchups(hero_id: int, data: dict) -> list[dict]:
    """Convert Stratz heroVsHeroMatchup response to DB row format.

    Combines advantage and disadvantage lists, each containing a 'vs' array
    of per-opponent stats with synergy scores.
    """
    matchups = data.get("data", {}).get("heroStats", {}).get("heroVsHeroMatchup", {})
    results: dict[int, dict] = {}

    for section in ("advantage", "disadvantage"):
        for entry in matchups.get(section, []):
            for opp in entry.get("vs", []):
                vs_id = opp["heroId2"]
                if vs_id == hero_id:
                    continue
                results[vs_id] = {
                    "hero_id": vs_id,
                    "games_played": opp["matchCount"],
                    "wins": opp["winCount"],
                    "synergy": opp["synergy"],
                }

    return list(results.values())


def fetch_matchups_stratz(
    db: Database,
    token: str,
    force: bool = False,
) -> tuple[int, int]:
    """Fetch hero matchup data from Stratz for all heroes."""
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
                "SELECT COUNT(*) as cnt FROM hero_matchups WHERE hero_id = ? AND synergy IS NOT NULL",
                (hero_id,),
            ).fetchone()
            if existing and existing["cnt"] > 0:
                skipped += 1
                logger.debug(
                    "[%d/%d] Hero %d (%s): already have Stratz data, skipping.",
                    i + 1, total, hero_id, name,
                )
                continue

        logger.info(
            "[%d/%d] Fetching Stratz matchups for hero %d (%s)...",
            i + 1, total, hero_id, name,
        )
        try:
            r = cffi_requests.post(
                "https://api.stratz.com/graphql",
                json={
                    "query": MATCHUP_QUERY,
                    "variables": {"heroId": hero_id},
                },
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
                impersonate="chrome120",
                timeout=30,
            )
            if r.status_code != 200:
                logger.error(
                    "Stratz API error for hero %d: status=%d", hero_id, r.status_code,
                )
                continue

            data = r.json()
            if "errors" in data:
                logger.error(
                    "Stratz GraphQL error for hero %d: %s",
                    hero_id, data["errors"],
                )
                continue

        except Exception:
            logger.exception(
                "Failed to fetch Stratz matchups for hero %d (%s)", hero_id, name,
            )
            continue

        matchups = _flatten_matchups(hero_id, data)
        if not matchups:
            logger.warning("Hero %d (%s): no matchup data from Stratz.", hero_id, name)
            continue

        try:
            db.insert_hero_matchups(hero_id, matchups)
        except Exception:
            logger.exception(
                "Failed to insert Stratz matchups for hero %d (%s)", hero_id, name,
            )
            continue

        fetched += 1

    return fetched, skipped


def run(
    token: str,
    force: bool,
    database_path: str | Path | None = None,
) -> None:
    cfg = load_config()
    db_path = str(Path(database_path).resolve()) if database_path else resolve_db_path(cfg)
    db = Database(db_path)
    db.connect()
    db.init_db()

    try:
        fetched, skipped = fetch_matchups_stratz(db, token, force)
        logger.info(
            "Done: %d heroes' Stratz matchups fetched, %d skipped.",
            fetched, skipped,
        )
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Fetch hero matchup data from Stratz GraphQL API"
    )
    add_single_database_argument(parser)
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch even if already in DB"
    )
    parser.add_argument(
        "--token",
        type=str,
        default=resolve_stratz_token(),
        help=(
            "Stratz API Bearer token (or set STRATZ_API_TOKEN; "
            "STRATZ_TOKEN is deprecated)"
        ),
    )
    args = parser.parse_args()

    if not args.token:
        parser.error(
            "Stratz token required. Pass --token or set STRATZ_API_TOKEN "
            "(STRATZ_TOKEN is deprecated)."
        )

    cfg = load_config()
    database = Path(args.database or resolve_db_path(cfg)).resolve()
    with database_writer_authority(database):
        run(args.token, args.force, database)


if __name__ == "__main__":
    main()
