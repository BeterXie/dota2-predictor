from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import event_intelligence.prematch_model as prematch_model
from event_intelligence.cluster_artifacts import build_cluster_feature_artifact
from event_intelligence.cluster_features import ClusterFeatureTarget, ClusterPlayer
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
    DRAFT_RESIDUAL_PURE_SCHEMA,
)
from event_intelligence.prematch_features import (
    PREMATCH_CLUSTER_MODEL_SCHEMA,
    PREMATCH_FEATURE_VERSION,
    PrematchFeatureSnapshot,
    project_prematch_features,
)
from event_intelligence.hero_clusters import (
    ClusterEvidenceMode,
    load_cluster_resource,
)
from event_intelligence.prematch_model import (
    ModelStatus,
    PREMATCH_MAX_ABS_STANDARDIZED_VALUE,
    PREMATCH_MIN_FEATURE_NONMISSING_SUPPORT,
    PREMATCH_MIN_STANDARDIZATION_SCALE,
    PredictionStatus,
    PrematchTrainingRow,
    fit_prematch_model,
    offset_logistic_objective_and_gradient,
    predict_prematch,
)
from event_intelligence.rosh_features import (
    ROSH_FEATURE_SCHEMA,
    ROSH_MODEL_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
)


UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
TRAINING_CUTOFF = START + timedelta(days=100)
MODE = AvailabilityMode.RECONSTRUCTED.value


def _digest(number: int) -> str:
    return f"{number:064x}"


def _snapshot(
    match_id: int,
    prediction_cutoff: datetime,
    *,
    team_base_logit: float,
    draft_signal: float,
    rosh_signal: float,
    missing_rosh_score_20: bool = False,
    availability_mode: str = MODE,
    cluster_signal: float | None = None,
) -> PrematchFeatureSnapshot:
    draft: dict[str, float | None] = {}
    for index, name in enumerate(DRAFT_RESIDUAL_PURE_SCHEMA):
        value = draft_signal if index == 0 else float(index) / 10.0
        draft[name] = value
        draft[f"{name}__log1p_support"] = math.log1p(20.0 + index)
        draft[f"{name}__coverage"] = 0.8
        draft[f"{name}__missing"] = 0.0
    rosh: dict[str, float | None] = {}
    for index, name in enumerate(ROSH_FEATURE_SCHEMA):
        missing = name == "score_20" and missing_rosh_score_20
        if missing:
            value = None
        elif name == "coverage":
            value = 0.9
        elif index == 0:
            value = rosh_signal
        else:
            value = float(index)
        rosh[name] = value
        rosh[f"{name}__missing"] = 1.0 if missing else 0.0
    return PrematchFeatureSnapshot(
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        availability_mode=availability_mode,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=team_base_logit,
        team_rating_run_id=_digest(1_000 + match_id),
        team_rating_artifact_hash=_digest(2_000 + match_id),
        team_rating_prediction_input_hash=_digest(3_000 + match_id),
        team_rating_combined_training_input_hash=_digest(4_000 + match_id),
        team_rating_support=30,
        draft_residual_input_hash=_digest(5_000 + match_id),
        draft_residual_authority_fingerprint=_digest(6_000 + match_id),
        draft_residual_team_rating_input_hash=_digest(7_000 + match_id),
        draft_residual_feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=20,
        draft_coverage=0.8,
        draft_features=tuple(draft.items()),
        rosh_status="available",
        rosh_missing_reason=None,
        rosh_input_hash=_digest(8_000 + match_id),
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=_digest(9_000 + match_id),
        rosh_evidence_hash=_digest(10_000 + match_id),
        rosh_formula_version="formula-v1",
        rosh_profile_hash=_digest(11_000 + match_id),
        rosh_result_hash=_digest(12_000 + match_id),
        rosh_coverage=0.9,
        rosh_features=tuple(rosh.items()),
        cluster_artifact=(
            None
            if cluster_signal is None
            else _cluster_artifact(
                match_id,
                prediction_cutoff,
                reverse=cluster_signal < 0.0,
            )
        ),
    )


def _cluster_artifact(
    match_id: int,
    prediction_cutoff: datetime,
    *,
    reverse: bool,
):
    resource = replace(
        load_cluster_resource(),
        published_at=None,
        evidence_mode=ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD,
        training_cutoff=START - timedelta(days=1),
    )

    def player(hero_id: int, role: str, lane: str) -> ClusterPlayer:
        return ClusterPlayer(hero_id, role, lane, 1.0, 1.0)

    radiant = (
        player(70, "core", "safe"),
        player(106, "core", "mid"),
        player(96, "core", "off"),
        player(50, "support", "safe"),
        player(100, "support", "off"),
    )
    dire = (
        player(73, "core", "safe"),
        player(39, "core", "mid"),
        player(78, "core", "off"),
        player(87, "support", "safe"),
        player(123, "support", "off"),
    )
    target = ClusterFeatureTarget(
        match_id=match_id,
        prediction_cutoff=prediction_cutoff,
        patch="7.41",
        evidence_mode=ClusterEvidenceMode.RECONSTRUCTED_WALK_FORWARD,
        radiant=dire if reverse else radiant,
        dire=radiant if reverse else dire,
    )
    return build_cluster_feature_artifact(target, resource)


def _rows(
    model_kind: str,
    count: int = 40,
    *,
    outcome_override: int | None = None,
    with_cluster: bool = False,
) -> tuple[PrematchTrainingRow, ...]:
    rows = []
    for index in range(count):
        prediction_cutoff = START + timedelta(days=index)
        team_logit = (index % 5 - 2) * 0.15
        draft_signal = float(index % 7 - 3)
        rosh_signal = float((index * 3) % 9 - 4)
        outcome = int(team_logit + 0.55 * draft_signal + 0.25 * rosh_signal > 0)
        if outcome_override is not None:
            outcome = outcome_override
        snapshot = _snapshot(
            index + 1,
            prediction_cutoff,
            team_base_logit=team_logit,
            draft_signal=draft_signal,
            rosh_signal=rosh_signal,
            missing_rosh_score_20=index % 5 == 0,
            cluster_signal=(
                None if not with_cluster else 1.0 if index % 2 == 0 else -1.0
            ),
        )
        rows.append(
            PrematchTrainingRow.from_snapshot(
                snapshot,
                model_kind=model_kind,
                completed_at=prediction_cutoff + timedelta(hours=1),
                result_usable_at=prediction_cutoff + timedelta(hours=2),
                outcome=outcome,
                series_id=f"series-{index // 2}",
                event_id=f"event-{index % 3}",
                patch_id=f"7.{40 + index // 20}",
            )
        )
    return tuple(rows)


def test_analytic_gradient_matches_finite_difference_and_excludes_offset() -> None:
    matrix = np.asarray(((1.0, -0.5), (0.2, 0.8), (-0.7, 1.1)))
    offsets = np.asarray((0.4, -0.3, 0.1))
    outcomes = np.asarray((1.0, 0.0, 1.0))
    parameters = np.asarray((0.2, -0.4, 0.7))
    regularization = 1.3
    loss, gradient = offset_logistic_objective_and_gradient(
        parameters,
        matrix,
        offsets,
        outcomes,
        regularization,
    )
    epsilon = 1e-6
    numerical = []
    for index in range(len(parameters)):
        step = np.zeros_like(parameters)
        step[index] = epsilon
        high, _ = offset_logistic_objective_and_gradient(
            parameters + step,
            matrix,
            offsets,
            outcomes,
            regularization,
        )
        low, _ = offset_logistic_objective_and_gradient(
            parameters - step,
            matrix,
            offsets,
            outcomes,
            regularization,
        )
        numerical.append((high - low) / (2.0 * epsilon))

    assert math.isfinite(loss)
    assert np.allclose(gradient, numerical, rtol=0.0, atol=1e-6)
    changed_offsets_loss, _ = offset_logistic_objective_and_gradient(
        parameters,
        matrix,
        offsets + 1.0,
        outcomes,
        regularization,
    )
    assert changed_offsets_loss != loss


def test_future_and_result_unavailable_rows_are_filtered_before_features() -> None:
    baseline_rows = _rows("team_only", 20)
    baseline = fit_prematch_model(
        baseline_rows,
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )
    future = replace(
        baseline_rows[0],
        match_id=90_001,
        input_snapshot_hash=_digest(90_001),
        prediction_cutoff=TRAINING_CUTOFF + timedelta(seconds=1),
        completed_at=TRAINING_CUTOFF + timedelta(hours=1),
        result_usable_at=TRAINING_CUTOFF + timedelta(hours=2),
        team_base_logit=float("inf"),
        features={"unknown_future": float("inf")},
    )
    unavailable = replace(
        baseline_rows[1],
        match_id=90_002,
        input_snapshot_hash=_digest(90_002),
        result_usable_at=None,
        team_base_logit=float("inf"),
        features={"unknown_unavailable": float("inf")},
    )
    late = replace(
        baseline_rows[2],
        match_id=90_003,
        input_snapshot_hash=_digest(90_003),
        completed_at=TRAINING_CUTOFF - timedelta(hours=1),
        result_usable_at=TRAINING_CUTOFF + timedelta(seconds=1),
        team_base_logit=float("inf"),
        features={"unknown_late": float("inf")},
    )
    changed = fit_prematch_model(
        (*baseline_rows, future, unavailable, late),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )

    assert changed == baseline


def test_training_only_imputation_and_binary_missing_flags_are_unstandardized() -> None:
    rows = _rows("team_plus_rosh", 30)
    model = fit_prematch_model(
        rows,
        TRAINING_CUTOFF,
        model_kind="team_plus_rosh",
        availability_mode=MODE,
        min_samples=5,
    )
    observed_score_20 = tuple(
        float(row.features["score_20"])
        for row in rows
        if row.features["score_20"] is not None
    )
    expected_imputation = math.fsum(observed_score_20) / len(observed_score_20)
    expected_scale = math.sqrt(
        math.fsum((value - expected_imputation) ** 2 for value in observed_score_20)
        / len(observed_score_20)
    )

    assert dict(model.imputation_values)["score_20"] == expected_imputation
    assert dict(model.standardization_means)["score_20"] == expected_imputation
    assert dict(model.standardization_scales)["score_20"] == pytest.approx(
        max(expected_scale, PREMATCH_MIN_STANDARDIZATION_SCALE)
    )
    assert dict(model.missing_counts)["score_20"] == 6
    assert dict(model.imputation_values)["score_20__missing"] == 0.0
    assert dict(model.standardization_means)["score_20__missing"] == 0.0
    assert dict(model.standardization_scales)["score_20__missing"] == 1.0


def test_sparse_feature_cannot_create_extreme_standardized_prediction() -> None:
    rows = list(_rows("team_plus_draft", 81))
    sparse_values = (-0.052711, -0.048686)
    for index, row in enumerate(rows):
        features = dict(row.features)
        features["role_residual_diff"] = (
            sparse_values[index] if index < len(sparse_values) else None
        )
        features["role_residual_diff__missing"] = float(index >= len(sparse_values))
        rows[index] = replace(row, features=features)

    model = fit_prematch_model(
        rows,
        TRAINING_CUTOFF,
        model_kind="team_plus_draft",
        availability_mode=MODE,
    )
    target = _snapshot(
        8_781_808_385,
        TRAINING_CUTOFF + timedelta(days=1),
        team_base_logit=0.0,
        draft_signal=0.0,
        rosh_signal=0.0,
    )
    target_features = dict(target.draft_features)
    target_features["role_residual_diff"] = 0.168472
    target_features["role_residual_diff__missing"] = 0.0
    target = replace(
        target,
        draft_features=tuple(target_features.items()),
        input_hash="",
    )

    prediction = predict_prematch(model, target, top_n=len(model.feature_names))
    contribution = next(
        row
        for row in prediction.top_contributions
        if row.feature_name == "role_residual_diff"
    )

    assert dict(model.missing_counts)["role_residual_diff"] == 79
    assert PREMATCH_MIN_FEATURE_NONMISSING_SUPPORT == 20
    assert dict(model.standardization_scales)["role_residual_diff"] == 1.0
    assert contribution.standardized_value <= PREMATCH_MAX_ABS_STANDARDIZED_VALUE
    assert contribution.coefficient == pytest.approx(0.0, abs=1e-12)
    assert contribution.log_odds_contribution == pytest.approx(0.0, abs=1e-12)
    assert prediction.raw_probability is not None
    assert 0.0 < prediction.raw_probability < 1.0


def test_training_rejects_raw_and_missing_flag_disagreement() -> None:
    rows = list(_rows("team_plus_rosh", 20))
    features = dict(rows[0].features)
    assert features["score_20"] is None
    features["score_20__missing"] = 0.0
    rows[0] = replace(rows[0], features=features)

    with pytest.raises(ValueError, match="missing flag.*disagrees"):
        fit_prematch_model(
            rows,
            TRAINING_CUTOFF,
            model_kind="team_plus_rosh",
            availability_mode=MODE,
            min_samples=5,
        )


def test_team_only_is_intercept_only_and_l2_never_penalizes_intercept() -> None:
    rows = _rows("team_only", 30)
    low_l2 = fit_prematch_model(
        rows,
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
        l2_regularization=0.1,
    )
    high_l2 = fit_prematch_model(
        rows,
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
        l2_regularization=100.0,
    )

    assert low_l2.status is ModelStatus.TRAINED
    assert low_l2.feature_names == ()
    assert low_l2.coefficients == ()
    assert len(low_l2.logit_covariance) == 1
    assert low_l2.intercept == high_l2.intercept
    assert low_l2.logit_covariance == high_l2.logit_covariance


def test_small_and_one_class_support_return_explicit_insufficient_artifacts() -> None:
    too_small = fit_prematch_model(
        _rows("team_only", 3),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=10,
    )
    one_class = fit_prematch_model(
        _rows("team_only", 20, outcome_override=1),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )

    assert too_small.status is ModelStatus.INSUFFICIENT_EVIDENCE
    assert too_small.reason == "support_below_minimum"
    assert one_class.status is ModelStatus.INSUFFICIENT_EVIDENCE
    assert one_class.reason == "single_class_training_data"


def test_covariance_dimensions_symmetry_and_psd() -> None:
    model = fit_prematch_model(
        _rows("team_plus_rosh", 35),
        TRAINING_CUTOFF,
        model_kind="team_plus_rosh",
        availability_mode=MODE,
        min_samples=5,
    )
    covariance = np.asarray(model.logit_covariance)

    assert covariance.shape == (len(ROSH_MODEL_SCHEMA) + 1,) * 2
    assert np.array_equal(covariance, covariance.T)
    assert float(np.min(np.linalg.eigvalsh(covariance))) >= -1e-10


def test_prediction_components_uncertainty_and_contribution_order_reconstruct() -> None:
    model = fit_prematch_model(
        _rows("team_plus_draft_rosh", 45),
        TRAINING_CUTOFF,
        model_kind="team_plus_draft_rosh",
        availability_mode=MODE,
        min_samples=10,
    )
    target = _snapshot(
        500,
        TRAINING_CUTOFF + timedelta(days=1),
        team_base_logit=0.35,
        draft_signal=1.25,
        rosh_signal=-0.75,
        missing_rosh_score_20=True,
    )
    prediction = predict_prematch(model, target, top_n=8)

    assert prediction.status is PredictionStatus.PREDICTED
    assert prediction.cluster_logit_delta is None
    assert prediction.total_adjustment == (
        prediction.learned_intercept
        + prediction.draft_logit_delta
        + prediction.rosh_logit_delta
    )
    reconstructed_logit = math.log(
        prediction.raw_probability / (1.0 - prediction.raw_probability)
    )
    assert reconstructed_logit == pytest.approx(
        prediction.team_base_logit + prediction.total_adjustment,
        abs=1e-12,
    )

    features = project_prematch_features(target, model.model_kind)
    means = dict(model.standardization_means)
    scales = dict(model.standardization_scales)
    imputation = dict(model.imputation_values)
    standardized = []
    for name in model.feature_names:
        raw = features[name]
        value = imputation[name] if raw is None else raw
        standardized.append((value - means[name]) / scales[name])
    design = np.asarray((1.0, *standardized))
    covariance = np.asarray(model.logit_covariance)
    expected_uncertainty = (
        prediction.raw_probability
        * (1.0 - prediction.raw_probability)
        * math.sqrt(max(0.0, float(design @ covariance @ design)))
    )
    assert prediction.parameter_uncertainty == pytest.approx(
        expected_uncertainty,
        abs=1e-15,
    )
    assert "score_20" in prediction.missing_features
    assert (
        tuple(
            sorted(
                prediction.top_contributions,
                key=lambda row: (-abs(row.log_odds_contribution), row.feature_name),
            )
        )
        == prediction.top_contributions
    )
    assert all(
        not row.feature_name.endswith("__missing")
        for row in prediction.top_contributions
    )


def test_cluster_model_learns_a_separate_cluster_delta() -> None:
    model = fit_prematch_model(
        _rows(
            "team_plus_draft_rosh_clusters",
            20,
            with_cluster=True,
        ),
        TRAINING_CUTOFF,
        model_kind="team_plus_draft_rosh_clusters",
        availability_mode=MODE,
        min_samples=4,
    )
    target = _snapshot(
        850,
        TRAINING_CUTOFF + timedelta(days=1),
        team_base_logit=0.1,
        draft_signal=0.25,
        rosh_signal=-0.5,
        cluster_signal=1.0,
    )
    prediction = predict_prematch(model, target, top_n=len(model.feature_names))

    assert prediction.status is PredictionStatus.PREDICTED
    assert prediction.cluster_logit_delta is not None
    assert prediction.total_adjustment == pytest.approx(
        prediction.learned_intercept
        + prediction.draft_logit_delta
        + prediction.rosh_logit_delta
        + prediction.cluster_logit_delta
    )
    cluster_contributions = prediction.top_cluster_contributions
    assert cluster_contributions
    assert all(
        row.feature_name in PREMATCH_CLUSTER_MODEL_SCHEMA
        for row in cluster_contributions
    )


def test_prediction_uses_none_for_components_outside_model_kind() -> None:
    target = _snapshot(
        700,
        TRAINING_CUTOFF + timedelta(days=1),
        team_base_logit=0.1,
        draft_signal=0.5,
        rosh_signal=-0.5,
    )
    team_only = fit_prematch_model(
        _rows("team_only", 20),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )
    rosh_only = fit_prematch_model(
        _rows("team_plus_rosh", 20),
        TRAINING_CUTOFF,
        model_kind="team_plus_rosh",
        availability_mode=MODE,
        min_samples=5,
    )

    team_prediction = predict_prematch(team_only, target)
    rosh_prediction = predict_prematch(rosh_only, target)

    assert team_prediction.draft_logit_delta is None
    assert team_prediction.rosh_logit_delta is None
    assert team_prediction.total_adjustment == team_prediction.learned_intercept
    assert rosh_prediction.draft_logit_delta is None
    assert rosh_prediction.rosh_logit_delta is not None
    assert rosh_prediction.total_adjustment == (
        rosh_prediction.learned_intercept + rosh_prediction.rosh_logit_delta
    )


def test_prediction_rejects_evidence_mode_mix() -> None:
    model = fit_prematch_model(
        _rows("team_only", 20),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )
    prospective = _snapshot(
        600,
        TRAINING_CUTOFF + timedelta(days=1),
        team_base_logit=0.0,
        draft_signal=0.0,
        rosh_signal=0.0,
        availability_mode=AvailabilityMode.PROSPECTIVE.value,
    )
    with pytest.raises(ValueError, match="availability modes"):
        predict_prematch(model, prospective)


def test_prediction_rejects_target_match_in_training_corpus() -> None:
    model = fit_prematch_model(
        _rows("team_only", 20),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )
    target = _snapshot(
        model.training_corpus[0].match_id,
        TRAINING_CUTOFF + timedelta(days=1),
        team_base_logit=0.0,
        draft_signal=0.0,
        rosh_signal=0.0,
    )
    with pytest.raises(ValueError, match="target match"):
        predict_prematch(model, target)


def test_unsuccessful_optimizer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prematch_model,
        "minimize",
        lambda *_args, **_kwargs: SimpleNamespace(
            success=False,
            status=2,
            message="forced failure",
        ),
    )
    with pytest.raises(RuntimeError, match="optimizer failed"):
        fit_prematch_model(
            _rows("team_only", 20),
            TRAINING_CUTOFF,
            model_kind="team_only",
            availability_mode=MODE,
            min_samples=5,
        )


def test_offset_and_snapshot_hash_are_embedded_in_canonical_corpus() -> None:
    model = fit_prematch_model(
        _rows("team_only", 20),
        TRAINING_CUTOFF,
        model_kind="team_only",
        availability_mode=MODE,
        min_samples=5,
    )
    first = model.training_corpus[0]

    assert first.team_base_logit == _rows("team_only", 20)[0].team_base_logit
    assert len(first.input_snapshot_hash) == 64
    assert first.features == ()
    assert first.missing_features == ()
