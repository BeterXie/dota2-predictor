from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from event_intelligence.draft_features import (
    AvailabilityMode,
    DerivedFactProvenance,
    DraftHeroMapEvidence,
    DraftMapEvidence,
    DraftPlayer,
    DraftTarget,
    DraftTeam,
    DraftTeamMapEvidence,
    ExpectedRoleAssignment,
    build_draft_feature_snapshot,
)
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_FEATURE_VERSION,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_PURE_SCHEMA,
    SHRINKAGE_STRENGTH,
    TeamRatingResidualEvidence,
    build_draft_residual_snapshot,
    build_draft_residual_snapshot_with_authority,
    project_draft_residual_features,
    replay_draft_residual_snapshot,
)
from event_intelligence.models import RolePurpose
from event_intelligence.roles import RoleSource
from event_intelligence.team_rating import RatingMapInput
from event_intelligence.team_rating_backtest import (
    LoadedTeamRatingMap,
    TeamRatingCorpus,
    TeamRatingParameters,
    TeamRatingSourceAuthority,
    TeamRatingWalkForwardRun,
    build_team_rating_walk_forward_runs,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
TARGET_CUTOFF = START + timedelta(days=10)
RADIANT_HEROES = (1, 2, 3, 4, 5)
DIRE_HEROES = (6, 7, 8, 9, 10)
RADIANT_PLAYERS = (101, 102, 103, 104, 105)
DIRE_PLAYERS = (201, 202, 203, 204, 205)
PARAMETERS = TeamRatingParameters(400.0, 16.0, 180.0, 1.0)
ALTERNATE_PARAMETERS = TeamRatingParameters(200.0, 32.0, 90.0, 2.0)


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _provenance(
    value: object,
    *,
    cutoff: datetime,
    first_usable_at: datetime | None = None,
) -> DerivedFactProvenance:
    return DerivedFactProvenance(
        cutoff=cutoff,
        first_usable_at=cutoff if first_usable_at is None else first_usable_at,
        input_hash=_digest(value),
        version="draft-residual-test/v1",
    )


def _historical_team(
    team_id: int,
    player_ids: tuple[int, ...],
    hero_ids: tuple[int, ...],
) -> DraftTeam:
    return DraftTeam(
        team_id,
        tuple(
            DraftPlayer(player_id, hero_id)
            for player_id, hero_id in zip(player_ids, hero_ids, strict=True)
        ),
    )


def _target_team(
    team_id: int,
    player_ids: tuple[int, ...],
    hero_ids: tuple[int, ...],
    *,
    confidence: float = 0.9,
) -> DraftTeam:
    return DraftTeam(
        team_id,
        tuple(
            DraftPlayer(
                player_id,
                hero_id,
                ExpectedRoleAssignment(
                    purpose=RolePurpose.EXPECTED_POSITION,
                    source=RoleSource.HISTORICAL_PATTERN,
                    position=position,
                    confidence=confidence,
                    provenance=_provenance(
                        ("target-role", player_id),
                        cutoff=TARGET_CUTOFF - timedelta(minutes=1),
                        first_usable_at=TARGET_CUTOFF,
                    ),
                ),
            )
            for position, (player_id, hero_id) in enumerate(
                zip(player_ids, hero_ids, strict=True),
                start=1,
            )
        ),
    )


def _hero_facts(
    player_ids: tuple[int, ...],
    hero_ids: tuple[int, ...],
    *,
    completed_at: datetime,
    role_confidence: float = 0.9,
    role_cutoff: datetime | None = None,
) -> tuple[DraftHeroMapEvidence, ...]:
    facts = []
    for position, (player_id, hero_id) in enumerate(
        zip(player_ids, hero_ids, strict=True),
        start=1,
    ):
        observed_cutoff = completed_at if role_cutoff is None else role_cutoff
        facts.append(
            DraftHeroMapEvidence(
                player_id=player_id,
                hero_id=hero_id,
                observed_position=position,
                observed_position_confidence=role_confidence,
                observed_role_purpose=RolePurpose.OBSERVED_POSITION,
                observed_role_source=RoleSource.SINGLE_MAP,
                observed_role_provenance=_provenance(
                    ("observed-role", player_id, observed_cutoff),
                    cutoff=observed_cutoff,
                ),
                control_seconds=float(20 + hero_id),
                hero_healing=float(100 + hero_id),
                last_hits=float(150 + hero_id),
                tower_damage=float(300 + hero_id),
                net_worth=float(10_000 + 100 * hero_id),
                buyback_count=hero_id % 2,
            )
        )
    return tuple(facts)


def _map(
    match_id: int,
    *,
    completed_at: datetime,
    duration_seconds: int = 40 * 60,
    radiant_win: bool = True,
    radiant_team_id: int = 10,
    dire_team_id: int = 20,
    radiant_heroes: tuple[int, ...] = RADIANT_HEROES,
    dire_heroes: tuple[int, ...] = DIRE_HEROES,
    role_confidence: float = 0.9,
    role_cutoff: datetime | None = None,
) -> DraftMapEvidence:
    state = _provenance(("state", match_id), cutoff=completed_at)
    return DraftMapEvidence(
        evidence_id=f"map:{match_id}",
        source_input_hash=_digest(("draft-source", match_id)),
        match_id=match_id,
        completed_at=completed_at,
        first_usable_at=completed_at,
        event_id="event-a",
        patch=741,
        duration_seconds=duration_seconds,
        series_id=match_id // 2 + 1,
        map_number=1,
        radiant=_historical_team(
            radiant_team_id,
            RADIANT_PLAYERS,
            radiant_heroes,
        ),
        dire=_historical_team(dire_team_id, DIRE_PLAYERS, dire_heroes),
        radiant_win=radiant_win,
        radiant_hero_evidence=_hero_facts(
            RADIANT_PLAYERS,
            radiant_heroes,
            completed_at=completed_at,
            role_confidence=role_confidence,
            role_cutoff=role_cutoff,
        ),
        dire_hero_evidence=_hero_facts(
            DIRE_PLAYERS,
            dire_heroes,
            completed_at=completed_at,
            role_confidence=role_confidence,
            role_cutoff=role_cutoff,
        ),
        radiant_team_evidence=DraftTeamMapEvidence(
            high_ground_events=2,
            state_provenance=state,
        ),
        dire_team_evidence=DraftTeamMapEvidence(
            high_ground_events=1,
            state_provenance=state,
        ),
    )


def _target(
    *,
    match_id: int = 999,
    radiant_team_id: int = 10,
    dire_team_id: int = 20,
    radiant_heroes: tuple[int, ...] = RADIANT_HEROES,
    dire_heroes: tuple[int, ...] = DIRE_HEROES,
    role_confidence: float = 0.9,
    availability_mode: AvailabilityMode = AvailabilityMode.RECONSTRUCTED,
) -> DraftTarget:
    return DraftTarget(
        match_id=match_id,
        prediction_cutoff=TARGET_CUTOFF,
        event_id="event-a",
        patch=741,
        radiant=_target_team(
            radiant_team_id,
            RADIANT_PLAYERS,
            radiant_heroes,
            confidence=role_confidence,
        ),
        dire=_target_team(
            dire_team_id,
            DIRE_PLAYERS,
            dire_heroes,
            confidence=role_confidence,
        ),
        availability_mode=availability_mode,
        series_id=500,
        map_number=1,
    )


def _team_runs(
    target: DraftTarget,
    history: tuple[DraftMapEvidence, ...],
    *,
    parameters: TeamRatingParameters = PARAMETERS,
) -> tuple[TeamRatingWalkForwardRun, tuple[TeamRatingWalkForwardRun, ...]]:
    distinct_history = {
        row.match_id: row for row in history if row.match_id != target.match_id
    }
    earliest = min(
        (
            row.completed_at - timedelta(seconds=row.duration_seconds)
            for row in distinct_history.values()
        ),
        default=target.prediction_cutoff,
    )
    warmup = RatingMapInput(
        match_id=9_000_000,
        series_id=1,
        event_id="warmup",
        started_at=earliest - timedelta(days=2),
        completed_at=earliest - timedelta(days=2) + timedelta(minutes=40),
        result_usable_at=earliest - timedelta(days=2) + timedelta(minutes=40),
        radiant_team_id=target.radiant.team_id,
        dire_team_id=target.dire.team_id,
        radiant_roster=tuple(player.player_id for player in target.radiant.players),
        dire_roster=tuple(player.player_id for player in target.dire.players),
        radiant_win=True,
    )
    rows = [warmup]
    for row in distinct_history.values():
        started_at = row.completed_at - timedelta(seconds=row.duration_seconds)
        rows.append(
            RatingMapInput(
                match_id=row.match_id,
                series_id=row.series_id,
                event_id=row.event_id,
                started_at=started_at,
                completed_at=row.completed_at,
                result_usable_at=row.completed_at,
                radiant_team_id=row.radiant.team_id,
                dire_team_id=row.dire.team_id,
                radiant_roster=tuple(
                    player.player_id for player in row.radiant.players
                ),
                dire_roster=tuple(player.player_id for player in row.dire.players),
                radiant_win=row.radiant_win,
            )
        )
    rows.append(
        RatingMapInput(
            match_id=target.match_id,
            series_id=target.series_id,
            event_id=target.event_id,
            started_at=target.prediction_cutoff,
            completed_at=target.prediction_cutoff + timedelta(minutes=40),
            result_usable_at=target.prediction_cutoff + timedelta(minutes=40),
            radiant_team_id=target.radiant.team_id,
            dire_team_id=target.dire.team_id,
            radiant_roster=tuple(
                player.player_id for player in target.radiant.players
            ),
            dire_roster=tuple(player.player_id for player in target.dire.players),
            radiant_win=False,
        )
    )
    ordered = tuple(sorted(rows, key=lambda row: (row.started_at, row.match_id)))
    corpus = TeamRatingCorpus(
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        formal_maps=len(ordered),
        maps=tuple(
            LoadedTeamRatingMap(
                row,
                row.started_at,
                "reconstructed_map_start",
                _source(row),
            )
            for row in ordered
        ),
    )
    runs = build_team_rating_walk_forward_runs(corpus, candidates=(parameters,))
    by_match = {run.artifact.target.match_id: run for run in runs}
    return (
        by_match[target.match_id],
        tuple(
            by_match[row.match_id]
            for row in history
            if row.match_id != target.match_id and row.match_id in by_match
        ),
    )


def test_versions_schema_and_single_map_residual_math_are_frozen() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    target_run, history_runs = _team_runs(target, (history,))

    snapshot = build_draft_residual_snapshot(
        target,
        (history,),
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )

    history_probability = history_runs[0].artifact.prediction.raw_probability
    expected_effect = (1.0 - history_probability) / (
        1.0 + SHRINKAGE_STRENGTH
    )
    assert DRAFT_RESIDUAL_FEATURE_VERSION == "draft-residual-features-v1"
    assert tuple(row.name for row in snapshot.pure_features) == (
        DRAFT_RESIDUAL_PURE_SCHEMA
    )
    assert len(DRAFT_RESIDUAL_MODEL_SCHEMA) == 40
    assert len(DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH) == 64
    assert snapshot.feature("hero_residual_diff").value == pytest.approx(
        2.0 * expected_effect
    )
    assert snapshot.feature("role_residual_diff").value == pytest.approx(
        2.0 * expected_effect
    )
    assert snapshot.feature("synergy_residual_diff").value == pytest.approx(
        2.0 * expected_effect
    )
    assert snapshot.feature("counter_residual_edge").value == pytest.approx(
        expected_effect
    )
    assert snapshot.feature("scaling_40m_residual_diff").value == pytest.approx(
        2.0 * expected_effect
    )
    for name in DRAFT_RESIDUAL_PURE_SCHEMA[:5]:
        estimate = snapshot.feature(name)
        assert estimate.support == 1
        assert estimate.effective_support == 1.0
        assert estimate.standard_error is None


def test_proxy_features_exactly_reuse_public_draft_v3_snapshot() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    v3 = build_draft_feature_snapshot(target, (history,))
    target_run, history_runs = _team_runs(target, (history,))
    residual = build_draft_residual_snapshot(
        target,
        (history,),
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )

    for name in DRAFT_RESIDUAL_PURE_SCHEMA[5:]:
        expected = v3.feature(name)
        actual = residual.feature(name)
        assert actual.value == expected.value
        assert actual.support == expected.support
        assert actual.effective_support == float(expected.support)
        assert actual.coverage == expected.coverage
        assert actual.missing_reason == expected.missing_reason
        assert actual.evidence_ids == tuple(sorted(set(expected.evidence_ids)))
        assert actual.standard_error is None


def test_target_outcome_and_future_inputs_do_not_change_snapshot() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    target_as_history = _map(
        target.match_id,
        completed_at=TARGET_CUTOFF + timedelta(minutes=40),
        radiant_win=True,
    )
    changed_target = replace(target_as_history, radiant_win=False)
    future = _map(202, completed_at=TARGET_CUTOFF + timedelta(days=1))
    target_run, all_runs = _team_runs(target, (history, future))
    history_run, future_run = all_runs
    baseline = build_draft_residual_snapshot(
        target,
        (history, target_as_history),
        target_team_rating=target_run,
        team_rating_history=(history_run,),
    )
    changed = build_draft_residual_snapshot(
        target,
        (history, changed_target),
        target_team_rating=target_run,
        team_rating_history=(history_run,),
    )
    extended = build_draft_residual_snapshot(
        target,
        (history, future),
        target_team_rating=target_run,
        team_rating_history=(history_run, future_run),
    )

    assert baseline == changed == extended


def test_history_baseline_revision_changes_residual_value_and_hash() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    target_run, history_runs = _team_runs(target, (history,))
    _alternate_target, alternate_history = _team_runs(
        target,
        (history,),
        parameters=ALTERNATE_PARAMETERS,
    )
    baseline = build_draft_residual_snapshot(
        target,
        (history,),
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )
    changed = build_draft_residual_snapshot(
        target,
        (history,),
        target_team_rating=target_run,
        team_rating_history=alternate_history,
    )

    assert baseline.feature("hero_residual_diff").value != changed.feature(
        "hero_residual_diff"
    ).value
    assert baseline.team_rating_input_hash != changed.team_rating_input_hash
    assert baseline.input_hash != changed.input_hash


def test_missing_baseline_is_unavailable_instead_of_neutral_probability() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    target_run, _history_runs = _team_runs(target, (history,))
    snapshot = build_draft_residual_snapshot(
        target,
        (history,),
        target_team_rating=target_run,
        team_rating_history=(),
    )

    assert snapshot.support == 0
    for name in DRAFT_RESIDUAL_PURE_SCHEMA[:5]:
        estimate = snapshot.feature(name)
        assert estimate.value is None
        assert estimate.coverage == 0.0
        assert estimate.missing_reason is not None


def test_role_cutoff_confidence_and_scaling_boundary_fail_closed() -> None:
    late_role = _map(
        101,
        completed_at=START + timedelta(days=1),
        duration_seconds=2_399,
        role_cutoff=TARGET_CUTOFF + timedelta(minutes=1),
    )
    exact_40 = _map(
        102,
        completed_at=START + timedelta(days=2),
        duration_seconds=2_400,
        role_confidence=0.69,
    )
    target = _target()
    target_run, history_runs = _team_runs(target, (late_role, exact_40))
    snapshot = build_draft_residual_snapshot(
        target,
        (late_role, exact_40),
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )

    assert snapshot.feature("role_residual_diff").value is None
    assert snapshot.feature("role_residual_diff").support == 0
    assert snapshot.feature("scaling_40m_residual_diff").support == 1


def test_side_swap_negates_directional_features_without_changing_support() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    swapped = _target(
        radiant_team_id=20,
        dire_team_id=10,
        radiant_heroes=DIRE_HEROES,
        dire_heroes=RADIANT_HEROES,
    )
    target_run, history_runs = _team_runs(target, (history,))
    swapped_target_run, _swapped_history_runs = _team_runs(swapped, (history,))
    baseline = build_draft_residual_snapshot(
        target,
        (history,),
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )
    swapped_snapshot = build_draft_residual_snapshot(
        swapped,
        (history,),
        target_team_rating=swapped_target_run,
        team_rating_history=history_runs,
    )

    for name in DRAFT_RESIDUAL_PURE_SCHEMA:
        original = baseline.feature(name)
        reversed_value = swapped_snapshot.feature(name)
        assert reversed_value.support == original.support
        assert reversed_value.coverage == original.coverage
        if original.value is None:
            assert reversed_value.value is None
        else:
            assert reversed_value.value == pytest.approx(-original.value)


def test_projection_preserves_missingness_support_and_schema_order() -> None:
    target = _target(role_confidence=0.0)
    target_run, _history_runs = _team_runs(target, ())
    snapshot = build_draft_residual_snapshot(
        target,
        (),
        target_team_rating=target_run,
        team_rating_history=(),
    )
    projected = project_draft_residual_features(snapshot)

    assert tuple(projected) == DRAFT_RESIDUAL_MODEL_SCHEMA
    assert projected["hero_residual_diff"] is None
    assert projected["hero_residual_diff__log1p_support"] == 0.0
    assert projected["hero_residual_diff__coverage"] == 0.0
    assert projected["hero_residual_diff__missing"] == 1.0


def test_authority_replay_is_order_invariant_and_rejects_tampering() -> None:
    first = _map(101, completed_at=START + timedelta(days=1))
    second = _map(102, completed_at=START + timedelta(days=2), radiant_win=False)
    target = _target()
    target_run, history_runs = _team_runs(target, (first, second))
    first_run, second_run = history_runs
    snapshot, authority = build_draft_residual_snapshot_with_authority(
        target,
        (second, first),
        target_team_rating=target_run,
        team_rating_history=(second_run, first_run),
    )
    reordered, reordered_authority = build_draft_residual_snapshot_with_authority(
        target,
        (first, second),
        target_team_rating=target_run,
        team_rating_history=(first_run, second_run),
    )

    assert snapshot == reordered == replay_draft_residual_snapshot(
        authority,
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )
    assert authority == reordered_authority
    tampered = {
        **authority,
        "eligible_team_rating_history": [
            {
                **authority["eligible_team_rating_history"][0],
                "radiant_probability": 0.99,
            },
            *authority["eligible_team_rating_history"][1:],
        ],
    }
    with pytest.raises(ValueError, match="authority claims do not replay"):
        replay_draft_residual_snapshot(
            tampered,
            target_team_rating=target_run,
            team_rating_history=history_runs,
        )
    _alternate_target, alternate_history = _team_runs(
        target,
        (first, second),
        parameters=ALTERNATE_PARAMETERS,
    )
    self_consistent_forgery = {
        **authority,
        "eligible_team_rating_history": [
            TeamRatingResidualEvidence.from_walk_forward_run(
                run,
                include_outcome=True,
            ).to_payload()
            for run in alternate_history
        ],
    }
    with pytest.raises(ValueError, match="authority claims do not replay"):
        replay_draft_residual_snapshot(
            self_consistent_forgery,
            target_team_rating=target_run,
            team_rating_history=history_runs,
        )


def test_duplicate_map_is_counted_once_and_conflicting_duplicate_is_rejected() -> None:
    history = _map(101, completed_at=START + timedelta(days=1))
    target = _target()
    target_run, history_runs = _team_runs(target, (history,))
    history_run = history_runs[0]
    snapshot = build_draft_residual_snapshot(
        target,
        (history, history),
        target_team_rating=target_run,
        team_rating_history=(history_run, history_run),
    )

    assert snapshot.feature("hero_residual_diff").support == 1
    with pytest.raises(ValueError, match="conflicting evidence_id"):
        build_draft_residual_snapshot(
            target,
            (history, replace(history, radiant_win=False)),
            target_team_rating=target_run,
            team_rating_history=(history_run,),
        )


def test_standard_error_is_clustered_by_unique_map() -> None:
    first = _map(101, completed_at=START + timedelta(days=1), radiant_win=True)
    second = _map(102, completed_at=START + timedelta(days=2), radiant_win=False)
    target = _target()
    target_run, history_runs = _team_runs(target, (first, second))
    snapshot = build_draft_residual_snapshot(
        target,
        (first, second),
        target_team_rating=target_run,
        team_rating_history=history_runs,
    )

    estimate = snapshot.feature("hero_residual_diff")
    assert estimate.support == 2
    assert estimate.standard_error is not None
    assert estimate.standard_error > 0.0
    assert all(
        snapshot.feature(name).standard_error is None
        for name in DRAFT_RESIDUAL_PURE_SCHEMA[5:]
    )


def test_reconstructed_team_rating_evidence_cannot_authorize_prospective_target() -> None:
    target = _target(availability_mode=AvailabilityMode.PROSPECTIVE)
    target_run, _history_runs = _team_runs(target, ())

    with pytest.raises(ValueError, match="mode does not match"):
        build_draft_residual_snapshot(
            target,
            (),
            target_team_rating=target_run,
            team_rating_history=(),
        )


def _rating_map(
    match_id: int,
    *,
    started_at: datetime,
    radiant_win: bool,
) -> RatingMapInput:
    completed_at = started_at + timedelta(minutes=40)
    return RatingMapInput(
        match_id=match_id,
        series_id=700,
        event_id="event-a",
        started_at=started_at,
        completed_at=completed_at,
        result_usable_at=completed_at,
        radiant_team_id=10,
        dire_team_id=20,
        radiant_roster=RADIANT_PLAYERS,
        dire_roster=DIRE_PLAYERS,
        radiant_win=radiant_win,
    )


def _source(row: RatingMapInput) -> TeamRatingSourceAuthority:
    return TeamRatingSourceAuthority(
        match_id=row.match_id,
        artifact_id=f"opendota:{row.match_id}",
        content_hash=_digest(("source", row.match_id)),
        artifact_usable_at=row.completed_at,
        observation_usable_at=row.completed_at,
    )


def test_team_rating_evidence_factory_verifies_run_and_strips_target_outcome() -> None:
    warmup = _rating_map(1, started_at=START, radiant_win=True)
    history = _rating_map(
        101,
        started_at=START + timedelta(days=1),
        radiant_win=True,
    )
    target = _rating_map(
        999,
        started_at=TARGET_CUTOFF,
        radiant_win=False,
    )
    corpus = TeamRatingCorpus(
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        formal_maps=3,
        maps=tuple(
            LoadedTeamRatingMap(
                row,
                row.started_at,
                "reconstructed_map_start",
                _source(row),
            )
            for row in (warmup, history, target)
        ),
    )
    runs = build_team_rating_walk_forward_runs(corpus, candidates=(PARAMETERS,))

    with pytest.raises(ValueError, match="trained run"):
        TeamRatingResidualEvidence.from_walk_forward_run(
            runs[0], include_outcome=True
        )
    with pytest.raises(ValueError, match="status does not match"):
        TeamRatingResidualEvidence.from_walk_forward_run(
            replace(runs[0], status="trained"),
            include_outcome=True,
        )
    with pytest.raises(ValueError, match="selection support"):
        TeamRatingResidualEvidence.from_walk_forward_run(
            replace(
                runs[0],
                selection=replace(runs[0].selection, support=1),
                status="trained",
            ),
            include_outcome=True,
        )
    history_evidence = TeamRatingResidualEvidence.from_walk_forward_run(
        runs[1], include_outcome=True
    )
    target_evidence = TeamRatingResidualEvidence.from_walk_forward_run(
        runs[2], include_outcome=False
    )

    assert history_evidence.radiant_win is True
    assert target_evidence.radiant_win is None
    assert target_evidence.first_usable_at is None
    assert target_evidence.reconstruction_rule is not None
    assert target_evidence.run_id == runs[2].run_id
    assert target_evidence.prediction_input_hash == (
        runs[2].artifact.prediction.input_hash
    )


def test_datetime_mode_and_conflicting_duplicate_evidence_fail_closed() -> None:
    target = _target()
    history = _map(101, completed_at=START + timedelta(days=1))
    target_run, history_runs = _team_runs(target, (history,))
    _alternate_target, alternate_history = _team_runs(
        target,
        (history,),
        parameters=ALTERNATE_PARAMETERS,
    )
    evidence = TeamRatingResidualEvidence.from_walk_forward_run(
        history_runs[0],
        include_outcome=True,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(
            evidence,
            prediction_cutoff=evidence.prediction_cutoff.replace(tzinfo=None),
            evidence_hash="",
        )
    with pytest.raises(ValueError, match="not canonical"):
        replace(
            evidence,
            target_started_at=evidence.target_started_at - timedelta(minutes=1),
            evidence_hash="",
        )
    later_target = replace(
        target,
        prediction_cutoff=target.prediction_cutoff + timedelta(minutes=1),
    )
    later_target_run, _later_history = _team_runs(later_target, (history,))
    with pytest.raises(ValueError, match="starts disagree"):
        build_draft_residual_snapshot(
            target,
            (history,),
            target_team_rating=later_target_run,
            team_rating_history=history_runs,
        )
    with pytest.raises(ValueError, match="conflicting Team Rating"):
        build_draft_residual_snapshot(
            target,
            (history,),
            target_team_rating=target_run,
            team_rating_history=(history_runs[0], alternate_history[0]),
        )
