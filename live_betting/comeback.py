"""Explainable, conservative comeback probability and entry gates."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .comeback_entry import decide_comeback_entry
from .market_state import MarketSurface
from .models import RoshLineupScore
from .profiles.draft_curve import DraftCurve, DraftPoint
from .profiles.player_form import PlayerForm
from .profiles.team_style import TeamStyleProfile
from .strategy_contract import (
    DEPLOYED_STRATEGY_VERSION,
    PROPOSED_STRATEGY_VERSION,
    PolicyEvaluation,
    build_strategy_contract,
    canonical_evaluator_inputs,
    decision_identity,
    evaluate_policy_reason,
)
from .vision import VisionObservation


STRATEGY_VERSION = DEPLOYED_STRATEGY_VERSION


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
    stake_multiplier: float = 0.0
    strategy_version: str = STRATEGY_VERSION


def _logit(probability: float) -> float:
    bounded = min(1 - 1e-6, max(1e-6, probability))
    return math.log(bounded / (1 - bounded))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _observation_draft_hash(observation: VisionObservation) -> str:
    payload = json.dumps(
        {
            "radiant": list(observation.radiant_hero_ids),
            "dire": list(observation.dire_hero_ids),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _select_rosh_minute_score(
    score: RoshLineupScore,
    game_clock_seconds: int,
) -> dict[str, float | int | str] | None:
    table_key = (
        "minute_table"
        if score.scoring_mode == "player_adjusted"
        else "pure_minute_table"
    )
    raw_table = score.evidence.get(table_key)
    if not isinstance(raw_table, (list, tuple)) or not raw_table:
        return None
    rows: list[tuple[int, float, float]] = []
    seen_minutes: set[int] = set()
    for raw_row in raw_table:
        if not isinstance(raw_row, Mapping):
            return None
        minute = raw_row.get("minute")
        value = raw_row.get("win_rate_graph")
        match_percentage = raw_row.get("match_percentage")
        if (
            isinstance(minute, bool)
            or not isinstance(minute, int)
            or not 20 <= minute <= 60
            or minute in seen_minutes
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or isinstance(match_percentage, bool)
            or not isinstance(match_percentage, (int, float))
            or not math.isfinite(float(match_percentage))
            or not 0.0 <= float(match_percentage) <= 100.0
        ):
            return None
        seen_minutes.add(minute)
        rows.append((minute, float(value), float(match_percentage)))
    target_minute = min(60, game_clock_seconds // 60)
    available = tuple(row for row in rows if row[0] <= target_minute)
    if not available:
        return None
    minute, value, match_percentage = max(available, key=lambda row: row[0])
    return {
        "table": table_key,
        "minute": minute,
        "score": value,
        "match_percentage": match_percentage,
    }


def _identity(
    *,
    observation: VisionObservation,
    decided_at: datetime,
    underdog_side: str,
    model_probability: float,
    reason: str,
    inputs: Mapping[str, Any],
    strategy_version: str,
) -> tuple[str, str]:
    assert observation.map_number is not None
    return decision_identity(
        raybet_match_id=observation.raybet_match_id,
        map_number=observation.map_number,
        decided_at=decided_at,
        underdog_side=underdog_side,
        model_probability=model_probability,
        reason=reason,
        inputs=inputs,
        strategy_version=strategy_version,
    )


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
        "global_calibration_passed": point.global_calibration_passed,
        "global_gate_ref": point.global_gate_ref,
        "feature_hash": point.feature_hash,
        "model_hash": point.model_hash,
        "calibration_hash": point.calibration_hash,
        "model_version": point.model_version,
        "model_kind": point.model_kind,
        "availability_mode": point.availability_mode,
        "input_snapshot_hash": point.input_snapshot_hash,
        "landmark_key": point.landmark_key,
        "curve_key": point.curve_key,
        "deployment_key": point.deployment_key,
        "target_snapshot_hash": point.target_snapshot_hash,
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
    strategy_version: str = STRATEGY_VERSION,
) -> ComebackDecision:
    """Create a durable structured decision for a pre-strategy hard gate."""
    if observation.map_number is None:
        raise ValueError("map number is required")
    contract_ref: dict[str, Any]
    if strategy_version == PROPOSED_STRATEGY_VERSION:
        try:
            contract_ref = build_strategy_contract(
                strategy_version=strategy_version
            ).as_input_ref()
        except (RuntimeError, ValueError):
            contract_ref = {
                "status": "strategy_contract_invalid",
                "strategy_version": strategy_version,
            }
    else:
        contract_ref = {
            "status": "unregistered_deployed_contract",
            "strategy_version": strategy_version,
        }
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
        "strategy_contract": contract_ref,
    }
    decision_key, input_ref = _identity(
        observation=observation,
        decided_at=decided_at,
        underdog_side=surface.underdog_side,
        model_probability=surface.underdog_probability,
        reason=reason,
        inputs=merged_inputs,
        strategy_version=strategy_version,
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
        strategy_version=strategy_version,
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
    strategy_version: str = STRATEGY_VERSION,
    min_edge: float = 0.08,
    input_refs: Mapping[str, Any] | None = None,
    rosh_lineup_score: RoshLineupScore | None = None,
) -> ComebackDecision:
    if observation.map_number is None or observation.game_clock_seconds is None:
        raise ValueError("confirmed map and game clock are required")

    stability_contract_value = (
        input_refs.get("stability")
        if isinstance(input_refs, Mapping)
        else None
    )
    stability_tolerance = (
        stability_contract_value.get(
            "maximum_absolute_devigged_probability_move"
        )
        if isinstance(stability_contract_value, Mapping)
        else None
    )
    contract = build_strategy_contract(
        strategy_version=strategy_version,
        minimum_edge=min_edge,
        stability_tolerance=(
            float(stability_tolerance)
            if isinstance(stability_tolerance, (int, float))
            and not isinstance(stability_tolerance, bool)
            and math.isfinite(float(stability_tolerance))
            else build_strategy_contract(
                strategy_version=strategy_version
            ).policy.stability_tolerance
        ),
    )
    policy = contract.policy
    point = draft_curve.at(observation.game_clock_seconds)
    draft_wait_reason = draft_curve.wait_reason(observation.game_clock_seconds)
    team_quality = min(underdog_style.quality, favorite_style.quality)
    player_quality = min(underdog_form.quality, favorite_form.quality)
    draft_quality = point.quality if point is not None else 0.0
    rosh_matches_draft = (
        rosh_lineup_score is not None
        and rosh_lineup_score.draft_hash == _observation_draft_hash(observation)
    )
    selected_rosh_score = (
        _select_rosh_minute_score(
            rosh_lineup_score,
            observation.game_clock_seconds,
        )
        if rosh_matches_draft and rosh_lineup_score is not None
        else None
    )

    team_raw = (
        (underdog_style.comeback_rate - policy.team_comeback_baseline)
        * policy.team_comeback_weight
        + (favorite_style.throw_rate - policy.favorite_throw_baseline)
        * policy.favorite_throw_weight
        - (favorite_style.closeout_rate - policy.favorite_closeout_baseline)
        * policy.favorite_closeout_weight
    )
    team_adjustment = team_raw * team_quality
    player_raw = (
        underdog_form.score - favorite_form.score
    ) * policy.player_form_weight
    player_form_suppressed = (
        rosh_lineup_score is not None
        and rosh_lineup_score.scoring_mode == "player_adjusted"
    )
    player_adjustment = (
        0.0 if player_form_suppressed else player_raw * player_quality
    )

    underdog_draft_probability = 0.5
    lineup_adjustment = 0.0
    if selected_rosh_score is not None and observation.radiant_team_side is not None:
        radiant_probability = min(
            1.0 - 1e-6,
            max(
                policy.probability_epsilon,
                (50.0 + float(selected_rosh_score["score"])) / 100.0,
            ),
        )
        underdog_draft_probability = (
            radiant_probability
            if surface.underdog_side == observation.radiant_team_side
            else 1.0 - radiant_probability
        )
        lineup_adjustment = (
            _logit(underdog_draft_probability)
            * policy.lineup_logit_weight
            * draft_quality
        )
    conservative_lineup = _conservative_positive(
        lineup_adjustment,
        draft_quality,
    )

    minute = observation.game_clock_seconds / 60.0
    late_adjustment = 0.0
    if minute >= policy.late_game_minute:
        late_adjustment = (
            underdog_style.late_game_rate - favorite_style.late_game_rate
        ) * policy.late_game_weight * team_quality
    # The market identifies the candidate underdog and price. It is not
    # independent evidence that the live situation can reverse.
    movement_adjustment = 0.0
    contributions = {
        "team_style": team_adjustment,
        "player_form": player_adjustment,
        "draft_curve": 0.0,
        "lineup_rosh": lineup_adjustment,
        "late_game_style": late_adjustment,
        "market_movement": movement_adjustment,
    }
    conservative_contributions = {
        "team_style": _conservative_positive(team_adjustment, team_quality),
        "player_form": _conservative_positive(player_adjustment, player_quality),
        "draft_curve": 0.0,
        "lineup_rosh": conservative_lineup,
        "late_game_style": _conservative_positive(late_adjustment, team_quality),
        # Market movement is kept separate and can never satisfy the required
        # independent team/player/draft reason below.
        "market_movement": movement_adjustment,
    }
    quality = (
        team_quality * policy.team_quality_weight
        + player_quality * policy.player_quality_weight
        + draft_quality * policy.draft_quality_weight
        + surface.quality * policy.market_quality_weight
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
        or lineup_adjustment > 0.0
    )
    entry = decide_comeback_entry(
        observation,
        underdog_side=surface.underdog_side,
        rosh_underdog_probability=(
            underdog_draft_probability if selected_rosh_score is not None else None
        ),
        policy=policy.entry,
    )

    post_strategy = (
        input_refs.get("post_strategy_gates")
        if isinstance(input_refs, Mapping)
        else None
    )
    evaluation = PolicyEvaluation(
        observation_confirmed=observation.is_confirmed,
        team_side_confirmed=(
            observation.radiant_team_side in {"team_one", "team_two"}
        ),
        stream_unpaused=observation.is_paused is False,
        market_surface_complete=surface.complete,
        underdog_price=surface.underdog_price,
        stable_two_snapshots=stable,
        situation_controllable=entry.situation.controllable,
        situation_reason=entry.situation.reason,
        rosh_lineup_available=rosh_lineup_score is not None,
        rosh_matches_draft=rosh_matches_draft,
        rosh_minute_score_available=selected_rosh_score is not None,
        entry_eligible=entry.eligible,
        entry_reason=entry.reason,
        draft_point_available=point is not None,
        draft_wait_reason=draft_wait_reason,
        draft_passes_live_gate=(
            point.passes_live_gate if point is not None else False
        ),
        data_quality=quality,
        independent_positive=independent_positive,
        edge=edge,
        conservative_probability=conservative_probability,
        market_probability=surface.underdog_probability,
        transport_identity_valid=(
            post_strategy.get("transport_identity_valid", True)
            if isinstance(post_strategy, Mapping)
            else True
        ),
        map_already_attempted=(
            post_strategy.get("map_already_attempted", False)
            if isinstance(post_strategy, Mapping)
            else False
        ),
    )
    reason = evaluate_policy_reason(evaluation, policy)

    stake_multiplier = 0.0
    if selected_rosh_score is not None and rosh_lineup_score is not None:
        if rosh_lineup_score.scoring_mode == "player_adjusted":
            stake_multiplier = policy.player_adjusted_stake
        else:
            stake_multiplier = max(
                policy.pure_stake_minimum,
                min(
                    policy.pure_stake_maximum,
                    round(
                        policy.pure_stake_scale
                        * float(selected_rosh_score["match_percentage"])
                        / 100.0,
                        2,
                    ),
                ),
            )

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
            "probability_move_used_as_evidence": False,
            "kill_handicap": surface.kill_handicap,
            "total_kills": surface.total_kills,
            "duration_minutes": surface.duration_minutes,
            "quality": surface.quality,
            "missing_markets": list(surface.missing_markets),
        },
        "draft_landmark": _point_inputs(point, draft_wait_reason),
        "draft_landmark_role": {
            "calibration_gate_only": True,
            "direction_authority": False,
        },
        "rosh_lineup_score": {
            **(
                rosh_lineup_score.as_input_ref()
                if rosh_lineup_score is not None
                else {"status": "unavailable"}
            ),
            "draft_matches_observation": rosh_matches_draft,
            "stake_multiplier": stake_multiplier,
            "selected_table": (
                selected_rosh_score["table"]
                if selected_rosh_score is not None
                else None
            ),
            "selected_minute": (
                selected_rosh_score["minute"]
                if selected_rosh_score is not None
                else None
            ),
            "selected_score": (
                selected_rosh_score["score"]
                if selected_rosh_score is not None
                else None
            ),
            "match_percentage": (
                selected_rosh_score["match_percentage"]
                if selected_rosh_score is not None
                else None
            ),
            "actual_stake_multiplier": stake_multiplier,
        },
        **entry.as_inputs(),
        "player_form_suppression": {
            "suppressed": player_form_suppressed,
            "reason": (
                "included_in_player_adjusted_rosh"
                if player_form_suppressed
                else None
            ),
        },
        "quality": {
            "team": team_quality,
            "player": player_quality,
            "draft": draft_quality,
            "aggregate": quality,
        },
        "conservative_contributions": conservative_contributions,
        "conservative_probability": conservative_probability,
        "independent_positive": independent_positive,
        "strategy_contract": contract.as_input_ref(),
        "strategy_evaluation": evaluation.as_dict(),
        "canonical_evaluator_inputs": canonical_evaluator_inputs(
            observation=observation,
            surface=surface,
            underdog_style=underdog_style,
            favorite_style=favorite_style,
            underdog_form=underdog_form,
            favorite_form=favorite_form,
            draft_curve=draft_curve,
            decided_at=decided_at,
            stable=stable,
            input_refs=dict(input_refs or {}),
            rosh_lineup_score=rosh_lineup_score,
        ),
    }
    decision_key, input_ref = _identity(
        observation=observation,
        decided_at=decided_at,
        underdog_side=surface.underdog_side,
        model_probability=model_probability,
        reason=reason,
        inputs=merged_inputs,
        strategy_version=contract.strategy_version,
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
        stake_multiplier=stake_multiplier,
        strategy_version=contract.strategy_version,
    )
