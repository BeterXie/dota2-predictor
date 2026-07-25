"""Fail-closed live situation and entry gates for comeback candidates."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from contracts.live_observation import is_canonical_net_worth_bucket


@dataclass(frozen=True)
class ComebackEntryPolicy:
    minimum_clock_seconds: int = 20 * 60
    maximum_clock_seconds: int = 45 * 60
    minimum_net_worth_deficit: int = 1_000
    maximum_net_worth_deficit: int = 10_000
    minimum_kill_deficit: int = 2
    maximum_kill_deficit: int = 10
    minimum_vision_confidence: float = 0.9


@dataclass(frozen=True)
class ComebackSituationDecision:
    controllable: bool
    reason: str
    source_status: str
    source: str | None
    confidence: float
    underdog_side: str
    underdog_kills: int | None = None
    opponent_kills: int | None = None
    kill_deficit: int | None = None
    underdog_net_worth: int | None = None
    opponent_net_worth: int | None = None
    net_worth_deficit: int | None = None
    net_worth_advantage_side: str | None = None
    net_worth_deficit_min: int | None = None
    net_worth_deficit_max: int | None = None
    unavailable_reason: str | None = None

    def as_input(self) -> dict[str, Any]:
        return {
            "controllable": self.controllable,
            "reason": self.reason,
            "source_status": self.source_status,
            "source": self.source,
            "confidence": self.confidence,
            "underdog_side": self.underdog_side,
            "underdog_kills": self.underdog_kills,
            "opponent_kills": self.opponent_kills,
            "kill_deficit": self.kill_deficit,
            "underdog_net_worth": self.underdog_net_worth,
            "opponent_net_worth": self.opponent_net_worth,
            "net_worth_deficit": self.net_worth_deficit,
            "net_worth_advantage_side": self.net_worth_advantage_side,
            "net_worth_deficit_min": self.net_worth_deficit_min,
            "net_worth_deficit_max": self.net_worth_deficit_max,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class ComebackEntryDecision:
    eligible: bool
    reason: str
    game_clock_seconds: int | None
    rosh_underdog_probability: float | None
    situation: ComebackSituationDecision
    policy: ComebackEntryPolicy

    def as_inputs(self) -> dict[str, Any]:
        return {
            "comeback_state": self.situation.as_input(),
            "entry_window": {
                "minimum_clock_seconds": self.policy.minimum_clock_seconds,
                "maximum_clock_seconds": self.policy.maximum_clock_seconds,
                "game_clock_seconds": self.game_clock_seconds,
                "inside": (
                    isinstance(self.game_clock_seconds, int)
                    and self.policy.minimum_clock_seconds
                    <= self.game_clock_seconds
                    <= self.policy.maximum_clock_seconds
                ),
            },
            "comeback_entry": {
                "eligible": self.eligible,
                "reason": self.reason,
                "rosh_underdog_probability": self.rosh_underdog_probability,
                "policy": {
                    "minimum_clock_seconds": self.policy.minimum_clock_seconds,
                    "maximum_clock_seconds": self.policy.maximum_clock_seconds,
                    "minimum_kill_deficit": self.policy.minimum_kill_deficit,
                    "maximum_kill_deficit": self.policy.maximum_kill_deficit,
                    "minimum_net_worth_deficit": (
                        self.policy.minimum_net_worth_deficit
                    ),
                    "maximum_net_worth_deficit": (
                        self.policy.maximum_net_worth_deficit
                    ),
                    "minimum_vision_confidence": (
                        self.policy.minimum_vision_confidence
                    ),
                },
            },
        }


def _value(value: object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _state(observation: object) -> object | None:
    return getattr(observation, "comeback_state", None)


def _unavailable(
    underdog_side: str,
    reason: str,
    *,
    source_status: str = "missing",
    source: str | None = None,
    confidence: float = 0.0,
    unavailable_reason: str | None = None,
) -> ComebackSituationDecision:
    return ComebackSituationDecision(
        controllable=False,
        reason=reason,
        source_status=source_status,
        source=source,
        confidence=confidence,
        underdog_side=underdog_side,
        unavailable_reason=unavailable_reason,
    )


def assess_comeback_situation(
    observation: object,
    *,
    underdog_side: str,
    policy: ComebackEntryPolicy = ComebackEntryPolicy(),
) -> ComebackSituationDecision:
    """Require a current HUD frame proving a material but controlled deficit."""
    if underdog_side not in {"team_one", "team_two"}:
        return _unavailable(underdog_side, "invalid_underdog_side")
    state = _state(observation)
    if state is None:
        return _unavailable(underdog_side, "vision_live_situation_missing")
    if getattr(observation, "screen_state", None) != "game":
        return _unavailable(
            underdog_side,
            "vision_live_situation_not_game_frame",
        )
    status = _value(state, "status")
    source = _value(state, "source")
    raw_confidence = _value(state, "confidence")
    confidence = (
        float(raw_confidence)
        if not isinstance(raw_confidence, bool)
        and isinstance(raw_confidence, (int, float))
        and math.isfinite(float(raw_confidence))
        else 0.0
    )
    unavailable_reason = _value(state, "unavailable_reason")
    if status == "unavailable":
        detail = (
            str(unavailable_reason).strip()
            if isinstance(unavailable_reason, str) and unavailable_reason.strip()
            else "unspecified"
        )
        return _unavailable(
            underdog_side,
            f"vision_live_situation_unavailable:{detail}",
            source_status="unavailable",
            source=None,
            confidence=0.0,
            unavailable_reason=detail,
        )
    if status != "available" or source != "vision_hud":
        return _unavailable(
            underdog_side,
            "vision_live_situation_invalid",
            source_status=str(status or "invalid"),
            source=str(source) if source is not None else None,
            confidence=confidence,
        )
    if confidence < policy.minimum_vision_confidence:
        return _unavailable(
            underdog_side,
            "vision_live_situation_low_confidence",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )

    names = ("radiant_kills", "dire_kills")
    values = {name: _value(state, name) for name in names}
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values.values()
    ):
        return _unavailable(
            underdog_side,
            "vision_live_situation_invalid",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    radiant_net_worth = _value(state, "radiant_net_worth")
    dire_net_worth = _value(state, "dire_net_worth")
    net_worth_values = (radiant_net_worth, dire_net_worth)
    exact_net_worth_present = any(value is not None for value in net_worth_values)
    if (radiant_net_worth is None) != (dire_net_worth is None) or any(
        value is not None
        and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
        for value in net_worth_values
    ):
        return _unavailable(
            underdog_side,
            "vision_live_situation_invalid",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    advantage_side = _value(state, "net_worth_advantage_side")
    advantage_min = _value(state, "net_worth_advantage_min")
    advantage_max = _value(state, "net_worth_advantage_max")
    advantage_values = (advantage_min, advantage_max)
    advantage_present = advantage_side is not None or any(
        value is not None for value in advantage_values
    )
    if advantage_present and (
        advantage_side not in {"radiant", "dire"}
        or not is_canonical_net_worth_bucket(advantage_min, advantage_max)
        or int(advantage_min) < policy.minimum_net_worth_deficit
        or int(advantage_max) >= policy.maximum_net_worth_deficit
    ):
        return _unavailable(
            underdog_side,
            "vision_live_situation_invalid",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    if exact_net_worth_present and advantage_present:
        return _unavailable(
            underdog_side,
            "vision_live_situation_invalid",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    if exact_net_worth_present:
        return _unavailable(
            underdog_side,
            "vision_net_worth_exact_totals_not_production_evidence",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    if not exact_net_worth_present and not advantage_present:
        return _unavailable(
            underdog_side,
            "vision_net_worth_evidence_missing",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    radiant_team_side = getattr(observation, "radiant_team_side", None)
    if radiant_team_side not in {"team_one", "team_two"}:
        return _unavailable(
            underdog_side,
            "team_side_not_confirmed",
            source_status="available",
            source="vision_hud",
            confidence=confidence,
        )
    underdog_is_radiant = underdog_side == radiant_team_side
    underdog_kills = int(
        values["radiant_kills"] if underdog_is_radiant else values["dire_kills"]
    )
    opponent_kills = int(
        values["dire_kills"] if underdog_is_radiant else values["radiant_kills"]
    )
    underdog_net_worth = (
        int(radiant_net_worth if underdog_is_radiant else dire_net_worth)
        if radiant_net_worth is not None and dire_net_worth is not None
        else None
    )
    opponent_net_worth = (
        int(dire_net_worth if underdog_is_radiant else radiant_net_worth)
        if radiant_net_worth is not None and dire_net_worth is not None
        else None
    )
    kill_deficit = opponent_kills - underdog_kills
    net_worth_deficit = (
        opponent_net_worth - underdog_net_worth
        if opponent_net_worth is not None and underdog_net_worth is not None
        else None
    )
    if net_worth_deficit is not None:
        net_worth_deficit_min = net_worth_deficit
        net_worth_deficit_max = net_worth_deficit
    else:
        assert isinstance(advantage_min, int)
        assert isinstance(advantage_max, int)
        underdog_radiant_side = "radiant" if underdog_is_radiant else "dire"
        if advantage_side == underdog_radiant_side:
            net_worth_deficit_min = -advantage_max
            net_worth_deficit_max = -advantage_min
        else:
            net_worth_deficit_min = advantage_min
            net_worth_deficit_max = advantage_max
    reason = "controlled_deficit"
    controllable = True
    if (
        kill_deficit > policy.maximum_kill_deficit
        or net_worth_deficit_max > policy.maximum_net_worth_deficit
    ):
        reason = "vision_situation_collapsed"
        controllable = False
    elif (
        kill_deficit < policy.minimum_kill_deficit
        or net_worth_deficit_min < policy.minimum_net_worth_deficit
    ):
        reason = "underdog_deficit_not_material"
        controllable = False
    return ComebackSituationDecision(
        controllable=controllable,
        reason=reason,
        source_status="available",
        source="vision_hud",
        confidence=confidence,
        underdog_side=underdog_side,
        underdog_kills=underdog_kills,
        opponent_kills=opponent_kills,
        kill_deficit=kill_deficit,
        underdog_net_worth=underdog_net_worth,
        opponent_net_worth=opponent_net_worth,
        net_worth_deficit=net_worth_deficit,
        net_worth_advantage_side=(
            str(advantage_side) if advantage_present else None
        ),
        net_worth_deficit_min=net_worth_deficit_min,
        net_worth_deficit_max=net_worth_deficit_max,
    )


def decide_comeback_entry(
    observation: object,
    *,
    underdog_side: str,
    rosh_underdog_probability: float | None,
    policy: ComebackEntryPolicy = ComebackEntryPolicy(),
) -> ComebackEntryDecision:
    situation = assess_comeback_situation(
        observation,
        underdog_side=underdog_side,
        policy=policy,
    )
    game_clock_seconds = getattr(observation, "game_clock_seconds", None)
    reason = "eligible"
    if not situation.controllable:
        reason = situation.reason
    elif (
        isinstance(game_clock_seconds, bool)
        or not isinstance(game_clock_seconds, int)
        or not policy.minimum_clock_seconds
        <= game_clock_seconds
        <= policy.maximum_clock_seconds
    ):
        reason = "comeback_entry_outside_time_window"
    elif (
        rosh_underdog_probability is None
        or not math.isfinite(rosh_underdog_probability)
    ):
        reason = "rosh_direction_unavailable"
    elif rosh_underdog_probability <= 0.5:
        reason = "rosh_direction_opposes_underdog"
    return ComebackEntryDecision(
        eligible=reason == "eligible",
        reason=reason,
        game_clock_seconds=(
            game_clock_seconds if isinstance(game_clock_seconds, int) else None
        ),
        rosh_underdog_probability=rosh_underdog_probability,
        situation=situation,
        policy=policy,
    )
