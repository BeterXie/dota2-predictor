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
from .monitor import _write_raw
from .models import Market
from .raybet import RayBetClient, RayBetMapFinal, parse_raybet_map_final
from .sanitize import sanitize_raybet_payload
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


@dataclass(frozen=True)
class VisionDraftIdentity:
    radiant_hero_ids: frozenset[int]
    dire_hero_ids: frozenset[int]
    radiant_team_side: str


def _scheduled_timestamp(value: str | None) -> int:
    if not value:
        return 0
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(parsed.timestamp())


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _draft_identity(
    radiant_value: object,
    dire_value: object,
) -> tuple[str, frozenset[int], frozenset[int]] | None:
    try:
        radiant = json.loads(str(radiant_value))
        dire = json.loads(str(dire_value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(radiant, list)
        or not isinstance(dire, list)
        or len(radiant) != 5
        or len(dire) != 5
    ):
        return None
    hero_ids = radiant + dire
    if (
        any(type(hero_id) is not int or hero_id <= 0 for hero_id in hero_ids)
        or len(set(hero_ids)) != 10
    ):
        return None
    payload = json.dumps(
        {"radiant": radiant, "dire": dire},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        frozenset(radiant),
        frozenset(dire),
    )


def _vision_drafts(
    connection: sqlite3.Connection,
    match_id: str,
    *,
    causal_cutoffs: dict[int, datetime] | None = None,
) -> dict[int, set[VisionDraftIdentity]]:
    """Return trusted draft identities, optionally at per-map event-time cutoffs.

    A conflicted map remains hidden by default.  A caller may opt into a
    historical replay only for a map whose dependent signal is known to have
    occurred before every recorded conflict.  This keeps post-match replay
    consistent with the causal gates used by settlement.
    """
    output: dict[int, set[VisionDraftIdentity]] = {}
    try:
        rows = connection.execute(
            """SELECT map_number, draft_hash, radiant_hero_ids, dire_hero_ids,
                      radiant_team_side, team_side_anchored_at,
                      team_side_source_frame_ref, anchored_at,
                      source_frame_ref, status, conflict_at
                 FROM vision_draft_anchors
                WHERE raybet_match_id=?""",
            (match_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    conflict_rows: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        map_number = int(row["map_number"])
        cutoff = (causal_cutoffs or {}).get(map_number)
        anchored_at = _parse_utc(row["anchored_at"])
        team_side = row["radiant_team_side"]
        team_side_anchored_at = _parse_utc(row["team_side_anchored_at"])
        if (
            anchored_at is None
            or team_side not in {"team_one", "team_two"}
            or team_side_anchored_at is None
            or team_side_anchored_at < anchored_at
            or not str(row["team_side_source_frame_ref"] or "").strip()
        ):
            continue
        if cutoff is not None:
            cutoff = _parse_utc(cutoff)
            if (
                cutoff is None
                or anchored_at > cutoff
                or team_side_anchored_at > cutoff
            ):
                continue
        status = str(row["status"])
        if status == "conflict":
            if cutoff is None:
                continue
            if map_number not in conflict_rows:
                try:
                    conflict_rows[map_number] = connection.execute(
                        """SELECT captured_at FROM vision_draft_conflicts
                            WHERE raybet_match_id=? AND map_number=?""",
                        (match_id, map_number),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return {}
            conflict_times = [_parse_utc(row["conflict_at"])]
            conflict_times.extend(
                _parse_utc(conflict["captured_at"])
                for conflict in conflict_rows[map_number]
            )
            if any(timestamp is None or timestamp <= cutoff for timestamp in conflict_times):
                continue
        elif status != "anchored":
            continue
        anchor_identity = _draft_identity(
            row["radiant_hero_ids"], row["dire_hero_ids"]
        )
        if anchor_identity is None or anchor_identity[0] != str(row["draft_hash"]):
            continue
        try:
            trusted_observations = connection.execute(
                """SELECT observation.captured_at,
                          observation.radiant_hero_ids,
                          observation.dire_hero_ids,
                          observation.radiant_team_side
                     FROM vision_observations AS observation
                    WHERE observation.raybet_match_id=?
                      AND observation.map_number=?
                      AND observation.confirmed=1
                      AND NOT EXISTS (
                          SELECT 1
                            FROM vision_observation_invalidations AS invalidation
                           WHERE invalidation.raybet_match_id=observation.raybet_match_id
                             AND invalidation.captured_at=observation.captured_at
                             AND invalidation.source_frame_ref=observation.source_frame_ref
                      )
                    ORDER BY observation.captured_at,
                             observation.source_frame_ref""",
                (match_id, map_number),
            ).fetchall()
            invalidations = connection.execute(
                """SELECT invalidation.captured_at
                     FROM vision_observation_invalidations AS invalidation
                     JOIN vision_observations AS observation
                       ON observation.raybet_match_id=invalidation.raybet_match_id
                      AND observation.captured_at=invalidation.captured_at
                      AND observation.source_frame_ref=invalidation.source_frame_ref
                    WHERE invalidation.raybet_match_id=?
                      AND observation.map_number=?""",
                (match_id, map_number),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        invalidated_at: list[datetime] = []
        invalidation_time_damaged = False
        for invalidation in invalidations:
            captured_at = _parse_utc(invalidation["captured_at"])
            if captured_at is None:
                invalidation_time_damaged = True
                break
            if cutoff is None or captured_at <= cutoff:
                invalidated_at.append(captured_at)
        if invalidation_time_damaged:
            continue
        latest_invalidation = max(invalidated_at, default=None)
        for observation in trusted_observations:
            captured_at = _parse_utc(observation["captured_at"])
            if (
                captured_at is None
                or captured_at < team_side_anchored_at
                or (cutoff is not None and captured_at > cutoff)
                or (
                    latest_invalidation is not None
                    and captured_at <= latest_invalidation
                )
            ):
                continue
            identity = _draft_identity(
                observation["radiant_hero_ids"], observation["dire_hero_ids"]
            )
            if (
                identity is not None
                and identity[0] == anchor_identity[0]
                and observation["radiant_team_side"] == team_side
            ):
                output.setdefault(map_number, set()).add(
                    VisionDraftIdentity(
                        radiant_hero_ids=anchor_identity[1],
                        dire_hero_ids=anchor_identity[2],
                        radiant_team_side=str(team_side),
                    )
                )
                break
    return output


def _opendota_matches_vision_identity(
    detail: dict,
    vision_identity: VisionDraftIdentity,
    *,
    team_one_id: int,
    team_two_id: int,
    opendota_league_id: int,
) -> bool:
    if (
        vision_identity.radiant_team_side not in {"team_one", "team_two"}
        or type(detail.get("leagueid")) is not int
        or detail["leagueid"] != opendota_league_id
        or type(detail.get("radiant_team_id")) is not int
        or type(detail.get("dire_team_id")) is not int
    ):
        return False
    expected_team_ids = (
        (team_one_id, team_two_id)
        if vision_identity.radiant_team_side == "team_one"
        else (team_two_id, team_one_id)
    )
    if (
        detail["radiant_team_id"],
        detail["dire_team_id"],
    ) != expected_team_ids:
        return False

    players = detail.get("players")
    if (
        not isinstance(players, list)
        or len(players) != 10
        or any(
            not isinstance(player, dict)
            or type(player.get("player_slot")) is not int
            or type(player.get("hero_id")) is not int
            or player["hero_id"] <= 0
            for player in players
        )
    ):
        return False
    slot_to_hero = {
        int(player["player_slot"]): int(player["hero_id"])
        for player in players
    }
    if set(slot_to_hero) != {*range(5), *range(128, 133)}:
        return False
    if len(set(slot_to_hero.values())) != 10:
        return False
    return (
        frozenset(slot_to_hero[slot] for slot in range(5))
        == vision_identity.radiant_hero_ids
        and frozenset(slot_to_hero[slot] for slot in range(128, 133))
        == vision_identity.dire_hero_ids
    )


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


class RayBetFinalRefreshIdentityError(ValueError):
    """The archived final response does not belong to the requested match."""


def _refresh_raybet_final(
    store: LiveBettingStore,
    raw_archive: RawArchive,
    raybet_client: RayBetClient,
    match_id: str,
) -> tuple[dict[str, object], datetime]:
    """Archive and normalize one final RayBet response atomically.

    The raw response is written before identity validation so an invalid
    provider response remains auditable without entering normalized state.
    """
    response = sanitize_raybet_payload(raybet_client.match_odds(match_id))
    observed_at = datetime.now(timezone.utc)
    _write_raw(raw_archive.root / "raybet", match_id, response, observed_at)
    result = response.get("result") if isinstance(response, dict) else None
    if (
        not isinstance(result, dict)
        or str(result.get("id") or "") != match_id
        or type(result.get("game_id")) is not int
        or int(result["game_id"]) != 151
    ):
        raise RayBetFinalRefreshIdentityError(
            f"RayBet final response identity mismatch for {match_id}"
        )
    with store.transaction():
        store.upsert_raybet_match(result, observed_at)
        snapshots = snapshots_from_payload(response, received_at=observed_at)
        store.store_odds_observation(
            source="direct",
            observation_key=_raybet_observation_key(match_id, observed_at, response),
            source_event_id=None,
            raybet_match_id=match_id,
            observed_at=observed_at,
            normalized_state_hash=normalized_state_hash(snapshots),
            snapshots=snapshots,
        )
    return result, observed_at


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
                      transport.source, outcome.odds_group_id, outcome.side,
                      outcome.supported, outcome.raw_json,
                      browser.payload_json AS browser_payload_json,
                      browser.game_id AS browser_game_id,
                      metadata.raw_json AS direct_result_json
                 FROM odds_transport_observations AS transport
                 LEFT JOIN odds_response_outcomes AS outcome
                   ON outcome.observation_key=transport.observation_key
                  AND outcome.market_type='winner'
                  AND outcome.period=?
                 LEFT JOIN browser_events AS browser
                   ON browser.event_id=transport.source_event_id
                 LEFT JOIN raybet_matches AS metadata
                   ON metadata.raybet_match_id=transport.raybet_match_id
                  AND metadata.updated_at=transport.observed_at
                  AND transport.source='direct'
                WHERE transport.raybet_match_id=?
                  AND transport.timing_status='on_time'
                  AND transport.processing_status='processed'
                ORDER BY transport.observed_at DESC,
                         transport.observation_key DESC,
                         outcome.odds_group_id, outcome.side""",
            (f"map_{map_number}", match_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    grouped: dict[str, list[sqlite3.Row]] = {}
    observation_order: list[str] = []
    for row in rows:
        observation_key = str(row["observation_key"])
        if observation_key not in grouped:
            observation_order.append(observation_key)
        grouped.setdefault(observation_key, []).append(row)
    for observation_key in observation_order:
        members = grouped[observation_key]
        observed_at = datetime.fromisoformat(str(members[0]["observed_at"]))
        source = str(members[0]["source"])
        exact_json = (
            members[0]["browser_payload_json"]
            if source == "browser"
            else members[0]["direct_result_json"]
        )
        if exact_json is not None:
            try:
                exact = json.loads(str(exact_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                return RayBetMapFinal(
                    "conflict", None, None, None, (),
                    "raybet_transport_payload_invalid",
                    f"raybet-transport:{observation_key}:map:{map_number}",
                    observed_at,
                )
            result = exact.get("result") if source == "browser" else exact
            if not isinstance(result, dict):
                return RayBetMapFinal(
                    "conflict", None, None, None, (),
                    "raybet_transport_payload_invalid",
                    f"raybet-transport:{observation_key}:map:{map_number}",
                    observed_at,
                )
            result = dict(result)
            if source == "browser" and result.get("game_id") is None:
                result["game_id"] = members[0]["browser_game_id"]
            final = parse_raybet_map_final(
                result,
                map_number,
                observed_at=observed_at,
                expected_match_id=match_id,
                expected_team_ids=team_ids,
            )
            if final.status in {"confirmed", "conflict"}:
                return final
            continue
        if source == "browser":
            return RayBetMapFinal(
                "conflict", None, None, None, (),
                "raybet_transport_payload_missing",
                f"raybet-transport:{observation_key}:map:{map_number}",
                observed_at,
            )
        odds: list[dict[str, object]] = []
        for row in members:
            if row["raw_json"] is None:
                continue
            try:
                raw = json.loads(str(row["raw_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                return RayBetMapFinal(
                    "conflict", None, None, None, (),
                    "raybet_transport_outcome_invalid",
                    f"raybet-transport:{observation_key}:map:{map_number}",
                    observed_at,
                )
            if not isinstance(raw, dict):
                return RayBetMapFinal(
                    "conflict", None, None, None, (),
                    "raybet_transport_outcome_invalid",
                    f"raybet-transport:{observation_key}:map:{map_number}",
                    observed_at,
                )
            odds.append(raw)
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
            observed_at=observed_at,
            expected_match_id=match_id,
            expected_team_ids=team_ids,
        )
        if final.status in {"confirmed", "conflict"}:
            return final
    return None


def _winner_order_rows(
    store: LiveBettingStore,
    raybet_match_id: str,
    map_number: int,
    *,
    include_conflicted: bool = False,
) -> list[sqlite3.Row]:
    rows = store.connection.execute(
        """SELECT o.order_key, o.odds_id, o.market_key, o.fill_price,
                  o.signal_transport_at
           FROM shadow_orders o JOIN shadow_map_attempts a ON a.order_key=o.order_key
           LEFT JOIN settlements s ON s.order_key=o.order_key
           WHERE a.raybet_match_id=? AND a.map_number=?
             AND o.status='filled' AND s.order_key IS NULL""",
        (raybet_match_id, map_number),
    ).fetchall()
    if include_conflicted:
        return rows
    return [
        row
        for row in rows
        if store.order_block_reason(str(row["order_key"])) is None
    ]


def _causal_draft_cutoffs(
    store: LiveBettingStore,
    match_id: str,
    map_numbers: set[int],
) -> dict[int, datetime]:
    """Return signal-time cutoffs for filled order lineage that remains valid."""
    if not map_numbers:
        return {}
    rows = store.connection.execute(
        """SELECT attempt.map_number, orders.order_key,
                  orders.signal_transport_at
             FROM shadow_orders AS orders
             JOIN shadow_map_attempts AS attempt
               ON attempt.order_key=orders.order_key
            WHERE attempt.raybet_match_id=? AND orders.status='filled'""",
        (match_id,),
    ).fetchall()
    output: dict[int, datetime] = {}
    for row in rows:
        map_number = int(row["map_number"])
        if map_number not in map_numbers:
            continue
        if store.order_block_reason(str(row["order_key"])) is not None:
            continue
        cutoff = _parse_utc(row["signal_transport_at"])
        if cutoff is not None:
            output[map_number] = cutoff
    return output


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
    *,
    expected_strict_mapping_id: int | None = None,
) -> dict[str, object]:
    """Atomically persist source evidence, resolution, settlement, and outbox."""
    with store.transaction():
        if expected_strict_mapping_id is not None:
            current_mapping = store.connection.execute(
                """SELECT 1
                     FROM strict_live_map_mappings AS mapping
                     LEFT JOIN strict_live_map_mapping_invalidations
                       AS direct_invalidation
                       ON direct_invalidation.mapping_id=mapping.mapping_id
                     LEFT JOIN strict_live_automatic_evidence_approvals AS approval
                       ON approval.approval_id=mapping.automatic_approval_id
                     LEFT JOIN strict_live_map_mapping_invalidations
                       AS source_invalidation
                       ON source_invalidation.mapping_id=approval.source_mapping_id
                    WHERE mapping.mapping_id=?
                      AND mapping.raybet_match_id=?
                      AND mapping.map_number=?
                      AND direct_invalidation.invalidation_id IS NULL
                      AND source_invalidation.invalidation_id IS NULL""",
                (
                    expected_strict_mapping_id,
                    result.raybet_match_id,
                    result.map_number,
                ),
            ).fetchone()
            if current_mapping is None:
                return {
                    "status": "strict_mapping_unverified",
                    "orders_settled": 0,
                }
        # Re-read the order and draft state under the write lock.  A conflict
        # arriving between a preflight read and settlement must not turn into
        # an automatic result.
        all_rows = _winner_order_rows(
            store, result.raybet_match_id, result.map_number,
            include_conflicted=True,
        )
        lineage_rows = store.connection.execute(
            """SELECT orders.order_key, orders.signal_transport_at,
                      settlement.review_required, settlement.result
                 FROM shadow_orders AS orders
                 JOIN shadow_map_attempts AS attempt
                   ON attempt.order_key=orders.order_key
                 LEFT JOIN settlements AS settlement
                   ON settlement.order_key=orders.order_key
                WHERE attempt.raybet_match_id=? AND attempt.map_number=?""",
            (result.raybet_match_id, result.map_number),
        ).fetchall()
        blocked_rows = []
        for row in all_rows:
            if store.order_block_reason(str(row["order_key"])) is not None:
                blocked_rows.append(row)
        blocked_reason = None
        for row in lineage_rows:
            candidate = store.order_block_reason(str(row["order_key"]))
            if candidate is not None:
                blocked_reason = candidate
                break
        blocked_lineage = blocked_reason is not None
        existing_review = any(
            bool(row["review_required"]) or str(row["result"] or "") == "review"
            for row in lineage_rows
        )
        rows = [row for row in all_rows if row not in blocked_rows]
        status, reason = reconcile_map_winners(
            raybet_status=raybet_final.status,
            raybet_winner=raybet_final.winner_side,
            opendota_winner=result.winner_side,
        )
        if raybet_final.status != "confirmed":
            reason = raybet_final.reason
        elif blocked_rows or blocked_lineage:
            status, reason = (
                "manual_review",
                blocked_reason or "vision_draft_conflict",
            )
        elif existing_review:
            status, reason = "manual_review", "existing_settlement_review"
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
            for row in all_rows:
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
        if stored is None and not store.insert_map_result(reconciled_result):
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
                reason="map_result_persistence_conflict",
                observed_at=result.settled_at,
            )
            assert reconciliation["status"] == "manual_review"
            for row in all_rows:
                store.insert_settlement(
                    str(row["order_key"]),
                    "review",
                    0.0,
                    result.settled_at,
                    reconciliation_ref,
                    True,
                )
            return {"status": "manual_review", "orders_settled": 0}
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
        """SELECT mapping.mapping_id, mapping.map_number, mapping.event_id,
                  mapping.team_one_id, mapping.team_two_id,
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
    integer_fields = (
        "mapping_id",
        "map_number",
        "team_one_id",
        "team_two_id",
        "canonical_team_one_id",
        "canonical_team_two_id",
    )
    if any(
        type(row[field]) is not int or int(row[field]) <= 0
        for row in strict_rows
        for field in integer_fields
    ) or any(not str(row["event_id"] or "").strip() for row in strict_rows):
        return {"status": "strict_mapping_identity_unverified"}
    team_pairs = {
        (int(row["canonical_team_one_id"]), int(row["canonical_team_two_id"]))
        for row in strict_rows
    }
    if len(team_pairs) != 1 or any(one == two for one, two in team_pairs):
        return {"status": "strict_mapping_team_conflict"}
    team_one_id, team_two_id = next(iter(team_pairs))
    raybet_team_pairs = {
        (int(row["team_one_id"]), int(row["team_two_id"])) for row in strict_rows
    }
    if len(raybet_team_pairs) != 1 or any(
        one == two for one, two in raybet_team_pairs
    ):
        return {"status": "strict_mapping_raybet_team_conflict"}
    raybet_team_ids = next(iter(raybet_team_pairs))
    event_ids = {str(row["event_id"]) for row in strict_rows}
    if len(event_ids) != 1:
        return {"status": "strict_mapping_event_conflict"}
    event_id = next(iter(event_ids))
    expected_team_id = team_one_id if team_side == "team_one" else team_two_id
    if team_id != expected_team_id:
        return {"status": "strict_mapping_team_mismatch"}
    mapping_ids_by_map: dict[int, set[int]] = {}
    for row in strict_rows:
        mapping_ids_by_map.setdefault(int(row["map_number"]), set()).add(
            int(row["mapping_id"])
        )
    if any(len(mapping_ids) != 1 for mapping_ids in mapping_ids_by_map.values()):
        return {"status": "strict_mapping_map_conflict"}
    strict_mapping_ids = {
        map_number: next(iter(mapping_ids))
        for map_number, mapping_ids in mapping_ids_by_map.items()
    }
    strict_maps = set(strict_mapping_ids)
    unresolved_maps = strict_maps - {
        int(row["map_number"])
        for row in store.connection.execute(
            """SELECT map_number FROM settlement_reconciliations
                WHERE raybet_match_id=?
                  AND status IN ('confirmed', 'manual_review')""",
            (match_id,),
        )
    }
    drafts = _vision_drafts(
        store.connection,
        match_id,
        causal_cutoffs=_causal_draft_cutoffs(store, match_id, unresolved_maps),
    )
    if not drafts:
        return {"status": "waiting_for_confirmed_draft"}
    try:
        event = store.connection.execute(
            "SELECT opendota_league_id FROM event_registry WHERE event_id=?",
            (event_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        event = None
    if (
        event is None
        or type(event["opendota_league_id"]) is not int
        or event["opendota_league_id"] <= 0
    ):
        return {"status": "strict_mapping_event_unverified"}
    opendota_league_id = int(event["opendota_league_id"])
    try:
        raybet_payload = json.loads(str(match["raw_json"]))
    except (TypeError, json.JSONDecodeError):
        return {"status": "waiting_for_raybet_final_payload"}
    raybet_observed_at = datetime.fromisoformat(str(match["updated_at"]))
    if raybet_observed_at.tzinfo is None:
        raybet_observed_at = raybet_observed_at.replace(tzinfo=timezone.utc)
    if raybet_client is not None and unresolved_maps:
        try:
            refreshed, raybet_observed_at = _refresh_raybet_final(
                store, raw_archive, raybet_client, match_id
            )
        except RayBetFinalRefreshIdentityError:
            return {"status": "raybet_final_refresh_identity_conflict"}
        except Exception as error:
            logger.warning(
                "RayBet final refresh failed for match_id=%s (%s)",
                match_id,
                type(error).__name__,
            )
        else:
            raybet_payload = refreshed
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
    exact_candidates: dict[
        int, dict[int, tuple[dict, datetime, str, str]]
    ] = {}
    ambiguous_maps: set[int] = set()
    candidates_by_id = {int(row["match_id"]): row for row in candidates}
    for summary in sorted(
        candidates_by_id.values(), key=lambda row: int(row.get("start_time") or 0)
    ):
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
        matching_maps = [
            map_number
            for map_number, identities in drafts.items()
            if map_number in strict_maps
            and any(
                _opendota_matches_vision_identity(
                    detail,
                    identity,
                    team_one_id=team_one_id,
                    team_two_id=team_two_id,
                    opendota_league_id=opendota_league_id,
                )
                for identity in identities
            )
        ]
        if len(matching_maps) != 1:
            if len(matching_maps) > 1:
                ambiguous_maps.update(matching_maps)
            continue
        map_number = matching_maps[0]
        exact_candidates.setdefault(map_number, {})[dota_match_id] = (
            detail,
            observed_at,
            detail_endpoint,
            detail_request_identity,
        )

    ambiguous_maps.update(
        map_number
        for map_number, matches in exact_candidates.items()
        if len(matches) > 1
    )
    if ambiguous_maps:
        quarantined_at = datetime.now(timezone.utc).isoformat()
        with store.transaction():
            for map_number in sorted(ambiguous_maps):
                store.connection.execute(
                    """UPDATE settlement_reconciliations
                          SET status='manual_review',
                              reason=CASE WHEN status='manual_review'
                                          THEN reason
                                          ELSE 'opendota_map_identity_ambiguous' END,
                              updated_at=?
                        WHERE raybet_match_id=? AND map_number=?""",
                    (quarantined_at, match_id, map_number),
                )
                store.connection.execute(
                    """UPDATE settlements SET review_required=1
                        WHERE order_key IN (
                            SELECT order_key FROM shadow_map_attempts
                             WHERE raybet_match_id=? AND map_number=?
                        )""",
                    (match_id, map_number),
                )
                outbox_rows = store.connection.execute(
                    """SELECT outbox.outbox_id
                         FROM notification_outbox AS outbox
                         JOIN shadow_map_attempts AS attempt
                           ON attempt.order_key=outbox.order_key
                        WHERE attempt.raybet_match_id=?
                          AND attempt.map_number=?
                          AND outbox.event_type='settled'
                          AND outbox.status IN ('pending', 'leased')""",
                    (match_id, map_number),
                ).fetchall()
                for outbox_row in outbox_rows:
                    outbox_id = int(outbox_row["outbox_id"])
                    store.connection.execute(
                        """UPDATE notification_outbox
                              SET status='dead_letter', lease_token=NULL,
                                  lease_until=NULL,
                                  last_error='opendota_map_identity_ambiguous',
                                  updated_at=?
                            WHERE outbox_id=?
                              AND status IN ('pending', 'leased')""",
                        (quarantined_at, outbox_id),
                    )
                    store.connection.execute(
                        """INSERT INTO notification_outbox_audit
                           (outbox_id, action, actor, reason, created_at)
                           VALUES (?, 'blocked', 'postmatch_identity',
                                   'opendota_map_identity_ambiguous', ?)""",
                        (outbox_id, quarantined_at),
                    )
        return {
            "status": "opendota_map_identity_ambiguous",
            "ambiguous_maps": sorted(ambiguous_maps),
        }

    labeled = settled = pending = manual_review = 0
    for map_number in sorted(exact_candidates):
        dota_match_id, candidate = next(iter(exact_candidates[map_number].items()))
        detail, observed_at, detail_endpoint, detail_request_identity = candidate
        winner_side, target_kills, opponent_kills = _winner(detail, team_id, team_side)
        if team_side == "team_one":
            team_one_kills, team_two_kills = target_kills, opponent_kills
        else:
            team_one_kills, team_two_kills = opponent_kills, target_kills
        duration = detail.get("duration")
        if type(duration) is not int or duration <= 0:
            raise ValueError("OpenDota map duration is incomplete or invalid")
        settled_at = datetime.now(timezone.utc)
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
        outcome = _reconcile_and_settle(
            store,
            result,
            raybet_final,
            expected_strict_mapping_id=strict_mapping_ids[map_number],
        )
        if outcome["status"] == "strict_mapping_unverified":
            return {
                "status": "strict_mapping_changed_during_postmatch",
                "map_number": map_number,
            }
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
                                   JOIN vision_draft_anchors AS anchor
                                     ON anchor.raybet_match_id=v.raybet_match_id
                                    AND anchor.map_number=v.map_number
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
