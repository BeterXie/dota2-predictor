from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from live_betting.map_decision_checkpoints import (
    LIVE_ODDS_MAX_AGE_SECONDS,
    LIVE_ODDS_VISION_GAP_MAX_SECONDS,
    LIVE_VISION_MAX_AGE_SECONDS,
    MINIMUM_EDGE,
    PREGAME_ODDS_MAX_AGE_SECONDS,
    checkpoint_evaluation_eligibility,
    _live_values,
    _pregame_values,
)
from live_betting.live_probability import (
    MODEL_VERSION as LIVE_PROBABILITY_MODEL_VERSION,
    VALIDATION_BY_MINUTE,
    estimate_radiant_win_probability,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
IDENTITY = {
    "raybet_match_id": "series-1",
    "map_number": 2,
    "mapping_version": 3,
}
PREDICTION = {
    "bridge_version": "live-draft-prospective-bridge-v1",
    "record_status": "paired",
    "p1_probability": 0.65,
    "missing_reason": None,
    "causal_status": "eligible",
    "causal_reason": None,
    "game_clock_seconds": None,
    "draft_state_marker": "draft_complete",
}


def _market(*, age_seconds: float = 1.0) -> dict[str, object]:
    return {
        "observation_key": "odds-observation-17",
        "odds_group_id": "winner-map-2",
        "observed_at": NOW - timedelta(seconds=age_seconds),
        "probabilities": {"team_one": 0.55, "team_two": 0.45},
        "prices": {"team_one": 1.8, "team_two": 2.1},
    }


def _snapshot(*, age_seconds: float = 1.0) -> dict[str, object]:
    return {
        "snapshot_id": 19,
        "captured_at": NOW - timedelta(seconds=age_seconds),
        "game_time_seconds": 300,
        "networth_lead": 1200,
        "radiant_kills": 8,
        "dire_kills": 5,
        "source_frame_ref": "vision-frame:series-1:map-2:300",
        "radiant_team_side": "team_one",
    }


def test_pregame_checkpoint_uses_fixed_edge_and_one_shadow_unit() -> None:
    checkpoint = _pregame_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(),
        radiant_match_side="team_one",
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "bet_team_a"
    assert checkpoint["selected_edge"] == pytest.approx(0.10)
    assert checkpoint["selected_edge"] >= MINIMUM_EDGE
    assert checkpoint["assumed_stake_units"] == 1.0
    assert checkpoint["odds_max_age_seconds"] == PREGAME_ODDS_MAX_AGE_SECONDS
    assert checkpoint["reason"] == "minimum_edge_met"


def test_stale_pregame_odds_are_traceable_skip_without_fabrication() -> None:
    checkpoint = _pregame_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(age_seconds=151.0),
        radiant_match_side="team_one",
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == "pregame_odds_stale"
    assert checkpoint["odds_age_seconds"] == 151.0
    assert checkpoint["odds_max_age_seconds"] == 150.0


def test_pregame_checkpoint_skips_when_lineup_authority_is_in_game() -> None:
    checkpoint = _pregame_values(
        identity=IDENTITY,
        prediction={
            **PREDICTION,
            "game_clock_seconds": 1,
            "draft_state_marker": "in_game",
        },
        market=_market(),
        radiant_match_side="team_one",
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == "pregame_authority_after_map_start"
    assert checkpoint["selected_edge"] is None
    assert '"draft_state_marker":"in_game"' in checkpoint["feature_availability_json"]
    assert '"game_clock_seconds":1' in checkpoint["feature_availability_json"]


def test_live_checkpoint_uses_validated_gold_lead_model_and_fixed_edge() -> None:
    checkpoint = _live_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(),
        snapshot=_snapshot(),
        checkpoint_minute=5,
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "bet_team_a"
    assert checkpoint["reason"] == "minimum_edge_met"
    assert checkpoint["model_probability_team_one"] == pytest.approx(0.7878, abs=0.001)
    assert checkpoint["selected_edge"] == pytest.approx(0.2378, abs=0.001)
    assert checkpoint["observed_price"] == 1.8
    assert checkpoint["vision_trusted"] is True
    assert checkpoint["vision_replay"] is False
    assert checkpoint["vision_age_seconds"] == 1.0
    assert checkpoint["vision_max_age_seconds"] == LIVE_VISION_MAX_AGE_SECONDS
    assert checkpoint["odds_max_age_seconds"] == LIVE_ODDS_MAX_AGE_SECONDS
    assert checkpoint["odds_vision_gap_max_seconds"] == (
        LIVE_ODDS_VISION_GAP_MAX_SECONDS
    )
    features = checkpoint["feature_availability_json"]
    assert f'"model_version":"{LIVE_PROBABILITY_MODEL_VERSION}"' in features
    assert '"live_probability_model":{"available":true' in features
    assert '"kills_used":false' in features
    assert '"levels":{"available":false,"reason":"not_collected"}' in features
    assert '"objectives":{"available":false,"reason":"not_collected"}' in features


@pytest.mark.parametrize(
    ("market", "snapshot", "expected_reason"),
    (
        (_market(age_seconds=16.0), _snapshot(), "live_odds_stale"),
        (_market(), _snapshot(age_seconds=6.0), "live_vision_stale"),
        (_market(), {**_snapshot(), "radiant_kills": None}, "live_kills_unavailable"),
        (
            _market(),
            {**_snapshot(), "radiant_team_side": None},
            "live_team_direction_unavailable",
        ),
    ),
)
def test_live_checkpoint_fails_closed_on_required_input_gate(
    market: dict[str, object],
    snapshot: dict[str, object],
    expected_reason: str,
) -> None:
    checkpoint = _live_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=market,
        snapshot=snapshot,
        checkpoint_minute=5,
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == expected_reason
    assert checkpoint["odds_max_age_seconds"] == 15.0
    assert checkpoint["vision_max_age_seconds"] == 5.0


def test_live_checkpoint_skips_when_odds_and_vision_gap_exceeds_limit() -> None:
    checkpoint = _live_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(age_seconds=-16.0),
        snapshot=_snapshot(age_seconds=0.0),
        checkpoint_minute=5,
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == "live_odds_vision_gap_exceeded"
    assert checkpoint["odds_vision_gap_seconds"] == 16.0
    assert checkpoint["odds_vision_gap_max_seconds"] == 15.0


def test_live_checkpoint_skips_when_validated_edge_is_below_threshold() -> None:
    checkpoint = _live_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(),
        snapshot={**_snapshot(), "networth_lead": -700},
        checkpoint_minute=5,
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == "edge_below_threshold"
    assert checkpoint["selected_edge"] is not None
    assert checkpoint["selected_edge"] < MINIMUM_EDGE


def test_live_checkpoint_does_not_extrapolate_past_validated_minutes() -> None:
    checkpoint = _live_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(),
        snapshot={**_snapshot(), "game_time_seconds": 3900},
        checkpoint_minute=65,
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == "live_probability_checkpoint_minute_not_validated"
    assert checkpoint["model_probability_team_one"] is None
    assert '"available":false' in checkpoint["feature_availability_json"]


def test_live_probability_update_respects_prior_and_lead_direction() -> None:
    tied = estimate_radiant_win_probability(
        prior_radiant_probability=0.65,
        radiant_networth_lead=0,
        checkpoint_minute=10,
    )
    ahead = estimate_radiant_win_probability(
        prior_radiant_probability=0.65,
        radiant_networth_lead=2000,
        checkpoint_minute=10,
    )
    behind = estimate_radiant_win_probability(
        prior_radiant_probability=0.65,
        radiant_networth_lead=-2000,
        checkpoint_minute=10,
    )

    assert tied.probability_radiant == pytest.approx(0.65)
    assert behind.probability_radiant < tied.probability_radiant
    assert ahead.probability_radiant > tied.probability_radiant


def test_every_enabled_live_model_minute_beats_its_holdout_baseline() -> None:
    assert set(VALIDATION_BY_MINUTE) == set(range(5, 61, 5))
    for validation in VALIDATION_BY_MINUTE.values():
        _, holdout, brier, baseline_brier, loss, baseline_loss, ece = validation
        assert holdout >= 50
        assert brier < baseline_brier
        assert loss < baseline_loss
        assert 0.0 <= ece <= 0.2


def test_live_checkpoint_records_missing_vision_instead_of_using_odds_only() -> None:
    checkpoint = _live_values(
        identity=IDENTITY,
        prediction=PREDICTION,
        market=_market(),
        snapshot=None,
        checkpoint_minute=10,
        decided_at=NOW,
    )

    assert checkpoint["decision"] == "skip"
    assert checkpoint["reason"] == "trusted_vision_checkpoint_missing"
    assert checkpoint["vision_snapshot_id"] is None
    assert checkpoint["vision_networth_lead"] is None
    assert checkpoint["vision_radiant_kills"] is None
    assert checkpoint["vision_dire_kills"] is None


def test_checkpoint_recorded_after_official_map_end_is_excluded() -> None:
    eligible, reason = checkpoint_evaluation_eligibility(
        NOW,
        "3",
        NOW - timedelta(minutes=31),
        30 * 60,
    )

    assert eligible is False
    assert reason == "checkpoint_recorded_after_official_map_end"


def test_live_checkpoint_remains_eligible_before_official_map_end() -> None:
    eligible, reason = checkpoint_evaluation_eligibility(
        NOW - timedelta(minutes=1),
        "3",
        NOW - timedelta(minutes=31),
        31 * 60,
    )

    assert eligible is True
    assert reason is None


def test_ended_map_without_exact_end_is_not_evaluation_eligible() -> None:
    eligible, reason = checkpoint_evaluation_eligibility(NOW, "3", None, None)

    assert eligible is False
    assert reason == "official_map_end_unavailable"
