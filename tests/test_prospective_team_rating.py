from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.prospective_team_rating import (
    AuthoritativeResult,
    PROSPECTIVE_CUTOFF_SOURCE,
    ProspectiveTarget,
    archive_run_artifact,
    build_prospective_team_rating_run,
    build_prospective_team_rating_seed,
    build_prospective_team_rating_storage_records,
    canonical_authoritative_results,
    load_prospective_team_rating_seed_json,
    team_rating_state_hash,
    verify_prospective_team_rating_run,
    verify_prospective_team_rating_seed,
)
from event_intelligence.team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
    TeamRatingTarget,
)


UTC = timezone.utc
ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG = TeamRatingConfig(
    initial_rating=1_500.0,
    scale=400.0,
    k_factor=16.0,
    inactivity_half_life_days=180.0,
    roster_carry_power=1.0,
    radiant_side_logit=0.0,
    config_version=TEAM_RATING_VERSION,
)


def _result(
    match_id: int,
    *,
    started_hours: int,
    usable_hours: int,
    radiant_win: bool | None = None,
) -> AuthoritativeResult:
    started_at = ORIGIN + timedelta(hours=started_hours)
    usable_at = ORIGIN + timedelta(hours=usable_hours)
    return AuthoritativeResult(
        row=RatingMapInput(
            match_id=match_id,
            series_id=10_000 + match_id // 2,
            event_id="formal-event",
            started_at=started_at,
            completed_at=started_at + timedelta(hours=1),
            result_usable_at=usable_at,
            radiant_team_id=1 + match_id % 4,
            dire_team_id=10 + match_id % 5,
            radiant_roster=(),
            dire_roster=(),
            radiant_win=(match_id % 2 == 0 if radiant_win is None else radiant_win),
        ),
        source_artifact_hash=f"{match_id:064x}",
        observed_at=usable_at + timedelta(minutes=1),
    )


def _seed(support: int = 20):
    results = tuple(
        _result(index, started_hours=index * 3, usable_hours=index * 3 + 2)
        for index in range(1, support + 1)
    )
    cutoff = ORIGIN + timedelta(hours=support * 3 + 3)
    return build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=results,
        seed_as_of=cutoff,
        seed_training_cutoff=cutoff,
        frozen_at=cutoff + timedelta(hours=1),
    )


def _target(match_id: int, started_at: datetime) -> ProspectiveTarget:
    return ProspectiveTarget(
        TeamRatingTarget(
            match_id=match_id,
            series_id=20_000 + match_id // 3,
            event_id="future-formal-event",
            started_at=started_at,
            radiant_team_id=1 + match_id % 4,
            dire_team_id=10 + match_id % 5,
            radiant_roster=(),
            dire_roster=(),
        ),
        prediction_cutoff=started_at,
    )


def test_seed_round_trip_and_hash_tamper_rejection() -> None:
    seed = _seed()

    assert load_prospective_team_rating_seed_json(seed.artifact_json) == seed
    assert seed.state_hash == team_rating_state_hash(seed.states)

    with pytest.raises(ValueError, match="content-addressed"):
        verify_prospective_team_rating_seed(
            replace(seed, artifact_hash="f" * 64)
        )


def test_result_manifest_orders_by_availability_then_map_time() -> None:
    later_map_earlier_result = _result(31, started_hours=20, usable_hours=22)
    earlier_map_later_result = _result(32, started_hours=10, usable_hours=23)
    cutoff = ORIGIN + timedelta(hours=30)

    ordered = canonical_authoritative_results(
        (earlier_map_later_result, later_map_earlier_result),
        after=ORIGIN,
        cutoff=cutoff,
    )

    assert tuple(value.row.match_id for value in ordered) == (31, 32)
    with pytest.raises(ValueError, match="target map"):
        canonical_authoritative_results(
            (later_map_earlier_result,),
            after=ORIGIN,
            cutoff=cutoff,
            target_match_id=31,
        )
    with pytest.raises(ValueError, match="follows"):
        canonical_authoritative_results(
            (_result(33, started_hours=29, usable_hours=31),),
            after=ORIGIN,
            cutoff=cutoff,
        )


def test_twenty_map_seed_incremental_replay_and_storage_contract() -> None:
    seed = _seed(20)
    base_at = seed.seed_as_of
    applied = (
        _result(101, started_hours=70, usable_hours=72),
        _result(102, started_hours=73, usable_hours=75),
    )
    target = _target(501, ORIGIN + timedelta(hours=80))

    run = build_prospective_team_rating_run(
        seed=seed,
        base_authority_hash=None,
        base_as_of=base_at,
        base_states=seed.states,
        applied_results=applied,
        target=target,
        created_at=target.prediction_cutoff - timedelta(minutes=5),
    )
    repeated = build_prospective_team_rating_run(
        seed=seed,
        base_authority_hash=None,
        base_as_of=base_at,
        base_states=seed.states,
        applied_results=tuple(reversed(applied)),
        target=target,
        created_at=target.prediction_cutoff - timedelta(minutes=5),
    )
    run_record, prediction_record, snapshots = (
        build_prospective_team_rating_storage_records(run)
    )

    assert repeated == run
    assert run_record.availability_mode == "prospective"
    assert run_record.status == "trained"
    assert prediction_record.status == "predicted"
    assert prediction_record.eventual_radiant_win is None
    assert prediction_record.cutoff_source == PROSPECTIVE_CUTOFF_SOURCE
    assert prediction_record.raw_probability == run.prediction.raw_probability
    assert snapshots


def test_one_hundred_future_targets_are_deterministic_and_team_only() -> None:
    seed = _seed(20)
    artifacts = set()
    for index in range(100):
        started_at = seed.frozen_at + timedelta(days=1, minutes=index)
        target = _target(10_000 + index, started_at)
        run = build_prospective_team_rating_run(
            seed=seed,
            base_authority_hash=None,
            base_as_of=seed.seed_as_of,
            base_states=seed.states,
            applied_results=(),
            target=target,
            created_at=started_at - timedelta(minutes=10),
        )
        verify_prospective_team_rating_run(run)
        artifact = run.artifact_json
        assert "pure_rosh_score" not in artifact
        assert "draft" not in artifact.lower()
        assert "cluster" not in artifact.lower()
        assert "odds" not in artifact.lower()
        artifacts.add(run.artifact_hash)

    assert len(artifacts) == 100


def test_cutoff_and_artifact_immutability(tmp_path) -> None:
    seed = _seed()
    target = _target(900, seed.frozen_at + timedelta(days=1))

    with pytest.raises(ValueError, match="available before cutoff"):
        build_prospective_team_rating_run(
            seed=seed,
            base_authority_hash=None,
            base_as_of=seed.seed_as_of,
            base_states=seed.states,
            applied_results=(),
            target=target,
            created_at=target.prediction_cutoff,
        )

    run = build_prospective_team_rating_run(
        seed=seed,
        base_authority_hash=None,
        base_as_of=seed.seed_as_of,
        base_states=seed.states,
        applied_results=(),
        target=target,
        created_at=target.prediction_cutoff - timedelta(minutes=1),
    )
    first = archive_run_artifact(run, tmp_path)
    second = archive_run_artifact(run, tmp_path)

    assert first == second
    assert first.read_text(encoding="utf-8") == run.artifact_json

    first.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="immutable"):
        archive_run_artifact(run, tmp_path)
