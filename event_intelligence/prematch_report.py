"""M6 ablation, calibration, and incremental-value reporting."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence, TypeVar

from .draft_model import BinaryMetrics, evaluate_binary_predictions
from .draft_residual_features import DRAFT_RESIDUAL_PURE_SCHEMA
from .prematch_backtest import (
    PREMATCH_BACKTEST_VERSION,
    PrematchBacktestResult,
    PrematchBacktestTarget,
)
from .prematch_calibration import PrematchCalibrationArtifact
from .prematch_model import PredictionStatus
from .team_rating_backtest import (
    BOOTSTRAP_ALGORITHM_VERSION,
    BootstrapInterval,
    MetricIntervals,
)


PREMATCH_BOOTSTRAP_SAMPLES = 1_000
PREMATCH_CALIBRATION_BINS = 5
PREMATCH_MIN_INCREMENTAL_SUPPORT = 20
PREMATCH_BOOTSTRAP_SEED_MATERIAL = f"{PREMATCH_BACKTEST_VERSION}:m0-m5"
PREMATCH_MODEL_RUN_METRICS_SCHEMA = "prematch-model-run-metrics/v1"

_SLICE_KINDS = (
    ("M0", "constant_50"),
    ("M1", "radiant_prior"),
    ("M2", "team_only"),
    ("M3", "team_plus_draft"),
    ("M4", "team_plus_rosh"),
    ("M5", "team_plus_draft_rosh"),
    ("M6_CLUSTER", "team_plus_draft_rosh_clusters"),
)
_INCREMENTAL_COMPARISONS = (
    ("M3-M2", "draft", "M3", "M2"),
    ("M4-M2", "rosh", "M4", "M2"),
    ("M5-M3", "rosh", "M5", "M3"),
    ("M5-M4", "draft", "M5", "M4"),
    # Deployment additionally requires a direct comparison with team_only.
    # This uses the complete common OOS population and is not R.O.S.H. support.
    ("M5-M2", "combined", "M5", "M2"),
    ("M6_CLUSTER-M5", "cluster", "M6_CLUSTER", "M5"),
)
_RELATED_COMPARISONS = {
    "team_only": ("M3-M2", "M4-M2", "M5-M2"),
    "team_plus_draft": ("M3-M2", "M5-M3"),
    "team_plus_rosh": ("M4-M2", "M5-M4"),
    "team_plus_draft_rosh": ("M5-M3", "M5-M4", "M5-M2"),
    "team_plus_draft_rosh_clusters": (
        "M6_CLUSTER-M5",
        "M5-M3",
        "M5-M4",
        "M5-M2",
    ),
}
_PointT = TypeVar("_PointT")


@dataclass(frozen=True)
class CoverageDistribution:
    support: int
    minimum: float | None
    p25: float | None
    median: float | None
    p75: float | None
    maximum: float | None
    mean: float | None


@dataclass(frozen=True)
class PrematchModelSliceReport:
    slice_id: str
    model_name: str
    eligible_targets: int
    predicted: int
    insufficient_evidence: int
    support: int
    draft_available_support: int
    rosh_available_support: int
    cluster_available_support: int
    coverage: CoverageDistribution
    brier_score: float | None
    log_loss: float | None
    ece: float | None
    auc: float | None
    accuracy: float | None
    intervals: MetricIntervals


@dataclass(frozen=True)
class PairedMetricReport:
    metric: str
    delta: float | None
    ci_90: BootstrapInterval
    ci_95: BootstrapInterval
    probability_of_improvement: float | None


@dataclass(frozen=True)
class PrematchIncrementalReport:
    comparison: str
    added_component: str
    available_support: int
    status: str
    reasons: tuple[str, ...]
    metrics: tuple[PairedMetricReport, ...]


@dataclass(frozen=True)
class PrematchCalibrationReport:
    slice_id: str
    model_kind: str
    status: str
    reason: str | None
    fit_support: int
    evaluation_support: int
    raw_metrics: BinaryMetrics | None
    calibrated_metrics: BinaryMetrics | None
    ece_90_upper: float | None
    gate_passed: bool
    gate_reasons: tuple[str, ...]
    calibration_hash: str


@dataclass(frozen=True)
class PrematchDefaultDecision:
    model_kind: str | None
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PrematchBacktestReport:
    backtest_version: str
    bootstrap_algorithm: str
    bootstrap_seed_material: str
    bootstrap_samples: int
    availability_mode: str
    formal_maps: int
    eligible_targets: int
    snapshot_targets: int
    unavailable_targets: int
    model_slices: tuple[PrematchModelSliceReport, ...]
    incremental_comparisons: tuple[PrematchIncrementalReport, ...]
    calibration: tuple[PrematchCalibrationReport, ...]
    default_decision: PrematchDefaultDecision


@dataclass(frozen=True)
class _EvaluationPoint:
    match_id: int
    series_id: int | None
    outcome: bool
    probability: float
    coverage: float
    draft_available: bool
    rosh_available: bool
    cluster_available: bool


@dataclass(frozen=True)
class _PairedPoint:
    match_id: int
    series_id: int | None
    outcome: bool
    candidate_probability: float
    baseline_probability: float


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _interval(values: Sequence[float], confidence: float) -> BootstrapInterval:
    tail = (1.0 - confidence) / 2.0
    return BootstrapInterval(
        _percentile(values, tail),
        _percentile(values, 1.0 - tail),
    )


def _coverage(values: Iterable[float]) -> CoverageDistribution:
    rows = tuple(sorted(float(value) for value in values))
    if not rows:
        return CoverageDistribution(0, None, None, None, None, None, None)
    return CoverageDistribution(
        support=len(rows),
        minimum=rows[0],
        p25=_percentile(rows, 0.25),
        median=_percentile(rows, 0.50),
        p75=_percentile(rows, 0.75),
        maximum=rows[-1],
        mean=math.fsum(rows) / len(rows),
    )


def _cluster_samples(
    points: Sequence[_PointT],
    *,
    series_id: Any,
    match_id: Any,
    seed_material: str,
    samples: int,
) -> tuple[tuple[_PointT, ...], ...]:
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
        raise ValueError("bootstrap sample count must be positive")
    if not points:
        return ()
    clusters: dict[str, list[_PointT]] = {}
    for point in points:
        series = series_id(point)
        match = match_id(point)
        key = f"series:{series}" if series is not None else f"match:{match}"
        clusters.setdefault(key, []).append(point)
    keys = sorted(clusters)
    seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    return tuple(
        tuple(
            point
            for _key in keys
            for point in clusters[keys[generator.randrange(len(keys))]]
        )
        for _sample in range(samples)
    )


def _metrics(points: Sequence[_EvaluationPoint]) -> BinaryMetrics:
    return evaluate_binary_predictions(
        (point.outcome for point in points),
        (point.probability for point in points),
        ece_bins=PREMATCH_CALIBRATION_BINS,
    )


def _metric_value(metrics: BinaryMetrics, name: str) -> float | None:
    return {
        "brier_score": metrics.brier_score,
        "log_loss": metrics.log_loss,
        "ece": metrics.expected_calibration_error,
        "auc": metrics.auc,
        "accuracy": metrics.accuracy,
    }[name]


def _metric_intervals(
    points: Sequence[_EvaluationPoint],
    *,
    slice_id: str,
    bootstrap_samples: int,
) -> MetricIntervals:
    estimates: dict[str, list[float]] = {
        name: [] for name in ("brier_score", "log_loss", "ece", "auc", "accuracy")
    }
    samples = _cluster_samples(
        points,
        series_id=lambda row: row.series_id,
        match_id=lambda row: row.match_id,
        seed_material=f"{PREMATCH_BOOTSTRAP_SEED_MATERIAL}:{slice_id}",
        samples=bootstrap_samples,
    )
    for sample in samples:
        metrics = _metrics(sample)
        for name in estimates:
            value = _metric_value(metrics, name)
            if value is not None:
                estimates[name].append(value)
    return MetricIntervals(
        brier_score_90=_interval(estimates["brier_score"], 0.90),
        brier_score_95=_interval(estimates["brier_score"], 0.95),
        log_loss_90=_interval(estimates["log_loss"], 0.90),
        log_loss_95=_interval(estimates["log_loss"], 0.95),
        ece_90=_interval(estimates["ece"], 0.90),
        ece_95=_interval(estimates["ece"], 0.95),
        auc_90=_interval(estimates["auc"], 0.90),
        auc_95=_interval(estimates["auc"], 0.95),
        accuracy_90=_interval(estimates["accuracy"], 0.90),
        accuracy_95=_interval(estimates["accuracy"], 0.95),
    )


def _availability(target: PrematchBacktestTarget) -> tuple[bool, bool, bool]:
    snapshot = target.snapshot
    if snapshot is None:
        return False, False, False
    draft_values = dict(snapshot.draft_features)
    draft_available = snapshot.draft_coverage > 0.0 and any(
        draft_values[name] is not None for name in DRAFT_RESIDUAL_PURE_SCHEMA
    )
    return (
        draft_available,
        snapshot.rosh_status == "available",
        snapshot.cluster_artifact is not None and snapshot.cluster_coverage > 0.0,
    )


def _coverage_for_model(target: PrematchBacktestTarget, slice_id: str) -> float:
    snapshot = target.snapshot
    if slice_id in {"M0", "M1", "M2"} or snapshot is None:
        return 1.0
    if slice_id == "M3":
        return snapshot.draft_coverage
    if slice_id == "M4":
        return snapshot.rosh_coverage
    if slice_id == "M5":
        return snapshot.coverage
    if slice_id == "M6_CLUSTER":
        return snapshot.cluster_coverage
    raise ValueError("unsupported prematch ablation slice")


def _points_by_slice(
    result: PrematchBacktestResult,
) -> dict[str, tuple[_EvaluationPoint, ...]]:
    eligible = tuple(
        row
        for row in result.corpus.targets
        if row.result_usable_at is not None
        and row.result_usable_at <= result.evaluation_cutoff
    )
    output: dict[str, list[_EvaluationPoint]] = {name: [] for name, _ in _SLICE_KINDS}
    for target in eligible:
        draft_available, rosh_available, cluster_available = _availability(target)
        for slice_id, probability in (
            ("M0", 0.5),
            ("M1", target.radiant_prior_probability),
        ):
            output[slice_id].append(
                _EvaluationPoint(
                    target.match_id,
                    target.series_id,
                    target.outcome,
                    probability,
                    _coverage_for_model(target, slice_id),
                    draft_available,
                    rosh_available,
                    cluster_available,
                )
            )
    slice_by_kind = {kind: slice_id for slice_id, kind in _SLICE_KINDS[2:]}
    for run in result.walk_forward_runs:
        prediction = run.prediction
        if (
            prediction.status is not PredictionStatus.PREDICTED
            or prediction.raw_probability is None
            or run.target.result_usable_at is None
            or run.target.result_usable_at > result.evaluation_cutoff
        ):
            continue
        slice_id = slice_by_kind[run.model_kind]
        draft_available, rosh_available, cluster_available = _availability(run.target)
        output[slice_id].append(
            _EvaluationPoint(
                run.target.match_id,
                run.target.series_id,
                run.target.outcome,
                prediction.raw_probability,
                _coverage_for_model(run.target, slice_id),
                draft_available,
                rosh_available,
                cluster_available,
            )
        )
    return {key: tuple(value) for key, value in output.items()}


def _model_slice(
    slice_id: str,
    model_name: str,
    points: Sequence[_EvaluationPoint],
    *,
    eligible_targets: int,
    bootstrap_samples: int,
) -> PrematchModelSliceReport:
    metrics = _metrics(points)
    return PrematchModelSliceReport(
        slice_id=slice_id,
        model_name=model_name,
        eligible_targets=eligible_targets,
        predicted=len(points),
        insufficient_evidence=max(0, eligible_targets - len(points)),
        support=metrics.support,
        draft_available_support=sum(point.draft_available for point in points),
        rosh_available_support=sum(point.rosh_available for point in points),
        cluster_available_support=sum(point.cluster_available for point in points),
        coverage=_coverage(point.coverage for point in points),
        brier_score=metrics.brier_score,
        log_loss=metrics.log_loss,
        ece=metrics.expected_calibration_error,
        auc=metrics.auc,
        accuracy=metrics.accuracy,
        intervals=_metric_intervals(
            points,
            slice_id=slice_id,
            bootstrap_samples=bootstrap_samples,
        ),
    )


def _loss(point: _PairedPoint, metric: str) -> float:
    candidate = min(max(point.candidate_probability, 1e-15), 1.0 - 1e-15)
    baseline = min(max(point.baseline_probability, 1e-15), 1.0 - 1e-15)
    outcome = float(point.outcome)
    if metric == "brier_score":
        return (candidate - outcome) ** 2 - (baseline - outcome) ** 2
    if metric == "log_loss":
        candidate_loss = -math.log(candidate if point.outcome else 1.0 - candidate)
        baseline_loss = -math.log(baseline if point.outcome else 1.0 - baseline)
        return candidate_loss - baseline_loss
    raise ValueError("unsupported paired metric")


def _paired_metric(
    points: Sequence[_PairedPoint],
    samples: Sequence[Sequence[_PairedPoint]],
    metric: str,
) -> PairedMetricReport:
    def estimate(rows: Sequence[_PairedPoint]) -> float | None:
        if not rows:
            return None
        return math.fsum(_loss(row, metric) for row in rows) / len(rows)

    estimates = tuple(
        value for sample in samples if (value := estimate(sample)) is not None
    )
    return PairedMetricReport(
        metric=metric,
        delta=estimate(points),
        ci_90=_interval(estimates, 0.90),
        ci_95=_interval(estimates, 0.95),
        probability_of_improvement=(
            None
            if not estimates
            else sum(value < 0.0 for value in estimates) / len(estimates)
        ),
    )


def _incremental_report(
    comparison: str,
    component: str,
    candidate: Sequence[_EvaluationPoint],
    baseline: Sequence[_EvaluationPoint],
    *,
    bootstrap_samples: int,
) -> PrematchIncrementalReport:
    candidate_by_match = {row.match_id: row for row in candidate}
    baseline_by_match = {row.match_id: row for row in baseline}
    paired: list[_PairedPoint] = []
    for match_id in sorted(candidate_by_match.keys() & baseline_by_match.keys()):
        candidate_point = candidate_by_match[match_id]
        baseline_point = baseline_by_match[match_id]
        available = (
            candidate_point.draft_available
            if component == "draft"
            else candidate_point.rosh_available
            if component == "rosh"
            else candidate_point.cluster_available
            if component == "cluster"
            else True
        )
        if not available:
            continue
        if (
            candidate_point.series_id != baseline_point.series_id
            or candidate_point.outcome != baseline_point.outcome
        ):
            raise ValueError("paired prematch target identities disagree")
        paired.append(
            _PairedPoint(
                match_id=match_id,
                series_id=candidate_point.series_id,
                outcome=candidate_point.outcome,
                candidate_probability=candidate_point.probability,
                baseline_probability=baseline_point.probability,
            )
        )
    samples = _cluster_samples(
        paired,
        series_id=lambda row: row.series_id,
        match_id=lambda row: row.match_id,
        seed_material=f"{PREMATCH_BOOTSTRAP_SEED_MATERIAL}:{comparison}",
        samples=bootstrap_samples,
    )
    metrics = tuple(
        _paired_metric(paired, samples, metric)
        for metric in ("brier_score", "log_loss")
    )
    if len(paired) < PREMATCH_MIN_INCREMENTAL_SUPPORT:
        status = "unsupported"
        reasons = (
            "cluster_evidence_unavailable"
            if component == "cluster" and not paired
            else f"{component}_available_support_below_{PREMATCH_MIN_INCREMENTAL_SUPPORT}",
            "no_incremental_value",
        )
    elif component == "cluster":
        by_name = {row.metric: row for row in metrics}
        brier = by_name["brier_score"]
        log_loss = by_name["log_loss"]
        brier_passed = (
            brier.delta is not None
            and brier.delta < 0.0
            and brier.ci_90.upper is not None
            and brier.ci_90.upper < 0.0
        )
        log_loss_clearly_worse = (
            log_loss.delta is not None
            and log_loss.delta > 0.0
            and log_loss.ci_90.lower is not None
            and log_loss.ci_90.lower > 0.0
        )
        status = (
            "incremental_value"
            if brier_passed and not log_loss_clearly_worse
            else "no_incremental_value"
        )
        reasons = tuple(
            reason
            for failed, reason in (
                (not brier_passed, "paired_brier_90_ci_not_below_zero"),
                (log_loss_clearly_worse, "paired_log_loss_clearly_worse"),
            )
            if failed
        )
    else:
        significant = any(
            row.delta is not None
            and row.delta < 0.0
            and row.ci_90.upper is not None
            and row.ci_90.upper < 0.0
            for row in metrics
        )
        status = "incremental_value" if significant else "no_incremental_value"
        reasons = () if significant else ("paired_90_ci_not_below_zero",)
    return PrematchIncrementalReport(
        comparison=comparison,
        added_component=component,
        available_support=len(paired),
        status=status,
        reasons=reasons,
        metrics=metrics,
    )


def _calibration_report(
    slice_id: str,
    artifact: PrematchCalibrationArtifact,
) -> PrematchCalibrationReport:
    return PrematchCalibrationReport(
        slice_id=slice_id,
        model_kind=artifact.model_kind,
        status=artifact.status.value,
        reason=artifact.reason,
        fit_support=artifact.fit_support,
        evaluation_support=artifact.evaluation_support,
        raw_metrics=artifact.raw_metrics,
        calibrated_metrics=artifact.calibrated_metrics,
        ece_90_upper=artifact.ece_90_upper,
        gate_passed=artifact.gate_passed,
        gate_reasons=artifact.gate_reasons,
        calibration_hash=artifact.calibration_hash,
    )


def _default_decision(
    result: PrematchBacktestResult,
    comparisons: Sequence[PrematchIncrementalReport],
    calibrations: Sequence[PrematchCalibrationReport],
) -> PrematchDefaultDecision:
    comparison = {row.comparison: row for row in comparisons}
    calibration = {row.model_kind: row for row in calibrations}
    candidates = (
        (
            "team_plus_draft_rosh_clusters",
            ("M6_CLUSTER-M5", "M5-M3", "M5-M4", "M5-M2"),
        ),
        (
            "team_plus_draft_rosh",
            ("M5-M3", "M5-M4", "M5-M2"),
        ),
        ("team_plus_draft", ("M3-M2",)),
        ("team_plus_rosh", ("M4-M2",)),
    )
    rejected: list[str] = []
    for model_kind, required in candidates:
        if not all(
            name in comparison and comparison[name].status == "incremental_value"
            for name in required
        ):
            continue
        gate = calibration[model_kind]
        if not gate.gate_passed:
            rejected.extend(
                (f"{model_kind}_calibration_gate_failed", *gate.gate_reasons)
            )
            continue
        return PrematchDefaultDecision(
            model_kind,
            (
                "reconstructed_only"
                if result.availability_mode == "reconstructed_walk_forward"
                else "shadow_collecting"
            ),
            ("reconstructed_evidence_cannot_authorize_prospective_runtime",)
            if result.availability_mode == "reconstructed_walk_forward"
            else ("prospective_validation_gate_not_evaluated_until_M8",),
        )
    unsupported = tuple(
        row.comparison for row in comparisons if row.status == "unsupported"
    )
    if rejected:
        return PrematchDefaultDecision(None, "failed", tuple(dict.fromkeys(rejected)))
    return PrematchDefaultDecision(
        None,
        "unsupported" if unsupported else "no_incremental_value",
        (
            tuple(f"{name}_unsupported" for name in unsupported)
            if unsupported
            else ("no_component_has_significant_incremental_value",)
        ),
    )


def build_prematch_report(
    result: PrematchBacktestResult,
    *,
    bootstrap_samples: int = PREMATCH_BOOTSTRAP_SAMPLES,
) -> PrematchBacktestReport:
    if not isinstance(result, PrematchBacktestResult):
        raise ValueError("result must be a PrematchBacktestResult")
    if (
        isinstance(bootstrap_samples, bool)
        or not isinstance(bootstrap_samples, int)
        or bootstrap_samples < 1
    ):
        raise ValueError("bootstrap sample count must be positive")
    points = _points_by_slice(result)
    model_slices = tuple(
        _model_slice(
            slice_id,
            model_name,
            points[slice_id],
            eligible_targets=result.corpus.eligible_targets,
            bootstrap_samples=bootstrap_samples,
        )
        for slice_id, model_name in _SLICE_KINDS
    )
    comparisons = tuple(
        _incremental_report(
            name,
            component,
            points[candidate],
            points[baseline],
            bootstrap_samples=bootstrap_samples,
        )
        for name, component, candidate, baseline in _INCREMENTAL_COMPARISONS
    )
    slice_by_kind = {kind: slice_id for slice_id, kind in _SLICE_KINDS[2:]}
    calibrations = tuple(
        _calibration_report(
            slice_by_kind[row.model_artifact.model_kind],
            row.calibration_artifact,
        )
        for row in result.final_models
    )
    return PrematchBacktestReport(
        backtest_version=result.backtest_version,
        bootstrap_algorithm=BOOTSTRAP_ALGORITHM_VERSION,
        bootstrap_seed_material=PREMATCH_BOOTSTRAP_SEED_MATERIAL,
        bootstrap_samples=bootstrap_samples,
        availability_mode=result.availability_mode,
        formal_maps=result.corpus.formal_maps,
        eligible_targets=result.corpus.eligible_targets,
        snapshot_targets=sum(row.snapshot is not None for row in result.corpus.targets),
        unavailable_targets=sum(row.snapshot is None for row in result.corpus.targets),
        model_slices=model_slices,
        incremental_comparisons=comparisons,
        calibration=calibrations,
        default_decision=_default_decision(result, comparisons, calibrations),
    )


def report_as_dict(report: PrematchBacktestReport) -> dict[str, Any]:
    if not isinstance(report, PrematchBacktestReport):
        raise ValueError("report must be a PrematchBacktestReport")
    return asdict(report)


def prematch_model_run_metrics(
    report: PrematchBacktestReport,
    model_kind: str,
) -> dict[str, Any]:
    if not isinstance(report, PrematchBacktestReport):
        raise ValueError("report must be a PrematchBacktestReport")
    related = _RELATED_COMPARISONS.get(model_kind)
    if related is None:
        raise ValueError("unsupported prematch model kind")
    slices = {row.model_name: row for row in report.model_slices}
    calibrations = {row.model_kind: row for row in report.calibration}
    if model_kind not in slices or model_kind not in calibrations:
        raise ValueError("prematch report lacks final model evidence")
    comparisons = {row.comparison: row for row in report.incremental_comparisons}
    if any(name not in comparisons for name in related):
        raise ValueError("prematch report lacks related paired evidence")
    return {
        "schema": PREMATCH_MODEL_RUN_METRICS_SCHEMA,
        "backtest_version": report.backtest_version,
        "availability_mode": report.availability_mode,
        "bootstrap": {
            "algorithm": report.bootstrap_algorithm,
            "seed_material": report.bootstrap_seed_material,
            "samples": report.bootstrap_samples,
        },
        "formal_evaluation": {
            "formal_maps": report.formal_maps,
            "eligible_targets": report.eligible_targets,
            "snapshot_targets": report.snapshot_targets,
            "unavailable_targets": report.unavailable_targets,
        },
        "slice": asdict(slices[model_kind]),
        "calibration": asdict(calibrations[model_kind]),
        "incremental_comparisons": [asdict(comparisons[name]) for name in related],
        "default_decision": asdict(report.default_decision),
    }


def report_as_markdown(report: PrematchBacktestReport) -> str:
    if not isinstance(report, PrematchBacktestReport):
        raise ValueError("report must be a PrematchBacktestReport")

    def render(value: float | None) -> str:
        return "N/A" if value is None else f"{value:.6f}"

    lines = [
        "# Prematch M6 Walk-Forward Report",
        "",
        f"- Availability mode: `{report.availability_mode}`",
        f"- Formal maps: {report.formal_maps}",
        f"- Eligible targets: {report.eligible_targets}",
        f"- Replayed snapshots: {report.snapshot_targets}",
        f"- Unavailable targets: {report.unavailable_targets}",
        "",
        "## Ablations",
        "",
        "| Slice | Model | Eligible | Predicted | Insufficient | Support | Cluster available | Coverage mean | Brier | Log loss | ECE | AUC | Accuracy |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.model_slices:
        lines.append(
            f"| {row.slice_id} | {row.model_name} | {row.eligible_targets} | "
            f"{row.predicted} | {row.insufficient_evidence} | {row.support} | "
            f"{row.cluster_available_support} | {render(row.coverage.mean)} | "
            f"{render(row.brier_score)} | "
            f"{render(row.log_loss)} | {render(row.ece)} | {render(row.auc)} | "
            f"{render(row.accuracy)} |"
        )
    lines.extend(
        (
            "",
            "## Incremental Comparisons",
            "",
            "| Comparison | Component | Available support | Status | Delta Brier (90% CI) | Delta log loss (90% CI) |",
            "| --- | --- | ---: | --- | --- | --- |",
        )
    )
    for row in report.incremental_comparisons:
        metrics = {metric.metric: metric for metric in row.metrics}
        brier = metrics["brier_score"]
        log_loss = metrics["log_loss"]
        lines.append(
            f"| {row.comparison} | {row.added_component} | {row.available_support} | "
            f"{row.status} | {render(brier.delta)} "
            f"[{render(brier.ci_90.lower)}, {render(brier.ci_90.upper)}] | "
            f"{render(log_loss.delta)} "
            f"[{render(log_loss.ci_90.lower)}, {render(log_loss.ci_90.upper)}] |"
        )
    lines.extend(
        (
            "",
            "## Calibration",
            "",
            "| Slice | Model | Status | Fit support | Evaluation support | Gate | ECE 90% upper |",
            "| --- | --- | --- | ---: | ---: | --- | ---: |",
        )
    )
    for row in report.calibration:
        lines.append(
            f"| {row.slice_id} | {row.model_kind} | {row.status} | "
            f"{row.fit_support} | {row.evaluation_support} | "
            f"{'pass' if row.gate_passed else 'stop'} | "
            f"{render(row.ece_90_upper)} |"
        )
    lines.extend(
        (
            "",
            "## Default Decision",
            "",
            f"- Model: `{report.default_decision.model_kind or 'none'}`",
            f"- Status: `{report.default_decision.status}`",
            "- Reasons: " + (", ".join(report.default_decision.reasons) or "none"),
        )
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "PREMATCH_BOOTSTRAP_SAMPLES",
    "PREMATCH_BOOTSTRAP_SEED_MATERIAL",
    "PREMATCH_CALIBRATION_BINS",
    "PREMATCH_MIN_INCREMENTAL_SUPPORT",
    "PREMATCH_MODEL_RUN_METRICS_SCHEMA",
    "CoverageDistribution",
    "PairedMetricReport",
    "PrematchBacktestReport",
    "PrematchCalibrationReport",
    "PrematchDefaultDecision",
    "PrematchIncrementalReport",
    "PrematchModelSliceReport",
    "build_prematch_report",
    "prematch_model_run_metrics",
    "report_as_dict",
    "report_as_markdown",
]
