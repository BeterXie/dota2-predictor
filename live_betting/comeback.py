"""Explainable, conservative comeback probability and entry gates."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .market_state import MarketSurface
from .profiles.draft_curve import DraftCurve, DraftPoint
from .profiles.player_form import PlayerForm
from .profiles.team_style import TeamStyleProfile
from .vision import VisionObservation


STRATEGY_VERSION = "comeback-shadow-v2-strict-landmarks"


@dataclass(frozen=True)
class ComebackDecision:
    decision_key: str
    raybet_match_id: str
    map_number: int
    decided_at: datetime
    underdog_side: str
    market_probability: float
    model_probability: float
    edge: float
    data_quality: float
    eligible: bool
    reason: str
    contributions: dict[str, float]
    input_ref: str
    conservative_probability: float = 0.0
    inputs: dict[str, Any] = field(default_factory=dict)
    strategy_version: str = STRATEGY_VERSION


def _logit(probability: float) -> float:
    bounded = min(1 - 1e-6, max(1e-6, probability))
    return math.log(bounded / (1 - bounded))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _identity(
    *,
    observation: VisionObservation,
    decided_at: datetime,
    underdog_side: str,
    model_probability: float,
    reason: str,
    inputs: Mapping[str, Any],
) -> tuple[str, str]:
    payload = {
        "match": observation.raybet_match_id,
        "map": observation.map_number,
        "decided_at": decided_at.isoformat(),
        "side": underdog_side,
        "probability": round(model_probability, 10),
        "reason": reason,
        "inputs": _jsonable(inputs),
        "version": STRATEGY_VERSION,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode()
    input_ref = hashlib.sha256(canonical).hexdigest()[:24]
    decision_key = hashlib.sha256(
        f"{observation.raybet_match_id}|{observation.map_number}|{input_ref}".encode()
    ).hexdigest()[:32]
    return decision_key, input_ref


def _point_inputs(point: DraftPoint | None, wait_reason: str | None) -> dict[str, Any]:
    if point is None:
        return {"status": "wait", "reason": wait_reason}
    return {
        "status": "validated",
        "minute": point.minute,
        "radiant_probability": point.radiant_probability,
        "quality": point.quality,
        "support": point.support,
        "uncertainty": point.uncertainty,
        "calibration_ref": point.calibration_ref,
        "input_refs": list(point.input_refs),
        "validation_reason": point.validation_reason,
    }


def no_signal_decision(
    *,
    observation: VisionObservation,
    surface: MarketSurface,
    decided_at: datetime,
    reason: str,
    inputs: Mapping[str, Any] | None = None,
) -> ComebackDecision:
    """Create a durable structured decision for a pre-strategy hard gate."""
    if observation.map_number is None:
        raise ValueError("map number is required")
    merged_inputs = {
        **dict(inputs or {}),
        "market": {
            "underdog_side": surface.underdog_side,
            "underdog_price": surface.underdog_price,
            "underdog_probability": surface.underdog_probability,
            "quality": surface.quality,
            "missing_markets": list(surface.missing_markets),
        },
        "vision": {
            "captured_at": observation.captured_at.isoformat(),
            "source_frame_ref": observation.source_frame_ref,
            "game_clock_seconds": observation.game_clock_seconds,
            "radiant_team_side": observation.radiant_team_side,
        },
    }
    decision_key, input_ref = _identity(
        observation=observation,
        decided_at=decided_at,
        underdog_side=surface.underdog_side,
        model_probability=surface.underdog_probability,
        reason=reason,
        inputs=merged_inputs,
    )
    return ComebackDecision(
        decision_key=decision_key,
        raybet_match_id=observation.raybet_match_id,
        map_number=observation.map_number,
        decided_at=decided_at,
        underdog_side=surface.underdog_side,
        market_probability=surface.underdog_probability,
        model_probability=surface.underdog_probability,
        edge=0.0,
        data_quality=0.0,
        eligible=False,
        reason=reason,
        contributions={},
        input_ref=input_ref,
        conservative_probability=surface.underdog_probability,
        inputs=merged_inputs,
    )


def _conservative_positive(value: float, quality: float) -> float:
    return value * max(0.0, min(1.0, quality)) if value > 0.0 else value


def score_comeback(
    *,
    observation: VisionObservation,
    surface: MarketSurface,
    underdog_style: TeamStyleProfile,
    favorite_style: TeamStyleProfile,
    underdog_form: PlayerForm,
    favorite_form: PlayerForm,
    draft_curve: DraftCurve,
    decided_at: datetime,
    stable: bool,
    min_edge: float = 0.08,
    input_refs: Mapping[str, Any] | None = None,
) -> ComebackDecision:
    if observation.map_number is None or observation.game_clock_seconds is None:
        raise ValueError("confirmed map and game clock are required")

    point = draft_curve.at(observation.game_clock_seconds)
    draft_wait_reason = draft_curve.wait_reason(observation.game_clock_seconds)
    team_quality = min(underdog_style.quality, favorite_style.quality)
    player_quality = min(underdog_form.quality, favorite_form.quality)
    draft_quality = point.quality if point is not None else 0.0

    team_raw = (
        (underdog_style.comeback_rate - 0.18) * 1.2
        + (favorite_style.throw_rate - 0.16) * 0.8
        - (favorite_style.closeout_rate - 0.84) * 0.8
    )
    team_adjustment = team_raw * team_quality
    player_raw = (underdog_form.score - favorite_form.score) * 0.35
    player_adjustment = player_raw * player_quality

    underdog_draft_probability = 0.5
    draft_adjustment = 0.0
    conservative_draft = 0.0
    if point is not None and observation.radiant_team_side is not None:
        underdog_draft_probability = (
            point.radiant_probability
            if surface.underdog_side == observation.radiant_team_side
            else 1.0 - point.radiant_probability
        )
        draft_adjustment = (
            _logit(underdog_draft_probability) * 0.45 * draft_quality
        )
        uncertainty = float(point.uncertainty or 0.0)
        lower_probability = max(1e-6, underdog_draft_probability - uncertainty)
        uncertainty_adjusted = _logit(lower_probability) * 0.45 * draft_quality
        conservative_draft = (
            min(
                _conservative_positive(draft_adjustment, draft_quality),
                uncertainty_adjusted,
            )
            if draft_adjustment > 0.0
            else min(draft_adjustment, uncertainty_adjusted)
        )

    minute = observation.game_clock_seconds / 60.0
    late_adjustment = 0.0
    if minute >= 25:
        late_adjustment = (
            underdog_style.late_game_rate - favorite_style.late_game_rate
        ) * 0.3 * team_quality
    movement_adjustment = max(-0.08, min(0.08, surface.probability_move * 1.5))
    contributions = {
        "team_style": team_adjustment,
        "player_form": player_adjustment,
        "draft_curve": draft_adjustment,
        "late_game_style": late_adjustment,
        "market_movement": movement_adjustment,
    }
    conservative_contributions = {
        "team_style": _conservative_positive(team_adjustment, team_quality),
        "player_form": _conservative_positive(player_adjustment, player_quality),
        "draft_curve": conservative_draft,
        "late_game_style": _conservative_positive(late_adjustment, team_quality),
        # Market movement is kept separate and can never satisfy the required
        # independent team/player/draft reason below.
        "market_movement": movement_adjustment,
    }
    quality = (
        team_quality * 0.35
        + player_quality * 0.20
        + draft_quality * 0.30
        + surface.quality * 0.15
    )
    raw_adjustment = math.fsum(contributions.values())
    conservative_adjustment = math.fsum(conservative_contributions.values())
    model_probability = _sigmoid(
        _logit(surface.underdog_probability) + raw_adjustment
    )
    conservative_probability = _sigmoid(
        _logit(surface.underdog_probability) + conservative_adjustment
    )
    edge = model_probability - surface.underdog_probability
    independent_positive = (
        team_adjustment + late_adjustment > 0.0
        or player_adjustment > 0.0
        or draft_adjustment > 0.0
    )

    reason = "eligible"
    if not observation.is_confirmed:
        reason = "vision_not_confirmed"
    elif observation.radiant_team_side not in {"team_one", "team_two"}:
        reason = "team_side_not_confirmed"
    elif observation.is_paused is not False:
        reason = "stream_paused_or_unknown"
    elif not surface.complete:
        reason = "market_surface_incomplete"
    elif not 2.5 <= surface.underdog_price <= 12.0:
        reason = "odds_outside_range"
    elif not stable:
        reason = "market_not_stable_two_snapshots"
    elif point is None:
        reason = draft_wait_reason or "draft_landmark_unavailable"
    elif not point.passes_live_gate:
        reason = "draft_landmark_support_or_calibration_failed"
    elif quality < 0.2:
        reason = "insufficient_data_quality"
    elif not independent_positive:
        reason = "no_independent_positive_contribution"
    elif edge < min_edge:
        reason = "edge_below_threshold"
    elif conservative_probability <= surface.underdog_probability:
        reason = "conservative_probability_not_above_market"

    merged_inputs = {
        **dict(input_refs or {}),
        "vision": {
            "captured_at": observation.captured_at.isoformat(),
            "source_frame_ref": observation.source_frame_ref,
            "game_clock_seconds": observation.game_clock_seconds,
            "radiant_team_side": observation.radiant_team_side,
        },
        "market": {
            "underdog_side": surface.underdog_side,
            "underdog_price": surface.underdog_price,
            "underdog_probability": surface.underdog_probability,
            "probability_move": surface.probability_move,
            "kill_handicap": surface.kill_handicap,
            "total_kills": surface.total_kills,
            "duration_minutes": surface.duration_minutes,
            "quality": surface.quality,
            "missing_markets": list(surface.missing_markets),
        },
        "draft_landmark": _point_inputs(point, draft_wait_reason),
        "quality": {
            "team": team_quality,
            "player": player_quality,
            "draft": draft_quality,
            "aggregate": quality,
        },
        "conservative_contributions": conservative_contributions,
        "conservative_probability": conservative_probability,
        "independent_positive": independent_positive,
    }
    decision_key, input_ref = _identity(
        observation=observation,
        decided_at=decided_at,
        underdog_side=surface.underdog_side,
        model_probability=model_probability,
        reason=reason,
        inputs=merged_inputs,
    )
    return ComebackDecision(
        decision_key=decision_key,
        raybet_match_id=observation.raybet_match_id,
        map_number=observation.map_number,
        decided_at=decided_at,
        underdog_side=surface.underdog_side,
        market_probability=surface.underdog_probability,
        model_probability=model_probability,
        edge=edge,
        data_quality=quality,
        eligible=reason == "eligible",
        reason=reason,
        contributions=contributions,
        input_ref=input_ref,
        conservative_probability=conservative_probability,
        inputs=merged_inputs,
    )
