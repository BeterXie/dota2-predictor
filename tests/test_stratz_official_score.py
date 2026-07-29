from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import prematch.stratz_official_profile as official_profile
import prematch.stratz_official_score as scorer_module
from prematch.stratz_official_profile import (
    RoshParityProfile,
    build_official_request_plan,
    get_profile,
)
from prematch.stratz_official_score import (
    ALL_RANK_FALLBACK,
    DIVINE_IMMORTAL,
    DraftSlot,
    NormalizedRoshInputs,
    PositionAggregate,
    ScoreError,
    SynergySample,
    TimeAggregate,
    aggregate_pair_synergy,
    normalize_official_responses,
    position_base_diff,
    profile_round,
    result_projection,
    score_official_rosh,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stratz_official_rosh" / "8904419709"
V1_FORMULA_VERSION = "stratz-official-rosh/2026-07-28-v1"
V2_FORMULA_VERSION = "stratz-official-rosh/2026-07-28-v2"
V1_GOLDEN_RESULT_HASH = "336eea9509ce716dd5c2cdcaba4580af7087fdb47477e81d7b469dfff8b6e890"
V2_GOLDEN_RESULT_HASH = "dfede8ca305703bf175699e7b9d504f319601042f88ecb3cb6b102e39de7d593"
V2_PROFILE = replace(
    get_profile(),
    rosh_profile_id="stratz-rosh-web-2026-07-28-v2",
    formula_version=V2_FORMULA_VERSION,
    request_profile_hash="d2" * 32,
)


def _request_started_at_for(plan) -> datetime:
    source = datetime.fromtimestamp(plan.analysis_input.date_time, timezone.utc)
    if plan.current_day_shift == 1:
        return source
    if plan.elapsed_days:
        return source + timedelta(days=plan.elapsed_days)
    return datetime.combine(source.date() + timedelta(days=1), datetime.min.time(), timezone.utc)


def _validate_active_profile(profile: RoshParityProfile) -> None:
    if profile != V2_PROFILE:
        raise ValueError("profile is not the active v2 identity")


def _validate_canonical_request_plan(plan) -> None:
    _validate_active_profile(plan.profile)
    canonical = build_official_request_plan(
        plan.analysis_input,
        request_started_at=_request_started_at_for(plan),
    )
    canonical = replace(canonical, profile=V2_PROFILE)
    if plan != canonical:
        raise ValueError("request plan drift")


@pytest.fixture(scope="module", autouse=True)
def canonical_plan_validator_boundary():
    """D2 bridge only; D3 supplies the real profile-boundary validator."""

    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        official_profile,
        "validate_canonical_request_plan",
        _validate_canonical_request_plan,
        raising=False,
    )
    patcher.setattr(
        official_profile,
        "validate_active_profile",
        _validate_active_profile,
        raising=False,
    )
    try:
        yield
    finally:
        patcher.undo()


@pytest.fixture(scope="module")
def expected() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def responses() -> list[dict[str, Any]]:
    return json.loads((FIXTURE_DIR / "responses.sanitized.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def historical_plan(expected: dict[str, Any], manifest: dict[str, Any]):
    captured_at = datetime.fromisoformat(manifest["provenance"]["captured_at"].replace("Z", "+00:00"))
    plan = build_official_request_plan(
        {
            "mode": "historical_match",
            "match_id": expected["input"]["match_id"],
            "date_time": expected["input"]["date_time"],
            "bracket_ids": expected["input"]["bracket_ids"],
        },
        request_started_at=captured_at,
    )
    return replace(plan, profile=V2_PROFILE)


@pytest.fixture(scope="module")
def normalized(historical_plan, responses):
    return normalize_official_responses(historical_plan, responses)


@pytest.fixture(scope="module")
def golden_result(historical_plan, normalized):
    return score_official_rosh(normalized, historical_plan.profile)


@pytest.mark.parametrize(
    ("value", "expected_value"),
    [
        (1.25, 1.3),
        (-1.25, -1.3),
        (2.55, 2.5),
        (-2.55, -2.5),
        (1.35, 1.4),
        (-1.35, -1.4),
        (1.2499999999999998, 1.2),
        (-1.2499999999999998, -1.2),
        (1.2500000000000002, 1.3),
        (-1.2500000000000002, -1.3),
        (1e21, 1e21),
        (-1e21, -1e21),
    ],
)
def test_profile_round_matches_js_to_fixed_boundaries(value: float, expected_value: float) -> None:
    assert profile_round(value) == expected_value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_profile_round_rejects_non_finite_values(value: float) -> None:
    with pytest.raises(ScoreError):
        profile_round(value)


@pytest.mark.parametrize(
    ("matches", "wins", "expected_value"),
    [
        (1, 1, 0.05),
        (999, 999, 49.95),
        (1000, 1000, 50.0),
        (1001, 1001, 50.0),
    ],
)
def test_position_reliability_boundaries(matches: int, wins: int, expected_value: float) -> None:
    assert position_base_diff(matches, wins) == pytest.approx(expected_value)


def test_position_zero_is_invalid() -> None:
    with pytest.raises(ScoreError):
        position_base_diff(0, 0)


@pytest.mark.parametrize("matches,wins", [(-1, 0), (10, 11)])
def test_position_invalid_counts_fail_closed(matches: int, wins: int) -> None:
    with pytest.raises(ScoreError):
        position_base_diff(matches, wins)


@pytest.mark.parametrize(
    ("count", "expected_value"),
    [(0, 0.0), (1, 0.1), (99, 9.9), (100, 10.0), (101, 10.0)],
)
def test_synergy_reliability_boundaries(count: int, expected_value: float) -> None:
    samples = [] if count == 0 else [(10.0, count)]
    assert aggregate_pair_synergy(samples) == expected_value


def test_synergy_includes_the_whole_crossing_week_then_stops() -> None:
    samples = [(10.0, 99), (-10.0, 100), (99.0, 100)]
    assert scorer_module._aggregate_pair_synergy(samples) == (-0.05, 199)
    assert aggregate_pair_synergy(samples) == -0.05
    assert aggregate_pair_synergy([(2.0, 100), (99.0, 100)]) == 2.0


def test_synergy_rounds_after_each_week_merge() -> None:
    samples = [(-0.5, 34), (-0.49, 33), (-0.49, 33)]
    assert aggregate_pair_synergy(samples) == -0.5


@pytest.mark.parametrize(
    ("value", "expected_value", "negative_zero"),
    [(-0.005, 0.0, True), (-0.015, -0.01, False)],
)
def test_synergy_uses_js_negative_half_rounding(
    value: float,
    expected_value: float,
    negative_zero: bool,
) -> None:
    result = aggregate_pair_synergy([(value, 100)])
    assert result == expected_value
    if negative_zero:
        assert result == 0.0
        assert math.copysign(1.0, result) < 0
    else:
        assert result != 0.0


@pytest.mark.parametrize("samples", [[(float("nan"), 1)], [(float("inf"), 1)], [(1.0, -1)]])
def test_synergy_invalid_values_fail_closed(samples: list[tuple[float, int]]) -> None:
    with pytest.raises(ScoreError):
        aggregate_pair_synergy(samples)


def _synthetic_inputs(
    *,
    synergies: tuple[SynergySample, ...] = (),
    all_rank: tuple[TimeAggregate, ...] | None = None,
    rank: tuple[TimeAggregate, ...] | None = None,
) -> NormalizedRoshInputs:
    draft = tuple(
        DraftSlot("RADIANT" if hero_id <= 5 else "DIRE", ((hero_id - 1) % 5) + 1, hero_id)
        for hero_id in range(1, 11)
    )
    positions = tuple(PositionAggregate(slot.hero_id, slot.position_id, 500, 1000) for slot in draft)
    if all_rank is None:
        all_rank = tuple(TimeAggregate(slot.hero_id, slot.position_id, 30, 500, 1000, 1000) for slot in draft)
    if rank is None:
        rank = tuple(TimeAggregate(slot.hero_id, slot.position_id, 30, 500, 1000, 1000) for slot in draft)
    return NormalizedRoshInputs(draft, positions, synergies, all_rank, rank)


def test_missing_synergy_pair_is_zero_and_with_vs_remain_separate() -> None:
    samples = (
        SynergySample(1, "with", 1, 2, 3.0, 100),
        SynergySample(1, "vs", 1, 6, -2.0, 100),
    )
    result = score_official_rosh(_synthetic_inputs(synergies=samples), V2_PROFILE)
    hero = next(row for row in result.hero_scores if row.hero_id == 1)
    untouched = next(row for row in result.hero_scores if row.hero_id == 2)
    assert hero.same_team_synergy == 3.0
    assert hero.opponent_matchup_synergy == -2.0
    assert hero.raw_score == 1.0
    assert untouched.same_team_synergy == 0.0
    assert untouched.opponent_matchup_synergy == 0.0


def test_rank_999_falls_back_and_rank_1000_does_not() -> None:
    base = _synthetic_inputs()
    rank = tuple(
        replace(row, bucket_match_count=999, window_win_count=400, window_match_count=1000)
        if row.hero_id == 1
        else replace(row, bucket_match_count=1000, window_win_count=400, window_match_count=1000)
        for row in base.rank_time_stats
    )
    all_rank = tuple(replace(row, window_win_count=600, window_match_count=1000) for row in base.all_rank_time_stats)
    result = score_official_rosh(replace(base, all_rank_time_stats=all_rank, rank_time_stats=rank), V2_PROFILE)
    point = result.minute_points[0]
    slots = {slot.hero_id: slot for slot in point.slots}
    assert slots[1].source == ALL_RANK_FALLBACK
    assert slots[1].win_rate_diff == pytest.approx(10.0)
    assert slots[2].source == DIVINE_IMMORTAL
    assert slots[2].win_rate_diff == pytest.approx(-10.0)
    assert point.rank_source_counts == {DIVINE_IMMORTAL: 9, ALL_RANK_FALLBACK: 1}


def test_minute_is_unavailable_when_both_sources_lack_one_slot() -> None:
    base = _synthetic_inputs()
    all_rank = tuple(row for row in base.all_rank_time_stats if row.hero_id != 1)
    rank = tuple(row for row in base.rank_time_stats if row.hero_id != 1)
    result = score_official_rosh(replace(base, all_rank_time_stats=all_rank, rank_time_stats=rank), V2_PROFILE)
    assert result.minute_points == ()


def test_time_window_uses_adjacent_rows_when_minute_21_is_missing() -> None:
    data = {
        "heroStats": {
            "heroStatsByTime_1": [
                {"heroId": 1, "time": 20, "winCount": 12, "matchCount": 20},
                {"heroId": 1, "time": 22, "winCount": 4, "matchCount": 10},
            ],
            **{f"heroStatsByTime_{position}": [] for position in range(2, 6)},
        }
    }
    rows = scorer_module._normalize_time(data, (DraftSlot("RADIANT", 1, 1),))
    assert [
        (row.minute, row.window_win_count, row.window_match_count, row.bucket_match_count)
        for row in rows
    ] == [(20, 12, 20, 10), (22, 12, 20, 10)]


def test_team_and_relative_scores_use_unrounded_raw_components() -> None:
    synergies: list[SynergySample] = []
    for start, value in ((1, 0.04), (6, -0.04)):
        team = list(range(start, start + 5))
        for index, hero_id in enumerate(team):
            synergies.append(SynergySample(1, "with", hero_id, team[(index + 1) % 5], value, 100))
    result = score_official_rosh(_synthetic_inputs(synergies=tuple(synergies)), V2_PROFILE)
    assert [row.display_score for row in result.hero_scores[:5]] == [0.0] * 5
    assert all(value == 0.0 for value in [row.display_score for row in result.hero_scores[5:]])
    assert result.radiant_team_raw_score == pytest.approx(0.2)
    assert result.radiant_team_score == 0.2
    assert result.dire_team_raw_score == pytest.approx(-0.2)
    assert result.dire_team_score == -0.2
    assert result.relative_advantage_raw == pytest.approx(0.4)
    assert result.relative_advantage == 0.4


def test_golden_fixture_matches_all_hero_team_and_minute_values(
    expected: dict[str, Any], golden_result
) -> None:
    expected_result = expected["result"]
    assert [
        (row.team_side, row.hero_id, row.position_id, row.display_score) for row in golden_result.hero_scores
    ] == [
        (row["team_side"], row["hero_id"], row["position_id"], row["display_score"])
        for row in expected_result["hero_scores"]
    ]
    assert golden_result.radiant_team_score == expected_result["radiant_team_score"] == -4.9
    assert golden_result.dire_team_score == expected_result["dire_team_score"] == -10.7
    assert golden_result.relative_advantage == expected_result["relative_advantage"] == 5.8
    expected_minutes = {row["minute"]: row["display_score"] for row in expected_result["minute_points"]}
    actual_minutes = {row.minute: row.display_score for row in golden_result.minute_points}
    assert {minute: actual_minutes[minute] for minute in expected_minutes} == expected_minutes
    assert len(golden_result.minute_points) == 41
    assert golden_result.relative_advantage != 13.3
    assert golden_result.formula_version == V2_FORMULA_VERSION
    assert golden_result.result_hash == V2_GOLDEN_RESULT_HASH
    assert golden_result.result_hash != V1_GOLDEN_RESULT_HASH


def test_result_hash_binds_the_active_profile_formula_version(normalized, golden_result, monkeypatch) -> None:
    alternate = replace(
        V2_PROFILE,
        rosh_profile_id="stratz-rosh-web-2026-07-28-v2-formula-variant",
        formula_version="stratz-official-rosh/2026-07-28-v2-formula-variant",
    )

    def validate(profile) -> None:
        if profile not in {V2_PROFILE, alternate}:
            raise ValueError("inactive")

    monkeypatch.setattr(official_profile, "validate_active_profile", validate)
    alternate_result = score_official_rosh(normalized, alternate)
    assert alternate_result.radiant_team_raw_score == golden_result.radiant_team_raw_score
    assert alternate_result.minute_points == golden_result.minute_points
    assert alternate_result.formula_version == alternate.formula_version
    assert result_projection(alternate_result)["formula_version"] == alternate.formula_version
    assert alternate_result.result_hash != golden_result.result_hash


def test_golden_minute_audit_is_complete(golden_result) -> None:
    point = next(row for row in golden_result.minute_points if row.minute == 36)
    assert len(point.slots) == 10
    assert sum(point.rank_source_counts.values()) == 10
    assert point.raw_score == pytest.approx((point.radiant_time_delta - point.dire_time_delta) / 10 + point.synergy_delta)
    assert all(slot.match_count >= 0 for slot in point.slots)


def test_explicit_draft_batch_replays_the_same_result(expected, manifest, responses, golden_result) -> None:
    captured_at = datetime.fromisoformat(manifest["provenance"]["captured_at"].replace("Z", "+00:00"))
    plan = build_official_request_plan(
        {
            "mode": "explicit_draft",
            "date_time": expected["input"]["date_time"],
            "bracket_ids": expected["input"]["bracket_ids"],
            "radiant": expected["input"]["radiant"],
            "dire": expected["input"]["dire"],
        },
        request_started_at=captured_at,
    )
    plan = replace(plan, profile=V2_PROFILE)
    result = score_official_rosh(normalize_official_responses(plan, responses[1:]), plan.profile)
    assert result.result_hash == golden_result.result_hash


def test_swapping_radiant_and_dire_reverses_relative_and_minute_scores(normalized, golden_result) -> None:
    swapped_draft = tuple(
        replace(slot, team_side="DIRE" if slot.team_side == "RADIANT" else "RADIANT") for slot in normalized.draft
    )
    swapped = score_official_rosh(replace(normalized, draft=swapped_draft), V2_PROFILE)
    assert swapped.radiant_team_raw_score == pytest.approx(golden_result.dire_team_raw_score)
    assert swapped.dire_team_raw_score == pytest.approx(golden_result.radiant_team_raw_score)
    assert swapped.relative_advantage_raw == pytest.approx(-golden_result.relative_advantage_raw)
    original_minutes = {row.minute: row for row in golden_result.minute_points}
    assert all(row.raw_score == pytest.approx(-original_minutes[row.minute].raw_score) for row in swapped.minute_points)


def test_response_array_order_does_not_change_position_identity_or_result_hash(
    historical_plan, responses, golden_result
) -> None:
    shuffled = copy.deepcopy(responses)
    rng = random.Random(8904419709)
    match = shuffled[0]["data"]["match"]
    rng.shuffle(match["players"])
    rng.shuffle(match["pickBans"])
    position_data = shuffled[1]["data"]["heroStats"]
    for field in ["heroes", *(f"heroesPos_{position}" for position in range(1, 6))]:
        rng.shuffle(position_data[field])
    count_data = shuffled[2]["data"]["stratz"]["page"]["matches"]
    rng.shuffle(count_data["matchesStatsDay"])
    rng.shuffle(count_data["matchesStatsWeek"])
    synergy_data = shuffled[3]["data"]["heroStats"]
    for week in range(1, 5):
        rows = synergy_data[f"matchUp_Prev_Week_{week}"]
        rng.shuffle(rows)
        for row in rows:
            rng.shuffle(row["with"])
            rng.shuffle(row["vs"])
    for response_index in (4, 5):
        time_data = shuffled[response_index]["data"]["heroStats"]
        for position in range(1, 6):
            rng.shuffle(time_data[f"heroStatsByTime_{position}"])
    result = score_official_rosh(normalize_official_responses(historical_plan, shuffled), historical_plan.profile)
    assert result.result_hash == golden_result.result_hash


def _replace_position_count(responses, *, match_count: int, win_count: int):
    batch = list(responses)
    response = dict(batch[1])
    data = dict(response["data"])
    hero_stats = dict(data["heroStats"])
    rows = list(hero_stats["heroesPos_1"])
    rows[0] = {**rows[0], "matchCount": match_count, "winCount": win_count}
    hero_stats["heroesPos_1"] = rows
    data["heroStats"] = hero_stats
    response["data"] = data
    batch[1] = response
    return batch


@pytest.mark.parametrize("match_count,win_count", [(-1, 0), (1, 2)])
def test_normalizer_rejects_invalid_count_rows(historical_plan, responses, match_count, win_count) -> None:
    with pytest.raises(ScoreError):
        normalize_official_responses(
            historical_plan,
            _replace_position_count(responses, match_count=match_count, win_count=win_count),
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_normalizer_rejects_non_finite_synergy(historical_plan, responses, value) -> None:
    batch = list(responses)
    response = dict(batch[3])
    data = dict(response["data"])
    hero_stats = dict(data["heroStats"])
    rows = list(hero_stats["matchUp_Prev_Week_1"])
    row = dict(rows[1])
    pairs = list(row["with"])
    pairs[0] = {**pairs[0], "synergy": value}
    row["with"] = pairs
    rows[1] = row
    hero_stats["matchUp_Prev_Week_1"] = rows
    data["heroStats"] = hero_stats
    response["data"] = data
    batch[3] = response
    with pytest.raises(ScoreError):
        normalize_official_responses(historical_plan, batch)


def test_graphql_errors_fail_closed_without_echoing_upstream_text(historical_plan, responses) -> None:
    batch = list(responses)
    batch[0] = {**batch[0], "errors": [{"message": "Bearer do-not-echo"}]}
    with pytest.raises(ScoreError) as exc_info:
        normalize_official_responses(historical_plan, batch)
    assert "do-not-echo" not in str(exc_info.value)


def test_missing_operation_fails_closed(historical_plan, responses) -> None:
    with pytest.raises(ScoreError):
        normalize_official_responses(historical_plan, responses[:3] + responses[4:])


def test_normalizer_calls_profile_canonical_plan_validator(
    historical_plan,
    responses,
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        official_profile,
        "validate_canonical_request_plan",
        calls.append,
    )
    normalize_official_responses(historical_plan, responses)
    assert calls == [historical_plan]


def test_normalizer_fails_closed_when_canonical_validator_rejects(
    historical_plan,
    responses,
    monkeypatch,
) -> None:
    def reject(_plan) -> None:
        raise ValueError("drift")

    monkeypatch.setattr(
        official_profile,
        "validate_canonical_request_plan",
        reject,
    )
    with pytest.raises(ScoreError, match="canonical validation"):
        normalize_official_responses(historical_plan, responses)


def test_normalizer_fails_closed_when_canonical_validator_is_missing(
    historical_plan,
    responses,
    monkeypatch,
) -> None:
    monkeypatch.delattr(official_profile, "validate_canonical_request_plan")
    with pytest.raises(ScoreError, match="validator is unavailable"):
        normalize_official_responses(historical_plan, responses)


def test_normalizer_rejects_joint_query_query_sha_and_request_hash_drift(
    historical_plan,
    responses,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        official_profile,
        "validate_canonical_request_plan",
        _validate_canonical_request_plan,
    )
    operations = list(historical_plan.operations)
    query = operations[0].query + "\n"
    operations[0] = replace(
        operations[0],
        query=query,
        query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
    )
    drifted = replace(historical_plan, operations=tuple(operations))
    drifted = replace(drifted, request_hash=official_profile.compute_request_hash(drifted))
    with pytest.raises(ScoreError, match="canonical validation"):
        normalize_official_responses(drifted, responses)


def test_normalizer_rejects_joint_variables_and_request_hash_drift(
    historical_plan,
    responses,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        official_profile,
        "validate_canonical_request_plan",
        _validate_canonical_request_plan,
    )
    operations = list(historical_plan.operations)
    variables = dict(operations[0].variables)
    variables["matchId"] += 1
    operations[0] = replace(operations[0], variables=variables)
    drifted = replace(historical_plan, operations=tuple(operations))
    drifted = replace(drifted, request_hash=official_profile.compute_request_hash(drifted))
    with pytest.raises(ScoreError, match="canonical validation"):
        normalize_official_responses(drifted, responses)


def test_v1_profile_cannot_bypass_active_normalize_or_score_gate(
    historical_plan,
    responses,
    normalized,
) -> None:
    v1_profile = get_profile(official_profile.PROFILE_ID)
    assert v1_profile.formula_version == V1_FORMULA_VERSION
    with pytest.raises(ScoreError, match="canonical validation"):
        normalize_official_responses(replace(historical_plan, profile=v1_profile), responses)
    with pytest.raises(ScoreError, match="not active"):
        score_official_rosh(normalized, v1_profile)


def test_scorer_fails_closed_when_active_profile_validator_is_missing(
    normalized,
    monkeypatch,
) -> None:
    monkeypatch.delattr(official_profile, "validate_active_profile")
    with pytest.raises(ScoreError, match="validator is unavailable"):
        score_official_rosh(normalized, V2_PROFILE)


def test_duplicate_historical_draft_fails_closed(historical_plan, responses) -> None:
    batch = list(responses)
    response = dict(batch[0])
    data = dict(response["data"])
    match = dict(data["match"])
    players = list(match["players"])
    players[1] = {**players[1], "heroId": players[0]["heroId"]}
    match["players"] = players
    data["match"] = match
    response["data"] = data
    batch[0] = response
    with pytest.raises(ScoreError):
        normalize_official_responses(historical_plan, batch)


def test_rogue_draft_side_fails_closed() -> None:
    inputs = _synthetic_inputs()
    draft = (replace(inputs.draft[0], team_side="ROGUE"), *inputs.draft[1:])
    with pytest.raises(ScoreError, match="RADIANT or DIRE"):
        score_official_rosh(replace(inputs, draft=draft), V2_PROFILE)


def test_eleventh_draft_slot_fails_closed() -> None:
    inputs = _synthetic_inputs()
    draft = (*inputs.draft, DraftSlot("RADIANT", 1, 11))
    with pytest.raises(ScoreError, match="exactly ten"):
        score_official_rosh(replace(inputs, draft=draft), V2_PROFILE)


def test_result_projection_contains_only_finite_json_numbers(golden_result) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, float):
            assert math.isfinite(value)

    visit(result_projection(golden_result))


def test_fixture_secret_scan() -> None:
    content = b"\n".join(
        (FIXTURE_DIR / name).read_bytes().lower()
        for name in ("requests.json", "responses.sanitized.json")
    )
    for term in (
        b"authorization",
        b"cookie",
        b"token",
        b"password",
        b"session",
        b"bearer ",
        b"email",
        b"steamaccountid",
    ):
        assert term not in content


def test_official_scorer_isolated_from_legacy_probability_and_stake_paths() -> None:
    source = (Path(__file__).parents[1] / "prematch" / "stratz_official_score.py").read_text(encoding="utf-8")
    assert "prematch.stratz_rosh" not in source
    assert "win_probability" not in source
    assert "stake" not in source
    assert "(50 +" not in source
