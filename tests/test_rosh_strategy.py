from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from live_betting.comeback import score_comeback
from live_betting.market_state import build_market_surface
from live_betting.models import Market, OddsSnapshot, RoshLineupScore
from live_betting.profiles.draft_curve import DraftCurve, DraftPoint
from live_betting.profiles.player_form import PlayerForm
from live_betting.profiles.team_style import TeamStyleProfile
from live_betting.shadow_strategy import ComebackShadowStrategy
from live_betting.vision import VisionObservation


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
OBSERVED_DRAFT_HASH = hashlib.sha256(
    json.dumps(
        {"radiant": [1, 2, 3, 4, 5], "dire": [6, 7, 8, 9, 10]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def _score(
    *,
    mode: str = "pure",
    pure: float = 20.0,
    adjusted: float = 25.0,
    match_percentage: float = 100.0,
    pure_table: list[dict[str, float | int]] | None = None,
    adjusted_table: list[dict[str, float | int]] | None = None,
) -> RoshLineupScore:
    player_adjusted = adjusted if mode == "player_adjusted" else None
    if pure_table is None:
        pure_table = [
            {
                "minute": minute,
                "win_rate_graph": pure,
                "match_percentage": match_percentage,
            }
            for minute in (20, 30, 60)
        ]
    if adjusted_table is None:
        adjusted_table = [
            {
                "minute": minute,
                "win_rate_graph": adjusted,
                "match_percentage": match_percentage,
            }
            for minute in (20, 30, 60)
        ]
    return RoshLineupScore(
        score_key="a" * 64,
        draft_hash=OBSERVED_DRAFT_HASH,
        player_identity_hash="b" * 64,
        pure_lineup_score=pure,
        player_adjusted_lineup_score=player_adjusted,
        effective_lineup_score=player_adjusted if player_adjusted is not None else pure,
        scoring_mode=mode,
        player_coverage_count=10 if mode == "player_adjusted" else 9,
        stake_multiplier=1.0 if mode == "player_adjusted" else 0.5,
        formula_version="dematus-rosh-test",
        source_name="stratz",
        source_week=1_774_224_000,
        cache_week_start=1_774_137_600,
        source_as_of=NOW,
        evidence_hash="c" * 64,
        evidence={
            "player_slots": [],
            "pure_minute_table": pure_table,
            "minute_table": adjusted_table,
        },
    )


def _observation(
    *,
    radiant_team_side: str = "team_one",
    game_clock_seconds: int = 30 * 60,
) -> VisionObservation:
    return VisionObservation(
        "match-1",
        1,
        NOW,
        game_clock_seconds,
        False,
        (1, 2, 3, 4, 5),
        (6, 7, 8, 9, 10),
        0.95,
        0.95,
        "frame",
        "game",
        radiant_team_side,
    )


def _snapshots(at: datetime) -> list[OddsSnapshot]:
    definitions = (
        ("favorite", "winner", "winner", "team_one", None, 1.4),
        ("underdog", "winner", "winner", "team_two", None, 3.0),
        ("kh-one", "kills", "kill_handicap", "team_one", -5.5, 1.9),
        ("kh-two", "kills", "kill_handicap", "team_two", 5.5, 1.9),
        ("total-over", "total", "total_kills", "over", 50.5, 1.9),
        ("total-under", "total", "total_kills", "under", 50.5, 1.9),
        ("duration-over", "duration", "duration", "over", 36.5, 1.9),
        ("duration-under", "duration", "duration", "under", 36.5, 1.9),
    )
    return [
        OddsSnapshot(
            "match-1",
            odds_id,
            group,
            at,
            price,
            1,
            Market(market_type, "map_1", side, line, f"{side}:{line}", True),
        )
        for odds_id, group, market_type, side, line, price in definitions
    ]


def _draft_curve(*, quality: float = 0.8) -> DraftCurve:
    return DraftCurve(
        tuple(
            DraftPoint(
                minute,
                0.99,
                0.0,
                0.0,
                quality,
                validated=True,
                support=250,
                calibration_ref="calibration:passed",
                input_refs=("model:immutable",),
                uncertainty=0.05,
                feature_hash="1" * 64,
                model_hash="2" * 64,
                calibration_hash="3" * 64,
                global_calibration_passed=True,
                global_gate_ref="global:passed",
                model_version="draft-logistic-l2-v1",
                model_kind="pure_draft",
                availability_mode="prospective",
                input_snapshot_hash="4" * 64,
            )
            for minute in (10, 20, 30, 40, 50)
        )
    )


def _style(team_id: int) -> TeamStyleProfile:
    return TeamStyleProfile(team_id, 100, 0.18, 0.16, 0.84, 0.35, 36.0, 1.0)


def _form(score: float = 0.0) -> PlayerForm:
    return PlayerForm((1, 2, 3, 4, 5), score, {}, 100, 1.0)


def _decision(
    score: RoshLineupScore | None,
    *,
    radiant_team_side: str = "team_one",
    game_clock_seconds: int = 30 * 60,
    underdog_form_score: float = 0.0,
    favorite_form_score: float = 0.0,
):
    return score_comeback(
        observation=_observation(
            radiant_team_side=radiant_team_side,
            game_clock_seconds=game_clock_seconds,
        ),
        surface=build_market_surface(_snapshots(NOW)),
        underdog_style=_style(2),
        favorite_style=_style(1),
        underdog_form=_form(underdog_form_score),
        favorite_form=_form(favorite_form_score),
        draft_curve=_draft_curve(),
        decided_at=NOW,
        stable=True,
        min_edge=0.0,
        rosh_lineup_score=score,
    )


def test_rosh_score_keeps_unbounded_upstream_values() -> None:
    score = _score(mode="player_adjusted", pure=125.0, adjusted=-125.0)

    assert score.pure_score == 125.0
    assert score.effective_score == -125.0


def test_rosh_probability_reverses_by_underdog_side_without_draft_double_count() -> None:
    score = _score(pure=20.0)
    dire_underdog = _decision(score, radiant_team_side="team_one")
    radiant_underdog = _decision(score, radiant_team_side="team_two")
    expected = math.log(0.7 / 0.3) * 0.45 * 0.8

    assert dire_underdog.contributions["lineup_rosh"] == pytest.approx(-expected)
    assert radiant_underdog.contributions["lineup_rosh"] == pytest.approx(expected)
    assert dire_underdog.contributions["draft_curve"] == 0.0
    assert radiant_underdog.contributions["draft_curve"] == 0.0
    assert dire_underdog.inputs["conservative_contributions"]["lineup_rosh"] == pytest.approx(-expected)
    assert radiant_underdog.inputs["conservative_contributions"]["lineup_rosh"] == pytest.approx(expected * 0.8)


def test_missing_rosh_score_fails_closed() -> None:
    decision = _decision(None, radiant_team_side="team_two")

    assert decision.eligible is False
    assert decision.reason == "rosh_lineup_score_unavailable"
    assert decision.stake_multiplier == 0.0
    assert decision.inputs["rosh_lineup_score"]["status"] == "unavailable"
    assert decision.inputs["rosh_lineup_score"]["selected_score"] is None


def test_mismatched_rosh_draft_fails_closed_without_lineup_contribution() -> None:
    decision = _decision(
        replace(_score(pure=35.0), draft_hash="d" * 64),
        radiant_team_side="team_two",
    )

    assert decision.eligible is False
    assert decision.reason == "rosh_lineup_draft_mismatch"
    assert decision.contributions["lineup_rosh"] == 0.0
    assert decision.stake_multiplier == 0.0
    assert decision.inputs["rosh_lineup_score"]["draft_matches_observation"] is False


def test_player_adjusted_rosh_suppresses_standalone_player_form() -> None:
    decision = _decision(
        _score(mode="player_adjusted", pure=0.0, adjusted=0.0),
        radiant_team_side="team_two",
        underdog_form_score=1.0,
    )

    assert decision.contributions["player_form"] == 0.0
    assert decision.inputs["conservative_contributions"]["player_form"] == 0.0
    assert decision.inputs["player_form_suppression"] == {
        "suppressed": True,
        "reason": "included_in_player_adjusted_rosh",
    }


def test_pure_rosh_keeps_standalone_player_form() -> None:
    decision = _decision(
        _score(pure=0.0),
        radiant_team_side="team_two",
        underdog_form_score=1.0,
    )

    assert decision.contributions["player_form"] == pytest.approx(0.35)
    assert decision.inputs["conservative_contributions"]["player_form"] == pytest.approx(0.35)
    assert decision.inputs["player_form_suppression"] == {
        "suppressed": False,
        "reason": None,
    }


def test_rosh_uses_current_minute_instead_of_permanent_late_endpoint() -> None:
    score = _score(
        pure=-30.0,
        pure_table=[
            {"minute": 20, "win_rate_graph": 30.0, "match_percentage": 80.0},
            {"minute": 60, "win_rate_graph": -30.0, "match_percentage": 20.0},
        ],
    )

    early = _decision(
        score,
        radiant_team_side="team_two",
        game_clock_seconds=15 * 60,
    )
    late = _decision(
        score,
        radiant_team_side="team_two",
        game_clock_seconds=60 * 60,
    )

    assert early.inputs["rosh_lineup_score"]["selected_minute"] == 20
    assert early.inputs["rosh_lineup_score"]["selected_score"] == 30.0
    assert early.contributions["lineup_rosh"] > 0.0
    assert late.inputs["rosh_lineup_score"]["selected_minute"] == 60
    assert late.inputs["rosh_lineup_score"]["selected_score"] == -30.0
    assert late.contributions["lineup_rosh"] < 0.0


def test_player_adjusted_mode_uses_adjusted_minute_curve() -> None:
    score = _score(
        mode="player_adjusted",
        pure=-30.0,
        adjusted=30.0,
        pure_table=[
            {"minute": 30, "win_rate_graph": -30.0, "match_percentage": 100.0}
        ],
        adjusted_table=[
            {"minute": 30, "win_rate_graph": 30.0, "match_percentage": 5.0}
        ],
    )

    decision = _decision(score, radiant_team_side="team_two")

    assert decision.inputs["rosh_lineup_score"]["selected_table"] == "minute_table"
    assert decision.inputs["rosh_lineup_score"]["selected_score"] == 30.0
    assert decision.contributions["lineup_rosh"] > 0.0
    assert decision.stake_multiplier == 1.0


def test_rosh_minute_selection_clamps_boundaries_and_uses_nearest_bucket() -> None:
    score = _score(
        pure=0.0,
        pure_table=[
            {"minute": 20, "win_rate_graph": 1.0, "match_percentage": 10.0},
            {"minute": 25, "win_rate_graph": 2.0, "match_percentage": 20.0},
            {"minute": 60, "win_rate_graph": 3.0, "match_percentage": 30.0},
        ],
    )

    before = _decision(score, game_clock_seconds=15 * 60)
    nearest = _decision(score, game_clock_seconds=23 * 60)
    after = _decision(score, game_clock_seconds=61 * 60)

    assert before.inputs["rosh_lineup_score"]["selected_minute"] == 20
    assert nearest.inputs["rosh_lineup_score"]["selected_minute"] == 25
    assert after.inputs["rosh_lineup_score"]["selected_minute"] == 60
    assert "pure_minute_table" not in before.inputs["rosh_lineup_score"]["evidence"]


@pytest.mark.parametrize(
    ("score", "expected_stake"),
    (
        (_score(pure=35.0), 0.5),
        (_score(pure=35.0, match_percentage=40.0), 0.2),
        (_score(mode="player_adjusted", adjusted=35.0), 1.0),
    ),
)
def test_shadow_order_uses_rosh_stake_multiplier(
    score: RoshLineupScore,
    expected_stake: float,
) -> None:
    previous = _snapshots(NOW)
    current_at = NOW + timedelta(seconds=3)
    current = [replace(row, received_at=current_at) for row in previous]
    result = ComebackShadowStrategy(min_edge=0.001).evaluate(
        snapshots=current,
        previous_snapshots=previous,
        observation=_observation(radiant_team_side="team_two"),
        previous_observation=_observation(radiant_team_side="team_two"),
        underdog_style=_style(2),
        favorite_style=_style(1),
        underdog_form=_form(),
        favorite_form=_form(),
        draft_curve=_draft_curve(),
        rosh_lineup_score=score,
        decided_at=current_at,
        map_already_attempted=False,
        signal_transport_key="current",
        previous_transport_key="previous",
    )

    assert result.decision.eligible is True
    assert result.order is not None
    assert result.order.stake == expected_stake
    assert result.decision.inputs["rosh_lineup_score"]["stake_multiplier"] == expected_stake
