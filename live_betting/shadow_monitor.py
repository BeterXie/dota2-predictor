"""Read-only live monitor that records comeback decisions and shadow fills."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .alignment import align_snapshots
from .comeback import no_signal_decision
from .health import record_health
from .market_state import build_market_surface
from .models import Market, OddsSnapshot, ShadowOrder
from .profiles import (
    PlayerForm,
    TeamStyleProfile,
    build_draft_curve,
)
from .shadow_strategy import ComebackShadowStrategy
from .storage import LiveBettingStore
from .strict_eligibility import query_strict_live_eligibility
from .vision import VisionObservation, read_jsonl


logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MAX_VISION_AGE = timedelta(seconds=30)
MAX_ODDS_TRANSPORT_AGE = timedelta(seconds=15)


@dataclass(frozen=True)
class TransportRef:
    observation_key: str
    observed_at: datetime


@dataclass(frozen=True)
class _VersionedTeamStyle(TeamStyleProfile):
    profile_cutoff: str = ""
    profile_version: str = ""
    input_hash: str = ""
    effective_sample_size: float = 0.0


@dataclass(frozen=True)
class _VersionedPlayerForm(PlayerForm):
    score_refs: tuple[tuple[int, int, str, str, str], ...] = ()
    cutoff: str = ""


def _snapshot(row: sqlite3.Row) -> OddsSnapshot:
    market = Market(
        str(row["market_type"]), str(row["period"]), row["side"], row["line"],
        str(row["outcome_key"]), bool(row["supported"]),
    )
    return OddsSnapshot(
        str(row["raybet_match_id"]), str(row["odds_id"]), row["odds_group_id"],
        datetime.fromisoformat(str(row["received_at"])), float(row["price"]),
        row["status"], market, row["last_update"], json.loads(row["raw_json"]),
    )


def latest_market_state(
    connection: sqlite3.Connection, match_id: str, map_number: int,
    *, as_of: datetime | None = None,
) -> list[OddsSnapshot]:
    as_of = as_of or datetime.now(timezone.utc)
    period = f"map_{map_number}"
    rows = connection.execute(
        """WITH ranked AS (
               SELECT o.*, ROW_NUMBER() OVER (
                   PARTITION BY odds_id ORDER BY received_at DESC, id DESC
               ) AS state_rank
               FROM odds_snapshots o
               WHERE raybet_match_id=? AND period=? AND received_at<=?
           )
           SELECT * FROM ranked o
           WHERE state_rank=1
             AND o.status IN ('1', '5', 'open', 'active', 'running')""",
        (match_id, period, as_of.isoformat()),
    ).fetchall()
    snapshots = [_snapshot(row) for row in rows]
    groups: dict[str, list[OddsSnapshot]] = {}
    for row in snapshots:
        if row.market.market_type == "winner" and row.odds_group_id:
            groups.setdefault(row.odds_group_id, []).append(row)
    complete = [rows for rows in groups.values()
                if {row.market.side for row in rows} == {"team_one", "team_two"}]
    if not complete:
        return []
    winner_group = max(complete, key=lambda group: max(row.received_at for row in group))
    winner_ids = {row.odds_id for row in winner_group}
    return [row for row in snapshots
            if row.market.market_type != "winner" or row.odds_id in winner_ids]


def _observation(row: sqlite3.Row) -> VisionObservation:
    return VisionObservation(
        str(row["raybet_match_id"]), row["map_number"],
        datetime.fromisoformat(str(row["captured_at"])), row["game_clock_seconds"],
        None if row["is_paused"] is None else bool(row["is_paused"]),
        tuple(json.loads(row["radiant_hero_ids"])),
        tuple(json.loads(row["dire_hero_ids"])), float(row["clock_confidence"]),
        float(row["draft_confidence"]), str(row["source_frame_ref"]),
        str(row["screen_state"]), row["radiant_team_side"],
    )


def _persist_decision(store: LiveBettingStore, decision: Any) -> bool:
    """Persist full inputs without changing the public numeric contributions."""
    inputs = getattr(decision, "inputs", None)
    if inputs is None or not hasattr(decision, "contributions"):
        return store.insert_decision(decision)
    audit_contributions: dict[str, Any] = {
        **decision.contributions,
        "__inputs__": inputs,
    }
    conservative = inputs.get("conservative_contributions")
    if conservative is not None:
        audit_contributions["__conservative__"] = conservative
    return store.insert_decision(
        replace(decision, contributions=audit_contributions)
    )


def ingest_vision(store: LiveBettingStore, path: Path) -> int:
    if not path.exists():
        return 0
    paths = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
    return sum(
        store.insert_vision_observation(row)
        for item in paths
        for row in read_jsonl(item)
    )


def persist_alignments(store: LiveBettingStore, match_id: str) -> int:
    odds_rows = store.connection.execute(
        """SELECT o.* FROM odds_snapshots o LEFT JOIN odds_alignments a
             ON a.odds_snapshot_id=o.id
           WHERE o.raybet_match_id=? AND a.odds_snapshot_id IS NULL
           ORDER BY o.received_at, o.id LIMIT 2000""",
        (match_id,),
    ).fetchall()
    observations = [_observation(row) for row in store.connection.execute(
        """SELECT * FROM vision_observations WHERE raybet_match_id=?
           ORDER BY captured_at""", (match_id,)
    )]
    aligned = align_snapshots(
        [(int(row["id"]), _snapshot(row)) for row in odds_rows], observations
    )
    return sum(store.insert_alignment(row) for row in aligned)


def _neutral_style() -> TeamStyleProfile:
    return TeamStyleProfile(0, 0, 0.18, 0.16, 0.84, 0.35, 36.0, 0.0)


def _neutral_form() -> PlayerForm:
    return PlayerForm((), 0.0, {}, 0, 0.0)


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _latest_versioned_style(
    connection: sqlite3.Connection,
    team_id: int,
    cutoff: datetime,
) -> TeamStyleProfile:
    cutoff_iso = cutoff.isoformat()
    try:
        row = connection.execute(
            """SELECT * FROM team_style_profiles
               WHERE team_id=? AND profile_cutoff<=? AND created_at<=?
               ORDER BY profile_cutoff DESC, created_at DESC, profile_id DESC
               LIMIT 1""",
            (team_id, cutoff_iso, cutoff_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return _neutral_style()
    if row is None:
        return _neutral_style()
    try:
        rates = {
            str(item["metric"]): item
            for item in json.loads(str(row["posterior_rates_json"]))
        }
        durations = {
            str(item["group"]): item
            for item in json.loads(str(row["duration_quantiles_json"]))
        }
        weighting = json.loads(str(row["weighting_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _neutral_style()

    def rate(metric: str, default: float) -> float:
        value = rates.get(metric, {}).get("mean")
        return float(value) if isinstance(value, (int, float)) else default

    duration_values = [
        float(value["p50"])
        for key, value in durations.items()
        if key in {"win", "loss", "even"}
        and isinstance(value.get("p50"), (int, float))
    ]
    effective_sample_size = max(0.0, float(row["effective_sample_size"]))
    quality = min(1.0, (effective_sample_size / 100.0) ** 0.5)
    maps = weighting.get("maps", []) if isinstance(weighting, dict) else []
    return _VersionedTeamStyle(
        team_id=team_id,
        matches=len(maps) if isinstance(maps, list) else 0,
        comeback_rate=rate("comeback_after_5000_deficit", 0.18),
        throw_rate=rate("throw_after_5000_lead", 0.16),
        closeout_rate=rate("closeout_after_5000_lead", 0.84),
        late_game_rate=rate("reach_40_minutes", 0.35),
        average_duration_minutes=(
            sum(duration_values) / len(duration_values) / 60.0
            if duration_values
            else 36.0
        ),
        quality=quality,
        profile_cutoff=str(row["profile_cutoff"]),
        profile_version=str(row["profile_version"]),
        input_hash=str(row["input_hash"]),
        effective_sample_size=effective_sample_size,
    )


def _latest_completed_roster(
    connection: sqlite3.Connection,
    team_id: int,
    cutoff_epoch: int,
) -> tuple[int, ...]:
    try:
        cutoff_iso = datetime.fromtimestamp(
            cutoff_epoch, tz=timezone.utc
        ).isoformat()
        row = connection.execute(
            """SELECT m.match_id
               FROM formal_map_eligibility AS f
               JOIN matches AS m ON m.match_id=f.match_id
               JOIN match_ingest_status AS status ON status.match_id=m.match_id
               JOIN raw_source_artifacts AS artifact
                 ON artifact.artifact_id=status.latest_raw_artifact_id
                AND artifact.content_hash=status.latest_raw_content_hash
               WHERE (m.radiant_team_id=? OR m.dire_team_id=?)
                 AND m.duration IS NOT NULL
                 AND m.start_time + m.duration < ?
                 AND status.player_readiness='ready'
                 AND artifact.first_usable_at IS NOT NULL
                 AND artifact.first_usable_at<=?
               ORDER BY m.start_time + m.duration DESC, m.match_id DESC
               LIMIT 1""",
            (team_id, team_id, cutoff_epoch, cutoff_iso),
        ).fetchone()
    except sqlite3.OperationalError:
        return ()
    if row is None:
        return ()
    players = connection.execute(
        """SELECT account_id FROM match_players
           WHERE match_id=? AND team_id=? AND account_id IS NOT NULL
           ORDER BY player_slot""",
        (int(row["match_id"]), team_id),
    ).fetchall()
    roster = tuple(int(player["account_id"]) for player in players)
    return (
        roster
        if len(roster) == 5
        and len(set(roster)) == 5
        and all(account_id > 0 for account_id in roster)
        else ()
    )


def _versioned_player_form(
    connection: sqlite3.Connection,
    account_ids: tuple[int, ...],
    cutoff: datetime,
    *,
    half_life_days: float = 30.0,
) -> PlayerForm:
    if not account_ids:
        return _neutral_form()
    cutoff_epoch = int(cutoff.timestamp())
    cutoff_iso = cutoff.isoformat()
    placeholders = ",".join("?" for _ in account_ids)
    try:
        rows = connection.execute(
            f"""WITH available AS (
                   SELECT score.*, m.start_time + m.duration AS completed_at,
                          ROW_NUMBER() OVER (
                              PARTITION BY score.account_id, score.match_id,
                                           score.player_slot
                              ORDER BY score.created_at DESC, score.score_id DESC
                          ) AS version_rank
                   FROM player_map_scores AS score
                   JOIN formal_map_eligibility AS f ON f.match_id=score.match_id
                   JOIN matches AS m ON m.match_id=score.match_id
                   JOIN match_ingest_status AS status
                     ON status.match_id=score.match_id
                   WHERE score.account_id IN ({placeholders})
                     AND score.position IS NOT NULL
                     AND m.duration IS NOT NULL
                     AND m.start_time + m.duration < ?
                     AND score.created_at<=?
                     AND score.benchmark_cutoff<=?
                     AND status.player_readiness='ready'
               ), ranked AS (
                   SELECT available.*, ROW_NUMBER() OVER (
                       PARTITION BY account_id
                       ORDER BY completed_at DESC, match_id DESC, player_slot
                   ) AS recent_rank
                   FROM available WHERE version_rank=1
               )
               SELECT * FROM ranked WHERE recent_rank<=20
               ORDER BY completed_at DESC, match_id DESC, player_slot""",
            (*account_ids, cutoff_epoch, cutoff_iso, cutoff_iso),
        ).fetchall()
    except sqlite3.OperationalError:
        return _neutral_form()
    if not rows:
        return _neutral_form()

    weighted_values: list[tuple[float, float, str]] = []
    refs: list[tuple[int, int, str, str, str]] = []
    for row in rows:
        age_days = max(0.0, (cutoff_epoch - int(row["completed_at"])) / 86400.0)
        time_weight = 0.5 ** (age_days / half_life_days)
        reliability = max(
            0.0,
            min(1.0, float(row["coverage"]) * float(row["role_confidence"])),
        )
        weight = time_weight * reliability
        if weight <= 0.0:
            continue
        normalized = max(
            -1.0, min(1.0, (float(row["execution_score"]) - 50.0) / 50.0)
        )
        role = f"position_{int(row['position'])}"
        weighted_values.append((normalized, weight, role))
        refs.append(
            (
                int(row["match_id"]),
                int(row["player_slot"]),
                str(row["input_hash"]),
                str(row["score_version"]),
                str(row["created_at"]),
            )
        )
    if not weighted_values:
        return _neutral_form()
    total_weight = sum(weight for _, weight, _ in weighted_values)
    score = sum(value * weight for value, weight, _ in weighted_values) / total_weight
    role_scores = {}
    for role in {item[2] for item in weighted_values}:
        selected = [item for item in weighted_values if item[2] == role]
        role_scores[role] = sum(value * weight for value, weight, _ in selected) / sum(
            weight for _, weight, _ in selected
        )
    # Current starters are not independently confirmed by RayBet. The latest
    # earlier completed strict roster is therefore useful but deliberately weak.
    coverage = min(1.0, (len(weighted_values) / (len(account_ids) * 20.0)) ** 0.5)
    quality = coverage * 0.35
    return _VersionedPlayerForm(
        account_ids=account_ids,
        score=score,
        role_scores=role_scores,
        matches=len(weighted_values),
        quality=quality,
        score_refs=tuple(refs),
        cutoff=cutoff_iso,
    )


def _profiles(
    connection: sqlite3.Connection, team_id: int | None, as_of: int
) -> tuple[TeamStyleProfile, PlayerForm]:
    if team_id is None:
        return _neutral_style(), _neutral_form()
    cutoff = datetime.fromtimestamp(as_of, tz=timezone.utc)
    roster = _latest_completed_roster(connection, team_id, as_of)
    return (
        _latest_versioned_style(connection, team_id, cutoff),
        _versioned_player_form(connection, roster, cutoff),
    )


def _profile_refs(
    style: TeamStyleProfile,
    form: PlayerForm,
) -> dict[str, Any]:
    style_refs = (
        {
            "team_id": style.team_id,
            "profile_cutoff": style.profile_cutoff,
            "profile_version": style.profile_version,
            "input_hash": style.input_hash,
            "effective_sample_size": style.effective_sample_size,
        }
        if isinstance(style, _VersionedTeamStyle)
        else {"team_id": style.team_id, "status": "versioned_profile_unavailable"}
    )
    form_refs = (
        {
            "account_ids": list(form.account_ids),
            "cutoff": form.cutoff,
            "score_refs": [
                {
                    "match_id": match_id,
                    "player_slot": player_slot,
                    "input_hash": input_hash,
                    "score_version": score_version,
                    "created_at": created_at,
                }
                for match_id, player_slot, input_hash, score_version, created_at
                in form.score_refs
            ],
        }
        if isinstance(form, _VersionedPlayerForm)
        else {"account_ids": list(form.account_ids), "status": "versioned_scores_unavailable"}
    )
    return {"team_style": style_refs, "player_form": form_refs}


def _shadow_order(row: sqlite3.Row) -> ShadowOrder:
    market_type, period, side, line = str(row["market_key"]).split("|", 3)
    outcome_key = str(row["signal_outcome_key"] or side)
    market = Market(
        market_type,
        period,
        side or None,
        float(line) if line else None,
        outcome_key,
        True,
    )
    return ShadowOrder(
        order_key=str(row["order_key"]),
        raybet_match_id=str(row["raybet_match_id"]),
        odds_id=str(row["odds_id"]),
        market=market,
        signaled_at=datetime.fromisoformat(str(row["signaled_at"])),
        model_probability=float(row["model_probability"]),
        market_probability=float(row["market_probability"]),
        signal_price=float(row["signal_price"]),
        signal_transport_key=str(row["signal_transport_key"]),
        signal_transport_at=datetime.fromisoformat(str(row["signal_transport_at"])),
        expires_at=datetime.fromisoformat(str(row["expires_at"])),
        signal_odds_group_id=row["signal_odds_group_id"],
        signal_outcome_key=row["signal_outcome_key"],
        signal_identity_verified=bool(row["signal_identity_verified"]),
        stake=float(row["stake"]),
        status=str(row["status"]),
        fill_price=row["fill_price"],
        filled_at=(
            datetime.fromisoformat(str(row["filled_at"]))
            if row["filled_at"]
            else None
        ),
        rejection_reason=row["rejection_reason"],
    )


def _pending_orders(store: LiveBettingStore) -> list[ShadowOrder]:
    rows = store.connection.execute(
        """SELECT o.* FROM shadow_orders o JOIN shadow_map_attempts a
             ON a.order_key=o.order_key
           WHERE o.status='pending' ORDER BY o.signaled_at, o.order_key"""
    ).fetchall()
    return [_shadow_order(row) for row in rows]


def _process_pending_order(
    store: LiveBettingStore, *, as_of: datetime,
) -> ShadowOrder | None:
    for pending in _pending_orders(store):
        watermark = store.processed_transport_watermark(
            pending.raybet_match_id, as_of=as_of
        )
        if watermark is None:
            continue
        resolved = store.process_pending_successor(pending, watermark=watermark)
        if resolved is not None:
            return resolved
    return None


def _transport_refs(
    connection: sqlite3.Connection,
    match_id: str,
    as_of: datetime,
) -> list[TransportRef]:
    rows = connection.execute(
        """SELECT observation_key, observed_at FROM odds_transport_observations
           WHERE raybet_match_id=? AND observed_at<=?
              AND timing_status='on_time' AND processing_status='processed'
           ORDER BY observed_at DESC, observation_key DESC LIMIT 2""",
        (match_id, as_of.isoformat()),
    ).fetchall()
    return [
        TransportRef(
            str(row["observation_key"]),
            datetime.fromisoformat(str(row["observed_at"])),
        )
        for row in rows
    ]


def market_state_for_transport(
    connection: sqlite3.Connection,
    transport: TransportRef,
    match_id: str,
    map_number: int,
) -> list[OddsSnapshot]:
    """Read only the exact normalized membership of one captured response."""
    period = f"map_{map_number}"
    try:
        rows = connection.execute(
            """SELECT * FROM odds_response_outcomes
               WHERE observation_key=? AND raybet_match_id=? AND period=?""",
            (transport.observation_key, match_id, period),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_snapshot(row) for row in rows]


def _aligned_transport_observation(
    snapshots: list[OddsSnapshot],
    observed_at: datetime,
    observations: list[VisionObservation],
) -> tuple[VisionObservation | None, str]:
    synthetic = [
        (-(index + 1), replace(snapshot, received_at=observed_at))
        for index, snapshot in enumerate(snapshots)
    ]
    aligned = align_snapshots(synthetic, observations)
    unusable = next((row for row in aligned if not row.usable), None)
    if unusable is not None:
        return None, unusable.reason
    anchors = {row.observation_captured_at for row in aligned}
    clocks = {row.game_clock_seconds for row in aligned}
    maps = {row.map_number for row in aligned}
    if len(anchors) != 1 or len(clocks) != 1 or len(maps) != 1:
        return None, "inconsistent_transport_alignment"
    anchor_at = next(iter(anchors), None)
    game_clock = next(iter(clocks), None)
    map_number = next(iter(maps), None)
    anchor = next(
        (
            row for row in observations
            if row.captured_at == anchor_at and row.map_number == map_number
            and row.is_confirmed and row.screen_state == "game"
        ),
        None,
    )
    if anchor is None or game_clock is None:
        return None, "aligned_observation_missing"
    return replace(anchor, game_clock_seconds=game_clock), "ok"


def run_once(
    store: LiveBettingStore,
    strategy: ComebackShadowStrategy,
    vision_path: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    run_at = now or datetime.now(timezone.utc)
    pending = _process_pending_order(store, as_of=run_at)
    if pending is not None:
        return {"status": f"shadow_{pending.status}", "order_key": pending.order_key}

    ingested = ingest_vision(store, vision_path)
    row = store.connection.execute(
        """SELECT * FROM vision_observations WHERE confirmed=1 AND screen_state='game'
             AND captured_at<=?
           ORDER BY captured_at DESC LIMIT 1""",
        (run_at.isoformat(),),
    ).fetchone()
    if not row:
        return {"status": "waiting_for_confirmed_vision", "vision_ingested": ingested}
    latest_observation = _observation(row)
    match_id = latest_observation.raybet_match_id
    transports = _transport_refs(store.connection, match_id, run_at)
    if not transports:
        return {"status": "waiting_for_odds_transport"}
    current_transport = transports[0]
    current_transport_at = current_transport.observed_at
    if run_at - current_transport_at > MAX_ODDS_TRANSPORT_AGE:
        return {"status": "waiting_for_fresh_odds"}

    causal_row = store.connection.execute(
        """SELECT * FROM vision_observations
           WHERE raybet_match_id=? AND confirmed=1 AND screen_state='game'
             AND captured_at<=?
           ORDER BY captured_at DESC LIMIT 1""",
        (match_id, current_transport_at.isoformat()),
    ).fetchone()
    if not causal_row:
        return {
            "status": "waiting_for_usable_alignment",
            "reason": "no_prior_confirmed_observation",
        }
    map_number = int(causal_row["map_number"])
    aligned = persist_alignments(store, match_id)
    snapshots = market_state_for_transport(
        store.connection, current_transport, match_id, map_number
    )
    if not snapshots:
        return {
            "status": "waiting_for_exact_transport_market",
            "reason": "exact_response_membership_missing",
            "transport_key": current_transport.observation_key,
            "aligned": aligned,
        }
    try:
        surface = build_market_surface(snapshots)
    except ValueError:
        return {
            "status": "waiting_for_complete_winner_market",
            "transport_key": current_transport.observation_key,
            "aligned": aligned,
        }

    observations = [_observation(item) for item in store.connection.execute(
        """SELECT * FROM vision_observations WHERE raybet_match_id=?
           ORDER BY captured_at""",
        (match_id,),
    )]
    observation, alignment_reason = _aligned_transport_observation(
        snapshots, current_transport_at, observations
    )
    if observation is None:
        return {
            "status": "waiting_for_usable_alignment",
            "reason": alignment_reason,
            "aligned": aligned,
        }
    if run_at - observation.captured_at > MAX_VISION_AGE:
        return {"status": "waiting_for_fresh_vision"}

    strict = query_strict_live_eligibility(
        store.connection,
        raybet_match_id=match_id,
        map_number=map_number,
        transport_observed_at=current_transport_at,
    )
    strict_inputs: dict[str, Any] = {
        "strict_live_eligibility": {
            "eligible": strict.eligible,
            "reason": strict.reason,
            "mapping_refs": strict.input_refs(),
        },
        "transport": {
            "current_key": current_transport.observation_key,
            "current_at": current_transport_at.isoformat(),
        },
    }
    if not strict.eligible or strict.mapping is None:
        decision = no_signal_decision(
            observation=observation,
            surface=surface,
            decided_at=current_transport_at,
            reason=f"strict_live_ineligible:{strict.reason}",
            inputs=strict_inputs,
        )
        _persist_decision(store, decision)
        return {
            "status": "no_signal",
            "reason": "strict_live_ineligible",
            "reason_code": strict.reason,
            "decision_key": decision.decision_key,
            "inputs": decision.inputs,
        }

    as_of = int(current_transport_at.timestamp())
    draft = build_draft_curve(
        store.connection,
        observation.radiant_hero_ids,
        observation.dire_hero_ids,
        as_of,
    )
    active_draft = draft.at(observation.game_clock_seconds or 0)
    if active_draft is None:
        draft_reason = draft.wait_reason(observation.game_clock_seconds or 0)
        draft_inputs = {
            **strict_inputs,
            "draft_curve": {
                "source_ref": draft.source_ref,
                "unavailable_reason": draft.unavailable_reason,
                "selection_reason": draft_reason,
            },
        }
        decision = no_signal_decision(
            observation=observation,
            surface=surface,
            decided_at=current_transport_at,
            reason=f"draft_landmark_unavailable:{draft_reason}",
            inputs=draft_inputs,
        )
        _persist_decision(store, decision)
        return {
            "status": "no_signal",
            "reason": "draft_landmark_unavailable",
            "reason_code": draft_reason,
            "decision_key": decision.decision_key,
            "inputs": decision.inputs,
        }

    previous_snapshots = None
    previous_observation = None
    previous_transport_key = None
    previous_transport_at = None
    if len(transports) == 2:
        candidate = transports[1]
        candidate_at = candidate.observed_at
        candidate_snapshots = market_state_for_transport(
            store.connection, candidate, match_id, map_number
        )
        if candidate_snapshots:
            candidate_observation, _ = _aligned_transport_observation(
                candidate_snapshots, candidate_at, observations
            )
            if candidate_observation is not None:
                previous_snapshots = candidate_snapshots
                previous_observation = candidate_observation
                previous_transport_key = candidate.observation_key
                previous_transport_at = candidate_at

    team_ids = [
        strict.mapping.canonical_team_one_id,
        strict.mapping.canonical_team_two_id,
    ]
    styles, forms = zip(*[_profiles(store.connection, team_id, as_of) for team_id in team_ids])
    surface_underdog = surface.underdog_side
    underdog_index = 0 if surface_underdog == "team_one" else 1
    favorite_index = 1 - underdog_index
    intelligence_refs = {
        **strict_inputs,
        "team_one_intelligence": _profile_refs(styles[0], forms[0]),
        "team_two_intelligence": _profile_refs(styles[1], forms[1]),
        "draft_curve": {
            "source_ref": draft.source_ref,
            "unavailable_reason": draft.unavailable_reason,
        },
    }
    result = strategy.evaluate(
        snapshots=snapshots, observation=observation,
        underdog_style=styles[underdog_index], favorite_style=styles[favorite_index],
        underdog_form=forms[underdog_index], favorite_form=forms[favorite_index],
        draft_curve=draft, decided_at=current_transport_at,
        map_already_attempted=store.has_map_attempt(match_id, map_number),
        previous_snapshots=previous_snapshots,
        previous_observation=previous_observation,
        snapshot_observed_at=current_transport_at,
        previous_snapshot_observed_at=previous_transport_at,
        signal_transport_key=current_transport.observation_key,
        previous_transport_key=previous_transport_key,
        input_refs=intelligence_refs,
    )
    _persist_decision(store, result.decision)
    if result.order and store.insert_map_order(result.order, map_number):
        return {
            "status": "shadow_pending", "order_key": result.order.order_key,
            "model_probability": result.decision.model_probability,
            "market_probability": result.decision.market_probability,
            "edge": result.decision.edge,
            "inputs": result.decision.inputs,
        }
    return {
        "status": "no_signal", "reason": result.decision.reason,
        "edge": result.decision.edge, "quality": result.decision.data_quality,
        "decision_key": result.decision.decision_key,
        "inputs": result.decision.inputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "data" / "dota2.db")
    parser.add_argument("--vision-jsonl", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    strategy = ComebackShadowStrategy()
    with LiveBettingStore(args.database) as store:
        store.init_schema()
        started_at = datetime.now(timezone.utc)
        record_health(
            store.connection,
            "shadow_worker",
            "starting",
            heartbeat_at=started_at,
            details={"source": "worker"},
        )
        while True:
            try:
                result = run_once(store, strategy, args.vision_jsonl)
                succeeded_at = datetime.now(timezone.utc)
                record_health(
                    store.connection,
                    "shadow_worker",
                    "healthy",
                    heartbeat_at=succeeded_at,
                    success_at=succeeded_at,
                    details={
                        "source": "worker",
                        "run_status": str(result.get("status", "unknown")),
                    },
                )
                print(json.dumps(result, ensure_ascii=False, default=str))
            except Exception as error:
                failed_at = datetime.now(timezone.utc)
                record_health(
                    store.connection,
                    "shadow_worker",
                    "degraded",
                    heartbeat_at=failed_at,
                    error_at=failed_at,
                    error=type(error).__name__,
                    details={"source": "worker"},
                )
                logger.exception("shadow monitor iteration failed")
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
