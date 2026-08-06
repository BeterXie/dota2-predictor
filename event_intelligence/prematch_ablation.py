"""Fixed-cohort Draft feature-group ablation for prematch research."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .draft_model import BinaryMetrics, evaluate_binary_predictions
from .prematch_backtest import PrematchBacktestTarget, PrematchCorpus
from .prematch_features import DRAFT_ABLATION_FEATURE_SCHEMAS
from .prematch_model import (
    DEFAULT_L2_REGULARIZATION,
    DEFAULT_MIN_SAMPLES,
    PredictionStatus,
    PrematchTrainingRow,
    fit_prematch_model,
    predict_prematch,
)
from .team_rating_backtest import BootstrapInterval


DRAFT_ABLATION_VERSION = "prematch-draft-ablation-v1"
DRAFT_ABLATION_BOOTSTRAP_SAMPLES = 1_000
DRAFT_ABLATION_SEED = f"{DRAFT_ABLATION_VERSION}:d0-d8"
DRAFT_ABLATION_VARIANTS = (
    ("D0", "Team-only", "team_only"),
    ("D1", "10 semantic values", "draft_ablation_d1_values"),
    ("D2", "hero + role residual", "draft_ablation_d2_hero_role"),
    ("D3", "synergy + counter", "draft_ablation_d3_synergy_counter"),
    ("D4", "scaling", "draft_ablation_d4_scaling"),
    ("D5", "control/save/waveclear/push/farm", "draft_ablation_d5_proxies"),
    ("D6", "values + missing", "draft_ablation_d6_values_missing"),
    (
        "D7",
        "values + support/coverage",
        "draft_ablation_d7_values_support_coverage",
    ),
    ("D8", "current full 40 columns", "team_plus_draft"),
)


@dataclass(frozen=True)
class DraftAblationMetrics:
    support: int
    brier_score: float | None
    log_loss: float | None
    ece: float | None
    auc: float | None
    accuracy: float | None


@dataclass(frozen=True)
class DraftAblationVariantReport:
    variant_id: str
    label: str
    model_kind: str
    feature_count: int
    eligible_targets: int
    predicted: int
    insufficient_evidence: int
    metrics: DraftAblationMetrics


@dataclass(frozen=True)
class DraftAblationComparisonReport:
    comparison: str
    paired_support: int
    brier_delta: float | None
    brier_ci_90: BootstrapInterval
    log_loss_delta: float | None
    log_loss_ci_90: BootstrapInterval
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DraftAblationDecision:
    optimize_prematch: bool
    promising_variants: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DraftAblationReport:
    version: str
    availability_mode: str
    formal_maps: int
    cohort_match_ids_hash: str
    bootstrap_samples: int
    variants: tuple[DraftAblationVariantReport, ...]
    comparisons: tuple[DraftAblationComparisonReport, ...]
    decision: DraftAblationDecision


@dataclass(frozen=True)
class _Point:
    match_id: int
    series_id: int | None
    outcome: bool
    probability: float


def _series_key(series_id: int | None, match_id: int) -> str:
    return f"series:{series_id}" if series_id is not None else f"match:{match_id}"


def _training_row(
    target: PrematchBacktestTarget,
    model_kind: str,
) -> PrematchTrainingRow:
    if target.snapshot is None or target.patch_id is None:
        raise ValueError("Draft ablation rows require an available snapshot")
    return PrematchTrainingRow.from_snapshot(
        target.snapshot,
        model_kind=model_kind,
        completed_at=target.completed_at,
        result_usable_at=target.result_usable_at,
        outcome=target.outcome,
        series_id=_series_key(target.series_id, target.match_id),
        event_id=target.event_id,
        patch_id=target.patch_id,
    )


def _metrics(points: Sequence[_Point]) -> DraftAblationMetrics:
    metrics: BinaryMetrics = evaluate_binary_predictions(
        (point.outcome for point in points),
        (point.probability for point in points),
        ece_bins=5,
    )
    return DraftAblationMetrics(
        support=metrics.support,
        brier_score=metrics.brier_score,
        log_loss=metrics.log_loss,
        ece=metrics.expected_calibration_error,
        auc=metrics.auc,
        accuracy=metrics.accuracy,
    )


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _interval(values: Sequence[float]) -> BootstrapInterval:
    return BootstrapInterval(_percentile(values, 0.05), _percentile(values, 0.95))


def _paired_samples(
    points: Sequence[tuple[_Point, _Point]],
    *,
    comparison: str,
    samples: int,
) -> tuple[tuple[tuple[_Point, _Point], ...], ...]:
    if not points:
        return ()
    clusters: dict[str, list[tuple[_Point, _Point]]] = {}
    for pair in points:
        point = pair[0]
        clusters.setdefault(_series_key(point.series_id, point.match_id), []).append(
            pair
        )
    keys = sorted(clusters)
    seed = int(
        hashlib.sha256(f"{DRAFT_ABLATION_SEED}:{comparison}".encode()).hexdigest()[
            :16
        ],
        16,
    )
    generator = random.Random(seed)
    return tuple(
        tuple(
            pair
            for _key in keys
            for pair in clusters[keys[generator.randrange(len(keys))]]
        )
        for _sample in range(samples)
    )


def _loss_delta(pair: tuple[_Point, _Point], metric: str) -> float:
    candidate, baseline = pair
    outcome = float(candidate.outcome)
    if metric == "brier_score":
        return (candidate.probability - outcome) ** 2 - (
            baseline.probability - outcome
        ) ** 2
    if metric == "log_loss":
        candidate_probability = min(
            max(candidate.probability, 1e-15), 1.0 - 1e-15
        )
        baseline_probability = min(
            max(baseline.probability, 1e-15), 1.0 - 1e-15
        )
        candidate_loss = -math.log(
            candidate_probability
            if candidate.outcome
            else 1.0 - candidate_probability
        )
        baseline_loss = -math.log(
            baseline_probability
            if baseline.outcome
            else 1.0 - baseline_probability
        )
        return candidate_loss - baseline_loss
    raise ValueError("unsupported Draft ablation metric")


def _mean_loss_delta(
    pairs: Sequence[tuple[_Point, _Point]],
    metric: str,
) -> float | None:
    if not pairs:
        return None
    return math.fsum(_loss_delta(pair, metric) for pair in pairs) / len(pairs)


def _comparison(
    variant_id: str,
    candidate: Sequence[_Point],
    baseline: Sequence[_Point],
    *,
    bootstrap_samples: int,
) -> DraftAblationComparisonReport:
    candidate_by_match = {point.match_id: point for point in candidate}
    baseline_by_match = {point.match_id: point for point in baseline}
    pairs: list[tuple[_Point, _Point]] = []
    for match_id in sorted(candidate_by_match.keys() & baseline_by_match.keys()):
        candidate_point = candidate_by_match[match_id]
        baseline_point = baseline_by_match[match_id]
        if (
            candidate_point.series_id != baseline_point.series_id
            or candidate_point.outcome != baseline_point.outcome
        ):
            raise ValueError("paired Draft ablation target identities disagree")
        pairs.append((candidate_point, baseline_point))
    comparison = f"{variant_id}-D0"
    samples = _paired_samples(
        pairs,
        comparison=comparison,
        samples=bootstrap_samples,
    )
    brier_estimates = tuple(
        value
        for sample in samples
        if (value := _mean_loss_delta(sample, "brier_score")) is not None
    )
    log_loss_estimates = tuple(
        value
        for sample in samples
        if (value := _mean_loss_delta(sample, "log_loss")) is not None
    )
    brier_delta = _mean_loss_delta(pairs, "brier_score")
    log_loss_delta = _mean_loss_delta(pairs, "log_loss")
    brier_interval = _interval(brier_estimates)
    log_loss_interval = _interval(log_loss_estimates)
    stable_improvement = any(
        delta is not None
        and delta < 0.0
        and interval.upper is not None
        and interval.upper < 0.0
        for delta, interval in (
            (brier_delta, brier_interval),
            (log_loss_delta, log_loss_interval),
        )
    )
    clearly_worse = any(
        delta is not None
        and delta > 0.0
        and interval.lower is not None
        and interval.lower > 0.0
        for delta, interval in (
            (brier_delta, brier_interval),
            (log_loss_delta, log_loss_interval),
        )
    )
    status = "promising" if stable_improvement and not clearly_worse else "rejected"
    reasons = tuple(
        reason
        for failed, reason in (
            (not stable_improvement, "no_paired_90_ci_below_zero"),
            (clearly_worse, "paired_metric_90_ci_above_zero"),
        )
        if failed
    )
    return DraftAblationComparisonReport(
        comparison=comparison,
        paired_support=len(pairs),
        brier_delta=brier_delta,
        brier_ci_90=brier_interval,
        log_loss_delta=log_loss_delta,
        log_loss_ci_90=log_loss_interval,
        status=status,
        reasons=reasons,
    )


def run_draft_ablation(
    corpus: PrematchCorpus,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
    bootstrap_samples: int = DRAFT_ABLATION_BOOTSTRAP_SAMPLES,
) -> DraftAblationReport:
    """Run D0-D8 on one immutable chronological cohort without persistence."""

    if not isinstance(corpus, PrematchCorpus):
        raise ValueError("corpus must be a PrematchCorpus")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    model_kinds = tuple(model_kind for _variant, _label, model_kind in DRAFT_ABLATION_VARIANTS)
    histories: dict[str, list[PrematchTrainingRow]] = {
        model_kind: [] for model_kind in model_kinds
    }
    points: dict[str, list[_Point]] = {model_kind: [] for model_kind in model_kinds}
    eligible_targets = sum(
        target.snapshot is not None and target.result_usable_at is not None
        for target in corpus.targets
    )
    for target in corpus.targets:
        if target.snapshot is None:
            continue
        for model_kind in model_kinds:
            model = fit_prematch_model(
                histories[model_kind],
                target.prediction_cutoff,
                model_kind=model_kind,
                availability_mode=corpus.availability_mode,
                min_samples=min_samples,
                l2_regularization=l2_regularization,
            )
            prediction = predict_prematch(model, target.snapshot)
            if (
                prediction.status is PredictionStatus.PREDICTED
                and prediction.raw_probability is not None
                and target.result_usable_at is not None
            ):
                points[model_kind].append(
                    _Point(
                        match_id=target.match_id,
                        series_id=target.series_id,
                        outcome=target.outcome,
                        probability=prediction.raw_probability,
                    )
                )
        for model_kind in model_kinds:
            histories[model_kind].append(_training_row(target, model_kind))

    variants = tuple(
        DraftAblationVariantReport(
            variant_id=variant_id,
            label=label,
            model_kind=model_kind,
            feature_count=(
                0
                if model_kind == "team_only"
                else 40
                if model_kind == "team_plus_draft"
                else len(DRAFT_ABLATION_FEATURE_SCHEMAS[model_kind])
            ),
            eligible_targets=eligible_targets,
            predicted=len(points[model_kind]),
            insufficient_evidence=max(0, eligible_targets - len(points[model_kind])),
            metrics=_metrics(points[model_kind]),
        )
        for variant_id, label, model_kind in DRAFT_ABLATION_VARIANTS
    )
    baseline = points["team_only"]
    comparisons = tuple(
        _comparison(
            variant_id,
            points[model_kind],
            baseline,
            bootstrap_samples=bootstrap_samples,
        )
        for variant_id, _label, model_kind in DRAFT_ABLATION_VARIANTS[1:]
    )
    promising = tuple(
        comparison.comparison.removesuffix("-D0")
        for comparison in comparisons
        if comparison.status == "promising"
    )
    decision = DraftAblationDecision(
        optimize_prematch=bool(promising),
        promising_variants=promising,
        reasons=(
            ()
            if promising
            else ("no_Draft_feature_group_has_stable_incremental_value",)
        ),
    )
    cohort_hash = hashlib.sha256(
        ",".join(str(target.match_id) for target in corpus.targets).encode()
    ).hexdigest()
    return DraftAblationReport(
        version=DRAFT_ABLATION_VERSION,
        availability_mode=corpus.availability_mode,
        formal_maps=corpus.formal_maps,
        cohort_match_ids_hash=cohort_hash,
        bootstrap_samples=bootstrap_samples,
        variants=variants,
        comparisons=comparisons,
        decision=decision,
    )


def report_as_dict(report: DraftAblationReport) -> dict[str, Any]:
    return asdict(report)


def report_as_markdown(report: DraftAblationReport) -> str:
    def render(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.6f}"

    lines = [
        "# Prematch Draft Ablation",
        "",
        f"- Version: `{report.version}`",
        f"- Formal maps: {report.formal_maps}",
        f"- Cohort hash: `{report.cohort_match_ids_hash}`",
        f"- Bootstrap samples: {report.bootstrap_samples}",
        f"- Optimize Prematch: `{str(report.decision.optimize_prematch).lower()}`",
        "",
        "| Variant | Features | Predicted | Brier | Log loss | ECE | AUC |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.variants:
        lines.append(
            f"| {row.variant_id} {row.label} | {row.feature_count} | {row.predicted} | "
            f"{render(row.metrics.brier_score)} | {render(row.metrics.log_loss)} | "
            f"{render(row.metrics.ece)} | {render(row.metrics.auc)} |"
        )
    lines.extend(
        (
            "",
            "## Paired Against D0",
            "",
            "| Comparison | Support | Brier delta (90% CI) | Log loss delta "
            "(90% CI) | Status |",
            "| --- | ---: | --- | --- | --- |",
        )
    )
    for row in report.comparisons:
        lines.append(
            f"| {row.comparison} | {row.paired_support} | "
            f"{render(row.brier_delta)} "
            f"[{render(row.brier_ci_90.lower)}, {render(row.brier_ci_90.upper)}] | "
            f"{render(row.log_loss_delta)} "
            f"[{render(row.log_loss_ci_90.lower)}, {render(row.log_loss_ci_90.upper)}] | "
            f"{row.status} |"
        )
    if report.decision.reasons:
        lines.extend(("", "## Stop Reasons", ""))
        lines.extend(f"- `{reason}`" for reason in report.decision.reasons)
    return "\n".join(lines) + "\n"


__all__ = [
    "DRAFT_ABLATION_BOOTSTRAP_SAMPLES",
    "DRAFT_ABLATION_VARIANTS",
    "DRAFT_ABLATION_VERSION",
    "DraftAblationComparisonReport",
    "DraftAblationDecision",
    "DraftAblationMetrics",
    "DraftAblationReport",
    "DraftAblationVariantReport",
    "report_as_dict",
    "report_as_markdown",
    "run_draft_ablation",
]
