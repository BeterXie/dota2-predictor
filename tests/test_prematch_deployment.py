from __future__ import annotations

import copy
import hashlib
import math
from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from event_intelligence.cluster_artifacts import build_cluster_feature_artifact
from event_intelligence.cluster_features import (
    ClusterFeatureTarget,
    ClusterPlayer,
)
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
)
from event_intelligence.prematch_artifacts import canonical_json_bytes
from event_intelligence.prematch_calibration import (
    PrematchCalibrationSample,
    build_prematch_calibration_artifact,
)
from event_intelligence.prematch_deployment import (
    PREMATCH_DEPLOYMENT_PROOF_SCHEMA,
    PREMATCH_DEPLOYMENT_VERSION,
    FrozenPrematchDeployment,
    assert_frozen_prematch_deployment_deployable,
    build_frozen_prematch_deployment,
    frozen_prematch_deployment_from_payload,
    load_frozen_prematch_deployment_json,
    replay_frozen_prematch_deployment,
    verify_frozen_prematch_deployment,
)
from event_intelligence.prematch_features import (
    PREMATCH_FEATURE_VERSION,
    PrematchFeatureSnapshot,
)
from event_intelligence.prematch_model import (
    PrematchTrainingRow,
    fit_prematch_model,
)
from event_intelligence.hero_clusters import (
    ClusterEvidenceMode,
    load_cluster_resource,
)
from event_intelligence.prematch_storage import prematch_dependency_fingerprint
from event_intelligence.rosh_features import (
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
)
from event_intelligence.team_rating import RatingMapInput, TeamRatingConfig
from event_intelligence.team_rating_artifacts import build_team_rating_artifact
from prematch.stratz_official_profile import get_profile


UTC = timezone.utc
MODEL_CUTOFF = datetime(2026, 6, 1, tzinfo=UTC)
TARGET_CUTOFF = MODEL_CUTOFF + timedelta(days=2)
RADIANT_ROSTER = (1, 2, 3, 4, 5)
DIRE_ROSTER = (6, 7, 8, 9, 10)
MODE = AvailabilityMode.PROSPECTIVE.value
MODEL_KIND = "team_plus_draft"


def _digest(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _rating_artifact():
    rows = tuple(
        RatingMapInput(
            match_id=index + 1,
            series_id=100 + index,
            event_id="event-a",
            started_at=MODEL_CUTOFF - timedelta(days=10 - index),
            completed_at=MODEL_CUTOFF
            - timedelta(days=10 - index)
            + timedelta(minutes=40),
            result_usable_at=MODEL_CUTOFF
            - timedelta(days=10 - index)
            + timedelta(minutes=41),
            radiant_team_id=10,
            dire_team_id=20,
            radiant_roster=RADIANT_ROSTER,
            dire_roster=DIRE_ROSTER,
            radiant_win=index % 2 == 0,
        )
        for index in (1, 2, 3)
    )
    target = RatingMapInput(
        match_id=999,
        series_id=199,
        event_id="event-a",
        started_at=TARGET_CUTOFF,
        completed_at=TARGET_CUTOFF + timedelta(minutes=40),
        result_usable_at=None,
        radiant_team_id=10,
        dire_team_id=20,
        radiant_roster=RADIANT_ROSTER,
        dire_roster=DIRE_ROSTER,
        radiant_win=True,
    )
    config = TeamRatingConfig(
        initial_rating=1500.0,
        scale=400.0,
        k_factor=16.0,
        inactivity_half_life_days=180.0,
        roster_carry_power=1.0,
        radiant_side_logit=0.0,
        config_version="team-rating-elo-v1",
    )
    return build_team_rating_artifact(
        rows,
        target=target,
        prediction_cutoff=TARGET_CUTOFF,
        training_cutoff=TARGET_CUTOFF,
        config=config,
    )


def _feature_snapshot(artifact) -> PrematchFeatureSnapshot:
    draft = {}
    for index, name in enumerate(DRAFT_RESIDUAL_MODEL_SCHEMA):
        draft[name] = None if name == "role_residual_diff" else float(index) / 10.0
        if name.endswith("__missing"):
            base = name.removesuffix("__missing")
            draft[name] = 1.0 if draft.get(base) is None else 0.0
    rosh = {}
    for index, name in enumerate(ROSH_MODEL_SCHEMA):
        if name.endswith("__missing"):
            rosh[name] = 0.0
        elif name == "coverage":
            rosh[name] = 1.0
        else:
            rosh[name] = 0.4 + index / 100.0
    profile = get_profile()
    return PrematchFeatureSnapshot(
        match_id=artifact.target.match_id,
        prediction_cutoff=TARGET_CUTOFF,
        availability_mode=MODE,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=math.log(artifact.prediction.raw_probability)
        - math.log1p(-artifact.prediction.raw_probability),
        team_rating_run_id=_digest("team-run"),
        team_rating_artifact_hash=artifact.artifact_hash,
        team_rating_prediction_input_hash=artifact.prediction.input_hash,
        team_rating_combined_training_input_hash=_digest("combined"),
        team_rating_support=artifact.prediction.support,
        draft_residual_input_hash=_digest("draft-input"),
        draft_residual_authority_fingerprint=_digest("draft-authority"),
        draft_residual_team_rating_input_hash=_digest("draft-team-input"),
        draft_residual_feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=10,
        draft_coverage=1.0,
        draft_features=tuple(draft.items()),
        rosh_status="available",
        rosh_missing_reason=None,
        rosh_input_hash=_digest("rosh-input"),
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=_digest("rosh-run"),
        rosh_evidence_hash=_digest("rosh-evidence"),
        rosh_formula_version=profile.formula_version,
        rosh_profile_hash=profile.canonical_profile_hash,
        rosh_result_hash=_digest("rosh-result"),
        rosh_coverage=1.0,
        rosh_features=tuple(rosh.items()),
    )


def _model_and_calibration(snapshot: PrematchFeatureSnapshot):
    draft_features = dict(snapshot.draft_features)
    rows = tuple(
        PrematchTrainingRow(
            match_id=index + 1,
            input_snapshot_hash=_digest(f"input-{index}"),
            prediction_cutoff=MODEL_CUTOFF - timedelta(days=100 - index),
            completed_at=MODEL_CUTOFF
            - timedelta(days=100 - index)
            + timedelta(hours=1),
            result_usable_at=MODEL_CUTOFF
            - timedelta(days=100 - index)
            + timedelta(hours=2),
            availability_mode=MODE,
            outcome=index % 2,
            series_id=f"series-{index}",
            event_id="event-a",
            patch_id="7.41",
            team_base_logit=(index % 5 - 2) / 3.0,
            features=draft_features,
        )
        for index in range(30)
    )
    model = fit_prematch_model(
        rows,
        MODEL_CUTOFF,
        model_kind=MODEL_KIND,
        availability_mode=MODE,
        min_samples=10,
    )
    samples = tuple(
        PrematchCalibrationSample(
            match_id=10_000 + index,
            series_id=f"cal-series-{index // 2}",
            event_id="event-a",
            patch_id="7.41",
            model_kind=MODEL_KIND,
            availability_mode=MODE,
            prediction_cutoff=MODEL_CUTOFF - timedelta(days=200 - index),
            result_usable_at=MODEL_CUTOFF
            - timedelta(days=200 - index)
            + timedelta(hours=1),
            raw_probability=0.15 if index % 2 == 0 else 0.85,
            outcome=index % 2,
            model_hash=_digest(f"oos-model-{index // 10}"),
            input_snapshot_hash=_digest(f"oos-input-{index}"),
        )
        for index in range(200)
    )
    calibration = build_prematch_calibration_artifact(
        samples,
        MODEL_CUTOFF,
        model_kind=MODEL_KIND,
        availability_mode=MODE,
    )
    return model, calibration


def _proof(model_hash: str, calibration_hash: str, *, status: str = "passed"):
    return {
        "schema": PREMATCH_DEPLOYMENT_PROOF_SCHEMA,
        "backtest_version": "prematch-walk-forward-v1",
        "availability_mode": MODE,
        "default_decision": {
            "model_kind": MODEL_KIND,
            "status": status,
            "reasons": [],
        },
        "calibration": [
            {
                "model_kind": MODEL_KIND,
                "status": "provisional" if status != "passed" else "passed",
                "gate_passed": True,
                "gate_reasons": [],
                "calibration_hash": calibration_hash,
            }
        ],
        "incremental_comparisons": [
            {
                "comparison": "M3-M2",
                "added_component": "draft",
                "available_support": 20,
                "status": "incremental_value",
                "reasons": [],
                "metrics": [
                    {
                        "metric": "brier_score",
                        "delta": -0.01,
                        "ci_90": {"lower": -0.02, "upper": -0.001},
                        "ci_95": {"lower": -0.03, "upper": 0.001},
                        "probability_of_improvement": 0.91,
                    },
                    {
                        "metric": "log_loss",
                        "delta": 0.01,
                        "ci_90": {"lower": -0.01, "upper": 0.02},
                        "ci_95": {"lower": -0.02, "upper": 0.03},
                        "probability_of_improvement": 0.49,
                    },
                ],
            }
        ],
        "model_hash": model_hash,
    }


@pytest.fixture(scope="module")
def bundle_parts():
    team = _rating_artifact()
    snapshot = _feature_snapshot(team)
    model, calibration = _model_and_calibration(snapshot)
    return team, snapshot, model, calibration


def _deployment(bundle_parts, **kwargs) -> FrozenPrematchDeployment:
    team, snapshot, model, calibration = bundle_parts
    return build_frozen_prematch_deployment(
        training_cutoff=MODEL_CUTOFF,
        availability_mode=MODE,
        dependency_fingerprint=prematch_dependency_fingerprint(snapshot),
        dependency_revision=3,
        team_rating_artifact=team,
        feature_snapshot=snapshot,
        prematch_model_artifact=model,
        calibration_artifact=calibration,
        report=_proof(model.model_hash, calibration.calibration_hash),
        **kwargs,
    )


def _cluster_artifact(snapshot: PrematchFeatureSnapshot, resource):
    def player(hero_id: int, role: str, lane: str) -> ClusterPlayer:
        return ClusterPlayer(hero_id, role, lane, 1.0, 1.0)

    return build_cluster_feature_artifact(
        ClusterFeatureTarget(
            match_id=snapshot.match_id,
            prediction_cutoff=snapshot.prediction_cutoff,
            patch="7.41",
            evidence_mode=ClusterEvidenceMode.PUBLISHED_STATIC,
            radiant=(
                player(70, "core", "safe"),
                player(106, "core", "mid"),
                player(96, "core", "off"),
                player(50, "support", "safe"),
                player(100, "support", "off"),
            ),
            dire=(
                player(73, "core", "safe"),
                player(39, "core", "mid"),
                player(78, "core", "off"),
                player(87, "support", "safe"),
                player(123, "support", "off"),
            ),
        ),
        resource,
    )


def test_build_replay_and_canonical_json_round_trip(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    assert PREMATCH_DEPLOYMENT_VERSION == "prematch-frozen-deployment-v1"
    assert (
        deployment.deployment_key
        == hashlib.sha256(
            canonical_json_bytes(deployment.to_payload(include_deployment_key=False))
        ).hexdigest()
    )
    raw = deployment.canonical_bytes().decode("utf-8")
    loaded = load_frozen_prematch_deployment_json(raw)
    assert loaded == deployment
    assert replay_frozen_prematch_deployment(deployment) == deployment
    assert (
        frozen_prematch_deployment_from_payload(deployment.to_payload()) == deployment
    )
    assert deployment.model_kind == MODEL_KIND
    assert deployment.static_gate_ready is True


def test_bundle_binds_optional_cluster_shadow_candidate(
    bundle_parts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team, base_snapshot, model, calibration = bundle_parts
    resource = replace(
        load_cluster_resource(),
        published_at=MODEL_CUTOFF - timedelta(days=1),
    )
    monkeypatch.setattr(
        "event_intelligence.prematch_deployment.load_cluster_resource",
        lambda: resource,
    )
    cluster_artifact = _cluster_artifact(base_snapshot, resource)
    snapshot = replace(
        base_snapshot,
        input_hash="",
        cluster_artifact=cluster_artifact,
    )
    candidate = fit_prematch_model(
        (),
        MODEL_CUTOFF,
        model_kind="team_plus_draft_rosh_clusters",
        availability_mode=MODE,
        min_samples=10,
    )
    deployment = build_frozen_prematch_deployment(
        training_cutoff=MODEL_CUTOFF,
        availability_mode=MODE,
        dependency_revision=3,
        team_rating_artifact=team,
        feature_snapshot=snapshot,
        prematch_model_artifact=model,
        calibration_artifact=calibration,
        cluster_candidate_model_artifact=candidate,
        report=_proof(model.model_hash, calibration.calibration_hash),
    )

    loaded = load_frozen_prematch_deployment_json(
        deployment.canonical_bytes().decode("utf-8")
    )

    assert loaded.cluster_candidate_model_artifact == candidate
    assert loaded.feature_snapshot.cluster_artifact == cluster_artifact


def test_exact_duplicate_is_idempotent_and_key_binds_all_inputs(bundle_parts) -> None:
    first = _deployment(bundle_parts)
    second = _deployment(bundle_parts)
    assert first == second
    forged_payload = copy.deepcopy(first.to_payload())
    forged_payload["dependency_revision"] = 4
    with pytest.raises(ValueError, match="deployment key|stale"):
        frozen_prematch_deployment_from_payload(forged_payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("deployment_version", "prematch-frozen-deployment-v999", "version"),
        ("availability_mode", "reconstructed_walk_forward", "modes"),
        ("training_cutoff", "2026-06-03T00:00:00+00:00", "cutoff"),
    ),
)
def test_mode_cutoff_and_unknown_version_fail_closed(
    bundle_parts,
    field: str,
    value: object,
    message: str,
) -> None:
    payload = copy.deepcopy(_deployment(bundle_parts).to_payload())
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        frozen_prematch_deployment_from_payload(payload)


def test_profile_hash_and_component_gate_fail_closed(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    tampered_snapshot = replace(
        deployment.feature_snapshot,
        rosh_profile_hash=_digest("wrong-profile"),
        input_hash="",
    )
    with pytest.raises(ValueError, match="profile hash"):
        build_frozen_prematch_deployment(
            training_cutoff=deployment.training_cutoff,
            availability_mode=deployment.availability_mode,
            dependency_fingerprint=prematch_dependency_fingerprint(tampered_snapshot),
            dependency_revision=deployment.dependency_revision,
            team_rating_artifact=deployment.team_rating_artifact,
            feature_snapshot=tampered_snapshot,
            prematch_model_artifact=deployment.prematch_model_artifact,
            calibration_artifact=deployment.calibration_artifact,
            report=deployment.m6_report_payload,
        )
    failed_proof = _proof(
        deployment.prematch_model_artifact.model_hash,
        deployment.calibration_artifact.calibration_hash,
    )
    failed_proof["calibration"][0]["gate_passed"] = False
    with pytest.raises(ValueError, match="calibration gate"):
        build_frozen_prematch_deployment(
            training_cutoff=deployment.training_cutoff,
            availability_mode=deployment.availability_mode,
            dependency_fingerprint=deployment.dependency_fingerprint,
            dependency_revision=deployment.dependency_revision,
            team_rating_artifact=deployment.team_rating_artifact,
            feature_snapshot=deployment.feature_snapshot,
            prematch_model_artifact=deployment.prematch_model_artifact,
            calibration_artifact=deployment.calibration_artifact,
            report=failed_proof,
        )


def test_dependency_revision_and_stale_callback_fail_closed(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    with pytest.raises(RuntimeError, match="revision"):
        verify_frozen_prematch_deployment(
            deployment,
            current_dependency_revision=4,
        )
    verify_frozen_prematch_deployment(
        deployment,
        current_dependency_revision=3,
        stale_callback=lambda _deployment: None,
    )
    with pytest.raises(RuntimeError, match="stale"):
        verify_frozen_prematch_deployment(
            deployment,
            stale_callback=lambda _deployment: "lineage changed",
        )


def test_default_team_only_ablation_cannot_be_deployed(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    proof = copy.deepcopy(deployment.to_payload()["m6_report"])
    proof["default_decision"]["model_kind"] = "team_only"
    with pytest.raises(ValueError, match="model kind|team_only"):
        build_frozen_prematch_deployment(
            training_cutoff=deployment.training_cutoff,
            availability_mode=deployment.availability_mode,
            dependency_fingerprint=deployment.dependency_fingerprint,
            dependency_revision=deployment.dependency_revision,
            team_rating_artifact=deployment.team_rating_artifact,
            feature_snapshot=deployment.feature_snapshot,
            prematch_model_artifact=deployment.prematch_model_artifact,
            calibration_artifact=deployment.calibration_artifact,
            report=proof,
        )


def test_ablation_status_cannot_replace_paired_metric_gate(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    proof = copy.deepcopy(deployment.to_payload()["m6_report"])
    proof["incremental_comparisons"][0]["metrics"][0]["ci_90"]["upper"] = 0.01
    with pytest.raises(ValueError, match="significant paired improvement"):
        build_frozen_prematch_deployment(
            training_cutoff=deployment.training_cutoff,
            availability_mode=deployment.availability_mode,
            dependency_fingerprint=deployment.dependency_fingerprint,
            dependency_revision=deployment.dependency_revision,
            team_rating_artifact=deployment.team_rating_artifact,
            feature_snapshot=deployment.feature_snapshot,
            prematch_model_artifact=deployment.prematch_model_artifact,
            calibration_artifact=deployment.calibration_artifact,
            report=proof,
        )


def test_report_payload_rejects_non_string_mapping_keys(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    proof = copy.deepcopy(deployment.to_payload()["m6_report"])
    proof["unexpected"] = {1: "would be coerced"}
    with pytest.raises(ValueError, match="object keys must be strings"):
        build_frozen_prematch_deployment(
            training_cutoff=deployment.training_cutoff,
            availability_mode=deployment.availability_mode,
            dependency_fingerprint=deployment.dependency_fingerprint,
            dependency_revision=deployment.dependency_revision,
            team_rating_artifact=deployment.team_rating_artifact,
            feature_snapshot=deployment.feature_snapshot,
            prematch_model_artifact=deployment.prematch_model_artifact,
            calibration_artifact=deployment.calibration_artifact,
            report=proof,
        )


def test_dependency_fingerprint_is_recomputed_from_feature_snapshot(
    bundle_parts,
) -> None:
    deployment = _deployment(bundle_parts)
    assert deployment.dependency_fingerprint == prematch_dependency_fingerprint(
        deployment.feature_snapshot
    )
    payload = copy.deepcopy(deployment.to_payload())
    payload["dependency_fingerprint"] = _digest("tampered-dependency")
    with pytest.raises(ValueError, match="dependency fingerprint"):
        frozen_prematch_deployment_from_payload(payload)


def test_reconstructed_bundle_is_never_runtime_ready(bundle_parts) -> None:
    deployment = _deployment(bundle_parts)
    forged = replace(
        deployment,
        availability_mode=AvailabilityMode.RECONSTRUCTED.value,
        deployment_key="",
    )
    with pytest.raises(ValueError, match="modes|reconstructed"):
        assert_frozen_prematch_deployment_deployable(
            forged,
            current_dependency_revision=3,
        )


def test_noncanonical_and_duplicate_json_fail_closed(bundle_parts) -> None:
    raw = _deployment(bundle_parts).canonical_bytes().decode("utf-8")
    with pytest.raises(ValueError, match="canonical"):
        load_frozen_prematch_deployment_json(" " + raw)
    duplicate = raw.replace(
        '"deployment_version":"prematch-frozen-deployment-v1"',
        '"deployment_version":"prematch-frozen-deployment-v1",'
        '"deployment_version":"prematch-frozen-deployment-v1"',
        1,
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_frozen_prematch_deployment_json(duplicate)
