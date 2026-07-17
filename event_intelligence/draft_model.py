"""Deterministic, explainable logistic models for draft landmarks."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from numbers import Real
from typing import Any, Iterable, Mapping

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score


MODEL_VERSION = "draft-logistic-l2-v1"
FEATURE_SCHEMA_VERSION = "draft-feature-schema-v1"
LANDMARK_MINUTES = (10, 20, 30, 40, 50)
DEFAULT_MIN_SAMPLES = 20
DEFAULT_L2_REGULARIZATION = 1.0
DEFAULT_CLASSIFICATION_THRESHOLD = 0.5
DEFAULT_ECE_BINS = 5
CALIBRATION_MIN_SUPPORT = 100
CALIBRATION_MAX_BRIER = 0.25
CALIBRATION_MAX_LOG_LOSS = math.log(2.0)
CALIBRATION_MAX_ECE = 0.10
CALIBRATION_MAX_ECE_UPPER = 0.15
_SOLVER = "lbfgs"
_MAX_ITERATIONS = 2_000
_TOLERANCE = 1e-10


class ModelStatus(str, Enum):
    TRAINED = "trained"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class PredictionStatus(str, Enum):
    PREDICTED = "predicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value, field="datetime").isoformat()


def _finite_float(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


def _binary_outcome(value: object) -> int:
    if isinstance(value, (bool, np.bool_)):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        result = float(value)
        if math.isfinite(result) and result in (0.0, 1.0):
            return int(result)
    raise ValueError("outcome must be 0 or 1")


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _sha256_digest(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
    ):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


@dataclass(frozen=True)
class FeatureSchema:
    """A canonical feature-name contract independent of mapping insertion order."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        raw_names = tuple(self.names)
        if not raw_names:
            raise ValueError("feature schema cannot be empty")
        if any(not isinstance(name, str) or not name.strip() for name in raw_names):
            raise ValueError("feature names must be non-empty strings")
        if len(set(raw_names)) != len(raw_names):
            raise ValueError("feature schema contains duplicate names")
        object.__setattr__(self, "names", tuple(sorted(raw_names)))

    @classmethod
    def from_names(cls, names: Iterable[str]) -> FeatureSchema:
        return cls(tuple(names))

    @property
    def schema_hash(self) -> str:
        return _sha256(
            {"version": FEATURE_SCHEMA_VERSION, "feature_names": self.names}
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": FEATURE_SCHEMA_VERSION,
            "feature_names": list(self.names),
            "feature_schema_hash": self.schema_hash,
        }


FeatureValue = float | int | None


@dataclass(frozen=True)
class DraftTrainingRow:
    """One settled map offered to a landmark model training invocation."""

    match_id: int
    input_snapshot_hash: str
    cutoff: datetime
    completed_at: datetime
    result_usable_at: datetime | None
    outcome: int | bool
    duration_minutes: float
    series_id: str | int
    features: Mapping[str, FeatureValue]


@dataclass(frozen=True)
class DraftModelArtifact:
    model_version: str
    model_kind: str
    status: ModelStatus
    reason: str | None
    horizon_minutes: int
    training_cutoff: datetime
    support: int
    series_support: int
    min_samples: int
    l2_regularization: float
    feature_names: tuple[str, ...]
    feature_schema_hash: str
    training_input_hash: str
    class_counts: tuple[tuple[int, int], ...]
    missing_counts: tuple[tuple[str, int], ...]
    imputation_values: tuple[tuple[str, float], ...]
    standardization_means: tuple[tuple[str, float], ...]
    standardization_scales: tuple[tuple[str, float], ...]
    coefficients: tuple[tuple[str, float], ...]
    intercept: float | None
    logit_covariance: tuple[tuple[float, ...], ...]
    model_hash: str

    def to_payload(self, *, include_model_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model_version": self.model_version,
            "model_kind": self.model_kind,
            "status": self.status.value,
            "reason": self.reason,
            "horizon_minutes": self.horizon_minutes,
            "training_cutoff": _iso(self.training_cutoff),
            "support": self.support,
            "series_support": self.series_support,
            "min_samples": self.min_samples,
            "l2_regularization": self.l2_regularization,
            "solver": _SOLVER,
            "max_iterations": _MAX_ITERATIONS,
            "tolerance": _TOLERANCE,
            "feature_names": list(self.feature_names),
            "feature_schema_hash": self.feature_schema_hash,
            "training_input_hash": self.training_input_hash,
            "class_counts": {str(key): count for key, count in self.class_counts},
            "missing_counts": dict(self.missing_counts),
            "imputation_values": dict(self.imputation_values),
            "standardization_means": dict(self.standardization_means),
            "standardization_scales": dict(self.standardization_scales),
            "coefficients": dict(self.coefficients),
            "intercept": self.intercept,
            "logit_covariance": [list(row) for row in self.logit_covariance],
        }
        if include_model_hash:
            payload["model_hash"] = self.model_hash
        return payload

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.to_payload())


@dataclass(frozen=True)
class FeatureContribution:
    feature_name: str
    input_value: float
    standardized_value: float
    coefficient: float
    log_odds_contribution: float
    was_imputed: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "feature_name": self.feature_name,
            "input_value": self.input_value,
            "standardized_value": self.standardized_value,
            "coefficient": self.coefficient,
            "log_odds_contribution": self.log_odds_contribution,
            "was_imputed": self.was_imputed,
        }


@dataclass(frozen=True)
class DraftPrediction:
    status: PredictionStatus
    reason: str | None
    probability: float | None
    uncertainty: float | None
    support: int
    model_hash: str
    input_snapshot_hash: str
    missing_features: tuple[str, ...]
    imputed_values: tuple[tuple[str, float], ...]
    top_contributions: tuple[FeatureContribution, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "probability": self.probability,
            "uncertainty": self.uncertainty,
            "support": self.support,
            "model_hash": self.model_hash,
            "input_snapshot_hash": self.input_snapshot_hash,
            "missing_features": list(self.missing_features),
            "imputed_values": dict(self.imputed_values),
            "top_contributions": [
                contribution.to_payload()
                for contribution in self.top_contributions
            ],
        }


def _align_features(
    schema: FeatureSchema,
    features: Mapping[str, FeatureValue],
) -> tuple[tuple[float | None, ...], tuple[str, ...]]:
    if not isinstance(features, Mapping):
        raise ValueError("features must be a mapping")
    invalid_keys = tuple(key for key in features if not isinstance(key, str))
    if invalid_keys:
        raise ValueError("feature names must be strings")
    unknown = tuple(sorted(set(features) - set(schema.names)))
    if unknown:
        raise ValueError(f"features outside fixed schema: {', '.join(unknown)}")

    values: list[float | None] = []
    missing: list[str] = []
    for name in schema.names:
        raw = features.get(name)
        if raw is None:
            values.append(None)
            missing.append(name)
            continue
        values.append(_finite_float(raw, field=f"feature {name!r}"))
    return tuple(values), tuple(missing)


PreparedTrainingRow = tuple[
    DraftTrainingRow, tuple[float | None, ...], tuple[str, ...]
]


def _prepared_row_payload(
    prepared: PreparedTrainingRow, schema: FeatureSchema
) -> dict[str, Any]:
    row, values, missing = prepared
    return {
        "match_id": row.match_id,
        "input_snapshot_hash": row.input_snapshot_hash,
        "cutoff": _iso(row.cutoff),
        "completed_at": _iso(row.completed_at),
        "result_usable_at": (
            None if row.result_usable_at is None else _iso(row.result_usable_at)
        ),
        "outcome": _binary_outcome(row.outcome),
        "duration_minutes": float(row.duration_minutes),
        "series_id": str(row.series_id),
        "features": dict(zip(schema.names, values, strict=True)),
        "missing_features": list(missing),
    }


def _training_input_hash(
    prepared: Iterable[PreparedTrainingRow],
    schema: FeatureSchema,
    horizon_minutes: int,
) -> str:
    rows = [_prepared_row_payload(row, schema) for row in prepared]
    return _sha256(
        {
            "horizon_minutes": horizon_minutes,
            "feature_schema_hash": schema.schema_hash,
            "rows": rows,
        }
    )


def _empty_column_statistics(
    schema: FeatureSchema,
    support: int,
) -> tuple[
    tuple[tuple[str, int], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    return (
        tuple((name, support) for name in schema.names),
        tuple((name, 0.0) for name in schema.names),
        tuple((name, 0.0) for name in schema.names),
        tuple((name, 1.0) for name in schema.names),
    )


def _column_statistics(
    prepared: tuple[
        tuple[DraftTrainingRow, tuple[float | None, ...], tuple[str, ...]], ...
    ],
    schema: FeatureSchema,
) -> tuple[
    np.ndarray,
    tuple[tuple[str, int], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
    tuple[tuple[str, float], ...],
]:
    support = len(prepared)
    if support == 0:
        missing, imputation, means, scales = _empty_column_statistics(schema, 0)
        return np.empty((0, len(schema.names))), missing, imputation, means, scales

    raw_columns = tuple(zip(*(values for _, values, _ in prepared), strict=True))
    matrix = np.empty((support, len(schema.names)), dtype=np.float64)
    missing_counts: list[tuple[str, int]] = []
    imputation_values: list[tuple[str, float]] = []
    means: list[tuple[str, float]] = []
    scales: list[tuple[str, float]] = []

    for index, (name, column) in enumerate(zip(schema.names, raw_columns, strict=True)):
        observed = tuple(value for value in column if value is not None)
        imputation = math.fsum(observed) / len(observed) if observed else 0.0
        complete = np.asarray(
            [imputation if value is None else value for value in column],
            dtype=np.float64,
        )
        mean = float(np.mean(complete))
        scale = float(np.sqrt(np.mean(np.square(complete - mean))))
        if scale <= 1e-12:
            scale = 1.0
        matrix[:, index] = (complete - mean) / scale
        missing_counts.append((name, support - len(observed)))
        imputation_values.append((name, imputation))
        means.append((name, mean))
        scales.append((name, scale))

    return (
        matrix,
        tuple(missing_counts),
        tuple(imputation_values),
        tuple(means),
        tuple(scales),
    )


def _artifact(
    *,
    model_kind: str,
    status: ModelStatus,
    reason: str | None,
    horizon_minutes: int,
    training_cutoff: datetime,
    support: int,
    series_support: int,
    min_samples: int,
    l2_regularization: float,
    schema: FeatureSchema,
    training_input_hash: str,
    class_counts: tuple[tuple[int, int], ...],
    missing_counts: tuple[tuple[str, int], ...],
    imputation_values: tuple[tuple[str, float], ...],
    means: tuple[tuple[str, float], ...],
    scales: tuple[tuple[str, float], ...],
    coefficients: tuple[tuple[str, float], ...] = (),
    intercept: float | None = None,
    covariance: tuple[tuple[float, ...], ...] = (),
) -> DraftModelArtifact:
    artifact = DraftModelArtifact(
        model_version=MODEL_VERSION,
        model_kind=model_kind,
        status=status,
        reason=reason,
        horizon_minutes=horizon_minutes,
        training_cutoff=training_cutoff,
        support=support,
        series_support=series_support,
        min_samples=min_samples,
        l2_regularization=l2_regularization,
        feature_names=schema.names,
        feature_schema_hash=schema.schema_hash,
        training_input_hash=training_input_hash,
        class_counts=class_counts,
        missing_counts=missing_counts,
        imputation_values=imputation_values,
        standardization_means=means,
        standardization_scales=scales,
        coefficients=coefficients,
        intercept=intercept,
        logit_covariance=covariance,
        model_hash="",
    )
    return replace(
        artifact,
        model_hash=_sha256(artifact.to_payload(include_model_hash=False)),
    )


def fit_draft_model(
    rows: Iterable[DraftTrainingRow],
    schema: FeatureSchema,
    training_cutoff: datetime,
    horizon_minutes: int,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    model_kind: str = "pure_draft",
    l2_regularization: float = DEFAULT_L2_REGULARIZATION,
) -> DraftModelArtifact:
    """Fit only rows whose result was available before ``training_cutoff``."""
    if horizon_minutes not in LANDMARK_MINUTES:
        raise ValueError(f"horizon_minutes must be one of {LANDMARK_MINUTES}")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 2:
        raise ValueError("min_samples must be an integer of at least 2")
    if model_kind not in {"pure_draft", "context_adjusted"}:
        raise ValueError("model_kind must be pure_draft or context_adjusted")
    regularization = _finite_float(
        l2_regularization, field="l2_regularization"
    )
    if regularization <= 0.0:
        raise ValueError("l2_regularization must be positive")
    cutoff = _utc(training_cutoff, field="training_cutoff")

    prepared_by_match: dict[int, PreparedTrainingRow] = {}
    for row in rows:
        if not isinstance(row, DraftTrainingRow):
            raise ValueError("rows must contain DraftTrainingRow values")
        row_cutoff = _utc(row.cutoff, field="row cutoff")
        if row_cutoff >= cutoff:
            continue
        completed_at = _utc(row.completed_at, field="row completed_at")
        if completed_at >= cutoff:
            continue
        if row.result_usable_at is None:
            continue
        result_usable_at = _utc(
            row.result_usable_at, field="row result_usable_at"
        )
        if result_usable_at > cutoff:
            continue
        if completed_at < row_cutoff:
            raise ValueError("row completed_at cannot precede its prediction cutoff")
        if result_usable_at < completed_at:
            raise ValueError("row result_usable_at cannot precede completed_at")
        duration = _finite_float(row.duration_minutes, field="duration_minutes")
        if duration <= 0.0:
            raise ValueError("duration_minutes must be positive")
        if duration <= horizon_minutes:
            continue
        match_id = _positive_integer(row.match_id, field="match_id")
        snapshot_hash = _sha256_digest(
            row.input_snapshot_hash, field="input_snapshot_hash"
        )
        outcome = _binary_outcome(row.outcome)
        series_id = str(row.series_id).strip()
        if not series_id:
            raise ValueError("series_id cannot be empty")
        values, missing = _align_features(schema, row.features)
        normalized = replace(
            row,
            match_id=match_id,
            input_snapshot_hash=snapshot_hash,
            cutoff=row_cutoff,
            completed_at=completed_at,
            result_usable_at=result_usable_at,
            outcome=outcome,
            duration_minutes=duration,
            series_id=series_id,
        )
        candidate = (normalized, values, missing)
        existing = prepared_by_match.get(match_id)
        if existing is not None:
            if _prepared_row_payload(existing, schema) != _prepared_row_payload(
                candidate, schema
            ):
                raise ValueError(f"conflicting training rows for match {match_id}")
            continue
        prepared_by_match[match_id] = candidate

    prepared = tuple(
        sorted(
            prepared_by_match.values(),
            key=lambda item: (
                item[0].cutoff,
                item[0].completed_at,
                item[0].result_usable_at,
                item[0].match_id,
                str(item[0].series_id),
            ),
        )
    )
    support = len(prepared)
    series_support = len({str(row.series_id) for row, _, _ in prepared})
    counts = {0: 0, 1: 0}
    for row, _, _ in prepared:
        counts[int(row.outcome)] += 1
    class_counts = tuple(sorted(counts.items()))
    input_hash = _training_input_hash(prepared, schema, horizon_minutes)
    matrix, missing_counts, imputation, means, scales = _column_statistics(
        prepared, schema
    )

    common = {
        "model_kind": model_kind,
        "horizon_minutes": horizon_minutes,
        "training_cutoff": cutoff,
        "support": support,
        "series_support": series_support,
        "min_samples": min_samples,
        "l2_regularization": regularization,
        "schema": schema,
        "training_input_hash": input_hash,
        "class_counts": class_counts,
        "missing_counts": missing_counts,
        "imputation_values": imputation,
        "means": means,
        "scales": scales,
    }
    if support < min_samples:
        return _artifact(
            **common,
            status=ModelStatus.INSUFFICIENT_EVIDENCE,
            reason="support_below_minimum",
        )
    if not counts[0] or not counts[1]:
        return _artifact(
            **common,
            status=ModelStatus.INSUFFICIENT_EVIDENCE,
            reason="single_class_training_data",
        )

    outcomes = np.asarray(
        [int(row.outcome) for row, _, _ in prepared], dtype=np.int64
    )
    estimator = LogisticRegression(
        C=1.0 / regularization,
        solver=_SOLVER,
        fit_intercept=True,
        max_iter=_MAX_ITERATIONS,
        tol=_TOLERANCE,
        random_state=0,
    )
    estimator.fit(matrix, outcomes)
    coefficient_values = tuple(float(value) for value in estimator.coef_[0])
    intercept = float(estimator.intercept_[0])

    fitted_probabilities = estimator.predict_proba(matrix)[:, 1]
    augmented = np.column_stack((np.ones(support), matrix))
    weights = fitted_probabilities * (1.0 - fitted_probabilities)
    hessian = augmented.T @ (augmented * weights[:, None])
    hessian[1:, 1:] += regularization * np.eye(len(schema.names))
    covariance_array = np.linalg.pinv(hessian, hermitian=True)
    covariance_array = (covariance_array + covariance_array.T) / 2.0
    covariance = tuple(
        tuple(float(value) for value in covariance_row)
        for covariance_row in covariance_array
    )

    return _artifact(
        **common,
        status=ModelStatus.TRAINED,
        reason=None,
        coefficients=tuple(zip(schema.names, coefficient_values, strict=True)),
        intercept=intercept,
        covariance=covariance,
    )


def _sigmoid(logit: float) -> float:
    if logit >= 0.0:
        return 1.0 / (1.0 + math.exp(-logit))
    exp_logit = math.exp(logit)
    return exp_logit / (1.0 + exp_logit)


def _verify_model_artifact(model: DraftModelArtifact) -> None:
    if not isinstance(model, DraftModelArtifact):
        raise ValueError("model must be a DraftModelArtifact")
    claimed = _sha256_digest(model.model_hash, field="model_hash")
    expected = _sha256(model.to_payload(include_model_hash=False))
    if model.model_hash != claimed or not hmac.compare_digest(claimed, expected):
        raise ValueError("model artifact hash does not match its parameters")


def predict_draft(
    model: DraftModelArtifact,
    features: Mapping[str, FeatureValue],
    *,
    top_n: int = 5,
) -> DraftPrediction:
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 0:
        raise ValueError("top_n must be a non-negative integer")
    _verify_model_artifact(model)
    schema = FeatureSchema(model.feature_names)
    if schema.schema_hash != model.feature_schema_hash:
        raise ValueError("model feature schema hash does not match feature names")
    values, missing = _align_features(schema, features)
    input_hash = _sha256(
        {
            "model_hash": model.model_hash,
            "feature_schema_hash": model.feature_schema_hash,
            "features": dict(zip(schema.names, values, strict=True)),
            "missing_features": missing,
        }
    )
    imputation = dict(model.imputation_values)
    imputed = tuple((name, imputation[name]) for name in missing)

    if model.status is not ModelStatus.TRAINED:
        return DraftPrediction(
            status=PredictionStatus.INSUFFICIENT_EVIDENCE,
            reason=model.reason,
            probability=None,
            uncertainty=None,
            support=model.support,
            model_hash=model.model_hash,
            input_snapshot_hash=input_hash,
            missing_features=missing,
            imputed_values=imputed,
            top_contributions=(),
        )

    coefficients = dict(model.coefficients)
    means = dict(model.standardization_means)
    scales = dict(model.standardization_scales)
    if model.intercept is None or set(coefficients) != set(schema.names):
        raise ValueError("trained model parameters are incomplete")

    contributions: list[FeatureContribution] = []
    standardized_values: list[float] = []
    for name, raw in zip(schema.names, values, strict=True):
        was_imputed = raw is None
        value = imputation[name] if was_imputed else raw
        if value is None:
            raise ValueError("model imputation parameters are incomplete")
        scale = scales[name]
        if not math.isfinite(scale) or scale <= 0.0:
            raise ValueError("model standardization scale must be positive")
        standardized = (value - means[name]) / scale
        contribution = coefficients[name] * standardized
        standardized_values.append(standardized)
        contributions.append(
            FeatureContribution(
                feature_name=name,
                input_value=value,
                standardized_value=standardized,
                coefficient=coefficients[name],
                log_odds_contribution=contribution,
                was_imputed=was_imputed,
            )
        )

    logit = model.intercept + math.fsum(
        contribution.log_odds_contribution for contribution in contributions
    )
    probability = _sigmoid(logit)
    covariance = np.asarray(model.logit_covariance, dtype=np.float64)
    expected_shape = (len(schema.names) + 1, len(schema.names) + 1)
    if covariance.shape != expected_shape or not np.all(np.isfinite(covariance)):
        raise ValueError("model covariance is incomplete")
    design = np.asarray((1.0, *standardized_values), dtype=np.float64)
    logit_variance = max(0.0, float(design @ covariance @ design))
    uncertainty = min(
        0.5,
        probability * (1.0 - probability) * math.sqrt(logit_variance),
    )
    ranked = tuple(
        sorted(
            contributions,
            key=lambda row: (-abs(row.log_odds_contribution), row.feature_name),
        )[:top_n]
    )
    return DraftPrediction(
        status=PredictionStatus.PREDICTED,
        reason=None,
        probability=probability,
        uncertainty=uncertainty,
        support=model.support,
        model_hash=model.model_hash,
        input_snapshot_hash=input_hash,
        missing_features=missing,
        imputed_values=imputed,
        top_contributions=ranked,
    )


@dataclass(frozen=True)
class CalibrationBin:
    bin_number: int
    count: int
    min_probability: float
    max_probability: float
    mean_probability: float
    event_rate: float
    absolute_gap: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "bin_number": self.bin_number,
            "count": self.count,
            "min_probability": self.min_probability,
            "max_probability": self.max_probability,
            "mean_probability": self.mean_probability,
            "event_rate": self.event_rate,
            "absolute_gap": self.absolute_gap,
        }


@dataclass(frozen=True)
class BinaryMetrics:
    support: int
    brier_score: float | None
    log_loss: float | None
    expected_calibration_error: float | None
    auc: float | None
    accuracy: float | None
    classification_threshold: float
    calibration_bins: tuple[CalibrationBin, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "support": self.support,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "expected_calibration_error": self.expected_calibration_error,
            "auc": self.auc,
            "accuracy": self.accuracy,
            "classification_threshold": self.classification_threshold,
            "calibration_bins": [row.to_payload() for row in self.calibration_bins],
        }


def _equal_count_calibration_bins(
    outcomes: tuple[int, ...],
    probabilities: tuple[float, ...],
    requested_bins: int,
) -> tuple[CalibrationBin, ...]:
    """Partition unique probabilities without splitting ties between bins."""
    grouped: dict[float, list[int]] = {}
    for outcome, probability in zip(outcomes, probabilities, strict=True):
        aggregate = grouped.setdefault(probability, [0, 0])
        aggregate[0] += 1
        aggregate[1] += outcome
    groups = tuple(
        (probability, values[0], values[1])
        for probability, values in sorted(grouped.items())
    )
    bin_count = min(requested_bins, len(groups))
    cumulative: list[int] = []
    running = 0
    for _, count, _ in groups:
        running += count
        cumulative.append(running)

    boundaries: list[int] = []
    start = 0
    support = len(outcomes)
    for bin_number in range(1, bin_count):
        last_allowed = len(groups) - (bin_count - bin_number) - 1
        target_count = bin_number * support / bin_count
        boundary = min(
            range(start, last_allowed + 1),
            key=lambda index: (abs(cumulative[index] - target_count), index),
        )
        boundaries.append(boundary)
        start = boundary + 1

    result: list[CalibrationBin] = []
    start = 0
    for bin_number, boundary in enumerate((*boundaries, len(groups) - 1), start=1):
        selected = groups[start : boundary + 1]
        count = sum(row[1] for row in selected)
        outcome_count = sum(row[2] for row in selected)
        mean_probability = (
            math.fsum(probability * group_count for probability, group_count, _ in selected)
            / count
        )
        event_rate = outcome_count / count
        result.append(
            CalibrationBin(
                bin_number=bin_number,
                count=count,
                min_probability=selected[0][0],
                max_probability=selected[-1][0],
                mean_probability=mean_probability,
                event_rate=event_rate,
                absolute_gap=abs(mean_probability - event_rate),
            )
        )
        start = boundary + 1
    return tuple(result)


def evaluate_binary_predictions(
    outcomes: Iterable[int | bool],
    probabilities: Iterable[float],
    *,
    classification_threshold: float = DEFAULT_CLASSIFICATION_THRESHOLD,
    ece_bins: int = DEFAULT_ECE_BINS,
) -> BinaryMetrics:
    threshold = _finite_float(
        classification_threshold, field="classification_threshold"
    )
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("classification_threshold must be between 0 and 1")
    if isinstance(ece_bins, bool) or not isinstance(ece_bins, int) or ece_bins < 1:
        raise ValueError("ece_bins must be a positive integer")
    outcome_values = tuple(_binary_outcome(value) for value in outcomes)
    probability_values = tuple(
        _finite_float(value, field="probability") for value in probabilities
    )
    if len(outcome_values) != len(probability_values):
        raise ValueError("outcomes and probabilities must have equal length")
    if any(not 0.0 <= value <= 1.0 for value in probability_values):
        raise ValueError("probabilities must be between 0 and 1")
    support = len(outcome_values)
    if support == 0:
        return BinaryMetrics(support, None, None, None, None, None, threshold, ())

    observed = np.asarray(outcome_values, dtype=np.float64)
    predicted = np.asarray(probability_values, dtype=np.float64)
    brier = float(np.mean(np.square(predicted - observed)))
    clipped = np.clip(predicted, 1e-15, 1.0 - 1e-15)
    log_loss = float(
        -np.mean(observed * np.log(clipped) + (1.0 - observed) * np.log1p(-clipped))
    )
    accuracy = float(np.mean((predicted >= threshold) == observed))
    auc = (
        float(roc_auc_score(observed, predicted))
        if len(set(outcome_values)) == 2
        else None
    )

    calibration = _equal_count_calibration_bins(
        outcome_values, probability_values, ece_bins
    )
    ece = math.fsum(row.count * row.absolute_gap for row in calibration) / support
    return BinaryMetrics(
        support=support,
        brier_score=brier,
        log_loss=log_loss,
        expected_calibration_error=ece,
        auc=auc,
        accuracy=accuracy,
        classification_threshold=threshold,
        calibration_bins=calibration,
    )


@dataclass(frozen=True)
class CalibrationGate:
    passed: bool
    reasons: tuple[str, ...]
    ece_upper_bound: float | None


def _in_range(value: object, low: float, high: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    number = float(value)
    if not math.isfinite(number) or number < low:
        return False
    return high is None or number <= high


def _calibration_structure_valid(metrics: BinaryMetrics) -> bool:
    bins = metrics.calibration_bins
    if len(bins) != DEFAULT_ECE_BINS:
        return False
    if tuple(row.bin_number for row in bins) != tuple(
        range(1, DEFAULT_ECE_BINS + 1)
    ):
        return False
    if any(
        isinstance(row.count, bool)
        or not isinstance(row.count, int)
        or row.count <= 0
        or not _in_range(row.min_probability, 0.0, 1.0)
        or not _in_range(row.max_probability, 0.0, 1.0)
        or not _in_range(row.mean_probability, 0.0, 1.0)
        or not _in_range(row.event_rate, 0.0, 1.0)
        or not _in_range(row.absolute_gap, 0.0, 1.0)
        or (
            row.min_probability > row.mean_probability
            and not math.isclose(
                row.min_probability,
                row.mean_probability,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or (
            row.mean_probability > row.max_probability
            and not math.isclose(
                row.mean_probability,
                row.max_probability,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        or not math.isclose(
            row.absolute_gap,
            abs(row.mean_probability - row.event_rate),
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for row in bins
    ):
        return False
    if any(first.max_probability >= second.min_probability for first, second in zip(bins, bins[1:])):
        return False
    if sum(row.count for row in bins) != metrics.support:
        return False
    if metrics.expected_calibration_error is None:
        return False
    calculated = (
        math.fsum(row.count * row.absolute_gap for row in bins) / metrics.support
        if metrics.support > 0
        else None
    )
    return calculated is not None and math.isclose(
        calculated,
        metrics.expected_calibration_error,
        rel_tol=0.0,
        abs_tol=1e-12,
    )


def passes_calibration_gate(
    metrics: BinaryMetrics,
    *,
    ece_upper_bound: float | None,
) -> CalibrationGate:
    if not isinstance(metrics, BinaryMetrics):
        raise ValueError("metrics must be BinaryMetrics")
    reasons: list[str] = []
    if (
        isinstance(metrics.support, bool)
        or not isinstance(metrics.support, int)
        or metrics.support < 0
    ):
        reasons.append("support_invalid")
    elif metrics.support < CALIBRATION_MIN_SUPPORT:
        reasons.append("support_below_100")
    if not _in_range(metrics.classification_threshold, 0.0, 1.0):
        reasons.append("classification_threshold_invalid")
    if not _in_range(metrics.brier_score, 0.0, 1.0):
        reasons.append("brier_out_of_range")
    elif metrics.brier_score >= CALIBRATION_MAX_BRIER:
        reasons.append("brier_not_below_0.25")
    if not _in_range(metrics.log_loss, 0.0):
        reasons.append("log_loss_out_of_range")
    elif metrics.log_loss >= CALIBRATION_MAX_LOG_LOSS:
        reasons.append("log_loss_not_below_ln2")
    if not _in_range(metrics.expected_calibration_error, 0.0, 1.0):
        reasons.append("ece_out_of_range")
    elif metrics.expected_calibration_error > CALIBRATION_MAX_ECE:
        reasons.append("ece_above_0.10")
    if not _calibration_structure_valid(metrics):
        reasons.append("calibration_bins_not_valid_five_bin_ece")
    if metrics.auc is not None and not _in_range(metrics.auc, 0.0, 1.0):
        reasons.append("auc_out_of_range")
    if metrics.accuracy is not None and not _in_range(metrics.accuracy, 0.0, 1.0):
        reasons.append("accuracy_out_of_range")
    if ece_upper_bound is None:
        reasons.append("ece_upper_bound_missing")
        upper = None
    else:
        upper = _finite_float(ece_upper_bound, field="ece_upper_bound")
        if not 0.0 <= upper <= 1.0:
            reasons.append("ece_upper_bound_out_of_range")
        else:
            if (
                _in_range(metrics.expected_calibration_error, 0.0, 1.0)
                and upper < float(metrics.expected_calibration_error)
            ):
                reasons.append("ece_upper_bound_below_point_estimate")
            if upper > CALIBRATION_MAX_ECE_UPPER:
                reasons.append("ece_upper_bound_above_0.15")
    return CalibrationGate(not reasons, tuple(reasons), upper)


@dataclass(frozen=True)
class LandmarkCandidate:
    minute: int
    validated: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.minute, bool)
            or not isinstance(self.minute, int)
            or self.minute not in LANDMARK_MINUTES
        ):
            raise ValueError(f"candidate minute must be one of {LANDMARK_MINUTES}")
        if not isinstance(self.validated, bool):
            raise ValueError("candidate validated must be boolean")


@dataclass(frozen=True)
class LandmarkSelection:
    status: str
    landmark_minute: int | None
    reason: str | None


def select_live_landmark(
    current_minute: float,
    candidates: Iterable[int | LandmarkCandidate],
    *,
    max_age_minutes: float = 10.0,
) -> LandmarkSelection:
    """Choose a validated past landmark without borrowing from the future."""
    minute = _finite_float(current_minute, field="current_minute")
    max_age = _finite_float(max_age_minutes, field="max_age_minutes")
    if minute < 0.0:
        raise ValueError("current_minute cannot be negative")
    if max_age < 0.0:
        raise ValueError("max_age_minutes cannot be negative")
    validated: set[int] = set()
    for value in candidates:
        candidate = value if isinstance(value, LandmarkCandidate) else LandmarkCandidate(value)
        if candidate.validated:
            validated.add(candidate.minute)

    if minute < LANDMARK_MINUTES[0]:
        return LandmarkSelection("wait", None, "before_first_landmark")
    past = tuple(value for value in validated if value <= minute)
    if not past:
        return LandmarkSelection("wait", None, "no_validated_past_landmark")
    selected = max(past)
    if minute - selected > max_age:
        return LandmarkSelection("wait", None, "validated_landmark_stale")
    return LandmarkSelection("selected", selected, None)
