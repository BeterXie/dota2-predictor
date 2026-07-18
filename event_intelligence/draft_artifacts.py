"""Immutable, self-verifying deployment artifacts for draft predictions."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from numbers import Real
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression

from .draft_model import (
    DEFAULT_ECE_BINS,
    LANDMARK_MINUTES,
    LEGACY_MODEL_ARTIFACT_VERSION,
    MODEL_ARTIFACT_VERSION,
    MODEL_VERSION,
    BinaryMetrics,
    CalibrationBin,
    CalibrationGate,
    DraftModelArtifact,
    DraftTrainingCorpusRow,
    ModelStatus,
    _MAX_ITERATIONS as MODEL_MAX_ITERATIONS,
    _SOLVER as MODEL_SOLVER,
    _TOLERANCE as MODEL_TOLERANCE,
    _TRAINER_RUNTIME as MODEL_TRAINER_RUNTIME,
    _expected_calibration_error,
    assert_model_artifact_deployable,
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
# Keep only the live and next candidate five-horizon bundles strongly referenced.
_VERIFICATION_CACHE_SIZE = len(LANDMARK_MINUTES) * 2

_MODEL_LEGACY_FIELDS = frozenset(
    {
        "model_version",
        "model_kind",
        "status",
        "reason",
        "horizon_minutes",
        "training_cutoff",
        "support",
        "series_support",
        "min_samples",
        "l2_regularization",
        "solver",
        "max_iterations",
        "tolerance",
        "feature_names",
        "feature_schema_hash",
        "training_input_hash",
        "class_counts",
        "missing_counts",
        "imputation_values",
        "standardization_means",
        "standardization_scales",
        "coefficients",
        "intercept",
        "logit_covariance",
        "model_hash",
    }
)
_MODEL_V2_FIELDS = _MODEL_LEGACY_FIELDS | {
    "artifact_version",
    "trainer_runtime",
    "training_corpus",
}
_TRAINING_CORPUS_ROW_FIELDS = frozenset(
    {
        "match_id",
        "input_snapshot_hash",
        "cutoff",
        "completed_at",
        "result_usable_at",
        "outcome",
        "duration_minutes",
        "series_id",
        "features",
        "missing_features",
    }
)
_CALIBRATION_ARTIFACT_FIELDS = frozenset(
    {
        "calibration_version",
        "model_hash",
        "model_version",
        "model_kind",
        "horizon_minutes",
        "feature_schema_hash",
        "evidence_mode",
        "source_ref",
        "fit_samples",
        "evaluation_samples",
        "method",
        "intercept",
        "slope",
        "metrics",
        "ece_upper_bound",
        "gate",
        "calibration_hash",
    }
)
_CALIBRATION_SAMPLE_FIELDS = frozenset(
    {
        "sample_id",
        "probability",
        "outcome",
        "observed_at",
        "settled_at",
        "cluster_id",
        "event_id",
    }
)
_CALIBRATION_METRIC_FIELDS = frozenset(
    {
        "support",
        "brier_score",
        "log_loss",
        "expected_calibration_error",
        "auc",
        "accuracy",
        "classification_threshold",
        "calibration_bins",
    }
)
_CALIBRATION_BIN_FIELDS = frozenset(
    {
        "bin_number",
        "count",
        "min_probability",
        "max_probability",
        "mean_probability",
        "event_rate",
        "absolute_gap",
    }
)
_CALIBRATION_GATE_FIELDS = frozenset(
    {"passed", "reasons", "ece_upper_bound", "prospective_required"}
)


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
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    result = value
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _nonempty(value: object, field: str) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    return result


def _strict_nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _exact_object(
    value: object,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with string keys")
    actual = frozenset(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown: {', '.join(unknown)}")
        raise ValueError(f"{label} keys do not match ({'; '.join(details)})")
    return value


def _strict_json_object(payload_json: str, label: str) -> Mapping[str, Any]:
    if not isinstance(payload_json, str):
        raise ValueError(f"{label} JSON must be a string")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        payload = json.loads(
            payload_json,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} JSON is invalid") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be an object")
    return payload


def _named_float_pairs(
    value: object,
    field: str,
    *,
    expected_names: tuple[str, ...] | None = None,
) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    result = tuple(
        sorted(
            (
                _strict_nonempty(name, f"{field} name"),
                _finite(number, f"{field}.{name}"),
            )
            for name, number in value.items()
        )
    )
    if expected_names is not None and tuple(name for name, _ in result) != expected_names:
        raise ValueError(f"{field} keys do not match feature_names")
    return result


def _training_corpus_row_from_payload(
    value: object,
    *,
    feature_names: tuple[str, ...],
) -> DraftTrainingCorpusRow:
    payload = _exact_object(
        value,
        _TRAINING_CORPUS_ROW_FIELDS,
        "model training corpus row",
    )
    features_raw = _exact_object(
        payload["features"],
        frozenset(feature_names),
        "model training corpus features",
    )
    features = tuple(
        (
            name,
            None
            if features_raw[name] is None
            else _finite(features_raw[name], f"training feature {name}"),
        )
        for name in feature_names
    )
    missing_raw = payload["missing_features"]
    if not isinstance(missing_raw, list) or any(
        not isinstance(name, str) for name in missing_raw
    ):
        raise ValueError("model training missing_features must be an array of strings")
    expected_missing = [name for name, number in features if number is None]
    if missing_raw != expected_missing:
        raise ValueError("model training missing_features do not match features")
    outcome = _integer(payload["outcome"], "training outcome")
    if outcome not in (0, 1):
        raise ValueError("training outcome must be 0 or 1")
    duration = _finite(payload["duration_minutes"], "training duration_minutes")
    if duration <= 0.0:
        raise ValueError("training duration_minutes must be positive")
    row = DraftTrainingCorpusRow(
        match_id=_integer(payload["match_id"], "training match_id", minimum=1),
        input_snapshot_hash=_digest(
            payload["input_snapshot_hash"],
            "training input_snapshot_hash",
        ),
        cutoff=_parse_utc(payload["cutoff"], "training cutoff"),
        completed_at=_parse_utc(payload["completed_at"], "training completed_at"),
        result_usable_at=_parse_utc(
            payload["result_usable_at"],
            "training result_usable_at",
        ),
        outcome=outcome,
        duration_minutes=duration,
        series_id=_strict_nonempty(payload["series_id"], "training series_id"),
        features=features,
    )
    if canonical_json_bytes(row.to_payload()) != canonical_json_bytes(dict(payload)):
        raise ValueError("model training corpus row is not canonical")
    return row


def model_artifact_from_payload(payload: Mapping[str, Any]) -> DraftModelArtifact:
    """Rehydrate a complete model and verify its canonical model hash."""

    if not isinstance(payload, Mapping):
        raise ValueError("model artifact payload must be an object")
    is_v2 = "artifact_version" in payload
    payload = _exact_object(
        payload,
        _MODEL_V2_FIELDS if is_v2 else _MODEL_LEGACY_FIELDS,
        "model artifact",
    )
    if is_v2 and payload["artifact_version"] != MODEL_ARTIFACT_VERSION:
        raise ValueError("unsupported draft model artifact version")
    if (
        payload["solver"] != MODEL_SOLVER
        or _integer(payload["max_iterations"], "max_iterations", minimum=1)
        != MODEL_MAX_ITERATIONS
        or _finite(payload["tolerance"], "tolerance") != MODEL_TOLERANCE
    ):
        raise ValueError("model solver contract does not match")
    feature_names_raw = payload.get("feature_names")
    if not isinstance(feature_names_raw, list) or not feature_names_raw:
        raise ValueError("model feature_names must be a non-empty array")
    feature_names = tuple(
        _strict_nonempty(value, "feature name") for value in feature_names_raw
    )
    if len(set(feature_names)) != len(feature_names):
        raise ValueError("model feature_names contain duplicates")

    class_counts_raw = _exact_object(
        payload["class_counts"],
        frozenset({"0", "1"}),
        "model class_counts",
    )
    class_counts = tuple(
        (label, _integer(class_counts_raw[str(label)], f"class_counts.{label}"))
        for label in (0, 1)
    )
    missing_raw = _exact_object(
        payload["missing_counts"],
        frozenset(feature_names),
        "model missing_counts",
    )
    missing_counts = tuple(
        (
            name,
            _integer(missing_raw[name], f"missing_counts.{name}"),
        )
        for name in sorted(feature_names)
    )
    covariance_raw = payload["logit_covariance"]
    if not isinstance(covariance_raw, list):
        raise ValueError("model logit_covariance must be an array")
    covariance = tuple(
        tuple(_finite(number, "logit covariance") for number in row)
        for row in covariance_raw
        if isinstance(row, list)
    )
    if len(covariance) != len(covariance_raw):
        raise ValueError("model logit_covariance rows must be arrays")

    if is_v2:
        runtime_raw = _exact_object(
            payload["trainer_runtime"],
            frozenset(name for name, _version in MODEL_TRAINER_RUNTIME),
            "model trainer_runtime",
        )
        trainer_runtime = tuple(
            (
                name,
                _strict_nonempty(runtime_raw[name], f"trainer_runtime.{name}"),
            )
            for name, _version in MODEL_TRAINER_RUNTIME
        )
        corpus_raw = payload["training_corpus"]
        if not isinstance(corpus_raw, list):
            raise ValueError("model training_corpus must be an array")
        training_corpus = tuple(
            _training_corpus_row_from_payload(
                row,
                feature_names=tuple(sorted(feature_names)),
            )
            for row in corpus_raw
        )
    else:
        trainer_runtime = ()
        training_corpus = ()

    artifact = DraftModelArtifact(
        artifact_version=(
            MODEL_ARTIFACT_VERSION if is_v2 else LEGACY_MODEL_ARTIFACT_VERSION
        ),
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
        trainer_runtime=trainer_runtime,
        training_corpus=training_corpus,
        class_counts=class_counts,
        missing_counts=missing_counts,
        imputation_values=_named_float_pairs(
            payload.get("imputation_values"),
            "imputation_values",
            expected_names=tuple(sorted(feature_names)),
        ),
        standardization_means=_named_float_pairs(
            payload.get("standardization_means"),
            "standardization_means",
            expected_names=tuple(sorted(feature_names)),
        ),
        standardization_scales=_named_float_pairs(
            payload.get("standardization_scales"),
            "standardization_scales",
            expected_names=tuple(sorted(feature_names)),
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
    if artifact.status is ModelStatus.TRAINED:
        if tuple(name for name, _ in artifact.coefficients) != tuple(sorted(feature_names)):
            raise ValueError("trained model coefficients do not match feature_names")
        expected = len(feature_names) + 1
        if artifact.intercept is None or len(covariance) != expected or any(
            len(row) != expected for row in covariance
        ):
            raise ValueError("trained model covariance is incomplete")
    _verify_model_artifact(artifact)
    if canonical_json_bytes(artifact.to_payload()) != canonical_json_bytes(dict(payload)):
        raise ValueError("model artifact payload is not canonical")
    return artifact


def load_model_artifact_json(payload_json: str) -> DraftModelArtifact:
    """Strictly parse raw JSON before replaying its model training corpus."""

    payload = _strict_json_object(payload_json, "model artifact")
    return model_artifact_from_payload(payload)


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
        row = _exact_object(
            payload,
            _CALIBRATION_SAMPLE_FIELDS,
            "calibration sample",
        )
        sample = cls(
            sample_id=_strict_nonempty(row["sample_id"], "sample_id"),
            probability=_finite(row["probability"], "sample probability"),
            outcome=_integer(row["outcome"], "sample outcome"),
            observed_at=_parse_utc(row["observed_at"], "sample observed_at"),
            settled_at=_parse_utc(row["settled_at"], "sample settled_at"),
            cluster_id=_strict_nonempty(row["cluster_id"], "cluster_id"),
            event_id=_strict_nonempty(row["event_id"], "event_id"),
        )
        if canonical_json_bytes(sample.to_payload()) != canonical_json_bytes(dict(row)):
            raise ValueError("calibration sample is not canonical")
        return sample


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
        ece, _bins = _expected_calibration_error(
            tuple(row[0] for row in selected),
            tuple(row[1] for row in selected),
            DEFAULT_ECE_BINS,
        )
        estimates.append(ece)
    if not estimates:
        return None
    estimates.sort()
    return estimates[math.ceil(0.90 * len(estimates)) - 1]


def _bin_from_payload(payload: Mapping[str, Any]) -> CalibrationBin:
    payload = _exact_object(
        payload,
        _CALIBRATION_BIN_FIELDS,
        "calibration bin",
    )
    return CalibrationBin(
        bin_number=_integer(payload["bin_number"], "bin_number", minimum=1),
        count=_integer(payload["count"], "bin count", minimum=1),
        min_probability=_finite(payload["min_probability"], "min_probability"),
        max_probability=_finite(payload["max_probability"], "max_probability"),
        mean_probability=_finite(payload["mean_probability"], "mean_probability"),
        event_rate=_finite(payload["event_rate"], "event_rate"),
        absolute_gap=_finite(payload["absolute_gap"], "absolute_gap"),
    )


def _metrics_from_payload(payload: Mapping[str, Any]) -> BinaryMetrics:
    payload = _exact_object(
        payload,
        _CALIBRATION_METRIC_FIELDS,
        "calibration metrics",
    )
    bins_raw = payload["calibration_bins"]
    if not isinstance(bins_raw, list):
        raise ValueError("calibration_bins must be an array")

    def optional(name: str) -> float | None:
        value = payload[name]
        return None if value is None else _finite(value, name)

    return BinaryMetrics(
        support=_integer(payload["support"], "metrics support"),
        brier_score=optional("brier_score"),
        log_loss=optional("log_loss"),
        expected_calibration_error=optional("expected_calibration_error"),
        auc=optional("auc"),
        accuracy=optional("accuracy"),
        classification_threshold=_finite(
            payload["classification_threshold"], "classification_threshold"
        ),
        calibration_bins=tuple(_bin_from_payload(row) for row in bins_raw),
    )


def _verify_calibration_cohorts(
    fit: Sequence[CalibrationSample],
    evaluation: Sequence[CalibrationSample],
) -> None:
    def order_key(row: CalibrationSample) -> tuple[datetime, str]:
        return row.observed_at, row.sample_id

    if tuple(fit) != tuple(sorted(fit, key=order_key)) or tuple(evaluation) != tuple(
        sorted(evaluation, key=order_key)
    ):
        raise ValueError("calibration cohorts must use canonical chronological order")
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
    _verify_calibration_cohorts(fit, evaluation)

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
    payload = _exact_object(
        payload,
        _CALIBRATION_ARTIFACT_FIELDS,
        "calibration artifact",
    )
    fit_raw = payload["fit_samples"]
    evaluation_raw = payload["evaluation_samples"]
    metrics_raw = payload["metrics"]
    if not isinstance(fit_raw, list) or not isinstance(evaluation_raw, list):
        raise ValueError("calibration cohorts must be arrays")
    if not isinstance(metrics_raw, Mapping):
        raise ValueError("calibration metrics must be an object")
    artifact = DraftCalibrationArtifact(
        calibration_version=_strict_nonempty(
            payload["calibration_version"], "calibration_version"
        ),
        model_hash=_digest(payload["model_hash"], "model_hash"),
        model_version=_strict_nonempty(payload["model_version"], "model_version"),
        model_kind=_strict_nonempty(payload["model_kind"], "model_kind"),
        horizon_minutes=_integer(
            payload["horizon_minutes"], "horizon_minutes", minimum=1
        ),
        feature_schema_hash=_digest(
            payload["feature_schema_hash"], "feature_schema_hash"
        ),
        evidence_mode=_strict_nonempty(payload["evidence_mode"], "evidence_mode"),
        source_ref=_strict_nonempty(payload["source_ref"], "source_ref"),
        fit_samples=tuple(CalibrationSample.from_payload(row) for row in fit_raw),
        evaluation_samples=tuple(
            CalibrationSample.from_payload(row) for row in evaluation_raw
        ),
        method=_strict_nonempty(payload["method"], "method"),
        intercept=_finite(payload["intercept"], "intercept"),
        slope=_finite(payload["slope"], "slope"),
        metrics=_metrics_from_payload(metrics_raw),
        ece_upper_bound=(
            None
            if payload["ece_upper_bound"] is None
            else _finite(payload["ece_upper_bound"], "ece_upper_bound")
        ),
        calibration_hash=_digest(payload["calibration_hash"], "calibration_hash"),
    )
    _verify_calibration_artifact(artifact)
    gate_payload = _exact_object(
        payload["gate"],
        _CALIBRATION_GATE_FIELDS,
        "calibration gate evidence",
    )
    gate = artifact.gate
    reasons = gate_payload["reasons"]
    if (
        not isinstance(reasons, list)
        or any(not isinstance(reason, str) for reason in reasons)
        or gate_payload["passed"] is not gate.passed
        or tuple(reasons) != gate.reasons
        or gate_payload["ece_upper_bound"] != gate.ece_upper_bound
        or gate_payload["prospective_required"] is not True
    ):
        raise ValueError("calibration gate evidence does not recompute")
    if canonical_json_bytes(artifact.to_payload()) != canonical_json_bytes(dict(payload)):
        raise ValueError("calibration artifact payload is not canonical")
    return artifact


def load_calibration_artifact_json(payload_json: str) -> DraftCalibrationArtifact:
    """Strictly parse raw calibration JSON before recomputing all evidence."""

    payload = _strict_json_object(payload_json, "calibration artifact")
    return calibration_artifact_from_payload(payload)


@lru_cache(maxsize=_VERIFICATION_CACHE_SIZE)
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
    _verify_calibration_cohorts(
        artifact.fit_samples,
        artifact.evaluation_samples,
    )
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
    assert_model_artifact_deployable(model)
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
    "assert_model_artifact_deployable",
    "assert_model_calibration_compatible",
    "build_calibration_artifact",
    "calibration_artifact_from_payload",
    "canonical_hash",
    "canonical_json_bytes",
    "load_calibration_artifact_json",
    "load_model_artifact_json",
    "model_artifact_from_payload",
]
