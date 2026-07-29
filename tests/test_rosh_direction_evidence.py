from __future__ import annotations

from dataclasses import replace

from live_betting.official_rosh_shadow_strategy import (
    OfficialRoshDirectionShadowStrategy,
)
from live_betting.rosh_evidence import (
    RoshEvidenceError,
    build_rosh_direction_evidence,
    official_rosh_draft_hash,
    resolve_underdog_draft_side,
)
from live_betting.rosh_parity_storage import (
    RoshMinutePointRecord,
    RoshRunRecord,
    StoredRoshRun,
)
from live_betting.strategy_contract import (
    OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION,
    PROPOSED_STRATEGY_VERSION,
    REGISTERED_OFFICIAL_ROSH_STRATEGY_CONTRACTS,
    REGISTERED_STRATEGY_CONTRACTS,
    build_official_rosh_strategy_contract,
)
from prematch.stratz_official_profile import get_profile


def _run(*, minute_36: float = -5.5, minute_37: float = 9.9) -> StoredRoshRun:
    profile = get_profile()
    record = RoshRunRecord(
        run_id="a" * 64,
        status="succeeded",
        mode="explicit_draft",
        match_id=None,
        date_time=1_785_000_000,
        draft_hash="b" * 64,
        draft={"radiant": [], "dire": []},
        rosh_profile_id=profile.rosh_profile_id,
        formula_version=profile.formula_version,
        request_profile_hash=profile.request_profile_hash,
        upstream_bundle_hash=profile.upstream_bundle_hash,
        scorer_source_hash=profile.scorer_source_hash,
        canonical_profile_hash=profile.canonical_profile_hash,
        serialization_version=profile.serialization_version,
        request_hash="c" * 64,
        request_manifest={},
        response_manifest=(),
        evidence_hash="d" * 64,
        collected_at="2026-07-29T00:00:00+00:00",
        radiant_team_score=-4.9,
        dire_team_score=-10.7,
        relative_advantage=5.8,
    )
    minutes = tuple(
        RoshMinutePointRecord(
            minute=minute,
            raw_score=score,
            display_score=score,
            radiant_time_delta=0.0,
            dire_time_delta=0.0,
            synergy_delta=0.0,
            source_audit={"rank_source_counts": {}, "slots": []},
        )
        for minute, score in ((36, minute_36), (37, minute_37))
    )
    return StoredRoshRun(record, (), minutes, {})


def test_latest_reached_minute_excludes_future_bucket_and_converts_dire() -> None:
    evidence = build_rosh_direction_evidence(
        _run(),
        observation_draft_hash="b" * 64,
        game_clock_seconds=36 * 60 + 59,
        underdog_side="DIRE",
    )

    assert evidence.selected_minute == 36
    assert evidence.radiant_score == -5.5
    assert evidence.underdog_direction_score == 5.5
    assert evidence.direction == "supports_underdog"
    assert len(evidence.evidence_hash) == 64


def test_official_draft_hash_is_position_aware_and_rejects_invalid_lineups() -> None:
    first = official_rosh_draft_hash((1, 2, 3, 4, 5), (6, 7, 8, 9, 10))
    reordered = official_rosh_draft_hash((2, 1, 3, 4, 5), (6, 7, 8, 9, 10))

    assert len(first) == 64
    assert reordered != first
    try:
        official_rosh_draft_hash((1, 1, 3, 4, 5), (6, 7, 8, 9, 10))
    except RoshEvidenceError as error:
        assert error.reason == "rosh_lineup_draft_mismatch"
    else:
        raise AssertionError("duplicate official Rosh draft must fail closed")


def test_team_side_conversion_requires_confirmed_radiant_mapping() -> None:
    assert resolve_underdog_draft_side(
        "team_two", radiant_team_side="team_one"
    ) == "DIRE"
    try:
        resolve_underdog_draft_side("team_two")
    except RoshEvidenceError as error:
        assert error.reason == "team_side_not_confirmed"
    else:
        raise AssertionError("unconfirmed team side must fail closed")


def test_v6_shadow_candidate_has_no_probability_edge_stake_or_order() -> None:
    result = OfficialRoshDirectionShadowStrategy().evaluate(
        run=_run(minute_36=5.5),
        observation_draft_hash="b" * 64,
        game_clock_seconds=36 * 60,
        underdog_side="RADIANT",
    )

    assert result.status == "shadow_candidate"
    assert result.reason == "calibrated_probability_unavailable"
    assert result.calibrated_probability is None
    assert result.edge is None
    assert result.stake_multiplier is None
    assert result.paper_order is None
    assert result.as_record()["cohort"] == {
        "m3_c": "shadow_candidate_or_rejection",
        "m3_e": None,
    }


def test_v6_rejects_neutral_opposition_and_evidence_hash_mismatch() -> None:
    strategy = OfficialRoshDirectionShadowStrategy()
    neutral = strategy.evaluate(
        run=_run(minute_36=0.0),
        observation_draft_hash="b" * 64,
        game_clock_seconds=36 * 60,
        underdog_side="RADIANT",
    )
    opposing = strategy.evaluate(
        run=_run(minute_36=-1.0),
        observation_draft_hash="b" * 64,
        game_clock_seconds=36 * 60,
        underdog_side="RADIANT",
    )
    valid = build_rosh_direction_evidence(
        _run(minute_36=1.0),
        observation_draft_hash="b" * 64,
        game_clock_seconds=36 * 60,
        underdog_side="RADIANT",
    )
    mismatch = strategy.evaluate(
        run=_run(minute_36=1.0),
        observation_draft_hash="b" * 64,
        game_clock_seconds=36 * 60,
        underdog_side="RADIANT",
        direction_evidence=replace(valid, evidence_hash="f" * 64),
    )

    assert neutral.reason == "rosh_direction_neutral"
    assert opposing.reason == "rosh_direction_opposes_underdog"
    assert mismatch.reason == "rosh_evidence_hash_mismatch"
    assert all(result.paper_order is None for result in (neutral, opposing, mismatch))


def test_v6_contract_is_separate_from_existing_v5_and_content_addressed() -> None:
    contract = build_official_rosh_strategy_contract()

    assert PROPOSED_STRATEGY_VERSION in REGISTERED_STRATEGY_CONTRACTS
    assert OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION not in REGISTERED_STRATEGY_CONTRACTS
    assert (
        OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION
        in REGISTERED_OFFICIAL_ROSH_STRATEGY_CONTRACTS
    )
    assert contract.strategy_version == OFFICIAL_ROSH_DIRECTION_STRATEGY_VERSION
    assert contract.policy_artifact["probability"]["rosh_score_to_probability"] is False
    assert contract.policy_artifact["stake"]["rosh_magnitude_used"] is False
    assert contract.policy_artifact["execution"]["paper_order_creation"] is False
