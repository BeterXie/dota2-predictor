from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.team_rating import (
    INACTIVITY_HALF_LIFE_DAYS_GRID,
    K_FACTOR_GRID,
    ROSTER_CARRY_POWER_GRID,
    SCALE_GRID,
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
    TeamRatingState,
    canonical_training_corpus,
    effective_team_rating,
    estimate_radiant_side_logit,
    predict_team_rating,
    rating_training_input_hash,
    replay_team_ratings,
    team_rating_probability,
    update_team_ratings,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
RADIANT_ROSTER = (1, 2, 3, 4, 5)
DIRE_ROSTER = (6, 7, 8, 9, 10)
_DEFAULT = object()


def _config(**changes: object) -> TeamRatingConfig:
    values = {
        "initial_rating": 1_500.0,
        "scale": 400.0,
        "k_factor": 32.0,
        "inactivity_half_life_days": None,
        "roster_carry_power": 1.0,
        "radiant_side_logit": 0.0,
        "config_version": TEAM_RATING_VERSION,
    }
    values.update(changes)
    return TeamRatingConfig(**values)  # type: ignore[arg-type]


def _row(
    match_id: int,
    *,
    started_at: datetime | None = None,
    result_usable_at: datetime | None | object = _DEFAULT,
    radiant_win: bool = True,
    radiant_team_id: int = 10,
    dire_team_id: int = 20,
    radiant_roster: tuple[int, ...] = RADIANT_ROSTER,
    dire_roster: tuple[int, ...] = DIRE_ROSTER,
    series_id: int | None = 100,
) -> RatingMapInput:
    started = started_at or START + timedelta(days=match_id)
    completed = started + timedelta(minutes=45)
    usable = (
        completed + timedelta(minutes=5)
        if result_usable_at is _DEFAULT
        else result_usable_at
    )
    return RatingMapInput(
        match_id=match_id,
        series_id=series_id,
        event_id="event-a",
        started_at=started,
        completed_at=completed,
        result_usable_at=usable,  # type: ignore[arg-type]
        radiant_team_id=radiant_team_id,
        dire_team_id=dire_team_id,
        radiant_roster=radiant_roster,
        dire_roster=dire_roster,
        radiant_win=radiant_win,
    )


def _states(
    *,
    radiant_rating: float = 1_600.0,
    dire_rating: float = 1_400.0,
    last_observed_at: datetime | None = START,
) -> tuple[TeamRatingState, ...]:
    return (
        TeamRatingState(10, radiant_rating, 4, RADIANT_ROSTER, last_observed_at),
        TeamRatingState(20, dire_rating, 6, DIRE_ROSTER, last_observed_at),
    )


def test_version_grids_and_side_prior_are_fixed() -> None:
    assert TEAM_RATING_VERSION == "team-rating-elo-v1"
    assert SCALE_GRID == (200.0, 300.0, 400.0)
    assert K_FACTOR_GRID == (8.0, 16.0, 24.0, 32.0)
    assert INACTIVITY_HALF_LIFE_DAYS_GRID == (None, 90.0, 180.0, 365.0)
    assert ROSTER_CARRY_POWER_GRID == (0.5, 1.0, 2.0)
    assert estimate_radiant_side_logit(0, 0) == 0.0
    assert estimate_radiant_side_logit(7, 10) == pytest.approx(
        math.log((8 / 12) / (4 / 12))
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        estimate_radiant_side_logit(2, 1)


def test_probability_is_side_swap_symmetric_and_monotonic() -> None:
    config = _config(radiant_side_logit=0.2)
    swapped_config = replace(config, radiant_side_logit=-0.2)

    probability = team_rating_probability(1_620.0, 1_430.0, config)
    swapped = team_rating_probability(1_430.0, 1_620.0, swapped_config)

    assert probability == pytest.approx(1.0 - swapped)
    assert team_rating_probability(1_500.0, 1_500.0, config) == pytest.approx(
        1.0 / (1.0 + math.exp(-0.2))
    )
    assert team_rating_probability(1_600.0, 1_500.0, config) > (
        team_rating_probability(1_550.0, 1_500.0, config)
    )


def test_elo_update_is_zero_sum_relative_to_effective_ratings() -> None:
    config = _config()
    states = _states()
    row = _row(1, started_at=START + timedelta(days=1))
    assert row.result_usable_at is not None
    radiant_before, _ = effective_team_rating(
        states[0], row.radiant_roster, row.started_at, config
    )
    dire_before, _ = effective_team_rating(
        states[1], row.dire_roster, row.started_at, config
    )

    updated = {
        state.team_id: state
        for state in update_team_ratings(
            states,
            row,
            row.result_usable_at,
            config,
        )
    }

    radiant_delta = updated[10].rating - radiant_before
    dire_delta = updated[20].rating - dire_before
    assert radiant_delta + dire_delta == pytest.approx(0.0)
    assert updated[10].maps_seen == 5
    assert updated[20].maps_seen == 7
    assert updated[10].last_observed_at == row.completed_at


def test_result_must_be_usable_before_it_updates_or_counts_support() -> None:
    config = _config()
    unavailable = _row(1, result_usable_at=None)
    future = _row(2)
    assert future.result_usable_at is not None

    assert (
        update_team_ratings((), unavailable, START + timedelta(days=20), config) == ()
    )
    assert (
        update_team_ratings(
            (),
            future,
            future.result_usable_at - timedelta(microseconds=1),
            config,
        )
        == ()
    )
    updated = update_team_ratings((), future, future.result_usable_at, config)
    assert sum(state.maps_seen for state in updated) == 2


def test_provider_result_delay_does_not_change_rating_update() -> None:
    config = _config(inactivity_half_life_days=1.0)
    started_at = START + timedelta(days=2)
    fast = _row(
        1,
        started_at=started_at,
        result_usable_at=started_at + timedelta(minutes=47),
    )
    delayed = replace(
        fast,
        result_usable_at=fast.completed_at + timedelta(hours=2),
    )
    assert delayed.result_usable_at is not None
    update_cutoff = delayed.result_usable_at + timedelta(minutes=1)

    fast_update = update_team_ratings(_states(), fast, update_cutoff, config)
    delayed_update = update_team_ratings(_states(), delayed, update_cutoff, config)

    assert fast_update == delayed_update
    assert all(state.last_observed_at == fast.completed_at for state in fast_update)


def test_same_series_map_inherits_only_an_available_previous_result() -> None:
    config = _config()
    first_started = START + timedelta(hours=1)
    first = _row(1, started_at=first_started, series_id=55)
    assert first.result_usable_at is not None
    second = _row(
        2,
        started_at=first.result_usable_at + timedelta(minutes=10),
        result_usable_at=None,
        series_id=55,
    )

    before = replay_team_ratings(
        (first,),
        first.result_usable_at - timedelta(microseconds=1),
        config,
    )
    after = replay_team_ratings((first,), first.result_usable_at, config)
    prediction = predict_team_rating(after, second, second.started_at, config)

    assert before == ()
    assert sum(state.maps_seen for state in after) == 2
    assert prediction.support == 2
    assert prediction.radiant_rating > prediction.dire_rating


def test_next_map_inactivity_starts_at_previous_completion() -> None:
    config = _config(inactivity_half_life_days=1.0)
    first = _row(
        1,
        started_at=START + timedelta(days=1),
        result_usable_at=START + timedelta(days=1, hours=7),
        series_id=55,
    )
    assert first.result_usable_at is not None
    states = replay_team_ratings((first,), first.result_usable_at, config)
    state_by_team = {state.team_id: state for state in states}
    second_started = first.completed_at + timedelta(days=1)
    second = _row(
        2,
        started_at=second_started,
        result_usable_at=None,
        series_id=55,
    )

    prediction = predict_team_rating(states, second, second_started, config)

    assert state_by_team[10].last_observed_at == first.completed_at
    assert prediction.radiant_rating == pytest.approx(
        config.initial_rating + 0.5 * (state_by_team[10].rating - config.initial_rating)
    )
    assert prediction.dire_rating == pytest.approx(
        config.initial_rating + 0.5 * (state_by_team[20].rating - config.initial_rating)
    )


def test_roster_carry_handles_same_changed_partial_and_missing_rosters() -> None:
    config = _config(roster_carry_power=2.0)
    state = TeamRatingState(10, 1_700.0, 5, RADIANT_ROSTER, START)

    same, same_continuity = effective_team_rating(state, RADIANT_ROSTER, START, config)
    changed, changed_continuity = effective_team_rating(
        state, (6, 7, 8, 9, 10), START, config
    )
    partial, partial_continuity = effective_team_rating(
        state, (1, 2, 3, 6, 7), START, config
    )
    missing, missing_continuity = effective_team_rating(state, (), START, config)

    assert (same, same_continuity) == (1_700.0, 1.0)
    assert (changed, changed_continuity) == (1_500.0, 0.0)
    assert partial_continuity == 0.6
    assert partial == pytest.approx(1_500.0 + 0.6**2 * 200.0)
    assert (missing, missing_continuity) == (1_700.0, None)

    unknown_previous = replace(state, roster=())
    no_guess, continuity = effective_team_rating(
        unknown_previous,
        RADIANT_ROSTER,
        START,
        config,
    )
    assert (no_guess, continuity) == (1_700.0, None)


def test_unknown_update_roster_does_not_preserve_stale_continuity() -> None:
    config = _config()
    row = _row(
        1,
        started_at=START + timedelta(days=1),
        radiant_roster=(),
    )
    assert row.result_usable_at is not None

    updated = update_team_ratings(_states(), row, row.result_usable_at, config)
    radiant_state = next(state for state in updated if state.team_id == 10)
    target = _row(
        2,
        started_at=row.result_usable_at + timedelta(hours=1),
        result_usable_at=None,
    )
    prediction = predict_team_rating(updated, target, target.started_at, config)

    assert radiant_state.roster == ()
    assert prediction.radiant_roster_continuity is None


def test_inactivity_half_life_is_effective_only_and_rejects_negative_time() -> None:
    config = _config(inactivity_half_life_days=90.0)
    state = TeamRatingState(10, 1_600.0, 5, (), START)

    effective, continuity = effective_team_rating(
        state,
        (),
        START + timedelta(days=90),
        config,
    )

    assert effective == pytest.approx(1_550.0)
    assert continuity is None
    assert state.rating == 1_600.0
    with pytest.raises(ValueError, match="cannot precede"):
        effective_team_rating(state, (), START - timedelta(seconds=1), config)


def test_rosters_are_canonical_and_incomplete_rosters_fail_closed() -> None:
    row = _row(
        1,
        radiant_roster=(5, 4, 3, 2, 1, 1),
        dire_roster=(10, 9, 8, 7, 6, 10),
    )
    assert row.radiant_roster == RADIANT_ROSTER
    assert row.dire_roster == DIRE_ROSTER

    with pytest.raises(ValueError, match="exactly five"):
        _row(2, radiant_roster=(1, 2, 3, 4))
    with pytest.raises(ValueError, match="positive integer"):
        _row(2, radiant_roster=(0, 2, 3, 4, 5))


@pytest.mark.parametrize("field", ["started_at", "completed_at", "result_usable_at"])
def test_naive_map_datetimes_are_rejected(field: str) -> None:
    row = _row(1)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(row, **{field: getattr(row, field).replace(tzinfo=None)})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_rating", float("nan")),
        ("scale", float("inf")),
        ("k_factor", float("-inf")),
        ("roster_carry_power", 0.0),
        ("inactivity_half_life_days", -1.0),
    ],
)
def test_invalid_config_numbers_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})

    with pytest.raises(ValueError, match="finite"):
        TeamRatingState(10, float("nan"), 0, (), None)


def test_corpus_order_duplicates_hashes_and_conflicts_are_deterministic() -> None:
    config = _config()
    first = _row(1)
    second = _row(2, radiant_win=False)
    cutoff = START + timedelta(days=20)

    ordered = canonical_training_corpus((second, first, first), cutoff)
    baseline_hash = rating_training_input_hash((first, second), cutoff, config)
    reordered_hash = rating_training_input_hash((second, first, first), cutoff, config)
    baseline_states = replay_team_ratings((first, second), cutoff, config)
    reordered_states = replay_team_ratings((second, first, first), cutoff, config)

    assert tuple(row.match_id for row in ordered) == (1, 2)
    assert reordered_hash == baseline_hash
    assert reordered_states == baseline_states
    with pytest.raises(ValueError, match="conflicting training rows"):
        canonical_training_corpus(
            (first, replace(first, radiant_win=not first.radiant_win)),
            cutoff,
        )


def test_corpus_replay_order_does_not_follow_provider_delay() -> None:
    config = _config(inactivity_half_life_days=30.0)
    first = _row(
        1,
        started_at=START + timedelta(days=1),
        result_usable_at=START + timedelta(days=5),
    )
    second = _row(
        2,
        started_at=START + timedelta(days=2),
        result_usable_at=START + timedelta(days=3),
        radiant_win=False,
    )
    cutoff = START + timedelta(days=10)

    ordered = canonical_training_corpus((second, first), cutoff)
    delayed_states = replay_team_ratings((second, first), cutoff, config)
    prompt_states = replay_team_ratings(
        (
            replace(first, result_usable_at=first.completed_at),
            replace(second, result_usable_at=second.completed_at),
        ),
        cutoff,
        config,
    )

    assert tuple(row.match_id for row in ordered) == (1, 2)
    assert delayed_states == prompt_states


def test_target_outcome_and_postmatch_fields_cannot_change_prediction() -> None:
    config = _config()
    target = _row(
        99,
        started_at=START + timedelta(days=30),
        result_usable_at=None,
    )
    changed = replace(
        target,
        completed_at=target.completed_at + timedelta(hours=2),
        result_usable_at=target.completed_at + timedelta(hours=3),
        radiant_win=not target.radiant_win,
    )
    states = _states(last_observed_at=START + timedelta(days=20))

    first = predict_team_rating(states, target, target.started_at, config)
    second = predict_team_rating(states, changed, changed.started_at, config)

    assert first == second
    with pytest.raises(ValueError, match="cannot follow"):
        predict_team_rating(
            states,
            target,
            target.started_at + timedelta(microseconds=1),
            config,
        )


def test_missing_target_rosters_do_not_guess_continuity() -> None:
    target = _row(
        99,
        started_at=START + timedelta(days=30),
        result_usable_at=None,
        radiant_roster=(),
        dire_roster=(),
    )

    prediction = predict_team_rating(
        _states(last_observed_at=START + timedelta(days=20)),
        target,
        target.started_at,
        _config(),
    )

    assert prediction.radiant_roster_continuity is None
    assert prediction.dire_roster_continuity is None
