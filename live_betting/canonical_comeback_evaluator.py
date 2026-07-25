"""Pure policy boundary for the canonical comeback evaluator."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .comeback_entry import ComebackEntryPolicy


@dataclass(frozen=True)
class ComebackStrategyPolicy:
    minimum_underdog_odds: float = 2.5
    maximum_underdog_odds: float = 12.0
    minimum_edge: float = 0.08
    stability_tolerance: float = 0.02
    minimum_data_quality: float = 0.2
    team_comeback_baseline: float = 0.18
    team_comeback_weight: float = 1.2
    favorite_throw_baseline: float = 0.16
    favorite_throw_weight: float = 0.8
    favorite_closeout_baseline: float = 0.84
    favorite_closeout_weight: float = 0.8
    player_form_weight: float = 0.35
    lineup_logit_weight: float = 0.45
    late_game_minute: int = 25
    late_game_weight: float = 0.3
    team_quality_weight: float = 0.35
    player_quality_weight: float = 0.20
    draft_quality_weight: float = 0.30
    market_quality_weight: float = 0.15
    pure_stake_scale: float = 0.5
    pure_stake_minimum: float = 0.1
    pure_stake_maximum: float = 0.5
    player_adjusted_stake: float = 1.0
    probability_epsilon: float = 0.000001
    entry: ComebackEntryPolicy = field(default_factory=ComebackEntryPolicy)


@dataclass(frozen=True)
class PolicyEvaluation:
    observation_confirmed: bool
    team_side_confirmed: bool
    stream_unpaused: bool
    market_surface_complete: bool
    underdog_price: float
    stable_two_snapshots: bool
    situation_controllable: bool
    situation_reason: str
    rosh_lineup_available: bool
    rosh_matches_draft: bool
    rosh_minute_score_available: bool
    entry_eligible: bool
    entry_reason: str
    draft_point_available: bool
    draft_wait_reason: str | None
    draft_passes_live_gate: bool
    data_quality: float
    independent_positive: bool
    edge: float
    conservative_probability: float
    market_probability: float
    transport_identity_valid: bool = True
    map_already_attempted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_policy_reason(
    evaluation: PolicyEvaluation,
    policy: ComebackStrategyPolicy,
) -> str:
    reason = "eligible"
    if not evaluation.observation_confirmed:
        reason = "vision_not_confirmed"
    elif not evaluation.team_side_confirmed:
        reason = "team_side_not_confirmed"
    elif not evaluation.stream_unpaused:
        reason = "stream_paused_or_unknown"
    elif not evaluation.market_surface_complete:
        reason = "market_surface_incomplete"
    elif not policy.minimum_underdog_odds <= evaluation.underdog_price <= policy.maximum_underdog_odds:
        reason = "odds_outside_range"
    elif not evaluation.stable_two_snapshots:
        reason = "market_not_stable_two_snapshots"
    elif not evaluation.situation_controllable:
        reason = evaluation.situation_reason
    elif not evaluation.rosh_lineup_available:
        reason = "rosh_lineup_score_unavailable"
    elif not evaluation.rosh_matches_draft:
        reason = "rosh_lineup_draft_mismatch"
    elif not evaluation.rosh_minute_score_available:
        reason = "rosh_minute_score_unavailable"
    elif not evaluation.entry_eligible:
        reason = evaluation.entry_reason
    elif not evaluation.draft_point_available:
        reason = evaluation.draft_wait_reason or "draft_landmark_unavailable"
    elif not evaluation.draft_passes_live_gate:
        reason = "draft_landmark_support_or_calibration_failed"
    elif evaluation.data_quality < policy.minimum_data_quality:
        reason = "insufficient_data_quality"
    elif not evaluation.independent_positive:
        reason = "no_independent_positive_contribution"
    elif evaluation.edge < policy.minimum_edge:
        reason = "edge_below_threshold"
    elif evaluation.conservative_probability <= evaluation.market_probability:
        reason = "conservative_probability_not_above_market"
    if not evaluation.transport_identity_valid:
        reason = "transport_identity_missing_or_reused"
    if evaluation.map_already_attempted:
        reason = "map_already_attempted"
    return reason


def strategy_probability(
    market_probability: float,
    contributions: Mapping[str, float],
) -> float | None:
    if not 0.0 <= market_probability <= 1.0:
        return None
    try:
        values = tuple(float(value) for value in contributions.values())
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    bounded = min(1.0 - 1e-6, max(1e-6, market_probability))
    score = math.log(bounded / (1.0 - bounded)) + math.fsum(values)
    if not math.isfinite(score):
        return None
    if score >= 0.0:
        return 1.0 / (1.0 + math.exp(-score))
    exp_score = math.exp(score)
    return exp_score / (1.0 + exp_score)
