from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import event_intelligence.prematch_report as prematch_report
from event_intelligence.draft_features import AvailabilityMode
from event_intelligence.draft_residual_features import (
    DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
    DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
    DRAFT_RESIDUAL_PURE_SCHEMA,
)
from event_intelligence.prematch_backtest import (
    PrematchBacktestTarget,
    PrematchCorpus,
    build_prematch_walk_forward,
)
from event_intelligence.prematch_features import (
    PREMATCH_FEATURE_VERSION,
    PrematchFeatureSnapshot,
)
from event_intelligence.prematch_report import (
    PrematchCalibrationReport,
    PrematchIncrementalReport,
    build_prematch_report,
    prematch_model_run_metrics,
    report_as_dict,
    report_as_markdown,
)
from event_intelligence.rosh_features import (
    ROSH_FEATURE_SCHEMA,
    ROSH_MODEL_SCHEMA_HASH,
)


UTC = timezone.utc
START = datetime(2026, 3, 1, tzinfo=UTC)
MODE = AvailabilityMode.RECONSTRUCTED.value


def _digest(number: int) -> str:
    return f"{number:064x}"


def _snapshot(index: int, *, rosh_available: bool) -> PrematchFeatureSnapshot:
    cutoff = START + timedelta(days=index)
    team_logit = (index % 5 - 2) * 0.18
    draft_signal = float(index % 7 - 3)
    draft: dict[str, float | None] = {}
    for offset, name in enumerate(DRAFT_RESIDUAL_PURE_SCHEMA):
        draft[name] = draft_signal if offset == 0 else float(offset) / 10.0
        draft[f"{name}__log1p_support"] = math.log1p(15 + index)
        draft[f"{name}__coverage"] = 0.75
        draft[f"{name}__missing"] = 0.0
    rosh: dict[str, float | None] = {}
    for offset, name in enumerate(ROSH_FEATURE_SCHEMA):
        value: float | None
        if not rosh_available and name != "coverage":
            value = None
        elif name == "coverage":
            value = 0.9 if rosh_available else 0.0
        else:
            value = float((index + offset) % 11 - 5)
        rosh[name] = value
        rosh[f"{name}__missing"] = 1.0 if value is None else 0.0
    match_id = index + 1
    return PrematchFeatureSnapshot(
        match_id=match_id,
        prediction_cutoff=cutoff,
        availability_mode=MODE,
        feature_version=PREMATCH_FEATURE_VERSION,
        team_base_logit=team_logit,
        team_rating_run_id=_digest(10_000 + match_id),
        team_rating_artifact_hash=_digest(20_000 + match_id),
        team_rating_prediction_input_hash=_digest(30_000 + match_id),
        team_rating_combined_training_input_hash=_digest(40_000 + match_id),
        team_rating_support=40,
        draft_residual_input_hash=_digest(50_000 + match_id),
        draft_residual_authority_fingerprint=_digest(60_000 + match_id),
        draft_residual_team_rating_input_hash=_digest(70_000 + match_id),
        draft_residual_feature_schema_hash=DRAFT_RESIDUAL_FEATURE_SCHEMA_HASH,
        draft_residual_model_schema_hash=DRAFT_RESIDUAL_MODEL_SCHEMA_HASH,
        draft_support=30,
        draft_coverage=0.75,
        draft_features=tuple(draft.items()),
        rosh_status="available" if rosh_available else "unavailable",
        rosh_missing_reason=None if rosh_available else "no_cutoff_legal_run",
        rosh_input_hash=_digest(80_000 + match_id),
        rosh_model_schema_hash=ROSH_MODEL_SCHEMA_HASH,
        rosh_run_id=_digest(90_000 + match_id) if rosh_available else None,
        rosh_evidence_hash=_digest(100_000 + match_id) if rosh_available else None,
        rosh_formula_version="formula-v1" if rosh_available else None,
        rosh_profile_hash=_digest(110_000 + match_id) if rosh_available else None,
        rosh_result_hash=_digest(120_000 + match_id) if rosh_available else None,
        rosh_coverage=0.9 if rosh_available else 0.0,
        rosh_features=tuple(rosh.items()),
    )


def _target(index: int) -> PrematchBacktestTarget:
    snapshot = _snapshot(index, rosh_available=5 <= index < 14)
    draft_signal = float(index % 7 - 3)
    rosh_signal = float((index * 2) % 9 - 4)
    outcome = snapshot.team_base_logit + 0.7 * draft_signal + 0.2 * rosh_signal > 0
    return PrematchBacktestTarget(
        match_id=index + 1,
        series_id=index // 2 + 1,
        event_id=f"event-{index // 10}",
        patch_id=f"7.{40 + index // 14}",
        prediction_cutoff=snapshot.prediction_cutoff,
        completed_at=snapshot.prediction_cutoff + timedelta(hours=1),
        result_usable_at=snapshot.prediction_cutoff + timedelta(hours=2),
        cutoff_source="reconstructed_map_start",
        availability_mode=MODE,
        outcome=outcome,
        team_base_probability=1.0 / (1.0 + math.exp(-snapshot.team_base_logit)),
        radiant_prior_probability=(index + 1.0) / (index + 2.0),
        snapshot=snapshot,
        failure_reason=None,
    )


@pytest.fixture(scope="module")
def backtest_result():
    targets = tuple(_target(index) for index in range(28))
    return build_prematch_walk_forward(
        PrematchCorpus(MODE, len(targets), targets),
        min_samples=4,
    )


def test_report_contains_all_slices_metrics_and_component_qualified_support(
    backtest_result,
) -> None:
    report = build_prematch_report(backtest_result, bootstrap_samples=80)

    assert tuple(row.slice_id for row in report.model_slices) == (
        "M0",
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
        "M6_CLUSTER",
    )
    assert all(row.eligible_targets == 28 for row in report.model_slices)
    assert all(row.support == row.predicted for row in report.model_slices)
    assert all(
        row.brier_score is not None
        and row.log_loss is not None
        and row.ece is not None
        and row.accuracy is not None
        for row in report.model_slices[:-1]
    )
    cluster_slice = report.model_slices[-1]
    assert cluster_slice.model_name == "team_plus_draft_rosh_clusters"
    assert cluster_slice.support == 0
    assert cluster_slice.cluster_available_support == 0
    comparisons = {row.comparison: row for row in report.incremental_comparisons}
    assert tuple(comparisons) == (
        "M3-M2",
        "M4-M2",
        "M5-M3",
        "M5-M4",
        "M5-M2",
        "M6_CLUSTER-M5",
    )
    assert comparisons["M4-M2"].available_support == 9
    assert comparisons["M5-M3"].available_support == 9
    assert comparisons["M4-M2"].status == "unsupported"
    assert comparisons["M5-M3"].status == "unsupported"
    assert "no_incremental_value" in comparisons["M4-M2"].reasons
    assert comparisons["M5-M2"].available_support == 24
    assert comparisons["M6_CLUSTER-M5"].available_support == 0
    assert comparisons["M6_CLUSTER-M5"].reasons == (
        "cluster_evidence_unavailable",
        "no_incremental_value",
    )
    assert report.default_decision.model_kind != "team_plus_draft_rosh"
    assert report.default_decision.status != "passed"


def test_bootstrap_report_is_deterministic_and_serializable(backtest_result) -> None:
    first = build_prematch_report(backtest_result, bootstrap_samples=40)
    second = build_prematch_report(backtest_result, bootstrap_samples=40)

    assert first == second
    payload = report_as_dict(first)
    assert payload["bootstrap_samples"] == 40
    assert payload["model_slices"][0]["support"] == 28
    markdown = report_as_markdown(first)
    assert "M5-M2" in markdown
    assert "M6_CLUSTER-M5" in markdown
    assert "cluster_evidence_unavailable" in str(payload)
    assert "Available support" in markdown
    assert "reconstructed_walk_forward" in markdown

    metrics = prematch_model_run_metrics(first, "team_plus_draft_rosh")
    assert metrics["schema"] == "prematch-model-run-metrics/v1"
    assert metrics["bootstrap"]["samples"] == 40
    assert [row["comparison"] for row in metrics["incremental_comparisons"]] == [
        "M5-M3",
        "M5-M4",
        "M5-M2",
    ]


def _incremental(name: str, status: str) -> PrematchIncrementalReport:
    return PrematchIncrementalReport(
        comparison=name,
        added_component="combined" if name == "M5-M2" else "draft",
        available_support=100,
        status=status,
        reasons=(),
        metrics=(),
    )


def _calibration(model_kind: str, gate_passed: bool) -> PrematchCalibrationReport:
    return PrematchCalibrationReport(
        slice_id="M5",
        model_kind=model_kind,
        status="reconstructed_only",
        reason=None,
        fit_support=50,
        evaluation_support=100,
        raw_metrics=None,
        calibrated_metrics=None,
        ece_90_upper=0.05 if gate_passed else 0.25,
        gate_passed=gate_passed,
        gate_reasons=() if gate_passed else ("ece_90_upper_above_0.15",),
        calibration_hash=_digest(len(model_kind)),
    )


def test_default_decision_falls_back_when_complex_candidate_fails_calibration() -> None:
    comparisons = (
        _incremental("M3-M2", "incremental_value"),
        _incremental("M4-M2", "no_incremental_value"),
        _incremental("M5-M3", "incremental_value"),
        _incremental("M5-M4", "incremental_value"),
        _incremental("M5-M2", "incremental_value"),
    )
    calibrations = (
        _calibration("team_only", True),
        _calibration("team_plus_draft", True),
        _calibration("team_plus_rosh", True),
        _calibration("team_plus_draft_rosh", False),
    )

    decision = prematch_report._default_decision(  # noqa: SLF001
        SimpleNamespace(availability_mode=MODE),
        comparisons,
        calibrations,
    )

    assert decision.model_kind == "team_plus_draft"
    assert decision.status == "reconstructed_only"


def test_combined_default_requires_direct_team_only_paired_gate() -> None:
    comparisons = (
        _incremental("M3-M2", "no_incremental_value"),
        _incremental("M4-M2", "no_incremental_value"),
        _incremental("M5-M3", "incremental_value"),
        _incremental("M5-M4", "incremental_value"),
        _incremental("M5-M2", "no_incremental_value"),
    )
    calibrations = tuple(
        _calibration(kind, True)
        for kind in (
            "team_only",
            "team_plus_draft",
            "team_plus_rosh",
            "team_plus_draft_rosh",
        )
    )

    decision = prematch_report._default_decision(  # noqa: SLF001
        SimpleNamespace(availability_mode=MODE),
        comparisons,
        calibrations,
    )

    assert decision.model_kind is None
    assert decision.status == "no_incremental_value"


def test_invalid_bootstrap_sample_count_is_rejected(backtest_result) -> None:
    with pytest.raises(ValueError, match="bootstrap sample count"):
        build_prematch_report(backtest_result, bootstrap_samples=0)


def _cluster_point(
    match_id: int,
    probability: float,
    outcome: bool,
    *,
    series_id: int,
):
    return prematch_report._EvaluationPoint(  # noqa: SLF001
        match_id=match_id,
        series_id=series_id,
        outcome=outcome,
        probability=probability,
        coverage=1.0,
        draft_available=True,
        rosh_available=True,
        cluster_available=True,
    )


def test_cluster_gate_requires_brier_ci_and_no_clear_log_loss_regression() -> None:
    baseline = tuple(
        _cluster_point(
            index + 1,
            0.6 if index % 2 else 0.4,
            bool(index % 2),
            series_id=index // 2,
        )
        for index in range(20)
    )
    improved = tuple(
        _cluster_point(
            index + 1,
            0.8 if index % 2 else 0.2,
            bool(index % 2),
            series_id=index // 2,
        )
        for index in range(20)
    )
    passed = prematch_report._incremental_report(  # noqa: SLF001
        "M6_CLUSTER-M5",
        "cluster",
        improved,
        baseline,
        bootstrap_samples=40,
    )

    assert passed.status == "incremental_value"
    assert passed.reasons == ()
    assert {row.metric: row for row in passed.metrics}[
        "brier_score"
    ].ci_90.upper < 0.0

    all_true_baseline = tuple(
        _cluster_point(index + 1, 0.6, True, series_id=1)
        for index in range(20)
    )
    log_loss_regression = tuple(
        _cluster_point(
            index + 1,
            1e-6 if index == 0 else 0.8,
            True,
            series_id=1,
        )
        for index in range(20)
    )
    stopped = prematch_report._incremental_report(  # noqa: SLF001
        "M6_CLUSTER-M5",
        "cluster",
        log_loss_regression,
        all_true_baseline,
        bootstrap_samples=20,
    )

    assert stopped.status == "no_incremental_value"
    assert stopped.reasons == ("paired_log_loss_clearly_worse",)
