"""Read-only orchestration for one comeback shadow order per map."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .comeback import ComebackDecision, score_comeback
from .market_state import build_market_surface
from .models import ModelQuote, OddsSnapshot, RoshLineupScore, ShadowOrder
from .profiles.draft_curve import DraftCurve
from .profiles.player_form import PlayerForm
from .profiles.team_style import TeamStyleProfile
from .strategy import make_order
from .vision import VisionObservation


@dataclass(frozen=True)
class StrategyResult:
    decision: ComebackDecision
    order: ShadowOrder | None


class ComebackShadowStrategy:
    def __init__(self, *, min_edge: float = 0.08, stability_tolerance: float = 0.02) -> None:
        self.min_edge = min_edge
        self.stability_tolerance = stability_tolerance

    def evaluate(
        self,
        *,
        snapshots: list[OddsSnapshot],
        observation: VisionObservation,
        underdog_style: TeamStyleProfile,
        favorite_style: TeamStyleProfile,
        underdog_form: PlayerForm,
        favorite_form: PlayerForm,
        draft_curve: DraftCurve,
        decided_at: datetime,
        map_already_attempted: bool,
        previous_snapshots: list[OddsSnapshot] | None = None,
        previous_observation: VisionObservation | None = None,
        snapshot_observed_at: datetime | None = None,
        previous_snapshot_observed_at: datetime | None = None,
        signal_transport_key: str | None = None,
        previous_transport_key: str | None = None,
        input_refs: Mapping[str, Any] | None = None,
        rosh_lineup_score: RoshLineupScore | None = None,
    ) -> StrategyResult:
        if observation.map_number is None:
            raise ValueError("map number is required")
        latest_current_row_at = max(row.received_at for row in snapshots)
        current_snapshot_at = snapshot_observed_at or latest_current_row_at
        if decided_at != current_snapshot_at:
            raise ValueError("decided_at must equal the current transport event time")
        expected_period = f"map_{observation.map_number}"
        current_context_valid = (
            current_snapshot_at >= latest_current_row_at
            and observation.captured_at <= current_snapshot_at
            and all(
                row.raybet_match_id == observation.raybet_match_id
                and row.market.period == expected_period
                for row in snapshots
            )
        )
        previous = None
        previous_snapshot_at = None
        previous_context_valid = False
        if previous_snapshots:
            previous = build_market_surface(previous_snapshots)
            latest_previous_row_at = max(row.received_at for row in previous_snapshots)
            previous_snapshot_at = (
                previous_snapshot_observed_at or latest_previous_row_at
            )
            previous_context_valid = (
                previous_snapshot_at >= latest_previous_row_at
                and previous_observation is not None
                and all(
                    row.raybet_match_id == observation.raybet_match_id
                    and row.market.period == expected_period
                    for row in previous_snapshots
                )
            )
            if previous_observation is not None:
                previous_context_valid = previous_context_valid and (
                    previous_observation.raybet_match_id == observation.raybet_match_id
                    and previous_observation.map_number == observation.map_number
                    and previous_observation.is_confirmed
                    and previous_observation.is_paused is False
                    and previous_observation.captured_at <= previous_snapshot_at
                )
        surface = build_market_surface(snapshots, previous)
        current_transport_key = signal_transport_key
        prior_transport_key = previous_transport_key
        identity_valid = (
            bool(current_transport_key)
            and bool(prior_transport_key)
            and current_transport_key != prior_transport_key
        )
        stable = (
            previous is not None
            and previous.complete
            and surface.complete
            and current_context_valid
            and previous_context_valid
            and previous_snapshot_at is not None
            and current_snapshot_at > previous_snapshot_at
            and identity_valid
            and previous.underdog_side == surface.underdog_side
            and abs(
                previous.underdog_probability - surface.underdog_probability
            ) <= self.stability_tolerance
        )
        decision_inputs = {
            **dict(input_refs or {}),
            "transport": {
                "current_key": current_transport_key,
                "current_at": current_snapshot_at.isoformat(),
                "previous_key": prior_transport_key,
                "previous_at": (
                    previous_snapshot_at.isoformat()
                    if previous_snapshot_at is not None
                    else None
                ),
            },
            "stability": {
                "stable": stable,
                "maximum_absolute_devigged_probability_move": (
                    self.stability_tolerance
                ),
                "actual_absolute_devigged_probability_move": (
                    abs(
                        previous.underdog_probability
                        - surface.underdog_probability
                    )
                    if previous is not None
                    and previous.underdog_side == surface.underdog_side
                    else None
                ),
            },
        }
        decision = score_comeback(
            observation=observation, surface=surface,
            underdog_style=underdog_style, favorite_style=favorite_style,
            underdog_form=underdog_form, favorite_form=favorite_form,
            draft_curve=draft_curve, decided_at=current_snapshot_at,
            stable=stable, min_edge=self.min_edge,
            input_refs=decision_inputs,
            rosh_lineup_score=rosh_lineup_score,
        )
        if not identity_valid:
            decision = ComebackDecision(
                **{
                    **decision.__dict__,
                    "eligible": False,
                    "reason": "transport_identity_missing_or_reused",
                }
            )
        if map_already_attempted:
            decision = ComebackDecision(
                **{**decision.__dict__, "eligible": False, "reason": "map_already_attempted"}
            )
        if not decision.eligible:
            return StrategyResult(decision, None)
        if current_transport_key is None:
            raise AssertionError("eligible decision lacks signal transport identity")
        underdog = next(
            row for row in snapshots
            if row.market.market_type == "winner" and row.market.side == decision.underdog_side
        )
        quote = ModelQuote(
            observation.raybet_match_id, f"map_{observation.map_number}",
            underdog.market, decision.model_probability, decision.market_probability,
            decision.edge, current_snapshot_at, decision.strategy_version,
            decision.input_ref,
        )
        return StrategyResult(
            decision,
            make_order(
                quote,
                underdog,
                min_edge=self.min_edge,
                signal_transport_key=current_transport_key,
                signal_transport_at=current_snapshot_at,
                stake_multiplier=decision.stake_multiplier,
            ),
        )
