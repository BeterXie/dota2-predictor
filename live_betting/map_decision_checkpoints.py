"""Record and settle independent, traceable Map decision checkpoints."""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from contracts.live_observation import MAP_START_EVIDENCE_WINDOW_SECONDS
from database.session import PostgresSession

from .health import record_health
from .live_probability import (
    MODEL_VERSION as LIVE_PROBABILITY_MODEL_VERSION,
    estimate_radiant_win_probability,
)
from .models import utc_now
from .raybet_state import (
    LIVE_MATCH_MAX_AGE,
    explicit_raybet_map_times,
    infer_current_map_number,
    raybet_match_is_live,
)
from .storage import LiveBettingStore


logger = logging.getLogger(__name__)
STRATEGY_VERSION = "map-decision-shadow-v1"
MINIMUM_EDGE = 0.08
PREGAME_ODDS_MAX_AGE_SECONDS = 150.0
LIVE_ODDS_MAX_AGE_SECONDS = 15.0
LIVE_VISION_MAX_AGE_SECONDS = 5.0
LIVE_ODDS_VISION_GAP_MAX_SECONDS = 15.0
LIVE_CHECKPOINT_INTERVAL_MINUTES = 5
LIVE_CHECKPOINT_CAPTURE_WINDOW_SECONDS = 15
PREGAME_MISSING_PREDICTION_GRACE_SECONDS = 60.0
ENDED_MATCH_STATUSES = frozenset(
    {
        "3",
        "4",
        "5",
        "abandoned",
        "cancelled",
        "closed",
        "completed",
        "ended",
        "finished",
        "settled",
        "void",
    }
)


_CHECKPOINT_COLUMNS = (
    "checkpoint_id, raybet_match_id, map_number, mapping_version, phase, "
    "checkpoint_minute, strategy_version, decision, assumed_stake_units, "
    "observed_price, model_probability_team_one, model_probability_team_two, "
    "market_probability_team_one, market_probability_team_two, selected_edge, "
    "odds_observation_key, odds_group_id, odds_observed_at, odds_age_seconds, "
    "odds_max_age_seconds, vision_snapshot_id, vision_source_frame_ref, "
    "vision_captured_at, vision_game_time_seconds, vision_networth_lead, "
    "vision_radiant_kills, vision_dire_kills, vision_age_seconds, "
    "vision_max_age_seconds, odds_vision_gap_seconds, "
    "odds_vision_gap_max_seconds, vision_trusted, vision_replay, "
    "input_versions_json, feature_availability_json, reason, decided_at, created_at"
)


def record_due_checkpoints(
    connection: PostgresSession,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    decided_at = _utc(now or utc_now(), "now")
    created = unchanged = 0
    for mapping in _locked_mappings(connection):
        prediction = _prediction(connection, mapping)
        mapping_age = max(
            0.0,
            (
                decided_at - _parse_utc(mapping["created_at"], "mapping created_at")
            ).total_seconds(),
        )
        pregame_ready = (
            prediction is not None
            or mapping_age >= PREGAME_MISSING_PREDICTION_GRACE_SECONDS
        )
        if pregame_ready and _pregame_mapping_is_current(
            connection,
            mapping,
            now=decided_at,
        ):
            result = record_pregame_checkpoint(
                connection,
                mapping=mapping,
                prediction=prediction,
                decided_at=decided_at,
            )
            created += int(result["inserted"])
            unchanged += int(not result["inserted"])

        for minute in _due_live_checkpoint_minutes(
            connection,
            mapping,
            now=decided_at,
        ):
            result = record_live_checkpoint(
                connection,
                mapping=mapping,
                prediction=prediction,
                checkpoint_minute=minute,
                decided_at=decided_at,
            )
            created += int(result["inserted"])
            unchanged += int(not result["inserted"])
    return {"created": created, "unchanged": unchanged}


def record_pregame_checkpoint(
    connection: PostgresSession,
    *,
    mapping: Mapping[str, Any],
    prediction: Mapping[str, Any] | None = None,
    decided_at: datetime,
) -> dict[str, object]:
    decided = _utc(decided_at, "decided_at")
    identity = _mapping_identity(mapping)
    _require_latest_locked_map(connection, identity)
    effective_prediction = (
        _normalize_prediction(prediction)
        if prediction is not None
        else _prediction(connection, mapping)
    )
    market = _latest_market(
        connection,
        identity["raybet_match_id"],
        identity["map_number"],
        decided,
    )
    direction = _radiant_match_side(
        connection,
        identity["raybet_match_id"],
        identity["map_number"],
    )
    values = _pregame_values(
        identity=identity,
        prediction=effective_prediction,
        market=market,
        radiant_match_side=direction,
        decided_at=decided,
    )
    return _insert_checkpoint(connection, values)


def record_live_checkpoint(
    connection: PostgresSession,
    *,
    mapping: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    checkpoint_minute: int,
    decided_at: datetime,
) -> dict[str, object]:
    decided = _utc(decided_at, "decided_at")
    identity = _mapping_identity(mapping)
    if checkpoint_minute < 5 or checkpoint_minute % 5:
        raise ValueError(
            "live checkpoint minute must be a positive five-minute boundary"
        )
    snapshot = _checkpoint_snapshot(
        connection,
        identity["raybet_match_id"],
        identity["map_number"],
        checkpoint_minute,
    )
    market = _latest_market(
        connection,
        identity["raybet_match_id"],
        identity["map_number"],
        decided,
    )
    values = _live_values(
        identity=identity,
        prediction=prediction,
        market=market,
        snapshot=snapshot,
        checkpoint_minute=checkpoint_minute,
        decided_at=decided,
    )
    return _insert_checkpoint(connection, values)


def settle_open_checkpoints(
    connection: PostgresSession,
    *,
    settled_at: datetime | None = None,
) -> dict[str, int]:
    settled = _utc(settled_at or utc_now(), "settled_at")
    rows = connection.execute(
        """SELECT checkpoint.checkpoint_id, checkpoint.raybet_match_id,
                  checkpoint.map_number, checkpoint.decision,
                  checkpoint.observed_price, result.dota_match_id,
                  result.winner_side, result.settled_at AS result_recorded_at
             FROM map_decision_checkpoints AS checkpoint
             JOIN raybet_matches AS series
               ON series.raybet_match_id=checkpoint.raybet_match_id
             JOIN map_results AS result
               ON result.raybet_match_id=checkpoint.raybet_match_id
              AND result.map_number=checkpoint.map_number
             JOIN matches AS official
               ON official.match_id=result.dota_match_id
             JOIN settlement_reconciliations AS reconciliation
               ON reconciliation.raybet_match_id=result.raybet_match_id
              AND reconciliation.map_number=result.map_number
              AND reconciliation.dota_match_id=result.dota_match_id
              AND reconciliation.status='confirmed'
             LEFT JOIN map_decision_checkpoint_settlements AS existing
               ON existing.checkpoint_id=checkpoint.checkpoint_id
            WHERE existing.checkpoint_id IS NULL
              AND lower(trim(CAST(series.status AS TEXT))) IN (
                  '3', '4', '5', 'abandoned', 'cancelled', 'closed',
                  'completed', 'ended', 'finished', 'settled', 'void'
              )
              AND official.start_time>0
              AND result.duration_seconds>0
              AND live_text_timestamp_utc(checkpoint.decided_at)
                  <= to_timestamp(official.start_time + result.duration_seconds)
            ORDER BY checkpoint.checkpoint_id"""
    ).fetchall()
    inserted = 0
    with connection.transaction():
        for row in rows:
            decision = str(row["decision"])
            winner = str(row["winner_side"])
            if decision == "skip":
                outcome, gross, profit = "skip", 0.0, 0.0
            else:
                selected = "team_one" if decision == "bet_team_a" else "team_two"
                if selected == winner:
                    price = float(row["observed_price"])
                    outcome, gross, profit = "win", price, price - 1.0
                else:
                    outcome, gross, profit = "loss", 0.0, -1.0
            cursor = connection.execute(
                """INSERT INTO map_decision_checkpoint_settlements
                   (checkpoint_id, raybet_match_id, map_number, dota_match_id,
                    winner_side, outcome, gross_return_units, profit_units,
                    result_source, result_recorded_at, settled_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed_map_result', ?, ?, ?)
                   ON CONFLICT(checkpoint_id) DO NOTHING""",
                (
                    int(row["checkpoint_id"]),
                    str(row["raybet_match_id"]),
                    int(row["map_number"]),
                    int(row["dota_match_id"]),
                    winner,
                    outcome,
                    gross,
                    profit,
                    str(row["result_recorded_at"]),
                    settled.isoformat(),
                    settled.isoformat(),
                ),
            )
            inserted += int(cursor.rowcount == 1)
    return {"settled": inserted, "unchanged": len(rows) - inserted}


def latest_map_checkpoints(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
) -> list[dict[str, object]]:
    rows = connection.execute(
        f"""SELECT {_CHECKPOINT_COLUMNS}
              FROM map_decision_checkpoints
             WHERE raybet_match_id=? AND map_number=?
             ORDER BY checkpoint_minute, checkpoint_id""",
        (raybet_match_id, map_number),
    ).fetchall()
    series = connection.execute(
        """SELECT series.status, official.start_time, result.duration_seconds
             FROM raybet_matches AS series
             LEFT JOIN map_results AS result
               ON result.raybet_match_id=series.raybet_match_id
              AND result.map_number=?
             LEFT JOIN matches AS official
               ON official.match_id=result.dota_match_id
            WHERE series.raybet_match_id=?""",
        (map_number, raybet_match_id),
    ).fetchone()
    checkpoints = []
    for row in rows:
        checkpoint = _checkpoint_payload(row)
        evaluation_eligible, evaluation_exclusion_reason = (
            checkpoint_evaluation_eligibility(
                checkpoint["decided_at"],
                None if series is None else series["status"],
                None if series is None else series["start_time"],
                None if series is None else series["duration_seconds"],
            )
        )
        checkpoint["evaluation_eligible"] = evaluation_eligible
        checkpoint["evaluation_exclusion_reason"] = evaluation_exclusion_reason
        settlement = connection.execute(
            """SELECT settlement_id, dota_match_id, winner_side, outcome,
                      gross_return_units, profit_units, result_source,
                      result_recorded_at, settled_at
                 FROM map_decision_checkpoint_settlements
                WHERE checkpoint_id=?""",
            (checkpoint["checkpoint_id"],),
        ).fetchone()
        checkpoint["settlement"] = None if settlement is None else dict(settlement)
        checkpoints.append(checkpoint)
    return checkpoints


def checkpoint_evaluation_eligibility(
    decided_at: object,
    series_status: object,
    official_start_time: object,
    duration_seconds: object,
) -> tuple[bool, str | None]:
    status = str(series_status or "").strip().casefold()
    if status not in ENDED_MATCH_STATUSES:
        return True, None
    ended_at = _official_map_end(official_start_time, duration_seconds)
    if ended_at is None:
        return False, "official_map_end_unavailable"
    decided = _parse_utc(decided_at, "checkpoint decided_at")
    if decided > ended_at:
        return False, "checkpoint_recorded_after_official_map_end"
    return True, None


def _official_map_end(
    official_start_time: object,
    duration_seconds: object,
) -> datetime | None:
    if type(duration_seconds) is not int or int(duration_seconds) <= 0:
        return None
    if isinstance(official_start_time, datetime):
        try:
            started_at = _utc(official_start_time, "official start_time")
        except ValueError:
            return None
    elif type(official_start_time) is int and int(official_start_time) > 0:
        try:
            started_at = datetime.fromtimestamp(
                int(official_start_time),
                timezone.utc,
            )
        except (OSError, OverflowError, ValueError):
            return None
    else:
        try:
            started_at = _parse_utc(official_start_time, "official start_time")
        except ValueError:
            return None
    return started_at + timedelta(seconds=int(duration_seconds))


def _locked_mappings(connection: PostgresSession) -> list[dict[str, object]]:
    rows = connection.execute(
        """WITH latest AS (
               SELECT raybet_match_id, map_number, MAX(version) AS version
                 FROM live_draft_mappings
                GROUP BY raybet_match_id, map_number
           )
           SELECT mapping.raybet_match_id, mapping.map_number, mapping.version,
                  MIN(mapping.created_at) AS created_at
             FROM live_draft_mappings AS mapping
             JOIN latest
              ON latest.raybet_match_id=mapping.raybet_match_id
              AND latest.map_number=mapping.map_number
              AND latest.version=mapping.version
             JOIN raybet_matches AS match_row
               ON match_row.raybet_match_id=mapping.raybet_match_id
            WHERE lower(match_row.status) IN (
                '1', '2', 'open', 'active', 'running', 'upcoming',
                'scheduled', 'not_started'
            )
            GROUP BY mapping.raybet_match_id, mapping.map_number, mapping.version
           HAVING COUNT(*)=10 AND COUNT(DISTINCT mapping.hero_id)=10
              AND COUNT(*) FILTER (WHERE mapping.is_locked=1)=10
              AND COUNT(*) FILTER (WHERE mapping.side='radiant')=5
              AND COUNT(*) FILTER (WHERE mapping.side='dire')=5
              AND COUNT(DISTINCT mapping.position)
                  FILTER (WHERE mapping.side='radiant')=5
              AND COUNT(DISTINCT mapping.position)
                  FILTER (WHERE mapping.side='dire')=5
            ORDER BY mapping.raybet_match_id, mapping.map_number"""
    ).fetchall()
    mappings = [
        {
            "raybet_match_id": str(row[0]),
            "map_number": int(row[1]),
            "version": int(row[2]),
            "created_at": str(row[3]),
        }
        for row in rows
    ]
    latest_by_series: dict[str, tuple[datetime, int, int]] = {}
    for mapping in mappings:
        match_id = str(mapping["raybet_match_id"])
        key = (
            _parse_utc(mapping["created_at"], "mapping created_at"),
            int(mapping["map_number"]),
            int(mapping["version"]),
        )
        latest_by_series[match_id] = max(latest_by_series.get(match_id, key), key)
    for mapping in mappings:
        match_id = str(mapping["raybet_match_id"])
        mapping["latest_locked_for_series"] = (
            _parse_utc(mapping["created_at"], "mapping created_at"),
            int(mapping["map_number"]),
            int(mapping["version"]),
        ) == latest_by_series[match_id]
    return mappings


def _pregame_mapping_is_current(
    connection: PostgresSession,
    mapping: Mapping[str, Any],
    *,
    now: datetime,
) -> bool:
    identity = _mapping_identity(mapping)
    provider_state = _provider_live_map_state(
        connection,
        str(identity["raybet_match_id"]),
        now=now,
    )
    if provider_state is not None:
        return provider_state[0] == identity["map_number"]
    return mapping.get("latest_locked_for_series") is True


def _require_latest_locked_map(
    connection: PostgresSession,
    identity: Mapping[str, Any],
) -> None:
    row = connection.execute(
        """SELECT MAX(map_number) FROM live_draft_mappings
            WHERE raybet_match_id=? AND is_locked=1""",
        (identity["raybet_match_id"],),
    ).fetchone()
    latest_map_number = None if row is None else row[0]
    if type(latest_map_number) is int and latest_map_number > identity["map_number"]:
        raise ValueError("pregame_target_is_not_latest_locked_map")


def _prediction(
    connection: PostgresSession,
    mapping: Mapping[str, Any],
) -> dict[str, object] | None:
    identity = _mapping_identity(mapping)
    row = connection.execute(
        """SELECT bridge_version, record_status, p1_probability, missing_reason,
                  causal_status, causal_reason, created_at,
                  game_clock_seconds, draft_state_marker
             FROM live_draft_prospective_predictions
            WHERE raybet_match_id=? AND map_number=? AND mapping_version=?""",
        (
            identity["raybet_match_id"],
            identity["map_number"],
            identity["mapping_version"],
        ),
    ).fetchone()
    if row is None:
        return None
    return {
        "bridge_version": str(row[0]),
        "record_status": str(row[1]),
        "p1_probability": None if row[2] is None else float(row[2]),
        "missing_reason": None if row[3] is None else str(row[3]),
        "causal_status": str(row[4]),
        "causal_reason": None if row[5] is None else str(row[5]),
        "created_at": str(row[6]),
        "game_clock_seconds": None if row[7] is None else int(row[7]),
        "draft_state_marker": None if row[8] is None else str(row[8]),
    }


def _normalize_prediction(prediction: Mapping[str, Any]) -> dict[str, object]:
    causal = prediction.get("causal_evidence")
    causal_payload = causal if isinstance(causal, Mapping) else {}
    return {
        "bridge_version": str(
            prediction.get("bridge_version") or prediction.get("version") or ""
        ),
        "record_status": str(prediction.get("record_status") or ""),
        "p1_probability": prediction.get("p1_probability"),
        "missing_reason": prediction.get("missing_reason"),
        "causal_status": str(
            prediction.get("causal_status") or causal_payload.get("causal_status") or ""
        ),
        "causal_reason": prediction.get("causal_reason")
        or causal_payload.get("causal_reason"),
        "created_at": prediction.get("created_at"),
        "game_clock_seconds": prediction.get("game_clock_seconds")
        if "game_clock_seconds" in prediction
        else causal_payload.get("game_clock_seconds"),
        "draft_state_marker": prediction.get("draft_state_marker")
        or causal_payload.get("draft_state_marker"),
    }


def _radiant_match_side(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
) -> str | None:
    row = connection.execute(
        """SELECT radiant_team_side
             FROM vision_draft_anchors
            WHERE raybet_match_id=? AND map_number=?
              AND status='anchored' AND conflict_at IS NULL""",
        (raybet_match_id, map_number),
    ).fetchone()
    side = None if row is None else str(row[0] or "")
    return side if side in {"team_one", "team_two"} else None


def _latest_market(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
    as_of: datetime,
) -> dict[str, object] | None:
    period = f"map_{map_number}"
    row = connection.execute(
        """SELECT market.observation_key, transport.observed_at,
                  market.odds_group_id, market.underdog_side,
                  market.underdog_price, market.underdog_probability,
                  favorite.side, favorite.price
             FROM trusted_odds_winner_market_authority AS market
             JOIN odds_transport_observations AS transport
               ON transport.observation_key=market.observation_key
              AND transport.raybet_match_id=market.raybet_match_id
             JOIN odds_response_outcomes_effective AS favorite
               ON favorite.observation_key=market.observation_key
              AND favorite.raybet_match_id=market.raybet_match_id
              AND favorite.period=market.period
              AND favorite.odds_group_id=market.odds_group_id
              AND favorite.response_state_hash=market.response_state_hash
              AND favorite.response_artifact_hash=market.response_artifact_hash
              AND favorite.side<>market.underdog_side
            WHERE market.raybet_match_id=? AND market.period=?
              AND transport.source='direct'
              AND transport.timing_status='on_time'
              AND transport.processing_status='processed'
              AND live_text_timestamp_utc(transport.observed_at)
                  <= live_text_timestamp_utc(?)
              AND favorite.storage_version='v2'
              AND favorite.market_type='winner'
              AND favorite.supported=1
              AND lower(trim(favorite.status::text))
                  IN ('1', 'open', 'active', 'running')
              AND favorite.price>1.0
            ORDER BY live_text_timestamp_utc(transport.observed_at) DESC,
                     market.observation_key DESC
            LIMIT 1""",
        (raybet_match_id, period, as_of.isoformat()),
    ).fetchone()
    if row is None:
        return None
    underdog_side, favorite_side = str(row[3]), str(row[6])
    if {underdog_side, favorite_side} != {"team_one", "team_two"}:
        return None
    underdog_probability = _probability(row[5])
    probabilities = {
        underdog_side: underdog_probability,
        favorite_side: 1.0 - underdog_probability,
    }
    prices = {
        underdog_side: _price(row[4]),
        favorite_side: _price(row[7]),
    }
    return {
        "observation_key": str(row[0]),
        "observed_at": _parse_utc(row[1], "odds observed_at"),
        "odds_group_id": str(row[2]),
        "probabilities": probabilities,
        "prices": prices,
    }


def _due_live_checkpoint_minutes(
    connection: PostgresSession,
    mapping: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> list[int]:
    identity = _mapping_identity(mapping)
    checked_at = _utc(now or utc_now(), "now")
    provider_state = _provider_live_map_state(
        connection,
        str(identity["raybet_match_id"]),
        now=checked_at,
    )
    if provider_state is not None and provider_state[0] != identity["map_number"]:
        return []
    provider_started_at = _provider_map_started_at(
        connection,
        str(identity["raybet_match_id"]),
        int(identity["map_number"]),
    )
    provider_boundary = (
        """OR live_text_timestamp_utc(snapshot.captured_at)>=
                     CAST(? AS timestamptz)"""
        if provider_started_at is not None
        else ""
    )
    row = connection.execute(
        f"""SELECT MAX(snapshot.game_time_seconds)
             FROM live_game_snapshots AS snapshot
             JOIN trusted_vision_observation_authority AS vision
               ON vision.raybet_match_id=snapshot.raybet_match_id
              AND vision.map_number=snapshot.map_number
              AND vision.captured_at=snapshot.captured_at
              AND vision.source_frame_ref=snapshot.screenshot_path
            WHERE snapshot.raybet_match_id=? AND snapshot.map_number=?
              AND snapshot.source='vision'
              AND (
                  snapshot.map_number=1
                  OR EXISTS (
                      SELECT 1
                        FROM trusted_vision_observation_authority AS map_start
                       WHERE map_start.raybet_match_id=snapshot.raybet_match_id
                         AND map_start.map_number=snapshot.map_number
                         AND map_start.game_clock_seconds BETWEEN 0 AND ?
                         AND live_text_timestamp_utc(map_start.captured_at)<=
                             live_text_timestamp_utc(snapshot.captured_at)
                  )
                  {provider_boundary}
              )""",
        (
            identity["raybet_match_id"],
            identity["map_number"],
            MAP_START_EVIDENCE_WINDOW_SECONDS,
            *(
                ()
                if provider_started_at is None
                else (provider_started_at.isoformat(),)
            ),
        ),
    ).fetchone()
    maximum = None if row is None else row[0]
    if maximum is None:
        if provider_state is None:
            return []
        started_at = provider_state[1].get(int(identity["map_number"]))
        if started_at is None or started_at > checked_at:
            return []
        maximum = int((checked_at - started_at).total_seconds())
    elif provider_state is None and not _latest_trusted_map_is_recent(
        connection,
        str(identity["raybet_match_id"]),
        int(identity["map_number"]),
        now=checked_at,
    ):
        return []
    if type(maximum) is not int or maximum < LIVE_CHECKPOINT_INTERVAL_MINUTES * 60:
        return []
    maximum_minute = int(maximum) // 60
    due = list(
        range(
            LIVE_CHECKPOINT_INTERVAL_MINUTES,
            maximum_minute + 1,
            LIVE_CHECKPOINT_INTERVAL_MINUTES,
        )
    )
    existing = {
        int(row[0])
        for row in connection.execute(
            """SELECT checkpoint_minute FROM map_decision_checkpoints
                WHERE raybet_match_id=? AND map_number=? AND phase='live'
                  AND strategy_version=?""",
            (
                identity["raybet_match_id"],
                identity["map_number"],
                STRATEGY_VERSION,
            ),
        ).fetchall()
    }
    return [minute for minute in due if minute not in existing]


def _latest_trusted_map_is_recent(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
    *,
    now: datetime,
) -> bool:
    row = connection.execute(
        """SELECT map_number, captured_at
             FROM trusted_vision_observation_authority
            WHERE raybet_match_id=?
            ORDER BY live_text_timestamp_utc(captured_at) DESC,
                     map_number DESC
            LIMIT 1""",
        (raybet_match_id,),
    ).fetchone()
    if row is None or int(row[0]) != map_number:
        return False
    age = (now - _parse_utc(row[1], "vision captured_at")).total_seconds()
    return 0.0 <= age <= LIVE_MATCH_MAX_AGE.total_seconds()


def _provider_live_map_state(
    connection: PostgresSession,
    raybet_match_id: str,
    *,
    now: datetime,
) -> tuple[int | None, dict[int, datetime]] | None:
    row = connection.execute(
        """SELECT raw_json, best_of, status, updated_at
             FROM raybet_matches WHERE raybet_match_id=?""",
        (raybet_match_id,),
    ).fetchone()
    if row is None or type(row[1]) is not int:
        return None
    checked_at = _utc(now, "now")
    if not raybet_match_is_live(row[2], row[3], now=checked_at):
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        current_map_number = infer_current_map_number(payload, int(row[1]))
    except ValueError:
        return None
    return current_map_number, explicit_raybet_map_times(payload, int(row[1]))


def _checkpoint_snapshot(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
    checkpoint_minute: int,
) -> dict[str, object] | None:
    target = checkpoint_minute * 60
    provider_started_at = _provider_map_started_at(
        connection,
        raybet_match_id,
        map_number,
    )
    provider_boundary = (
        """OR live_text_timestamp_utc(snapshot.captured_at)>=
                     CAST(? AS timestamptz)"""
        if provider_started_at is not None
        else ""
    )
    row = connection.execute(
        f"""SELECT snapshot.snapshot_id, snapshot.captured_at,
                  snapshot.game_time_seconds, snapshot.networth_lead,
                  snapshot.radiant_kills, snapshot.dire_kills,
                  snapshot.screenshot_path, vision.radiant_team_side
             FROM live_game_snapshots AS snapshot
             JOIN trusted_vision_observation_authority AS vision
               ON vision.raybet_match_id=snapshot.raybet_match_id
              AND vision.map_number=snapshot.map_number
              AND vision.captured_at=snapshot.captured_at
              AND vision.source_frame_ref=snapshot.screenshot_path
            WHERE snapshot.raybet_match_id=? AND snapshot.map_number=?
              AND snapshot.source='vision'
              AND (
                  snapshot.map_number=1
                  OR EXISTS (
                      SELECT 1
                        FROM trusted_vision_observation_authority AS map_start
                       WHERE map_start.raybet_match_id=snapshot.raybet_match_id
                         AND map_start.map_number=snapshot.map_number
                         AND map_start.game_clock_seconds BETWEEN 0 AND ?
                         AND live_text_timestamp_utc(map_start.captured_at)<=
                             live_text_timestamp_utc(snapshot.captured_at)
                  )
                  {provider_boundary}
              )
              AND snapshot.game_time_seconds>=?
              AND snapshot.game_time_seconds<=?
            ORDER BY snapshot.game_time_seconds, snapshot.captured_at,
                     snapshot.snapshot_id
            LIMIT 1""",
        (
            raybet_match_id,
            map_number,
            MAP_START_EVIDENCE_WINDOW_SECONDS,
            *(
                ()
                if provider_started_at is None
                else (provider_started_at.isoformat(),)
            ),
            target,
            target + LIVE_CHECKPOINT_CAPTURE_WINDOW_SECONDS,
        ),
    ).fetchone()
    if row is None:
        return None
    return {
        "snapshot_id": int(row[0]),
        "captured_at": _parse_utc(row[1], "vision captured_at"),
        "game_time_seconds": int(row[2]),
        "networth_lead": int(row[3]),
        "radiant_kills": None if row[4] is None else int(row[4]),
        "dire_kills": None if row[5] is None else int(row[5]),
        "source_frame_ref": str(row[6]),
        "radiant_team_side": str(row[7]),
    }


def _provider_map_started_at(
    connection: PostgresSession,
    raybet_match_id: str,
    map_number: int,
) -> datetime | None:
    row = connection.execute(
        "SELECT raw_json, best_of FROM raybet_matches WHERE raybet_match_id=?",
        (raybet_match_id,),
    ).fetchone()
    if row is None or type(row[1]) is not int:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return explicit_raybet_map_times(payload, int(row[1])).get(map_number)


def _pregame_values(
    *,
    identity: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    market: Mapping[str, Any] | None,
    radiant_match_side: str | None,
    decided_at: datetime,
) -> dict[str, object]:
    probabilities = _model_probabilities(prediction, radiant_match_side)
    odds_age = _age_seconds(decided_at, market.get("observed_at")) if market else None
    reason = "minimum_edge_not_evaluated"
    decision = "skip"
    selected_edge = observed_price = None
    authority_clock = None if prediction is None else prediction.get("game_clock_seconds")
    authority_marker = (
        "" if prediction is None else str(prediction.get("draft_state_marker") or "")
    )
    if prediction is not None and (
        authority_marker == "in_game"
        or (type(authority_clock) is int and authority_clock >= 0)
    ):
        reason = "pregame_authority_after_map_start"
    elif prediction is None:
        reason = "pregame_prediction_unavailable"
    elif str(prediction.get("causal_status")) != "eligible":
        reason = str(prediction.get("causal_reason") or "pregame_prediction_ineligible")
    elif probabilities is None:
        reason = str(
            prediction.get("missing_reason") or "pregame_probability_unavailable"
        )
    elif market is None:
        reason = "pregame_odds_unavailable"
    elif odds_age is None or odds_age > PREGAME_ODDS_MAX_AGE_SECONDS:
        reason = "pregame_odds_stale"
    else:
        market_probabilities = market["probabilities"]
        assert isinstance(market_probabilities, Mapping)
        edges = {
            side: probabilities[side] - float(market_probabilities[side])
            for side in ("team_one", "team_two")
        }
        selected = "team_one" if edges["team_one"] >= edges["team_two"] else "team_two"
        selected_edge = edges[selected]
        prices = market["prices"]
        assert isinstance(prices, Mapping)
        observed_price = float(prices[selected])
        if selected_edge >= MINIMUM_EDGE:
            decision = "bet_team_a" if selected == "team_one" else "bet_team_b"
            reason = "minimum_edge_met"
        else:
            reason = "edge_below_threshold"
    return _base_values(
        identity=identity,
        phase="pregame",
        checkpoint_minute=0,
        decision=decision,
        observed_price=observed_price,
        model_probabilities=probabilities,
        market=market,
        selected_edge=selected_edge,
        odds_age=odds_age,
        odds_max_age=PREGAME_ODDS_MAX_AGE_SECONDS,
        snapshot=None,
        reason=reason,
        prediction=prediction,
        decided_at=decided_at,
    )


def _live_values(
    *,
    identity: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    market: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    checkpoint_minute: int,
    decided_at: datetime,
) -> dict[str, object]:
    direction = (
        None if snapshot is None else str(snapshot.get("radiant_team_side") or "")
    )
    pregame_probabilities = _model_probabilities(prediction, direction)
    probabilities: dict[str, float] | None = None
    live_model_context: dict[str, object] = {
        "available": False,
        "reason": "inputs_not_eligible",
        "model_version": LIVE_PROBABILITY_MODEL_VERSION,
    }
    decision = "skip"
    observed_price = None
    selected_edge = None
    odds_age = _age_seconds(decided_at, market.get("observed_at")) if market else None
    vision_age = (
        _age_seconds(decided_at, snapshot.get("captured_at")) if snapshot else None
    )
    gap = None
    if market is not None and snapshot is not None:
        gap = abs(
            (
                _parse_utc(market["observed_at"], "odds observed_at")
                - _parse_utc(snapshot["captured_at"], "vision captured_at")
            ).total_seconds()
        )
    if snapshot is None:
        reason = "trusted_vision_checkpoint_missing"
    elif vision_age is None or vision_age > LIVE_VISION_MAX_AGE_SECONDS:
        reason = "live_vision_stale"
    elif snapshot.get("radiant_kills") is None or snapshot.get("dire_kills") is None:
        reason = "live_kills_unavailable"
    elif direction not in {"team_one", "team_two"}:
        reason = "live_team_direction_unavailable"
    elif market is None:
        reason = "live_odds_unavailable"
    elif odds_age is None or odds_age > LIVE_ODDS_MAX_AGE_SECONDS:
        reason = "live_odds_stale"
    elif gap is None or gap > LIVE_ODDS_VISION_GAP_MAX_SECONDS:
        reason = "live_odds_vision_gap_exceeded"
    elif pregame_probabilities is None:
        reason = "pregame_probability_unavailable"
    else:
        try:
            estimate = estimate_radiant_win_probability(
                prior_radiant_probability=float(pregame_probabilities[direction]),
                radiant_networth_lead=int(snapshot["networth_lead"]),
                checkpoint_minute=checkpoint_minute,
            )
        except ValueError as error:
            reason = str(error)
            live_model_context["reason"] = reason
        else:
            radiant_probability = estimate.probability_radiant
            probabilities = (
                {
                    "team_one": radiant_probability,
                    "team_two": 1.0 - radiant_probability,
                }
                if direction == "team_one"
                else {
                    "team_one": 1.0 - radiant_probability,
                    "team_two": radiant_probability,
                }
            )
            live_model_context = {
                **estimate.context(),
                "features_used": [
                    "pregame_radiant_probability",
                    "radiant_networth_lead",
                    "checkpoint_minute",
                ],
                "kills_used": False,
            }
            market_probabilities = market["probabilities"]
            assert isinstance(market_probabilities, Mapping)
            edges = {
                side: probabilities[side] - float(market_probabilities[side])
                for side in ("team_one", "team_two")
            }
            selected = (
                "team_one" if edges["team_one"] >= edges["team_two"] else "team_two"
            )
            selected_edge = edges[selected]
            prices = market["prices"]
            assert isinstance(prices, Mapping)
            observed_price = float(prices[selected])
            if selected_edge >= MINIMUM_EDGE:
                decision = "bet_team_a" if selected == "team_one" else "bet_team_b"
                reason = "minimum_edge_met"
            else:
                decision = "skip"
                reason = "edge_below_threshold"
    if not bool(live_model_context["available"]):
        live_model_context["reason"] = reason
    return _base_values(
        identity=identity,
        phase="live",
        checkpoint_minute=checkpoint_minute,
        decision=decision,
        observed_price=observed_price,
        model_probabilities=probabilities,
        market=market,
        selected_edge=selected_edge,
        odds_age=odds_age,
        odds_max_age=LIVE_ODDS_MAX_AGE_SECONDS,
        snapshot=snapshot,
        reason=reason,
        prediction=prediction,
        decided_at=decided_at,
        vision_age=vision_age,
        odds_vision_gap=gap,
        pregame_probability_available=pregame_probabilities is not None,
        live_model_context=live_model_context,
    )


def _base_values(
    *,
    identity: Mapping[str, Any],
    phase: str,
    checkpoint_minute: int,
    decision: str,
    observed_price: float | None,
    model_probabilities: Mapping[str, float] | None,
    market: Mapping[str, Any] | None,
    selected_edge: float | None,
    odds_age: float | None,
    odds_max_age: float,
    snapshot: Mapping[str, Any] | None,
    reason: str,
    prediction: Mapping[str, Any] | None,
    decided_at: datetime,
    vision_age: float | None = None,
    odds_vision_gap: float | None = None,
    pregame_probability_available: bool | None = None,
    live_model_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    market_probabilities = None if market is None else market["probabilities"]
    return {
        "raybet_match_id": identity["raybet_match_id"],
        "map_number": identity["map_number"],
        "mapping_version": identity["mapping_version"],
        "phase": phase,
        "checkpoint_minute": checkpoint_minute,
        "strategy_version": STRATEGY_VERSION,
        "decision": decision,
        "assumed_stake_units": 1.0,
        "observed_price": observed_price,
        "model_probability_team_one": _mapping_number(model_probabilities, "team_one"),
        "model_probability_team_two": _mapping_number(model_probabilities, "team_two"),
        "market_probability_team_one": _mapping_number(
            market_probabilities, "team_one"
        ),
        "market_probability_team_two": _mapping_number(
            market_probabilities, "team_two"
        ),
        "selected_edge": selected_edge,
        "odds_observation_key": None if market is None else market["observation_key"],
        "odds_group_id": None if market is None else market["odds_group_id"],
        "odds_observed_at": (
            None if market is None else _iso(market["observed_at"], "odds observed_at")
        ),
        "odds_age_seconds": odds_age,
        "odds_max_age_seconds": odds_max_age,
        "vision_snapshot_id": None if snapshot is None else snapshot["snapshot_id"],
        "vision_source_frame_ref": (
            None if snapshot is None else snapshot["source_frame_ref"]
        ),
        "vision_captured_at": (
            None
            if snapshot is None
            else _iso(snapshot["captured_at"], "vision captured_at")
        ),
        "vision_game_time_seconds": (
            None if snapshot is None else snapshot["game_time_seconds"]
        ),
        "vision_networth_lead": None if snapshot is None else snapshot["networth_lead"],
        "vision_radiant_kills": None if snapshot is None else snapshot["radiant_kills"],
        "vision_dire_kills": None if snapshot is None else snapshot["dire_kills"],
        "vision_age_seconds": vision_age,
        "vision_max_age_seconds": None
        if phase == "pregame"
        else LIVE_VISION_MAX_AGE_SECONDS,
        "odds_vision_gap_seconds": odds_vision_gap,
        "odds_vision_gap_max_seconds": (
            None if phase == "pregame" else LIVE_ODDS_VISION_GAP_MAX_SECONDS
        ),
        "vision_trusted": snapshot is not None,
        "vision_replay": False,
        "input_versions_json": _json_text(
            {
                "strategy_version": STRATEGY_VERSION,
                "mapping_version": identity["mapping_version"],
                "prediction_bridge_version": (
                    None if prediction is None else prediction.get("bridge_version")
                ),
                "live_probability_model_version": (
                    LIVE_PROBABILITY_MODEL_VERSION if phase == "live" else None
                ),
                "odds_authority": "trusted_odds_winner_market_authority",
                "vision_authority": "trusted_vision_observation_authority",
            }
        ),
        "feature_availability_json": _json_text(
            {
                "pregame_probability": {
                    "available": (
                        model_probabilities is not None
                        if pregame_probability_available is None
                        else pregame_probability_available
                    ),
                    "reason": None
                    if (
                        model_probabilities is not None
                        if pregame_probability_available is None
                        else pregame_probability_available
                    )
                    else "paired_prediction_unavailable",
                },
                "pregame_authority": {
                    "draft_state_marker": (
                        None if prediction is None else prediction.get("draft_state_marker")
                    ),
                    "game_clock_seconds": (
                        None if prediction is None else prediction.get("game_clock_seconds")
                    ),
                },
                "live_probability_model": dict(
                    live_model_context
                    or {
                        "available": False,
                        "reason": "not_applicable",
                        "model_version": LIVE_PROBABILITY_MODEL_VERSION,
                    }
                ),
                "vision_clock": snapshot is not None,
                "vision_direction": snapshot is not None
                and snapshot.get("radiant_team_side") in {"team_one", "team_two"},
                "vision_networth": snapshot is not None,
                "vision_kills": snapshot is not None
                and snapshot.get("radiant_kills") is not None
                and snapshot.get("dire_kills") is not None,
                "levels": {"available": False, "reason": "not_collected"},
                "objectives": {"available": False, "reason": "not_collected"},
            }
        ),
        "reason": reason,
        "decided_at": decided_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _insert_checkpoint(
    connection: PostgresSession,
    values: Mapping[str, object],
) -> dict[str, object]:
    columns = tuple(values)
    placeholders = ", ".join("?" for _ in columns)
    with connection.transaction():
        inserted = connection.execute(
            f"""INSERT INTO map_decision_checkpoints ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(raybet_match_id, map_number, phase, checkpoint_minute,
                            strategy_version) DO NOTHING
                RETURNING {_CHECKPOINT_COLUMNS}""",
            tuple(values[column] for column in columns),
        ).fetchone()
        if inserted is not None:
            return {**_checkpoint_payload(inserted), "inserted": True}
        existing = connection.execute(
            f"""SELECT {_CHECKPOINT_COLUMNS}
                  FROM map_decision_checkpoints
                 WHERE raybet_match_id=? AND map_number=? AND phase=?
                   AND checkpoint_minute=? AND strategy_version=?""",
            (
                values["raybet_match_id"],
                values["map_number"],
                values["phase"],
                values["checkpoint_minute"],
                values["strategy_version"],
            ),
        ).fetchone()
    if existing is None:
        raise RuntimeError("map decision checkpoint persistence failed")
    return {**_checkpoint_payload(existing), "inserted": False}


def _checkpoint_payload(row: Any) -> dict[str, object]:
    payload = dict(row)
    payload["input_versions"] = json.loads(str(payload.pop("input_versions_json")))
    payload["feature_availability"] = json.loads(
        str(payload.pop("feature_availability_json"))
    )
    return payload


def _model_probabilities(
    prediction: Mapping[str, Any] | None,
    radiant_match_side: str | None,
) -> dict[str, float] | None:
    if (
        prediction is None
        or str(prediction.get("record_status")) != "paired"
        or str(prediction.get("causal_status")) != "eligible"
        or radiant_match_side not in {"team_one", "team_two"}
    ):
        return None
    value = prediction.get("p1_probability")
    if value is None:
        return None
    radiant = _probability(value)
    dire = 1.0 - radiant
    return (
        {"team_one": radiant, "team_two": dire}
        if radiant_match_side == "team_one"
        else {"team_one": dire, "team_two": radiant}
    )


def _mapping_identity(mapping: Mapping[str, Any]) -> dict[str, object]:
    match_id = str(mapping.get("raybet_match_id") or "").strip()
    map_number = int(mapping.get("map_number") or 0)
    mapping_version = int(mapping.get("version") or 0)
    if not match_id or map_number not in range(1, 6) or mapping_version <= 0:
        raise ValueError("locked Map mapping identity is invalid")
    return {
        "raybet_match_id": match_id,
        "map_number": map_number,
        "mapping_version": mapping_version,
    }


def _age_seconds(now: datetime, value: object) -> float:
    observed = _parse_utc(value, "observed_at")
    return max(0.0, (now - observed).total_seconds())


def _parse_utc(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{field} must be an ISO timestamp") from error
    return _utc(parsed, field)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: object, field: str) -> str:
    return _parse_utc(value, field).isoformat()


def _probability(value: object) -> float:
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("probability must be finite and between zero and one")
    return result


def _price(value: object) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 1.0:
        raise ValueError("price must be valid decimal odds")
    return result


def _mapping_number(value: object, key: str) -> float | None:
    return float(value[key]) if isinstance(value, Mapping) and key in value else None


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--schema-prepared", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval <= 0:
        raise ValueError("interval must be positive")
    with LiveBettingStore(args.database_url) as store:
        if not args.schema_prepared:
            store.init_schema()
        while True:
            started_at = utc_now()
            try:
                checkpoints = record_due_checkpoints(store.connection, now=started_at)
                settlements = settle_open_checkpoints(
                    store.connection, settled_at=started_at
                )
                record_health(
                    store.connection,
                    "map_decision_worker",
                    "healthy",
                    heartbeat_at=started_at,
                    success_at=started_at,
                    details={
                        "source": "worker",
                        "checkpoints": checkpoints,
                        "settlements": settlements,
                        "strategy_version": STRATEGY_VERSION,
                        "real_betting_enabled": False,
                    },
                )
                print(
                    json.dumps(
                        {"checkpoints": checkpoints, "settlements": settlements},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as error:
                failed_at = utc_now()
                record_health(
                    store.connection,
                    "map_decision_worker",
                    "degraded",
                    heartbeat_at=failed_at,
                    error_at=failed_at,
                    error=type(error).__name__,
                    details={"source": "worker", "real_betting_enabled": False},
                )
                logger.exception("Map decision checkpoint iteration failed")
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    raise SystemExit(main())


__all__ = [
    "LIVE_ODDS_MAX_AGE_SECONDS",
    "LIVE_ODDS_VISION_GAP_MAX_SECONDS",
    "LIVE_VISION_MAX_AGE_SECONDS",
    "MINIMUM_EDGE",
    "PREGAME_ODDS_MAX_AGE_SECONDS",
    "STRATEGY_VERSION",
    "checkpoint_evaluation_eligibility",
    "latest_map_checkpoints",
    "record_due_checkpoints",
    "record_live_checkpoint",
    "record_pregame_checkpoint",
    "settle_open_checkpoints",
]
