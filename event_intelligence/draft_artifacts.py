"""Immutable, self-verifying deployment artifacts for draft predictions."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression

from .draft_model import (
    DEFAULT_ECE_BINS,
    LANDMARK_MINUTES,
    MODEL_VERSION,
    BinaryMetrics,
    CalibrationBin,
    CalibrationGate,
    DraftModelArtifact,
    ModelStatus,
    _verify_model_artifact,
    evaluate_binary_predictions,
    passes_calibration_gate,
)


CALIBRATION_ARTIFACT_VERSION = "draft-platt-calibration-v1"
CALIBRATION_METHOD = "platt_logit"
CALIBRATION_MIN_FIT_SUPPORT = 20
CALIBRATION_BOOTSTRAP_SAMPLES = 1_000
CALIBRATION_EVIDENCE_MODES = {
    "reconstructed_walk_forward",
    "prospective",
}
_LOGIT_EPSILON = 1e-9
_SOLVER = "lbfgs"
_MAX_ITERATIONS = 2_000
_TOLERANCE = 1e-10


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_hash(payload: object) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO timestamp") from error
    return _aware_utc(parsed, field)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _digest(value: object, field: str) -> str:
    result = str(value)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _nonempty(value: object, field: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    return result


def _named_float_pairs(
    value: object,
    field: str,
) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return tuple(
        sorted(
            (
                _nonempty(name, f"{field} name"),
                _finite(number, f"{field}.{name}"),
            )
            for name, number in value.items()
        )
    )


def model_artifact_from_payload(payload: Mapping[str, Any]) -> DraftModelArtifact:
    """Rehydrate a complete model and verify its canonical model hash."""

    if not isinstance(payload, Mapping):
        raise ValueError("model artifact payload must be an object")
    feature_names_raw = payload.get("feature_names")
    if not isinstance(feature_names_raw, list) or not feature_names_raw:
        raise ValueError("model feature_names must be a non-empty array")
    feature_names = tuple(_nonempty(value, "feature name") for value in feature_names_raw)
    if len(set(feature_names)) != len(feature_names):
        raise ValueError("model feature_names contain duplicates")

    class_counts_raw = payload.get("class_counts")
    if not isinstance(class_counts_raw, Mapping):
        raise ValueError("model class_counts must be an object")
    try:
        class_counts = tuple(
            sorted(
                (
                    int(label),
                    _integer(count, f"class_counts.{label}"),
                )
                for label, count in class_counts_raw.items()
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError("model class_counts are invalid") from error
    if {label for label, _ in class_counts} != {0, 1}:
        raise ValueError("model class_counts must contain classes 0 and 1")

    missing_raw = payload.get("missing_counts")
    if not isinstance(missing_raw, Mapping):
        raise ValueError("model missing_counts must be an object")
    missing_counts = tuple(
        sorted(
            (
                _nonempty(name, "missing feature name"),
                _integer(count, f"missing_counts.{name}"),
            )
            for name, count in missing_raw.items()
        )
    )
    covariance_raw = payload.get("logit_covariance")
    if not isinstance(covariance_raw, list):
        raise ValueError("model logit_covariance must be an array")
    covariance = tuple(
        tuple(_finite(number, "logit covariance") for number in row)
        for row in covariance_raw
        if isinstance(row, list)
    )
    if len(covariance) != len(covariance_raw):
        raise ValueError("model logit_covariance rows must be arrays")

    artifact = DraftModelArtifact(
        model_version=_nonempty(payload.get("model_version"), "model_version"),
        model_kind=_nonempty(payload.get("model_kind"), "model_kind"),
        status=ModelStatus(str(payload.get("status"))),
        reason=(
            None
            if payload.get("reason") is None
            else _nonempty(payload.get("reason"), "model reason")
        ),
        horizon_minutes=_integer(payload.get("horizon_minutes"), "horizon_minutes", minimum=1),
        training_cutoff=_parse_utc(payload.get("training_cutoff"), "training_cutoff"),
        support=_integer(payload.get("support"), "support"),
        series_support=_integer(payload.get("series_support"), "series_support"),
        min_samples=_integer(payload.get("min_samples"), "min_samples", minimum=2),
        l2_regularization=_finite(payload.get("l2_regularization"), "l2_regularization"),
        feature_names=feature_names,
        feature_schema_hash=_digest(payload.get("feature_schema_hash"), "feature_schema_hash"),
        training_input_hash=_digest(payload.get("training_input_hash"), "training_input_hash"),
        class_counts=class_counts,
        missing_counts=missing_counts,
        imputation_values=_named_float_pairs(payload.get("imputation_values"), "imputation_values"),
        standardization_means=_named_float_pairs(
            payload.get("standardization_means"), "standardization_means"
        ),
        standardization_scales=_named_float_pairs(
            payload.get("standardization_scales"), "standardization_scales"
        ),
        coefficients=_named_float_pairs(payload.get("coefficients"), "coefficients"),
        intercept=(
            None
            if payload.get("intercept") is None
            else _finite(payload.get("intercept"), "intercept")
        ),
        logit_covariance=covariance,
        model_hash=_digest(payload.get("model_hash"), "model_hash"),
    )
    if artifact.model_version != MODEL_VERSION:
        raise ValueError("unsupported draft model version")
    if artifact.model_kind not in {"pure_draft", "context_adjusted"}:
        raise ValueError("unsupported draft model kind")
    if artifact.horizon_minutes not in LANDMARK_MINUTES:
        raise ValueError("unsupported draft landmark horizon")
    if artifact.l2_regularization <= 0.0:
        raise ValueError("model l2_regularization must be positive")
    named_fields = (
        artifact.missing_counts,
        artifact.imputation_values,
        artifact.standardization_means,
        artifact.standardization_scales,
    )
    if any(tuple(name for name, _ in rows) != tuple(sorted(feature_names)) for rows in named_fields):
        raise ValueError("model feature parameters do not match feature_names")
    if artifact.status is ModelStatus.TRAINED:
        if tuple(name for name, _ in artifact.coefficients) != tuple(sorted(feature_names)):
            raise ValueError("trained model coefficients do not match feature_names")
        expected = len(feature_names) + 1
        if artifact.intercept is None or len(covariance) != expected or any(
            len(row) != expected for row in covariance
        ):
            raise ValueError("trained model covariance is incomplete")
    _verify_model_artifact(artifact)
    return artifact


@dataclass(frozen=True)
class CalibrationSample:
    sample_id: str
    probability: float
    outcome: int
    observed_at: datetime
    settled_at: datetime
    cluster_id: str
    event_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _nonempty(self.sample_id, "sample_id"))
        probability = _finite(self.probability, "sample probability")
        if not 0.0 <= probability <= 1.0:
            raise ValueError("sample probability must be between 0 and 1")
        object.__setattr__(self, "probability", probability)
        if isinstance(self.outcome, bool):
            object.__setattr__(self, "outcome", int(self.outcome))
        elif self.outcome not in (0, 1):
            raise ValueError("sample outcome must be 0 or 1")
        observed = _aware_utc(self.observed_at, "sample observed_at")
        settled = _aware_utc(self.settled_at, "sample settled_at")
        if settled < observed:
            raise ValueError("sample settlement cannot precede prediction")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "settled_at", settled)
        object.__setattr__(self, "cluster_id", _nonempty(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "event_id", _nonempty(self.event_id, "event_id"))

    def to_payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "probability": self.probability,
            "outcome": self.outcome,
            "observed_at": self.observed_at.isoformat(),
            "settled_at": self.settled_at.isoformat(),
            "cluster_id": self.cluster_id,
            "event_id": self.event_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CalibrationSample":
        if not isinstance(payload, Mapping):
            raise ValueError("calibration sample must be an object")
        return cls(
            sample_id=str(payload.get("sample_id", "")),
            probability=_finite(payload.get("probability"), "sample probability"),
            outcome=_integer(payload.get("outcome"), "sample outcome"),
            observed_at=_parse_utc(payload.get("observed_at"), "sample observed_at"),
            settled_at=_parse_utc(payload.get("settled_at"), "sample settled_at"),
            cluster_id=str(payload.get("cluster_id", "")),
            event_id=str(payload.get("event_id", "")),
        )


def _logit(probability: float) -> float:
    bounded = min(1.0 - _LOGIT_EPSILON, max(_LOGIT_EPSILON, probability))
    return math.log(bounded / (1.0 - bounded))


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _fit_platt(samples: Sequence[CalibrationSample]) -> tuple[float, float]:
    if len(samples) < CALIBRATION_MIN_FIT_SUPPORT or len({row.outcome for row in samples}) < 2:
        return 0.0, 1.0
    matrix = np.asarray([[_logit(row.probability)] for row in samples], dtype=np.float64)
    outcomes = np.asarray([row.outcome for row in samples], dtype=np.int64)
    estimator = LogisticRegression(
        C=1_000_000.0,
        solver=_SOLVER,
        fit_intercept=True,
        max_iter=_MAX_ITERATIONS,
        tol=_TOLERANCE,
        random_state=0,
    )
    estimator.fit(matrix, outcomes)
    return float(estimator.intercept_[0]), float(estimator.coef_[0][0])


def _apply_platt(probability: float, intercept: float, slope: float) -> float:
    return _sigmoid(intercept + slope * _logit(probability))


def _bootstrap_ece_upper(
    samples: Sequence[CalibrationSample],
    probabilities: Sequence[float],
    *,
    seed_material: str,
) -> float | None:
    if not samples:
        return None
    grouped: dict[str, list[tuple[int, float]]] = {}
    for sample, probability in zip(samples, probabilities, strict=True):
        grouped.setdefault(sample.cluster_id, []).append((sample.outcome, probability))
    keys = sorted(grouped)
    generator = random.Random(
        int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    )
    estimates: list[float] = []
    for _ in range(CALIBRATION_BOOTSTRAP_SAMPLES):
        selected = [
            row
            for _key in keys
            for row in grouped[keys[generator.randrange(len(keys))]]
        ]
        metrics = evaluate_binary_predictions(
            (row[0] for row in selected),
            (row[1] for row in selected),
            ece_bins=DEFAULT_ECE_BINS,
        )
        if metrics.expected_calibration_error is not None:
            estimates.append(metrics.expected_calibration_error)
    if not estimates:
        return None
    estimates.sort()
    return estimates[math.ceil(0.90 * len(estimates)) - 1]


def _bin_from_payload(payload: Mapping[str, Any]) -> CalibrationBin:
    return CalibrationBin(
        bin_number=_integer(payload.get("bin_number"), "bin_number", minimum=1),
        count=_integer(payload.get("count"), "bin count", minimum=1),
        min_probability=_finite(payload.get("min_probability"), "min_probability"),
        max_probability=_finite(payload.get("max_probability"), "max_probability"),
        mean_probability=_finite(payload.get("mean_probability"), "mean_probability"),
        event_rate=_finite(payload.get("event_rate"), "event_rate"),
        absolute_gap=_finite(payload.get("absolute_gap"), "absolute_gap"),
    )


def _metrics_from_payload(payload: Mapping[str, Any]) -> BinaryMetrics:
    bins_raw = payload.get("calibration_bins")
    if not isinstance(bins_raw, list):
        raise ValueError("calibration_bins must be an array")

    def optional(name: str) -> float | None:
        value = payload.get(name)
        return None if value is None else _finite(value, name)

    return BinaryMetrics(
        support=_integer(payload.get("support"), "metrics support"),
        brier_score=optional("brier_score"),
        log_loss=optional("log_loss"),
        expected_calibration_error=optional("expected_calibration_error"),
        auc=optional("auc"),
        accuracy=optional("accuracy"),
        classification_threshold=_finite(
            payload.get("classification_threshold"), "classification_threshold"
        ),
        calibration_bins=tuple(_bin_from_payload(row) for row in bins_raw),
    )


@dataclass(frozen=True)
class DraftCalibrationArtifact:
    calibration_version: str
    model_hash: str
    model_version: str
    model_kind: str
    horizon_minutes: int
    feature_schema_hash: str
    evidence_mode: str
    source_ref: str
    fit_samples: tuple[CalibrationSample, ...]
    evaluation_samples: tuple[CalibrationSample, ...]
    method: str
    intercept: float
    slope: float
    metrics: BinaryMetrics
    ece_upper_bound: float | None
    calibration_hash: str

    @property
    def gate(self) -> CalibrationGate:
        return passes_calibration_gate(
            self.metrics,
            ece_upper_bound=self.ece_upper_bound,
        )

    @property
    def passes_live_gate(self) -> bool:
        return self.evidence_mode == "prospective" and self.gate.passed

    @property
    def support(self) -> int:
        return self.metrics.support

    def apply(self, probability: float) -> float:
        _verify_calibration_artifact(self)
        value = _finite(probability, "probability")
        if not 0.0 <= value <= 1.0:
            raise ValueError("probability must be between 0 and 1")
        return _apply_platt(value, self.intercept, self.slope)

    def to_payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        gate = self.gate
        payload: dict[str, Any] = {
            "calibration_version": self.calibration_version,
            "model_hash": self.model_hash,
            "model_version": self.model_version,
            "model_kind": self.model_kind,
            "horizon_minutes": self.horizon_minutes,
            "feature_schema_hash": self.feature_schema_hash,
            "evidence_mode": self.evidence_mode,
            "source_ref": self.source_ref,
            "fit_samples": [row.to_payload() for row in self.fit_samples],
            "evaluation_samples": [row.to_payload() for row in self.evaluation_samples],
            "method": self.method,
            "intercept": self.intercept,
            "slope": self.slope,
            "metrics": self.metrics.to_payload(),
            "ece_upper_bound": self.ece_upper_bound,
            "gate": {
                "passed": gate.passed,
                "reasons": list(gate.reasons),
                "ece_upper_bound": gate.ece_upper_bound,
                "prospective_required": True,
            },
        }
        if include_hash:
            payload["calibration_hash"] = self.calibration_hash
        return payload


def build_calibration_artifact(
    model: DraftModelArtifact,
    *,
    evidence_mode: str,
    source_ref: str,
    fit_samples: Iterable[CalibrationSample],
    evaluation_samples: Iterable[CalibrationSample],
) -> DraftCalibrationArtifact:
    """Fit Platt parameters and evaluate them on a disjoint chronological cohort."""

    _verify_model_artifact(model)
    if evidence_mode not in CALIBRATION_EVIDENCE_MODES:
        raise ValueError("unsupported calibration evidence mode")
    fit = tuple(sorted(fit_samples, key=lambda row: (row.observed_at, row.sample_id)))
    evaluation = tuple(
        sorted(evaluation_samples, key=lambda row: (row.observed_at, row.sample_id))
    )
    fit_ids = {row.sample_id for row in fit}
    evaluation_ids = {row.sample_id for row in evaluation}
    if len(fit_ids) != len(fit) or len(evaluation_ids) != len(evaluation):
        raise ValueError("calibration sample IDs must be unique")
    if fit_ids & evaluation_ids:
        raise ValueError("fit and evaluation calibration cohorts must be disjoint")
    if fit and evaluation and max(row.settled_at for row in fit) > min(
        row.observed_at for row in evaluation
    ):
        raise ValueError("calibration fit cohort must settle before evaluation starts")

    intercept, slope = _fit_platt(fit)
    calibrated = tuple(
        _apply_platt(row.probability, intercept, slope) for row in evaluation
    )
    metrics = evaluate_binary_predictions(
        (row.outcome for row in evaluation),
        calibrated,
        ece_bins=DEFAULT_ECE_BINS,
    )
    source = _nonempty(source_ref, "source_ref")
    upper = _bootstrap_ece_upper(
        evaluation,
        calibrated,
        seed_material=canonical_hash(
            {
                "source_ref": source,
                "model_hash": model.model_hash,
                "fit": [row.to_payload() for row in fit],
                "evaluation": [row.to_payload() for row in evaluation],
            }
        ),
    )
    artifact = DraftCalibrationArtifact(
        calibration_version=CALIBRATION_ARTIFACT_VERSION,
        model_hash=model.model_hash,
        model_version=model.model_version,
        model_kind=model.model_kind,
        horizon_minutes=model.horizon_minutes,
        feature_schema_hash=model.feature_schema_hash,
        evidence_mode=evidence_mode,
        source_ref=source,
        fit_samples=fit,
        evaluation_samples=evaluation,
        method=CALIBRATION_METHOD,
        intercept=intercept,
        slope=slope,
        metrics=metrics,
        ece_upper_bound=upper,
        calibration_hash="",
    )
    return replace(
        artifact,
        calibration_hash=canonical_hash(artifact.to_payload(include_hash=False)),
    )


def calibration_artifact_from_payload(
    payload: Mapping[str, Any],
) -> DraftCalibrationArtifact:
    if not isinstance(payload, Mapping):
        raise ValueError("calibration artifact payload must be an object")
    fit_raw = payload.get("fit_samples")
    evaluation_raw = payload.get("evaluation_samples")
    metrics_raw = payload.get("metrics")
    if not isinstance(fit_raw, list) or not isinstance(evaluation_raw, list):
        raise ValueError("calibration cohorts must be arrays")
    if not isinstance(metrics_raw, Mapping):
        raise ValueError("calibration metrics must be an object")
    artifact = DraftCalibrationArtifact(
        calibration_version=_nonempty(
            payload.get("calibration_version"), "calibration_version"
        ),
        model_hash=_digest(payload.get("model_hash"), "model_hash"),
        model_version=_nonempty(payload.get("model_version"), "model_version"),
        model_kind=_nonempty(payload.get("model_kind"), "model_kind"),
        horizon_minutes=_integer(
            payload.get("horizon_minutes"), "horizon_minutes", minimum=1
        ),
        feature_schema_hash=_digest(
            payload.get("feature_schema_hash"), "feature_schema_hash"
        ),
        evidence_mode=_nonempty(payload.get("evidence_mode"), "evidence_mode"),
        source_ref=_nonempty(payload.get("source_ref"), "source_ref"),
        fit_samples=tuple(CalibrationSample.from_payload(row) for row in fit_raw),
        evaluation_samples=tuple(
            CalibrationSample.from_payload(row) for row in evaluation_raw
        ),
        method=_nonempty(payload.get("method"), "method"),
        intercept=_finite(payload.get("intercept"), "intercept"),
        slope=_finite(payload.get("slope"), "slope"),
        metrics=_metrics_from_payload(metrics_raw),
        ece_upper_bound=(
            None
            if payload.get("ece_upper_bound") is None
            else _finite(payload.get("ece_upper_bound"), "ece_upper_bound")
        ),
        calibration_hash=_digest(
            payload.get("calibration_hash"), "calibration_hash"
        ),
    )
    _verify_calibration_artifact(artifact)
    gate_payload = payload.get("gate")
    if not isinstance(gate_payload, Mapping):
        raise ValueError("calibration gate evidence must be an object")
    gate = artifact.gate
    if (
        gate_payload.get("passed") is not gate.passed
        or tuple(gate_payload.get("reasons", ())) != gate.reasons
        or gate_payload.get("ece_upper_bound") != gate.ece_upper_bound
        or gate_payload.get("prospective_required") is not True
    ):
        raise ValueError("calibration gate evidence does not recompute")
    return artifact


def _verify_calibration_artifact(artifact: DraftCalibrationArtifact) -> None:
    if artifact.calibration_version != CALIBRATION_ARTIFACT_VERSION:
        raise ValueError("unsupported calibration artifact version")
    if artifact.model_version != MODEL_VERSION:
        raise ValueError("unsupported calibration model version")
    if artifact.model_kind not in {"pure_draft", "context_adjusted"}:
        raise ValueError("unsupported calibration model kind")
    if artifact.horizon_minutes not in LANDMARK_MINUTES:
        raise ValueError("unsupported calibration horizon")
    if artifact.evidence_mode not in CALIBRATION_EVIDENCE_MODES:
        raise ValueError("unsupported calibration evidence mode")
    if artifact.method != CALIBRATION_METHOD:
        raise ValueError("unsupported calibration method")
    if {row.sample_id for row in artifact.fit_samples} & {
        row.sample_id for row in artifact.evaluation_samples
    }:
        raise ValueError("calibration cohorts overlap")
    expected_intercept, expected_slope = _fit_platt(artifact.fit_samples)
    if not math.isclose(artifact.intercept, expected_intercept, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("calibration intercept does not recompute")
    if not math.isclose(artifact.slope, expected_slope, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("calibration slope does not recompute")
    probabilities = tuple(
        _apply_platt(row.probability, artifact.intercept, artifact.slope)
        for row in artifact.evaluation_samples
    )
    metrics = evaluate_binary_predictions(
        (row.outcome for row in artifact.evaluation_samples),
        probabilities,
        ece_bins=DEFAULT_ECE_BINS,
    )
    if canonical_json_bytes(metrics.to_payload()) != canonical_json_bytes(
        artifact.metrics.to_payload()
    ):
        raise ValueError("calibration metrics do not recompute")
    upper = _bootstrap_ece_upper(
        artifact.evaluation_samples,
        probabilities,
        seed_material=canonical_hash(
            {
                "source_ref": artifact.source_ref,
                "model_hash": artifact.model_hash,
                "fit": [row.to_payload() for row in artifact.fit_samples],
                "evaluation": [row.to_payload() for row in artifact.evaluation_samples],
            }
        ),
    )
    if upper != artifact.ece_upper_bound:
        raise ValueError("calibration ECE upper bound does not recompute")
    claimed = _digest(artifact.calibration_hash, "calibration_hash")
    expected = canonical_hash(artifact.to_payload(include_hash=False))
    if not hmac.compare_digest(claimed, expected):
        raise ValueError("calibration artifact hash does not match its evidence")


def assert_model_calibration_compatible(
    model: DraftModelArtifact,
    calibration: DraftCalibrationArtifact,
) -> None:
    _verify_model_artifact(model)
    _verify_calibration_artifact(calibration)
    if (
        calibration.model_hash != model.model_hash
        or calibration.model_version != model.model_version
        or calibration.model_kind != model.model_kind
        or calibration.horizon_minutes != model.horizon_minutes
        or calibration.feature_schema_hash != model.feature_schema_hash
    ):
        raise ValueError("model and calibration artifacts are incompatible")
    if model.status is not ModelStatus.TRAINED:
        raise ValueError("deployment model is not trained")


__all__ = [
    "CALIBRATION_ARTIFACT_VERSION",
    "CALIBRATION_EVIDENCE_MODES",
    "CalibrationSample",
    "DraftCalibrationArtifact",
    "assert_model_calibration_compatible",
    "build_calibration_artifact",
    "calibration_artifact_from_payload",
    "canonical_hash",
    "canonical_json_bytes",
    "model_artifact_from_payload",
]
