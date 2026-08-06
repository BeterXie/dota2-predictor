from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from event_intelligence.prospective_rosh_candidate import (
    candidate_probability,
    load_frozen_prospective_rosh_candidate,
)
from event_intelligence.prospective_rosh_shadow import (
    SettledShadowRow,
    TeamRatingAuthority,
    archive_exact_artifacts,
    build_prospective_rosh_evidence,
    build_shadow_evaluation,
    build_shadow_prediction,
    build_shadow_settlement,
    replay_archived_pure_rosh,
)
from event_intelligence.raw_archive import canonical_json_bytes
from live_betting.rosh_parity import ExactByteArtifactStore
from prematch.stratz_rosh import build_rosh_query_requests


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "stratz-rosh.json"


def _digest(value: int) -> str:
    return f"{value:064x}"


def _artifact_bundles(tmp_path: Path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    store = ExactByteArtifactStore(tmp_path / "artifacts")
    hero_ids = [*fixture["radiant_heroes"], *fixture["dire_heroes"]]
    query_requests = build_rosh_query_requests(
        hero_ids,
        int(datetime(2026, 8, 6, 14, 50, tzinfo=UTC).timestamp()),
    )
    requests = archive_exact_artifacts(
        store,
        {
            operation: canonical_json_bytes(payload)
            for operation, payload in query_requests.items()
        },
    )
    responses = archive_exact_artifacts(
        store,
        {
            operation: canonical_json_bytes(payload)
            for operation, payload in fixture["responses"].items()
        },
    )
    return fixture, store, requests, responses


def _team(cutoff: datetime) -> TeamRatingAuthority:
    return TeamRatingAuthority(
        prediction_id=7,
        run_id=_digest(1),
        prediction_cutoff=cutoff,
        probability=0.55,
        rating_version="team-rating-elo-v1",
        artifact_version="team-rating-artifact-v1",
        artifact_hash=_digest(2),
        input_hash=_digest(3),
        training_input_hash=_digest(4),
    )


def test_exact_archived_replay_builds_player_free_paired_shadow(tmp_path: Path) -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    fixture, store, requests, responses = _artifact_bundles(tmp_path)
    cutoff = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    evidence = build_prospective_rosh_evidence(
        candidate,
        artifact_root=store.root,
        radiant_heroes=fixture["radiant_heroes"],
        dire_heroes=fixture["dire_heroes"],
        request_artifacts=requests,
        response_artifacts=responses,
        statistics_cutoff=cutoff - timedelta(minutes=10),
        available_at=cutoff - timedelta(minutes=5),
    )

    replayed, normalized_hash = replay_archived_pure_rosh(
        store.root,
        responses,
        radiant_heroes=fixture["radiant_heroes"],
        dire_heroes=fixture["dire_heroes"],
    )
    prediction = build_shadow_prediction(
        candidate,
        match_id=9000000001,
        series_id=1200001,
        team_rating=_team(cutoff),
        rosh_evidence=evidence,
        created_at=cutoff - timedelta(minutes=1),
    )

    assert replayed == pytest.approx(8.7)
    assert evidence.pure_rosh_score == replayed
    assert evidence.normalized_statistics_hash == normalized_hash
    assert prediction.record_status == "paired"
    assert prediction.p1_probability is not None
    assert prediction.p1_probability > prediction.p0_probability
    assert "player" not in evidence.to_payload()


def test_late_or_drifted_rosh_evidence_fails_closed_to_p0_only(
    tmp_path: Path,
) -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    fixture, store, requests, responses = _artifact_bundles(tmp_path)
    cutoff = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    evidence = build_prospective_rosh_evidence(
        candidate,
        artifact_root=store.root,
        radiant_heroes=fixture["radiant_heroes"],
        dire_heroes=fixture["dire_heroes"],
        request_artifacts=requests,
        response_artifacts=responses,
        statistics_cutoff=cutoff - timedelta(minutes=1),
        available_at=cutoff + timedelta(seconds=1),
    )
    late = build_shadow_prediction(
        candidate,
        match_id=9000000002,
        series_id=1200002,
        team_rating=_team(cutoff),
        rosh_evidence=evidence,
        created_at=cutoff - timedelta(seconds=1),
    )
    drifted = build_shadow_prediction(
        candidate,
        match_id=9000000003,
        series_id=1200003,
        team_rating=_team(cutoff),
        rosh_evidence=replace(evidence, profile_hash="f" * 64),
        created_at=cutoff - timedelta(seconds=1),
    )

    for prediction in (late, drifted):
        assert prediction.record_status == "p0_only"
        assert prediction.p1_probability is None
        assert prediction.rosh_evidence is None
        assert prediction.missing_reason == "rosh_evidence_invalid"


def test_prediction_and_settlement_hashes_are_deterministic_and_separate() -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    cutoff = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    arguments = dict(
        candidate=candidate,
        match_id=9000000004,
        series_id=1200004,
        team_rating=_team(cutoff),
        rosh_evidence=None,
        missing_reason="rosh_not_available_before_cutoff",
        created_at=cutoff - timedelta(seconds=1),
    )
    first = build_shadow_prediction(**arguments)
    second = build_shadow_prediction(**arguments)
    assert first == second
    settlement = build_shadow_settlement(
        first,
        eventual_radiant_win=1,
        result_artifact_hash=_digest(9),
        result_usable_at=cutoff + timedelta(hours=1),
        settled_at=cutoff + timedelta(hours=1, minutes=1),
        created_at=cutoff + timedelta(hours=1, minutes=2),
    )
    assert settlement.prediction_hash == first.prediction_hash
    assert settlement.settlement_hash != first.prediction_hash
    assert first.record_status == "p0_only"


def _settled_rows(count: int) -> tuple[SettledShadowRow, ...]:
    candidate = load_frozen_prospective_rosh_candidate()
    start = datetime(2026, 8, 7, 0, 0, tzinfo=UTC)
    rows = []
    for index in range(count):
        outcome = index % 2
        score = 10.0 if outcome else -10.0
        p1, standardized, contribution = candidate_probability(
            candidate,
            team_probability=0.5,
            pure_rosh_score=score,
        )
        rows.append(
            SettledShadowRow(
                prediction_hash=_digest(1000 + index),
                candidate_hash=candidate.artifact_hash,
                match_id=9100000000 + index,
                series_id=1300000 + index // 2,
                prediction_cutoff=start + timedelta(minutes=index),
                record_status="paired",
                p0_probability=0.5,
                p1_probability=p1,
                pure_rosh_score=score,
                standardized_rosh_score=standardized,
                rosh_logit_contribution=contribution,
                missing_reason=None,
                rosh_profile_hash=candidate.prospective_profile_hash,
                rosh_formula_version=candidate.retrospective_formula_version,
                rosh_scorer_source_hash=candidate.scorer_source_hash,
                outcome=outcome,
                event_id=f"event-{index % 2}",
                patch=60,
            )
        )
    return tuple(rows)


def test_preregistered_20_100_200_stages_freeze_first_paired_window() -> None:
    candidate = load_frozen_prospective_rosh_candidate()
    rows = _settled_rows(205)
    created = datetime(2026, 8, 8, tzinfo=UTC)
    stage20 = build_shadow_evaluation(
        candidate,
        rows,
        stage=20,
        created_at=created,
    )
    stage100 = build_shadow_evaluation(
        candidate,
        rows,
        stage=100,
        created_at=created,
    )
    stage200 = build_shadow_evaluation(
        candidate,
        rows,
        stage=200,
        created_at=created,
        bootstrap_samples=50,
    )
    repeated = build_shadow_evaluation(
        candidate,
        rows[:200],
        stage=200,
        created_at=created,
        bootstrap_samples=50,
    )

    assert stage20.report["effectiveness_conclusion_allowed"] is False
    assert stage100.report["effectiveness_conclusion_allowed"] is False
    assert stage100.report["profile_drift"]["detected"] is False
    assert stage200.window_manifest_hash == repeated.window_manifest_hash
    assert stage200.report_hash == repeated.report_hash
    assert stage200.report["comparison"]["delta_m1_minus_m0"][
        "brier_score"
    ] < 0
    assert stage200.report["comparison"]["delta_m1_minus_m0"][
        "log_loss"
    ] < 0
    assert stage200.report["deployment_eligible"] is False
