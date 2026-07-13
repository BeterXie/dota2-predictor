"""Match completed maps to OpenDota by exact draft and settle shadow orders."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch.client import OpenDotaClient
from fetch.db import Database

from .models import Market
from .settlement import MapResult, settle
from .storage import LiveBettingStore


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StoredMapResult:
    raybet_match_id: str
    map_number: int
    dota_match_id: int
    winner_side: str
    team_one_kills: int
    team_two_kills: int
    duration_seconds: int
    evidence_ref: str
    settled_at: datetime


def _scheduled_timestamp(value: str | None) -> int:
    if not value:
        return 0
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(parsed.timestamp())


def _vision_drafts(connection: sqlite3.Connection, match_id: str) -> dict[int, set[frozenset[int]]]:
    output: dict[int, set[frozenset[int]]] = {}
    rows = connection.execute(
        """SELECT map_number, radiant_hero_ids, dire_hero_ids
           FROM vision_observations
           WHERE raybet_match_id=? AND confirmed=1 AND map_number IS NOT NULL""",
        (match_id,),
    )
    for map_number, radiant, dire in rows:
        heroes = frozenset(json.loads(radiant) + json.loads(dire))
        if len(heroes) == 10:
            output.setdefault(int(map_number), set()).add(heroes)
    return output


def _winner(detail: dict, team_id: int, team_side: str) -> tuple[str, int, int]:
    target_radiant = int(detail.get("radiant_team_id") or 0) == team_id
    target_won = bool(detail.get("radiant_win")) == target_radiant
    winner_side = team_side if target_won else (
        "team_two" if team_side == "team_one" else "team_one"
    )
    radiant_score = int(detail.get("radiant_score") or 0)
    dire_score = int(detail.get("dire_score") or 0)
    target_kills = radiant_score if target_radiant else dire_score
    opponent_kills = dire_score if target_radiant else radiant_score
    return winner_side, target_kills, opponent_kills


def _settle_winner_orders(store: LiveBettingStore, result: StoredMapResult) -> int:
    rows = store.connection.execute(
        """SELECT o.order_key, o.market_key, o.fill_price
           FROM shadow_orders o JOIN shadow_map_attempts a ON a.order_key=o.order_key
           LEFT JOIN settlements s ON s.order_key=o.order_key
           WHERE a.raybet_match_id=? AND a.map_number=?
             AND o.status='filled' AND s.order_key IS NULL""",
        (result.raybet_match_id, result.map_number),
    ).fetchall()
    count = 0
    for order_key, key, fill_price in rows:
        market_type, period, side, line = str(key).split("|", 3)
        if market_type != "winner":
            continue
        market = Market(market_type, period, side or None,
                        float(line) if line else None, side, True)
        map_result = MapResult(
            result.winner_side, result.team_one_kills, result.team_two_kills,
            result.duration_seconds / 60.0, {},
        )
        outcome, returned = settle(market, map_result, float(fill_price))
        count += int(store.insert_settlement(
            str(order_key), outcome, returned, result.settled_at, result.evidence_ref
        ))
    return count


async def label_once(
    store: LiveBettingStore, client: OpenDotaClient, archive: Database,
    match_id: str, team_id: int, team_side: str,
) -> dict[str, object]:
    match = store.connection.execute(
        "SELECT status, scheduled_at FROM raybet_matches WHERE raybet_match_id=?",
        (match_id,),
    ).fetchone()
    if not match:
        return {"status": "waiting_for_match_metadata"}
    if str(match[0]) != "2":
        return {"status": "waiting_for_raybet_completion"}
    strict_rows = store.connection.execute(
        """SELECT map_number, canonical_team_one_id, canonical_team_two_id
             FROM strict_live_map_mappings
            WHERE raybet_match_id=? ORDER BY map_number""",
        (match_id,),
    ).fetchall()
    if not strict_rows:
        return {"status": "waiting_for_strict_mapping"}
    team_pairs = {
        (int(row["canonical_team_one_id"]), int(row["canonical_team_two_id"]))
        for row in strict_rows
    }
    if len(team_pairs) != 1:
        return {"status": "strict_mapping_team_conflict"}
    team_one_id, team_two_id = next(iter(team_pairs))
    expected_team_id = team_one_id if team_side == "team_one" else team_two_id
    if team_id != expected_team_id:
        return {"status": "strict_mapping_team_mismatch"}
    strict_maps = {int(row["map_number"]) for row in strict_rows}
    drafts = _vision_drafts(store.connection, match_id)
    if not drafts:
        return {"status": "waiting_for_confirmed_draft"}
    scheduled = _scheduled_timestamp(match[1])
    summaries = await client.get_team_matches(team_id)
    candidates = [row for row in summaries if
                  abs(int(row.get("start_time") or 0) - scheduled) <= 6 * 3600]
    labeled = settled = 0
    for summary in sorted(candidates, key=lambda row: int(row.get("start_time") or 0)):
        dota_match_id = int(summary["match_id"])
        if store.connection.execute(
            "SELECT 1 FROM map_results WHERE dota_match_id=?", (dota_match_id,)
        ).fetchone():
            continue
        detail = await client.get_match(dota_match_id)
        hero_set = frozenset(
            int(player.get("hero_id") or 0) for player in detail.get("players") or []
        )
        matching_maps = [
            map_number
            for map_number, sets in drafts.items()
            if map_number in strict_maps and hero_set in sets
        ]
        if len(matching_maps) != 1:
            continue
        map_number = matching_maps[0]
        winner_side, target_kills, opponent_kills = _winner(detail, team_id, team_side)
        if team_side == "team_one":
            team_one_kills, team_two_kills = target_kills, opponent_kills
        else:
            team_one_kills, team_two_kills = opponent_kills, target_kills
        result = StoredMapResult(
            match_id, map_number, dota_match_id, winner_side,
            team_one_kills, team_two_kills, int(detail.get("duration") or 0),
            f"opendota:{dota_match_id}", datetime.now(timezone.utc),
        )
        if store.insert_map_result(result):
            archive.insert_match(detail)
            labeled += 1
            settled += _settle_winner_orders(store, result)
    return {"status": "labeled", "maps": labeled, "orders_settled": settled}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--match-id")
    parser.add_argument("--team-id", type=int)
    parser.add_argument("--team-side", choices=("team_one", "team_two"))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.all and not (args.match_id and args.team_id and args.team_side):
        parser.error("provide --all or --match-id, --team-id, and --team-side")

    async def run() -> int:
        client = OpenDotaClient(rate_limit=30)
        archive = Database(str(args.database))
        archive.connect()
        try:
            with LiveBettingStore(args.database) as store:
                store.init_schema()
                while True:
                    try:
                        if args.all:
                            results = []
                            rows = store.connection.execute(
                                """SELECT DISTINCT r.raybet_match_id,
                                          mapping.canonical_team_one_id
                                   FROM raybet_matches AS r
                                   JOIN vision_observations AS v
                                     ON v.raybet_match_id=r.raybet_match_id
                                   JOIN strict_live_map_mappings AS mapping
                                     ON mapping.raybet_match_id=r.raybet_match_id
                                   WHERE r.status='2' AND v.confirmed=1"""
                            ).fetchall()
                            for match_id, team_id in rows:
                                result = await label_once(
                                    store,
                                    client,
                                    archive,
                                    str(match_id),
                                    int(team_id),
                                    "team_one",
                                )
                                results.append({"match_id": match_id, **result})
                            result = {"status": "batch", "matches": results}
                        else:
                            result = await label_once(
                                store, client, archive, args.match_id,
                                args.team_id, args.team_side,
                            )
                        print(json.dumps(result, ensure_ascii=False))
                    except Exception:
                        logger.exception("post-match labeling iteration failed")
                        if args.once:
                            return 1
                    if args.once:
                        return 0
                    await asyncio.sleep(args.interval)
        finally:
            await client.close()
            archive.close()

    return asyncio.run(run())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
