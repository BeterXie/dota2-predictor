from __future__ import annotations

from types import SimpleNamespace

import pytest

from live_betting.comeback_entry import (
    ComebackEntryPolicy,
    assess_comeback_situation,
    decide_comeback_entry,
)


def observation(
    *,
    clock: int = 30 * 60,
    state: object | None = None,
) -> object:
    values = {
        "game_clock_seconds": clock,
        "radiant_team_side": "team_one",
        "screen_state": "game",
    }
    if state is not None:
        values["comeback_state"] = state
    return SimpleNamespace(**values)


def hud_state(
    *,
    confidence: float = 0.95,
    radiant_kills: int = 14,
    dire_kills: int = 18,
    radiant_net_worth: int | None = 42_000,
    dire_net_worth: int | None = 47_000,
    net_worth_advantage_side: str | None = None,
    net_worth_advantage_min: int | None = None,
    net_worth_advantage_max: int | None = None,
) -> dict[str, object]:
    return {
        "status": "available",
        "source": "vision_hud",
        "confidence": confidence,
        "radiant_kills": radiant_kills,
        "dire_kills": dire_kills,
        "radiant_net_worth": radiant_net_worth,
        "dire_net_worth": dire_net_worth,
        "net_worth_advantage_side": net_worth_advantage_side,
        "net_worth_advantage_min": net_worth_advantage_min,
        "net_worth_advantage_max": net_worth_advantage_max,
        "unavailable_reason": None,
    }


def test_legacy_observation_without_live_state_fails_closed() -> None:
    decision = decide_comeback_entry(
        observation(),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert not decision.eligible
    assert decision.reason == "vision_live_situation_missing"


def test_explicit_unavailable_state_preserves_source_reason() -> None:
    decision = decide_comeback_entry(
        observation(
            state={
                "status": "unavailable",
                "source": None,
                "confidence": 0.0,
                "radiant_kills": None,
                "dire_kills": None,
                "radiant_net_worth": None,
                "dire_net_worth": None,
                "unavailable_reason": "hud_live_state_ocr_unavailable",
            }
        ),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert not decision.eligible
    assert decision.reason == (
        "vision_live_situation_unavailable:hud_live_state_ocr_unavailable"
    )
    assert decision.as_inputs()["comeback_state"]["unavailable_reason"] == (
        "hud_live_state_ocr_unavailable"
    )


def test_non_game_frame_cannot_supply_live_situation() -> None:
    current = observation(state=hud_state())
    current.screen_state = "loading"

    decision = decide_comeback_entry(
        current,
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert decision.reason == "vision_live_situation_not_game_frame"


def test_controlled_deficit_and_rosh_direction_allow_entry() -> None:
    decision = decide_comeback_entry(
        observation(
            state=hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=5_000,
                net_worth_advantage_max=5_999,
            )
        ),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert decision.eligible
    assert decision.reason == "eligible"
    assert decision.situation.net_worth_deficit is None
    assert decision.situation.net_worth_deficit_min == 5_000
    assert decision.situation.kill_deficit == 4


def test_kills_only_state_cannot_prove_a_controlled_economic_deficit() -> None:
    decision = decide_comeback_entry(
        observation(
            state=hud_state(radiant_net_worth=None, dire_net_worth=None)
        ),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert not decision.eligible
    assert decision.reason == "vision_net_worth_evidence_missing"
    assert decision.as_inputs()["entry_window"]["inside"] is True
    assert decision.as_inputs()["comeback_entry"]["policy"] == {
        "minimum_clock_seconds": 1200,
        "maximum_clock_seconds": 2700,
        "minimum_kill_deficit": 2,
        "maximum_kill_deficit": 10,
        "minimum_net_worth_deficit": 1000,
        "maximum_net_worth_deficit": 10000,
        "minimum_vision_confidence": 0.9,
    }


def test_exact_net_worth_totals_cannot_satisfy_v4_production_gate() -> None:
    decision = decide_comeback_entry(
        observation(state=hud_state()),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert not decision.eligible
    assert decision.reason == "vision_net_worth_exact_totals_not_production_evidence"


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [(5_000, 5_000), (500, 1_499), (5_000, 6_000)],
)
def test_noncanonical_net_worth_range_cannot_satisfy_v4_production_gate(
    minimum: int,
    maximum: int,
) -> None:
    decision = decide_comeback_entry(
        observation(
            state=hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=minimum,
                net_worth_advantage_max=maximum,
            )
        ),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert not decision.eligible
    assert decision.reason == "vision_live_situation_invalid"


@pytest.mark.parametrize(
    ("side", "minimum", "maximum", "eligible", "reason"),
    [
        ("dire", 0, 999, False, "underdog_deficit_not_material"),
        ("dire", 1_000, 1_999, True, "eligible"),
        ("dire", 9_000, 9_999, True, "eligible"),
        ("dire", 10_000, 10_999, False, "vision_situation_collapsed"),
        ("radiant", 1_000, 1_999, False, "underdog_deficit_not_material"),
    ],
)
def test_bucketed_net_worth_advantage_is_bounded_and_side_aware(
    side: str,
    minimum: int,
    maximum: int,
    eligible: bool,
    reason: str,
) -> None:
    decision = decide_comeback_entry(
        observation(
            state=hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side=side,
                net_worth_advantage_min=minimum,
                net_worth_advantage_max=maximum,
            )
        ),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
    )

    assert decision.eligible is eligible
    assert decision.reason == reason
    assert decision.situation.net_worth_deficit_min == (
        minimum if side == "dire" else -maximum
    )
    assert decision.situation.net_worth_deficit_max == (
        maximum if side == "dire" else -minimum
    )


@pytest.mark.parametrize(
    ("clock", "rosh_probability", "reason"),
    [
        (19 * 60 + 59, 0.62, "comeback_entry_outside_time_window"),
        (45 * 60 + 1, 0.62, "comeback_entry_outside_time_window"),
        (30 * 60, 0.50, "rosh_direction_opposes_underdog"),
        (30 * 60, 0.38, "rosh_direction_opposes_underdog"),
    ],
)
def test_time_and_rosh_direction_are_explicit_hard_gates(
    clock: int,
    rosh_probability: float,
    reason: str,
) -> None:
    decision = decide_comeback_entry(
        observation(
            clock=clock,
            state=hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=5_000,
                net_worth_advantage_max=5_999,
            ),
        ),
        underdog_side="team_one",
        rosh_underdog_probability=rosh_probability,
    )

    assert not decision.eligible
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (
            hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=11_000,
                net_worth_advantage_max=11_999,
            ),
            "vision_situation_collapsed",
        ),
        (
            hud_state(
                radiant_kills=5,
                dire_kills=16,
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=5_000,
                net_worth_advantage_max=5_999,
            ),
            "vision_situation_collapsed",
        ),
        (
            hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=0,
                net_worth_advantage_max=999,
            ),
            "underdog_deficit_not_material",
        ),
        (
            hud_state(
                radiant_kills=17,
                dire_kills=18,
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=5_000,
                net_worth_advantage_max=5_999,
            ),
            "underdog_deficit_not_material",
        ),
    ],
)
def test_non_controllable_situations_are_rejected(
    state: object,
    reason: str,
) -> None:
    situation = assess_comeback_situation(
        observation(state=state),
        underdog_side="team_one",
    )

    assert not situation.controllable
    assert situation.reason == reason


def test_policy_thresholds_are_explicitly_overridable() -> None:
    policy = ComebackEntryPolicy(maximum_net_worth_deficit=4_000)
    decision = decide_comeback_entry(
        observation(
            state=hud_state(
                radiant_net_worth=None,
                dire_net_worth=None,
                net_worth_advantage_side="dire",
                net_worth_advantage_min=5_000,
                net_worth_advantage_max=5_999,
            )
        ),
        underdog_side="team_one",
        rosh_underdog_probability=0.62,
        policy=policy,
    )

    assert decision.reason == "vision_situation_collapsed"
