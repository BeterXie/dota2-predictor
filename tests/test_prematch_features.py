from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import event_intelligence.prematch_features as prematch_features
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_FEATURE_VERSION,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_PURE_SCHEMA,
    DraftResidualSnapshot,
    ResidualFeatureEstimate,
    build_team_rating_residual_evidence_cache,
)
from event_intelligence.prematch_features import (
    PREMATCH_FEATURE_SCHEMA_HASHES,
    PREMATCH_FEATURE_SCHEMAS,
    PREMATCH_FEATURE_VERSION,
    TEAM_PLUS_DRAFT_ROSH_SCHEMA,
    PrematchFeatureSnapshot,
    build_prematch_feature_snapshot,
    prematch_feature_schema,
    project_prematch_features,
    verify_prematch_feature_snapshot,
)
from event_intelligence.rosh_features import (
    ROSH_AUTHORITY_SCHEMA,
    ROSH_FEATURE_VERSION,
    ROSH_MODEL_SCHEMA,
    ROSH_UNAVAILABLE_AUTHORITY_SCHEMA,
    RoshFeatureSnapshot,
    build_unavailable_rosh_feature_snapshot_with_authority,
)
from event_intelligence.team_rating import (
    TEAM_RATING_VERSION,
    RatingMapInput,
    TeamRatingConfig,
)
from event_intelligence.team_rating_artifacts import build_team_rating_artifact
from event_intelligence.team_rating_backtest import (
    ParameterSelection,
    TeamRatingParameters,
    TeamRatingSourceAuthority,
    TeamRatingWalkForwardRun,
    combined_team_rating_training_input_hash,
    team_rating_authority_fingerprint,
    team_rating_run_id,
)


UTC = timezone.utc
TARGET_CUTOFF = datetime(2026, 7, 1, tzinfo=UTC)
RADIANT_HEROES = (1, 2, 3, 4, 5)
DIRE_HEROES = (6, 7, 8, 9, 10)


def _digest(number: int) -> str:
    return f"{number:064x}"


def _map(
    match_id: int,
    started_at: datetime,
    *,
    radiant_win: bool,
) -> RatingMapInput:
    return RatingMapInput(
        match_id=match_id,
        series_id=max(1, match_id // 2),
        event_id="event-a",
        started_at=started_at,
        completed_at=started_at + timedelta(minutes=40),
        result_usable_at=started_at + timedelta(minutes=41),
        radiant_team_id=10 + match_id % 2,
        dire_team_id=20 + match_id % 2,
        radiant_roster=(1, 2, 3, 4, 5),
        dire_roster=(6, 7, 8, 9, 10),
        radiant_win=radiant_win,
    )


def _source(row: RatingMapInput) -> TeamRatingSourceAuthority:
    return TeamRatingSourceAuthority(
        match_id=row.match_id,
        artifact_id=f"map-{row.match_id}",
        content_hash=_digest(100 + row.match_id),
        artifact_usable_at=None,
        observation_usable_at=None,
    )


def _team_run(*, outcome: bool = True) -> TeamRatingWalkForwardRun:
    history = (
        _map(1, TARGET_CUTOFF - timedelta(days=3), radiant_win=True),
        _map(2, TARGET_CUTOFF - timedelta(days=2), radiant_win=False),
    )
    target = _map(100, TARGET_CUTOFF, radiant_win=outcome)
    config = TeamRatingConfig(
        initial_rating=1_500.0,
        scale=400.0,
        k_factor=16.0,
        inactivity_half_life_days=None,
        roster_carry_power=1.0,
        radiant_side_logit=0.0,
        config_version=TEAM_RATING_VERSION,
    )
    artifact = build_team_rating_artifact(
        history,
        target=target,
        prediction_cutoff=TARGET_CUTOFF,
        training_cutoff=TARGET_CUTOFF,
        config=config,
    )
    target_source = _source(target)
    training_sources = tuple(_source(row) for row in artifact.ordered_training_corpus)
    authority = team_rating_authority_fingerprint(
        target_source=target_source,
        ordered_training_sources=training_sources,
    )
    combined = combined_team_rating_training_input_hash(
        artifact_training_input_hash=artifact.training_input_hash,
        authority_fingerprint=authority,
    )
    return TeamRatingWalkForwardRun(
        run_id=team_rating_run_id(
            availability_mode=AvailabilityMode.RECONSTRUCTED,
            artifact_hash=artifact.artifact_hash,
            authority_fingerprint=authority,
        ),
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        cutoff_source="reconstructed_map_start",
        selection=ParameterSelection(
            parameters=TeamRatingParameters(400.0, 16.0, None, 1.0),
            support=len(history),
            log_loss=0.6,
            brier_score=0.2,
        ),
        config=config,
        artifact=artifact,
        target_source_authority=target_source,
        ordered_training_sources=training_sources,
        authority_fingerprint=authority,
        combined_training_input_hash=combined,
        series_id=target.series_id,
        event_id=target.event_id,
        eventual_radiant_win=outcome,
        radiant_prior_probability=0.5,
        status="trained",
    )


def _draft_snapshot(
    *,
    match_id: int = 100,
    prediction_cutoff: datetime = TARGET_CUTOFF,
    availability_mode: str = AvailabilityMode.RECONSTRUCTED.value,
) -> DraftResidualSnapshot:
    estimates = []
    for index, name in enumerate(DRAFT_RESIDUAL_PURE_SCHEMA):
        missing = index == 1
        estimates.append(
            ResidualFeatureEstimate(
                name=name,
                value=None if missing else (index - 4.0) / 10.0,
                support=0 if missing else 12,
                effective_support=0.0 if missing else 8.0,
                coverage=0.0 if missing else 0.8,
                standard_error=None if missing else 0.1,
                missing_reason="insufficient_support" if missing else None,
                evidence_ids=() if missing else (f"e-{index}",),
            )
        )
    return DraftResidualSnapshot(
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        feature_version=DRAFT_RESIDUAL_FEATURE_VERSION,
        feature_schema=DRAFT_RESIDUAL_PURE_SCHEMA,
        feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        team_rating_input_hash=_digest(201),
        authority_fingerprint=_digest(202),
        pure_features=tuple(estimates),
        context_features=(),
        support=12,
        coverage=0.72,
        input_hash=_digest(203),
    )


def _rosh_snapshot(
    *,
    available: bool = True,
    match_id: int = 100,
    prediction_cutoff: datetime = TARGET_CUTOFF,
    availability_mode: str = AvailabilityMode.RECONSTRUCTED.value,
) -> RoshFeatureSnapshot:
    identity = _digest(301) if available else None
    return RoshFeatureSnapshot(
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        status="available" if available else "unavailable",
        missing_reason=None if available else "archive_not_found",
        feature_version=ROSH_FEATURE_VERSION,
        formula_version="formula-v1" if available else None,
        profile_hash=identity,
        result_hash=_digest(302) if available else None,
        run_id=_digest(303) if available else None,
        evidence_hash=_digest(304) if available else None,
        relative_advantage=3.0 if available else None,
        score_20=None,
        score_30=4.0 if available else None,
        score_40=5.0 if available else None,
        score_50=None,
        slope_20_40=None,
        slope_30_50=None,
        curve_min=4.0 if available else None,
        curve_max=5.0 if available else None,
        curve_range=1.0 if available else None,
        direction_flip_count=1 if available else None,
        position_min_support=9 if available else None,
        synergy_min_support=7 if available else None,
        rank_fallback_ratio=0.2 if available else None,
        coverage=0.6 if available else 0.0,
        input_hash=_digest(305 if available else 306),
    )


def _draft_authority(
    *,
    positions_complete: bool = True,
    radiant_hero_ids: tuple[int, ...] = RADIANT_HEROES,
    dire_hero_ids: tuple[int, ...] = DIRE_HEROES,
) -> dict[str, object]:
    def players(hero_ids: tuple[int, ...], *, incomplete: bool) -> list[object]:
        return [
            {
                "player_id": index + 100,
                "hero_id": hero_id,
                "expected_role": {
                    "position": None if incomplete and index == 4 else index + 1,
                },
            }
            for index, hero_id in enumerate(hero_ids)
        ]

    return {
        "draft_authority": {
            "target": {
                "radiant": {
                    "players": players(
                        radiant_hero_ids,
                        incomplete=not positions_complete,
                    )
                },
                "dire": {"players": players(dire_hero_ids, incomplete=False)},
            }
        }
    }


def _standard_rosh_authority(
    *,
    radiant_hero_ids: tuple[int, ...] = RADIANT_HEROES,
    dire_hero_ids: tuple[int, ...] = DIRE_HEROES,
) -> dict[str, object]:
    return {
        "schema": ROSH_AUTHORITY_SCHEMA,
        "target": {
            "radiant_hero_ids": list(radiant_hero_ids),
            "dire_hero_ids": list(dire_hero_ids),
        },
    }


def _snapshot(
    *, outcome: bool = True, rosh_available: bool = True
) -> PrematchFeatureSnapshot:
    return prematch_features._compose_prematch_feature_snapshot(
        _team_run(outcome=outcome),
        _draft_snapshot(),
        _rosh_snapshot(available=rosh_available),
    )


def test_model_kind_schemas_are_fixed_exact_and_deterministic() -> None:
    assert PREMATCH_FEATURE_VERSION == "prematch-features-v1"
    assert prematch_feature_schema("team_only") == ()
    assert prematch_feature_schema("team_plus_draft") == DRAFT_RESIDUAL_MODEL_SCHEMA
    assert prematch_feature_schema("team_plus_rosh") == ROSH_MODEL_SCHEMA
    assert prematch_feature_schema("team_plus_draft_rosh") == (
        DRAFT_RESIDUAL_MODEL_SCHEMA + ROSH_MODEL_SCHEMA
    )
    assert TEAM_PLUS_DRAFT_ROSH_SCHEMA == prematch_feature_schema(
        "team_plus_draft_rosh"
    )
    assert tuple(PREMATCH_FEATURE_SCHEMAS) == (
        "team_only",
        "team_plus_draft",
        "team_plus_rosh",
        "team_plus_draft_rosh",
    )
    assert all(len(value) == 64 for value in PREMATCH_FEATURE_SCHEMA_HASHES.values())
    with pytest.raises(ValueError, match="unsupported"):
        prematch_feature_schema("dynamic")


@pytest.mark.parametrize(
    ("component", "replacement", "message"),
    (
        ("draft", {"match_id": 101}, "match_id"),
        (
            "draft",
            {"prediction_cutoff": TARGET_CUTOFF - timedelta(seconds=1)},
            "cutoff",
        ),
        ("draft", {"availability_mode": AvailabilityMode.PROSPECTIVE.value}, "mode"),
        ("rosh", {"match_id": 101}, "match_id"),
        ("rosh", {"prediction_cutoff": TARGET_CUTOFF - timedelta(seconds=1)}, "cutoff"),
        ("rosh", {"availability_mode": AvailabilityMode.PROSPECTIVE.value}, "mode"),
    ),
)
def test_m2_m3_m4_identity_mismatches_fail_closed(
    component: str,
    replacement: dict[str, object],
    message: str,
) -> None:
    draft = _draft_snapshot()
    rosh = _rosh_snapshot()
    if component == "draft":
        draft = replace(draft, **replacement)
    else:
        rosh = replace(rosh, **replacement)
    with pytest.raises(ValueError, match=message):
        prematch_features._compose_prematch_feature_snapshot(_team_run(), draft, rosh)


def test_public_builder_replays_both_external_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    team_run = _team_run()
    draft = _draft_snapshot()
    rosh = _rosh_snapshot()
    draft_authority = _draft_authority()
    rosh_authority = _standard_rosh_authority()
    history = (team_run,)
    rosh_runs = (object(),)
    links = (object(),)
    calls: dict[str, object] = {}

    def replay_draft(authority, *, target_team_rating, team_rating_history):
        calls["draft"] = (authority, target_team_rating, tuple(team_rating_history))
        return draft

    def replay_rosh(authority, *, runs, artifact_root, match_links):
        calls["rosh"] = (
            authority,
            tuple(runs),
            artifact_root,
            tuple(match_links),
        )
        return rosh

    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        replay_draft,
    )
    monkeypatch.setattr(
        prematch_features,
        "replay_rosh_feature_snapshot",
        replay_rosh,
    )

    snapshot = build_prematch_feature_snapshot(
        draft_authority,
        rosh_authority,
        target_team_rating=team_run,
        team_rating_history=history,
        rosh_runs=rosh_runs,
        artifact_root=tmp_path,
        match_links=links,
    )

    assert snapshot.match_id == team_run.artifact.target.match_id
    assert calls["draft"] == (draft_authority, team_run, history)
    assert calls["rosh"] == (rosh_authority, rosh_runs, tmp_path, links)


def test_m5_compose_with_verified_evidence_cache_matches_strict_path() -> None:
    team_run = _team_run()
    draft = _draft_snapshot()
    rosh = _rosh_snapshot()
    strict = prematch_features._compose_prematch_feature_snapshot(
        team_run,
        draft,
        rosh,
    )
    cache = build_team_rating_residual_evidence_cache((team_run,))
    cached = prematch_features._compose_prematch_feature_snapshot(
        team_run,
        draft,
        rosh,
        team_rating_evidence_cache=cache,
    )

    assert cached == strict
    assert cached.input_hash == strict.input_hash


def test_public_builder_rejects_complete_position_cross_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        lambda *_args, **_kwargs: _draft_snapshot(),
    )
    monkeypatch.setattr(
        prematch_features,
        "replay_rosh_feature_snapshot",
        lambda *_args, **_kwargs: _rosh_snapshot(),
    )

    with pytest.raises(ValueError, match="position hero identities disagree"):
        build_prematch_feature_snapshot(
            _draft_authority(),
            _standard_rosh_authority(
                radiant_hero_ids=(2, 1, 3, 4, 5),
            ),
            target_team_rating=_team_run(),
            team_rating_history=(),
            rosh_runs=(),
            artifact_root=tmp_path,
        )


def test_public_builder_replays_incomplete_positions_as_fixed_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    draft_authority = _draft_authority(positions_complete=False)
    _rosh, rosh_authority = build_unavailable_rosh_feature_snapshot_with_authority(
        match_id=100,
        prediction_cutoff=TARGET_CUTOFF,
        availability_mode=AvailabilityMode.RECONSTRUCTED,
        radiant_hero_ids=reversed(RADIANT_HEROES),
        dire_hero_ids={*DIRE_HEROES},
    )
    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        lambda *_args, **_kwargs: _draft_snapshot(),
    )

    snapshot = build_prematch_feature_snapshot(
        draft_authority,
        rosh_authority,
        target_team_rating=_team_run(),
        team_rating_history=(),
        rosh_runs=(),
        artifact_root=tmp_path,
    )

    assert rosh_authority["schema"] == ROSH_UNAVAILABLE_AUTHORITY_SCHEMA
    assert snapshot.rosh_status == "unavailable"
    assert snapshot.rosh_missing_reason == "expected_positions_incomplete"
    assert snapshot.rosh_coverage == 0.0
    assert dict(snapshot.rosh_features)["relative_advantage"] is None


def test_public_builder_rejects_incomplete_position_cross_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _rosh, other_draft_authority = (
        build_unavailable_rosh_feature_snapshot_with_authority(
            match_id=100,
            prediction_cutoff=TARGET_CUTOFF,
            availability_mode=AvailabilityMode.RECONSTRUCTED,
            radiant_hero_ids=(1, 2, 3, 4, 11),
            dire_hero_ids=DIRE_HEROES,
        )
    )
    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        lambda *_args, **_kwargs: _draft_snapshot(),
    )

    with pytest.raises(ValueError, match="hero sets disagree"):
        build_prematch_feature_snapshot(
            _draft_authority(positions_complete=False),
            other_draft_authority,
            target_team_rating=_team_run(),
            team_rating_history=(),
            rosh_runs=(),
            artifact_root=tmp_path,
        )


def test_public_builder_requires_authority_schema_for_position_completeness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        lambda *_args, **_kwargs: _draft_snapshot(),
    )
    monkeypatch.setattr(
        prematch_features,
        "replay_rosh_feature_snapshot",
        lambda *_args, **_kwargs: _rosh_snapshot(available=False),
    )

    with pytest.raises(ValueError, match="incomplete Draft positions require"):
        build_prematch_feature_snapshot(
            _draft_authority(positions_complete=False),
            _standard_rosh_authority(),
            target_team_rating=_team_run(),
            team_rating_history=(),
            rosh_runs=(),
            artifact_root=tmp_path,
        )

    _snapshot, unavailable_authority = (
        build_unavailable_rosh_feature_snapshot_with_authority(
            match_id=100,
            prediction_cutoff=TARGET_CUTOFF,
            availability_mode=AvailabilityMode.RECONSTRUCTED,
            radiant_hero_ids=RADIANT_HEROES,
            dire_hero_ids=DIRE_HEROES,
        )
    )
    with pytest.raises(ValueError, match="complete Draft positions require"):
        build_prematch_feature_snapshot(
            _draft_authority(),
            unavailable_authority,
            target_team_rating=_team_run(),
            team_rating_history=(),
            rosh_runs=(),
            artifact_root=tmp_path,
        )


def test_public_builder_propagates_replay_tampering_and_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    team_run = _team_run()
    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Draft authority tampered")
        ),
    )
    monkeypatch.setattr(
        prematch_features,
        "replay_rosh_feature_snapshot",
        lambda *_args, **_kwargs: _rosh_snapshot(),
    )
    with pytest.raises(ValueError, match="authority tampered"):
        build_prematch_feature_snapshot(
            {},
            {},
            target_team_rating=team_run,
            team_rating_history=(),
            rosh_runs=(),
            artifact_root=tmp_path,
        )

    monkeypatch.setattr(
        prematch_features,
        "replay_draft_residual_snapshot",
        lambda *_args, **_kwargs: _draft_snapshot(match_id=999),
    )
    with pytest.raises(ValueError, match="match_id"):
        build_prematch_feature_snapshot(
            _draft_authority(),
            _standard_rosh_authority(),
            target_team_rating=team_run,
            team_rating_history=(),
            rosh_runs=(),
            artifact_root=tmp_path,
        )


def test_target_outcome_is_absent_and_cannot_change_snapshot() -> None:
    radiant = _snapshot(outcome=True)
    dire = _snapshot(outcome=False)

    assert radiant == dire
    assert radiant.input_hash == dire.input_hash
    assert "outcome" not in str(radiant.to_payload()).lower()
    assert "radiant_win" not in str(radiant.to_payload()).lower()


def test_unavailable_rosh_stays_missing_and_is_never_a_probability() -> None:
    snapshot = _snapshot(rosh_available=False)
    projected = project_prematch_features(snapshot, "team_plus_rosh")

    assert snapshot.rosh_status == "unavailable"
    assert snapshot.support == 2
    assert snapshot.coverage == pytest.approx((0.72 * 10) / 25)
    assert snapshot.missing_reason == "rosh:archive_not_found"
    assert projected["relative_advantage"] is None
    assert projected["relative_advantage__missing"] == 1.0
    assert projected["score_40"] is None
    assert projected["score_40__missing"] == 1.0
    assert not any("probability" in name for name in projected)
    assert projected["coverage"] == 0.0


def test_snapshot_hash_is_stable_binds_upstream_hashes_and_rejects_tampering() -> None:
    first = _snapshot()
    repeated = _snapshot()

    assert first == repeated
    verify_prematch_feature_snapshot(first)
    assert tuple(project_prematch_features(first, "team_plus_draft_rosh")) == (
        TEAM_PLUS_DRAFT_ROSH_SCHEMA
    )
    revised = prematch_features._compose_prematch_feature_snapshot(
        _team_run(),
        replace(_draft_snapshot(), input_hash=_digest(999)),
        _rosh_snapshot(),
    )
    assert revised.input_hash != first.input_hash
    with pytest.raises(ValueError, match="input hash"):
        replace(first, team_base_logit=first.team_base_logit + 0.1)


def test_snapshot_rejects_noncanonical_schema_hashes_and_missing_flags() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="schema hash"):
        replace(snapshot, rosh_model_schema_hash=_digest(999))

    features = list(snapshot.draft_features)
    missing_index = next(
        index
        for index, (name, _value) in enumerate(features)
        if name.endswith("__missing")
    )
    name, value = features[missing_index]
    features[missing_index] = (name, 1.0 - float(value))
    with pytest.raises(ValueError, match="disagrees"):
        replace(snapshot, draft_features=tuple(features))


def test_snapshot_mirrors_canonical_rosh_availability_projection() -> None:
    unavailable = _snapshot(rosh_available=False)
    unavailable_features = dict(unavailable.rosh_features)
    unavailable_features["relative_advantage"] = 1.5
    unavailable_features["relative_advantage__missing"] = 0.0
    with pytest.raises(ValueError, match="not canonical"):
        replace(
            unavailable,
            rosh_features=tuple(unavailable_features.items()),
            input_hash="",
        )

    available = _snapshot()
    available_features = dict(available.rosh_features)
    available_features["relative_advantage"] = None
    available_features["relative_advantage__missing"] = 1.0
    with pytest.raises(ValueError, match="core signals"):
        replace(
            available,
            rosh_features=tuple(available_features.items()),
            input_hash="",
        )
