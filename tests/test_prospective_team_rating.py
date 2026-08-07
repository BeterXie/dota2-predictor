from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    canonical_team_rating_replay_results,
    load_prospective_team_rating_seed_json,
    rebuild_prospective_team_rating_state,
    team_rating_state_hash,
    verify_prospective_team_rating_run,
    verify_prospective_team_rating_seed,
)
from event_intelligence.team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
    TeamRatingTarget,
    replay_team_ratings,
)
from event_intelligence.raw_archive import canonical_json_bytes


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
    duration_hours: int = 1,
    radiant_team_id: int | None = None,
    dire_team_id: int | None = None,
) -> AuthoritativeResult:
    started_at = ORIGIN + timedelta(hours=started_hours)
    usable_at = ORIGIN + timedelta(hours=usable_hours)
    return AuthoritativeResult(
        row=RatingMapInput(
            match_id=match_id,
            series_id=10_000 + match_id // 2,
            event_id="formal-event",
            started_at=started_at,
            completed_at=started_at + timedelta(hours=duration_hours),
            result_usable_at=usable_at,
            radiant_team_id=(
                1 + match_id % 4 if radiant_team_id is None else radiant_team_id
            ),
            dire_team_id=(
                10 + match_id % 5 if dire_team_id is None else dire_team_id
            ),
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
    assert tuple(
        value.row.match_id for value in canonical_team_rating_replay_results(ordered)
    ) == (32, 31)
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


def test_authority_duplicates_are_idempotent_and_conflicts_fail_closed() -> None:
    result = _result(40, started_hours=10, usable_hours=12)
    cutoff = ORIGIN + timedelta(hours=20)

    assert canonical_authoritative_results(
        (result, result), after=ORIGIN, cutoff=cutoff
    ) == (result,)
    with pytest.raises(ValueError, match="conflicting result authority"):
        canonical_authoritative_results(
            (result, replace(result, source_artifact_hash="f" * 64)),
            after=ORIGIN,
            cutoff=cutoff,
        )


def test_seed_separates_availability_authority_from_chronological_replay() -> None:
    later_map_earlier_result = _result(41, started_hours=20, usable_hours=22)
    earlier_map_late_backfill = _result(42, started_hours=10, usable_hours=23)
    cutoff = ORIGIN + timedelta(hours=30)

    seed = build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=(earlier_map_late_backfill, later_map_earlier_result),
        seed_as_of=cutoff,
        seed_training_cutoff=cutoff,
        frozen_at=cutoff + timedelta(hours=1),
    )

    assert tuple(row.row.match_id for row in seed.source_manifest) == (41, 42)
    assert tuple(row.row.match_id for row in seed.rating_replay_order) == (42, 41)
    assert seed.source_manifest_hash != seed.rating_replay_order_hash
    verify_prospective_team_rating_seed(seed)


def test_operational_failure_pair_replays_without_changing_source_times() -> None:
    current_started = datetime(2026, 4, 24, 18, 23, 21, tzinfo=UTC)
    current_completed = datetime(2026, 4, 24, 19, 5, tzinfo=UTC)
    previous_started = datetime(2026, 4, 24, 19, 40, tzinfo=UTC)
    previous_completed = datetime(2026, 4, 24, 20, 21, 37, tzinfo=UTC)
    first_usable = datetime(2026, 7, 13, 12, 26, 9, 977647, tzinfo=UTC)
    second_usable = datetime(2026, 7, 13, 12, 26, 10, 250873, tzinfo=UTC)

    def authority(
        match_id: int,
        started_at: datetime,
        completed_at: datetime,
        usable_at: datetime,
        radiant_team_id: int,
    ) -> AuthoritativeResult:
        return AuthoritativeResult(
            RatingMapInput(
                match_id=match_id,
                series_id=match_id,
                event_id="operational-regression",
                started_at=started_at,
                completed_at=completed_at,
                result_usable_at=usable_at,
                radiant_team_id=radiant_team_id,
                dire_team_id=radiant_team_id + 100,
                radiant_roster=(),
                dire_roster=(),
                radiant_win=True,
            ),
            source_artifact_hash=f"{match_id:064x}",
            observed_at=second_usable + timedelta(seconds=1),
        )

    later_map = authority(
        8_784_700_248,
        previous_started,
        previous_completed,
        first_usable,
        1,
    )
    earlier_map_late = authority(
        8_784_597_508,
        current_started,
        current_completed,
        second_usable,
        2,
    )
    cutoff = second_usable + timedelta(seconds=1)
    seed = build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=(later_map, earlier_map_late),
        seed_as_of=cutoff,
        seed_training_cutoff=cutoff,
        frozen_at=cutoff + timedelta(seconds=1),
    )

    assert tuple(row.row.match_id for row in seed.source_manifest) == (
        8_784_700_248,
        8_784_597_508,
    )
    assert tuple(row.row.match_id for row in seed.rating_replay_order) == (
        8_784_597_508,
        8_784_700_248,
    )
    assert seed.rating_replay_order[0].row.started_at == current_started
    assert seed.rating_replay_order[1].row.completed_at == previous_completed


def test_earlier_map_available_after_cutoff_remains_excluded() -> None:
    cutoff = ORIGIN + timedelta(hours=20)
    result = _result(43, started_hours=1, usable_hours=21)

    with pytest.raises(ValueError, match="follows prospective prediction cutoff"):
        canonical_authoritative_results((result,), after=None, cutoff=cutoff)


def test_same_start_tie_break_and_team_overlap_validation() -> None:
    same_start_long = _result(
        50,
        started_hours=10,
        usable_hours=14,
        duration_hours=2,
        radiant_team_id=1,
        dire_team_id=11,
    )
    same_start_short = _result(
        51,
        started_hours=10,
        usable_hours=14,
        duration_hours=1,
        radiant_team_id=2,
        dire_team_id=12,
    )
    assert tuple(
        row.row.match_id
        for row in canonical_team_rating_replay_results(
            (same_start_long, same_start_short)
        )
    ) == (51, 50)

    first = _result(
        52,
        started_hours=10,
        usable_hours=14,
        duration_hours=3,
        radiant_team_id=3,
        dire_team_id=13,
    )
    overlapping = _result(
        53,
        started_hours=12,
        usable_hours=15,
        duration_hours=1,
        radiant_team_id=3,
        dire_team_id=14,
    )
    with pytest.raises(ValueError, match="overlapping_team_match_chronology"):
        canonical_team_rating_replay_results((first, overlapping))


def test_late_backfill_forces_full_rebuild_equal_to_zero_state_replay() -> None:
    seed_result = _result(60, started_hours=20, usable_hours=22)
    seed_cutoff = ORIGIN + timedelta(hours=30)
    seed = build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=(seed_result,),
        seed_as_of=seed_cutoff,
        seed_training_cutoff=seed_cutoff,
        frozen_at=seed_cutoff + timedelta(hours=1),
    )
    late_backfill = _result(61, started_hours=10, usable_hours=40)
    target = _target(601, ORIGIN + timedelta(hours=50))
    run = build_prospective_team_rating_run(
        seed=seed,
        base_authority_hash=None,
        base_as_of=seed.seed_as_of,
        base_states=seed.states,
        applied_results=(late_backfill,),
        target=target,
        created_at=target.prediction_cutoff - timedelta(minutes=1),
    )
    applied, replay_order, rebuilt = rebuild_prospective_team_rating_state(
        seed,
        (late_backfill,),
        cutoff=target.prediction_cutoff,
        target_match_id=target.target.match_id,
    )
    full_replay = build_prospective_team_rating_seed(
        config=CONFIG,
        source_results=(seed_result, late_backfill),
        seed_as_of=target.prediction_cutoff,
        seed_training_cutoff=target.prediction_cutoff,
        frozen_at=target.prediction_cutoff + timedelta(minutes=1),
    )

    assert applied == (late_backfill,)
    assert tuple(row.row.match_id for row in replay_order) == (61, 60)
    assert run.state_before_target == rebuilt
    assert run.rating_replay_order == replay_order
    assert run.rating_replay_order_hash == full_replay.rating_replay_order_hash
    assert run.state_before_target == full_replay.states


def test_seed_artifact_rejects_authority_replay_and_state_tampering() -> None:
    seed = _seed(3)
    payload = json.loads(seed.artifact_json)
    payload["rating_replay_order"][0]["result"]["started_at"] = (
        ORIGIN - timedelta(hours=1)
    ).isoformat()
    tampered = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError):
        load_prospective_team_rating_seed_json(tampered)
    with pytest.raises(ValueError, match="exact replay disagrees"):
        verify_prospective_team_rating_seed(
            replace(seed, rating_replay_order_hash="f" * 64)
        )
    with pytest.raises(ValueError, match="exact replay disagrees"):
        verify_prospective_team_rating_seed(replace(seed, state_hash="f" * 64))

    first = seed.source_manifest[0]
    row = first.row
    valid_authority_tampering = (
        replace(first, source_artifact_hash="f" * 64),
        replace(first, row=replace(row, started_at=row.started_at - timedelta(seconds=1))),
        replace(first, row=replace(row, completed_at=row.completed_at - timedelta(seconds=1))),
        replace(
            first,
            row=replace(
                row,
                result_usable_at=row.result_usable_at + timedelta(seconds=1),
            ),
        ),
    )
    for changed in valid_authority_tampering:
        with pytest.raises(ValueError, match="exact replay disagrees"):
            verify_prospective_team_rating_seed(
                replace(seed, source_manifest=(changed, *seed.source_manifest[1:]))
            )
    with pytest.raises(ValueError, match="exact replay disagrees"):
        verify_prospective_team_rating_seed(
            replace(seed, source_manifest_hash="f" * 64)
        )
    with pytest.raises(ValueError, match="exact replay disagrees"):
        verify_prospective_team_rating_seed(
            replace(seed, rating_replay_order=tuple(reversed(seed.rating_replay_order)))
        )
    with pytest.raises(ValueError, match="exact replay disagrees"):
        verify_prospective_team_rating_seed(
            replace(seed, config=replace(seed.config, k_factor=seed.config.k_factor + 1.0))
        )
    with pytest.raises(ValueError, match="exact replay disagrees"):
        verify_prospective_team_rating_seed(
            replace(
                seed,
                states=(
                    replace(seed.states[0], rating=seed.states[0].rating + 1.0),
                    *seed.states[1:],
                ),
            )
        )


def test_reconstructed_team_rating_replay_contract_is_unchanged() -> None:
    rows = tuple(
        _result(index, started_hours=index * 3, usable_hours=index * 3 + 2).row
        for index in range(1, 5)
    )
    cutoff = ORIGIN + timedelta(hours=20)

    assert replay_team_ratings(rows, cutoff, CONFIG) == replay_team_ratings(
        tuple(reversed(rows)), cutoff, CONFIG
    )


def test_accepted_fixed_config_artifact_identity_is_unchanged() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "event_intelligence"
        / "resources"
        / "team_rating_accepted_config_v1.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert hashlib.sha256(canonical_json_bytes(payload)).hexdigest() == (
        "b527319ab1035d6cae6550820cd0854b467f845537d033909b4f2e45e706c19a"
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
