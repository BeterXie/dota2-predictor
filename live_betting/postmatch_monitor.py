"""Match completed maps to OpenDota by exact draft and settle shadow orders."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fetch.client import OpenDotaClient
from fetch.db import Database
from event_intelligence.ingest_adapters import SQLiteIngestAdapter
from event_intelligence.raw_archive import RawArchive, canonical_json_bytes
from event_intelligence.registry import EventRegistry
from event_intelligence.storage import IntelligenceStorage

from .health import record_health
from .markets import normalized_state_hash, snapshots_from_payload
from .models import Market
from .raybet import RayBetClient, RayBetMapFinal, parse_raybet_map_final
from .settlement import MapResult, reconcile_map_winners, settle
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
    radiant_team_id = detail.get("radiant_team_id")
    radiant_win = detail.get("radiant_win")
    radiant_score = detail.get("radiant_score")
    dire_score = detail.get("dire_score")
    if (
        type(radiant_team_id) is not int
        or type(radiant_win) is not bool
        or type(radiant_score) is not int
        or type(dire_score) is not int
        or radiant_score < 0
        or dire_score < 0
        or team_side not in {"team_one", "team_two"}
    ):
        raise ValueError("OpenDota map result is incomplete or invalid")
    target_radiant = radiant_team_id == team_id
    target_won = radiant_win == target_radiant
    winner_side = team_side if target_won else (
        "team_two" if team_side == "team_one" else "team_one"
    )
    target_kills = radiant_score if target_radiant else dire_score
    opponent_kills = dire_score if target_radiant else radiant_score
    return winner_side, target_kills, opponent_kills


def _opendota_evidence_ref(detail: dict, dota_match_id: int) -> str:
    encoded = json.dumps(
        detail,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"opendota:{dota_match_id}:sha256:{digest}"


def _raybet_observation_key(
    match_id: str, observed_at: datetime, payload: dict[str, object]
) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return hashlib.sha256(
        f"direct\n{match_id}\n{observed_at.isoformat()}\n{digest}".encode("utf-8")
    ).hexdigest()


def _latest_exact_raybet_final(
    store: LiveBettingStore,
    match_id: str,
    map_number: int,
    *,
    team_ids: tuple[int, int],
) -> RayBetMapFinal | None:
    """Resolve a map from the newest complete immutable transport response."""
    try:
        rows = store.connection.execute(
            """SELECT transport.observation_key, transport.observed_at,
                      outcome.odds_group_id, outcome.side, outcome.raw_json
                 FROM odds_transport_observations AS transport
                 JOIN odds_response_outcomes AS outcome
                   ON outcome.observation_key=transport.observation_key
                WHERE outcome.raybet_match_id=?
                  AND outcome.market_type='winner'
                  AND outcome.period=?
                  AND outcome.supported=1
                  AND transport.timing_status='on_time'
                  AND transport.processing_status='processed'
                ORDER BY transport.observed_at DESC,
                         transport.observation_key DESC,
                         outcome.odds_group_id, outcome.side""",
            (match_id, f"map_{map_number}"),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    observation_times: dict[str, str] = {}
    observation_order: list[str] = []
    for row in rows:
        observation_key = str(row["observation_key"])
        if observation_key not in observation_times:
            observation_times[observation_key] = str(row["observed_at"])
            observation_order.append(observation_key)
        group_key = (observation_key, str(row["odds_group_id"] or ""))
        grouped.setdefault(group_key, []).append(row)
    for observation_key in observation_order:
        for (candidate_key, _group_id), members in grouped.items():
            if candidate_key != observation_key or {str(row["side"]) for row in members} != {
                "team_one", "team_two"
            }:
                continue
            odds: list[dict[str, object]] = []
            for row in members:
                try:
                    raw = json.loads(str(row["raw_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    odds = []
                    break
                if not isinstance(raw, dict):
                    odds = []
                    break
                odds.append(raw)
            if len(odds) != 2:
                continue
            final = parse_raybet_map_final(
                {
                    "id": match_id,
                    "game_id": 151,
                    "team": [
                        {"pos": 1, "team_id": team_ids[0]},
                        {"pos": 2, "team_id": team_ids[1]},
                    ],
                    "odds": odds,
                },
                map_number,
                observed_at=datetime.fromisoformat(observation_times[observation_key]),
                expected_match_id=match_id,
                expected_team_ids=team_ids,
            )
            if final.status in {"confirmed", "conflict"}:
                return final
    return None


def _winner_order_rows(
    store: LiveBettingStore, raybet_match_id: str, map_number: int
) -> list[sqlite3.Row]:
    return store.connection.execute(
        """SELECT o.order_key, o.odds_id, o.market_key, o.fill_price
           FROM shadow_orders o JOIN shadow_map_attempts a ON a.order_key=o.order_key
           LEFT JOIN settlements s ON s.order_key=o.order_key
           WHERE a.raybet_match_id=? AND a.map_number=?
             AND o.status='filled' AND s.order_key IS NULL""",
        (raybet_match_id, map_number),
    ).fetchall()


def _settle_winner_orders(
    store: LiveBettingStore,
    result: StoredMapResult,
    rows: list[sqlite3.Row] | None = None,
) -> int:
    rows = rows if rows is not None else _winner_order_rows(
        store, result.raybet_match_id, result.map_number
    )
    count = 0
    for row in rows:
        order_key, key, fill_price = row["order_key"], row["market_key"], row["fill_price"]
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


def _reconcile_and_settle(
    store: LiveBettingStore,
    result: StoredMapResult,
    raybet_final: RayBetMapFinal,
) -> dict[str, object]:
    """Atomically persist source evidence, resolution, settlement, and outbox."""
    rows = _winner_order_rows(store, result.raybet_match_id, result.map_number)
    status, reason = reconcile_map_winners(
        raybet_status=raybet_final.status,
        raybet_winner=raybet_final.winner_side,
        opendota_winner=result.winner_side,
    )
    if raybet_final.status != "confirmed":
        reason = raybet_final.reason
    elif status == "confirmed":
        for row in rows:
            parts = str(row["market_key"]).split("|", 3)
            if len(parts) != 4:
                status, reason = "manual_review", "order_market_identity_invalid"
                break
            market_type, period, side, line = parts
            fill_price = row["fill_price"]
            if (
                market_type != "winner"
                or period != f"map_{result.map_number}"
                or side not in {"team_one", "team_two"}
                or line
                or isinstance(fill_price, bool)
                or not isinstance(fill_price, (int, float))
                or not float(fill_price) > 1.0
            ):
                status, reason = "manual_review", "order_market_identity_invalid"
                break
            selection_won = raybet_final.selection_won(str(row["odds_id"]))
            if selection_won is None:
                status, reason = "pending", "raybet_order_outcome_missing"
                break
            if selection_won != (side == raybet_final.winner_side):
                status, reason = "manual_review", "raybet_order_outcome_conflict"
                break

    reconciliation_ref = (
        f"settlement-reconciliation:{result.raybet_match_id}:map:{result.map_number}"
    )
    opendota_facts = {
        "dota_match_id": result.dota_match_id,
        "winner_side": result.winner_side,
        "team_one_kills": result.team_one_kills,
        "team_two_kills": result.team_two_kills,
        "duration_seconds": result.duration_seconds,
    }
    with store.transaction():
        reconciliation = store.record_settlement_reconciliation(
            raybet_match_id=result.raybet_match_id,
            map_number=result.map_number,
            dota_match_id=result.dota_match_id,
            raybet_status=raybet_final.status,
            raybet_winner_side=raybet_final.winner_side,
            opendota_winner_side=result.winner_side,
            raybet_evidence_ref=raybet_final.evidence_ref,
            opendota_evidence_ref=result.evidence_ref,
            raybet_facts=raybet_final.facts(),
            opendota_facts=opendota_facts,
            status=status,
            reason=reason,
            observed_at=result.settled_at,
        )
        effective_status = str(reconciliation["status"])
        if effective_status == "manual_review":
            for row in rows:
                store.insert_settlement(
                    str(row["order_key"]),
                    "review",
                    0.0,
                    result.settled_at,
                    reconciliation_ref,
                    True,
                )
            return {"status": "manual_review", "orders_settled": 0}
        if effective_status != "confirmed":
            return {"status": "pending", "orders_settled": 0}

        reconciled_result = replace(result, evidence_ref=reconciliation_ref)
        stored = store.connection.execute(
            """SELECT dota_match_id, winner_side FROM map_results
                WHERE raybet_match_id=? AND map_number=?""",
            (result.raybet_match_id, result.map_number),
        ).fetchone()
        if stored is not None and tuple(stored) != (
            result.dota_match_id,
            result.winner_side,
        ):
            reconciliation = store.record_settlement_reconciliation(
                raybet_match_id=result.raybet_match_id,
                map_number=result.map_number,
                dota_match_id=result.dota_match_id,
                raybet_status=raybet_final.status,
                raybet_winner_side=raybet_final.winner_side,
                opendota_winner_side=result.winner_side,
                raybet_evidence_ref=raybet_final.evidence_ref,
                opendota_evidence_ref=result.evidence_ref,
                raybet_facts=raybet_final.facts(),
                opendota_facts=opendota_facts,
                status="manual_review",
                reason="stored_map_result_conflict",
                observed_at=result.settled_at,
            )
            assert reconciliation["status"] == "manual_review"
            return {"status": "manual_review", "orders_settled": 0}
        if stored is None:
            store.insert_map_result(reconciled_result)
        settled = _settle_winner_orders(store, reconciled_result, rows)
        return {"status": "confirmed", "orders_settled": settled}


async def label_once(
    store: LiveBettingStore, client: OpenDotaClient, raw_archive: RawArchive,
    match_id: str, team_id: int, team_side: str,
    raybet_client: RayBetClient | None = None,
) -> dict[str, object]:
    match = store.connection.execute(
        """SELECT scheduled_at, raw_json, updated_at
             FROM raybet_matches WHERE raybet_match_id=?""",
        (match_id,),
    ).fetchone()
    if not match:
        return {"status": "waiting_for_match_metadata"}
    strict_rows = store.connection.execute(
        """SELECT mapping.map_number, mapping.team_one_id, mapping.team_two_id,
                  mapping.canonical_team_one_id, mapping.canonical_team_two_id
             FROM strict_live_map_mappings AS mapping
             LEFT JOIN strict_live_map_mapping_invalidations AS direct_invalidation
               ON direct_invalidation.mapping_id=mapping.mapping_id
             LEFT JOIN strict_live_automatic_evidence_approvals AS approval
               ON approval.approval_id=mapping.automatic_approval_id
             LEFT JOIN strict_live_map_mapping_invalidations AS source_invalidation
               ON source_invalidation.mapping_id=approval.source_mapping_id
            WHERE mapping.raybet_match_id=?
              AND direct_invalidation.invalidation_id IS NULL
              AND source_invalidation.invalidation_id IS NULL
            ORDER BY mapping.map_number""",
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
    raybet_team_pairs = {
        (int(row["team_one_id"]), int(row["team_two_id"])) for row in strict_rows
    }
    if len(raybet_team_pairs) != 1:
        return {"status": "strict_mapping_raybet_team_conflict"}
    raybet_team_ids = next(iter(raybet_team_pairs))
    expected_team_id = team_one_id if team_side == "team_one" else team_two_id
    if team_id != expected_team_id:
        return {"status": "strict_mapping_team_mismatch"}
    strict_maps = {int(row["map_number"]) for row in strict_rows}
    unresolved_maps = strict_maps - {
        int(row["map_number"])
        for row in store.connection.execute(
            """SELECT map_number FROM settlement_reconciliations
                WHERE raybet_match_id=?
                  AND status IN ('confirmed', 'manual_review')""",
            (match_id,),
        )
    }
    try:
        raybet_payload = json.loads(str(match["raw_json"]))
    except (TypeError, json.JSONDecodeError):
        return {"status": "waiting_for_raybet_final_payload"}
    raybet_observed_at = datetime.fromisoformat(str(match["updated_at"]))
    if raybet_observed_at.tzinfo is None:
        raybet_observed_at = raybet_observed_at.replace(tzinfo=timezone.utc)
    if raybet_client is not None and unresolved_maps:
        try:
            refreshed_response = raybet_client.match_odds(match_id)
            refreshed = refreshed_response.get("result")
        except Exception as error:
            logger.warning(
                "RayBet final refresh failed for match_id=%s (%s)",
                match_id,
                type(error).__name__,
            )
        else:
            if (
                not isinstance(refreshed, dict)
                or str(refreshed.get("id") or "") != match_id
                or type(refreshed.get("game_id")) is not int
                or int(refreshed["game_id"]) != 151
            ):
                return {"status": "raybet_final_refresh_identity_conflict"}
            raybet_observed_at = datetime.now(timezone.utc)
            with store.transaction():
                store.upsert_raybet_match(refreshed, raybet_observed_at)
                snapshots = snapshots_from_payload(
                    refreshed_response, received_at=raybet_observed_at
                )
                store.store_odds_observation(
                    source="direct",
                    observation_key=_raybet_observation_key(
                        match_id, raybet_observed_at, refreshed_response
                    ),
                    source_event_id=None,
                    raybet_match_id=match_id,
                    observed_at=raybet_observed_at,
                    normalized_state_hash=normalized_state_hash(snapshots),
                    snapshots=snapshots,
                )
            raybet_payload = refreshed
    drafts = _vision_drafts(store.connection, match_id)
    if not drafts:
        return {"status": "waiting_for_confirmed_draft"}
    scheduled = _scheduled_timestamp(match["scheduled_at"])
    summaries = await client.get_team_matches(team_id)
    summary_observed_at = datetime.now(timezone.utc)
    raw_archive.archive_json(
        source="opendota",
        endpoint=f"/api/teams/{team_id}/matches",
        request_identity=f"/api/teams/{team_id}/matches",
        payload_bytes=canonical_json_bytes(summaries),
        observed_at=summary_observed_at,
        match_id=None,
        status_code=200,
    )
    candidates = [
        row
        for row in summaries
        if type(row.get("match_id")) is int
        and int(row["match_id"]) > 0
        and type(row.get("start_time")) is int
        and abs(int(row["start_time"]) - scheduled) <= 6 * 3600
    ]
    labeled = settled = pending = manual_review = 0
    for summary in sorted(candidates, key=lambda row: int(row.get("start_time") or 0)):
        dota_match_id = int(summary["match_id"])
        detail = await client.get_match(dota_match_id)
        observed_at = datetime.now(timezone.utc)
        detail_endpoint = f"/api/matches/{dota_match_id}"
        detail_request_identity = detail_endpoint
        raw_archive.archive_json(
            source="opendota",
            endpoint=detail_endpoint,
            request_identity=detail_request_identity,
            payload_bytes=canonical_json_bytes(detail),
            observed_at=observed_at,
            match_id=dota_match_id,
            status_code=200,
            first_usable_at=None,
        )
        if type(detail.get("match_id")) is not int or detail["match_id"] != dota_match_id:
            raise ValueError("OpenDota match identity is invalid")
        players = detail.get("players")
        if (
            not isinstance(players, list)
            or len(players) != 10
            or any(
                not isinstance(player, dict)
                or type(player.get("hero_id")) is not int
                or int(player["hero_id"]) <= 0
                for player in players
            )
        ):
            continue
        hero_set = frozenset(int(player["hero_id"]) for player in players)
        if len(hero_set) != 10:
            continue
        matching_maps = [
            map_number
            for map_number, sets in drafts.items()
            if map_number in strict_maps and hero_set in sets
        ]
        if len(matching_maps) != 1:
            continue
        map_number = matching_maps[0]
        if {
            detail.get("radiant_team_id"),
            detail.get("dire_team_id"),
        } != {team_one_id, team_two_id}:
            continue
        winner_side, target_kills, opponent_kills = _winner(detail, team_id, team_side)
        if team_side == "team_one":
            team_one_kills, team_two_kills = target_kills, opponent_kills
        else:
            team_one_kills, team_two_kills = opponent_kills, target_kills
        duration = detail.get("duration")
        if type(duration) is not int or duration <= 0:
            raise ValueError("OpenDota map duration is incomplete or invalid")
        settled_at = datetime.now(timezone.utc)
        raw_archive.archive_json(
            source="opendota",
            endpoint=detail_endpoint,
            request_identity=detail_request_identity,
            payload_bytes=canonical_json_bytes(detail),
            observed_at=observed_at,
            match_id=dota_match_id,
            status_code=200,
            first_usable_at=settled_at,
        )
        result = StoredMapResult(
            match_id, map_number, dota_match_id, winner_side,
            team_one_kills, team_two_kills, duration,
            _opendota_evidence_ref(detail, dota_match_id), settled_at,
        )
        raybet_final = _latest_exact_raybet_final(
            store,
            match_id,
            map_number,
            team_ids=raybet_team_ids,
        ) or parse_raybet_map_final(
            raybet_payload,
            map_number,
            observed_at=raybet_observed_at,
            expected_match_id=match_id,
            expected_team_ids=raybet_team_ids,
        )
        existed = store.connection.execute(
            """SELECT 1 FROM map_results
                WHERE raybet_match_id=? AND map_number=?""",
            (match_id, map_number),
        ).fetchone()
        outcome = _reconcile_and_settle(store, result, raybet_final)
        if outcome["status"] == "confirmed" and existed is None:
            labeled += 1
        elif outcome["status"] == "pending":
            pending += 1
        elif outcome["status"] == "manual_review":
            manual_review += 1
        settled += int(outcome["orders_settled"])
    return {
        "status": "labeled",
        "maps": labeled,
        "orders_settled": settled,
        "settlement_pending": pending,
        "settlement_manual_review": manual_review,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--match-id")
    parser.add_argument("--team-id", type=int)
    parser.add_argument("--team-side", choices=("team_one", "team_two"))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=ROOT / "data" / "raw" / "event_intelligence",
    )
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if not args.all and not (args.match_id and args.team_id and args.team_side):
        parser.error("provide --all or --match-id, --team-id, and --team-side")

    async def run() -> int:
        client = OpenDotaClient(rate_limit=30)
        raybet_client = RayBetClient()
        try:
            with LiveBettingStore(args.database) as store:
                if not getattr(args, "schema_prepared", False):
                    store.init_schema()
                intelligence_storage = IntelligenceStorage(
                    args.database, connection=store.connection
                )
                if not getattr(args, "schema_prepared", False):
                    intelligence_storage.init_schema()
                    Database(connection=store.connection).init_db()
                registry = EventRegistry(intelligence_storage)
                ingest_store = SQLiteIngestAdapter(
                    intelligence_storage, registry,
                    Database(connection=store.connection),
                )
                raw_archive = RawArchive(
                    args.archive_root,
                    observation_sink=ingest_store.record_raw_artifact,
                )
                started_at = datetime.now(timezone.utc)
                record_health(
                    store.connection,
                    "postmatch_worker",
                    "starting",
                    heartbeat_at=started_at,
                    details={"source": "worker"},
                )
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
                                   LEFT JOIN strict_live_map_mapping_invalidations
                                     AS direct_invalidation
                                     ON direct_invalidation.mapping_id=mapping.mapping_id
                                   LEFT JOIN strict_live_automatic_evidence_approvals
                                     AS approval
                                     ON approval.approval_id=mapping.automatic_approval_id
                                   LEFT JOIN strict_live_map_mapping_invalidations
                                     AS source_invalidation
                                     ON source_invalidation.mapping_id=approval.source_mapping_id
                                   WHERE v.confirmed=1
                                     AND direct_invalidation.invalidation_id IS NULL
                                     AND source_invalidation.invalidation_id IS NULL"""
                            ).fetchall()
                            for match_id, team_id in rows:
                                result = await label_once(
                                    store,
                                    client,
                                    raw_archive,
                                    str(match_id),
                                    int(team_id),
                                    "team_one",
                                    raybet_client,
                                )
                                results.append({"match_id": match_id, **result})
                            result = {"status": "batch", "matches": results}
                        else:
                            result = await label_once(
                                store, client, raw_archive, args.match_id,
                                args.team_id, args.team_side, raybet_client,
                            )
                        succeeded_at = datetime.now(timezone.utc)
                        record_health(
                            store.connection,
                            "postmatch_worker",
                            "healthy",
                            heartbeat_at=succeeded_at,
                            success_at=succeeded_at,
                            details={
                                "source": "worker",
                                "run_status": result.get("status"),
                            },
                        )
                        print(json.dumps(result, ensure_ascii=False))
                    except Exception as error:
                        failed_at = datetime.now(timezone.utc)
                        record_health(
                            store.connection,
                            "postmatch_worker",
                            "degraded",
                            heartbeat_at=failed_at,
                            error_at=failed_at,
                            error=type(error).__name__,
                            details={"source": "worker"},
                        )
                        logger.exception("post-match labeling iteration failed")
                        if args.once:
                            return 1
                    if args.once:
                        return 0
                    await asyncio.sleep(args.interval)
        finally:
            await client.close()
            raybet_client.close()

    return asyncio.run(run())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
